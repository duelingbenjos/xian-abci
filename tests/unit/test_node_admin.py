import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian_accounts import Ed25519Account

from xian.node_admin import (
    ExistingHomeOptions,
    apply_snapshot_archive,
    configure_existing_home,
    fetch_seed_node_status,
    resolve_p2p_seeds,
)
from xian.node_setup import (
    BdsOptions,
    MetricsOptions,
    NodeConfigOptions,
    write_toml,
)
from xian.toml_utils import load as load_toml
from xian.toml_utils import loads as load_toml_string


def _signed_snapshot_manifest(
    *,
    archive_url: str,
    archive_bytes: bytes,
    chain_id: str = "xian-test-1",
    height: int = 123,
    signing_account: Ed25519Account | None = None,
) -> tuple[dict[str, object], Ed25519Account]:
    account = signing_account or Ed25519Account.generate()
    manifest = {
        "manifest_version": 1,
        "chain_id": chain_id,
        "height": height,
        "snapshot_url": archive_url,
        "snapshot_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "signing_public_key": account.public_key,
    }
    payload = json.dumps(manifest, separators=(",", ":"), sort_keys=True)
    manifest["signature"] = account.sign_message(payload)
    return manifest, account


class NodeAdminTests(unittest.TestCase):
    def test_resolve_p2p_seeds_uses_explicit_seed(self):
        self.assertEqual(
            resolve_p2p_seeds(p2p_seeds=("abc@127.0.0.1:26656",)),
            ["abc@127.0.0.1:26656"],
        )

    def test_resolve_p2p_seeds_queries_status_endpoint(self):
        with patch(
            "xian.node_admin.fetch_seed_node_status",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ):
            self.assertEqual(
                resolve_p2p_seeds(discover_seeds=("127.0.0.1",)),
                ["node-123@127.0.0.1:26656"],
            )

    def test_fetch_seed_node_status_brackets_ipv6_status_url(self):
        with patch(
            "xian.node_admin._fetch_json_url",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ) as fetch:
            self.assertEqual(
                fetch_seed_node_status("::1", attempts=1),
                {"result": {"node_info": {"id": "node-123"}}},
            )

        fetch.assert_called_once_with("http://[::1]:26657/status", timeout=3.0)

    def test_fetch_seed_node_status_accepts_bracketed_ipv6_port(self):
        with patch(
            "xian.node_admin._fetch_json_url",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ) as fetch:
            fetch_seed_node_status("[::1]:36657", attempts=1)

        fetch.assert_called_once_with("http://[::1]:36657/status", timeout=3.0)

    def test_fetch_seed_node_status_preserves_explicit_https_ipv6_url(self):
        with patch(
            "xian.node_admin._fetch_json_url",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ) as fetch:
            fetch_seed_node_status("https://::1:36657", attempts=1)

        fetch.assert_called_once_with("https://[::1]:36657/status", timeout=3.0)

    def test_resolve_p2p_seeds_brackets_discovered_ipv6_seed(self):
        with patch(
            "xian.node_admin.fetch_seed_node_status",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ):
            self.assertEqual(
                resolve_p2p_seeds(discover_seeds=("::1",)),
                ["node-123@[::1]:26656"],
            )

    def test_resolve_p2p_seeds_brackets_explicit_ipv6_seed(self):
        self.assertEqual(
            resolve_p2p_seeds(p2p_seeds=("abc@::1:26656",)),
            ["abc@[::1]:26656"],
        )

    def test_apply_snapshot_archive_verifies_signed_manifest_and_extracts_tar(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            (home / "data").mkdir()
            (home / "data" / "old.txt").write_text("old", encoding="utf-8")
            (home / "xian").mkdir()
            (home / "xian" / "old.txt").write_text("old", encoding="utf-8")

            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            archive_bytes = archive_stream.getvalue()
            manifest, account = _signed_snapshot_manifest(
                archive_url="https://example.invalid/snapshot.tar.gz",
                archive_bytes=archive_bytes,
            )

            with (
                patch(
                    "xian.node_admin._fetch_json_url",
                    return_value=manifest,
                ),
                patch(
                    "xian.node_admin._download_binary_url",
                    return_value=archive_bytes,
                ),
            ):
                archive_path = apply_snapshot_archive(
                    "https://example.invalid/snapshot-manifest.json",
                    home,
                    trusted_manifest_public_keys=[account.public_key],
                    expected_chain_id="xian-test-1",
                )

            self.assertTrue((home / "data" / "new.txt").exists())
            self.assertFalse((home / "data" / "old.txt").exists())
            self.assertFalse((home / "xian" / "old.txt").exists())
            self.assertEqual(archive_path, "snapshot.tar.gz")

    def test_apply_snapshot_archive_rejects_unsigned_remote_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)

            with self.assertRaisesRegex(
                ValueError,
                "requires expected_sha256 or trusted snapshot signing keys",
            ):
                apply_snapshot_archive(
                    "https://example.invalid/snapshot.tar.gz",
                    home,
                )

    def test_apply_snapshot_archive_rejects_untrusted_manifest_signer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            manifest, _account = _signed_snapshot_manifest(
                archive_url="https://example.invalid/snapshot.tar.gz",
                archive_bytes=archive_stream.getvalue(),
            )

            with patch(
                "xian.node_admin._fetch_json_url",
                return_value=manifest,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "signer is not trusted",
                ):
                    apply_snapshot_archive(
                        "https://example.invalid/snapshot-manifest.json",
                        home,
                        trusted_manifest_public_keys=["b" * 64],
                    )

    def test_apply_snapshot_archive_verifies_expected_sha256(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)

            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            archive_bytes = archive_stream.getvalue()
            expected_sha256 = hashlib.sha256(archive_bytes).hexdigest()

            with patch(
                "xian.node_admin._download_binary_url",
                return_value=archive_bytes,
            ):
                archive_path = apply_snapshot_archive(
                    "https://example.invalid/snapshot.tar.gz",
                    home,
                    expected_sha256=expected_sha256,
                )

            self.assertEqual(archive_path, "snapshot.tar.gz")
            self.assertTrue((home / "data" / "new.txt").exists())

    def test_apply_snapshot_archive_restores_only_application_data(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            (home / "config").mkdir()
            (home / "config" / "priv_validator_key.json").write_text("local key")
            (home / "config" / "node_key.json").write_text("local node key")
            (home / "data").mkdir()
            (home / "data" / "priv_validator_state.json").write_text("local height")

            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                for name, payload in (
                    ("./data/blockstore.db", b"blocks"),
                    ("xian/state.db", b"state"),
                    ("data/priv_validator_state.json", b"archive height"),
                    ("config/priv_validator_key.json", b"archive key"),
                    ("config/node_key.json", b"archive node key"),
                    ("config/config.toml", b"archive config"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            archive_bytes = archive_stream.getvalue()

            with patch(
                "xian.node_admin._download_binary_url",
                return_value=archive_bytes,
            ):
                apply_snapshot_archive(
                    "https://example.invalid/snapshot.tar.gz",
                    home,
                    expected_sha256=hashlib.sha256(archive_bytes).hexdigest(),
                )

            self.assertEqual((home / "data" / "blockstore.db").read_text(), "blocks")
            self.assertEqual((home / "xian" / "state.db").read_text(), "state")
            self.assertEqual(
                (home / "data" / "priv_validator_state.json").read_text(), "local height"
            )
            self.assertEqual(
                (home / "config" / "priv_validator_key.json").read_text(), "local key"
            )
            self.assertEqual((home / "config" / "node_key.json").read_text(), "local node key")
            self.assertFalse((home / "config" / "config.toml").exists())

    def test_apply_snapshot_archive_rejects_wrong_sha256(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)

            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            with patch(
                "xian.node_admin._download_binary_url",
                return_value=archive_stream.getvalue(),
            ):
                with self.assertRaises(ValueError):
                    apply_snapshot_archive(
                        "https://example.invalid/snapshot.tar.gz",
                        home,
                        expected_sha256="deadbeef",
                    )

    def test_configure_existing_home_renders_config_and_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "rocksdb"
db_dir = "custom-data"
log_level = "debug"
log_format = "json"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://0.0.0.0:30057"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:30056"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()
            )
            write_toml(config_path, existing_config)

            configs_dir = tmp_path / "xian-configs"
            network_dir = configs_dir / "networks" / "local"
            network_dir.mkdir(parents=True)
            genesis_payload = {
                "chain_id": "xian-mainnet-next",
                "validators": [],
                "abci_genesis": {},
            }
            (network_dir / "genesis.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                result = configure_existing_home(
                    home=home,
                    moniker="updated-node",
                    validator_private_key_hex=(
                        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                    ),
                    p2p_seeds=["seed-1@127.0.0.1:26656"],
                    p2p_persistent_peers=["peer-1@127.0.0.1:26656"],
                    genesis_source="local",
                    enable_pruning=True,
                    blocks_to_keep=5000,
                    allow_cors=False,
                    statesync_enable=True,
                    statesync_rpc_servers=[
                        "http://rpc-1.internal:26657",
                        "http://rpc-2.internal:26657",
                    ],
                    statesync_trust_height=120,
                    statesync_trust_hash="ab" * 32,
                    statesync_trust_period="336h0m0s",
                    metrics_enabled=True,
                    metrics_host="0.0.0.0",
                    metrics_port=9208,
                    metrics_bds_refresh_seconds=7.5,
                    transaction_trace_logging=True,
                    app_log_level="warning",
                    app_log_json=True,
                    app_log_rotation_hours=4,
                    app_log_retention_days=12,
                    simulation_enabled=False,
                    simulation_max_concurrency=4,
                    simulation_timeout_ms=4500,
                    simulation_max_chi=750000,
                    tx_fee_mode="free_metered",
                    free_tx_max_chi=350_000,
                    free_block_max_chi=1_400_000,
                    bds_host="postgres",
                    bds_port=5544,
                    bds_database="xian_index",
                    bds_user="indexer",
                    bds_password="secret",
                    bds_pool_min_size=2,
                    bds_pool_max_size=6,
                    bds_statement_timeout_ms=5000,
                    bds_acquire_timeout_ms=15000,
                    bds_application_name="xian-bds-test",
                    bds_queue_max_size=321,
                    bds_catchup_enabled=False,
                    bds_catchup_poll_seconds=2.5,
                    bds_rpc_url="http://rpc.internal:26657",
                    bds_spool_dir="/var/lib/xian/bds-spool",
                    bds_spool_warn_entries=512,
                    bds_spool_warn_bytes=1_073_741_824,
                    bds_disk_free_warn_bytes=4_294_967_296,
                )

            rendered_config = load_toml(config_path)
            rendered_xian_config = load_toml(home / "config" / "xian.toml")
            rendered_validator_key = json.loads(
                (home / "config" / "priv_validator_key.json").read_text(encoding="utf-8")
            )
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )

            self.assertEqual(rendered_config["moniker"], "updated-node")
            self.assertEqual(
                rendered_config["p2p"]["seeds"],
                "seed-1@127.0.0.1:26656",
            )
            self.assertEqual(
                rendered_config["p2p"]["persistent_peers"],
                "peer-1@127.0.0.1:26656",
            )
            self.assertEqual(rendered_config["rpc"]["laddr"], "tcp://0.0.0.0:30057")
            self.assertEqual(rendered_config["p2p"]["laddr"], "tcp://0.0.0.0:30056")
            self.assertEqual(rendered_config["db_backend"], "rocksdb")
            self.assertEqual(rendered_config["db_dir"], "custom-data")
            self.assertEqual(rendered_config["rpc"]["cors_allowed_origins"], [])
            self.assertTrue(rendered_config["statesync"]["enable"])
            self.assertEqual(
                rendered_config["statesync"]["rpc_servers"],
                "http://rpc-1.internal:26657,http://rpc-2.internal:26657",
            )
            self.assertEqual(rendered_config["statesync"]["trust_height"], 120)
            self.assertEqual(rendered_config["statesync"]["trust_hash"], "ab" * 32)
            self.assertEqual(rendered_config["statesync"]["trust_period"], "336h0m0s")
            self.assertNotIn("xian", rendered_config)
            self.assertTrue(rendered_xian_config["pruning_enabled"])
            self.assertEqual(rendered_xian_config["blocks_to_keep"], 5000)
            self.assertNotIn("tracer_mode", rendered_xian_config)
            self.assertTrue(rendered_xian_config["metrics_enabled"])
            self.assertEqual(rendered_xian_config["metrics_host"], "0.0.0.0")
            self.assertEqual(rendered_xian_config["metrics_port"], 9208)
            self.assertEqual(rendered_xian_config["metrics_bds_refresh_seconds"], 7.5)
            self.assertTrue(rendered_xian_config["transaction_trace_logging"])
            self.assertEqual(rendered_xian_config["app_log_level"], "WARNING")
            self.assertTrue(rendered_xian_config["app_log_json"])
            self.assertEqual(rendered_xian_config["app_log_rotation_hours"], 4)
            self.assertEqual(rendered_xian_config["app_log_retention_days"], 12)
            self.assertFalse(rendered_xian_config["simulation_enabled"])
            self.assertEqual(rendered_xian_config["simulation_max_concurrency"], 4)
            self.assertEqual(rendered_xian_config["simulation_timeout_ms"], 4500)
            self.assertEqual(rendered_xian_config["simulation_max_chi"], 750000)
            self.assertEqual(rendered_xian_config["tx_fee_mode"], "free_metered")
            self.assertEqual(rendered_xian_config["free_tx_max_chi"], 350_000)
            self.assertEqual(rendered_xian_config["free_block_max_chi"], 1_400_000)
            self.assertEqual(rendered_xian_config["bds"]["host"], "postgres")
            self.assertEqual(rendered_xian_config["bds"]["port"], 5544)
            self.assertEqual(rendered_xian_config["bds"]["database"], "xian_index")
            self.assertEqual(rendered_xian_config["bds"]["user"], "indexer")
            self.assertEqual(rendered_xian_config["bds"]["password"], "secret")
            self.assertEqual(rendered_xian_config["bds"]["pool_min_size"], 2)
            self.assertEqual(rendered_xian_config["bds"]["pool_max_size"], 6)
            self.assertEqual(rendered_xian_config["bds"]["statement_timeout_ms"], 5000)
            self.assertEqual(rendered_xian_config["bds"]["acquire_timeout_ms"], 15000)
            self.assertEqual(
                rendered_xian_config["bds"]["application_name"],
                "xian-bds-test",
            )
            self.assertEqual(rendered_xian_config["bds"]["queue_max_size"], 321)
            self.assertFalse(rendered_xian_config["bds"]["catchup_enabled"])
            self.assertEqual(rendered_xian_config["bds"]["catchup_poll_seconds"], 2.5)
            self.assertEqual(
                rendered_xian_config["bds"]["rpc_url"],
                "http://rpc.internal:26657",
            )
            self.assertEqual(
                rendered_xian_config["bds"]["spool_dir"],
                "/var/lib/xian/bds-spool",
            )
            self.assertEqual(rendered_xian_config["bds"]["spool_warn_entries"], 512)
            self.assertEqual(
                rendered_xian_config["bds"]["spool_warn_bytes"],
                1_073_741_824,
            )
            self.assertEqual(
                rendered_xian_config["bds"]["disk_free_warn_bytes"],
                4_294_967_296,
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-mainnet-next")
            self.assertIn("address", rendered_validator_key)
            self.assertEqual(
                result["config_path"],
                str(home / "config" / "config.toml"),
            )
            self.assertEqual(
                result["xian_config_path"],
                str(home / "config" / "xian.toml"),
            )
            self.assertEqual(result["p2p_seeds"], ["seed-1@127.0.0.1:26656"])
            self.assertEqual(
                result["p2p_persistent_peers"],
                ["peer-1@127.0.0.1:26656"],
            )

    def test_configure_existing_home_accepts_options_object(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""
""".strip()
            )
            write_toml(config_path, existing_config)

            result = configure_existing_home(
                options=ExistingHomeOptions(
                    moniker="updated-node",
                    validator_private_key_hex=(
                        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                    ),
                    home=home,
                    p2p_seeds=["seed-1@127.0.0.1:26656"],
                    node_config=NodeConfigOptions(
                        moniker="updated-node",
                        allow_cors=False,
                        metrics=MetricsOptions(port=9208),
                        bds=BdsOptions(
                            application_name="xian-bds-test",
                        ),
                    ),
                )
            )

            rendered_config = load_toml(config_path)
            rendered_xian_config = load_toml(home / "config" / "xian.toml")
            self.assertEqual(rendered_config["moniker"], "updated-node")
            self.assertEqual(
                rendered_config["p2p"]["seeds"],
                "seed-1@127.0.0.1:26656",
            )
            self.assertEqual(rendered_config["rpc"]["cors_allowed_origins"], [])
            self.assertNotIn("xian", rendered_config)
            self.assertEqual(rendered_xian_config["metrics_port"], 9208)
            self.assertEqual(
                rendered_xian_config["bds"]["application_name"],
                "xian-bds-test",
            )
            self.assertEqual(result["p2p_seeds"], ["seed-1@127.0.0.1:26656"])

    def test_configure_existing_home_accepts_network_first_genesis_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()
            )
            write_toml(config_path, existing_config)

            configs_dir = tmp_path / "xian-configs"
            network_dir = configs_dir / "networks" / "mainnet"
            network_dir.mkdir(parents=True)
            genesis_payload = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {},
            }
            (network_dir / "genesis.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                result = configure_existing_home(
                    home=home,
                    moniker="updated-node",
                    validator_private_key_hex=None,
                    genesis_source="mainnet",
                )

            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-local-1")
            self.assertEqual(
                result["genesis_path"],
                str(home / "config" / "genesis.json"),
            )

    def test_configure_existing_home_accepts_genesis_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = load_toml_string(
                """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()
            )
            write_toml(config_path, existing_config)

            genesis_payload = {
                "chain_id": "xian-preset-1",
                "validators": [
                    {
                        "address": "ABC",
                        "pub_key": {
                            "type": "tendermint/PubKeyEd25519",
                            "value": "pub",
                        },
                        "power": "10",
                        "name": "",
                    }
                ],
                "abci_genesis": {
                    "hash": "abc",
                    "number": "0",
                    "genesis": [],
                    "origin": {"sender": "", "signature": ""},
                },
            }

            result = configure_existing_home(
                home=home,
                moniker="updated-node",
                validator_private_key_hex=None,
                genesis_payload=genesis_payload,
            )

            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis, genesis_payload)
            self.assertEqual(
                result["genesis_path"],
                str(home / "config" / "genesis.json"),
            )


if __name__ == "__main__":
    unittest.main()
