from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.request import urlopen

from xian_accounts import is_valid_ed25519_key, verify_message

from xian.config_paths import resolve_genesis_source
from xian.constants import Constants as c
from xian.fee_policy import (
    DEFAULT_FREE_BLOCK_MAX_CHI,
    DEFAULT_FREE_TX_MAX_CHI,
    DEFAULT_TX_FEE_MODE,
)
from xian.node_setup import (
    DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_ENABLED,
    DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE,
    DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES,
    DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS,
    DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO,
    DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS,
    DEFAULT_PARALLEL_EXECUTION_WORKERS,
    AppLoggingOptions,
    BdsOptions,
    MetricsOptions,
    NodeConfigOptions,
    ParallelExecutionOptions,
    SimulationOptions,
    StateSyncOptions,
    build_priv_validator_key,
    render_node_configs,
    write_json,
    write_toml,
)
from xian.toml_utils import load as load_toml
from xian.utils.cometbft import format_url_netloc, normalize_rpc_url, split_host_port

_ROOT_CONFIG_KEYS_TO_PRESERVE = (
    "db_backend",
    "db_dir",
    "genesis_file",
    "priv_validator_key_file",
    "priv_validator_state_file",
    "node_key_file",
    "abci",
    "log_level",
    "log_format",
    "version",
)
_NESTED_CONFIG_KEYS_TO_PRESERVE = (
    ("rpc", "laddr"),
    ("p2p", "laddr"),
)
_DEFAULT_DISCOVERY_RPC_PORT = 26657
_DEFAULT_DISCOVERY_P2P_PORT = 26656


@dataclass(frozen=True, slots=True)
class ExistingHomeOptions:
    moniker: str
    validator_private_key_hex: str | None = None
    home: Path = c.COMETBFT_HOME
    discover_seeds: tuple[str, ...] = ()
    p2p_seeds: tuple[str, ...] = ()
    p2p_persistent_peers: tuple[str, ...] = ()
    snapshot_url: str | None = None
    snapshot_signing_public_keys: tuple[str, ...] = ()
    snapshot_expected_chain_id: str | None = None
    genesis_source: str | None = None
    genesis_payload: dict[str, Any] | None = None
    node_config: NodeConfigOptions | None = None


def load_existing_cometbft_config(
    config_path: Path = c.COMETBFT_CONFIG,
) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError("initialize CometBFT first")

    with open(config_path, "r", encoding="utf-8") as handle:
        return load_toml(handle)


def preserve_runtime_config(
    rendered_config: dict[str, Any], existing_config: dict[str, Any]
) -> dict[str, Any]:
    for key in _ROOT_CONFIG_KEYS_TO_PRESERVE:
        rendered_config[key] = existing_config[key]

    for section, key in _NESTED_CONFIG_KEYS_TO_PRESERVE:
        rendered_config[section][key] = existing_config[section][key]

    return rendered_config


def _seed_host_port(seed_node: str) -> tuple[str, str | None]:
    seed = seed_node.strip()
    if "://" in seed:
        parsed = urlsplit(normalize_rpc_url(seed))
        port = str(parsed.port) if parsed.port is not None else None
        return parsed.hostname or "", port
    return split_host_port(seed)


def _seed_rpc_status_url(seed_node: str) -> str:
    seed = seed_node.strip()
    scheme = "http"
    if "://" in seed:
        parsed = urlsplit(normalize_rpc_url(seed))
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        port = str(parsed.port) if parsed.port is not None else None
    else:
        host, port = split_host_port(seed)
    return urlunsplit(
        (
            scheme,
            format_url_netloc(host, port or _DEFAULT_DISCOVERY_RPC_PORT),
            "status",
            "",
            "",
        )
    )


def _p2p_seed_address(node_id: str, seed_node: str) -> str:
    host, _port = _seed_host_port(seed_node)
    return f"{node_id}@{format_url_netloc(host, _DEFAULT_DISCOVERY_P2P_PORT)}"


def _normalize_p2p_seed(seed: str) -> str:
    node_id, separator, address = seed.rpartition("@")
    if not separator:
        return seed

    host, port = split_host_port(address)
    if not host or port is None:
        return seed
    return f"{node_id}@{format_url_netloc(host, port)}"


