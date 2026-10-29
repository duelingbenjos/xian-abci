from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracting.artifacts import build_contract_artifacts
from contracting.local import BUILTIN_SUBMISSION_SOURCE_PATH
from contracting.runtime_features import normalize_runtime_features
from contracting.storage.driver import Driver
from xian_accounts import Ed25519Account
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.config_paths import (
    resolve_contracts_dir as resolve_configs_contracts_dir,
)
from xian.execution_engine import VmRuntime, build_vm_runtime, execute_vm_contract
from xian.runtime_features import (
    RuntimeFeatureResolution,
    genesis_runtime_feature_entries,
    install_driver_runtime_features,
)
from xian.state_root import StateRootCache

TEMPLATE_ARG_PATTERN = re.compile(r"%%(.*?)%%")
DEFAULT_CONSENSUS_PARAMS = {
    "block": {
        "max_bytes": "22020096",
        "max_gas": "-1",
        "time_iota_ms": "1000",
    },
    "evidence": {
        "max_age_num_blocks": "100000",
        "max_age_duration": "172800000000000",
        "max_bytes": "1048576",
    },
    "validator": {
        "pub_key_types": ["ed25519"],
    },
    "version": {},
}
DEFAULT_PRESET_GENESIS_PRIVATE_KEY = (
    "1111111111111111111111111111111111111111111111111111111111111111"
)


def hash_state_changes(state_changes: list[dict[str, Any]]) -> str:
    ordered_state_changes = sorted(state_changes, key=lambda item: item["key"])
    digest = hashlib.sha3_256()
    digest.update(encode(ordered_state_changes).encode("utf-8"))
    return digest.hexdigest()


def resolve_contracts_dir(contracts_dir: Path | None = None) -> Path:
    if contracts_dir is not None:
        return contracts_dir.resolve()
    return resolve_configs_contracts_dir()


