"""Build a Xian genesis from a legacy (pre-VM) chain state export.

The legacy node's ``export_state`` tool writes ``exported_state.json`` with the
full contract key/value state (``genesis``) and the per-sender nonces
(``nonces``). This module turns such an export into a genesis for the current
runtime:

1. deploy the target network bundle's system contracts (currency, validators,
   governance, ...) exactly as a fresh network would;
2. recover the authored source of every legacy user contract, recompile it to
   ``xian_vm_v1`` artifacts and install it with its legacy metadata and state;
3. carry legacy state of system contracts the bundle replaces across under
   explicit rules (currency balances replace the bundle allocation, allowances
   move to ``approvals``, ...), and retire legacy-only system contracts;
4. carry every nonce, so no pre-migration transaction can be replayed;
5. hash, verify by replaying the genesis the way ``InitChain`` does, and emit
   a report of every decision.

Nothing is migrated silently: each transform is listed in the report, and any
contract that cannot be recompiled stops the build unless the operator
supplies an override source or explicitly skips it.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from pathlib import Path
from typing import Any

from contracting import constants as contracting_constants
from contracting.artifacts import build_contract_artifacts
from contracting.names import is_safe_contract_name
from contracting.storage.driver import Driver
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import decode, encode
from xian_runtime_types.time import Datetime

from xian.constants import Constants
from xian.genesis_builder import (
    DEFAULT_PRESET_GENESIS_PRIVATE_KEY,
    build_cometbft_genesis,
    build_validator_genesis_entry_from_public_key,
    deploy_bundle_contracts,
    finalize_genesis_block,
    load_contract_bundle_config,
    resolve_contracts_dir,
)
from xian.legacy_source import (
    COMPAT_REWRITES,
    LegacySourceError,
    apply_compat_rewrites,
    recover_legacy_source,
)
from xian.state_root import StateRootCache
from xian.utils.block import apply_state_changes_from_block

INDEX_SEPARATOR = contracting_constants.INDEX_SEPARATOR
DELIMITER = contracting_constants.DELIMITER

LEGACY_CODE_KEY = "__code__"
LEGACY_OWNER_KEY = "__owner__"
LEGACY_DEVELOPER_KEY = "__developer__"
LEGACY_SUBMITTED_KEY = "__submitted__"
LEGACY_METADATA_KEYS = frozenset(
    {LEGACY_CODE_KEY, LEGACY_OWNER_KEY, LEGACY_DEVELOPER_KEY, LEGACY_SUBMITTED_KEY}
)

# Legacy system contracts with no counterpart in current network bundles.
# Their code is not carried over; their state is dropped except where a named
# transform below moves value out of them.
LEGACY_RETIRED_CONTRACTS = frozenset({"masternodes", "stamp_cost", "members", "submission"})
LEGACY_VALIDATOR_CONTRACT = "masternodes"
CURRENCY_CONTRACT = "currency"
FOUNDATION_CONTRACT = "foundation"

DEFAULT_COMPAT_REWRITES = frozenset(COMPAT_REWRITES)
SENDER_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LegacyMigrationError(ValueError):
    pass


# Ledger arithmetic must be exact: legacy balances carry up to 30 decimal
# places, far beyond Python's default 28 significant digits. Any rounding raises.
_EXACT = Context(prec=200, traps=[Inexact, InvalidOperation, Overflow])


def _dsum(values) -> Decimal:
    total = Decimal(0)
    for value in values:
        total = _EXACT.add(total, value)
    return total


# --- Export loading ---------------------------------------------------------


@dataclass
class LegacyContract:
    name: str
    code: str | None = None
    owner: Any = None
    developer: Any = None
    submitted: Any = None
    # variable path after "<contract>." (e.g. "balances:abc") -> value
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class LegacyExport:
    source_hash: str
    source_height: int
    contracts: dict[str, LegacyContract]
    nonces: dict[str, int]
    sha256: str
    entry_count: int


def load_legacy_export(path: Path, *, expected_sha256: str | None = None) -> LegacyExport:
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise LegacyMigrationError(
            f"export sha256 mismatch: expected {expected_sha256}, got {digest}"
        )
    return parse_legacy_export(decode(raw.decode("utf-8")), sha256=digest)


def parse_legacy_export(data: dict[str, Any], *, sha256: str = "") -> LegacyExport:
    if not isinstance(data, dict) or not isinstance(data.get("genesis"), list):
        raise LegacyMigrationError("legacy export must be an object with a 'genesis' list")

    contracts: dict[str, LegacyContract] = {}
    seen: set[str] = set()
    for entry in data["genesis"]:
        key = entry["key"]
        if key in seen:
            raise LegacyMigrationError(f"duplicate key in legacy export: {key}")
        seen.add(key)
        contract_name, separator, variable = key.partition(INDEX_SEPARATOR)
        if not separator or not variable or contract_name.startswith("__"):
            raise LegacyMigrationError(f"unexpected key in legacy export: {key!r}")
        contract = contracts.setdefault(contract_name, LegacyContract(name=contract_name))
        value = entry["value"]
        if variable == LEGACY_CODE_KEY:
            contract.code = value
        elif variable == LEGACY_OWNER_KEY:
            contract.owner = value
        elif variable == LEGACY_DEVELOPER_KEY:
            contract.developer = value
        elif variable == LEGACY_SUBMITTED_KEY:
            contract.submitted = value
        else:
            contract.state[variable] = value

    for name, contract in contracts.items():
        if contract.code is None:
            raise LegacyMigrationError(f"legacy contract {name!r} has state but no __code__")
    if not contracts:
        raise LegacyMigrationError("legacy export contains no contracts")

    nonces: dict[str, int] = {}
    for entry in data.get("nonces", []):
        sender = entry["key"]
        # Legacy exports strip the run-state delimiter; accept either form.
        if sender.endswith(DELIMITER):
            sender = sender[: -len(DELIMITER)]
        value = entry["value"]
        if not SENDER_PATTERN.fullmatch(sender):
            raise LegacyMigrationError(f"unexpected nonce sender: {sender!r}")
        if type(value) is not int or value < 0:
            raise LegacyMigrationError(f"invalid nonce for {sender}: {value!r}")
        if sender in nonces:
            raise LegacyMigrationError(f"duplicate nonce sender: {sender}")
        nonces[sender] = value

    return LegacyExport(
        source_hash=str(data.get("hash", "")),
        source_height=int(data.get("number", 0) or 0),
        contracts=contracts,
        nonces=nonces,
        sha256=sha256,
        entry_count=len(data["genesis"]),
    )


# --- Migration options and report -------------------------------------------


@dataclass
class LegacyValidator:
    public_key: str
    power: int = 10
    name: str = ""


@dataclass
class LegacyMigrationOptions:
    chain_id: str
    genesis_time: str
    network: str = "mainnet"
    contracts_dir: Path | None = None
    runtime_features: dict[str, Any] | None = None
    # None keeps legacy numbering: first new block = legacy height + 1
    initial_height: int | None = None
    founder_private_key: str | None = None
    validators: list[LegacyValidator] | None = None
    compat_rewrites: frozenset[str] = DEFAULT_COMPAT_REWRITES
    resolve_name_collisions: bool = True
    source_overrides: dict[str, str] = field(default_factory=dict)
    skip_contracts: frozenset[str] = frozenset()
    refund_masternode_holdings: bool = True


@dataclass
class LegacyMigrationResult:
    genesis: dict[str, Any]
    report: dict[str, Any]
    recovered_sources: dict[str, str]


def _parse_genesis_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _submission_time(genesis_time: datetime) -> Datetime:
    return Datetime(
        genesis_time.year,
        genesis_time.month,
        genesis_time.day,
        hour=genesis_time.hour,
        minute=genesis_time.minute,
    )


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, ContractingDecimal):
        return Decimal(str(value))
    if isinstance(value, bool) or value is None:
        raise LegacyMigrationError(f"non-numeric balance: {value!r}")
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    raise LegacyMigrationError(f"non-numeric balance: {value!r}")


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_float(item) for item in value)
    return False


def _jsonable(value: Any) -> Any:
    return json.loads(encode(value))


# --- System contract adapters -----------------------------------------------


def _currency_balance_entries(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split legacy ``balances`` into account balances and allowances.

    Legacy currency stored allowances as ``balances:<owner>:<spender>``; the
    current contract keeps them in ``approvals:<owner>:<spender>``.
    """
    balances: dict[str, Any] = {}
    allowances: dict[str, Any] = {}
    prefix = f"balances{DELIMITER}"
    for variable, value in state.items():
        if not variable.startswith(prefix):
            continue
        parts = variable[len(prefix) :].split(DELIMITER)
        if len(parts) == 1:
            balances[parts[0]] = value
        elif len(parts) == 2:
            allowances[DELIMITER.join(parts)] = value
        else:
            raise LegacyMigrationError(f"unexpected currency balance key: {variable}")
    return balances, allowances


