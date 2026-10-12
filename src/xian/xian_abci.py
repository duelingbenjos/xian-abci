# ruff: noqa: E402
import asyncio
import gc
import importlib
import signal
import sys
from dataclasses import replace


def _refuse_optimized_python() -> None:
    # Refuse to boot under PYTHONOPTIMIZE / -O. The interpreter strips assert
    # statements and __debug__-guarded blocks; many of those guard
    # consensus-critical invariants (private-method gating, balance/stamp
    # caps, hash-key delimiter and size limits, contract-size cap). A
    # validator running with optimization on would silently diverge from the
    # rest of the network.
    if not __debug__:
        sys.stderr.write(
            "xian-abci refuses to run with PYTHONOPTIMIZE / python -O: "
            "assert statements guard consensus-critical invariants and "
            "stripping them produces divergent state. Re-run without -O.\n"
        )
        sys.exit(2)


def _require_zk_verifier_if_enabled(runtime_features: dict | None) -> None:
    if not (runtime_features or {}).get("zk", False):
        return

    # Refuse to boot zk-enabled chains without the native verifier. Shielded
    # contracts call `zk.verify_groth16(...)` on the consensus path; if the
    # native backend is absent, that call raises instead of returning a verdict,
    # so a node without it would compute a different transaction outcome and
    # fork the chain.
    from contracting.stdlib.bridge import zk as zk_bridge

    if not zk_bridge.is_available():
        sys.stderr.write(
            "xian-abci refuses to run this zk-enabled chain without the native "
            "zk verifier. Shielded-contract proof verification is consensus "
            "critical; a node missing the backend would fork the network. "
            "Install xian-tech-abci[zk] (or xian-tech-contracting[zk]).\n"
        )
        sys.exit(2)


_refuse_optimized_python()

from contracting.local import ContractingClient
from loguru import logger

from abci.server import ABCIServer
from abci.utils import get_logger
from xian.account_registry import AccountRegistry
from xian.app_logging import (
    build_log_fields,
    configure_logging,
    log_level_includes,
)
from xian.constants import Constants
from xian.execution_engine import build_vm_runtime
from xian.execution_policy import validate_vm_execution_config
from xian.fee_policy import TxFeePolicy
from xian.methods import (
    check_tx,
    commit,
    echo,
    finalize_block,
    info,
    init_chain,
    prepare_proposal,
    process_proposal,
    query,
)
from xian.metrics import MetricsService
from xian.node_setup import (
    DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE,
    DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES,
    DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS,
    DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO,
    DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS,
    DEFAULT_PARALLEL_EXECUTION_WORKERS,
)
from xian.nonce import NonceStorage
from xian.parallel_executor import ParallelBlockExecutor
from xian.perf import PerfTracker
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.runtime_features import (
    install_driver_runtime_features,
    resolve_runtime_features,
)
from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig
from xian.services.state_sync import StateSnapshotManager
from xian.simulator import QuerySimulationService
from xian.state_root import StateRootCache
from xian.utils.block import get_latest_block_height, reconcile_latest_block
from xian.utils.cometbft import (
    load_genesis_data,
    load_tendermint_config,
    load_xian_config,
    resolve_local_rpc_url,
)
from xian.utils.state_patches import StatePatchManager, resolve_state_patch_dir
from xian.validators import ValidatorHandler

get_logger("requests").setLevel(30)
get_logger("urllib3").setLevel(30)
get_logger("asyncio").setLevel(30)


def resolve_abci_socket_path(proxy_app: str | None) -> str:
    if proxy_app is None or proxy_app == "":
        return "/tmp/abci.sock"
    if not proxy_app.startswith("unix://"):
        raise ValueError(f"xian-abci only supports unix proxy_app values, got {proxy_app!r}")
    return proxy_app.removeprefix("unix://")


def load_module(module_path, original_module_path):
    """
    Inplace replace of a module with a new one and taking its name.
    """
    try:
        # Replace all functions in the original module with the new modules functions
        module = importlib.import_module(module_path)
        modified_original_module = importlib.import_module(original_module_path)
        for name in dir(module):
            if name.startswith("__"):
                continue
            setattr(modified_original_module, name, getattr(module, name))
        sys.modules[original_module_path] = modified_original_module
        del sys.modules[module_path]
        gc.collect()
        logger.info(f"Loaded module {module_path}")
    except Exception as e:
        raise Exception(f"Failed to load module {module_path}: {e}")


