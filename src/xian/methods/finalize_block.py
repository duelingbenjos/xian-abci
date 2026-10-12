import json

from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import Event, EventAttribute
from cometbft.abci.v1beta3.types_pb2 import ExecTxResult, ResponseFinalizeBlock
from xian.account_registry import is_registry_transaction, registry_for_app
from xian.app_logging import build_log_fields
from xian.constants import Constants as c
from xian.fee_policy import TxFeePolicy
from xian.methods._evidence import maybe_apply_evidence_penalties
from xian.services.bds.payloads import BdsBlockPayload, BdsTransactionPayload
from xian.shielded_preverify import warm_shielded_proof_cache
from xian.utils.block import (
    convert_cometbft_time_to_datetime,
    get_nanotime_from_block_time,
)
from xian.utils.encoding import (
    convert_binary_to_hex,
    encode_abci_json,
    hash_bytes,
    stringify_decimals,
)
from xian.utils.tx import (
    SequentialNonceTracker,
    decode_and_validate_transaction_static_bytes,
    validate_consensus_transaction_after_static,
)

STATE_CHANGE_TRANSLATION_TABLE = str.maketrans({".": "_", ":": "__"})
SYSTEM_REBALANCE_SENDER = "__validator_epoch_driver__"


def _error_tx_result(message: str, **log_context) -> ExecTxResult:
    logger.bind(**build_log_fields(**log_context)).error(message)
    return ExecTxResult(
        code=c.ErrorCode,
        data=json.dumps({"error": message}).encode(),
        gas_used=0,
    )


