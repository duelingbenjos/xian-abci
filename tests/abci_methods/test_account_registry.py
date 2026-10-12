"""Real ABCI/storage migration rehearsals. All keys are ephemeral test keys."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures.mock_constants import MockConstants
from nacl.signing import SigningKey
from utils import setup_fixtures, teardown_fixtures
from xian_accounts import hybrid
from xian_runtime_types.decimal import ContractingDecimal

from cometbft.abci.v1beta3.types_pb2 import (
    RequestFinalizeBlock,
    RequestPrepareProposal,
    RequestProcessProposal,
)
from xian.account_registry import POLICY_KEY, RECORD_PREFIX, AccountRegistry
from xian.fee_policy import TxFeePolicy
from xian.state_export import export_state, import_state, load_exported_state
from xian.state_root import compute_driver_state_root
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes
from xian.utils.tx import decode_and_validate_transaction_static_bytes
from xian.xian_abci import Xian


def pair():
    ed = SigningKey.generate()
    public, secret = hybrid.backend().generate_keypair()
    return hybrid.PublicKeys(bytes(ed.verify_key), public), (bytes(ed), secret)


def record(epoch=0):
    active, active_secret = pair()
    recovery, recovery_secret = pair()
    return hybrid.AccountRecord(epoch, active, recovery), active_secret, recovery_secret


def proof(keys, epoch, message, secret, role="transaction"):
    return hybrid.sign(
        keys, epoch, message, ed_seed=secret[0], pq_secret=secret[1], role=role
    ).hex()


def wire(tx):
    return encode_transaction_bytes(canonical_json_text(tx))


class TestAccountRegistry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cwd = os.getcwd()
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.tx_fee_policy = TxFeePolicy.free_metered()
        self.app.chain_id = "local-registry-test"
        self.old = SigningKey.generate()
        # Preserve case too: the previous validator accepts this exact identity.
        self.sender = bytes(self.old.verify_key).hex().upper()
        self.driver = self.app.client.raw_driver
        self.initial = {
            f"currency.balances:{self.sender}": ContractingDecimal(50000),
            f"con_token.balances:{self.sender}": 99,
            f"con_token.approvals:{self.sender}:spender": 15,
            "con_owned.owner": self.sender,
            f"__n.{self.sender}:": 3,
        }
        for key, value in self.initial.items():
            self.driver.set(key, value)
        self.driver.set(POLICY_KEY, {"version": 1, "enrollment_open": True, "transition_chi": 1000})
        self.driver.commit()
        self.app.state_root_cache.rebuild(self.driver.items().items())
        self.current, self.active, self.recovery = record()
        self.height = 1

    async def asyncTearDown(self):
        self.app.parallel_block_executor.close()
        teardown_fixtures()
        os.chdir(self.cwd)

    def payload(self, nonce=4):
        return {
            "sender": self.sender,
            "nonce": nonce,
            "chain_id": self.app.chain_id,
            "chi_supplied": 1000,
            "contract": "currency",
            "function": "transfer",
            "kwargs": {"to": "receiver", "amount": 1},
        }

    def legacy(self, nonce=4):
        payload = self.payload(nonce)
        return {
            "payload": payload,
            "metadata": {
                "signature": self.old.sign(canonical_json_text(payload).encode()).signature.hex()
            },
        }

    def hybrid_tx(self, nonce=5, current=None, secret=None):
        current, secret = current or self.current, secret or self.active
        payload = self.payload(nonce)
        return {
            "payload": payload,
            "metadata": {
                "tx_format": hybrid.TX_FORMAT,
                "auth": {
                    "scheme": hybrid.SCHEME,
                    "signature": proof(
                        current.active, current.epoch, canonical_json_text(payload).encode(), secret
                    ),
                },
            },
        }

    def transition(
        self,
        operation="enroll",
        nonce=4,
        candidate=None,
        active=None,
        recovery=None,
        current=None,
        authority=None,
    ):
        candidate = candidate or self.current
        active, recovery = active or self.active, recovery or self.recovery
        payload = self.payload(nonce)
        payload.update(
            contract="_accounts", function=operation, kwargs={"record": candidate.to_dict()}
        )
        message = canonical_json_text(payload).encode()
        if operation == "enroll":
            signature = self.old.sign(hybrid.enrollment_transcript(message)).signature.hex()
            scheme = hybrid.ENROLL_SCHEME
        else:
            current = current or self.current
            keys = current.recovery if operation == "recover" else current.active
            authority = authority or (self.recovery if operation == "recover" else self.active)
            signature = proof(keys, current.epoch, message, authority, "authority")
            scheme = hybrid.SCHEME
        return {
            "payload": payload,
            "metadata": {
                "tx_format": hybrid.TX_FORMAT,
                "auth": {
                    "scheme": scheme,
                    "signature": signature,
                    "active_proof": proof(
                        candidate.active, candidate.epoch, message, active, "new_active"
                    ),
                    "recovery_proof": proof(
                        candidate.recovery, candidate.epoch, message, recovery, "new_recovery"
                    ),
                },
            },
        }

    async def finalize(self, txs, commit=True):
        response = await self.app.finalize_block(
            RequestFinalizeBlock(
                height=self.height, txs=[wire(t) for t in txs], hash=bytes([self.height]) * 32
            )
        )
        if commit:
            await self.app.commit()
            self.height += 1
        return response

    async def enroll(self):
        response = await self.finalize([self.transition()])
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)

    async def test_enrollment_preserves_identity_balances_permissions_and_nonce(self):
        before = compute_driver_state_root(self.driver)
        tx = self.transition()
        self.assertEqual((await self.app.check_tx(wire(self.legacy()))).code, 0)
        # Avoid a mempool reservation collision: CheckTx is independent of block consensus.
        self.app.nonce_storage.pending_nonces.clear()
        self.assertEqual((await self.app.check_tx(wire(tx))).code, 0)
        self.assertIsNone(self.driver.get(RECORD_PREFIX + self.sender))
        proposal = await self.app.process_proposal(RequestProcessProposal(txs=[wire(tx)]))
        self.assertEqual(proposal.status, 1)
        response = await self.finalize([tx], commit=False)
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)
        self.assertNotEqual(response.app_hash, before)
        # Finalize stages the record; mempool queries still see committed state.
        self.assertIsNone(self.driver.get(RECORD_PREFIX + self.sender))
        await self.app.commit()
        self.height += 1
        for key, value in self.initial.items():
            self.assertEqual(self.driver.get(key), 4 if key.startswith("__n.") else value)
        self.assertEqual(self.driver.get(RECORD_PREFIX + self.sender), self.current.to_dict())
        self.assertEqual((await self.app.check_tx(wire(self.legacy(5)))).code, 1)
        self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx()))).code, 0)
        self.assertEqual(compute_driver_state_root(self.driver), response.app_hash)
        self.assertIsNone(self.driver.get(RECORD_PREFIX + self.sender.lower()))

    async def test_proposal_and_finalize_block_old_key_after_enrollment(self):
        txs = [self.transition(), self.legacy(5), self.hybrid_tx()]
        prepared = await self.app.prepare_proposal(
            RequestPrepareProposal(max_tx_bytes=1_000_000, txs=[wire(t) for t in txs])
        )
        self.assertEqual(list(prepared.txs), [wire(txs[0])])
        rejected = await self.app.process_proposal(
            RequestProcessProposal(txs=[wire(t) for t in txs[:2]])
        )
        self.assertEqual(rejected.status, 2)
        response = await self.finalize(txs)
        self.assertEqual([r.code for r in response.tx_results], [0, 1, 1])
        self.assertEqual(self.app.nonce_storage.get_nonce(self.sender), 4)

    async def test_recovery_rotation_and_replay(self):
        await self.enroll()
        next_record, active, recovery = record(1)
        recover = self.transition("recover", 5, next_record, active, recovery)
        response = await self.finalize([recover])
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)
        self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx(6)))).code, 1)
        self.assertEqual(
            (await self.app.check_tx(wire(self.hybrid_tx(6, next_record, active)))).code, 0
        )
        self.assertEqual((await self.app.check_tx(wire(recover))).code, 1)
        third, a3, r3 = record(2)
        rotate = self.transition("rotate", 6, third, a3, r3, next_record, active)
        response = await self.finalize([rotate])
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)
        self.assertEqual(self.driver.get(RECORD_PREFIX + self.sender), third.to_dict())
        self.assertEqual(self.driver.get(f"currency.balances:{self.sender}"), 50000)

    async def test_failed_proofs_never_modify_state_or_reserve_nonce(self):
        good = self.transition()
        mutations = [
            lambda t: t["payload"].update(nonce=5),
            lambda t: t["payload"].update(chain_id="another-chain"),
            lambda t: t["payload"].update(sender=self.sender.lower()),
            lambda t: t["payload"].update(chi_supplied=1001),
            lambda t: t["metadata"]["auth"].update(
                recovery_proof=t["metadata"]["auth"]["active_proof"]
            ),
            lambda t: t["metadata"]["auth"].update(active_proof="00" * hybrid.AUTH_BYTES),
            lambda t: t["metadata"]["auth"].update(signature="00" * 64),
            lambda t: t["metadata"].update(signature="00" * 64),
            lambda t: t["metadata"]["auth"].update(public_key=bytes(self.old.verify_key).hex()),
            lambda t: t["payload"]["kwargs"]["record"].update(epoch=True),
            lambda t: t["payload"]["kwargs"]["record"].update(version=True),
        ]
        before = self.driver.items()
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(good)
                mutate(bad)
                self.assertEqual((await self.app.check_tx(wire(bad))).code, 1)
                self.assertEqual(self.app.nonce_storage.get_next_nonce(self.sender), 4)
                self.assertEqual(self.driver.items(), before)
        self.assertEqual((await self.app.check_tx(wire(good))).code, 0)

    async def test_disabled_and_closed_enrollment_preserve_legacy_access(self):
        for policy in [None, {"version": 1, "enrollment_open": False, "transition_chi": 1000}]:
            self.driver.set(POLICY_KEY, policy)
            self.driver.commit()
            self.assertEqual((await self.app.check_tx(wire(self.transition()))).code, 1)
            self.assertEqual((await self.app.check_tx(wire(self.legacy()))).code, 0)
        self.assertIsNone(self.driver.get(RECORD_PREFIX + self.sender))

    async def test_invalid_admission_burst_preserves_funds_and_nonce(self):
        good = self.transition()
        stale_nonce = self.transition(nonce=100)
        bad_proof = copy.deepcopy(good)
        bad_proof["metadata"]["auth"]["signature"] = "00" * 64
        corpus = [wire(stale_nonce), wire(bad_proof), b"not-hex", b"5b" * 2000]
        before = self.driver.items()
        for index in range(128):
            self.assertEqual((await self.app.check_tx(corpus[index % len(corpus)])).code, 1)
        self.assertEqual(self.driver.items(), before)
        self.assertEqual(self.app.nonce_storage.get_next_nonce(self.sender), 4)
        self.assertEqual((await self.app.check_tx(wire(good))).code, 0)
        response = await self.finalize([good])
        self.assertEqual(response.tx_results[0].code, 0)
        self.assertEqual(self.driver.get(f"currency.balances:{self.sender}"), 50000)
        self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx()))).code, 0)

    async def test_missing_policy_or_corrupt_record_cannot_restore_legacy_authority(self):
        await self.enroll()
        for value in [self.current.to_dict(), {}, "invalid"]:
            self.driver.set(RECORD_PREFIX + self.sender, value)
            self.driver.set(POLICY_KEY, None)
            self.driver.commit()
            self.assertEqual((await self.app.check_tx(wire(self.legacy(5)))).code, 1)
            self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx()))).code, 1)

    async def test_native_installation_cannot_change_registry_verdicts(self):
        registry = AccountRegistry(self.driver, self.app.chain_id)
        for tx in [self.transition(), self.legacy()]:
            with patch(
                "xian.utils.tx._native_decode_and_validate_transaction_static",
                side_effect=AssertionError("stateless native path must not be used"),
            ):
                decoded = decode_and_validate_transaction_static_bytes(
                    wire(tx), chain_id=self.app.chain_id, account_registry=registry
                )
                self.assertEqual(decoded, tx)

    async def test_export_import_and_restart_preserve_registry_and_funds(self):
        await self.enroll()
        with tempfile.TemporaryDirectory() as directory:
            path = export_state(output_dir=Path(directory), storage_home=MockConstants.STORAGE_HOME)
            exported = load_exported_state(path)
            with tempfile.TemporaryDirectory() as restored:
                import_state(exported_state=exported, storage_home=Path(restored))
                from contracting.storage.driver import Driver

                driver = Driver(storage_home=Path(restored))
                self.assertEqual(driver.get(RECORD_PREFIX + self.sender), self.current.to_dict())
                self.assertEqual(driver.get(f"currency.balances:{self.sender}"), 50000)
                self.assertEqual(driver.get(f"__n.{self.sender}:"), 4)
                AccountRegistry(driver, self.app.chain_id).authorize(self.hybrid_tx())
                with self.assertRaises(Exception):
                    AccountRegistry(driver, self.app.chain_id).authorize(self.legacy(5))
        # Construct a new node against the committed database.
        self.app.parallel_block_executor.close()
        self.app = await Xian.create(constants=MockConstants)
        self.app.chain_id = "local-registry-test"
        self.app.tx_fee_policy = TxFeePolicy.free_metered()
        self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx()))).code, 0)

    async def test_insufficient_execution_funds_preserve_authorization(self):
        self.app.tx_fee_policy = TxFeePolicy.paid_metered()
        self.driver.set(f"currency.balances:{self.sender}", 0)
        self.driver.commit()
        response = await self.finalize([self.transition()])
        self.assertEqual(response.tx_results[0].code, 1)
        self.assertIsNone(self.driver.get(RECORD_PREFIX + self.sender))
        self.assertEqual(self.app.nonce_storage.get_nonce(self.sender), 4)

    async def test_wire_limit_before_decode(self):
        self.app.max_tx_bytes = hybrid.MAX_WIRE_BYTES
        with patch(
            "xian.utils.tx.decode_transaction_bytes",
            side_effect=AssertionError("must reject first"),
        ):
            response = await self.app.check_tx(b"a" * (hybrid.MAX_WIRE_BYTES + 1))
        self.assertEqual(response.code, 1)
        self.assertIn("maximum configured size", response.log)

    async def test_registry_writes_roll_back_if_processor_write_fails(self):
        self.app.tx_fee_policy = TxFeePolicy.paid_metered()
        original_set = self.driver.set
        before = self.driver.get(f"currency.balances:{self.sender}")

        def failing_set(key, value, *args, **kwargs):
            if key == f"currency.balances:{self.sender}":
                raise OSError("injected fee write failure after registry write")
            return original_set(key, value, *args, **kwargs)

        with patch.object(self.driver, "set", side_effect=failing_set):
            response = await self.finalize([self.transition()])
        self.assertEqual(response.tx_results[0].code, 1)
        self.assertIsNone(self.driver.get(RECORD_PREFIX + self.sender))
        self.assertEqual(self.driver.get(f"currency.balances:{self.sender}"), before)
        self.assertEqual(self.app.nonce_storage.get_nonce(self.sender), 3)

    async def test_commit_failure_is_retryable_without_partial_enrollment(self):
        tx = self.transition()
        response = await self.finalize([tx], commit=False)
        self.assertEqual(response.tx_results[0].code, 0)
        with patch.object(
            self.driver._store, "batch_set", side_effect=OSError("injected commit failure")
        ):
            with self.assertRaises(OSError):
                await self.app.commit()
        self.assertIsNone(self.driver._store.get(RECORD_PREFIX + self.sender))
        self.assertEqual(self.driver._store.get(f"__n.{self.sender}:"), 3)
        await self.app.commit()
        self.assertEqual(
            self.driver._store.get(RECORD_PREFIX + self.sender), self.current.to_dict()
        )
        self.assertEqual(self.driver._store.get(f"__n.{self.sender}:"), 4)

    async def test_hybrid_spending_keeps_contract_signer_and_existing_token_balance(self):
        # A source-backed VM token using the existing account identity as its key.
        source = """balances = Hash(default_value=0)
