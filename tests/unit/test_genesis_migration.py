import json
from pathlib import Path

import pytest
from xian_accounts import Ed25519Account

from xian.genesis_builder import build_bundle_network_genesis, build_genesis_block
from xian.genesis_migration import (
    CLOSED_POLICY,
    amount_text,
    atoms,
    convert_legacy_export,
    convert_legacy_state,
    digest,
    prepare_bundle,
    reconcile,
    strict_json,
    validate_migration_bundle,
)

CONFIGS = Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
OWNER = Ed25519Account("12" * 32)
ADDRESS = OWNER.public_key.upper()


def inputs(tmp_path, *, contract=False):
    snapshot = {
        "schema": "xian.currency_snapshot.v1",
        "chain_id": "old-test",
        "height": 123,
        "block_hash": "ab" * 32,
        "scope": "complete",
        "contracts": ["currency"],
        "total_supply": "10.000000000000000000000000000001",
        "balances": [{"account": ADDRESS, "amount": "10.000000000000000000000000000001"}],
    }
    if contract:
        snapshot["balances"].append({"account": "team_lock", "amount": "5"})
        snapshot["total_supply"] = "15.000000000000000000000000000001"
    raw = json.dumps(snapshot).encode()
    plan = {
        "schema": "xian.balance_migration.v1",
        "source": {
            **{key: snapshot[key] for key in ("chain_id", "height", "block_hash")},
            "sha256": digest(raw),
        },
        "target_chain_id": "new-test",
        "contract_dispositions": {},
    }
    if contract:
        plan["contract_dispositions"]["team_lock"] = {
            "reason": "Synthetic beneficiary reconciliation",
            "balances": {ADDRESS: "5"},
        }
    (tmp_path / "snapshot.json").write_bytes(raw)
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    return snapshot, plan, json.loads((CONFIGS / "contracts_mainnet.json").read_text())


def run(snapshot, plan, bundle):
    raw = json.dumps(snapshot).encode()
    plan["source"]["sha256"] = digest(raw)
    return reconcile(raw, json.dumps(plan).encode(), bundle)


@pytest.mark.parametrize(
    "bad", [1, 1.5, True, "-1", "NaN", "Infinity", "1e2", "01", "0." + "1" * 31, "1" * 62]
)
def test_amount_rejects_lossy_or_invalid_input(bad):
    with pytest.raises(ValueError):
        atoms(bad)


def test_exact_arithmetic_and_duplicate_json():
    amount = "9" * 61 + "." + "9" * 30
    assert amount_text(atoms(amount)) == amount
    with pytest.raises(ValueError, match="duplicate JSON"):
        strict_json('{"a":1,"a":2}')


def test_contract_disposition_preserves_total_and_original_case(tmp_path):
    snapshot, plan, bundle = inputs(tmp_path, contract=True)
    balances, report = run(snapshot, plan, bundle)
    assert balances == {ADDRESS: snapshot["total_supply"]}
    assert report["enrollment_ready"] is False
    assert report["launch_approved"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_contract",
        "user_redirect",
        "supply",
        "duplicate",
        "unfunded_target",
        "contract_sum",
        "same_chain",
        "sample",
    ],
)
def test_fail_closed(tmp_path, mutation):
    snapshot, plan, bundle = inputs(tmp_path, contract=True)
    if mutation == "missing_contract":
        plan["contract_dispositions"] = {}
    if mutation == "user_redirect":
        plan["contract_dispositions"][ADDRESS] = {"reason": "bad", "balances": {"ab" * 32: "10"}}
    if mutation == "supply":
        snapshot["total_supply"] = "16"
    if mutation == "duplicate":
        snapshot["balances"].append(snapshot["balances"][0])
    if mutation == "unfunded_target":
        plan["contract_dispositions"]["team_lock"]["balances"] = {"con_future": "5"}
    if mutation == "contract_sum":
        plan["contract_dispositions"]["team_lock"]["balances"] = {ADDRESS: "4"}
    if mutation == "same_chain":
        plan["target_chain_id"] = snapshot["chain_id"]
    if mutation == "sample":
        snapshot["scope"] = "sample"
    with pytest.raises(ValueError):
        run(snapshot, plan, bundle)


def test_raw_legacy_rows_do_not_mint_allowances():
    balances, excluded = convert_legacy_state(
        [
            {"key": "currency.balances:" + ADDRESS, "value": '"1.25"'},
            {"key": "currency.balances:" + ADDRESS + ":spender", "value": '"999999"'},
            {"key": "currency.balances", "value": '"1"'},
        ]
    )
    assert balances == [{"account": ADDRESS, "amount": "1.25"}]
    assert len(excluded) == 2
    with pytest.raises(ValueError):
        convert_legacy_state([{"key": "currency.balances:" + ADDRESS, "value": 1.25}])


def test_pending_mainnet_is_not_buildable():
    with pytest.raises(ValueError, match="pending"):
        build_genesis_block(founder_private_key="12" * 32, network="mainnet", contracts_dir=CONFIGS)


