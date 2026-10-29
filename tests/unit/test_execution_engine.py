import hashlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from contracting.artifacts import build_contract_artifacts
from contracting.compilation.compiler import ContractingCompiler
from contracting.execution.executor import Executor
from contracting.local import ContractingClient
from contracting.runtime_features import set_driver_runtime_features
from contracting.storage.driver import Driver
from xian_accounts import Ed25519Account
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import safe_repr
from xian_runtime_types.time import Datetime

from xian.execution_engine import (
    _load_vm_runtime_bindings,
    build_vm_runtime,
    clear_prepared_contract_cache,
    execute_vm_contract,
    execute_vm_transaction,
    prepare_vm_contract,
    vm_metering_writes,
)
from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json, normalize_for_abci_json, stringify_decimals


def augment_execution_output_with_driver_state(
    output: dict,
    *,
    before_state: dict | None,
    after_state: dict,
) -> dict:
    augmented = dict(output)
    merged_writes = dict(augmented.get("writes", {}))
    previous_pending = (
        {} if before_state is None else before_state["pending_writes"]
    )
    for key, value in after_state["pending_writes"].items():
        if key not in previous_pending or _normalize_value(
            previous_pending[key]
        ) != _normalize_value(value):
            merged_writes[key] = value
    augmented["writes"] = merged_writes
    return augmented


def compare_execution_results(
    authoritative_output: dict,
    native_output,
    *,
    ignore_write_keys: set[str] | None = None,
) -> dict:
    ignored = ignore_write_keys or set()
    expected_writes = {
        key: _normalize_value(value)
        for key, value in sorted(authoritative_output.get("writes", {}).items())
        if key not in ignored
    }
    actual_writes = {
        key: _normalize_value(value)
        for key, value in sorted(native_output.writes.items())
        if key not in ignored
    }
    mismatches = {}
    fields = {
        "status_code": (
            authoritative_output["status_code"],
            native_output.status_code,
        ),
        "result": (
            _normalize_value(authoritative_output["result"]),
            _normalize_value(native_output.result),
        ),
        "writes": (expected_writes, actual_writes),
        "events": (
            _normalize_value(authoritative_output.get("events", [])),
            _normalize_value(native_output.events),
        ),
    }
    for field, (expected, actual) in fields.items():
        if expected != actual:
            mismatches[field] = (expected, actual)
    return mismatches


def _normalize_value(value):
    if isinstance(value, BaseException):
        return safe_repr(value)
    return stringify_decimals(normalize_for_abci_json(value))


