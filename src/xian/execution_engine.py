from __future__ import annotations

import decimal
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from contracting.runtime_features import (
    RUNTIME_FEATURE_ZK,
    module_ir_uses_runtime_feature,
    runtime_feature_enabled,
)
from xian_runtime_types.decimal import ContractingDecimal

from xian.execution_policy import (
    VM_BYTECODE_VERSION,
    VM_EXECUTION_MODE,
    VM_GAS_SCHEDULE,
)


@dataclass(frozen=True, slots=True)
class VmRuntime:
    runtime_info: dict[str, Any] | None = None

    @property
    def mode(self) -> str:
        return VM_EXECUTION_MODE


@dataclass(frozen=True, slots=True)
class VmPreparedContract:
    contract_name: str
    artifact_hash: str
    imported_contracts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VmExecutionResult:
    status_code: int
    result: Any
    writes: dict[str, Any]
    events: list[dict[str, Any]]
    chi_used: int = 0
    contract_costs: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class VmTransactionResult:
    output: VmExecutionResult
    writes: dict[str, Any]
    chi_used: int
    contract_costs: dict[str, int]
    reads: dict[str, Any]
    prefix_reads: frozenset[str]


_VM_PREPARED_CONTRACTS: dict[tuple[str, str], VmPreparedContract] = {}


def vm_requires_contract_source(
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
) -> bool:
    return (
        contract_name == "submission"
        and function_name == "submit_contract"
        and (not isinstance(kwargs.get("code"), str) or kwargs.get("code") == "")
    )


def vm_contract_source_error(
    contract_name: str,
    function_name: str,
) -> str:
    return f"xian_vm_v1 requires source code for {contract_name}.{function_name}"


def metering_write_keys(
    driver,
    *,
    sender: str,
    currency_contract: str = "currency",
    balances_hash: str = "balances",
) -> set[str]:
    if hasattr(driver, "make_key"):
        key = driver.make_key(currency_contract, balances_hash, [sender])
    else:
        key = f"{currency_contract}.{balances_hash}:{sender}"
    return {key}


def vm_metering_writes(
    driver,
    *,
    sender: str,
    chi_used: int,
    chi_cost: int,
    coerce_balance=None,
    base_writes: dict[str, Any] | None = None,
    currency_contract: str = "currency",
    balances_hash: str = "balances",
) -> dict[str, object]:
    if chi_used <= 0:
        return {}
    balances_key = next(
        iter(
            metering_write_keys(
                driver,
                sender=sender,
                currency_contract=currency_contract,
                balances_hash=balances_hash,
            )
        )
    )
    if coerce_balance is None:
        coerce_balance = _coerce_balance_value
    if base_writes is not None and balances_key in base_writes:
        balance = coerce_balance(base_writes[balances_key])
    else:
        driver_get = getattr(driver, "get", lambda _key: None)
        balance = coerce_balance(driver_get(balances_key))
    to_deduct = ContractingDecimal(chi_used) / ContractingDecimal(chi_cost)
    balance = max(balance - to_deduct, 0)
    return {balances_key: balance}


def _coerce_balance_value(balance):
    if isinstance(balance, ContractingDecimal):
        return balance
    if isinstance(balance, dict):
        return ContractingDecimal(balance.get("__fixed__"))
    if balance is None:
        return 0
    if isinstance(balance, str | float | decimal.Decimal):
        return ContractingDecimal(str(balance))
    return balance


def clear_prepared_contract_cache() -> None:
    _VM_PREPARED_CONTRACTS.clear()


@lru_cache(maxsize=1)
def _load_vm_runtime_bindings():
    try:
        import xian_vm_core
    except ImportError as exc:  # pragma: no cover - exercised via unit tests
        raise ValueError(
            "execution engine 'xian_vm_v1' requires the native "
            "'xian-tech-vm-core' package to be installed"
        ) from exc
    return xian_vm_core