def fetch_seed_node_status(
    seed_node: str,
    *,
    attempts: int = 10,
    timeout_seconds: float = 3.0,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        try:
            return _fetch_json_url(
                _seed_rpc_status_url(seed_node),
                timeout=timeout_seconds,
            )
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ):
            if attempt == attempts - 1:
                break
            sleep(poll_interval_seconds)

    return None


def resolve_p2p_seeds(
    *,
    discover_seeds: tuple[str, ...] = (),
    p2p_seeds: tuple[str, ...] = (),
) -> list[str]:
    resolved = [_normalize_p2p_seed(seed) for seed in p2p_seeds]
    for seed in discover_seeds:
        status = fetch_seed_node_status(seed)
        if status is None:
            raise RuntimeError(f"failed to get node information from seed node {seed}")

        node_id = status["result"]["node_info"]["id"]
        resolved.append(_p2p_seed_address(node_id, seed))
    return resolved


SNAPSHOT_RESTORED_DIRECTORIES = ("data", "xian")
PRIV_VALIDATOR_STATE_PATH = Path("data") / "priv_validator_state.json"


def _snapshot_member_is_restored(name: str) -> bool:
    relative = Path(name)
    parts = [part for part in relative.parts if part != "."]
    if not parts or parts[0] not in SNAPSHOT_RESTORED_DIRECTORIES:
        return False
    # The signing high-water mark belongs to this node's validator key, never
    # to the archive: restoring an older one would allow double-signing.
    return Path(*parts) != PRIV_VALIDATOR_STATE_PATH


def _safe_extract_tar_archive(archive_path: Path, target_path: Path) -> None:
    target_root = target_path.resolve()
    with tarfile.open(archive_path) as archive:
        members = []
        for member in archive.getmembers():
            member_path = (target_root / member.name).resolve()
            if not member_path.is_relative_to(target_root):
                raise ValueError(f"snapshot archive contains invalid path: {member.name}")
            # Only application data is restored. Keys and config (for example
            # config/priv_validator_key.json or config/node_key.json) stay as
            # they are on this node.
            if _snapshot_member_is_restored(member.name):
                members.append(member)
        archive.extractall(path=target_root, members=members, filter="data")


def _download_snapshot_archive(snapshot_url: str, target_path: Path) -> Path:
    parsed_url = urlparse(snapshot_url)
    filename = Path(parsed_url.path).name or "snapshot.tar.gz"
    archive_path = target_path / filename
    archive_path.write_bytes(_download_binary_url(snapshot_url, timeout=30))
    return archive_path


