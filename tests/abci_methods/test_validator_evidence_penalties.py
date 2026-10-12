import hashlib
import unittest
from io import BytesIO

from fixtures.mock_constants import MockConstants
from google.protobuf.timestamp_pb2 import Timestamp
from utils import setup_fixtures, teardown_fixtures

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit, Validator
from cometbft.abci.v1beta2.types_pb2 import Misbehavior, MisbehaviorType
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    Response,
)
from xian.config_paths import resolve_contracts_dir
from xian.xian_abci import Xian

NODE_1 = "11" * 32
NODE_2 = "22" * 32
NODE_3 = "33" * 32


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


def validator_address(validator_key: str) -> bytes:
    return hashlib.sha256(bytes.fromhex(validator_key)).digest()[:20]


class ValidatorEvidencePenaltyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.handler = ProtocolHandler(self.app)
        self.driver = self.app.client.raw_driver
        contracts_dir = resolve_contracts_dir()

        contract_args = {
            "currency": {"vk": "xian-deployer"},
            "chi_cost": {"initial_rate": 20},
            "rewards": None,
            "dao": None,
        }
        for contract_file in [
            "currency.s.py",
            "dao.s.py",
            "rewards.s.py",
            "chi_cost.s.py",
        ]:
            path = contracts_dir / contract_file
            code = path.read_text(encoding="utf-8")
            name = contract_file.split(".")[0]
            self.app.client.submit(
                code,
                name=name,
                constructor_args=contract_args[name],
            )

        members_code = (contracts_dir / "validators.s.py").read_text(encoding="utf-8")
        self.app.client.submit(
            members_code,
            name="validators",
            constructor_args={
                "genesis_nodes": [NODE_1, NODE_2, NODE_3],
                "genesis_registration_fee": 1000,
            },
        )
        self.membership = self.app.client.get_contract_proxy("validators")

    async def asyncTearDown(self):
        teardown_fixtures()

    def create_finalize_block_request(
        self,
        height: int,
        *,
        misbehavior=None,
    ) -> Request:
        timestamp = Timestamp()
        timestamp.seconds = height
        return Request(
            finalize_block=RequestFinalizeBlock(
                height=height,
                time=timestamp,
                txs=[],
                misbehavior=misbehavior or [],
                hash=f"block-{height}".encode("utf-8"),
            )
        )

    async def finalize_block(self, height: int, *, misbehavior=None):
        raw = await self.handler.process(
            "finalize_block",
            self.create_finalize_block_request(height, misbehavior=misbehavior),
        )
        response = await deserialize(raw)
        await self.handler.process("commit", Request(commit=RequestCommit()))
        return response

    def duplicate_vote_evidence(self, validator_key: str, *, height: int):
        evidence_time = Timestamp()
        evidence_time.seconds = height
        return Misbehavior(
            type=MisbehaviorType.DUPLICATE_VOTE,
            validator=Validator(
                address=validator_address(validator_key),
                power=10,
            ),
            height=height,
            time=evidence_time,
            total_voting_power=30,
        )

    async def test_finalize_block_applies_duplicate_vote_evidence_without_user_tx(
        self,
    ):
        self.driver.set(f"validators.self_bond:{NODE_1}", 200)
        self.driver.set("currency.balances:validators", 200)
        dao_balance_before = self.driver.get("currency.balances:dao") or 0

        before = await self.finalize_block(1)
        after = await self.finalize_block(
            2,
            misbehavior=[self.duplicate_vote_evidence(NODE_1, height=2)],
        )

        self.assertEqual(self.driver.get(f"validators.total_slashed:{NODE_1}"), 10)
        self.assertEqual(self.driver.get(f"validators.self_bond:{NODE_1}"), 190)
        self.assertTrue(self.driver.get(f"validators.jailed:{NODE_1}"))
        self.assertEqual(self.driver.get(f"validators.statuses:{NODE_1}"), "approved")
        self.assertNotIn(NODE_1, self.driver.get("validators.active_validators"))
        self.assertEqual(
            self.driver.get("currency.balances:dao"),
            dao_balance_before + 10,
        )
        self.assertNotEqual(
            before.finalize_block.app_hash,
            after.finalize_block.app_hash,
        )

    async def test_finalize_block_evidence_rebalances_auto_validator_set(self):
        self.driver.set("validators.active_validators", [NODE_1, NODE_2])
        self.driver.set("validators.candidates", [NODE_3])
        self.driver.set(f"validators.statuses:{NODE_1}", "active")
        self.driver.set(f"validators.statuses:{NODE_2}", "active")
        self.driver.set(f"validators.statuses:{NODE_3}", "approved")
        self.driver.set(f"validators.pending_registrations:{NODE_3}", False)
        self.driver.set(f"validators.pending_leave:{NODE_1}", False)
        self.driver.set(f"validators.pending_leave:{NODE_2}", False)
        self.driver.set(f"validators.pending_leave:{NODE_3}", False)
        self.driver.set(f"validators.self_bond:{NODE_1}", 200)
        self.driver.set(f"validators.self_bond:{NODE_2}", 150)
        self.driver.set(f"validators.self_bond:{NODE_3}", 120)
        self.driver.set(f"validators.total_delegated:{NODE_1}", 0)
        self.driver.set(f"validators.total_delegated:{NODE_2}", 0)
        self.driver.set(f"validators.total_delegated:{NODE_3}", 0)
        self.driver.set(f"validators.delegator_lists:{NODE_1}", [])
        self.driver.set(f"validators.delegator_lists:{NODE_2}", [])
        self.driver.set(f"validators.delegator_lists:{NODE_3}", [])
        self.driver.set(f"validators.requested_power:{NODE_1}", 10)
        self.driver.set(f"validators.requested_power:{NODE_2}", 10)
        self.driver.set(f"validators.requested_power:{NODE_3}", 10)
        self.driver.set(f"validators.powers:{NODE_1}", 10)
        self.driver.set(f"validators.powers:{NODE_2}", 10)
        self.driver.set(f"validators.powers:{NODE_3}", 0)
        self.driver.set(f"validators.reward_keys:{NODE_1}", NODE_1)
        self.driver.set(f"validators.reward_keys:{NODE_2}", NODE_2)
        self.driver.set(f"validators.reward_keys:{NODE_3}", NODE_3)
        self.driver.set(f"validators.eligible_at_epoch:{NODE_3}", 0)
        self.driver.set("validators.config:selection_mode", "auto_top_n")
        self.driver.set("validators.config:max_validators", 2)
        self.driver.set("validators.config:power_mode", "requested")
        self.driver.set("validators.config:rebalance_interval", 1)
        self.driver.set("validators.config:min_self_bond", 100)
        self.driver.set("validators.config:min_total_bond", 100)
        self.driver.set("currency.balances:validators", 470)

        await self.finalize_block(
            1,
            misbehavior=[self.duplicate_vote_evidence(NODE_1, height=1)],
        )

        self.assertTrue(self.driver.get(f"validators.jailed:{NODE_1}"))
        self.assertEqual(self.driver.get(f"validators.self_bond:{NODE_1}"), 190)
        self.assertEqual(self.driver.get("validators.active_validators"), [NODE_2, NODE_3])
        self.assertEqual(self.driver.get(f"validators.statuses:{NODE_3}"), "active")

    async def test_finalize_block_does_not_double_apply_duplicate_evidence(self):
        self.driver.set(f"validators.self_bond:{NODE_1}", 200)
        self.driver.set("currency.balances:validators", 200)
        evidence = self.duplicate_vote_evidence(NODE_1, height=7)

        await self.finalize_block(7, misbehavior=[evidence])
        await self.finalize_block(8, misbehavior=[evidence])

        self.assertEqual(self.driver.get(f"validators.total_slashed:{NODE_1}"), 10)
        self.assertEqual(self.driver.get(f"validators.self_bond:{NODE_1}"), 190)

    async def test_finalize_block_slashes_pending_unbond_for_exited_validator(self):
        self.driver.set("validators.active_validators", [NODE_2, NODE_3])
        self.driver.set("validators.candidates", [])
        self.driver.set(f"validators.statuses:{NODE_1}", "left")
        self.driver.set(f"validators.self_bond:{NODE_1}", 0)
        self.driver.set(f"validators.total_delegated:{NODE_1}", 0)
        self.driver.set("validators.pending_unbond_counter", 1)
        self.driver.set(f"validators.pending_unbond_owner_ids:{NODE_1}", [1])
        self.driver.set(f"validators.pending_unbond_validator_ids:{NODE_1}", [1])
        self.driver.set(
            "validators.pending_unbonds:1",
            {
                "id": 1,
                "owner": NODE_1,
                "validator": NODE_1,
                "amount": 40,
                "kind": "self_bond",
                "created_block": 5,
                "created_at": 5,
                "unlock_at": 15,
                "claimed": False,
            },
        )
        self.driver.set("currency.balances:validators", 40)
        dao_balance_before = self.driver.get("currency.balances:dao") or 0

        before = await self.finalize_block(9)
        after = await self.finalize_block(
            10,
            misbehavior=[self.duplicate_vote_evidence(NODE_1, height=4)],
        )

        self.assertEqual(self.driver.get(f"validators.total_slashed:{NODE_1}"), 2)
        self.assertEqual(
            self.driver.get("validators.pending_unbonds:1")["amount"],
            38,
        )
        self.assertEqual(self.driver.get(f"validators.statuses:{NODE_1}"), "left")
        self.assertFalse(self.driver.get(f"validators.jailed:{NODE_1}"))
        self.assertEqual(
            self.driver.get("currency.balances:dao"),
            dao_balance_before + 2,
        )
        self.assertNotEqual(
            before.finalize_block.app_hash,
            after.finalize_block.app_hash,
        )
