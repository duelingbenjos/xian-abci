from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from contracting.compilation import parser
from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

from cometbft.abci.v1beta1.types_pb2 import ResponseQuery
from xian.constants import Constants as c
from xian.execution_engine import (
    execute_vm_contract,
    prepare_vm_contract,
)
from xian.services.bds.candles import get_candle_source_spec
from xian.utils.block import (
    get_committed_latest_block,
    get_latest_block_height,
    get_latest_block_nanos,
    nanoseconds_to_utc_datetime,
)
from xian.utils.encoding import encode_abci_json, encode_str

DEFAULT_KEYS_QUERY_LIMIT = 100
MAX_KEYS_QUERY_LIMIT = 200
DEFAULT_LIST_QUERY_LIMIT = 100
MAX_LIST_QUERY_LIMIT = 1000
DEFAULT_DEX_CANDLE_INTERVAL_SECONDS = 60
MAX_DEX_CANDLE_INTERVAL_SECONDS = 604800


@dataclass(frozen=True, slots=True)
class OffsetPagination:
    limit: int = DEFAULT_LIST_QUERY_LIMIT
    offset: int = 0


@dataclass(frozen=True, slots=True)
class BdsQueryOptions:
    limit: int = DEFAULT_LIST_QUERY_LIMIT
    offset: int = 0
    after_id: int | None = None
    after_note_index: int = 0


@dataclass(frozen=True, slots=True)
class QueryContext:
    app: Any
    path_parts: list[str]
    params: dict[str, str]
    route: str
    key: str

    @property
    def raw_driver(self):
        return self.app.client.raw_driver


@dataclass(frozen=True, slots=True)
class QueryResult:
    result: Any
    key: str | None = None


class UnknownQueryPath(ValueError):
    pass


def _query_params(path_parts: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for path in path_parts:
        if "=" not in path:
            continue
        key, value = path.split("=", 1)
        params[key] = value
    return params


def _submission_iso(value) -> str | None:
    if isinstance(value, dict):
        time_value = value.get("__time__")
        if isinstance(time_value, (list, tuple)) and len(time_value) >= 6:
            year, month, day, hour, minute, second = time_value[:6]
            return (
                f"{int(year):04d}-{int(month):02d}-{int(day):02d}T"
                f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}Z"
            )
    return None


