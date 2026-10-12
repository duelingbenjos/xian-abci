"""Exact native-balance migration. No network access or historical code execution."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCALE = 10**30
MAX_ATOMS = 10**91 - 1
ACCOUNT = re.compile(r"[0-9a-fA-F]{64}\Z")
NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,254}\Z")
AMOUNT = re.compile(r"(0|[1-9][0-9]{0,60})(?:\.([0-9]{1,30}))?\Z")
CLOSED_POLICY = {"version": 1, "enrollment_open": False, "transition_chi": 100000}


def strict_json(raw: bytes | str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def atoms(value: str) -> int:
    match = AMOUNT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("amount must be an unsigned fixed decimal string (61/30 digits)")
    result = int(match[1]) * SCALE + int((match[2] or "").ljust(30, "0"))
    if result > MAX_ATOMS:
        raise ValueError("amount exceeds runtime range")
    return result


def amount_text(value: int) -> str:
    if not 0 <= value <= MAX_ATOMS:
        raise ValueError("balance or total exceeds runtime range")
    integer, fraction = divmod(value, SCALE)
    return str(integer) + ("." + str(fraction).zfill(30).rstrip("0") if fraction else "")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _object(value, keys: set[str], label: str):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"invalid {label} fields; expected {sorted(keys)}")


def _account(value):
    if not isinstance(value, str) or not (ACCOUNT.fullmatch(value) or NAME.fullmatch(value)):
        raise ValueError(f"invalid balance account: {value!r}")
    return value


def reconcile(snapshot_raw: bytes, plan_raw: bytes, bundle: dict, *, rehearsal=False):
    snapshot, plan = strict_json(snapshot_raw), strict_json(plan_raw)
    _object(
        snapshot,
        {
            "schema",
            "chain_id",
            "height",
            "block_hash",
            "scope",
            "total_supply",
            "balances",
            "contracts",
        },
        "snapshot",
    )
    _object(plan, {"schema", "source", "target_chain_id", "contract_dispositions"}, "plan")
    if (
        snapshot["schema"] != "xian.currency_snapshot.v1"
        or plan["schema"] != "xian.balance_migration.v1"
    ):
        raise ValueError("unsupported migration schema")
    if snapshot["scope"] not in ("complete", "sample") or (
        snapshot["scope"] == "sample" and not rehearsal
    ):
        raise ValueError("sample snapshots require explicit rehearsal mode")
    if type(snapshot["height"]) is not int or snapshot["height"] <= 0:
        raise ValueError("source height must be positive")
    if not isinstance(snapshot["block_hash"], str) or not ACCOUNT.fullmatch(snapshot["block_hash"]):
        raise ValueError("invalid source block hash")
    for chain in (snapshot["chain_id"], plan["target_chain_id"]):
        if not isinstance(chain, str) or not chain or len(chain) > 50:
            raise ValueError("invalid chain ID")
    if snapshot["chain_id"] == plan["target_chain_id"]:
        raise ValueError("target chain ID must differ from source")
    source = {key: snapshot[key] for key in ("chain_id", "height", "block_hash")}
    source["sha256"] = digest(snapshot_raw)
    if plan["source"] != source:
        raise ValueError("snapshot identity or file digest differs from plan")
    source_contracts = snapshot["contracts"]
    if (
        not isinstance(source_contracts, list)
        or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in source_contracts)
        or len(set(source_contracts)) != len(source_contracts)
    ):
        raise ValueError("invalid source contract inventory")
    source_contracts = set(source_contracts)
    contracts = bundle.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("bundle must have contracts")
    names = [item.get("submit_as", item.get("name")) for item in contracts]
    if len(set(names)) != len(names) or any(
        not isinstance(name, str) or not NAME.fullmatch(name) for name in names
    ):
        raise ValueError("invalid or duplicate target contract names")
    if any(ACCOUNT.fullmatch(name) for name in names):
        raise ValueError("genesis contract names must not collide with account addresses")
    if "currency" not in names or {"team_lock", "permit_authorizer", "_accounts"}.intersection(
        names
    ):
        raise ValueError("invalid fresh-chain contract set")
    if not isinstance(snapshot["balances"], list) or not snapshot["balances"]:
        raise ValueError("snapshot must contain balances")
    dispositions = plan["contract_dispositions"]
    if not isinstance(dispositions, dict):
        raise ValueError("contract_dispositions must be an object")
    source_balances, result, used, rows = {}, {}, set(), []
    for row in snapshot["balances"]:
        _object(row, {"account", "amount"}, "balance")
        account, value = _account(row["account"]), atoms(row["amount"])
        if account in source_balances:
            raise ValueError(f"duplicate balance account: {account}")
        source_balances[account] = value
        if ACCOUNT.fullmatch(account) and account not in source_contracts:
            destinations = {account: value}
        elif value == 0:
            destinations = {}
        else:
            disposition = dispositions.get(account)
            _object(disposition, {"reason", "balances"}, f"disposition for {account}")
            if not isinstance(disposition["reason"], str) or not disposition["reason"].strip():
                raise ValueError("contract disposition needs a review reason")
            if not isinstance(disposition["balances"], dict) or not disposition["balances"]:
                raise ValueError("contract disposition needs destinations")
            destinations = {
                _account(key): atoms(amount) for key, amount in disposition["balances"].items()
            }
            if sum(destinations.values()) != value:
                raise ValueError(f"contract disposition does not conserve {account}")
            used.add(account)
        for destination, amount in destinations.items():
            if (
                not ACCOUNT.fullmatch(destination) or destination in source_contracts
            ) and destination not in names:
                raise ValueError(f"funded contract is not deployed in genesis: {destination}")
            result[destination] = result.get(destination, 0) + amount
            amount_text(result[destination])
        rows.append(
            {
                "source": account,
                "amount": amount_text(value),
                "destinations": {
                    key: amount_text(amount) for key, amount in sorted(destinations.items())
                },
            }
        )
    if set(dispositions) != used:
        raise ValueError("unused contract disposition or attempted user-account reassignment")
    total = sum(source_balances.values())
    if total <= 0 or total != atoms(snapshot["total_supply"]) or total != sum(result.values()):
        raise ValueError("snapshot supply or destination supply mismatch")
    balances = {key: amount_text(value) for key, value in sorted(result.items())}
    report = {
        "schema": "xian.balance_migration_report.v1",
        "source": source,
        "target_chain_id": plan["target_chain_id"],
        "scope": snapshot["scope"],
        "plan_sha256": digest(plan_raw),
        "total_supply": amount_text(total),
        "source_accounts": len(source_balances),
        "destination_accounts": len(balances),
        "enrollment_ready": False,
        "launch_approved": False,
        "reconciliation": rows,
    }
    return balances, report


def prepare_bundle(
    snapshot: Path,
    plan: Path,
    contracts_dir: Path,
    bundle_name: str,
    output_dir: Path,
    *,
    rehearsal=False,
):
    """Publish a new self-contained build directory; never overwrite operator files."""
    if not NAME.fullmatch(bundle_name):
        raise ValueError("invalid bundle name")
    if output_dir.exists():
        raise ValueError("output directory must not exist")
    snapshot_raw, plan_raw = snapshot.read_bytes(), plan.read_bytes()
    bundle = strict_json((contracts_dir / f"contracts_{bundle_name}.json").read_bytes())
    balances, report = reconcile(snapshot_raw, plan_raw, bundle, rehearsal=rehearsal)
    bundle = deepcopy(bundle)
    currency = next(item for item in bundle["contracts"] if item.get("name") == "currency")
    currency.setdefault("constructor_args", {})["initial_balances"] = balances
    bundle["account_registry"] = dict(CLOSED_POLICY)
    files = {}
    for contract in bundle["contracts"]:
        name = contract["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("migration bundles require literal source names")
        extension = bundle.get("extension")
        if extension != ".s.py":
            raise ValueError("unsupported contract extension")
        filename = name + extension
        files[filename] = (contracts_dir / filename).read_bytes()
    bundle["balance_migration"] = {
        "status": "rehearsal" if rehearsal else "reconciled",
        "source_sha256": digest(snapshot_raw),
        "plan_sha256": digest(plan_raw),
        "target_chain_id": report["target_chain_id"],
        "source_hashes": {name: digest(raw) for name, raw in sorted(files.items())},
    }
    bundle["balance_migration"]["configuration_sha256"] = configuration_digest(bundle)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".migration-", dir=output_dir.parent))
    try:
        for name, raw in files.items():
            (temporary / name).write_bytes(raw)
        (temporary / "migration-snapshot.json").write_bytes(snapshot_raw)
        (temporary / "migration-plan.json").write_bytes(plan_raw)
        (temporary / f"contracts_{bundle_name}.json").write_text(
            json.dumps(bundle, indent=2) + "\n"
        )
        (temporary / "migration-report.json").write_text(json.dumps(report, indent=2) + "\n")
        # Do not replace an output another process created while we were preparing.
        if output_dir.exists():
            raise ValueError("output directory appeared during migration")
        temporary.rename(output_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return report


def configuration_digest(bundle):
    return digest(
        json.dumps(
            {key: value for key, value in bundle.items() if key != "balance_migration"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def validate_migration_bundle(bundle: dict, directory: Path, overrides=None):
    migration = bundle.get("balance_migration")
    if migration is None:
        return None
    if not isinstance(migration, dict) or migration.get("status") not in (
        "reconciled",
        "rehearsal",
    ):
        raise ValueError("genesis allocation is pending a reconciled source snapshot")
    if migration.get("configuration_sha256") != configuration_digest(bundle):
        raise ValueError("migration constructor configuration digest mismatch")
    if overrides:
        raise ValueError("migration bundles cannot use constructor overrides")
    snapshot_raw = (directory / "migration-snapshot.json").read_bytes()
    plan_raw = (directory / "migration-plan.json").read_bytes()
    if migration.get("source_sha256") != digest(snapshot_raw) or migration.get(
        "plan_sha256"
    ) != digest(plan_raw):
        raise ValueError("migration input digest mismatch")
    balances, report = reconcile(
        snapshot_raw, plan_raw, bundle, rehearsal=migration["status"] == "rehearsal"
    )
    if migration.get("target_chain_id") != report["target_chain_id"]:
        raise ValueError("migration target chain mismatch")
    currency = next(item for item in bundle["contracts"] if item.get("name") == "currency")
    if currency.get("constructor_args", {}).get("initial_balances") != balances:
        raise ValueError("currency allocation differs from reconciliation")
    expected = {}
    for item in bundle["contracts"]:
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or bundle.get("extension") != ".s.py"
        ):
            raise ValueError("invalid migration source path")
        filename = name + ".s.py"
        expected[filename] = digest((directory / filename).read_bytes())
    if migration.get("source_hashes") != expected:
        raise ValueError("migration contract source digest mismatch")
    if bundle.get("account_registry") != CLOSED_POLICY:
        raise ValueError("migration enrollment must remain disabled")
    return balances, report


def verify_migrated_state(driver, expected):
    if expected is None:
        return
    balances, report = expected
    observed = driver.items(prefix="currency.balances:")
    if {
        key.removeprefix("currency.balances:"): atoms(decimal_text(str(value)))
        for key, value in observed.items()
    } != {key: atoms(value) for key, value in balances.items()}:
        raise ValueError("constructed genesis balances differ from migration")
    if atoms(decimal_text(str(driver.get("currency.metadata:total_supply")))) != atoms(
        report["total_supply"]
    ):
        raise ValueError("constructed genesis supply differs from migration")
    if driver.items(prefix="currency.approvals:") or driver.items(prefix="_accounts.records:"):
        raise ValueError("fresh migration contains approvals or account records")
    if driver.get("_accounts.policy") != CLOSED_POLICY:
        raise ValueError("constructed registry policy differs from closed policy")


def decimal_text(value):
    # Legacy Decimal serializes signed zero and scientific notation.
    # Parse text exactly; never route source amounts through a JSON float.
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("legacy amount must be bounded decimal text or integer")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid legacy decimal") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("negative or non-finite legacy holding")
    if number.is_zero():
        value = "0"
    else:
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        while digits[-1] == 0:
            digits.pop()
            exponent += 1
        if exponent < -30 or len(digits) + exponent > 61:
            raise ValueError("legacy holding cannot be represented exactly by new runtime")
        value = format(Decimal((sign, tuple(digits), exponent)), "f")
    return amount_text(atoms(value))


def convert_legacy_state(rows: list[dict]):
    """Decode legacy BDS state rows, separating allowances from actual holdings.

    This converts a supplied export; it makes no claim that live paginated BDS
    responses form a complete or height-consistent snapshot.
    """
    if not isinstance(rows, list):
        raise ValueError("legacy state must be a row array")
    balances, excluded, seen = [], [], set()
    for row in rows:
        _object(row, {"key", "value"}, "legacy state row")
        key = row["key"]
        if not isinstance(key, str) or key in seen:
            raise ValueError("invalid or duplicate legacy state key")
        seen.add(key)
        if key == "currency.balances":
            excluded.append({"key": key, "reason": "hash root is not an account balance"})
            continue
        if not key.startswith("currency.balances:"):
            raise ValueError("unexpected key outside currency balances")
        parts = key.split(":")
        if len(parts) == 3:
            if not parts[1] or not parts[2]:
                raise ValueError("malformed legacy allowance")
            excluded.append({"key": key, "reason": "legacy allowance, not currency"})
            continue
        if len(parts) != 2:
            raise ValueError("malformed legacy balance key")
        account = _account(parts[1])
        value = row["value"]
        if isinstance(value, str):
            value = strict_json(value)
        if isinstance(value, dict) and set(value) == {"__fixed__"}:
            value = value["__fixed__"]
        if type(value) is int:
            value = str(value)
        amount = decimal_text(value)
        balances.append({"account": account, "amount": amount})
    return sorted(balances, key=lambda item: item["account"]), excluded


def convert_legacy_export(
    state_path: Path,
    *,
    chain_id: str,
    height: int,
    block_hash: str,
    total_supply: str,
    scope: str,
    output_path: Path,
    contract_inventory: Path | None = None,
):
    """Create a reviewable snapshot from operator-supplied legacy BDS rows."""
    if scope == "complete" and contract_inventory is None:
        raise ValueError("complete snapshots require a source contract inventory")
    contracts = strict_json(contract_inventory.read_bytes()) if contract_inventory else []
    if (
        not isinstance(contracts, list)
        or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in contracts)
        or len(set(contracts)) != len(contracts)
    ):
        raise ValueError("invalid source contract inventory")
    # Hash the same bytes we convert, even if the operator's export is replaced
    # while this offline command runs.
    state_raw = state_path.read_bytes()
    balances, excluded = convert_legacy_state(strict_json(state_raw))
    if sum(atoms(row["amount"]) for row in balances) != atoms(total_supply):
        raise ValueError("export does not reconcile with supplied total supply")
    if scope not in ("complete", "sample"):
        raise ValueError("invalid snapshot scope")
    if type(height) is not int or height <= 0 or not ACCOUNT.fullmatch(block_hash):
        raise ValueError("invalid source height or block hash")
    if not isinstance(chain_id, str) or not chain_id or len(chain_id) > 50:
        raise ValueError("invalid source chain ID")
    snapshot = {
        "schema": "xian.currency_snapshot.v1",
        "chain_id": chain_id,
        "height": height,
        "block_hash": block_hash,
        "scope": scope,
        "total_supply": amount_text(atoms(total_supply)),
        "balances": balances,
        "contracts": contracts,
    }
    raw = (json.dumps(snapshot, indent=2) + "\n").encode()
    with output_path.open("xb") as handle:
        handle.write(raw)
    return {
        "snapshot_sha256": digest(raw),
        "export_sha256": digest(state_raw),
        "balance_accounts": len(balances),
        "excluded": excluded,
        "scope": scope,
        "source_consensus_verified": False,
    }
