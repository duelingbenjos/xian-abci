import hashlib
import time

from contracting.execution.parallel import ExecutionAccess as TransactionAccess
from contracting.stdlib.bridge import zk as zk_bridge
from loguru import logger
from xian_runtime_types.encoding import convert_dict, safe_repr
from xian_runtime_types.time import Datetime

from xian.account_registry import execute_registry_transaction, is_registry_transaction
from xian.app_logging import build_log_fields
from xian.execution_engine import (
    VmRuntime,
    execute_vm_transaction,
    prepare_vm_contract,
    vm_contract_source_error,
    vm_metering_writes,
    vm_requires_contract_source,
)
from xian.fee_policy import TxFeePolicy
from xian.utils.block import nanoseconds_to_utc_datetime
from xian.utils.tx import canonical_transaction_size_bytes, tx_hash_from_tx


class TxProcessor:
    def __init__(
        self,
        client,
        metering=False,
        profiler=None,
        trace_logging: bool = False,
        execution_runtime: VmRuntime | None = None,
    ):
        self.client = client
        self.profiler = profiler
        self.trace_logging = trace_logging
        self.execution_runtime = execution_runtime or VmRuntime()
        self.currency_contract = "currency"
        self.balances_hash = "balances"
        self.chi_cost_key = self.client.raw_driver.make_key(
            "chi_cost",
            "S",
            ["value"],
        )
        self.cached_chi_cost = None

    def reset_block_cache(self) -> None:
        self.cached_chi_cost = None
        zk_bridge.clear_verified_proof_cache()

    def get_chi_cost(self):
        if self.cached_chi_cost is None:
            self.cached_chi_cost = (
                self.client.get_var(
                    contract="chi_cost",
                    variable="S",
                    arguments=["value"],
                )
                or 1
            )
        return self.cached_chi_cost

    def update_chi_cost_cache(self, base_writes: dict) -> None:
        if self.chi_cost_key in base_writes:
            self.cached_chi_cost = base_writes[self.chi_cost_key]

    def process_tx(
        self,
        tx,
        rewards_handler=None,
        *,
        fee_policy: TxFeePolicy | None = None,
        track_access: bool = False,
    ):
        started_ns = time.perf_counter_ns()
        driver = self.client.raw_driver
        # Native authorization updates must not survive a failed processor result.
        registry_checkpoint = None
        if is_registry_transaction(tx):
            registry_checkpoint = {
                name: getattr(driver, name).copy()
                for name in (
                    "pending_writes",
                    "pending_reads",
                    "transaction_reads",
                    "transaction_read_prefixes",
                    "transaction_writes",
                    "log_events",
                )
            }
        previous_read_tracking = driver.track_transaction_reads
        driver.set_transaction_read_tracking(track_access)
        environment = self.get_environment(tx=tx)
        chi_cost = self.get_chi_cost()
        fee_policy = fee_policy or TxFeePolicy.system_unmetered()

        try:
            # Execute the transaction
            output = self.execute_tx(
                transaction=tx,
                chi_cost=chi_cost,
                environment=environment,
                metering=fee_policy.meter_execution,
                charge_fees=fee_policy.charge_fees,
            )
            if output is None:
                return {
                    "tx_result": None,
                    "chi_rewards_amount": 0,
                    "chi_rewards_contract": None,
                    "base_writes": {},
                    "reward_deltas": {},
                    "access": None,
                }

            # Process the result of the executor
            processed = self.process_tx_output(
                output=output,
                transaction=tx,
                chi_cost=chi_cost,
                rewards_handler=rewards_handler,
                charge_fees=fee_policy.charge_fees,
                distribute_fee_rewards=fee_policy.distribute_fee_rewards,
                track_access=track_access,
            )
            tx_result = processed["tx_result"]
            self.update_chi_cost_cache(processed["base_writes"])
            tx_result = self.prune_tx_result(tx_result)

            response = {
                "tx_result": tx_result,
                "chi_rewards_amount": output["chi_used"],
                "chi_rewards_contract": tx["payload"]["contract"],
            }
            if track_access:
                response.update(
                    {
                        "base_writes": processed["base_writes"],
                        "reward_deltas": processed["reward_deltas"],
                        "access": self.build_access_record(
                            tx=tx,
                            status_code=output["status_code"],
                            reads=processed["reads"],
                            prefix_reads=processed["prefix_reads"],
                            base_writes=processed["base_writes"],
                            reward_deltas=processed["reward_deltas"],
                        ),
                    }
                )
            return response
        except Exception as e:
            if registry_checkpoint is not None:
                for name, value in registry_checkpoint.items():
                    setattr(driver, name, value)
            logger.bind(
                **build_log_fields(
                    stage="process_tx",
                    tx=tx,
                    extra={"error_type": type(e).__name__},
                )
            ).exception("Transaction processing failed unexpectedly")
            response = {
                "tx_result": None,
                "chi_rewards_amount": 0,
                "chi_rewards_contract": None,
            }
            if track_access:
                response.update(
                    {
                        "base_writes": {},
                        "reward_deltas": {},
                        "access": None,
                    }
                )
            return response
        finally:
            driver.set_transaction_read_tracking(previous_read_tracking)
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_process_total",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def execute_tx(
        self,
        transaction,
        chi_cost,
        environment: dict | None = None,
        metering=False,
        charge_fees: bool = True,
    ):
        execution_runtime = getattr(
            self,
            "execution_runtime",
            VmRuntime(),
        )
        resolved_environment = environment or {}
        if self.trace_logging:
            logger.bind(
                **build_log_fields(
                    stage="execute_tx",
                    tx=transaction,
                    extra={
                        "chi_cost": chi_cost,
                        "metering": metering,
                    },
                )
            ).debug("Executing transaction")
        started_ns = time.perf_counter_ns()

        try:
            if is_registry_transaction(transaction):
                return execute_registry_transaction(
                    self.client.raw_driver, transaction, chi_cost=chi_cost, charge_fees=charge_fees
                )
            prepare_vm_contract(
                execution_runtime,
                self.client.raw_driver,
                transaction["payload"]["contract"],
            )
            converted_kwargs = convert_dict(transaction["payload"]["kwargs"])
            if vm_requires_contract_source(
                transaction["payload"]["contract"],
                transaction["payload"]["function"],
                converted_kwargs,
            ):
                return {
                    "status_code": 1,
                    "result": ValueError(
                        vm_contract_source_error(
                            transaction["payload"]["contract"],
                            transaction["payload"]["function"],
                        )
                    ),
                    "writes": {},
                    "events": [],
                    "chi_used": 0,
                    "reads": {},
                    "prefix_reads": frozenset(),
                    "contract_costs": {},
                }
            return self._execute_vm_tx(
                transaction=transaction,
                kwargs=converted_kwargs,
                chi_cost=chi_cost,
                environment=resolved_environment,
                metering=metering,
                charge_fees=charge_fees,
            )
        except (TypeError, ValueError) as err:
            logger.bind(
                **build_log_fields(
                    stage="execute_tx",
                    tx=transaction,
                    extra={
                        "chi_cost": chi_cost,
                        "chi_supplied": transaction["payload"]["chi_supplied"],
                        "metering": metering,
                        "error_type": type(err).__name__,
                    },
                )
            ).exception("Transaction execution failed before producing an executor result")
            return None
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_execute",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def _execute_vm_tx(
        self,
        *,
        transaction: dict,
        kwargs: dict,
        chi_cost: int,
        environment: dict,
        metering: bool,
        charge_fees: bool,
    ) -> dict:
        driver = self.client.raw_driver
        if hasattr(driver, "clear_transaction_reads"):
            driver.clear_transaction_reads()
        else:
            getattr(driver, "transaction_reads", {}).clear()
            getattr(driver, "transaction_read_prefixes", set()).clear()
        if hasattr(driver, "clear_transaction_writes"):
            driver.clear_transaction_writes()
        else:
            getattr(driver, "transaction_writes", {}).clear()
        outcome = execute_vm_transaction(
            self.execution_runtime,
            driver,
            sender=transaction["payload"]["sender"],
            contract_name=transaction["payload"]["contract"],
            function_name=transaction["payload"]["function"],
            kwargs=kwargs,
            environment=environment,
            chi_budget=transaction["payload"]["chi_supplied"],
            chi_cost=chi_cost,
            meter=metering,
            transaction_size_bytes=canonical_transaction_size_bytes(transaction),
            apply_metering_writes=charge_fees,
            currency_contract=self.currency_contract,
            balances_hash=self.balances_hash,
        )

        return {
            "status_code": outcome.output.status_code,
            "result": outcome.output.result,
            "writes": outcome.writes,
            "events": list(outcome.output.events),
            "chi_used": outcome.chi_used,
            "reads": dict(getattr(outcome, "reads", {}) or {}),
            "prefix_reads": frozenset(getattr(outcome, "prefix_reads", frozenset()) or ()),
            "contract_costs": outcome.contract_costs,
        }

    def process_tx_output(
        self,
        output,
        transaction,
        chi_cost,
        rewards_handler,
        *,
        charge_fees: bool = True,
        distribute_fee_rewards: bool = True,
        track_access: bool = False,
    ):
        started_ns = time.perf_counter_ns()
        try:
            if self.trace_logging:
                logger.bind(
                    **build_log_fields(
                        stage="process_tx_output",
                        tx=transaction,
                        status=output["status_code"],
                        extra={
                            "chi_used": output["chi_used"],
                            "write_count": len(output["writes"]),
                        },
                    )
                ).debug("Processing executor output")
            if output["status_code"] > 0:
                logger.bind(
                    **build_log_fields(
                        stage="process_tx_output",
                        tx=transaction,
                        status=output["status_code"],
                        extra={
                            "chi_used": output["chi_used"],
                            "write_count": len(output["writes"]),
                            "result": safe_repr(output["result"]),
                        },
                    )
                ).warning("Transaction execution returned a non-zero status")

            tx_hash = tx_hash_from_tx(transaction)

            rewards = None
            reward_deltas = {}
            reward_records = []
            if (
                distribute_fee_rewards
                and output["status_code"] == 0
                and rewards_handler is not None
            ):
                rewards, reward_deltas, reward_records = rewards_handler.build_tx_reward_outputs(
                    total_chi_to_split=output["chi_used"],
                    contract=transaction["payload"]["contract"],
                    contract_costs=output.get("contract_costs"),
                )

            base_writes = self.determine_writes_from_output(
                status_code=output["status_code"],
                ouput_writes=output["writes"],
                chi_used=output["chi_used"],
                chi_cost=chi_cost,
                tx_sender=transaction["payload"]["sender"],
                charge_fees=charge_fees,
            )
            writes = self.materialize_writes(base_writes, reward_deltas)
            reads = frozenset()
            prefix_reads = frozenset()
            if track_access:
                output_reads = output.get("reads")
                if isinstance(output_reads, dict):
                    reads = frozenset(output_reads.keys())
                elif output_reads is not None:
                    reads = frozenset(output_reads)
                else:
                    reads = frozenset(self.client.raw_driver.transaction_reads.keys())

                output_prefix_reads = output.get("prefix_reads")
                if output_prefix_reads is not None:
                    prefix_reads = frozenset(output_prefix_reads)
                else:
                    prefix_reads = frozenset(self.client.raw_driver.transaction_read_prefixes)

            for write in writes:
                self.client.raw_driver.set(key=write["key"], value=write["value"])

            tx_output = {
                "hash": tx_hash,
                "status": output["status_code"],
                "state": writes,
                "events": output["events"],
                "chi_used": output["chi_used"],
                "result": safe_repr(output["result"]),
                "rewards": rewards if rewards else None,
                "reward_records": reward_records or None,
            }

            if self.trace_logging:
                logger.bind(
                    **build_log_fields(
                        stage="tx_result",
                        tx=transaction,
                        tx_hash=tx_hash,
                        status=output["status_code"],
                        extra={
                            "chi_used": output["chi_used"],
                            "state_write_count": len(writes),
                            "event_count": len(output["events"]),
                        },
                    )
                ).debug("Produced transaction result")

            return {
                "tx_result": tx_output,
                "reads": reads,
                "prefix_reads": prefix_reads,
                "base_writes": base_writes,
                "reward_deltas": reward_deltas,
            }
        finally:
            if self.profiler is not None:
                self.profiler.observe(
                    "tx_process_output",
                    time.perf_counter_ns() - started_ns,
                    block_scoped=True,
                )

    def apply_tx_result(self, tx_result: dict) -> None:
        for write in tx_result["state"]:
            self.client.raw_driver.set(key=write["key"], value=write["value"])

    def determine_writes_from_output(
        self,
        status_code,
        ouput_writes,
        chi_used,
        chi_cost,
        tx_sender,
        charge_fees=True,
    ):
        # Only apply the writes if the tx passes
        if status_code == 0:
            return dict(ouput_writes)
        if not charge_fees:
            return {}
        else:
            return vm_metering_writes(
                self.client.raw_driver,
                sender=tx_sender,
                chi_used=chi_used,
                chi_cost=chi_cost,
                currency_contract=self.currency_contract,
                balances_hash=self.balances_hash,
            )

    def materialize_writes(self, base_writes, reward_deltas):
        writes_map = dict(base_writes)

        for key, delta in reward_deltas.items():
            if key in writes_map:
                writes_map[key] += delta
                continue

            current_value = self.client.raw_driver.get(key, save=False)
            if current_value is None:
                current_value = 0
            writes_map[key] = current_value + delta

        writes = [{"key": k, "value": v} for k, v in writes_map.items()]
        # Write ordering is consensus-critical: every validator must apply
        # state in the same order, so a sort failure has to halt the node.
        writes.sort(key=lambda x: x["key"])

        return writes

    def get_environment(self, tx):
        block_meta = tx["b_meta"]
        nanos = block_meta["nanos"]
        signature = tx["metadata"].get("signature")
        if signature is None:
            signature = tx["metadata"]["auth"]["signature"]
        chain_id = block_meta["chain_id"]

        # Nanos is derived from the block header time consensus agreed on, so
        # it is identical for every tx in the block and deterministic across
        # validators. CometBFT computes header time as the weighted median of
        # validator precommit timestamps (BFT Time), or takes the proposer's
        # timestamp when Proposer-Based Timestamps are enabled.

        return {
            "block_hash": block_meta["hash"],  # hash nanos
            "block_num": block_meta["height"],  # block number
            "__input_hash": self.get_timestamp_hash_from_tx(nanos, signature),
            "__xian_execution_mode__": self.execution_runtime.mode,
            "now": self.get_now_from_nanos(nanos=nanos),
            "chain_id": chain_id,
        }

    def get_timestamp_hash_from_tx(self, nanos, signature):
        h = hashlib.sha3_256()
        h.update("{}".format(str(nanos) + signature).encode())
        return h.hexdigest()

    def get_now_from_nanos(self, nanos):
        block_time = nanoseconds_to_utc_datetime(nanos)
        return Datetime._from_datetime(block_time)

    def prune_tx_result(self, tx_result: dict):
        return tx_result

    def estimate_access(self, tx: dict) -> TransactionAccess | None:
        payload = tx.get("payload")
        if not isinstance(payload, dict):
            return None

        sender = payload.get("sender")
        contract = payload.get("contract")
        function = payload.get("function")
        kwargs = payload.get("kwargs") or {}
        if (
            not isinstance(sender, str)
            or not isinstance(contract, str)
            or not isinstance(function, str)
            or not isinstance(kwargs, dict)
        ):
            return None

        def make_key(variable: str, args: list[object]) -> str:
            return self.client.raw_driver.make_key(contract, variable, args)

        reads: set[str] = set()
        writes: set[str] = set()

        if function == "transfer":
            to = kwargs.get("to")
            if not isinstance(to, str):
                return None
            balance_keys = {
                make_key(self.balances_hash, [sender]),
                make_key(self.balances_hash, [to]),
            }
            reads.update(balance_keys)
            writes.update(balance_keys)
        elif function == "approve":
            to = kwargs.get("to")
            if not isinstance(to, str):
                return None
            writes.add(make_key("approvals", [sender, to]))
        elif function == "transfer_from":
            to = kwargs.get("to")
            main_account = kwargs.get("main_account")
            if not isinstance(to, str) or not isinstance(main_account, str):
                return None
            keys = {
                make_key("approvals", [main_account, sender]),
                make_key(self.balances_hash, [main_account]),
                make_key(self.balances_hash, [to]),
            }
            reads.update(keys)
            writes.update(keys)
        elif function == "change_metadata":
            key = kwargs.get("key")
            if not isinstance(key, str):
                return None
            reads.add(make_key("metadata", ["operator"]))
            writes.add(make_key("metadata", [key]))
        elif function == "set_operator":
            writes.add(make_key("metadata", ["operator"]))
        elif function == "balance_of":
            address = kwargs.get("address")
            if not isinstance(address, str):
                return None
            reads.add(make_key(self.balances_hash, [address]))
        elif contract in {"chi_cost", "rewards"} and function == "current_value":
            reads.add(make_key("S", ["value"]))
        elif contract in {"chi_cost", "rewards"} and function == "set_value":
            writes.add(make_key("S", ["value"]))
        else:
            return None

        return TransactionAccess(
            index=-1,
            sender=sender,
            nonce=payload.get("nonce", 0),
            reads=frozenset(reads),
            prefix_reads=frozenset(),
            writes=frozenset(writes),
            additive_writes=frozenset(),
            status=0,
        )

    def build_access_record(
        self,
        tx: dict,
        status_code: int,
        reads: frozenset[str],
        prefix_reads: frozenset[str],
        base_writes: dict,
        reward_deltas: dict,
    ) -> TransactionAccess:
        return TransactionAccess(
            index=-1,
            sender=tx["payload"]["sender"],
            nonce=tx["payload"].get("nonce", 0),
            reads=reads,
            prefix_reads=prefix_reads,
            writes=frozenset(base_writes.keys()),
            additive_writes=frozenset(reward_deltas.keys()),
            status=status_code,
        )