def build_vm_runtime() -> VmRuntime:
    bindings = _load_vm_runtime_bindings()
    runtime_info = bindings.runtime_info()
    if runtime_info.get("vm_profile") != VM_EXECUTION_MODE:
        raise ValueError(
            "xian_vm_v1 native runtime reported an unexpected vm_profile: "
            f"{runtime_info.get('vm_profile')!r}"
        )
    if not bindings.supports_execution_policy(
        VM_BYTECODE_VERSION,
        VM_GAS_SCHEDULE,
    ):
        raise ValueError(
            "xian_vm_v1 native runtime does not support execution policy "
            f"bytecode_version={VM_BYTECODE_VERSION!r} "
            f"gas_schedule={VM_GAS_SCHEDULE!r}"
        )

    return VmRuntime(runtime_info=runtime_info)


def prepare_vm_contract(
    runtime: VmRuntime,
    driver,
    contract_name: str,
) -> VmPreparedContract | None:
    return _prepare_vm_contract_bundle(
        driver=driver,
        contract_name=contract_name,
        stack=(),
    )


def execute_vm_contract(
    runtime: VmRuntime,
    driver,
    *,
    sender: str,
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
    environment: dict[str, Any],
    meter: bool = False,
    chi_budget: int = 0,
    transaction_size_bytes: int = 0,
) -> VmExecutionResult:
    if vm_requires_contract_source(
        contract_name,
        function_name,
        kwargs,
    ):
        return VmExecutionResult(
            status_code=1,
            result=ValueError(vm_contract_source_error(contract_name, function_name)),
            writes={},
            events=[],
            chi_used=0,
            contract_costs={},
        )

    bindings = _load_vm_runtime_bindings()
    context = {
        "signer": sender,
        "caller": sender,
        "this": contract_name,
        "entry": (contract_name, function_name),
        "owner": getattr(driver, "get_owner", lambda _name: None)(contract_name),
        "submission_name": (kwargs.get("name") if contract_name == "submission" else None),
        "now": environment.get("now"),
        "block_num": environment.get("block_num"),
        "block_hash": environment.get("block_hash"),
        "chain_id": environment.get("chain_id"),
    }
    try:
        output = bindings.execute_contract(
            driver=driver,
            contract_name=contract_name,
            function_name=function_name,
            args=[],
            kwargs=kwargs,
            context=context,
            meter=meter,
            chi_budget_raw=max(int(chi_budget), 0) * 1000,
            transaction_size_bytes=max(int(transaction_size_bytes), 0),
        )
    except Exception as exc:
        return VmExecutionResult(
            status_code=1,
            result=exc,
            writes={},
            events=[],
            chi_used=0,
            contract_costs={},
        )

    status_code = int(output.status_code)
    result = output.result
    writes = dict(output.writes)
    events = list(output.events)
    if status_code == 0 and _has_non_string_dict_key((result, writes, events)):
        # The ABCI result encoding and the state root only accept string
        # dict keys, so a value the contract produced with any other key
        # must fail the transaction here instead of failing the block.
        status_code = 1
        result = ValueError("Non-string key types not allowed.")
        writes = {}
        events = []

    return VmExecutionResult(
        status_code=status_code,
        result=result,
        writes=writes,
        events=events,
        chi_used=int(getattr(output, "chi_used", 0)),
        contract_costs=dict(getattr(output, "contract_costs", {}) or {}),
    )