def load_contract_bundle_config(
    network: str,
    *,
    contracts_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_contracts_dir = resolve_contracts_dir(contracts_dir)
    config_path = resolved_contracts_dir / f"contracts_{network}.json"
    with open(config_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _genesis_submission_time() -> Datetime:
    now = datetime.today()
    return Datetime(now.year, now.month, now.day, hour=now.hour, minute=now.minute)


def _stage_native_submission_contract(driver: Driver, *, submission_time: Datetime) -> None:
    source = BUILTIN_SUBMISSION_SOURCE_PATH.read_text(encoding="utf-8")
    artifacts = build_contract_artifacts(
        module_name="submission",
        source=source,
        lint=False,
        vm_profile="xian_vm_v1",
    )
    driver.set_contract(
        name="submission",
        source=artifacts["source"],
        vm_ir_json=artifacts["vm_ir_json"],
        timestamp=submission_time,
    )


def _deploy_native_genesis_contract(
    *,
    runtime: VmRuntime,
    driver: Driver,
    contract_name: str,
    source: str,
    owner: str | None,
    constructor_args: dict[str, Any] | None,
    submission_time: Datetime | None = None,
) -> None:
    output = execute_vm_contract(
        runtime,
        driver,
        sender="sys",
        contract_name="submission",
        function_name="submit_contract",
        kwargs={
            "name": contract_name,
            "code": source,
            "owner": owner,
            "constructor_args": constructor_args or {},
        },
        environment={
            "now": submission_time if submission_time is not None else _genesis_submission_time()
        },
        meter=False,
    )
    if output.status_code != 0:
        raise ValueError(f"Failed to deploy genesis contract {contract_name}: {output.result}")
    driver.apply_writes(output.writes)


def _find_validators_contract(contract_config: dict[str, Any]) -> dict[str, Any]:
    for contract in contract_config["contracts"]:
        if contract.get("name") == "validators":
            return contract
    raise ValueError("contract bundle does not define validators seed data")


def render_template_values(value: Any, substitutions: dict[str, str]) -> Any:
    if isinstance(value, str):
        match = TEMPLATE_ARG_PATTERN.search(value)
        if match is None:
            return value
        replacement = substitutions[match.group(1)]
        return value.replace(match.group(0), replacement)

    if isinstance(value, list):
        return [render_template_values(item, substitutions) for item in value]

    if isinstance(value, dict):
        return {key: render_template_values(item, substitutions) for key, item in value.items()}

    return value


def deploy_bundle_contracts(
    driver: Driver,
    *,
    founder_private_key: str,
    network: str,
    contracts_dir: Path,
    constructor_overrides: dict[str, dict[str, Any]] | None = None,
    runtime_features: dict[str, Any] | None = None,
    submission_time: Datetime | None = None,
) -> None:
    """Stage submission and deploy a network bundle's contracts into ``driver``.

    The writes stay pending in the driver so callers can layer further state
    on top before ``finalize_genesis_block`` hashes and signs the result.
    """
    resolved_runtime_features = normalize_runtime_features(runtime_features)
    install_driver_runtime_features(
        driver,
        RuntimeFeatureResolution(
            features=resolved_runtime_features,
            source="genesis_builder",
        ),
    )
    runtime = build_vm_runtime()
    if submission_time is None:
        submission_time = _genesis_submission_time()
    _stage_native_submission_contract(driver, submission_time=submission_time)

    contract_config = load_contract_bundle_config(
        network,
        contracts_dir=contracts_dir,
    )

    wallet = Ed25519Account(founder_private_key)
    substitutions = {
        "founder_privkey": founder_private_key,
        "founder_private_key": founder_private_key,
        "founder_public_key": wallet.public_key,
    }
    extension = contract_config["extension"]

    for contract in contract_config["contracts"]:
        contract_name = render_template_values(contract["name"], substitutions)
        submit_as = contract.get("submit_as")
        if submit_as is not None:
            contract_name = render_template_values(submit_as, substitutions)

        contract_path = contracts_dir / (
            render_template_values(contract["name"], substitutions) + extension
        )
        code = contract_path.read_text(encoding="utf-8")
        owner = render_template_values(contract.get("owner"), substitutions)
        constructor_args = render_template_values(contract.get("constructor_args"), substitutions)
        override_args = (
            constructor_overrides.get(contract_name) or constructor_overrides.get(contract["name"])
            if constructor_overrides is not None
            else None
        )
        if override_args:
            if constructor_args is None:
                constructor_args = {}
            constructor_args.update(override_args)

        if not driver.has_contract(contract_name):
            _deploy_native_genesis_contract(
                runtime=runtime,
                driver=driver,
                contract_name=contract_name,
                source=code,
                owner=owner,
                constructor_args=constructor_args,
                submission_time=submission_time,
            )

    for entry in genesis_runtime_feature_entries(resolved_runtime_features):
        driver.set(entry["key"], entry["value"])


def finalize_genesis_block(
    driver: Driver,
    *,
    founder_private_key: str | None,
    number: int | str = "0",
) -> dict[str, Any]:
    """Commit the driver's pending writes and render them as a signed genesis block."""
    genesis_block = {
        "hash": "",
        "number": str(number),
        "genesis": [],
        "origin": {
            "signature": "",
            "sender": "",
        },
    }

    driver.hard_apply("0")
    driver.flush_cache()
    for key, value in driver.items().items():
        genesis_block["genesis"].append({"key": key, "value": value})
    genesis_block["genesis"] = sorted(
        genesis_block["genesis"],
        key=lambda item: item["key"],
    )
    genesis_block["hash"] = StateRootCache.from_driver(driver).root_hash.hex()
    if founder_private_key:
        wallet = Ed25519Account(founder_private_key)
        genesis_block["origin"]["sender"] = wallet.public_key
        genesis_block["origin"]["signature"] = wallet.sign_msg(
            hash_state_changes(genesis_block["genesis"])
        )
    return genesis_block


def _build_genesis_block(
    *,
    founder_private_key: str,
    network: str,
    contracts_dir: Path,
    storage_home: Path,
    constructor_overrides: dict[str, dict[str, Any]] | None = None,
    runtime_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    driver = Driver(storage_home=storage_home)
    deploy_bundle_contracts(
        driver,
        founder_private_key=founder_private_key,
        network=network,
        contracts_dir=contracts_dir,
        constructor_overrides=constructor_overrides,
        runtime_features=runtime_features,
    )
    return finalize_genesis_block(driver, founder_private_key=founder_private_key)


def build_genesis_block(
    *,
    founder_private_key: str,
    network: str = "devnet",
    contracts_dir: Path | None = None,
    storage_home: Path | None = None,
    constructor_overrides: dict[str, dict[str, Any]] | None = None,
    runtime_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_contracts_dir = resolve_contracts_dir(contracts_dir)

    if storage_home is not None:
        return _build_genesis_block(
            founder_private_key=founder_private_key,
            network=network,
            contracts_dir=resolved_contracts_dir,
            storage_home=storage_home.resolve(),
            constructor_overrides=constructor_overrides,
            runtime_features=runtime_features,
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        return _build_genesis_block(
            founder_private_key=founder_private_key,
            network=network,
            contracts_dir=resolved_contracts_dir,
            storage_home=Path(tmp_dir),
            constructor_overrides=constructor_overrides,
            runtime_features=runtime_features,
        )


def build_validator_genesis_entry(
    *,
    priv_validator_key: dict[str, Any],
    power: int | str = 10,
    name: str = "",
) -> dict[str, Any]:
    return {
        "address": priv_validator_key["address"],
        "pub_key": priv_validator_key["pub_key"],
        "power": str(power),
        "name": name,
    }


def build_validator_genesis_entry_from_public_key(
    *,
    public_key_hex: str,
    power: int | str = 10,
    name: str = "",
) -> dict[str, Any]:
    public_key_bytes = bytes.fromhex(public_key_hex)
    address_bytes = hashlib.sha256(public_key_bytes).digest()[:20]
    return {
        "address": address_bytes.hex().upper(),
        "pub_key": {
            "type": "tendermint/PubKeyEd25519",
            "value": base64.b64encode(public_key_bytes).decode("ascii"),
        },
        "power": str(power),
        "name": name,
    }


def derive_genesis_validators_from_bundle(
    *,
    network: str,
    contracts_dir: Path | None = None,
    validator_power: int | str | None = None,
) -> list[dict[str, Any]]:
    contract_config = load_contract_bundle_config(
        network,
        contracts_dir=contracts_dir,
    )
    validators_contract = _find_validators_contract(contract_config)
    constructor_args = validators_contract.get("constructor_args") or {}
    genesis_nodes = constructor_args.get("genesis_nodes")
    if not isinstance(genesis_nodes, list) or not genesis_nodes:
        raise ValueError("contract bundle validators seed data must define genesis_nodes")

    configured_powers = constructor_args.get("genesis_powers") or {}
    default_node_power = constructor_args.get("default_node_power", 10)

    return [
        build_validator_genesis_entry_from_public_key(
            public_key_hex=public_key_hex,
            power=(
                validator_power
                if validator_power is not None
                else configured_powers.get(public_key_hex, default_node_power)
            ),
        )
        for public_key_hex in genesis_nodes
    ]


def build_cometbft_genesis(
    *,
    chain_id: str,
    abci_genesis: dict[str, Any],
    validators: list[dict[str, Any]] | None = None,
    genesis_time: str | None = None,
) -> dict[str, Any]:
    resolved_genesis_time = genesis_time
    if resolved_genesis_time is None:
        resolved_genesis_time = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )

    return {
        "genesis_time": resolved_genesis_time,
        "chain_id": chain_id,
        "initial_height": str(abci_genesis.get("number", "0")),
        "consensus_params": deepcopy(DEFAULT_CONSENSUS_PARAMS),
        "validators": validators or [],
        "app_hash": str(abci_genesis.get("hash", "")),
        "abci_genesis": abci_genesis,
    }


def build_single_validator_genesis(
    *,
    chain_id: str,
    priv_validator_key: dict[str, Any],
    founder_private_key: str,
    network: str = "local",
    validator_name: str = "",
    validator_power: int | str = 10,
    registration_fee: int = 100000,
    contracts_dir: Path | None = None,
    runtime_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    founder_wallet = Ed25519Account(founder_private_key)
    return build_local_network_genesis(
        chain_id=chain_id,
        founder_private_key=founder_private_key,
        validators=[
            {
                "account_public_key": founder_wallet.public_key,
                "name": validator_name,
                "power": validator_power,
                "priv_validator_key": priv_validator_key,
            }
        ],
        network=network,
        registration_fee=registration_fee,
        contracts_dir=contracts_dir,
        runtime_features=runtime_features,
    )


def build_local_network_genesis(
    *,
    chain_id: str,
    founder_private_key: str,
    validators: list[dict[str, Any]],
    network: str = "local",
    registration_fee: int = 100000,
    contracts_dir: Path | None = None,
    runtime_features: dict[str, Any] | None = None,
    validator_constructor_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not validators:
        raise ValueError("at least one validator is required")

    contract_config = load_contract_bundle_config(
        network,
        contracts_dir=contracts_dir,
    )
    validators_contract = _find_validators_contract(contract_config)
    seeded_validator_args = validators_contract.get("constructor_args") or {}
    founder_wallet = Ed25519Account(founder_private_key)
    genesis_nodes = [validator["account_public_key"] for validator in validators]
    genesis_powers = {
        validator["account_public_key"]: validator.get("power", 10) for validator in validators
    }
    genesis_reward_keys = {
        validator["account_public_key"]: (
            validator.get("reward_key") or validator["account_public_key"]
        )
        for validator in validators
    }
    validator_overrides = {
        "genesis_nodes": genesis_nodes,
        "genesis_registration_fee": registration_fee,
    }
    if "genesis_powers" in seeded_validator_args:
        validator_overrides["genesis_powers"] = genesis_powers
    if "genesis_reward_keys" in seeded_validator_args:
        validator_overrides["genesis_reward_keys"] = genesis_reward_keys
    if validator_constructor_overrides:
        validator_overrides.update(validator_constructor_overrides)
    abci_genesis = build_genesis_block(
        founder_private_key=founder_private_key,
        network=network,
        contracts_dir=contracts_dir,
        constructor_overrides={
            "currency": {"vk": founder_wallet.public_key},
            "foundation": {"vk": founder_wallet.public_key},
            "validators": dict(validator_overrides),
        },
        runtime_features=runtime_features,
    )
    validator_entries = [
        build_validator_genesis_entry(
            priv_validator_key=validator["priv_validator_key"],
            power=validator.get("power", 10),
            name=validator.get("name", ""),
        )
        for validator in validators
    ]
    return build_cometbft_genesis(
        chain_id=chain_id,
        abci_genesis=abci_genesis,
        validators=validator_entries,
    )


def build_bundle_network_genesis(
    *,
    chain_id: str,
    network: str,
    contracts_dir: Path | None = None,
    genesis_time: str | None = None,
    runtime_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    abci_genesis = build_genesis_block(
        founder_private_key=DEFAULT_PRESET_GENESIS_PRIVATE_KEY,
        network=network,
        contracts_dir=contracts_dir,
        runtime_features=runtime_features,
    )
    abci_genesis["origin"] = {"sender": "", "signature": ""}
    return build_cometbft_genesis(
        chain_id=chain_id,
        abci_genesis=abci_genesis,
        validators=derive_genesis_validators_from_bundle(
            network=network,
            contracts_dir=contracts_dir,
        ),
        genesis_time=genesis_time,
    )


def write_genesis_block(path: Path, genesis_block: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(encode(genesis_block))


def update_cometbft_genesis(genesis_path: Path, *, abci_genesis: dict[str, Any]) -> None:
    with open(genesis_path, "r", encoding="utf-8") as handle:
        cometbft_genesis = json.load(handle)

    cometbft_genesis["abci_genesis"] = abci_genesis
    cometbft_genesis["initial_height"] = str(abci_genesis["number"])
    cometbft_genesis["app_hash"] = str(abci_genesis.get("hash", ""))

    with open(genesis_path, "w", encoding="utf-8") as handle:
        handle.write(encode(cometbft_genesis))