def _safe_positive_int(value, default: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    if normalized <= 0:
        return default
    return normalized


def _maybe_run_validator_epoch_rebalance(self, *, height: int):
    driver = self.client.raw_driver
    selection_mode = driver.get("validators.config:selection_mode", save=False)
    if selection_mode in (None, "manual"):
        return None, False

    rebalance_interval = _safe_positive_int(
        driver.get("validators.config:rebalance_interval", save=False),
        1,
    )
    current_epoch = height // rebalance_interval
    last_rebalance_epoch = driver.get(
        "validators.last_rebalance_epoch",
        save=False,
    )
    if last_rebalance_epoch is not None and current_epoch <= last_rebalance_epoch:
        return None, False

    rebalance_tx = {
        "payload": {
            "sender": SYSTEM_REBALANCE_SENDER,
            "contract": "validators",
            "function": "rebalance",
            "kwargs": {},
            "chi_supplied": 0,
        },
        "metadata": {
            "signature": f"validator-epoch-rebalance:{height}",
        },
        "b_meta": self.current_block_meta,
    }
    result = self.tx_processor.process_tx(
        rebalance_tx,
        rewards_handler=None,
        fee_policy=TxFeePolicy.system_unmetered(),
        track_access=False,
    )
    tx_result = result.get("tx_result")
    if tx_result is None or tx_result.get("status") != 0:
        logger.bind(
            **build_log_fields(
                stage="finalize_epoch_rebalance",
                block_height=height,
                block_hash=self.current_block_meta["hash"],
                status=(tx_result or {}).get("status"),
            )
        ).warning("Automatic epoch rebalance did not apply")
        return tx_result, False

    logger.bind(
        **build_log_fields(
            stage="finalize_epoch_rebalance",
            block_height=height,
            block_hash=self.current_block_meta["hash"],
            extra={
                "state_write_count": len(tx_result.get("state", [])),
            },
        )
    ).info("Applied automatic validator epoch rebalance")
    return tx_result, len(tx_result.get("state", [])) > 0


def _state_change_event(tx_result):
    state_changes = []
    for state in tx_result["state"]:
        state_key = state["key"].translate(STATE_CHANGE_TRANSLATION_TABLE)
        state_value = str(state["value"])
        state_changes.append(EventAttribute(key=state_key, value=state_value))
    if not state_changes:
        return None
    return Event(type="StateChange", attributes=state_changes)


def _contract_event_attributes(contract_event):
    attrs = [
        EventAttribute(
            key="contract",
            value=str(contract_event.get("contract", "")),
            index=True,
        ),
        EventAttribute(
            key="signer",
            value=str(contract_event.get("signer", "")),
            index=True,
        ),
        EventAttribute(
            key="caller",
            value=str(contract_event.get("caller", "")),
            index=True,
        ),
    ]
    for key, value in contract_event.get("data_indexed", {}).items():
        attrs.append(
            EventAttribute(
                key=str(key),
                value=str(stringify_decimals(value)),
                index=True,
            )
        )
    for key, value in contract_event.get("data", {}).items():
        attrs.append(
            EventAttribute(
                key=str(key),
                value=str(stringify_decimals(value)),
                index=False,
            )
        )
    return attrs


def _build_tx_events(tx_result):
    if tx_result["status"] != 0:
        return []

    tx_events = []
    state_event = _state_change_event(tx_result)
    if state_event is not None:
        tx_events.append(state_event)

    for contract_event in tx_result.get("events", []):
        tx_events.append(
            Event(
                type=str(contract_event.get("event", "ContractEvent")),
                attributes=_contract_event_attributes(contract_event),
            )
        )
    return tx_events


def _initialize_block(self, req):
    nanos = get_nanotime_from_block_time(req.time)
    block_hash = convert_binary_to_hex(req.hash)
    block_datetime = convert_cometbft_time_to_datetime(nanos)
    height = req.height
    self.current_block_meta = {
        "nanos": nanos,
        "height": height,
        "hash": block_hash,
        "chain_id": self.chain_id,
    }
    self.tx_processor.reset_block_cache()
    logger.bind(
        **build_log_fields(
            stage="finalize_start",
            block_height=height,
            block_hash=block_hash,
            extra={"tx_count": len(req.txs)},
        )
    ).info("Finalizing block")
    return nanos, block_hash, block_datetime, height


def _decode_block_transactions(self, req, *, height: int, block_hash: str):
    decoded_entries = []
    nonce_tracker = SequentialNonceTracker(self.nonce_storage.get_nonce)
    account_registry = registry_for_app(self)
    block_chi_supplied = 0
    with self.profiler.scope("finalize_decode", block_scoped=True):
        for tx_bytes in req.txs:
            try:
                tx = decode_and_validate_transaction_static_bytes(
                    tx_bytes,
                    chain_id=self.chain_id,
                    max_raw_tx_bytes=self.max_tx_bytes,
                    account_registry=account_registry,
                )
            except Exception as e:
                decoded_entries.append(
                    {
                        "tx_bytes": tx_bytes,
                        "error": _error_tx_result(
                            f"Error decoding transaction: {e}",
                            stage="finalize_decode",
                            raw_tx=tx_bytes,
                            block_height=height,
                            block_hash=block_hash,
                        ),
                    }
                )
                continue

            try:
                self.tx_fee_policy.validate_tx(tx)
                next_block_chi_supplied = block_chi_supplied + self.tx_fee_policy.tx_chi_supplied(
                    tx
                )
                self.tx_fee_policy.validate_block_total(next_block_chi_supplied)
                validate_consensus_transaction_after_static(
                    tx,
                    nonce_tracker=nonce_tracker,
                )
                account_registry.accept(tx)
                block_chi_supplied = next_block_chi_supplied
            except Exception as e:
                decoded_entries.append(
                    {
                        "tx_bytes": tx_bytes,
                        "error": _error_tx_result(
                            f"Invalid transaction in block: {e}",
                            stage="finalize_decode",
                            tx=tx,
                            raw_tx=tx_bytes,
                            block_height=height,
                            block_hash=block_hash,
                        ),
                    }
                )
                continue
            # Attach block metadata only after consensus validation, since it
            # is not part of the canonical signed transaction shape.
            tx["b_meta"] = self.current_block_meta
            decoded_entries.append({"tx": tx, "tx_bytes": tx_bytes})

    decoded_txs = [entry["tx"] for entry in decoded_entries if "tx" in entry]
    rejected_tx_count = sum(1 for entry in decoded_entries if "error" in entry)
    logger.bind(
        **build_log_fields(
            stage="finalize_decode",
            block_height=height,
            block_hash=block_hash,
            extra={
                "decoded_tx_count": len(decoded_txs),
                "rejected_tx_count": rejected_tx_count,
            },
        )
    ).info("Finished block transaction validation")
    return decoded_entries, decoded_txs


def _preverify_shielded_for_serial_execution(self, decoded_txs: list[dict]):
    if self.parallel_block_executor.is_enabled_for_batch(len(decoded_txs)):
        return

    with self.profiler.scope("finalize_shielded_preverify", block_scoped=True):
        shielded_preverify_stats = warm_shielded_proof_cache(
            driver=self.tx_processor.client.raw_driver,
            txs=decoded_txs,
        )
    self.profiler.set_block_metadata(
        shielded_preverify_candidates=shielded_preverify_stats.candidate_count,
        shielded_preverify_verified=shielded_preverify_stats.verified_count,
        shielded_preverify_failed=shielded_preverify_stats.failed_count,
    )


def _execute_transactions_in_parallel(
    self,
    decoded_txs: list[dict],
    *,
    height: int,
    block_hash: str,
):
    if any(is_registry_transaction(tx) for tx in decoded_txs):
        return None
    with self.profiler.scope("finalize_parallel", block_scoped=True):
        parallel_execution = self.parallel_block_executor.execute(
            txs=decoded_txs,
            tx_processor=self.tx_processor,
            fee_policy=self.tx_fee_policy,
            rewards_handler=self.rewards_handler,
        )
    if parallel_execution is None:
        return None

    processed_results, parallel_stats = parallel_execution
    self.profiler.set_block_metadata(
        parallel_enabled=True,
        parallel_worker_count=parallel_stats.worker_count,
        parallel_estimated_known_transactions=(parallel_stats.estimated_known_transactions),
        parallel_estimated_unknown_transactions=(parallel_stats.estimated_unknown_transactions),
        parallel_estimated_stage_count=parallel_stats.estimated_stage_count,
        parallel_estimated_parallelizable_transactions=(
            parallel_stats.estimated_parallelizable_transactions
        ),
        parallel_estimated_known_shapes=parallel_stats.estimated_known_shapes,
        parallel_estimated_unknown_shapes=parallel_stats.estimated_unknown_shapes,
        parallel_planned_stage_count=parallel_stats.planned_stage_count,
        parallel_planned_parallelizable_transactions=(
            parallel_stats.planned_parallelizable_transactions
        ),
        parallel_speculative_wave_count=parallel_stats.speculative_wave_count,
        parallel_speculative_accepted=parallel_stats.speculative_accepted,
        parallel_speculative_rejected=parallel_stats.speculative_rejected,
        parallel_serial_prefiltered=parallel_stats.serial_prefiltered,
        parallel_serial_fallbacks=parallel_stats.serial_fallbacks,
        parallel_guardrail_fallbacks=parallel_stats.guardrail_fallbacks,
    )
    logger.bind(
        **build_log_fields(
            stage="finalize_parallel",
            block_height=height,
            block_hash=block_hash,
            extra={
                "speculative_accepted": parallel_stats.speculative_accepted,
                "decoded_tx_count": len(decoded_txs),
                "estimated_known_transactions": (parallel_stats.estimated_known_transactions),
                "estimated_unknown_transactions": (parallel_stats.estimated_unknown_transactions),
                "estimated_unknown_shapes": (parallel_stats.estimated_unknown_shapes),
                "speculative_wave_count": parallel_stats.speculative_wave_count,
                "serial_prefiltered": parallel_stats.serial_prefiltered,
                "serial_fallbacks": parallel_stats.serial_fallbacks,
                "guardrail_fallbacks": parallel_stats.guardrail_fallbacks,
                "speculative_rejected": parallel_stats.speculative_rejected,
                "planned_stage_count": parallel_stats.planned_stage_count,
                "worker_count": parallel_stats.worker_count,
            },
        )
    ).info("Parallel block execution summary")
    return processed_results


def _execute_block_transactions(
    self,
    decoded_entries: list[dict],
    processed_results,
    *,
    height: int,
    block_hash: str,
):
    processed_iter = iter(processed_results or [])
    processed_entries = []
    with self.profiler.scope("finalize_execute", block_scoped=True):
        for block_tx_index, entry in enumerate(decoded_entries):
            if "error" in entry:
                processed_entries.append({"error": entry["error"]})
                continue

            tx = entry["tx"]
            tx_bytes = entry["tx_bytes"]
            try:
                if processed_results is not None:
                    result = next(processed_iter)
                else:
                    result = self.tx_processor.process_tx(
                        tx,
                        fee_policy=self.tx_fee_policy,
                        rewards_handler=self.rewards_handler,
                        track_access=False,
                    )
            except Exception as e:
                processed_entries.append(
                    {
                        "error": _error_tx_result(
                            f"Error processing tx: {e}",
                            stage="finalize_execute",
                            tx=tx,
                            raw_tx=tx_bytes,
                            block_height=height,
                            block_hash=block_hash,
                            tx_index=block_tx_index,
                        )
                    }
                )
                continue

            tx_result = result.get("tx_result")
            if tx_result is None:
                processed_entries.append(
                    {
                        "error": _error_tx_result(
                            "Transaction processor returned no tx_result",
                            stage="finalize_execute",
                            tx=tx,
                            raw_tx=tx_bytes,
                            block_height=height,
                            block_hash=block_hash,
                            tx_index=block_tx_index,
                        )
                    }
                )
                continue

            processed_entries.append(
                {
                    "block_tx_index": block_tx_index,
                    "tx": tx,
                    "tx_bytes": tx_bytes,
                    "tx_result": tx_result,
                }
            )
    return processed_entries


def _assemble_tx_results(
    self,
    processed_entries: list[dict],
    *,
    height: int,
    block_hash: str,
):
    tx_results = []
    bds_transactions = []
    with self.profiler.scope("finalize_result_assembly", block_scoped=True):
        for entry in processed_entries:
            if "error" in entry:
                tx_results.append(entry["error"])
                continue

            block_tx_index = entry["block_tx_index"]
            tx = entry["tx"]
            tx_bytes = entry["tx_bytes"]
            tx_result = entry["tx_result"]

            tx_hash = tx_result.get("hash")
            if not tx_hash:
                tx_results.append(
                    _error_tx_result(
                        "Transaction processor returned no tx hash",
                        stage="finalize_result_assembly",
                        tx=tx,
                        raw_tx=tx_bytes,
                        block_height=height,
                        block_hash=block_hash,
                        tx_index=block_tx_index,
                    )
                )
                continue

            self.nonce_storage.set_nonce_by_tx(tx)
            parsed_tx_result = encode_abci_json(tx_result)
            if self.transaction_trace_debug_logging:
                logger.bind(
                    **build_log_fields(
                        stage="finalize_tx_result",
                        tx=tx,
                        tx_hash=tx_hash,
                        block_height=height,
                        block_hash=block_hash,
                        tx_index=block_tx_index,
                        status=tx_result["status"],
                        extra={
                            "chi_used": tx_result["chi_used"],
                            "state_write_count": len(tx_result["state"]),
                            "event_count": len(tx_result.get("events", [])),
                        },
                    )
                ).debug("Finalized transaction result")
            if self.transaction_trace_full_logging:
                logger.bind(
                    **build_log_fields(
                        stage="finalize_tx_result",
                        tx=tx,
                        tx_hash=tx_hash,
                        block_height=height,
                        block_hash=block_hash,
                        tx_index=block_tx_index,
                        status=tx_result["status"],
                        extra={
                            "payload_bytes": len(parsed_tx_result),
                        },
                    )
                ).trace(parsed_tx_result.decode())

            tx_results.append(
                ExecTxResult(
                    code=tx_result["status"],
                    data=parsed_tx_result,
                    gas_used=0,
                    events=_build_tx_events(tx_result),
                )
            )

            if self.bds_enabled:
                cometbft_hash = hash_bytes(tx_bytes).upper()
                tx_result["hash"] = cometbft_hash
                bds_transactions.append(
                    BdsTransactionPayload(
                        tx_index=block_tx_index,
                        envelope={key: value for key, value in tx.items() if key != "b_meta"},
                        payload=tx["payload"],
                        tx_result=tx_result,
                    )
                )
    return tx_results, bds_transactions


def _apply_evidence_and_epoch_rebalance(
    self,
    req,
    *,
    height: int,
):
    with self.profiler.scope("finalize_evidence", block_scoped=True):
        maybe_apply_evidence_penalties(
            self,
            req,
            height=height,
        )

    with self.profiler.scope("finalize_epoch_rebalance", block_scoped=True):
        _maybe_run_validator_epoch_rebalance(
            self,
            height=height,
        )


def _prepare_commit_boundary(
    self,
    *,
    height: int,
    nanos: int,
    block_hash: str,
):
    with self.profiler.scope("finalize_state_root", block_scoped=True):
        with self.profiler.scope("finalize_commit_prepare", block_scoped=True):
            validator_updates = self.validator_handler.build_validator_updates(height)

            patch_hash = None
            applied_patches = []
            if hasattr(self, "state_patch_manager"):
                patch_hash, applied_patches = self.state_patch_manager.apply_patches_for_block(
                    height,
                    nanos,
                    block_hash=block_hash,
                )

                if patch_hash:
                    logger.bind(
                        **build_log_fields(
                            stage="finalize_state_root",
                            block_height=height,
                            block_hash=block_hash,
                            extra={"patch_hash": patch_hash},
                        )
                    ).info("Applied state patch before state-root calculation")

            # Commit boundary: pending writes are now represented by app_hash,
            # but only commit.hard_apply may persist and clear them.
            self.merkle_root_hash = self.state_root_cache.prepare(
                self.client.raw_driver.pending_writes
            )
    return validator_updates, patch_hash, applied_patches


async def _enqueue_bds_block(
    self,
    *,
    block_datetime,
    bds_transactions: list[BdsTransactionPayload],
    applied_patches: list,
    patch_hash,
    height: int,
    block_hash: str,
):
    if not self.bds_enabled:
        return

    with self.profiler.scope("finalize_bds_enqueue", block_scoped=True):
        try:
            await self.bds.enqueue_block(
                BdsBlockPayload(
                    block_meta=self.current_block_meta.copy(),
                    block_time=block_datetime,
                    app_hash=self.merkle_root_hash.hex().upper(),
                    transactions=bds_transactions,
                    state_patches=applied_patches,
                    state_patch_hash=patch_hash,
                )
            )
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="finalize_bds_enqueue",
                    block_height=height,
                    block_hash=block_hash,
                    extra={"error_type": type(exc).__name__},
                )
            ).exception("BDS enqueue failed for block")