last_signer = Variable()
@export
def transfer(to: str, amount: int):
    assert balances[ctx.caller] >= amount
    balances[ctx.caller] -= amount
    balances[to] += amount
    last_signer.set(ctx.signer)
"""
        self.driver.set_contract_from_source(name="con_migration_token", source=source)
        self.driver.set(f"con_migration_token.balances:{self.sender}", 99)
        self.driver.commit()
        self.app.state_root_cache.rebuild(self.driver.items().items())
        await self.enroll()
        tx = self.hybrid_tx()
        tx["payload"]["contract"] = "con_migration_token"
        tx["payload"]["chi_supplied"] = 100000
        tx["metadata"]["auth"]["signature"] = proof(
            self.current.active, 0, canonical_json_text(tx["payload"]).encode(), self.active
        )
        response = await self.finalize([tx])
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)
        self.assertEqual(self.driver.get(f"con_migration_token.balances:{self.sender}"), 98)
        self.assertEqual(self.driver.get("con_migration_token.balances:receiver"), 1)
        self.assertEqual(self.driver.get("con_migration_token.last_signer"), self.sender)

    async def test_paid_enrollment_deducts_only_the_configured_fee(self):
        self.app.tx_fee_policy = TxFeePolicy.paid_metered()
        self.driver.set("chi_cost.S:value", 20)
        self.driver.commit()
        # Isolate fee accounting from the chain's separately configured reward distribution.
        self.app.rewards_handler = None
        await self.enroll()
        self.assertEqual(self.driver.get(f"currency.balances:{self.sender}"), 49950)
        self.assertEqual(self.driver.get("con_owned.owner"), self.sender)

    async def test_closed_enrollment_does_not_disable_spending_or_recovery(self):
        await self.enroll()
        self.driver.set(
            POLICY_KEY, {"version": 1, "enrollment_open": False, "transition_chi": 1000}
        )
        self.driver.commit()
        self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx()))).code, 0)
        candidate, a, r = record(1)
        tx = self.transition("recover", 5, candidate, a, r)
        response = await self.finalize([tx])
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)

    async def test_recovery_commit_failure_restart_and_stale_backups(self):
        await self.enroll()
        candidate, active, recovery = record(1)
        tx = self.transition("recover", 5, candidate, active, recovery)
        response = await self.finalize([tx], commit=False)
        self.assertEqual(response.tx_results[0].code, 0)
        with patch.object(self.driver._store, "batch_set", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.app.commit()
        # Durable state keeps the entire prior authorization and nonce on failure.
        self.assertEqual(
            self.driver._store.get(RECORD_PREFIX + self.sender), self.current.to_dict()
        )
        self.assertEqual(self.driver._store.get(f"__n.{self.sender}:"), 4)
        await self.app.commit()
        self.app.parallel_block_executor.close()
        self.app = await Xian.create(constants=MockConstants)
        self.app.chain_id = "local-registry-test"
        self.app.tx_fee_policy = TxFeePolicy.free_metered()
        self.driver = self.app.client.raw_driver
        self.height += 1
        self.assertEqual(self.driver.get(RECORD_PREFIX + self.sender), candidate.to_dict())
        self.assertEqual(
            (await self.app.check_tx(wire(self.hybrid_tx(6, candidate, active)))).code, 0
        )
        third, a3, r3 = record(2)
        for stale in [
            self.legacy(6),
            self.hybrid_tx(6),
            self.transition("recover", 6, third, a3, r3),
            self.transition("rotate", 6, third, a3, r3),
        ]:
            self.assertEqual((await self.app.check_tx(wire(stale))).code, 1)
        # The current recovery backup remains usable after restart and enrollment closure.
        self.driver.set(
            POLICY_KEY, {"version": 1, "enrollment_open": False, "transition_chi": 1000}
        )
        self.driver.commit()
        response = await self.finalize(
            [self.transition("recover", 6, third, a3, r3, candidate, recovery)]
        )
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)
        self.assertEqual(self.driver.get(RECORD_PREFIX + self.sender), third.to_dict())
        for key, value in self.initial.items():
            if not key.startswith("__n."):
                self.assertEqual(self.driver.get(key), value)

    async def test_failed_recovery_fee_keeps_current_keys_and_can_retry_with_next_nonce(self):
        await self.enroll()
        self.app.tx_fee_policy = TxFeePolicy.paid_metered()
        self.app.rewards_handler = None
        self.driver.set("chi_cost.S:value", 20)
        self.driver.set(f"currency.balances:{self.sender}", 0)
        self.driver.commit()
        candidate, active, recovery = record(1)
        response = await self.finalize([self.transition("recover", 5, candidate, active, recovery)])
        self.assertEqual(response.tx_results[0].code, 1)
        self.assertEqual(self.driver.get(RECORD_PREFIX + self.sender), self.current.to_dict())
        self.assertEqual(self.app.nonce_storage.get_nonce(self.sender), 5)
        # Funding the same identity is enough; no old key or transfer to a new address is required.
        self.driver.set(f"currency.balances:{self.sender}", 100)
        self.driver.commit()
        self.assertEqual((await self.app.check_tx(wire(self.hybrid_tx(6)))).code, 0)
        response = await self.finalize([self.transition("recover", 6, candidate, active, recovery)])
        self.assertEqual(response.tx_results[0].code, 0, response.tx_results[0].data)
        self.assertEqual(self.driver.get(RECORD_PREFIX + self.sender), candidate.to_dict())
        self.assertEqual(self.driver.get(f"currency.balances:{self.sender}"), 50)
        self.assertEqual(self.driver.get(f"con_token.balances:{self.sender}"), 99)
        self.assertEqual(self.driver.get("con_owned.owner"), self.sender)

    async def test_registry_absence_has_explicit_wire_marker(self):
        from cometbft.abci.v1beta1.types_pb2 import RequestQuery

        response = await self.app.query(RequestQuery(path="/get/" + RECORD_PREFIX + self.sender))
        self.assertEqual(response.code, 0)
        self.assertEqual(response.value, b"\x00")
        await self.enroll()
        response = await self.app.query(RequestQuery(path="/get/" + RECORD_PREFIX + self.sender))
        self.assertEqual(response.code, 0)
        self.assertEqual(json.loads(response.value), self.current.to_dict())