def _active_streams(state: dict[str, Any]) -> list[dict[str, Any]]:
    streams: dict[str, dict[str, Any]] = defaultdict(dict)
    prefix = f"streams{DELIMITER}"
    for variable, value in state.items():
        if variable.startswith(prefix):
            stream_id, _, field_name = variable[len(prefix) :].rpartition(DELIMITER)
            streams[stream_id][field_name] = value
    return [
        {
            "stream_id": stream_id,
            "sender": fields.get("sender"),
            "receiver": fields.get("receiver"),
            "rate": _jsonable(fields.get("rate")),
            "closes": _jsonable(fields.get("closes")),
            "claimed": _jsonable(fields.get("claimed")),
        }
        for stream_id, fields in sorted(streams.items())
        if fields.get("status") == "active"
    ]


# --- Build ------------------------------------------------------------------


class _Builder:
    def __init__(
        self,
        export: LegacyExport,
        options: LegacyMigrationOptions,
        driver: Driver,
        submission_time: Datetime,
    ):
        self.export = export
        self.submission_time = submission_time
        self.options = options
        self.driver = driver
        self.report: dict[str, Any] = {
            "contracts": {},
            "system_contracts": {},
            "warnings": [],
        }
        self.recovered_sources: dict[str, str] = {}
        self.accounted_keys = 0

    def warn(self, message: str) -> None:
        self.report["warnings"].append(message)

    def set(self, key: str, value: Any) -> None:
        self.driver.set(key, value, enforce_write_cap=False)

    # -- validators and constructor overrides --

    def resolve_validators(self) -> list[LegacyValidator]:
        if self.options.validators:
            return list(self.options.validators)
        legacy = self.export.contracts.get(LEGACY_VALIDATOR_CONTRACT)
        nodes = legacy.state.get("nodes") if legacy is not None else None
        if not isinstance(nodes, list) or not nodes:
            raise LegacyMigrationError(
                "no validators supplied and the export has no masternodes.nodes list"
            )
        return [LegacyValidator(public_key=node) for node in nodes]

    def constructor_overrides(self, validators: list[LegacyValidator]) -> dict[str, dict]:
        bundle = load_contract_bundle_config(
            self.options.network, contracts_dir=self.options.contracts_dir
        )
        validator_args = next(
            (
                c.get("constructor_args") or {}
                for c in bundle["contracts"]
                if c["name"] == "validators"
            ),
            None,
        )
        overrides: dict[str, dict] = {}
        if validator_args is not None:
            validator_overrides: dict[str, Any] = {
                "genesis_nodes": [v.public_key for v in validators],
            }
            if "genesis_powers" in validator_args:
                validator_overrides["genesis_powers"] = {v.public_key: v.power for v in validators}
            if "genesis_reward_keys" in validator_args:
                validator_overrides["genesis_reward_keys"] = {
                    v.public_key: v.public_key for v in validators
                }
            overrides["validators"] = validator_overrides
        foundation = self.export.contracts.get(FOUNDATION_CONTRACT)
        if foundation is not None and isinstance(foundation.state.get("owner"), str):
            overrides[FOUNDATION_CONTRACT] = {"vk": foundation.state["owner"]}
        return overrides

    # -- user contracts --

    def compile_contract(self, contract: LegacyContract) -> tuple[dict[str, str], dict]:
        name = contract.name
        entry: dict[str, Any] = {"kind": "user"}
        override = self.options.source_overrides.get(name)
        storage_renames: dict[str, str] = {}
        if override is not None:
            source = override
            entry["source"] = "override"
        else:
            recovered = recover_legacy_source(
                name,
                contract.code,
                resolve_collisions=self.options.resolve_name_collisions,
            )
            source = recovered.source
            storage_renames = recovered.storage_renames
            entry["source"] = "recovered"
            if recovered.collision_renames:
                entry["collision_renames"] = recovered.collision_renames
            if storage_renames:
                entry["storage_renames"] = storage_renames
        try:
            artifacts = build_contract_artifacts(
                module_name=name, source=source, lint=True, vm_profile="xian_vm_v1"
            )
        except Exception as first_error:
            if override is not None or not self.options.compat_rewrites:
                raise
            rewritten, applied = apply_compat_rewrites(source, allowed=self.options.compat_rewrites)
            if not applied:
                raise first_error
            artifacts = build_contract_artifacts(
                module_name=name, source=rewritten, lint=True, vm_profile="xian_vm_v1"
            )
            entry["compat_rewrites"] = applied
            entry["compile_error_before_rewrite"] = str(first_error).splitlines()[0]
        self.recovered_sources[name] = artifacts["source"]
        entry["source_sha256"] = hashlib.sha256(artifacts["source"].encode("utf-8")).hexdigest()
        entry["storage_renames_map"] = storage_renames
        return artifacts, entry

    def install_user_contract(self, contract: LegacyContract) -> None:
        name = contract.name
        if name in self.options.skip_contracts:
            self.report["contracts"][name] = {
                "kind": "user",
                "status": "skipped",
                "dropped_keys": len(contract.state) + self._metadata_count(contract),
            }
            self.accounted_keys += len(contract.state) + self._metadata_count(contract)
            return
        if not is_safe_contract_name(name):
            raise LegacyMigrationError(f"legacy contract name {name!r} is not valid on xian_vm_v1")

        artifacts, entry = self.compile_contract(contract)
        storage_renames = entry.pop("storage_renames_map")
        submitted = contract.submitted
        if submitted is None:
            # Never fall back to wall-clock time: the genesis must be reproducible.
            submitted = self.submission_time
            entry["submitted_defaulted_to_genesis_time"] = True
        self.driver.set_contract(
            name=name,
            source=artifacts["source"],
            vm_ir_json=artifacts["vm_ir_json"],
            owner=contract.owner,
            timestamp=submitted,
            developer=contract.developer,
            deployer=None,
            initiator=None,
        )
        float_values = 0
        for variable, value in contract.state.items():
            target = variable
            base, separator, rest = variable.partition(DELIMITER)
            if base in storage_renames:
                target = storage_renames[base] + separator + rest
            if _contains_float(value):
                float_values += 1
            self.set(f"{name}{INDEX_SEPARATOR}{target}", value)
        self.accounted_keys += len(contract.state) + self._metadata_count(contract)
        entry["status"] = "migrated"
        entry["state_keys"] = len(contract.state)
        if float_values:
            entry["float_values"] = float_values
        self.report["contracts"][name] = entry

    @staticmethod
    def _metadata_count(contract: LegacyContract) -> int:
        return 1 + sum(
            value is not None for value in (contract.owner, contract.developer, contract.submitted)
        )

    # -- system contracts --

    def migrate_replaced_system_contract(self, contract: LegacyContract) -> None:
        name = contract.name
        entry: dict[str, Any] = {
            "policy": "replaced_by_bundle_code",
            "legacy_state_keys": len(contract.state),
        }
        if name == CURRENCY_CONTRACT:
            self.migrate_currency(contract, entry)
        else:
            copied, conflicts = 0, {}
            for variable, value in contract.state.items():
                key = f"{name}{INDEX_SEPARATOR}{variable}"
                current = self.driver.get(key)
                if current is not None:
                    if encode(current) != encode(value):
                        conflicts[variable] = {
                            "bundle": _jsonable(current),
                            "legacy": _jsonable(value),
                        }
                    continue
                self.set(key, value)
                copied += 1
            entry["copied_keys"] = copied
            if conflicts:
                entry["bundle_value_kept"] = conflicts
        self.accounted_keys += len(contract.state) + self._metadata_count(contract)
        self.report["system_contracts"][name] = entry

    def migrate_currency(self, contract: LegacyContract, entry: dict[str, Any]) -> None:
        state = contract.state
        balances, allowances = _currency_balance_entries(state)

        # The legacy ledger replaces the bundle's fresh allocation outright.
        bundle_balances = self.driver.items(
            f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}balances{DELIMITER}"
        )
        bundle_approvals = self.driver.items(
            f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}approvals{DELIMITER}"
        )
        for key in [*bundle_balances, *bundle_approvals]:
            self.driver.delete(key)
        entry["bundle_allocation_removed"] = {
            key.rsplit(DELIMITER, 1)[-1]: _jsonable(value) for key, value in bundle_balances.items()
        }

        for account, value in balances.items():
            self.set(f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}balances{DELIMITER}{account}", value)
        for pair, value in allowances.items():
            self.set(f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}approvals{DELIMITER}{pair}", value)
        entry["balances"] = len(balances)
        entry["allowances_moved_to_approvals"] = len(allowances)

        other_copied, conflicts = 0, {}
        for variable, value in state.items():
            if variable.startswith(f"balances{DELIMITER}"):
                continue
            key = f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}{variable}"
            current = self.driver.get(key)
            if current is not None:
                if encode(current) != encode(value):
                    conflicts[variable] = {"bundle": _jsonable(current), "legacy": _jsonable(value)}
                continue
            self.set(key, value)
            other_copied += 1
        entry["other_keys_copied"] = other_copied
        if conflicts:
            entry["bundle_value_kept"] = conflicts

        streams = _active_streams(state)
        if streams:
            entry["active_streams"] = streams
            self.warn(
                f"currency: {len(streams)} active legacy streams were copied as inert state; "
                "the current currency contract has no stream functions, so stream balances "
                "(e.g. dao_funding_stream) stay frozen until governance migrates them"
            )

    def refund_masternode_holdings(self) -> dict[str, Any] | None:
        legacy = self.export.contracts.get(LEGACY_VALIDATOR_CONTRACT)
        if legacy is None:
            return None
        prefix = f"holdings{DELIMITER}"
        holdings = {
            variable[len(prefix) :]: _as_decimal(value)
            for variable, value in legacy.state.items()
            if variable.startswith(prefix) and _as_decimal(value) != 0
        }
        if not holdings:
            return {}
        escrow_key = (
            f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}balances{DELIMITER}{LEGACY_VALIDATOR_CONTRACT}"
        )
        escrow = _as_decimal(self.driver.get(escrow_key) or 0)
        total = _dsum(holdings.values())
        if total > escrow:
            raise LegacyMigrationError(
                f"masternode holdings {total} exceed the masternodes currency balance {escrow}"
            )
        for account, amount in sorted(holdings.items()):
            key = f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}balances{DELIMITER}{account}"
            current = _as_decimal(self.driver.get(key) or 0)
            self.set(key, ContractingDecimal(str(_EXACT.add(current, amount))))
        self.set(escrow_key, ContractingDecimal(str(_EXACT.subtract(escrow, total))))
        return {account: str(amount) for account, amount in sorted(holdings.items())}

    def retire_contract(self, contract: LegacyContract) -> None:
        self.accounted_keys += len(contract.state) + self._metadata_count(contract)
        entry: dict[str, Any] = {"policy": "retired", "dropped_state_keys": len(contract.state)}
        if contract.name == LEGACY_VALIDATOR_CONTRACT:
            entry["legacy_nodes"] = contract.state.get("nodes")
        if contract.name == "stamp_cost":
            entry["legacy_value"] = _jsonable(contract.state.get("S:value"))
        self.report["system_contracts"][contract.name] = entry

    # -- invariants --

    def currency_supply(self) -> Decimal:
        prefix = f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}balances{DELIMITER}"
        return _dsum(
            _as_decimal(value)
            for key, value in self.driver.items(prefix).items()
            if DELIMITER not in key[len(prefix) :]
        )