def _finish_finalize_logging(
    self,
    *,
    decoded_entries: list[dict],
    decoded_txs: list[dict],
    tx_results: list[ExecTxResult],
    height: int,
    block_hash: str,
):
    rejected_tx_count = sum(1 for entry in decoded_entries if "error" in entry)
    self.profiler.set_block_metadata(
        decoded_tx_count=len(decoded_txs),
        error_tx_count=rejected_tx_count,
        finalized_tx_result_count=len(tx_results),
    )
    logger.bind(
        **build_log_fields(
            stage="finalize_complete",
            block_height=height,
            block_hash=block_hash,
            extra={
                "decoded_tx_count": len(decoded_txs),
                "rejected_tx_count": rejected_tx_count,
                "finalized_tx_result_count": len(tx_results),
                "app_hash": self.merkle_root_hash.hex().upper(),
            },
        )
    ).info("Completed block finalization")


async def finalize_block(self, req) -> ResponseFinalizeBlock:
    nanos, block_hash, block_datetime, height = _initialize_block(self, req)

    decoded_entries, decoded_txs = _decode_block_transactions(
        self,
        req,
        height=height,
        block_hash=block_hash,
    )
    _preverify_shielded_for_serial_execution(self, decoded_txs)
    processed_results = _execute_transactions_in_parallel(
        self,
        decoded_txs,
        height=height,
        block_hash=block_hash,
    )

    with self.profiler.scope("finalize_tx_loop", block_scoped=True):
        processed_entries = _execute_block_transactions(
            self,
            decoded_entries,
            processed_results,
            height=height,
            block_hash=block_hash,
        )
        tx_results, bds_transactions = _assemble_tx_results(
            self,
            processed_entries,
            height=height,
            block_hash=block_hash,
        )

    _apply_evidence_and_epoch_rebalance(
        self,
        req,
        height=height,
    )
    validator_updates, patch_hash, applied_patches = _prepare_commit_boundary(
        self,
        height=height,
        nanos=nanos,
        block_hash=block_hash,
    )
    await _enqueue_bds_block(
        self,
        block_datetime=block_datetime,
        bds_transactions=bds_transactions,
        applied_patches=applied_patches,
        patch_hash=patch_hash,
        height=height,
        block_hash=block_hash,
    )

    _finish_finalize_logging(
        self,
        decoded_entries=decoded_entries,
        decoded_txs=decoded_txs,
        tx_results=tx_results,
        height=height,
        block_hash=block_hash,
    )

    self._stage_pending_commit_driver_state()

    return ResponseFinalizeBlock(
        validator_updates=validator_updates,
        tx_results=tx_results,
        app_hash=self.merkle_root_hash,
    )