class ExecutionEngineRuntimeTests(unittest.TestCase):
    def setUp(self):
        _load_vm_runtime_bindings.cache_clear()
        clear_prepared_contract_cache()

    def test_build_runtime_for_vm_requires_native_package(self):
        with mock.patch.dict(sys.modules, {"xian_vm_core": None}):
            with self.assertRaisesRegex(ValueError, "xian-tech-vm-core"):
                build_vm_runtime()

    def test_build_runtime_for_vm_requires_supported_native_constants(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {"vm_profile": "xian_vm_v1"},
            supports_execution_policy=lambda *_args: False,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            with self.assertRaisesRegex(ValueError, "does not support"):
                build_vm_runtime()

    def test_build_runtime_for_vm_records_runtime_info(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_vm_runtime()

        self.assertEqual(runtime.runtime_info["vm_profile"], "xian_vm_v1")
        self.assertEqual(runtime.mode, "xian_vm_v1")

    def test_vm_metering_writes_uses_exact_decimal_division(self):
        driver = mock.Mock()
        driver.make_key.return_value = "currency.balances:alice"
        driver.get.return_value = ContractingDecimal("10000")

        writes = vm_metering_writes(
            driver,
            sender="alice",
            chi_used=10000,
            chi_cost=3,
        )

        self.assertEqual(
            writes["currency.balances:alice"],
            ContractingDecimal("6666.666666666666666666666666666667"),
        )

    def test_prepare_vm_contract_recurses_static_imports(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
            validate_module_ir=mock.Mock(),
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            built_runtime = build_vm_runtime()
            driver = mock.Mock()
            driver.get_contract_ir.side_effect = (
                lambda name, vm_profile="xian_vm_v1": {
                    "con_parent": (
                        ContractingCompiler(
                            module_name="con_parent"
                        ).lower_to_ir_json(
                            "import con_child\n\n@export\ndef ping():\n    return con_child.ping()\n",
                            vm_profile="xian_vm_v1",
                            indent=None,
                            sort_keys=True,
                        )
                    ),
                    "con_child": (
                        ContractingCompiler(
                            module_name="con_child"
                        ).lower_to_ir_json(
                            "@export\ndef ping():\n    return 'pong'\n",
                            vm_profile="xian_vm_v1",
                            indent=None,
                            sort_keys=True,
                        )
                    ),
                }.get(name)
            )
            driver.get_contract_source.return_value = None
            prepared = prepare_vm_contract(
                built_runtime,
                driver,
                "con_parent",
            )

        self.assertEqual(prepared.contract_name, "con_parent")
        self.assertEqual(prepared.imported_contracts, ("con_child",))
        self.assertEqual(fake_bindings.validate_module_ir.call_count, 2)
        driver.get_contract_source.assert_not_called()

    def test_prepare_vm_contract_accepts_persisted_vm_ir(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
            validate_module_ir=mock.Mock(),
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_vm_runtime()

            driver = mock.Mock()
            parent_ir = ContractingCompiler(
                module_name="con_parent"
            ).lower_to_ir_json(
                "import con_child\n\n@export\ndef ping():\n    return con_child.ping()\n",
                vm_profile="xian_vm_v1",
                indent=None,
                sort_keys=True,
            )
            child_ir = ContractingCompiler(
                module_name="con_child"
            ).lower_to_ir_json(
                "@export\ndef ping():\n    return 'pong'\n",
                vm_profile="xian_vm_v1",
                indent=None,
                sort_keys=True,
            )
            driver.get_contract_ir.side_effect = (
                lambda name, vm_profile="xian_vm_v1": {
                    "con_parent": parent_ir,
                    "con_child": child_ir,
                }.get(name)
            )
            driver.get_contract_source.return_value = None

            prepared = prepare_vm_contract(
                runtime,
                driver,
                "con_parent",
            )

        self.assertEqual(prepared.contract_name, "con_parent")
        self.assertEqual(prepared.imported_contracts, ("con_child",))
        self.assertEqual(fake_bindings.validate_module_ir.call_count, 2)
        driver.get_contract_source.assert_not_called()

    def test_execute_native_submission_deploy_stages_contract_artifacts(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        code = (
            "v = Variable()\n\n"
            "@construct\n"
            "def seed(value: int = 1):\n"
            "    v.set(value)\n\n"
            "@export\n"
            "def ping():\n"
            "    return v.get()\n"
        )
        output = execute_vm_contract(
            runtime,
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_native_submission_probe",
                "code": code,
                "constructor_args": {"value": 9},
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=True,
            chi_budget=10_000,
            transaction_size_bytes=256,
        )

        self.assertEqual(output.status_code, 0)
        self.assertIn("con_native_submission_probe.__source__", output.writes)
        self.assertIn(
            "con_native_submission_probe.__xian_ir_v1__", output.writes
        )
        self.assertEqual(output.writes["con_native_submission_probe.v"], 9)
        self.assertTrue(
            any(
                event.get("event") == "ContractDeployed"
                for event in output.events
            )
        )
        self.assertGreater(output.chi_used, 0)
        self.assertIn("submission", output.contract_costs)
        driver.flush_full()

    def test_execute_native_submission_rejects_submitted_ir_artifacts(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        source = "@export\ndef ping():\n    return 'pong'\n"
        forged_artifacts = {
            "format": "xian_contract_artifact_v1",
            "module_name": "con_forged_ir_probe",
            "vm_profile": "xian_vm_v1",
            "source": source,
            "vm_ir_json": "{}",
            "hashes": {},
        }

        output = execute_vm_contract(
            runtime,
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_forged_ir_probe",
                "code": source,
                "deployment_artifacts": forged_artifacts,
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("deployment_artifacts", str(output.result))
        self.assertEqual(output.writes, {})
        driver.flush_full()

    def test_execute_native_submission_rejects_zk_contract_when_feature_disabled(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        set_driver_runtime_features(driver, {"zk": False})
        code = (
            "@export\n"
            "def check():\n"
            "    return zk.is_available()\n"
        )
        output = execute_vm_contract(
            runtime,
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_zk_probe",
                "code": code,
                "constructor_args": {},
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("zk runtime feature is disabled", str(output.result))
        driver.flush_full()

    def test_prepare_vm_contract_rejects_zk_contract_when_feature_disabled(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        set_driver_runtime_features(driver, {"zk": False})
        code = (
            "@export\n"
            "def check():\n"
            "    return zk.is_available()\n"
        )
        artifacts = build_contract_artifacts(
            module_name="con_zk_probe",
            source=code,
            lint=True,
            vm_profile="xian_vm_v1",
        )
        driver.set_contract(
            name="con_zk_probe",
            source=artifacts["source"],
            vm_ir_json=artifacts["vm_ir_json"],
        )

        with self.assertRaisesRegex(ValueError, "zk runtime feature is disabled"):
            prepare_vm_contract(runtime, driver, "con_zk_probe")
        driver.flush_full()

    def test_execute_native_submission_change_owner_stages_metadata_write(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        driver.set_contract_from_source(
            "con_owned",
            "@export\ndef ping():\n    return 1\n",
            owner="alice",
            developer="alice",
            deployer="alice",
            initiator="alice",
        )

        output = execute_vm_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="submission",
            function_name="change_owner",
            kwargs={"contract": "con_owned", "new_owner": "bob"},
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
        )

        self.assertEqual(output.status_code, 0)
        self.assertEqual(output.writes["con_owned.__owner__"], "bob")
        self.assertTrue(
            any(
                event.get("event") == "ContractOwnerChanged"
                for event in output.events
            )
        )
        driver.flush_full()

    def test_execute_native_token_factory_deploys_child_contract(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        token_factory_source = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
            / "token_factory.s.py"
        ).read_text()
        driver.set_contract_from_source(
            "token_factory",
            token_factory_source,
            owner="sys",
            developer="sys",
            deployer="sys",
            initiator="sys",
        )

        output = execute_vm_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="token_factory",
            function_name="create_token",
            kwargs={
                "token_contract": "con_demo_token",
                "token_name": "Demo Token",
                "token_symbol": "DEMO",
                "initial_supply": 42,
                "initial_holder": "bob",
                "operator_address": "carol",
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=True,
            chi_budget=100_000,
            transaction_size_bytes=512,
        )

        self.assertEqual(output.status_code, 0)
        self.assertIn("con_demo_token.__source__", output.writes)
        self.assertIn("con_demo_token.__xian_ir_v1__", output.writes)
        self.assertEqual(output.writes["con_demo_token.balances:bob"], 42)
        self.assertEqual(
            output.writes["con_demo_token.metadata:token_symbol"],
            "DEMO",
        )
        self.assertEqual(output.writes["con_demo_token.operator"], "carol")
        self.assertTrue(
            any(
                event.get("event") == "ContractDeployed"
                for event in output.events
            )
        )
        self.assertTrue(
            any(event.get("event") == "TokenCreated" for event in output.events)
        )
        self.assertIn("token_factory", output.contract_costs)
        self.assertIn("submission", output.contract_costs)
        driver.flush_full()

    def test_execute_native_members_reregister_after_removal_matches_python_runtime(
        self,
    ):
        contracts_dir = (
            Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        )
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            for name, constructor_args in (
                ("currency", {"vk": "node1"}),
                ("dao", None),
                ("rewards", None),
                ("chi_cost", {"initial_rate": 20}),
            ):
                client.submit(
                    (contracts_dir / f"{name}.s.py").read_text(),
                    name=name,
                    constructor_args=constructor_args,
                    owner="members" if name in {"dao", "rewards"} else None,
                )

            for node in ("node1", "node2", "node3", "node4", "node5"):
                client.raw_driver.set(
                    client.raw_driver.make_key("currency", "balances", [node]),
                    1_000_000,
                )
            client.raw_driver.set("rewards.S:value", [0.88, 0.01, 0.01, 0.1])

            client.submit(
                (contracts_dir / "validators.s.py").read_text(),
                name="members",
                constructor_args={
                    "genesis_registration_fee": 100_000,
                    "genesis_nodes": [
                        "node1",
                        "node2",
                        "node3",
                        "node4",
                        "node5",
                    ],
                },
            )

            runtime = build_vm_runtime()
            processor = TxProcessor(
                client=client,
                execution_runtime=runtime,
            )

            def tx(sender, contract, function, kwargs, nonce, height):
                return {
                    "payload": {
                        "sender": sender,
                        "contract": contract,
                        "function": function,
                        "kwargs": kwargs,
                        "nonce": nonce,
                        "chi_supplied": 200_000,
                        "chain_id": "test-chain",
                    },
                    "metadata": {"signature": "abc"},
                    "b_meta": {
                        "nanos": height * 1_000_000_000,
                        "hash": f"0x{height:064x}",
                        "height": height,
                        "chain_id": "test-chain",
                    },
                }

            remove_flow = (
                tx(
                    "node1",
                    "members",
                    "propose_vote",
                    {"type_of_vote": "remove_member", "arg": "node3"},
                    1,
                    1,
                ),
                tx(
                    "node2",
                    "members",
                    "vote",
                    {"proposal_id": 1, "vote": "yes"},
                    1,
                    2,
                ),
                tx(
                    "node4",
                    "members",
                    "vote",
                    {"proposal_id": 1, "vote": "yes"},
                    1,
                    3,
                ),
                tx(
                    "node5",
                    "members",
                    "vote",
                    {"proposal_id": 1, "vote": "yes"},
                    1,
                    4,
                ),
            )
            for call in remove_flow:
                result = processor.process_tx(call, fee_policy=TxFeePolicy.paid_metered())
                self.assertIsNotNone(result["tx_result"])
                self.assertEqual(result["tx_result"]["status"], 0)

            approve_result = processor.process_tx(
                tx(
                    "node3",
                    "currency",
                    "approve",
                    {"amount": 100_000, "to": "members"},
                    1,
                    5,
                ),
                fee_policy=TxFeePolicy.paid_metered(),
            )
            self.assertEqual(approve_result["tx_result"]["status"], 0)

            reregister_result = processor.process_tx(
                tx(
                    "node3",
                    "members",
                    "register",
                    {
                        "requested_validator_power": 12,
                        "moniker": "node-3-return",
                        "network_endpoint": "localnet://node-3",
                    },
                    2,
                    6,
                ),
                fee_policy=TxFeePolicy.paid_metered(),
            )

            self.assertIsNotNone(reregister_result["tx_result"])
            self.assertEqual(reregister_result["tx_result"]["status"], 0)
            self.assertEqual(
                client.raw_driver.get("members.pending_registrations:node3"),
                True,
            )
            self.assertIsNone(client.raw_driver.get("members.joined_at:node3"))
            self.assertIsNone(client.raw_driver.get("members.left_at:node3"))
        finally:
            client.flush()

    def test_execute_native_permit_authorizer_matches_python_runtime(self):
        contracts_dir = (
            Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        )
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            client.submit(
                (contracts_dir / "permit_authorizer.s.py").read_text(),
                name="permit_authorizer",
            )
            client.submit(
                (contracts_dir / "currency.s.py").read_text(),
                name="currency",
                constructor_args={"vk": "sys"},
            )
            client.get_contract_proxy("currency").balances["sys"] = 100_000

            owner = (
                "ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791"
            )
            deadline = str(Datetime(2026, 4, 12, 12, 1))
            permit_msg = "\n".join(
                [
                    "xian-permit-v2",
                    "chain_id:test-chain",
                    "authorizer:permit_authorizer",
                    "token_contract:currency",
                    f"owner:{owner}",
                    "spender:some_spender",
                    "amount:100",
                    f"deadline:{deadline}",
                    "nonce:0",
                ]
            )
            permit_hash = hashlib.sha3_256(
                permit_msg.encode("utf-8")
            ).hexdigest()

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                client.raw_driver,
                sender="sys",
                contract_name="permit_authorizer",
                function_name="permit",
                kwargs={
                    "token_contract": "currency",
                    "owner": owner,
                    "spender": "some_spender",
                    "value": 100,
                    "deadline": deadline,
                    "nonce": 0,
                    "signature": Ed25519Account(
                        "ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8"
                    ).sign_msg(permit_msg),
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 23,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=15_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=512,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.output.writes[
                    "currency.approvals:"
                    "ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791:"
                    "some_spender"
                ],
                100,
            )
            self.assertTrue(
                outcome.output.writes[f"permit_authorizer.permits:{permit_hash}"]
            )
            self.assertEqual(
                outcome.output.writes[
                    "permit_authorizer.nonces:"
                    "ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791"
                ],
                1,
            )
            self.assertTrue(
                any(
                    event.get("contract") == "currency"
                    and event.get("event") == "Approve"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_currency_transfer_coerces_float_kwargs_like_python(
        self,
    ):
        contracts_dir = (
            Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        )
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            client.submit(
                (contracts_dir / "currency.s.py").read_text(),
                name="currency",
                constructor_args={"vk": "sys"},
            )
            client.raw_driver.delete("currency.__owner__")
            client.raw_driver.set(
                client.raw_driver.make_key("currency", "balances", ["worker0"]),
                ContractingDecimal("5000"),
            )
            client.raw_driver.set(
                client.raw_driver.make_key("currency", "balances", ["worker1"]),
                ContractingDecimal("5000"),
            )
            client.raw_driver.commit()

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                client.raw_driver,
                sender="worker0",
                contract_name="currency",
                function_name="transfer",
                kwargs={"amount": 1.0, "to": "worker1"},
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 40,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=1_500,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=936,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.writes["currency.balances:worker0"],
                ContractingDecimal("4999")
                - (
                    ContractingDecimal(outcome.chi_used)
                    / ContractingDecimal(20)
                ),
            )
            self.assertEqual(
                outcome.output.writes["currency.balances:worker1"],
                ContractingDecimal("5001"),
            )
            self.assertTrue(
                any(
                    event.get("contract") == "currency"
                    and event.get("event") == "Transfer"
                    and event.get("data", {}).get("amount")
                    == ContractingDecimal("1")
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_submission_coerces_constructor_arg_floats(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir
            / "xian-stack"
            / "workloads"
            / "dex_mixed"
            / "token_fixture.py"
        ).read_text()
        contract_name = "con_tokena_probe"
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            client.raw_driver.set("currency.balances:sys", 100_000)
            client.raw_driver.commit()

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                client.raw_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": contract_name,
                    "code": source,
                    "constructor_args": {
                        "owner": "sys",
                        "supply": 5_000_000.0,
                        "name": "Workload Token A",
                        "symbol": "WTA",
                    },
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 54,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=150_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.output.writes[f"{contract_name}.balances:sys"],
                ContractingDecimal("5000000"),
            )
        finally:
            client.flush()

    def test_execute_native_submission_large_artifact_deployment_succeeds(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir / "xian-stack" / "workloads" / "dex_mixed" / "con_pairs.py"
        ).read_text()
        contract_name = "con_pairs_probe"
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            client.raw_driver.set("currency.balances:sys", 250_000)
            client.raw_driver.commit()

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                client.raw_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": contract_name,
                    "code": source,
                    "constructor_args": {},
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 56,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=300_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertIn(
                f"{contract_name}.__xian_ir_v1__", outcome.output.writes
            )
        finally:
            client.flush()

    def test_execute_native_submission_compact_shielded_artifacts_match_python_runtime(
        self,
    ):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir
            / "xian-contracts"
            / "contracts"
            / "shielded-note-token"
            / "src"
            / "con_shielded_note_token.py"
        ).read_text()
        zk_registry_source = (
            root_dir / "xian-configs" / "contracts" / "zk_registry.s.py"
        ).read_text()
        contract_name = "con_shielded_note_probe"
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            client.raw_driver.set("currency.balances:sys", 100_000_000)
            client.raw_driver.commit()
            client.submit(
                zk_registry_source,
                name="zk_registry",
                owner="governance",
            )

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                client.raw_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": contract_name,
                    "code": source,
                    "constructor_args": {
                        "token_name": "Local Private USD",
                        "token_symbol": "lpUSD",
                        "operator_address": "sys",
                        "root_window_size": 32,
                    },
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 58,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=25_000_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertIn(
                f"{contract_name}.__xian_ir_v1__", outcome.output.writes
            )
            self.assertEqual(
                outcome.output.writes[f"{contract_name}.metadata:token_symbol"],
                "lpUSD",
            )
            self.assertTrue(
                any(
                    event.get("event") == "ContractDeployed"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_shielded_configure_vk_matches_python_runtime(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir
            / "xian-contracts"
            / "contracts"
            / "shielded-note-token"
            / "src"
            / "con_shielded_note_token.py"
        ).read_text()
        zk_registry_source = (
            root_dir / "xian-configs" / "contracts" / "zk_registry.s.py"
        ).read_text()
        contract_name = "con_shielded_note_probe"
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            driver = client.raw_driver
            driver.set("currency.balances:sys", 100_000_000)
            driver.commit()
            client.submit(
                zk_registry_source,
                name="zk_registry",
                owner="sys",
            )
            client.submit(
                source,
                name=contract_name,
                constructor_args={
                    "token_name": "Local Private USD",
                    "token_symbol": "lpUSD",
                    "operator_address": "sys",
                    "root_window_size": 32,
                },
            )
            registry = client.get_contract_proxy("zk_registry")
            registry.register_vk(
                vk_id="demo-note",
                vk_hex="0x1234",
                circuit_name="demo",
                version="1",
                circuit_family="shielded_note_v4",
                statement_version="4",
                tree_depth=20,
                leaf_capacity=2**20,
                max_inputs=4,
                max_outputs=4,
                setup_mode="dev",
                setup_ceremony="test",
                artifact_hash="0x12",
                bundle_hash="0x34",
                warning="",
            )

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                driver,
                sender="sys",
                contract_name=contract_name,
                function_name="configure_vk",
                kwargs={
                    "action": "deposit",
                    "vk_id": "demo-note",
                },
                environment={
                    "now": Datetime(2026, 4, 13, 1, 0),
                    "block_num": 250,
                    "block_hash": "block-250",
                    "chain_id": "test-chain",
                },
                chi_budget=500_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=0,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(outcome.output.result, "demo-note")
            self.assertEqual(
                outcome.output.writes[f"{contract_name}.vk_ids:deposit"],
                "demo-note",
            )
            self.assertTrue(
                any(
                    event.get("event") == "VerifyingKeyConfigured"
                    and event.get("data_indexed", {}).get("action") == "deposit"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_hash_prefix_scan_matches_python_runtime(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir
            / "xian-stack"
            / "workloads"
            / "parallel_probe"
            / "con_parallel_probe.py"
        ).read_text()

        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            driver = client.raw_driver
            driver.set("currency.balances:sys", 100_000_000)
            driver.commit()
            client.submit(source, name="con_parallel_probe", owner="sys")
            driver.set("con_parallel_probe.values:g:seed", 13)
            driver.commit()

            runtime = build_vm_runtime()
            outcome = execute_vm_transaction(
                runtime,
                driver,
                sender="sys",
                contract_name="con_parallel_probe",
                function_name="snapshot_sum",
                kwargs={"group": "g", "tag": "obs-1"},
                environment={
                    "now": Datetime(2026, 4, 13, 1, 0),
                    "block_num": 251,
                    "block_hash": "block-251",
                    "chain_id": "test-chain",
                },
                chi_budget=2_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(outcome.output.result, 13)
            self.assertEqual(
                outcome.output.writes["con_parallel_probe.observations:obs-1"],
                13,
            )
            self.assertIn("con_parallel_probe.values:g:", outcome.prefix_reads)
        finally:
            client.flush()

    def test_execute_native_dex_add_liquidity_matches_python_runtime(self):
        root_dir = Path(__file__).resolve().parents[3]
        workload_dir = root_dir / "xian-stack" / "workloads" / "dex_mixed"
        token_source = (workload_dir / "token_fixture.py").read_text()
        pairs_source = (workload_dir / "con_pairs.py").read_text()
        pairs_name = "con_pairs_probe"
        dex_name = "con_dex_probe"
        dex_source = (
            (workload_dir / "con_dex.py")
            .read_text()
            .replace(
                'DEX_PAIRS = "con_pairs"',
                f'DEX_PAIRS = "{pairs_name}"',
                1,
            )
        )

        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        driver = client.raw_driver
        executor = Executor(driver=driver)
        runtime = build_vm_runtime()

        def apply_output_writes(output: dict[str, object]) -> None:
            for key, value in output["writes"].items():
                driver.set(key, value)
            driver.commit()

        def execute_python(
            *,
            sender: str,
            contract_name: str,
            function_name: str,
            kwargs: dict[str, object],
            environment: dict[str, object],
            chi: int,
            transaction_size_bytes: int,
        ) -> dict[str, object]:
            before_state = driver.snapshot_state()
            output = executor.execute(
                sender=sender,
                contract_name=contract_name,
                function_name=function_name,
                kwargs=kwargs,
                environment=environment,
                auto_commit=False,
                metering=True,
                chi=chi,
                chi_cost=20,
                transaction_size_bytes=transaction_size_bytes,
            )
            output = augment_execution_output_with_driver_state(
                output,
                before_state=before_state,
                after_state=driver.snapshot_state(),
            )
            driver.restore_state(before_state)
            return output

        def deploy_contract(
            name: str,
            source: str,
            constructor_args: dict[str, object],
            *,
            chi: int,
        ) -> None:
            output = execute_python(
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": name,
                    "code": source,
                    "constructor_args": constructor_args,
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 1,
                    "block_hash": "deploy-block",
                    "chain_id": "test-chain",
                },
                chi=chi,
                transaction_size_bytes=len(source.encode("utf-8")),
            )
            self.assertEqual(output["status_code"], 0)
            apply_output_writes(output)

        try:
            driver.set("currency.balances:sys", 1_000_000)
            driver.commit()

            deploy_contract(
                "con_tokena_probe",
                token_source,
                {
                    "owner": "sys",
                    "supply": 5_000_000.0,
                    "name": "Token A",
                    "symbol": "TA",
                },
                chi=150_000,
            )
            deploy_contract(
                "con_tokenb_probe",
                token_source,
                {
                    "owner": "sys",
                    "supply": 5_000_000.0,
                    "name": "Token B",
                    "symbol": "TB",
                },
                chi=150_000,
            )
            deploy_contract(pairs_name, pairs_source, {}, chi=300_000)
            deploy_contract(dex_name, dex_source, {}, chi=200_000)

            execution_environment = {
                "now": Datetime(2026, 4, 12, 12, 5),
                "block_num": 77,
                "block_hash": "block-77",
                "chain_id": "test-chain",
            }
            for token_name in ("con_tokena_probe", "con_tokenb_probe"):
                approval = execute_python(
                    sender="sys",
                    contract_name=token_name,
                    function_name="approve",
                    kwargs={
                        "amount": 500_000.0,
                        "to": dex_name,
                    },
                    environment=execution_environment,
                    chi=7_500,
                    transaction_size_bytes=0,
                )
                self.assertEqual(approval["status_code"], 0)
                apply_output_writes(approval)

            outcome = execute_vm_transaction(
                runtime,
                driver,
                sender="sys",
                contract_name=dex_name,
                function_name="addLiquidity",
                kwargs={
                    "tokenA": "con_tokena_probe",
                    "tokenB": "con_tokenb_probe",
                    "amountADesired": 250_000.0,
                    "amountBDesired": 250_000.0,
                    "amountAMin": 240_000.0,
                    "amountBMin": 240_000.0,
                    "to": "sys",
                    "deadline": Datetime(2026, 4, 12, 12, 10),
                },
                environment=execution_environment,
                chi_budget=60_000,
                chi_cost=20,
                meter=True,
                transaction_size_bytes=0,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.output.result,
                (
                    ContractingDecimal("250000"),
                    ContractingDecimal("250000"),
                    ContractingDecimal("249999.99999999"),
                ),
            )
            self.assertEqual(
                outcome.output.writes["con_pairs_probe.pairs:1:reserve0"],
                ContractingDecimal("250000"),
            )
            self.assertEqual(
                outcome.output.writes["con_pairs_probe.pairs:1:reserve1"],
                ContractingDecimal("250000"),
            )
            self.assertTrue(
                any(
                    event.get("contract") == "con_pairs_probe"
                    and event.get("event") == "Mint"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_prepare_vm_contract_requires_persisted_ir(
        self,
    ):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_vm_runtime()

        driver = mock.Mock()
        driver.get_contract_ir.return_value = None
        driver.get_contract_source.return_value = (
            "@export\ndef ping():\n    return 'source only'\n"
        )

        with self.assertRaisesRegex(
            ValueError,
            "requires persisted __xian_ir_v1__",
        ):
            prepare_vm_contract(runtime, driver, "con_missing")

        driver.get_contract_source.assert_not_called()

    def test_augment_execution_output_with_driver_state_merges_hidden_writes(
        self,
    ):
        before_state = {
            "pending_writes": {"currency.balances:alice": 1_000_000},
        }
        after_state = {
            "pending_writes": {
                "currency.balances:alice": "999966.05",
                "con_demo.__source__": "source",
                "con_demo.counter": 0,
            },
        }

        augmented = augment_execution_output_with_driver_state(
            {
                "status_code": 0,
                "result": None,
                "writes": {"currency.balances:alice": "999966.05"},
                "events": [],
            },
            before_state=before_state,
            after_state=after_state,
        )

        self.assertEqual(
            augmented["writes"],
            {
                "currency.balances:alice": "999966.05",
                "con_demo.__source__": "source",
                "con_demo.counter": 0,
            },
        )

    def test_execute_vm_contract_converts_runtime_errors(self):
        runtime = types.SimpleNamespace(mode="xian_vm_v1")
        driver = mock.Mock()
        driver.get_owner.return_value = "alice"
        fake_bindings = types.SimpleNamespace(
            execute_contract=mock.Mock(side_effect=RuntimeError("boom"))
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            output = execute_vm_contract(
                runtime,
                driver,
                sender="alice",
                contract_name="currency",
                function_name="transfer",
                kwargs={"amount": 5},
                environment={
                    "now": "ts",
                    "block_num": 7,
                    "block_hash": "abc",
                    "chain_id": "xian-local",
                },
            )

        self.assertEqual(output.status_code, 1)
        self.assertEqual(str(output.result), "boom")
        self.assertEqual(output.writes, {})
        self.assertEqual(output.events, [])

    def test_execute_vm_contract_fails_outputs_with_non_string_dict_keys(self):
        runtime = types.SimpleNamespace(mode="xian_vm_v1")
        driver = mock.Mock()
        driver.get_owner.return_value = None
        outputs = [
            {"result": {1: 2}, "writes": {}, "events": []},
            {"result": None, "writes": {"con_demo.v": [{"a": {5: 1}}]}, "events": []},
            {"result": None, "writes": {}, "events": [{"data": {(1, 2): "x"}}]},
        ]

        for fields in outputs:
            fake_bindings = types.SimpleNamespace(
                execute_contract=mock.Mock(
                    return_value=types.SimpleNamespace(
                        status_code=0, chi_used=7, contract_costs={}, **fields
                    )
                )
            )
            with mock.patch(
                "xian.execution_engine._load_vm_runtime_bindings",
                return_value=fake_bindings,
            ):
                output = execute_vm_contract(
                    runtime,
                    driver,
                    sender="alice",
                    contract_name="con_demo",
                    function_name="f",
                    kwargs={},
                    environment={},
                )

            self.assertEqual(output.status_code, 1)
            self.assertEqual(str(output.result), "Non-string key types not allowed.")
            self.assertEqual(output.writes, {})
            self.assertEqual(output.events, [])
            self.assertEqual(output.chi_used, 7)

    def test_execute_native_non_string_dict_key_fails_the_transaction(self):
        source = (
            "v = Variable()\n"
            "h = Hash()\n\n"
            "@export\n"
            "def store():\n"
            "    v.set({1: 2})\n\n"
            "@export\n"
            "def store_nested():\n"
            "    h['k'] = [{'a': {5: 1}}]\n\n"
            "@export\n"
            "def give():\n"
            "    return {1: 2}\n"
        )
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            driver = client.raw_driver
            driver.set("currency.balances:sys", 100_000_000)
            driver.commit()
            client.submit(source, name="con_int_keys", owner="sys")
            driver.commit()

            runtime = build_vm_runtime()
            for function_name in ("store", "store_nested", "give"):
                outcome = execute_vm_transaction(
                    runtime,
                    driver,
                    sender="sys",
                    contract_name="con_int_keys",
                    function_name=function_name,
                    kwargs={},
                    environment={
                        "now": Datetime(2026, 4, 13, 1, 0),
                        "block_num": 1,
                        "block_hash": "block-1",
                        "chain_id": "test-chain",
                    },
                    chi_budget=2_000,
                    chi_cost=20,
                    meter=True,
                )
                self.assertEqual(outcome.output.status_code, 1, function_name)
                self.assertFalse(
                    any(key.startswith("con_int_keys.") for key in outcome.writes),
                    function_name,
                )
                encode_abci_json({"result": outcome.output.result, "writes": outcome.writes})
        finally:
            client.flush()

    def test_execute_vm_contract_tolerates_missing_get_owner(self):
        runtime = types.SimpleNamespace(mode="xian_vm_v1")
        driver = types.SimpleNamespace()
        captured = {}

        def fake_execute_contract(**kwargs):
            captured["context"] = kwargs["context"]
            return types.SimpleNamespace(
                status_code=0,
                result="ok",
                writes={},
                events=[],
                chi_used=0,
                contract_costs={},
            )

        fake_bindings = types.SimpleNamespace(
            execute_contract=fake_execute_contract
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            output = execute_vm_contract(
                runtime,
                driver,
                sender="alice",
                contract_name="currency",
                function_name="transfer",
                kwargs={"amount": 5},
                environment={
                    "now": "ts",
                    "block_num": 7,
                    "block_hash": "abc",
                    "chain_id": "xian-local",
                },
            )

        self.assertEqual(output.status_code, 0)
        self.assertIsNone(captured["context"]["owner"])

    def test_execute_vm_contract_rejects_source_only_contracts(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        driver.set_contract_from_source(
            "con_source_only",
            "@export\ndef ping():\n    return 'pong'\n",
            owner="alice",
            developer="alice",
            deployer="alice",
            initiator="alice",
        )
        driver.delete(driver.make_key("con_source_only", "__xian_ir_v1__"))

        output = execute_vm_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="con_source_only",
            function_name="ping",
            kwargs={},
            environment={},
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("requires persisted __xian_ir_v1__", str(output.result))

    def test_execute_native_submission_requires_deterministic_now(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        source = "@export\ndef ping():\n    return 'pong'\n"

        output = execute_vm_contract(
            runtime,
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_deterministic_probe",
                "code": source,
            },
            environment={},
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("deterministic now context", str(output.result))

    def test_execute_native_submission_enforces_constructor_write_capacity_limit(
        self,
    ):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        source = (
            "blob = Variable()\n\n"
            "@construct\n"
            "def seed(payload: str):\n"
            "    blob.set(payload)\n\n"
            "@export\n"
            "def blob_size():\n"
            "    return len(blob.get())\n"
        )
        output = execute_vm_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_write_limit_probe",
                "code": source,
                "constructor_args": {"payload": "a" * 140_000},
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
                "__xian_execution_mode__": "xian_vm_v1",
            },
            meter=True,
            chi_budget=180_000,
            transaction_size_bytes=150_000,
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("maximum write capacity", str(output.result))
        self.assertEqual(output.writes, {})
        self.assertEqual(output.events, [])

    def test_dynamic_importlib_call_matches_python_runtime(self):
        runtime = build_vm_runtime()
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        driver.set(
            driver.make_key("currency", "balances", ["alice"]), 1_000_000
        )

        environment = {
            "now": Datetime(2026, 4, 12, 12, 0),
            "block_num": 7,
            "block_hash": "abc123",
            "chain_id": "xian-local",
            "__xian_execution_mode__": "xian_vm_v1",
        }
        submission_environment = dict(environment)
        submission_environment.pop("__xian_execution_mode__", None)
        executor = Executor(driver=driver, metering=True)

        leaf_name = "con_dynamic_leaf"
        leaf_source = (
            "touch_total = Variable()\n\n"
            "@construct\n"
            "def seed():\n"
            "    touch_total.set(0)\n\n"
            "@export\n"
            "def touch(account: str, amount: int):\n"
            "    touch_total.set((touch_total.get() or 0) + amount)\n"
            "    return touch_total.get()\n"
        )
        router_name = "con_dynamic_router"
        router_source = (
            "@export\n"
            "def dynamic_touch(target_contract: str, function_name: str, account: str, amount: int):\n"
            "    return {\n"
            "        'router_ctx': {\n"
            "            'this': ctx.this,\n"
            "            'caller': ctx.caller,\n"
            "            'signer': ctx.signer,\n"
            "            'entry': f'{ctx.entry[0]}.{ctx.entry[1]}',\n"
            "        },\n"
            "        'result': importlib.call(\n"
            "            target_contract,\n"
            "            function_name,\n"
            "            {'account': account, 'amount': amount},\n"
            "        ),\n"
            "    }\n"
        )

        for name, source in (
            (leaf_name, leaf_source),
            (router_name, router_source),
        ):
            output = executor.execute(
                sender="alice",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": name,
                    "code": source,
                    "constructor_args": {},
                },
                environment=submission_environment,
                auto_commit=False,
                metering=True,
                chi=180_000,
                transaction_size_bytes=len(source.encode("utf-8")),
            )
            output = augment_execution_output_with_driver_state(
                output,
                before_state=driver.snapshot_state(),
                after_state=driver.snapshot_state(),
            )
            for key, value in output["writes"].items():
                driver.set(key, value)
            driver.commit()
            driver.set(
                driver.make_key("currency", "balances", ["alice"]), 1_000_000
            )
            driver.commit()

        call_kwargs = {
            "target_contract": leaf_name,
            "function_name": "touch",
            "account": "alice",
            "amount": 3,
        }
        before_state = driver.snapshot_state()
        native_output = execute_vm_contract(
            runtime,
            driver,
            sender="alice",
            contract_name=router_name,
            function_name="dynamic_touch",
            kwargs=call_kwargs,
            environment=environment,
            meter=False,
            chi_budget=0,
            transaction_size_bytes=0,
        )
        driver.restore_state(before_state)
        python_output = executor.execute(
            sender="alice",
            contract_name=router_name,
            function_name="dynamic_touch",
            kwargs=call_kwargs,
            environment=environment,
            auto_commit=False,
            metering=False,
            transaction_size_bytes=0,
        )
        python_output = augment_execution_output_with_driver_state(
            python_output,
            before_state=before_state,
            after_state=driver.snapshot_state(),
        )
        driver.restore_state(before_state)

        mismatches = compare_execution_results(
            python_output,
            native_output,
        )

        self.assertEqual(native_output.status_code, 0)
        self.assertEqual(
            native_output.result,
            {
                "router_ctx": {
                    "this": router_name,
                    "caller": "alice",
                    "signer": "alice",
                    "entry": f"{router_name}.dynamic_touch",
                },
                "result": 3,
            },
        )
        self.assertEqual(
            native_output.writes,
            {f"{leaf_name}.touch_total": 3},
        )
        self.assertEqual(mismatches, {})

    def test_compare_execution_results_reports_mismatched_fields(self):
        native_output = types.SimpleNamespace(
            status_code=0,
            result={"amount": 7},
            writes={"currency.balances:alice": 7},
            events=[],
        )

        mismatches = compare_execution_results(
            {
                "status_code": 0,
                "result": {"amount": 5},
                "writes": {"currency.balances:alice": 5},
                "events": [],
            },
            native_output,
        )

        self.assertEqual(sorted(mismatches), ["result", "writes"])

    def test_compare_execution_results_can_ignore_metering_write_keys(self):
        native_output = types.SimpleNamespace(
            status_code=0,
            result={"amount": 5},
            writes={"demo.balances:alice": 5},
            events=[],
        )

        mismatches = compare_execution_results(
            {
                "status_code": 0,
                "result": {"amount": 5},
                "writes": {
                    "demo.balances:alice": 5,
                    "currency.balances:alice": "999.35",
                },
                "events": [],
            },
            native_output,
            ignore_write_keys={"currency.balances:alice"},
        )

        self.assertEqual(mismatches, {})