def test_generated_bundle_roundtrip_and_tamper_detection(tmp_path):
    inputs(tmp_path, contract=True)
    out = tmp_path / "out"
    prepare_bundle(tmp_path / "snapshot.json", tmp_path / "plan.json", CONFIGS, "mainnet", out)
    genesis = build_bundle_network_genesis(
        chain_id="new-test",
        network="mainnet",
        contracts_dir=out,
        genesis_time="2026-01-01T00:00:00Z",
    )
    repeated = build_bundle_network_genesis(
        chain_id="new-test",
        network="mainnet",
        contracts_dir=out,
        genesis_time="2026-01-01T00:00:00Z",
    )
    assert repeated == genesis
    state = {row["key"]: row["value"] for row in genesis["abci_genesis"]["genesis"]}
    assert atoms(str(state["currency.balances:" + ADDRESS])) == atoms(
        "15.000000000000000000000000000001"
    )
    assert state["_accounts.policy"] == CLOSED_POLICY
    assert not any(
        key.startswith(("currency.approvals:", "_accounts.records:", "permit_authorizer."))
        for key in state
    )
    assert not any("team_lock" in key for key in state)
    # Import, commit, reopen, verify old-key authorization and execute a transfer.
    from contracting.storage.driver import Driver

    from xian.account_registry import AccountRegistry
    from xian.execution_engine import build_vm_runtime, execute_vm_contract
    from xian.utils.encoding import canonical_json_text
    from xian.utils.tx import validate_transaction_static

    storage = tmp_path / "state"
    driver = Driver(storage_home=storage)
    for key, value in state.items():
        driver.set(key, value)
    driver.hard_apply("0")
    driver.close()
    driver = Driver(storage_home=storage)
    payload = {
        "sender": ADDRESS,
        "nonce": 0,
        "chain_id": "new-test",
        "chi_supplied": 10000,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"to": "ab" * 32, "amount": 1},
    }
    tx = {
        "payload": payload,
        "metadata": {"signature": OWNER.sign_message(canonical_json_text(payload))},
    }
    validate_transaction_static(
        tx, chain_id="new-test", account_registry=AccountRegistry(driver, "new-test")
    )
    result = execute_vm_contract(
        build_vm_runtime(),
        driver,
        sender=ADDRESS,
        contract_name="currency",
        function_name="transfer",
        kwargs=payload["kwargs"],
        environment={"chain_id": "new-test"},
        meter=False,
    )
    assert result.status_code == 0, result.result
    driver.apply_writes(result.writes)
    driver.hard_apply("1")
    driver.close()
    driver = Driver(storage_home=storage)
    assert atoms(str(driver.get("currency.balances:" + ADDRESS))) == atoms(
        "14.000000000000000000000000000001"
    )
    assert driver.get("currency.balances:" + "ab" * 32) == 1
    assert driver.get("_accounts.policy") == CLOSED_POLICY
    driver.close()
    with pytest.raises(ValueError, match="chain ID"):
        build_bundle_network_genesis(
            chain_id="wrong",
            network="mainnet",
            contracts_dir=out,
            genesis_time="2026-01-01T00:00:00Z",
        )
    with pytest.raises(ValueError, match="must not exist"):
        prepare_bundle(tmp_path / "snapshot.json", tmp_path / "plan.json", CONFIGS, "mainnet", out)
    bundle = strict_json((out / "contracts_mainnet.json").read_bytes())
    (out / "currency.s.py").write_text((out / "currency.s.py").read_text() + "\n# tamper\n")
    with pytest.raises(ValueError, match="digest"):
        validate_migration_bundle(bundle, out)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"-0"', "0"),
        ('"1E+3"', "1000"),
        ('"1E-30"', "0." + "0" * 29 + "1"),
        ('{"__fixed__":"2.50"}', "2.5"),
    ],
)
def test_legacy_decimal_text_is_converted_exactly(raw, expected):
    balances, _ = convert_legacy_state([{"key": "currency.balances:" + ADDRESS, "value": raw}])
    assert balances[0]["amount"] == expected


@pytest.mark.parametrize("raw", ['"-0.01"', '"1E-31"', '"1E+61"', '"1E+999999"', '"NaN"', "1.25"])
def test_legacy_unrepresentable_holdings_block_conversion(raw):
    with pytest.raises(ValueError):
        convert_legacy_state([{"key": "currency.balances:" + ADDRESS, "value": raw}])


def test_hex_named_source_contract_cannot_masquerade_as_user(tmp_path):
    snapshot, plan, bundle = inputs(tmp_path)
    snapshot["contracts"].append("ab" * 32)
    snapshot["balances"][0]["account"] = "ab" * 32
    with pytest.raises(ValueError, match="disposition"):
        run(snapshot, plan, bundle)


def test_export_provenance_hashes_the_bytes_actually_converted(tmp_path, monkeypatch):
    state = tmp_path / "export.json"
    original = json.dumps([{"key": "currency.balances:" + ADDRESS, "value": "10"}]).encode()
    reads = []
    real_read = Path.read_bytes

    def changing_read(path):
        if path == state:
            reads.append(path)
            return original if len(reads) == 1 else b"[]"
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", changing_read)
    report = convert_legacy_export(state, chain_id="old-test", height=1, block_hash="ab" * 32,
        total_supply="10", scope="sample", output_path=tmp_path / "snapshot.json")
    assert report["export_sha256"] == digest(original)
    assert len(reads) == 1