def _bounded_int_param(
    params: dict[str, str],
    key: str,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    try:
        value = int(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _offset_pagination(
    params: dict[str, str],
    *,
    default_limit: int = DEFAULT_LIST_QUERY_LIMIT,
    max_limit: int = MAX_LIST_QUERY_LIMIT,
) -> OffsetPagination:
    return OffsetPagination(
        limit=_bounded_int_param(
            params,
            "limit",
            default=default_limit,
            minimum=0,
            maximum=max_limit,
        ),
        offset=_bounded_int_param(
            params,
            "offset",
            default=0,
            minimum=0,
        ),
    )


def _bds_query_options(params: dict[str, str]) -> BdsQueryOptions:
    pagination = _offset_pagination(params)
    after_id = params.get("after_id")
    parsed_after_id = None
    if after_id is not None:
        try:
            parsed_after_id = max(int(after_id), 0)
        except (TypeError, ValueError):
            parsed_after_id = None

    return BdsQueryOptions(
        limit=pagination.limit,
        offset=pagination.offset,
        after_id=parsed_after_id,
        after_note_index=_bounded_int_param(
            params,
            "after_note_index",
            default=0,
            minimum=0,
        ),
    )


def _duration_seconds_param(
    params: dict[str, str],
    key: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int = MAX_DEX_CANDLE_INTERVAL_SECONDS,
) -> int:
    raw = params.get(key)
    if raw is None:
        return default

    value_text = raw.strip().lower()
    multiplier = 1
    for suffix, suffix_multiplier in (
        ("s", 1),
        ("m", 60),
        ("h", 3600),
        ("d", 86400),
        ("w", 604800),
    ):
        if value_text.endswith(suffix):
            value_text = value_text[: -len(suffix)]
            multiplier = suffix_multiplier
            break

    try:
        value = int(value_text) * multiplier
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _optional_timestamp_param(params: dict[str, str], key: str) -> datetime | None:
    raw = params.get(key)
    if raw is None:
        return None

    value = raw.strip()
    if not value:
        return None

    try:
        if value.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _keys_query_after_key(prefix: str, after: str | None) -> str | None:
    if after is None:
        return None
    normalized = str(after).strip()
    if not normalized:
        return None
    if normalized.startswith(prefix):
        return normalized
    delimiter = "" if prefix.endswith(":") else ":"
    return f"{prefix}{delimiter}{normalized}"


def _keys_query_suffix(prefix: str, full_key: str) -> str:
    delimiter = "" if prefix.endswith(":") else ":"
    prefix_with_delimiter = f"{prefix}{delimiter}"
    if full_key.startswith(prefix_with_delimiter):
        return full_key[len(prefix_with_delimiter) :]
    if full_key.startswith(prefix):
        return full_key[len(prefix) :].lstrip(":")
    return full_key


def _sort_contract_records(
    records: list[dict],
    *,
    sort_key: str,
    descending: bool,
) -> list[dict]:
    if sort_key == "name":
        return sorted(
            records,
            key=lambda record: (
                str(record.get("name", "")).lower(),
                str(record.get("submitted_at") or ""),
            ),
            reverse=descending,
        )

    return sorted(
        records,
        key=lambda record: (
            str(record.get("submitted_at") or ""),
            str(record.get("name", "")).lower(),
        ),
        reverse=descending,
    )


def _make_vm_query_environment(ctx: QueryContext) -> dict[str, Any]:
    block_meta = ctx.app.current_block_meta if isinstance(ctx.app.current_block_meta, dict) else {}
    block_nanos = block_meta.get("nanos")
    if block_nanos is None:
        try:
            block_nanos = get_latest_block_nanos(ctx.raw_driver.storage_home)
        except Exception:
            block_nanos = 0
    block_height = block_meta.get("height") or 0
    block_hash = block_meta.get("hash")
    if block_hash is None:
        block_hash = hashlib.sha3_256(
            (
                f"query:block:{getattr(ctx.app, 'chain_id', '')}:"
                f"{block_height}:{int(block_nanos or 0)}"
            ).encode("utf-8")
        ).hexdigest()
    now = Datetime._from_datetime(nanoseconds_to_utc_datetime(int(block_nanos or 0)))
    return {
        "block_hash": block_hash,
        "block_num": block_height,
        "__input_hash": hashlib.sha3_256(
            f"query:{ctx.route}:{ctx.key}:{int(block_nanos or 0)}".encode("utf-8")
        ).hexdigest(),
        "__xian_execution_mode__": "xian_vm_v1",
        "now": now,
        "chain_id": getattr(ctx.app, "chain_id", None),
    }


def _call_contract_view(
    ctx: QueryContext,
    contract_name: str,
    function_name: str,
    *,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    runtime = ctx.app.execution_runtime
    has_contract = getattr(ctx.raw_driver, "has_contract", None)
    if has_contract is not None and not has_contract(contract_name):
        return None
    if has_contract is None:
        get_contract_ir = getattr(ctx.raw_driver, "get_contract_ir", None)
        contract_ir = get_contract_ir(contract_name) if get_contract_ir else None
        if ctx.raw_driver.get_contract_source(contract_name) is None and contract_ir is None:
            return None

    state = ctx.raw_driver.snapshot_state()
    try:
        prepare_vm_contract(runtime, ctx.raw_driver, contract_name)
        output = execute_vm_contract(
            runtime,
            ctx.raw_driver,
            sender=ctx.key or "__query__",
            contract_name=contract_name,
            function_name=function_name,
            kwargs=dict(kwargs or {}),
            environment=_make_vm_query_environment(ctx),
            meter=False,
        )
        if output.status_code != 0:
            logger.error(f"VM query failed for {contract_name}.{function_name}: {output.result!r}")
            return None
        return output.result
    finally:
        ctx.raw_driver.restore_state(state)


def _pending_validators_votes(
    raw_driver,
    *,
    limit: int = DEFAULT_LIST_QUERY_LIMIT,
    offset: int = 0,
) -> list[dict]:
    total_votes = raw_driver.get("validators.total_votes") or 0
    items: list[dict] = []
    for proposal_id in range(total_votes, 0, -1):
        record = raw_driver.get(f"validators.votes:{proposal_id}")
        if not isinstance(record, dict):
            continue
        if record.get("finalized") is True:
            continue
        if record.get("status") != "pending":
            continue
        entry = dict(record)
        entry["proposal_id"] = proposal_id
        items.append(entry)
    if offset > 0:
        items = items[offset:]
    if limit >= 0:
        items = items[:limit]
    return items


def _validators_vote(raw_driver, proposal_id: int) -> dict | None:
    record = raw_driver.get(f"validators.votes:{proposal_id}")
    if not isinstance(record, dict):
        return None
    entry = dict(record)
    entry["proposal_id"] = proposal_id
    return entry


def _validators_vote_records(raw_driver, proposal_id: int) -> list[dict]:
    record = _validators_vote(raw_driver, proposal_id)
    if not isinstance(record, dict):
        return []

    snapshot = record.get("member_snapshot")
    if not isinstance(snapshot, list):
        snapshot = record.get("voters")
    if not isinstance(snapshot, list):
        snapshot = []

    records: list[dict] = []
    for voter in snapshot:
        if not isinstance(voter, str):
            continue
        records.append(
            {
                "proposal_id": proposal_id,
                "voter": voter,
                "vote": raw_driver.get(f"validators.vote_records:{proposal_id}:{voter}"),
                "weight": raw_driver.get(f"validators.vote_weights:{proposal_id}:{voter}") or 0,
            }
        )
    return records


def _contract_ir_for_metadata(raw_driver, contract_name: str) -> dict[str, Any] | None:
    get_contract_ir = getattr(raw_driver, "get_contract_ir", None)
    if get_contract_ir is None:
        return None

    module_ir_json = get_contract_ir(contract_name)
    if not isinstance(module_ir_json, str) or not module_ir_json:
        return None

    try:
        module_ir = json.loads(module_ir_json)
    except json.JSONDecodeError:
        logger.warning(f"invalid contract IR metadata for {contract_name}")
        return None

    if not isinstance(module_ir, dict):
        return None
    return module_ir


def _methods_for_contract_ir(module_ir: dict[str, Any]) -> list[dict[str, Any]]:
    methods: list[dict[str, Any]] = []
    for function in module_ir.get("functions", []):
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or name.startswith("__"):
            continue

        arguments: list[dict[str, Any]] = []
        for parameter in function.get("parameters", []):
            if not isinstance(parameter, dict):
                continue
            parameter_name = parameter.get("name")
            if not isinstance(parameter_name, str):
                continue
            argument = {"name": parameter_name}
            annotation = parameter.get("annotation")
            if isinstance(annotation, str):
                argument["type"] = annotation
            arguments.append(argument)

        methods.append({"name": name, "arguments": arguments})
    return methods


def _variables_for_contract_ir(module_ir: dict[str, Any]) -> dict[str, list[str]]:
    variables: list[str] = []
    hashes: list[str] = []

    for declaration in module_ir.get("global_declarations", []):
        if not isinstance(declaration, dict) or declaration.get("node") != "storage_decl":
            continue
        name = declaration.get("name")
        storage_type = declaration.get("storage_type")
        if not isinstance(name, str):
            continue
        if storage_type == "Variable":
            variables.append(name.removeprefix("__"))
        elif storage_type == "Hash":
            hashes.append(name.removeprefix("__"))

    return {"variables": variables, "hashes": hashes}


def _resolve_contract_info(ctx: QueryContext, contract_name: str) -> dict[str, Any]:
    return {
        "name": contract_name,
        "owner": ctx.raw_driver.get_owner(contract_name),
        "developer": ctx.raw_driver.get_contract_developer(contract_name),
        "deployer": ctx.raw_driver.get_contract_deployer(contract_name),
        "initiator": ctx.raw_driver.get_contract_initiator(contract_name),
        "submitted_at": _submission_iso(ctx.raw_driver.get_time_submitted(contract_name)),
        "has_source": ctx.raw_driver.get_contract_source(contract_name) is not None,
    }


def _resolve_contract_listing(ctx: QueryContext) -> dict[str, Any]:
    pagination = _offset_pagination(ctx.params)
    sort_key = ctx.params.get("sort", "submitted_at").strip().lower()
    if sort_key not in {"submitted_at", "created_at", "name"}:
        sort_key = "submitted_at"
    if sort_key == "created_at":
        sort_key = "submitted_at"
    descending = ctx.params.get("order", "desc").strip().lower() != "asc"

    contract_names = sorted(set(ctx.app.client.get_contracts()))
    records: list[dict] = []
    for contract_name in contract_names:
        submitted_value = ctx.raw_driver.get(f"{contract_name}.__submitted__")
        records.append(
            {
                "name": contract_name,
                "submitted_at": _submission_iso(submitted_value),
                "developer": ctx.raw_driver.get(f"{contract_name}.__developer__"),
                "has_source": ctx.raw_driver.get_contract_source(contract_name) is not None,
            }
        )

    sorted_records = _sort_contract_records(
        records,
        sort_key=sort_key,
        descending=descending,
    )
    return {
        "items": sorted_records[pagination.offset : pagination.offset + pagination.limit],
        "total": len(sorted_records),
        "limit": pagination.limit,
        "offset": pagination.offset,
        "sort": sort_key,
        "order": "desc" if descending else "asc",
    }


def _resolve_perf_status(ctx: QueryContext) -> dict[str, Any]:
    result = ctx.app.profiler.snapshot()
    result["parallel_execution_enabled"] = bool(ctx.app.parallel_block_executor.enabled)
    result["parallel_execution_workers"] = int(ctx.app.parallel_block_executor.workers)
    result["parallel_execution_min_transactions"] = int(
        ctx.app.parallel_block_executor.min_batch_size
    )
    result["parallel_execution_max_speculative_waves"] = int(
        ctx.app.parallel_block_executor.max_speculative_waves
    )
    result["parallel_execution_min_wave_acceptance_ratio"] = float(
        ctx.app.parallel_block_executor.min_wave_acceptance_ratio
    )
    result["parallel_execution_low_acceptance_min_wave_size"] = int(
        ctx.app.parallel_block_executor.low_acceptance_min_wave_size
    )
    result["parallel_execution_access_estimates_enabled"] = bool(
        ctx.app.parallel_block_executor.use_access_estimates
    )
    return result


def _resolve_pending_unbonds(ctx: QueryContext) -> list[dict] | None:
    pending_ids = _call_contract_view(
        ctx,
        "validators",
        "get_pending_unbond_ids",
        kwargs={"owner": ctx.key},
    )
    if pending_ids is None:
        return None
    result: list[dict] = []
    for unbond_id in pending_ids or []:
        unbond = _call_contract_view(
            ctx,
            "validators",
            "get_pending_unbond",
            kwargs={"unbond_id": unbond_id},
        )
        if isinstance(unbond, dict):
            entry = dict(unbond)
            entry["unbond_id"] = unbond_id
            result.append(entry)
    return result


def _resolve_keys_query(ctx: QueryContext) -> QueryResult:
    prefix = ctx.path_parts[1]
    limit = _bounded_int_param(
        ctx.params,
        "limit",
        default=DEFAULT_KEYS_QUERY_LIMIT,
        minimum=1,
        maximum=MAX_KEYS_QUERY_LIMIT,
    )
    after = ctx.params.get("after")
    full_after_key = _keys_query_after_key(prefix, after)
    list_of_keys, has_more = ctx.raw_driver.scan_keys_from_disk(
        prefix,
        limit=limit,
        after_key=full_after_key,
    )
    items = [_keys_query_suffix(prefix, full_key) for full_key in list_of_keys]
    return QueryResult(
        result={
            "prefix": prefix,
            "items": items,
            "limit": limit,
            "after": after if after else None,
            "next_after": items[-1] if has_more and items else None,
            "has_more": has_more,
        },
        key=prefix,
    )


def _current_bds_block_height(ctx: QueryContext) -> int | None:
    if isinstance(ctx.app.current_block_meta, dict):
        height = ctx.app.current_block_meta.get("height")
        if isinstance(height, int):
            return height
    return get_latest_block_height()


async def _handle_get(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=ctx.raw_driver.get(ctx.path_parts[1]))


async def _handle_health(_ctx: QueryContext) -> QueryResult:
    return QueryResult(result="OK")


async def _handle_get_next_nonce(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=ctx.app.nonce_storage.get_next_nonce(ctx.key))


async def _handle_contract_source(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=ctx.raw_driver.get_contract_source(ctx.key))


async def _handle_contract_ir(ctx: QueryContext) -> QueryResult:
    get_contract_ir = getattr(ctx.raw_driver, "get_contract_ir", None)
    if get_contract_ir is None:
        return QueryResult(result=None)
    return QueryResult(result=get_contract_ir(ctx.key))


async def _handle_contract_methods(ctx: QueryContext) -> QueryResult:
    module_ir = _contract_ir_for_metadata(ctx.raw_driver, ctx.key)
    if module_ir is not None:
        return QueryResult(result={"methods": _methods_for_contract_ir(module_ir)})

    contract_source = ctx.raw_driver.get_contract_source(ctx.key)
    if contract_source is None:
        return QueryResult(result=None)
    return QueryResult(result={"methods": parser.methods_for_contract(contract_source)})


async def _handle_contract_vars(ctx: QueryContext) -> QueryResult:
    module_ir = _contract_ir_for_metadata(ctx.raw_driver, ctx.key)
    if module_ir is not None:
        return QueryResult(result=_variables_for_contract_ir(module_ir))

    contract_source = ctx.raw_driver.get_contract_source(ctx.key)
    if contract_source is None:
        return QueryResult(result=None)
    return QueryResult(result=parser.variables_for_contract(contract_source))


async def _handle_contract_info(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=_resolve_contract_info(ctx, ctx.key))


async def _handle_contracts(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=_resolve_contract_listing(ctx))


async def _handle_ping(_ctx: QueryContext) -> QueryResult:
    return QueryResult(result={"status": "online"})


async def _handle_perf_status(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=_resolve_perf_status(ctx))


async def _handle_validators_policy(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=_call_contract_view(
            ctx,
            "validators",
            "get_policy_config",
        )
    )


async def _handle_validators_active(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=_call_contract_view(
            ctx,
            "validators",
            "get_active_validators",
        )
    )


async def _handle_validators_candidates(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=_call_contract_view(
            ctx,
            "validators",
            "get_pending_candidates",
        )
    )


async def _handle_validators_validator(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=_call_contract_view(
            ctx,
            "validators",
            "get_validator",
            kwargs={"account": ctx.key},
        )
    )


async def _handle_validators_pending_unbonds(
    ctx: QueryContext,
) -> QueryResult:
    return QueryResult(result=_resolve_pending_unbonds(ctx))


async def _handle_validators_open_votes(ctx: QueryContext) -> QueryResult:
    pagination = _offset_pagination(ctx.params)
    return QueryResult(
        result=_pending_validators_votes(
            ctx.raw_driver,
            limit=pagination.limit,
            offset=pagination.offset,
        )
    )


async def _handle_validators_vote(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=_validators_vote(ctx.raw_driver, int(ctx.path_parts[1])))


async def _handle_validators_vote_records(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=_validators_vote_records(
            ctx.raw_driver,
            int(ctx.path_parts[1]),
        )
    )


async def _handle_simulate_tx(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=await ctx.app.simulator.simulate_encoded_transaction(ctx.path_parts[1])
    )


async def _handle_state_patch_bundles(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=ctx.app.state_patch_manager.get_local_bundle_inventory())


async def _handle_scheduled_state_patches(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=ctx.app.state_patch_manager.get_scheduled_patch_inventory(int(ctx.path_parts[1]))
    )


async def _handle_keys(ctx: QueryContext) -> QueryResult:
    return _resolve_keys_query(ctx)


def _require_bds(ctx: QueryContext):
    if not hasattr(ctx.app, "bds"):
        raise RuntimeError("BDS service is not initialized")
    return ctx.app.bds


async def _handle_blocks(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(result=await _require_bds(ctx).get_blocks(options.limit, options.offset))


async def _handle_bds_status(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=await _require_bds(ctx).get_status(
            current_block_height=_current_bds_block_height(ctx)
        )
    )


async def _handle_bds_spool(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_spool_entries(
            options.limit,
            options.offset,
        )
    )


async def _handle_block(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_block(int(ctx.key)))


async def _handle_block_by_hash(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_block_by_hash(ctx.key))


async def _handle_tx(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_tx(ctx.key))


async def _handle_txs_for_block(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_txs_for_block(ctx.key))


async def _handle_txs_by_sender(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_txs_by_sender(
            ctx.key,
            options.limit,
            options.offset,
        )
    )


async def _handle_addresses(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result={
            "available": True,
            "items": await _require_bds(ctx).get_recent_addresses(
                options.limit,
                options.offset,
            ),
            "limit": options.limit,
            "offset": options.offset,
        }
    )


async def _handle_txs_by_contract(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_txs_by_contract(
            ctx.key,
            options.limit,
            options.offset,
        )
    )


async def _handle_events_for_tx(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_events_for_tx(ctx.key))


async def _handle_shielded_output_tags(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    tag_kind = ctx.params.get("kind", "sync_hint").strip().lower()
    if tag_kind not in {"sync_hint", "discovery_tag"}:
        tag_kind = "sync_hint"
    return QueryResult(
        result={
            "available": True,
            "items": await _require_bds(ctx).get_shielded_output_tags(
                ctx.key,
                options.limit,
                options.offset,
                kind=tag_kind,
                after_id=options.after_id,
            ),
            "limit": options.limit,
            "offset": options.offset,
        }
    )


async def _handle_shielded_wallet_history(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    tag_kind = ctx.params.get("kind", "sync_hint").strip().lower()
    if tag_kind not in {"sync_hint", "discovery_tag"}:
        tag_kind = "sync_hint"
    return QueryResult(
        result={
            "available": True,
            "items": await _require_bds(ctx).get_shielded_wallet_history(
                ctx.key,
                options.limit,
                options.after_note_index,
                kind=tag_kind,
            ),
            "limit": options.limit,
            "after_note_index": options.after_note_index,
        }
    )


async def _handle_events(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_events(
            ctx.path_parts[1],
            ctx.path_parts[2],
            options.limit,
            options.offset,
            after_id=options.after_id,
        )
    )


async def _handle_recent_events(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result={
            "available": True,
            "items": await _require_bds(ctx).get_recent_events(
                options.limit,
                options.offset,
            ),
            "limit": options.limit,
            "offset": options.offset,
        }
    )


async def _handle_dex_candles(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    spec = get_candle_source_spec(ctx.params.get("source"))

    contract = ctx.params.get("contract")
    if contract is not None:
        spec = spec.with_contract(contract)

    market_id, _market_id_int = spec.normalize_market_id(ctx.key)
    interval_seconds = _duration_seconds_param(
        ctx.params,
        "interval",
        default=DEFAULT_DEX_CANDLE_INTERVAL_SECONDS,
    )
    return QueryResult(
        result=await _require_bds(ctx).get_dex_candles(
            market_id,
            source_spec=spec,
            interval_seconds=interval_seconds,
            limit=options.limit,
            offset=options.offset,
            start_time=_optional_timestamp_param(ctx.params, "start"),
            end_time=_optional_timestamp_param(ctx.params, "end"),
        )
    )


async def _handle_state(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_state(
            ctx.key,
            options.limit,
            options.offset,
        )
    )


async def _handle_token_balances(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    include_zero = ctx.params.get("include_zero", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    return QueryResult(
        result=await _require_bds(ctx).get_token_balances(
            ctx.key,
            options.limit,
            options.offset,
            include_zero=include_zero,
        )
    )


async def _handle_token_contracts(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_token_contracts(
            options.limit,
            options.offset,
        )
    )


async def _handle_state_history(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_state_history(
            ctx.key,
            options.limit,
            options.offset,
        )
    )


async def _handle_state_previous(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_state_previous(ctx.key))


async def _handle_state_for_tx(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_state_for_tx(ctx.key))


async def _handle_state_for_block(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_state_for_block(ctx.key))


async def _handle_state_patches(ctx: QueryContext) -> QueryResult:
    options = _bds_query_options(ctx.params)
    return QueryResult(
        result=await _require_bds(ctx).get_state_patches(
            options.limit,
            options.offset,
        )
    )


async def _handle_state_patches_for_block(ctx: QueryContext) -> QueryResult:
    return QueryResult(
        result=await _require_bds(ctx).get_state_patches_for_block(int(ctx.path_parts[1]))
    )


async def _handle_state_patch(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_state_patch_by_hash(ctx.key))


async def _handle_state_changes_for_patch(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_state_changes_for_patch(ctx.key))


async def _handle_developer_rewards(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_developer_rewards(ctx.key))


async def _handle_contract_summary(ctx: QueryContext) -> QueryResult:
    return QueryResult(result=await _require_bds(ctx).get_contract_summary(ctx.key))


CORE_QUERY_HANDLERS = {
    "get": _handle_get,
    "health": _handle_health,
    "get_next_nonce": _handle_get_next_nonce,
    "contract_source": _handle_contract_source,
    "contract_ir": _handle_contract_ir,
    "contract_methods": _handle_contract_methods,
    "contract_vars": _handle_contract_vars,
    "contract_info": _handle_contract_info,
    "contracts": _handle_contracts,
    "ping": _handle_ping,
    "perf_status": _handle_perf_status,
    "simulate_tx": _handle_simulate_tx,
    "state_patch_bundles": _handle_state_patch_bundles,
    "scheduled_state_patches": _handle_scheduled_state_patches,
    "keys": _handle_keys,
    "validators_policy": _handle_validators_policy,
    "validators_active": _handle_validators_active,
    "validators_candidates": _handle_validators_candidates,
    "validators_validator": _handle_validators_validator,
    "validators_pending_unbonds": _handle_validators_pending_unbonds,
    "validators_open_votes": _handle_validators_open_votes,
    "validators_vote": _handle_validators_vote,
    "validators_vote_records": _handle_validators_vote_records,
}


BDS_QUERY_HANDLERS = {
    "blocks": _handle_blocks,
    "bds_status": _handle_bds_status,
    "bds_spool": _handle_bds_spool,
    "block": _handle_block,
    "block_by_hash": _handle_block_by_hash,
    "tx": _handle_tx,
    "txs_for_block": _handle_txs_for_block,
    "txs_by_sender": _handle_txs_by_sender,
    "addresses": _handle_addresses,
    "txs_by_contract": _handle_txs_by_contract,
    "events_for_tx": _handle_events_for_tx,
    "shielded_output_tags": _handle_shielded_output_tags,
    "shielded_wallet_history": _handle_shielded_wallet_history,
    "events": _handle_events,
    "recent_events": _handle_recent_events,
    "dex_candles": _handle_dex_candles,
    "state": _handle_state,
    "token_contracts": _handle_token_contracts,
    "token_balances": _handle_token_balances,
    "state_history": _handle_state_history,
    "state_previous": _handle_state_previous,
    "state_for_tx": _handle_state_for_tx,
    "state_for_block": _handle_state_for_block,
    "state_patches": _handle_state_patches,
    "state_patches_for_block": _handle_state_patches_for_block,
    "state_patch": _handle_state_patch,
    "state_changes_for_patch": _handle_state_changes_for_patch,
    "developer_rewards": _handle_developer_rewards,
    "contract_summary": _handle_contract_summary,
}


async def _execute_query(ctx: QueryContext) -> QueryResult:
    handler = CORE_QUERY_HANDLERS.get(ctx.route)
    if handler is not None:
        return await handler(ctx)

    if ctx.app.bds_enabled:
        handler = BDS_QUERY_HANDLERS.get(ctx.route)
        if handler is not None:
            return await handler(ctx)

    raise UnknownQueryPath(f"Unknown query path: {ctx.route}")


def _encode_query_value(result: Any) -> tuple[bytes | None, str | None]:
    if result is None:
        return None, None
    if isinstance(result, bool):
        return encode_str("True" if result else "False"), "bool"
    if isinstance(result, str):
        return encode_str(result), "str"
    if isinstance(result, int):
        return encode_str(str(result)), "int"
    if isinstance(result, float) or isinstance(result, ContractingDecimal):
        return encode_str(str(result)), "decimal"
    if isinstance(result, dict):
        return encode_abci_json(result), "dict"
    if isinstance(result, list):
        return encode_abci_json(result), "list"
    return encode_str(str(result)), "str"


async def query(self, req) -> ResponseQuery:
    """
    Query the application state
    Request Ex. http://localhost:26657/abci_query?path="path"
    (Yes you need to quote the path)
    """

    # Xian.query holds the state-transition lock across this read and dispatch.
    # Use the LMDB marker: current_block_meta may describe an uncommitted block,
    # and the auxiliary JSON mirror can lag a successful commit.
    height = get_committed_latest_block(self.client.raw_driver)["height"]
    if req.height not in (0, height):
        return ResponseQuery(
            code=c.ErrorCode,
            height=height,
            log=(
                f"Unsupported query height {req.height}; only latest committed "
                f"height {height} is available (height=0 selects latest)"
            ),
        )
    if req.prove:
        return ResponseQuery(
            code=c.ErrorCode,
            height=height,
            log="Merkle proof queries are not supported; use prove=false",
        )

    logger.debug(req.path)
    path_parts = [part for part in req.path.split("/") if part]
    route = path_parts[0] if path_parts else ""
    key = path_parts[1] if len(path_parts) > 1 else ""
    ctx = QueryContext(
        app=self,
        path_parts=path_parts,
        params=_query_params(path_parts),
        route=route,
        key=key,
    )

    try:
        outcome = await _execute_query(ctx)
        value, type_of_data = _encode_query_value(outcome.result)
        # Registry clients distinguish an explicitly absent record from malformed
        # RPC responses. Empty protobuf bytes become JSON null in CometBFT.
        if ctx.route == "get" and ctx.key.startswith("_accounts.") and outcome.result is None:
            value = b"\x00"
    except UnknownQueryPath as err:
        error = str(err)
        logger.error(error)
        return ResponseQuery(
            code=c.ErrorCode,
            height=height,
            value=b"\x00",
            info=None,
            log=error,
        )
    except Exception as err:
        logger.error(err)
        return ResponseQuery(code=c.ErrorCode, height=height)

    return ResponseQuery(
        code=c.OkCode,
        height=height,
        value=value,
        info=type_of_data,
        key=encode_str(outcome.key if outcome.key is not None else key),
    )