def _has_non_string_dict_key(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    return True
                pending.append(nested)
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
    return False


def execute_vm_transaction(
    runtime: VmRuntime,
    driver,
    *,
    sender: str,
    contract_name: str,
    function_name: str,
    kwargs: dict[str, Any],
    environment: dict[str, Any],
    chi_budget: int,
    chi_cost: int,
    meter: bool,
    transaction_size_bytes: int = 0,
    apply_metering_on_success_only: bool = True,
    apply_metering_writes: bool = True,
    currency_contract: str = "currency",
    balances_hash: str = "balances",
) -> VmTransactionResult:
    base_driver_state = driver.snapshot_state()
    vm_output = execute_vm_contract(
        runtime,
        driver,
        sender=sender,
        contract_name=contract_name,
        function_name=function_name,
        kwargs=kwargs,
        environment=environment,
        meter=meter,
        chi_budget=chi_budget,
        transaction_size_bytes=transaction_size_bytes,
    )
    vm_reads = deepcopy(driver.transaction_reads)
    vm_prefix_reads = frozenset(deepcopy(getattr(driver, "transaction_read_prefixes", set())))

    chi_used = int(vm_output.chi_used or 0)
    contract_costs = dict(vm_output.contract_costs or {})
    merged_writes = dict(vm_output.writes)
    driver.restore_state(base_driver_state)

    if apply_metering_writes and (vm_output.status_code == 0 or not apply_metering_on_success_only):
        if meter and chi_used > 0:
            merged_writes.update(
                vm_metering_writes(
                    driver,
                    sender=sender,
                    chi_used=chi_used,
                    chi_cost=chi_cost,
                    base_writes=merged_writes,
                    currency_contract=currency_contract,
                    balances_hash=balances_hash,
                )
            )

    return VmTransactionResult(
        output=vm_output,
        writes=merged_writes,
        chi_used=chi_used,
        contract_costs=contract_costs,
        reads=vm_reads,
        prefix_reads=vm_prefix_reads,
    )


def _prepare_vm_contract_bundle(
    *,
    driver,
    contract_name: str,
    stack: tuple[str, ...],
) -> VmPreparedContract:
    if contract_name in stack:
        raise ValueError(
            "xian_vm_v1 import graph contains a cycle: " + " -> ".join((*stack, contract_name))
        )

    artifact_hash, module_ir_json = _load_vm_module_ir_json(driver, contract_name)
    module_ir = json.loads(module_ir_json)
    _reject_disabled_runtime_features(driver, contract_name, module_ir)

    cache_key = (contract_name, artifact_hash)
    cached = _VM_PREPARED_CONTRACTS.get(cache_key)
    if cached is not None:
        return cached

    bindings = _load_vm_runtime_bindings()
    bindings.validate_module_ir(module_ir)

    imported_contracts = tuple(
        sorted(
            {
                str(import_spec["module"])
                for import_spec in module_ir.get("imports", [])
                if import_spec.get("module")
            }
        )
    )
    prepared = VmPreparedContract(
        contract_name=contract_name,
        artifact_hash=artifact_hash,
        imported_contracts=imported_contracts,
    )
    _VM_PREPARED_CONTRACTS[cache_key] = prepared

    next_stack = (*stack, contract_name)
    for imported_contract in imported_contracts:
        _prepare_vm_contract_bundle(
            driver=driver,
            contract_name=imported_contract,
            stack=next_stack,
        )

    return prepared


def _reject_disabled_runtime_features(
    driver, contract_name: str, module_ir: dict[str, Any]
) -> None:
    if module_ir_uses_runtime_feature(
        module_ir,
        RUNTIME_FEATURE_ZK,
    ) and not runtime_feature_enabled(
        driver,
        RUNTIME_FEATURE_ZK,
        default_enabled=True,
    ):
        raise ValueError(
            f"contract '{contract_name}' uses zk host syscalls, "
            "but the chain zk runtime feature is disabled"
        )


def _load_vm_module_ir_json(
    driver,
    contract_name: str,
) -> tuple[str, str]:
    get_contract_ir = getattr(driver, "get_contract_ir", None)
    if get_contract_ir is not None:
        module_ir_json = get_contract_ir(
            contract_name,
            vm_profile="xian_vm_v1",
        )
        if isinstance(module_ir_json, str) and module_ir_json:
            return (
                hashlib.sha256(module_ir_json.encode("utf-8")).hexdigest(),
                module_ir_json,
            )

    raise ValueError(
        "xian_vm_v1 requires persisted __xian_ir_v1__ for "
        f"contract '{contract_name}'; stored source is inspection-only"
    )
