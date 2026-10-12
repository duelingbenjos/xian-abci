import os
import unittest

from contracting.local import ContractingClient
from xian_runtime_types.time import Datetime

from xian.config_paths import resolve_contracts_dir


class TestMembersContract(unittest.TestCase):
    def setUp(self):
        # Bootstrap the environment
        self.chain_id = "test-chain"
        self.environment = {
            "chain_id": self.chain_id,
            "block_num": 0,
        }
        self.deployer_vk = "xian-deployer"

        self.client = ContractingClient(environment=self.environment)
        self.client.flush()

        # Set up paths and load contracts
        self.contracts_dir = str(resolve_contracts_dir())

        # Deploy required contracts first with correct constructor args
        contract_args = {
            "currency": {"vk": self.deployer_vk},
            "chi_cost": {"initial_rate": 20},
            "rewards": None,
            "dao": None,
        }

        for contract in ["currency.s.py", "dao.s.py", "rewards.s.py", "chi_cost.s.py"]:
            path = os.path.join(self.contracts_dir, contract)
            with open(path) as f:
                code = f.read()
                name = contract.split(".")[0]
                self.client.submit(code, name=name, constructor_args=contract_args[name])

        # Deploy members contract
        members_path = os.path.join(self.contracts_dir, "validators.s.py")
        with open(members_path) as f:
            code = f.read()
            self.client.submit(
                code,
                name="validators",
                constructor_args={
                    "genesis_nodes": ["node1", "node2", "node3"],
                    "genesis_registration_fee": 1000,
                },
            )

        self.members = self.client.get_contract_proxy("validators")
        self.currency = self.client.get_contract_proxy("currency")

        # Add initial balance to deployer directly
        self.currency.balances[self.deployer_vk] = 100000

    def approve_policy_update(self, environment=None, **updates):
        if environment is None:
            environment = {"block_num": 0}
        self.members.propose_vote(
            type_of_vote="update_policy",
            arg=updates,
            signer="node1",
            environment=environment,
        )
        proposal_id = self.members.total_votes.get()
        self.members.vote(
            proposal_id=proposal_id,
            vote="yes",
            signer="node2",
            environment=environment,
        )
        self.members.vote(
            proposal_id=proposal_id,
            vote="yes",
            signer="node3",
            environment=environment,
        )
        return self.members.get_policy_config(signer="node1")

    def assert_vote_payload_rejected(self, type_of_vote, arg):
        proposal_count = self.members.total_votes.get()
        with self.assertRaises(AssertionError):
            self.members.propose_vote(
                type_of_vote=type_of_vote,
                arg=arg,
                signer="node1",
            )
        self.assertEqual(self.members.total_votes.get(), proposal_count)

    def test_initial_setup(self):
        # GIVEN the initial setup from constructor
        # WHEN checking initial values
        nodes = self.members.active_validators.get()
        fee = self.members.registration_fee.get()
        types = self.members.types.get()

        # THEN values should match constructor args
        self.assertEqual(len(nodes), 3)
        self.assertEqual(fee, 1000)
        self.assertTrue("add_member" in types)
        self.assertTrue("remove_member" in types)
        self.assertNotIn("create_stream", types)
        self.assertNotIn("change_close_time", types)
        self.assertNotIn("finalize_stream", types)
        self.assertNotIn("close_balance_finalize", types)

    def test_register_new_member(self):
        # GIVEN sufficient funds for registration
        self.currency.approve(amount=1000, to="validators", signer="new_member")
        self.currency.transfer(amount=1000, to="new_member", signer=self.deployer_vk)

        # WHEN registering
        self.members.register(signer="new_member")

        # THEN registration should be pending
        validator = self.members.get_validator(account="new_member", signer="new_member")
        self.assertTrue(self.members.pending_registrations["new_member"])
        self.assertEqual(self.members.holdings["new_member"], 1000)
        self.assertEqual(self.members.statuses["new_member"], "pending")
        self.assertEqual(self.members.reward_keys["new_member"], "new_member")
        self.assertEqual(self.members.requested_power["new_member"], 10)
        self.assertEqual(validator["commission_bps"], 0)

    def test_propose_and_approve_new_member(self):
        # GIVEN a pending registration
        self.currency.approve(amount=1000, to="validators", signer="new_member")
        self.currency.transfer(amount=1000, to="new_member", signer=self.deployer_vk)
        self.members.register(
            signer="new_member",
            requested_validator_power=25,
            reward_key="reward-wallet",
            moniker="new-node",
        )

        # WHEN proposing and voting
        self.members.propose_vote(type_of_vote="add_member", arg="new_member", signer="node1")

        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        # THEN member should be added
        nodes = self.members.active_validators.get()
        self.assertTrue("new_member" in nodes)
        self.assertEqual(self.members.powers["new_member"], 25)
        self.assertEqual(self.members.reward_keys["new_member"], "reward-wallet")
        self.assertEqual(self.members.statuses["new_member"], "active")
        self.assertFalse(self.members.pending_registrations["new_member"])

    def test_vote_records_snapshot_and_events(self):
        self.members.propose_vote(
            type_of_vote="topic_vote",
            arg={"topic": "governance-ui-read-model"},
            signer="node1",
        )

        self.assertEqual(
            self.members.get_vote_voter_snapshot(proposal_id=1),
            ["node1", "node2", "node3"],
        )
        self.assertEqual(
            self.members.get_vote_record(proposal_id=1, voter="node1"),
            {
                "proposal_id": 1,
                "voter": "node1",
                "vote": "yes",
                "weight": 10,
            },
        )

        self.members.vote(proposal_id=1, vote="no", signer="node2")

        self.assertEqual(self.members.votes[1]["status"], "rejected")
        self.assertEqual(
            self.members.get_vote_records(proposal_id=1),
            [
                {
                    "proposal_id": 1,
                    "voter": "node1",
                    "vote": "yes",
                    "weight": 10,
                },
                {
                    "proposal_id": 1,
                    "voter": "node2",
                    "vote": "no",
                    "weight": 10,
                },
                {
                    "proposal_id": 1,
                    "voter": "node3",
                    "vote": None,
                    "weight": 10,
                },
            ],
        )

    def test_vote_payloads_are_validated_before_proposal_creation(self):
        invalid_payloads = [
            ("topic_vote", "free-form-string"),
            ("topic_vote", {"topic": ""}),
            ("topic_vote", {"topic": "ok", "unexpected": True}),
            ("change_registration_fee", 0),
            ("change_registration_fee", -1),
            ("change_registration_fee", True),
            ("reward_change", [0.5, 0.5]),
            ("reward_change", [0.5, 0.6, -0.1, 0]),
            ("dao_payout", {"amount": 10, "to": "recipient"}),
            (
                "dao_payout",
                {"contract_name": "currency", "amount": -1, "to": "recipient"},
            ),
            (
                "dao_payout",
                {"contract_name": "missing", "amount": 1, "to": "recipient"},
            ),
            ("chi_cost_change", 0),
            ("change_types", []),
            ("change_types", ["topic_vote", "topic_vote"]),
            ("update_policy", {}),
            ("update_policy", {"unknown_policy": 1}),
            ("update_policy", {"max_validators": True}),
            ("update_policy", {"duplicate_vote_jail": "yes"}),
            ("jail_member", {"reason": "missing member"}),
            ("slash_member", {"member": "node1", "slash_bps": 1.5}),
            ("set_member_power", {"member": "node1", "power": 1.5}),
        ]

        for type_of_vote, arg in invalid_payloads:
            with self.subTest(type_of_vote=type_of_vote, arg=arg):
                self.assert_vote_payload_rejected(type_of_vote, arg)

    def test_reward_change_allows_zero_value_buckets(self):
        requested_split = [0.70, 0, 0, 0.30]
        rewards = self.client.get_contract_proxy("rewards")

        self.members.propose_vote(
            type_of_vote="reward_change",
            arg=requested_split,
            signer="node1",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.assertEqual(rewards.current_value(), requested_split)

    def test_remove_member_rejects_inactive_approved_validator(self):
        self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=2,
        )

        self.assertEqual(self.members.statuses["node3"], "approved")
        self.assertNotIn("node3", self.members.active_validators.get())
        with self.assertRaisesRegex(AssertionError, "Active only"):
            self.members.propose_vote(
                type_of_vote="remove_member",
                arg="node3",
                signer="node1",
            )

    def test_change_types_cannot_remove_recovery_vote_types(self):
        recovery_types = self.members.get_recovery_vote_types(signer="node1")
        self.assertEqual(
            recovery_types,
            [
                "add_member",
                "remove_member",
                "jail_member",
                "unjail_member",
                "slash_member",
                "set_member_power",
                "change_registration_fee",
                "chi_cost_change",
                "change_types",
                "update_policy",
            ],
        )

        for recovery_type in recovery_types:
            with self.subTest(recovery_type=recovery_type):
                requested_types = [
                    vote_type
                    for vote_type in self.members.types.get()
                    if vote_type != recovery_type
                ]
                proposal_count = self.members.total_votes.get()
                with self.assertRaisesRegex(AssertionError, recovery_type):
                    self.members.propose_vote(
                        type_of_vote="change_types",
                        arg=requested_types,
                        signer="node1",
                    )
                self.assertEqual(self.members.total_votes.get(), proposal_count)

    def test_change_types_can_replace_non_recovery_vote_types(self):
        requested_types = self.members.get_recovery_vote_types(signer="node1") + ["topic_vote"]

        self.members.propose_vote(
            type_of_vote="change_types",
            arg=requested_types,
            signer="node1",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.assertEqual(self.members.types.get(), requested_types)
        self.members.propose_vote(
            type_of_vote="topic_vote",
            arg={"topic": "known optional vote types remain configurable"},
            signer="node1",
        )
        self.assertEqual(self.members.votes[2]["type"], "topic_vote")

    def test_positive_registration_fee_is_a_valid_governed_value(self):
        self.members.propose_vote(
            type_of_vote="change_registration_fee",
            arg=2000,
            signer="node1",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.assertEqual(self.members.registration_fee.get(), 2000)

    def test_announce_and_leave(self):
        # GIVEN a node announcing leave
        current_time = Datetime(year=2024, month=1, day=1)
        self.members.announce_leave(
            signer="node1",
            environment={"now": current_time, "block_num": 0},
        )

        # WHEN time passes (7 days + 1 hour to be safe)
        future_time = Datetime(year=2024, month=1, day=8, hour=1)

        self.members.leave(
            signer="node1",
            environment={"now": future_time, "block_num": 1},
        )

        # THEN they should be removed from nodes
        nodes = self.members.active_validators.get()
        self.assertTrue("node1" not in nodes)

    def test_vote_expiry(self):
        # GIVEN a pending vote
        self.currency.approve(amount=1000, to="validators", signer="new_member")
        self.currency.transfer(amount=1000, to="new_member", signer=self.deployer_vk)
        self.members.register(signer="new_member")

        self.members.propose_vote(
            type_of_vote="add_member",
            arg="new_member",
            signer="node1",
            environment={"now": Datetime(year=2024, month=1, day=1)},
        )

        # WHEN trying to vote after expiry
        future_time = Datetime(year=2024, month=1, day=8)

        # THEN vote should fail
        with self.assertRaises(AssertionError):
            self.members.vote(
                proposal_id=1, vote="yes", signer="node2", environment={"now": future_time}
            )

    def test_set_member_power_vote_updates_validator_power(self):
        self.members.propose_vote(
            type_of_vote="set_member_power",
            arg={"member": "node2", "power": 42},
            signer="node1",
        )

        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.assertEqual(self.members.powers["node2"], 42)
        self.assertEqual(self.members.member_weight(account="node2"), 42)

    def test_update_policy_vote_configures_auto_top_n_selection(self):
        policy = self.approve_policy_update(
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            min_self_bond=100,
            min_total_bond=100,
        )

        self.assertEqual(policy["selection_mode"], "auto_top_n")
        self.assertEqual(policy["max_validators"], 2)
        self.assertEqual(policy["power_mode"], "requested")
        self.assertEqual(policy["min_self_bond"], 100)
        self.assertEqual(policy["min_total_bond"], 100)
        self.assertEqual(self.members.active_validators.get(), [])

    def test_rebalance_auto_top_n_selects_highest_total_bonded_validators(self):
        self.approve_policy_update(
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            min_self_bond=100,
            min_total_bond=100,
        )

        for validator, amount in {
            "node1": 500,
            "node2": 500,
            "node3": 500,
            "delegator1": 500,
        }.items():
            self.currency.transfer(
                amount=amount,
                to=validator,
                signer=self.deployer_vk,
            )
            self.currency.approve(amount=amount, to="validators", signer=validator)

        self.members.update_registration(
            requested_validator_power=30,
            signer="node1",
        )
        self.members.update_registration(
            requested_validator_power=20,
            signer="node2",
        )
        self.members.update_registration(
            requested_validator_power=10,
            signer="node3",
        )

        self.members.bond_self(amount=300, signer="node1")
        self.members.bond_self(amount=200, signer="node2")
        self.members.bond_self(amount=100, signer="node3")

        rebalance = self.members.rebalance(
            signer="delegator1",
            environment={"block_num": 1},
        )

        self.assertEqual(rebalance["selected"], ["node1", "node2"])
        self.assertEqual(self.members.active_validators.get(), ["node1", "node2"])
        self.assertEqual(self.members.powers["node1"], 30)
        self.assertEqual(self.members.powers["node2"], 20)
        self.assertEqual(self.members.powers["node3"], 0)
        self.assertEqual(self.members.statuses["node3"], "approved")

        self.members.delegate(
            validator="node3",
            amount=250,
            signer="delegator1",
        )
        rebalance = self.members.rebalance(
            signer="delegator1",
            environment={"block_num": 2},
        )

        self.assertEqual(rebalance["selected"], ["node3", "node1"])
        self.assertEqual(self.members.active_validators.get(), ["node3", "node1"])
        self.assertEqual(self.members.statuses["node2"], "approved")
        self.assertEqual(self.members.powers["node3"], 10)
        self.assertEqual(self.members.powers["node2"], 0)

    def test_rebalance_excludes_validator_with_pending_leave(self):
        current_time = Datetime(year=2024, month=1, day=1, hour=12)
        self.approve_policy_update(
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            min_self_bond=100,
            min_total_bond=100,
        )

        for validator, amount in {"node1": 400, "node2": 350, "node3": 300}.items():
            self.currency.transfer(amount=amount, to=validator, signer=self.deployer_vk)
            self.currency.approve(amount=amount, to="validators", signer=validator)

        self.members.bond_self(amount=300, signer="node1")
        self.members.bond_self(amount=250, signer="node2")
        self.members.bond_self(amount=200, signer="node3")

        self.members.rebalance(
            signer="node1",
            environment={"block_num": 1, "now": current_time},
        )
        self.assertEqual(self.members.active_validators.get(), ["node1", "node2"])
        self.assertEqual(self.members.statuses["node3"], "approved")

        self.members.announce_leave(
            signer="node1",
            environment={"block_num": 1, "now": current_time},
        )
        self.members.rebalance(
            signer="node2",
            environment={"block_num": 2, "now": current_time},
        )

        self.assertEqual(self.members.active_validators.get(), ["node2", "node3"])
        self.assertEqual(self.members.statuses["node1"], "leaving")
        self.assertFalse(self.members.get_validator(account="node1", signer="node1")["active"])

        exited = self.members.leave(
            signer="node1",
            environment={
                "block_num": 2,
                "now": Datetime(year=2024, month=1, day=8, hour=13),
            },
        )

        self.assertEqual(exited["status"], "left")
        self.assertEqual(exited["self_bond"], 0)
        self.assertEqual(exited["pending_unbond_count"], 1)
        self.assertEqual(exited["pending_unbond_total"], 300)

    def test_hybrid_mode_requires_vote_approval_before_rebalance_can_activate_candidate(
        self,
    ):
        self.approve_policy_update(
            selection_mode="hybrid",
            max_validators=4,
            power_mode="equal",
            min_self_bond=0,
            min_total_bond=0,
        )

        self.currency.transfer(amount=2000, to="new_member", signer=self.deployer_vk)
        self.currency.approve(amount=1500, to="validators", signer="new_member")
        self.members.register(
            signer="new_member",
            environment={"block_num": 0},
        )
        self.members.bond_self(amount=300, signer="new_member")

        rebalance = self.members.rebalance(
            signer="new_member",
            environment={"block_num": 1},
        )

        self.assertEqual(rebalance["selected"], ["node1", "node2", "node3"])
        self.assertFalse("new_member" in self.members.active_validators.get())
        self.assertEqual(self.members.statuses["new_member"], "pending")

        self.members.propose_vote(
            type_of_vote="add_member",
            arg="new_member",
            signer="node1",
            environment={"block_num": 2},
        )
        self.members.vote(
            proposal_id=2,
            vote="yes",
            signer="node2",
            environment={"block_num": 2},
        )
        self.members.vote(
            proposal_id=2,
            vote="yes",
            signer="node3",
            environment={"block_num": 2},
        )

        self.assertTrue("new_member" in self.members.active_validators.get())
        self.assertEqual(self.members.statuses["new_member"], "active")

    def test_rebalance_interval_and_activation_delay_are_enforced(self):
        self.approve_policy_update(
            selection_mode="auto_top_n",
            max_validators=1,
            power_mode="requested",
            rebalance_interval=5,
            activation_delay_epochs=1,
            min_self_bond=100,
            min_total_bond=100,
        )

        self.currency.transfer(amount=2000, to="new_member", signer=self.deployer_vk)
        self.currency.approve(amount=1500, to="validators", signer="new_member")
        self.members.register(
            signer="new_member",
            requested_validator_power=25,
            environment={"block_num": 0},
        )
        self.members.bond_self(amount=300, signer="new_member")

        with self.assertRaises(AssertionError):
            self.members.rebalance(
                signer="new_member",
                environment={"block_num": 0},
            )

        with self.assertRaises(AssertionError):
            self.members.rebalance(
                signer="new_member",
                environment={"block_num": 4},
            )

        rebalance = self.members.rebalance(
            signer="new_member",
            environment={"block_num": 5},
        )

        self.assertEqual(rebalance["epoch"], 1)
        self.assertEqual(rebalance["selected"], ["new_member"])
        self.assertEqual(self.members.statuses["new_member"], "active")

    def test_rebalance_churn_limit_allows_only_one_replacement_per_epoch(self):
        for validator, amount in {
            "node1": 600,
            "node2": 600,
            "node4": 1600,
            "node5": 1600,
        }.items():
            self.currency.transfer(
                amount=amount,
                to=validator,
                signer=self.deployer_vk,
            )
            self.currency.approve(amount=amount, to="validators", signer=validator)

        self.members.bond_self(amount=300, signer="node1")
        self.members.bond_self(amount=200, signer="node2")

        self.members.register(
            signer="node4",
            requested_validator_power=40,
            environment={"block_num": 0},
        )
        self.members.bond_self(amount=400, signer="node4")
        self.members.register(
            signer="node5",
            requested_validator_power=35,
            environment={"block_num": 0},
        )
        self.members.bond_self(amount=350, signer="node5")

        policy = self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            max_active_set_churn=1,
            min_self_bond=100,
            min_total_bond=100,
        )

        self.assertEqual(policy["max_active_set_churn"], 1)
        self.assertEqual(self.members.active_validators.get(), ["node4", "node1"])

        rebalance = self.members.rebalance(
            signer="node5",
            environment={"block_num": 1},
        )

        self.assertEqual(rebalance["selected"], ["node4", "node5"])
        self.assertEqual(self.members.active_validators.get(), ["node4", "node5"])
        self.assertEqual(self.members.statuses["node2"], "approved")
        self.assertEqual(self.members.statuses["node1"], "approved")

    def test_rebalance_margin_prevents_small_lead_from_replacing_incumbent(self):
        self.currency.transfer(amount=1000, to="node1", signer=self.deployer_vk)
        self.currency.transfer(amount=1000, to="node2", signer=self.deployer_vk)
        self.currency.transfer(amount=1000, to="node3", signer=self.deployer_vk)
        self.currency.approve(amount=1000, to="validators", signer="node1")
        self.currency.approve(amount=1000, to="validators", signer="node2")
        self.currency.approve(amount=1000, to="validators", signer="node3")

        self.members.bond_self(amount=200, signer="node1")
        self.members.bond_self(amount=190, signer="node2")

        self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            min_self_bond=100,
            min_total_bond=100,
            min_bond_margin_bps=1000,
        )

        self.members.bond_self(amount=195, signer="node3")
        rebalance = self.members.rebalance(
            signer="node3",
            environment={"block_num": 1},
        )

        self.assertEqual(rebalance["selected"], ["node1", "node2"])

        self.members.bond_self(amount=20, signer="node3")
        rebalance = self.members.rebalance(
            signer="node3",
            environment={"block_num": 2},
        )

        self.assertEqual(rebalance["selected"], ["node3", "node1"])

    def test_auto_mode_can_disable_manual_override_votes(self):
        self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=1,
            power_mode="equal",
            manual_override_enabled=False,
        )

        with self.assertRaises(AssertionError):
            self.members.propose_vote(
                type_of_vote="remove_member",
                arg="node1",
                signer="node1",
            )

        with self.assertRaises(AssertionError):
            self.members.propose_vote(
                type_of_vote="set_member_power",
                arg={"member": "node1", "power": 99},
                signer="node1",
            )

        with self.assertRaises(AssertionError):
            self.members.propose_vote(
                type_of_vote="jail_member",
                arg={"member": "node1", "reason": "downtime"},
                signer="node1",
            )

        with self.assertRaises(AssertionError):
            self.members.propose_vote(
                type_of_vote="unjail_member",
                arg="node1",
                signer="node1",
            )

    def test_auto_mode_still_allows_slash_votes_when_manual_overrides_are_disabled(
        self,
    ):
        self.currency.transfer(amount=500, to="node1", signer=self.deployer_vk)
        self.currency.transfer(amount=500, to="node2", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="node1")
        self.currency.approve(amount=500, to="validators", signer="node2")

        self.members.bond_self(amount=200, signer="node1")
        self.members.bond_self(amount=150, signer="node2")

        self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=2,
            min_self_bond=100,
            min_total_bond=100,
            manual_override_enabled=False,
        )

        self.members.propose_vote(
            type_of_vote="slash_member",
            arg={"member": "node1", "slash_bps": 1000, "reason": "downtime"},
            signer="node2",
            environment={"block_num": 1},
        )
        self.members.vote(
            proposal_id=2,
            vote="yes",
            signer="node1",
            environment={"block_num": 1},
        )

        self.assertEqual(self.members.total_slashed["node1"], 20)
        self.assertEqual(self.members.self_bond["node1"], 180)

    def test_slash_vote_slashes_live_bond_pro_rata_and_uses_configured_destination(
        self,
    ):
        policy = self.approve_policy_update(slash_destination="slash_treasury")
        self.assertEqual(policy["slash_destination"], "slash_treasury")

        self.currency.transfer(amount=2000, to="node1", signer=self.deployer_vk)
        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="node1")
        self.currency.approve(amount=500, to="validators", signer="delegator1")

        self.members.bond_self(amount=300, signer="node1")
        self.members.delegate(
            validator="node1",
            amount=200,
            signer="delegator1",
        )

        treasury_balance_before = self.currency.balances["slash_treasury"] or 0

        self.members.propose_vote(
            type_of_vote="slash_member",
            arg={"member": "node1", "slash_bps": 2000, "reason": "equivocation"},
            signer="node2",
        )
        self.members.vote(proposal_id=2, vote="yes", signer="node1")
        self.members.vote(proposal_id=2, vote="yes", signer="node3")

        slash_result = self.members.votes[2]["result"]

        self.assertTrue("node1" in self.members.active_validators.get())
        self.assertEqual(self.members.self_bond["node1"], 240)
        self.assertEqual(self.members.total_delegated["node1"], 160)
        self.assertEqual(self.members.delegations["delegator1", "node1"], 160)
        self.assertEqual(self.members.total_slashed["node1"], 100)
        self.assertIsNotNone(self.members.last_slashed_at["node1"])
        self.assertEqual(self.currency.balances["slash_treasury"], treasury_balance_before + 100)
        self.assertEqual(slash_result["slash_amount"], 100)
        self.assertEqual(slash_result["self_bond_slashed"], 60)
        self.assertEqual(slash_result["delegated_slashed"], 40)
        self.assertEqual(slash_result["destination"], "slash_treasury")

    def test_slash_in_auto_mode_rebalances_when_target_falls_below_minimums(self):
        for validator, amount in {
            "node1": 500,
            "node2": 500,
            "node3": 500,
        }.items():
            self.currency.transfer(
                amount=amount,
                to=validator,
                signer=self.deployer_vk,
            )
            self.currency.approve(amount=amount, to="validators", signer=validator)

        self.members.bond_self(amount=200, signer="node1")
        self.members.bond_self(amount=150, signer="node2")
        self.members.bond_self(amount=130, signer="node3")

        self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            min_self_bond=100,
            min_total_bond=100,
        )

        dao_balance_before = self.currency.balance_of(address="dao")
        self.assertEqual(self.members.active_validators.get(), ["node1", "node2"])

        self.members.propose_vote(
            type_of_vote="slash_member",
            arg={"member": "node1", "slash_bps": 6000, "reason": "equivocation"},
            signer="node2",
            environment={"block_num": 1},
        )
        self.members.vote(
            proposal_id=2,
            vote="yes",
            signer="node1",
            environment={"block_num": 1},
        )

        self.assertEqual(self.members.self_bond["node1"], 80)
        self.assertEqual(self.members.active_validators.get(), ["node2", "node3"])
        self.assertEqual(self.currency.balances["dao"], dao_balance_before + 120)

    def test_jail_and_unjail_flow_in_manual_mode(self):
        self.currency.transfer(amount=2000, to="node1", signer=self.deployer_vk)
        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="node1")
        self.currency.approve(amount=500, to="validators", signer="delegator1")

        self.members.bond_self(amount=300, signer="node1")
        self.members.delegate(
            validator="node1",
            amount=200,
            signer="delegator1",
        )

        self.members.propose_vote(
            type_of_vote="jail_member",
            arg={"member": "node1", "reason": "downtime"},
            signer="node2",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node1")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.assertFalse("node1" in self.members.active_validators.get())
        self.assertTrue(self.members.jailed["node1"])
        self.assertEqual(self.members.statuses["node1"], "approved")
        self.assertEqual(self.members.powers["node1"], 0)
        self.assertEqual(self.members.self_bond["node1"], 300)
        self.assertEqual(self.members.total_delegated["node1"], 200)
        self.assertEqual(
            self.members.get_validator(account="node1", signer="node1")["jail_reason"],
            "downtime",
        )
        self.assertIsNotNone(
            self.members.get_validator(account="node1", signer="node1")["last_jailed_at"]
        )

        with self.assertRaises(AssertionError):
            self.members.delegate(
                validator="node1",
                amount=1,
                signer="delegator1",
            )

        with self.assertRaises(AssertionError):
            self.members.bond_self(amount=1, signer="node1")

        self.members.propose_vote(
            type_of_vote="unjail_member",
            arg="node1",
            signer="node2",
        )
        self.members.vote(proposal_id=2, vote="yes", signer="node3")

        self.assertFalse(self.members.jailed["node1"])
        self.assertEqual(self.members.statuses["node1"], "approved")
        self.assertFalse("node1" in self.members.active_validators.get())
        self.assertIsNotNone(
            self.members.get_validator(account="node1", signer="node1")["last_unjailed_at"]
        )

        self.members.propose_vote(
            type_of_vote="add_member",
            arg="node1",
            signer="node2",
        )
        self.members.vote(proposal_id=3, vote="yes", signer="node3")

        self.assertTrue("node1" in self.members.active_validators.get())
        self.assertEqual(self.members.statuses["node1"], "active")

    def test_jail_in_auto_mode_excludes_validator_until_unjailed(self):
        for validator, amount in {
            "node1": 600,
            "node2": 600,
            "node3": 600,
        }.items():
            self.currency.transfer(
                amount=amount,
                to=validator,
                signer=self.deployer_vk,
            )
            self.currency.approve(amount=amount, to="validators", signer=validator)

        self.members.bond_self(amount=300, signer="node1")
        self.members.bond_self(amount=250, signer="node2")
        self.members.bond_self(amount=200, signer="node3")

        self.approve_policy_update(
            environment={"block_num": 0},
            selection_mode="auto_top_n",
            max_validators=2,
            power_mode="requested",
            min_self_bond=100,
            min_total_bond=100,
        )

        self.assertEqual(self.members.active_validators.get(), ["node1", "node2"])

        self.members.propose_vote(
            type_of_vote="jail_member",
            arg={"member": "node1", "reason": "downtime"},
            signer="node2",
            environment={"block_num": 1},
        )
        self.members.vote(
            proposal_id=2,
            vote="yes",
            signer="node1",
            environment={"block_num": 1},
        )

        self.assertTrue(self.members.jailed["node1"])
        self.assertEqual(self.members.statuses["node1"], "approved")
        self.assertEqual(self.members.active_validators.get(), ["node2", "node3"])

        self.members.propose_vote(
            type_of_vote="unjail_member",
            arg="node1",
            signer="node2",
            environment={"block_num": 2},
        )
        self.members.vote(
            proposal_id=3,
            vote="yes",
            signer="node3",
            environment={"block_num": 2},
        )

        self.assertFalse(self.members.jailed["node1"])
        self.assertEqual(self.members.active_validators.get(), ["node1", "node2"])

    def test_leave_refunds_registration_bond(self):
        self.currency.approve(amount=1000, to="validators", signer="new_member")
        self.currency.transfer(amount=1000, to="new_member", signer=self.deployer_vk)
        self.members.register(signer="new_member")
        self.members.propose_vote(
            type_of_vote="add_member",
            arg="new_member",
            signer="node1",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        before_balance = self.currency.balances["new_member"]
        current_time = Datetime(year=2024, month=1, day=1)
        self.members.announce_leave(
            signer="new_member", environment={"now": current_time, "block_num": 0}
        )

        future_time = Datetime(year=2024, month=1, day=8, hour=1)
        self.members.leave(signer="new_member", environment={"now": future_time, "block_num": 1})

        self.assertEqual(self.members.statuses["new_member"], "left")
        self.assertEqual(self.members.holdings["new_member"], 0)
        self.assertEqual(self.currency.balances["new_member"], before_balance + 1000)

    def test_leave_forces_pending_unbonds_for_validator_and_delegator_stake(self):
        start_time = Datetime(year=2024, month=1, day=1, hour=12, minute=0, second=0)
        leave_time = Datetime(year=2024, month=1, day=8, hour=13, minute=0, second=0)
        claim_time = Datetime(year=2024, month=1, day=16, hour=13, minute=0, second=0)

        self.currency.transfer(amount=3000, to="new_member", signer=self.deployer_vk)
        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=2000, to="validators", signer="new_member")
        self.currency.approve(amount=500, to="validators", signer="delegator1")

        self.members.register(
            signer="new_member",
            environment={"now": start_time, "block_num": 0},
        )
        self.members.propose_vote(
            type_of_vote="add_member",
            arg="new_member",
            signer="node1",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.members.bond_self(amount=300, signer="new_member")
        self.members.delegate(
            validator="new_member",
            amount=200,
            signer="delegator1",
            environment={"now": start_time},
        )

        validator_balance_before_exit = self.currency.balances["new_member"]
        delegator_balance_before_claim = self.currency.balances["delegator1"]

        self.members.announce_leave(
            signer="new_member",
            environment={"now": start_time, "block_num": 0},
        )
        self.members.leave(
            signer="new_member",
            environment={"now": leave_time, "block_num": 8},
        )

        self.assertEqual(self.members.statuses["new_member"], "left")
        self.assertEqual(self.members.self_bond["new_member"], 0)
        self.assertEqual(self.members.total_delegated["new_member"], 0)
        self.assertEqual(self.members.delegations["delegator1", "new_member"], 0)
        self.assertEqual(
            self.members.get_delegators(validator="new_member", signer="new_member"),
            [],
        )

        validator_unbond_ids = self.members.get_pending_unbond_ids(
            owner="new_member",
            signer="new_member",
        )
        delegator_unbond_ids = self.members.get_pending_unbond_ids(
            owner="delegator1",
            signer="delegator1",
        )

        self.assertEqual(len(validator_unbond_ids), 1)
        self.assertEqual(len(delegator_unbond_ids), 1)

        validator_unbond = self.members.get_pending_unbond(
            unbond_id=validator_unbond_ids[0],
            signer="new_member",
        )
        delegator_unbond = self.members.get_pending_unbond(
            unbond_id=delegator_unbond_ids[0],
            signer="delegator1",
        )

        self.assertEqual(validator_unbond["kind"], "self_bond")
        self.assertEqual(validator_unbond["amount"], 300)
        self.assertEqual(validator_unbond["reason"], "left")
        self.assertEqual(delegator_unbond["kind"], "delegation")
        self.assertEqual(delegator_unbond["amount"], 200)
        self.assertEqual(delegator_unbond["reason"], "left")

        self.members.claim_unbond(
            unbond_id=validator_unbond["id"],
            signer="new_member",
            environment={"now": claim_time},
        )
        self.members.claim_unbond(
            unbond_id=delegator_unbond["id"],
            signer="delegator1",
            environment={"now": claim_time},
        )

        self.assertEqual(
            self.currency.balances["new_member"],
            validator_balance_before_exit + 1300,
        )
        self.assertEqual(
            self.currency.balances["delegator1"],
            delegator_balance_before_claim + 200,
        )

    def test_remove_member_forces_pending_unbonds_for_validator_and_delegator_stake(
        self,
    ):
        self.currency.transfer(amount=3000, to="new_member", signer=self.deployer_vk)
        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=2000, to="validators", signer="new_member")
        self.currency.approve(amount=500, to="validators", signer="delegator1")

        self.members.register(signer="new_member")
        self.members.propose_vote(
            type_of_vote="add_member",
            arg="new_member",
            signer="node1",
        )
        self.members.vote(proposal_id=1, vote="yes", signer="node2")
        self.members.vote(proposal_id=1, vote="yes", signer="node3")

        self.members.bond_self(amount=300, signer="new_member")
        self.members.delegate(
            validator="new_member",
            amount=200,
            signer="delegator1",
        )

        self.members.propose_vote(
            type_of_vote="remove_member",
            arg="new_member",
            signer="node1",
        )
        self.members.vote(proposal_id=2, vote="yes", signer="node2")
        self.members.vote(proposal_id=2, vote="yes", signer="node3")
        self.members.vote(
            proposal_id=2,
            vote="yes",
            signer="new_member",
            environment={"block_num": 2},
        )

        self.assertEqual(self.members.statuses["new_member"], "removed")
        self.assertEqual(self.members.self_bond["new_member"], 0)
        self.assertEqual(self.members.total_delegated["new_member"], 0)
        self.assertEqual(self.members.delegations["delegator1", "new_member"], 0)

        validator_unbond_ids = self.members.get_pending_unbond_ids(
            owner="new_member",
            signer="new_member",
        )
        delegator_unbond_ids = self.members.get_pending_unbond_ids(
            owner="delegator1",
            signer="delegator1",
        )

        self.assertEqual(len(validator_unbond_ids), 1)
        self.assertEqual(len(delegator_unbond_ids), 1)
        self.assertEqual(
            self.members.get_pending_unbond(
                unbond_id=validator_unbond_ids[0],
                signer="new_member",
            )["reason"],
            "removed",
        )
        self.assertEqual(
            self.members.get_pending_unbond(
                unbond_id=delegator_unbond_ids[0],
                signer="delegator1",
            )["reason"],
            "removed",
        )

    def test_self_bond_and_delegation_update_reward_distribution_state(self):
        self.currency.transfer(amount=2000, to="node1", signer=self.deployer_vk)
        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="node1")
        self.currency.approve(amount=500, to="validators", signer="delegator1")

        self.members.update_profile(
            commission_bps_value=1200,
            signer="node1",
        )
        self.members.bond_self(amount=300, signer="node1")
        self.members.delegate(
            validator="node1",
            amount=200,
            reward_key="delegator1-reward",
            signer="delegator1",
        )

        reward_info = self.members.get_reward_distribution_info(validator="node1", signer="node1")

        self.assertEqual(self.members.self_bond["node1"], 300)
        self.assertEqual(self.members.total_delegated["node1"], 200)
        self.assertEqual(self.members.delegations["delegator1", "node1"], 200)
        self.assertEqual(
            self.members.get_delegators(validator="node1", signer="node1"),
            ["delegator1"],
        )
        self.assertEqual(reward_info["commission_bps"], 1200)
        self.assertEqual(reward_info["total_bond"], 500)

    def test_undelegate_creates_claimable_unbond(self):
        base_time = Datetime(year=2024, month=1, day=1, hour=12, minute=0, second=0)
        future_time = Datetime(year=2024, month=1, day=9, hour=12, minute=0, second=0)

        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="delegator1")
        self.members.delegate(
            validator="node1",
            amount=200,
            signer="delegator1",
            environment={"now": base_time},
        )

        unbond = self.members.undelegate(
            validator="node1",
            amount=50,
            signer="delegator1",
            environment={"now": base_time, "block_num": 1},
        )

        self.assertEqual(unbond["amount"], 50)
        self.assertEqual(unbond["owner"], "delegator1")
        self.assertEqual(self.members.delegations["delegator1", "node1"], 150)

        claimed = self.members.claim_unbond(
            unbond_id=unbond["id"],
            signer="delegator1",
            environment={"now": future_time},
        )

        self.assertTrue(claimed["claimed"])

    def test_evidence_slashes_pending_unbond_for_prior_infraction(self):
        base_time = Datetime(year=2024, month=1, day=1, hour=12, minute=0, second=0)
        claim_time = Datetime(year=2024, month=1, day=9, hour=12, minute=0, second=0)

        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="delegator1")
        self.members.delegate(
            validator="node1",
            amount=200,
            signer="delegator1",
            environment={"now": base_time, "block_num": 3},
        )

        unbond = self.members.undelegate(
            validator="node1",
            amount=50,
            signer="delegator1",
            environment={"now": base_time, "block_num": 7},
        )
        dao_balance_before = self.currency.balance_of(address="dao")

        result = self.members.apply_evidence_penalty(
            member="node1",
            infraction_type="DUPLICATE_VOTE",
            evidence_id="duplicate-vote-prior-infraction",
            evidence_height=6,
            signer="__evidence_penalty_driver__",
            environment={"now": base_time, "block_num": 10},
        )
        unbond_after = self.members.get_pending_unbond(
            unbond_id=unbond["id"],
            signer="delegator1",
        )

        self.assertTrue(result["applied"])
        self.assertEqual(result["slash_result"]["delegated_slashed"], 7.5)
        self.assertEqual(result["slash_result"]["pending_unbond_slashed"], 2.5)
        self.assertEqual(self.members.delegations["delegator1", "node1"], 142.5)
        self.assertEqual(self.members.total_delegated["node1"], 142.5)
        self.assertEqual(unbond_after["created_block"], 7)
        self.assertEqual(unbond_after["amount"], 47.5)
        self.assertEqual(self.currency.balances["dao"], dao_balance_before + 10)
        validator = self.members.get_validator(account="node1", signer="node1")
        self.assertEqual(
            validator["last_evidence_id"],
            "duplicate-vote-prior-infraction",
        )
        self.assertEqual(validator["last_evidence_type"], "DUPLICATE_VOTE")
        self.assertEqual(validator["last_evidence_height"], 6)
        self.assertEqual(validator["last_evidence_at"], base_time)

        claimed = self.members.claim_unbond(
            unbond_id=unbond["id"],
            signer="delegator1",
            environment={"now": claim_time},
        )

        self.assertEqual(claimed["amount"], 47.5)
        self.assertTrue(claimed["claimed"])

    def test_evidence_does_not_slash_pending_unbond_for_later_infraction(self):
        base_time = Datetime(year=2024, month=1, day=1, hour=12, minute=0, second=0)

        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=500, to="validators", signer="delegator1")
        self.members.delegate(
            validator="node1",
            amount=200,
            signer="delegator1",
            environment={"now": base_time, "block_num": 3},
        )

        unbond = self.members.undelegate(
            validator="node1",
            amount=50,
            signer="delegator1",
            environment={"now": base_time, "block_num": 7},
        )
        dao_balance_before = self.currency.balance_of(address="dao")

        result = self.members.apply_evidence_penalty(
            member="node1",
            infraction_type="DUPLICATE_VOTE",
            evidence_id="duplicate-vote-later-infraction",
            evidence_height=8,
            signer="__evidence_penalty_driver__",
            environment={"now": base_time, "block_num": 10},
        )
        unbond_after = self.members.get_pending_unbond(
            unbond_id=unbond["id"],
            signer="delegator1",
        )

        self.assertTrue(result["applied"])
        self.assertEqual(result["slash_result"]["delegated_slashed"], 7.5)
        self.assertEqual(result["slash_result"]["pending_unbond_slashed"], 0)
        self.assertEqual(self.members.delegations["delegator1", "node1"], 142.5)
        self.assertEqual(self.members.total_delegated["node1"], 142.5)
        self.assertEqual(unbond_after["created_block"], 7)
        self.assertEqual(unbond_after["amount"], 50)
        self.assertEqual(self.currency.balances["dao"], dao_balance_before + 7.5)

    def test_unregister_forces_pending_unbonds_for_candidate_stake(self):
        self.currency.transfer(amount=3000, to="new_member", signer=self.deployer_vk)
        self.currency.transfer(amount=2000, to="delegator1", signer=self.deployer_vk)
        self.currency.approve(amount=2000, to="validators", signer="new_member")
        self.currency.approve(amount=500, to="validators", signer="delegator1")

        self.members.register(signer="new_member")
        self.members.bond_self(amount=300, signer="new_member")
        self.members.delegate(
            validator="new_member",
            amount=200,
            signer="delegator1",
        )

        self.members.unregister(
            signer="new_member",
            environment={"block_num": 1},
        )

        self.assertEqual(self.members.statuses["new_member"], "withdrawn")
        self.assertFalse("new_member" in self.members.candidates.get())
        self.assertEqual(self.members.self_bond["new_member"], 0)
        self.assertEqual(self.members.total_delegated["new_member"], 0)
        self.assertEqual(self.members.delegations["delegator1", "new_member"], 0)

        validator_unbond_ids = self.members.get_pending_unbond_ids(
            owner="new_member",
            signer="new_member",
        )
        delegator_unbond_ids = self.members.get_pending_unbond_ids(
            owner="delegator1",
            signer="delegator1",
        )

        self.assertEqual(len(validator_unbond_ids), 1)
        self.assertEqual(len(delegator_unbond_ids), 1)
        self.assertEqual(
            self.members.get_pending_unbond(
                unbond_id=validator_unbond_ids[0],
                signer="new_member",
            )["reason"],
            "withdrawn",
        )
        self.assertEqual(
            self.members.get_pending_unbond(
                unbond_id=delegator_unbond_ids[0],
                signer="delegator1",
            )["reason"],
            "withdrawn",
        )

        validators = self.members.get_validators(signer="node1")
        withdrawn = next(
            validator for validator in validators if validator["account"] == "new_member"
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertFalse(withdrawn["active"])
        self.assertEqual(withdrawn["pending_unbond_count"], 2)
        self.assertEqual(withdrawn["pending_unbond_total"], 500)
        self.assertIsNotNone(withdrawn["next_unbond_unlock_at"])
        self.assertIn("last_rebalance_epoch", withdrawn)
        self.assertIn("selection_eligible_at_last_rebalance", withdrawn)

    def test_vote_snapshot_excludes_members_added_after_proposal_creation(self):
        self.currency.approve(amount=1000, to="validators", signer="new_member")
        self.currency.transfer(amount=1000, to="new_member", signer=self.deployer_vk)
        self.members.register(signer="new_member")

        self.members.propose_vote(
            type_of_vote="change_registration_fee",
            arg=2000,
            signer="node1",
        )

        self.members.propose_vote(
            type_of_vote="add_member",
            arg="new_member",
            signer="node1",
        )
        self.members.vote(proposal_id=2, vote="yes", signer="node2")
        self.members.vote(proposal_id=2, vote="yes", signer="node3")

        with self.assertRaises(AssertionError):
            self.members.vote(
                proposal_id=1,
                vote="yes",
                signer="new_member",
            )