def _legacy_currency_supply(export: LegacyExport) -> Decimal:
    currency = export.contracts.get(CURRENCY_CONTRACT)
    if currency is None:
        return Decimal(0)
    balances, _ = _currency_balance_entries(currency.state)
    return _dsum(_as_decimal(value) for value in balances.values())


def build_legacy_migration_genesis(
    export: LegacyExport,
    options: LegacyMigrationOptions,
    *,
    storage_home: Path | None = None,
) -> LegacyMigrationResult:
    if storage_home is None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            return build_legacy_migration_genesis(export, options, storage_home=Path(tmp_dir))

    genesis_time = _parse_genesis_time(options.genesis_time)
    contracts_dir = resolve_contracts_dir(options.contracts_dir)
    options.contracts_dir = contracts_dir
    unknown = (set(options.source_overrides) | set(options.skip_contracts)) - set(export.contracts)
    if unknown:
        raise LegacyMigrationError(f"overrides/skips name unknown contracts: {sorted(unknown)}")

    driver = Driver(storage_home=storage_home)
    submission_time = _submission_time(genesis_time)
    builder = _Builder(export, options, driver, submission_time)
    validators = builder.resolve_validators()
    founder_key = options.founder_private_key or DEFAULT_PRESET_GENESIS_PRIVATE_KEY

    deploy_bundle_contracts(
        driver,
        founder_private_key=founder_key,
        network=options.network,
        contracts_dir=contracts_dir,
        constructor_overrides=builder.constructor_overrides(validators),
        runtime_features=options.runtime_features,
        submission_time=submission_time,
    )
    bundle_contracts = set(driver.get_contract_files()) | {
        name for name in export.contracts if driver.has_contract(name)
    }

    failures: dict[str, str] = {}
    for name, contract in sorted(export.contracts.items()):
        try:
            if name in LEGACY_RETIRED_CONTRACTS and name not in bundle_contracts:
                builder.retire_contract(contract)
            elif name in bundle_contracts:
                builder.migrate_replaced_system_contract(contract)
            else:
                builder.install_user_contract(contract)
        except (LegacySourceError, LegacyMigrationError) as exc:
            failures[name] = str(exc)
        except Exception as exc:  # compiler diagnostics surface as generic errors
            failures[name] = str(exc).splitlines()[0] if str(exc) else repr(exc)
    if failures:
        details = "\n".join(f"  {name}: {reason}" for name, reason in sorted(failures.items()))
        raise LegacyMigrationError(
            f"{len(failures)} legacy contracts could not be migrated. Supply a source "
            f"override or skip them explicitly:\n{details}"
        )

    refunds = None
    if options.refund_masternode_holdings:
        refunds = builder.refund_masternode_holdings()
        if refunds:
            builder.report["system_contracts"][LEGACY_VALIDATOR_CONTRACT][
                "holdings_refunded_to_depositors"
            ] = refunds

    # Every key in the export must have been routed somewhere.
    if builder.accounted_keys != export.entry_count:
        raise LegacyMigrationError(
            f"accounted for {builder.accounted_keys} of {export.entry_count} export entries"
        )

    legacy_supply = _legacy_currency_supply(export)
    if CURRENCY_CONTRACT in export.contracts:
        migrated_supply = builder.currency_supply()
        if migrated_supply != legacy_supply:
            raise LegacyMigrationError(
                f"currency supply changed during migration: {legacy_supply} -> {migrated_supply}"
            )
        builder.set(
            f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}metadata{DELIMITER}total_supply",
            ContractingDecimal(str(legacy_supply)),
        )

    for sender, nonce in sorted(export.nonces.items()):
        builder.set(f"{Constants.NONCE_FILENAME}{INDEX_SEPARATOR}{sender}{DELIMITER}", nonce)

    initial_height = (
        options.initial_height if options.initial_height is not None else export.source_height + 1
    )
    if initial_height < 1:
        raise LegacyMigrationError("initial_height must be at least 1")
    abci_genesis = finalize_genesis_block(
        driver,
        founder_private_key=options.founder_private_key,
        number=initial_height - 1,
    )
    if options.founder_private_key is None:
        abci_genesis["origin"] = {"sender": "", "signature": ""}

    contract_entries = builder.report["contracts"]
    report = {
        "schema": "xian-legacy-migration-report/v1",
        "source": {
            "export_sha256": export.sha256,
            "legacy_app_hash": export.source_hash,
            "legacy_height": export.source_height,
            "entries": export.entry_count,
            "contracts": len(export.contracts),
            "nonces": len(export.nonces),
        },
        "target": {
            "chain_id": options.chain_id,
            "network_bundle": options.network,
            "genesis_time": options.genesis_time,
            "initial_height": initial_height,
            "app_hash": abci_genesis["hash"],
            "validators": [v.__dict__ for v in validators],
        },
        "summary": {
            "user_contracts_migrated": sum(
                1 for e in contract_entries.values() if e["status"] == "migrated"
            ),
            "user_contracts_skipped": sorted(
                n for n, e in contract_entries.items() if e["status"] == "skipped"
            ),
            "compat_rewritten": sorted(
                n for n, e in contract_entries.items() if "compat_rewrites" in e
            ),
            "collision_renamed": sorted(
                n for n, e in contract_entries.items() if "collision_renames" in e
            ),
            "source_overrides": sorted(
                n for n, e in contract_entries.items() if e.get("source") == "override"
            ),
            "currency_supply": str(legacy_supply),
            "nonces_carried": len(export.nonces),
            "contracts_with_float_values": sorted(
                n for n, e in contract_entries.items() if e.get("float_values")
            ),
        },
        **builder.report,
    }
    report["contracts_reading_currency_balances"] = sorted(
        name
        for name, source in builder.recovered_sources.items()
        if re.search(r"foreign_contract\s*=\s*['\"]currency['\"]", source)
        and re.search(r"foreign_name\s*=\s*['\"]balances['\"]", source)
    )
    if report["contracts_reading_currency_balances"]:
        builder.warn(
            "contracts that read currency.balances through ForeignHash will no longer see "
            "allowances there (they moved to currency.approvals): "
            + ", ".join(report["contracts_reading_currency_balances"])
        )

    migration_meta = {
        "source_chain_app_hash": export.source_hash,
        "source_height": export.source_height,
        "export_sha256": export.sha256,
    }
    abci_genesis["migration"] = migration_meta
    genesis = build_cometbft_genesis(
        chain_id=options.chain_id,
        abci_genesis=abci_genesis,
        validators=[
            build_validator_genesis_entry_from_public_key(
                public_key_hex=v.public_key, power=v.power, name=v.name
            )
            for v in validators
        ],
        genesis_time=options.genesis_time,
    )
    genesis["initial_height"] = str(initial_height)
    return LegacyMigrationResult(
        genesis=genesis, report=report, recovered_sources=builder.recovered_sources
    )