class Xian:
    def __init__(self, constants=Constants()):
        self.constants = constants
        try:
            self.cometbft_config = load_tendermint_config(constants)
            self.xian_config = load_xian_config(constants)
            self.genesis = load_genesis_data(constants)
        except Exception as e:
            logger.error(e)
            raise SystemExit()

        xian_config = self.xian_config
        validate_vm_execution_config(xian_config)
        self.execution_runtime = build_vm_runtime()
        self.execution_mode = self.execution_runtime.mode
        self.client = ContractingClient(
            storage_home=constants.STORAGE_HOME,
        )
        reconcile_latest_block(
            self.client.raw_driver,
            constants.STORAGE_HOME,
        )
        try:
            self.runtime_feature_resolution = resolve_runtime_features(
                driver=self.client.raw_driver,
                genesis=self.genesis,
            )
        except ValueError as exc:
            logger.error(str(exc))
            raise SystemExit() from exc
        self.runtime_features = install_driver_runtime_features(
            self.client.raw_driver,
            self.runtime_feature_resolution,
        )
        _require_zk_verifier_if_enabled(self.runtime_features)
        self.state_root_cache = StateRootCache.from_driver(self.client.raw_driver)
        self.chain_id = self.genesis.get("chain_id", None)
        if self.chain_id is None:
            raise ValueError("No value set for 'chain_id' in genesis block")
        if AccountRegistry(self.client.raw_driver, self.chain_id).policy() is not None:
            from xian_accounts.hybrid import backend

            backend()
        self.profiler = PerfTracker.from_env(
            cometbft_home=constants.COMETBFT_HOME,
            node_name=self.cometbft_config.get("moniker", ""),
            chain_id=self.chain_id,
            execution_mode=self.execution_mode,
        )
        self.state_snapshot_manager = StateSnapshotManager(
            storage_home=constants.STORAGE_HOME,
            chain_id=self.chain_id,
        )
        self.validator_handler = ValidatorHandler(self)
        self.nonce_storage = NonceStorage(
            self.client,
            reservation_ttl_seconds=xian_config.get(
                "pending_nonce_reservation_ttl_seconds",
                60.0,
            ),
            max_pending_nonces_per_sender=xian_config.get(
                "max_pending_nonces_per_sender",
                128,
            ),
        )
        try:
            configured_max_tx_bytes = int(
                self.cometbft_config.get("mempool", {}).get(
                    "max_tx_bytes",
                    4 * 1024 * 1024,
                )
            )
        except (TypeError, ValueError):
            configured_max_tx_bytes = 4 * 1024 * 1024
        self.max_tx_bytes = max(configured_max_tx_bytes, 1)
        self.app_log_level = str(xian_config.get("app_log_level", "INFO")).upper()
        self.transaction_trace_logging = xian_config.get("transaction_trace_logging", False)
        self.transaction_trace_debug_logging = (
            self.transaction_trace_logging and log_level_includes(self.app_log_level, "DEBUG")
        )
        self.transaction_trace_full_logging = self.transaction_trace_logging and log_level_includes(
            self.app_log_level, "TRACE"
        )
        self.tx_processor = TxProcessor(
            client=self.client,
            profiler=self.profiler,
            trace_logging=self.transaction_trace_debug_logging,
            execution_runtime=self.execution_runtime,
        )
        self.tx_fee_policy = TxFeePolicy.from_runtime_config(xian_config)
        self.enable_tx_fee = self.tx_fee_policy.charge_fees
        self.simulator = QuerySimulationService(
            storage_home=constants.STORAGE_HOME,
            execution_runtime=self.execution_runtime,
            get_block_meta=lambda: self.current_block_meta,
            get_state_snapshot=lambda: self.client.raw_driver.snapshot_state(),
            chain_id=self.chain_id,
            enabled=xian_config.get("simulation_enabled", True),
            max_concurrency=xian_config.get("simulation_max_concurrency", 2),
            timeout_ms=xian_config.get("simulation_timeout_ms", 3000),
            max_chi=xian_config.get("simulation_max_chi", 1_000_000),
            charge_fees=self.tx_fee_policy.charge_fees,
        )
        self.rewards_handler = RewardsHandler(client=self.client)
        self.current_block_meta: dict = None
        self.merkle_root_hash = None
        # FinalizeBlock leaves consensus writes pending until Commit, but
        # CometBFT flushes outstanding CheckTx requests before it calls Commit.
        # Stage those writes outside the live driver so CheckTx can drain
        # against committed state without deadlocking the commit boundary.
        self._state_transition_lock = asyncio.Lock()
        self._pending_commit_driver_state = None

        self.bds_enabled = xian_config.get("bds_enabled", False)
        self.bds_config = BdsConfig.from_runtime_settings(xian_config)
        if self.bds_config.spool_dir is None:
            self.bds_config = replace(
                self.bds_config,
                spool_dir=str(constants.STORAGE_HOME / "bds-spool"),
            )
        if self.bds_config.rpc_url is None:
            self.bds_config = replace(
                self.bds_config,
                rpc_url=resolve_local_rpc_url(
                    str(self.cometbft_config.get("rpc", {}).get("laddr", "tcp://127.0.0.1:26657"))
                ),
            )

        self.pruning_enabled = xian_config.get("pruning_enabled", False)
        # If pruning is enabled, this is the number of blocks to keep history for
        self.blocks_to_keep = xian_config.get("blocks_to_keep", 100000)
        self.parallel_block_executor = ParallelBlockExecutor(
            storage_home=constants.STORAGE_HOME,
            enabled=xian_config.get(
                "parallel_execution_enabled",
                DEFAULT_PARALLEL_EXECUTION_ENABLED,
            ),
            workers=xian_config.get(
                "parallel_execution_workers",
                DEFAULT_PARALLEL_EXECUTION_WORKERS,
            ),
            min_transactions=xian_config.get(
                "parallel_execution_min_transactions",
                DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS,
            ),
            max_speculative_waves=xian_config.get(
                "parallel_execution_max_speculative_waves",
                DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES,
            ),
            min_wave_acceptance_ratio=xian_config.get(
                "parallel_execution_min_wave_acceptance_ratio",
                DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO,
            ),
            low_acceptance_min_wave_size=xian_config.get(
                "parallel_execution_low_acceptance_min_wave_size",
                DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE,
            ),
            access_estimates_enabled=xian_config.get(
                "parallel_execution_access_estimates_enabled",
                DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED,
            ),
            execution_runtime=self.execution_runtime,
        )
        if xian_config.get(
            "parallel_execution_warm_workers",
            DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS,
        ):
            self.parallel_block_executor.warm(use_rewards_handler=True)
        self.app_version = 1
        self.metrics_service = MetricsService.from_runtime_settings(self, xian_config)

        if self.genesis.get("abci_genesis", None) is None:
            raise ValueError("No value set for 'abci_genesis' in Tendermint genesis.json")

        self.current_block_rewards = {}

        self.state_patch_manager = StatePatchManager(
            self.client.raw_driver,
            chain_id=self.chain_id,
        )
        patch_dir_path = resolve_state_patch_dir(self.constants)

        logger.bind(
            **build_log_fields(
                stage="startup",
                extra={
                    "chain_id": self.chain_id,
                    "execution_mode": self.execution_mode,
                    "bds_enabled": self.bds_enabled,
                    "tx_fee_mode": self.tx_fee_policy.mode,
                    "runtime_features": self.runtime_features,
                    "runtime_features_source": self.runtime_feature_resolution.source,
                    "simulation_enabled": self.simulator.enabled,
                    "parallel_execution_enabled": (self.parallel_block_executor.enabled),
                    "transaction_trace_logging": (self.transaction_trace_logging),
                    "transaction_trace_debug_logging": (self.transaction_trace_debug_logging),
                    "transaction_trace_full_logging": (self.transaction_trace_full_logging),
                },
            )
        ).info("Initialized Xian runtime")

        if patch_dir_path.exists():
            logger.bind(
                **build_log_fields(
                    stage="startup",
                    extra={"state_patch_dir": str(patch_dir_path)},
                )
            ).info("Loading local state patch bundles")
            self.state_patch_manager.load_patches(str(patch_dir_path))
        else:
            logger.bind(
                **build_log_fields(
                    stage="startup",
                    extra={"state_patch_dir": str(patch_dir_path)},
                )
            ).warning("No local state patch bundle directory found")
            self.state_patch_manager.loaded = True

    @classmethod
    async def create(cls, constants=Constants()):
        self = cls(constants=constants)
        if self.bds_enabled:
            self.bds = BDS(self.bds_config, raw_driver=self.client.raw_driver)
            self._bds_storage_initialized = False
        return self

    async def start_runtime(self):
        if self.bds_enabled and hasattr(self, "bds"):
            if not getattr(self, "_bds_storage_initialized", False):
                await self.bds.initialize_storage(cometbft_genesis=self.genesis)
                self._bds_storage_initialized = True
            await self.bds.start()
        await self.metrics_service.start()

    def _release_state_transition_lock(self):
        if self._state_transition_lock.locked():
            self._state_transition_lock.release()

    def _stage_pending_commit_driver_state(self):
        if self._pending_commit_driver_state is not None:
            raise RuntimeError("previous block has staged writes that were not committed")

        self._pending_commit_driver_state = self.client.raw_driver.detach_pending_state()

    def _restore_pending_commit_driver_state(self):
        staged_state = self._pending_commit_driver_state
        if staged_state is None:
            return

        self.client.raw_driver.attach_pending_state(staged_state)
        self._pending_commit_driver_state = None

    def _discard_pending_commit_driver_state(self):
        self._pending_commit_driver_state = None

    async def echo(self, req):
        """
        Echo a string to test an ABCI client/server implementation
        """
        res = echo.echo(self, req)
        return res

    async def info(self, req):
        """
        Called every time the application starts
        Return information about the application state.
        """
        res = await info.info(self, req)
        return res

    async def init_chain(self, req):
        """Called once upon genesis."""
        resp = await init_chain.init_chain(self, req)
        return resp

    async def check_tx(self, raw_tx):
        """
        Technically optional - not involved in processing blocks.
        Guardian of the mempool: every node runs CheckTx before letting a transaction into its local mempool.
        The transaction may come from an external user or another node
        """
        async with self._state_transition_lock:
            with self.profiler.scope("check_tx"):
                res = await check_tx.check_tx(self, raw_tx)
        return res

    async def finalize_block(self, req):
        """
        Contains the fields of the newly decided block.
        This method is equivalent to the call sequence BeginBlock, [DeliverTx], and EndBlock in the previous version of ABCI.
        """
        await self._state_transition_lock.acquire()
        self.profiler.start_block(req.height, len(req.txs))
        try:
            with self.profiler.scope("finalize_block", block_scoped=True):
                res = await finalize_block.finalize_block(self, req)
            self.profiler.end_block(app_hash=res.app_hash.hex())
            return res
        except Exception as exc:
            self._discard_pending_commit_driver_state()
            self.state_root_cache.rollback()
            self.profiler.end_block(error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._release_state_transition_lock()

    async def commit(self):
        """
        Signal the Application to persist the application state. Application is expected to persist its state at the end of this call, before calling ResponseCommit.
        """
        await self._state_transition_lock.acquire()
        try:
            res = await commit.commit(self)
            return res
        finally:
            self._release_state_transition_lock()

    async def process_proposal(self, req):
        """
        Contains all information on the proposed block needed to fully execute it.
        """
        async with self._state_transition_lock:
            with self.profiler.scope("process_proposal"):
                res = await process_proposal.process_proposal(self, req)
        return res

    async def prepare_proposal(self, req):
        """
        RequestPrepareProposal contains a preliminary set of transactions txs that CometBFT retrieved from the mempool, called raw proposal. The Application can modify this set and return a modified set of transactions via ResponsePrepareProposal.txs .
        """
        async with self._state_transition_lock:
            with self.profiler.scope("block_packing"):
                res = await prepare_proposal.prepare_proposal(self, req)
        return res

    async def list_snapshots(self, req):
        del req
        return self.state_snapshot_manager.list_snapshots_response()

    async def offer_snapshot(self, req):
        current_height = get_latest_block_height(self.client.raw_driver.storage_home)
        return self.state_snapshot_manager.offer_snapshot_response(
            req.snapshot,
            app_hash=req.app_hash,
            current_height=current_height,
        )

    async def load_snapshot_chunk(self, req):
        return self.state_snapshot_manager.load_snapshot_chunk_response(
            height=req.height,
            format_version=req.format,
            chunk_index=req.chunk,
        )

    async def apply_snapshot_chunk(self, req):
        response = self.state_snapshot_manager.apply_snapshot_chunk_response(
            index=req.index,
            chunk=req.chunk,
            sender=req.sender,
        )
        if response.result == response.ACCEPT:
            # Import uses a separate driver over the same LMDB environment.
            # Drop cached pre-import reads before serving Info/queries/execution.
            self.client.raw_driver.flush_cache()
            self.nonce_storage.flush_pending()
            self.state_root_cache.rebuild(self.client.raw_driver.items().items())
        return response

    async def query(self, req):
        """
        Query the application state
        Request Ex. http://localhost:26657/abci_query?path="path"
        """
        async with self._state_transition_lock:
            res = await query.query(self, req)
        return res

    async def close(self):
        self.parallel_block_executor.close()
        self.simulator.close()
        await self.metrics_service.close()
        if self.bds_enabled and hasattr(self, "bds"):
            await self.bds.close()


def main():
    _refuse_optimized_python()
    constants = Constants()
    configure_logging(constants)

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    app = asyncio.run(Xian.create(constants=constants))
    ABCIServer(
        app=app,
        socket_path=resolve_abci_socket_path(
            str(app.cometbft_config.get("proxy_app", "unix:///tmp/abci.sock"))
        ),
    ).run()


def signal_handler(signum, frame):
    logger.info("Shutting down...")
    logger.remove()
    sys.exit(0)


if __name__ == "__main__":
    main()