def _fetch_json_url(url: str, *, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
    return json.loads(payload)


def _download_binary_url(url: str, *, timeout: float) -> bytes:
    with urlopen(url, timeout=timeout) as response:
        return response.read()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_snapshot_manifest_public_keys(
    public_keys: list[str] | None,
) -> list[str]:
    if public_keys is None:
        return []

    normalized: list[str] = []
    for value in public_keys:
        if not isinstance(value, str):
            raise ValueError("trusted snapshot signing keys must be hex strings")
        key = value.strip()
        if not is_valid_ed25519_key(key):
            raise ValueError(f"invalid snapshot signing key: {value!r}")
        normalized.append(key)
    return normalized


def _canonical_snapshot_manifest_payload(manifest: dict[str, Any]) -> str:
    payload = {
        "manifest_version": manifest["manifest_version"],
        "chain_id": manifest["chain_id"],
        "height": manifest["height"],
        "snapshot_url": manifest["snapshot_url"],
        "snapshot_sha256": manifest["snapshot_sha256"],
        "signing_public_key": manifest["signing_public_key"],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _verify_snapshot_manifest(
    manifest: dict[str, Any],
    *,
    trusted_manifest_public_keys: list[str],
    expected_chain_id: str | None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("snapshot manifest must be a JSON object")

    manifest_version = manifest.get("manifest_version")
    if isinstance(manifest_version, bool) or not isinstance(manifest_version, int):
        raise ValueError("snapshot manifest_version must be an integer")
    if manifest_version != 1:
        raise ValueError(f"unsupported snapshot manifest_version: {manifest_version}")

    chain_id = manifest.get("chain_id")
    if not isinstance(chain_id, str) or chain_id == "":
        raise ValueError("snapshot manifest chain_id must be a string")
    if expected_chain_id is not None and chain_id != expected_chain_id:
        raise ValueError(
            f"snapshot manifest chain_id mismatch: expected {expected_chain_id}, got {chain_id}"
        )

    height = manifest.get("height")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise ValueError("snapshot manifest height must be a positive integer")

    snapshot_url = manifest.get("snapshot_url")
    if not isinstance(snapshot_url, str) or snapshot_url == "":
        raise ValueError("snapshot manifest snapshot_url must be a string")

    snapshot_sha256 = manifest.get("snapshot_sha256")
    if not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64:
        raise ValueError("snapshot manifest snapshot_sha256 must be a 64-character hex string")
    try:
        bytes.fromhex(snapshot_sha256)
    except ValueError as ex:
        raise ValueError(
            "snapshot manifest snapshot_sha256 must be a 64-character hex string"
        ) from ex
    snapshot_sha256 = snapshot_sha256.lower()

    signing_public_key = manifest.get("signing_public_key")
    if not isinstance(signing_public_key, str) or not is_valid_ed25519_key(signing_public_key):
        raise ValueError("snapshot manifest signing_public_key must be a valid Ed25519 hex key")
    if signing_public_key not in trusted_manifest_public_keys:
        raise ValueError("snapshot manifest signer is not trusted")

    signature = manifest.get("signature")
    if not isinstance(signature, str) or signature == "":
        raise ValueError("snapshot manifest signature must be a hex string")

    normalized_manifest = {
        "manifest_version": manifest_version,
        "chain_id": chain_id,
        "height": height,
        "snapshot_url": snapshot_url,
        "snapshot_sha256": snapshot_sha256,
        "signing_public_key": signing_public_key,
        "signature": signature,
    }
    payload = _canonical_snapshot_manifest_payload(normalized_manifest)
    if not verify_message(signing_public_key, payload, signature):
        raise ValueError("snapshot manifest signature verification failed")
    return normalized_manifest


def apply_snapshot_archive(
    snapshot_url: str,
    home: Path,
    *,
    expected_sha256: str | None = None,
    trusted_manifest_public_keys: list[str] | None = None,
    expected_chain_id: str | None = None,
) -> str:
    home.mkdir(parents=True, exist_ok=True)
    trusted_keys = _normalize_snapshot_manifest_public_keys(trusted_manifest_public_keys)
    effective_snapshot_url = snapshot_url
    effective_sha256 = expected_sha256
    if expected_sha256 is None:
        if not trusted_keys:
            raise ValueError(
                "remote snapshot restore requires expected_sha256 or trusted snapshot signing keys"
            )
        manifest = _fetch_json_url(snapshot_url, timeout=30)
        verified_manifest = _verify_snapshot_manifest(
            manifest,
            trusted_manifest_public_keys=trusted_keys,
            expected_chain_id=expected_chain_id,
        )
        effective_snapshot_url = verified_manifest["snapshot_url"]
        effective_sha256 = verified_manifest["snapshot_sha256"]

    with tempfile.TemporaryDirectory(dir=home) as tmp_dir:
        archive_path = _download_snapshot_archive(
            effective_snapshot_url,
            Path(tmp_dir),
        )
        if not tarfile.is_tarfile(archive_path):
            raise ValueError("snapshot archive must be a .tar or .tar.gz file")
        if effective_sha256 is not None:
            actual_sha256 = _sha256_file(archive_path)
            if actual_sha256.lower() != effective_sha256.lower():
                raise ValueError(
                    "snapshot archive sha256 mismatch: "
                    f"expected {effective_sha256}, got {actual_sha256}"
                )

        validator_state_path = home / PRIV_VALIDATOR_STATE_PATH
        validator_state = (
            validator_state_path.read_bytes() if validator_state_path.is_file() else None
        )

        for directory in (home / name for name in SNAPSHOT_RESTORED_DIRECTORIES):
            if directory.exists():
                shutil.rmtree(directory)

        _safe_extract_tar_archive(archive_path, home)
        if validator_state is not None:
            validator_state_path.parent.mkdir(parents=True, exist_ok=True)
            validator_state_path.write_bytes(validator_state)
        return archive_path.name


def resolve_home_relative_path(home: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return home / path


def configure_existing_home(
    *,
    options: ExistingHomeOptions | None = None,
    moniker: str | None = None,
    validator_private_key_hex: str | None = None,
    home: Path = c.COMETBFT_HOME,
    allow_cors: bool = True,
    discover_seeds: list[str] | None = None,
    p2p_seeds: list[str] | None = None,
    p2p_persistent_peers: list[str] | None = None,
    snapshot_url: str | None = None,
    snapshot_signing_public_keys: list[str] | None = None,
    snapshot_expected_chain_id: str | None = None,
    genesis_source: str | None = None,
    genesis_payload: dict[str, Any] | None = None,
    prometheus: bool = True,
    bds_enabled: bool = False,
    enable_pruning: bool = False,
    blocks_to_keep: int = 100000,
    block_policy_mode: str = "on_demand",
    block_policy_interval: str = "0s",
    statesync_enable: bool = False,
    statesync_rpc_servers: list[str] | None = None,
    statesync_trust_height: int = 0,
    statesync_trust_hash: str = "",
    statesync_trust_period: str = "168h0m0s",
    metrics_enabled: bool = True,
    metrics_host: str = "127.0.0.1",
    metrics_port: int = 9108,
    metrics_bds_refresh_seconds: float = 5.0,
    transaction_trace_logging: bool = False,
    app_log_level: str = "INFO",
    app_log_json: bool = False,
    app_log_rotation_hours: int = 1,
    app_log_retention_days: int = 7,
    simulation_enabled: bool = True,
    simulation_max_concurrency: int = 2,
    simulation_timeout_ms: int = 3000,
    simulation_max_chi: int = 1_000_000,
    tx_fee_mode: str = DEFAULT_TX_FEE_MODE,
    free_tx_max_chi: int = DEFAULT_FREE_TX_MAX_CHI,
    free_block_max_chi: int = DEFAULT_FREE_BLOCK_MAX_CHI,
    parallel_execution_enabled: bool = DEFAULT_PARALLEL_EXECUTION_ENABLED,
    parallel_execution_workers: int = DEFAULT_PARALLEL_EXECUTION_WORKERS,
    parallel_execution_min_transactions: int = (DEFAULT_PARALLEL_EXECUTION_MIN_TRANSACTIONS),
    parallel_execution_max_speculative_waves: int = (
        DEFAULT_PARALLEL_EXECUTION_MAX_SPECULATIVE_WAVES
    ),
    parallel_execution_min_wave_acceptance_ratio: float = (
        DEFAULT_PARALLEL_EXECUTION_MIN_WAVE_ACCEPTANCE_RATIO
    ),
    parallel_execution_low_acceptance_min_wave_size: int = (
        DEFAULT_PARALLEL_EXECUTION_LOW_ACCEPTANCE_MIN_WAVE_SIZE
    ),
    parallel_execution_warm_workers: bool = (DEFAULT_PARALLEL_EXECUTION_WARM_WORKERS),
    parallel_execution_access_estimates_enabled: bool = (
        DEFAULT_PARALLEL_EXECUTION_ACCESS_ESTIMATES_ENABLED
    ),
    pending_nonce_reservation_ttl_seconds: float = 60.0,
    max_pending_nonces_per_sender: int = 128,
    bds_dsn: str = "",
    bds_host: str = "",
    bds_port: int = 5432,
    bds_database: str = "xian",
    bds_user: str = "",
    bds_password: str = "",
    bds_pool_min_size: int = 1,
    bds_pool_max_size: int = 10,
    bds_statement_timeout_ms: int = 0,
    bds_acquire_timeout_ms: int = 10_000,
    bds_application_name: str = "xian-bds",
    bds_queue_max_size: int = 128,
    bds_catchup_enabled: bool = True,
    bds_catchup_poll_seconds: float = 1.0,
    bds_rpc_url: str = "",
    bds_spool_dir: str = "",
    bds_spool_warn_entries: int = 256,
    bds_spool_warn_bytes: int = 536_870_912,
    bds_disk_free_warn_bytes: int = 2_147_483_648,
) -> dict[str, str | list[str] | None]:
    if options is None:
        if moniker is None:
            raise TypeError("moniker is required when options is not provided")
        node_config = NodeConfigOptions(
            moniker=moniker,
            allow_cors=allow_cors,
            bds_enabled=bds_enabled,
            enable_pruning=enable_pruning,
            blocks_to_keep=blocks_to_keep,
            transaction_trace_logging=transaction_trace_logging,
            block_policy_mode=block_policy_mode,
            block_policy_interval=block_policy_interval,
            statesync=StateSyncOptions(
                enable=statesync_enable,
                rpc_servers=tuple(statesync_rpc_servers or ()),
                trust_height=statesync_trust_height,
                trust_hash=statesync_trust_hash,
                trust_period=statesync_trust_period,
            ),
            metrics=MetricsOptions(
                enabled=metrics_enabled,
                host=metrics_host,
                port=metrics_port,
                bds_refresh_seconds=metrics_bds_refresh_seconds,
            ),
            app_logging=AppLoggingOptions(
                level=app_log_level,
                json_logging=app_log_json,
                rotation_hours=app_log_rotation_hours,
                retention_days=app_log_retention_days,
            ),
            simulation=SimulationOptions(
                enabled=simulation_enabled,
                max_concurrency=simulation_max_concurrency,
                timeout_ms=simulation_timeout_ms,
                max_chi=simulation_max_chi,
            ),
            tx_fee_mode=tx_fee_mode,
            free_tx_max_chi=free_tx_max_chi,
            free_block_max_chi=free_block_max_chi,
            parallel_execution=ParallelExecutionOptions(
                enabled=parallel_execution_enabled,
                workers=parallel_execution_workers,
                min_transactions=parallel_execution_min_transactions,
                max_speculative_waves=(parallel_execution_max_speculative_waves),
                min_wave_acceptance_ratio=(parallel_execution_min_wave_acceptance_ratio),
                low_acceptance_min_wave_size=(parallel_execution_low_acceptance_min_wave_size),
                warm_workers=parallel_execution_warm_workers,
                access_estimates_enabled=(parallel_execution_access_estimates_enabled),
            ),
            pending_nonce_reservation_ttl_seconds=(pending_nonce_reservation_ttl_seconds),
            max_pending_nonces_per_sender=max_pending_nonces_per_sender,
            bds=BdsOptions(
                dsn=bds_dsn,
                host=bds_host,
                port=bds_port,
                database=bds_database,
                user=bds_user,
                password=bds_password,
                pool_min_size=bds_pool_min_size,
                pool_max_size=bds_pool_max_size,
                statement_timeout_ms=bds_statement_timeout_ms,
                acquire_timeout_ms=bds_acquire_timeout_ms,
                application_name=bds_application_name,
                queue_max_size=bds_queue_max_size,
                catchup_enabled=bds_catchup_enabled,
                catchup_poll_seconds=bds_catchup_poll_seconds,
                rpc_url=bds_rpc_url,
                spool_dir=bds_spool_dir,
                spool_warn_entries=bds_spool_warn_entries,
                spool_warn_bytes=bds_spool_warn_bytes,
                disk_free_warn_bytes=bds_disk_free_warn_bytes,
            ),
            prometheus=prometheus,
        )
        options = ExistingHomeOptions(
            moniker=moniker,
            validator_private_key_hex=validator_private_key_hex,
            home=home,
            discover_seeds=tuple(discover_seeds or ()),
            p2p_seeds=tuple(p2p_seeds or ()),
            p2p_persistent_peers=tuple(p2p_persistent_peers or ()),
            snapshot_url=snapshot_url,
            snapshot_signing_public_keys=tuple(snapshot_signing_public_keys or ()),
            snapshot_expected_chain_id=snapshot_expected_chain_id,
            genesis_source=genesis_source,
            genesis_payload=genesis_payload,
            node_config=node_config,
        )

    request = options
    node_config = request.node_config or NodeConfigOptions(moniker=request.moniker)
    config_path = request.home / "config" / "config.toml"
    existing_config = load_existing_cometbft_config(config_path)
    p2p_seeds = resolve_p2p_seeds(
        discover_seeds=request.discover_seeds,
        p2p_seeds=request.p2p_seeds,
    )
    rendered_configs = render_node_configs(
        options=NodeConfigOptions(
            moniker=node_config.moniker,
            p2p_seeds=tuple(p2p_seeds),
            p2p_persistent_peers=request.p2p_persistent_peers,
            allow_cors=node_config.allow_cors,
            bds_enabled=node_config.bds_enabled,
            enable_pruning=node_config.enable_pruning,
            blocks_to_keep=node_config.blocks_to_keep,
            transaction_trace_logging=node_config.transaction_trace_logging,
            block_policy_mode=node_config.block_policy_mode,
            block_policy_interval=node_config.block_policy_interval,
            statesync=node_config.statesync,
            metrics=node_config.metrics,
            app_logging=node_config.app_logging,
            simulation=node_config.simulation,
            tx_fee_mode=node_config.tx_fee_mode,
            free_tx_max_chi=node_config.free_tx_max_chi,
            free_block_max_chi=node_config.free_block_max_chi,
            parallel_execution=node_config.parallel_execution,
            pending_nonce_reservation_ttl_seconds=(
                node_config.pending_nonce_reservation_ttl_seconds
            ),
            max_pending_nonces_per_sender=(node_config.max_pending_nonces_per_sender),
            bds=node_config.bds,
            proxy_app=node_config.proxy_app,
            prometheus=node_config.prometheus,
        )
    )
    rendered_config = rendered_configs["cometbft"]
    xian_config = rendered_configs["xian"]
    config = preserve_runtime_config(rendered_config, existing_config)

    snapshot_archive_name: str | None = None
    if request.snapshot_url:
        snapshot_archive_name = apply_snapshot_archive(
            request.snapshot_url,
            request.home,
            trusted_manifest_public_keys=list(request.snapshot_signing_public_keys) or None,
            expected_chain_id=request.snapshot_expected_chain_id,
        )

    genesis_target_path: Path | None = None
    if request.genesis_payload is not None or request.genesis_source is not None:
        genesis_target_path = resolve_home_relative_path(
            request.home,
            config["genesis_file"],
        )
        genesis_target_path.parent.mkdir(parents=True, exist_ok=True)
        if request.genesis_payload is not None:
            write_json(
                genesis_target_path,
                request.genesis_payload,
                overwrite=True,
            )
        elif request.genesis_source is not None:
            genesis_source_path = resolve_genesis_source(request.genesis_source)
            shutil.copy2(genesis_source_path, genesis_target_path)

    priv_validator_key_path: Path | None = None
    if request.validator_private_key_hex is not None:
        priv_validator_key_path = resolve_home_relative_path(
            request.home,
            config["priv_validator_key_file"],
        )
        priv_validator_key = build_priv_validator_key(request.validator_private_key_hex)
        priv_validator_key.pop("_private_key_hex", None)
        write_json(
            priv_validator_key_path,
            priv_validator_key,
            overwrite=True,
        )

    xian_config_path = request.home / "config" / "xian.toml"
    write_toml(config_path, config, overwrite=True)
    write_toml(xian_config_path, xian_config, overwrite=True)
    return {
        "config_path": str(config_path),
        "xian_config_path": str(xian_config_path),
        "genesis_path": (str(genesis_target_path) if genesis_target_path else None),
        "priv_validator_key_path": (
            str(priv_validator_key_path) if priv_validator_key_path else None
        ),
        "snapshot_archive_name": snapshot_archive_name,
        "p2p_seeds": list(p2p_seeds),
        "p2p_persistent_peers": list(request.p2p_persistent_peers),
    }