# --- Verification -----------------------------------------------------------


class _ReplayClient:
    def __init__(self, driver: Driver):
        self.raw_driver = driver


class _ReplayNonces:
    def __init__(self, driver: Driver):
        self.driver = driver

    def set_nonce(self, sender: str, value: int) -> None:
        self.driver.set(
            f"{Constants.NONCE_FILENAME}{INDEX_SEPARATOR}{sender}{DELIMITER}",
            value,
            enforce_write_cap=False,
        )


def verify_migration_genesis(
    genesis: dict[str, Any],
    export: LegacyExport,
    report: dict[str, Any],
    *,
    storage_home: Path | None = None,
) -> dict[str, Any]:
    """Replay ``abci_genesis`` as InitChain does and check it against the export.

    The genesis is round-tripped through JSON first so the check covers what a
    node actually reads from ``genesis.json``. It checks the state root, every
    nonce, every migrated user contract's artifacts and every one of its state
    values, and the currency ledger.
    """
    if storage_home is None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            return verify_migration_genesis(genesis, export, report, storage_home=Path(tmp_dir))

    abci_genesis = decode(encode(genesis))["abci_genesis"]
    driver = Driver(storage_home=storage_home)
    apply_state_changes_from_block(_ReplayClient(driver), _ReplayNonces(driver), abci_genesis)
    driver.flush_cache()
    root = StateRootCache.from_driver(driver).root_hash.hex()
    if root != abci_genesis["hash"]:
        raise LegacyMigrationError(
            f"replayed state root {root} != genesis hash {abci_genesis['hash']}"
        )
    if genesis.get("app_hash") != abci_genesis["hash"]:
        raise LegacyMigrationError("cometbft app_hash does not match abci_genesis.hash")

    problems: list[str] = []
    for sender, nonce in export.nonces.items():
        key = f"{Constants.NONCE_FILENAME}{INDEX_SEPARATOR}{sender}{DELIMITER}"
        if driver.get(key) != nonce:
            problems.append(f"nonce {sender}")

    checked_values = 0
    for name, entry in report["contracts"].items():
        if entry.get("status") != "migrated":
            continue
        contract = export.contracts[name]
        if not driver.has_contract(name) or driver.get_contract_ir(name) is None:
            problems.append(f"contract {name} has no xian_vm_v1 artifacts")
            continue
        renames = entry.get("storage_renames", {})
        for variable, value in contract.state.items():
            base, separator, rest = variable.partition(DELIMITER)
            target = renames.get(base, base) + separator + rest
            actual = driver.get(f"{name}{INDEX_SEPARATOR}{target}")
            # init_chain stores top-level floats as decimals; compare encodings
            expected = ContractingDecimal(str(value)) if isinstance(value, float) else value
            if encode(actual) != encode(expected):
                problems.append(f"value {name}.{variable}")
            checked_values += 1

    currency = export.contracts.get(CURRENCY_CONTRACT)
    if currency is not None:
        balances, allowances = _currency_balance_entries(currency.state)
        refunds = (
            report["system_contracts"]
            .get(LEGACY_VALIDATOR_CONTRACT, {})
            .get("holdings_refunded_to_depositors", {})
        )
        refund_total = _dsum(Decimal(v) for v in refunds.values())
        for account, value in balances.items():
            expected = _EXACT.add(_as_decimal(value), Decimal(refunds.get(account, "0")))
            if account == LEGACY_VALIDATOR_CONTRACT:
                expected = _EXACT.subtract(expected, refund_total)
            actual = driver.get(f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}balances{DELIMITER}{account}")
            if _as_decimal(actual if actual is not None else 0) != expected:
                problems.append(f"balance {account}")
        for pair, value in allowances.items():
            actual = driver.get(f"{CURRENCY_CONTRACT}{INDEX_SEPARATOR}approvals{DELIMITER}{pair}")
            if encode(actual) != encode(value):
                problems.append(f"allowance {pair}")

    if problems:
        preview = ", ".join(problems[:20])
        raise LegacyMigrationError(f"{len(problems)} verification failures: {preview}")
    return {
        "app_hash": root,
        "keys": len(driver.items()),
        "nonces_verified": len(export.nonces),
        "contract_values_verified": checked_values,
    }
