import os
import unittest

from contracting.local import ContractingClient

from xian.config_paths import resolve_contracts_dir


class TestCurrencyContract(unittest.TestCase):
    def setUp(self):

        # Called before every test, bootstraps the environment.
        self.chain_id = "test-chain"
        self.environment = {"chain_id": self.chain_id}
        self.deployer_vk = "xian-deployer"

        self.client = ContractingClient(environment=self.environment)
        self.client.flush()

        self.contracts_dir = str(resolve_contracts_dir())

        currency_path = os.path.join(self.contracts_dir, "currency.s.py")

        with open(currency_path) as f:
            code = f.read()
            self.client.submit(code, name="currency", constructor_args={"vk": self.deployer_vk})

        self.currency = self.client.get_contract_proxy("currency")

    def tearDown(self):
        # Called after every test, ensures each test starts with a clean slate and is isolated from others
        self.client.flush()

    def test_balance_of(self):
        # GIVEN
        receiver = "receiver_account"
        self.currency.balances[receiver] = 100000000000000

        # WHEN
        balance = self.currency.balance_of(address=receiver, signer="sys")

        # THEN
        self.assertEqual(balance, 100000000000000)

    def test_initial_balance(self):
        # GIVEN the initial setup
        # WHEN checking the initial balance
        sys_balance = self.currency.balances[self.deployer_vk]
        # THEN the balance should be as expected
        self.assertEqual(sys_balance, 111111111)

    def test_no_implicit_contract_allocations(self):
        self.assertEqual(self.currency.balance_of(address="team_lock"), 0)
        self.assertEqual(self.currency.balance_of(address="dao"), 0)

    def test_intermediate_stream_accounts_are_empty(self):
        self.assertEqual(self.currency.balance_of(address="team_vesting", signer="sys"), 0)
        self.assertEqual(self.currency.balance_of(address="dao_funding_stream", signer="sys"), 0)

    def test_transfer(self):
        # GIVEN a transfer setup
        self.currency.transfer(amount=100, to="bob", signer=self.deployer_vk)
        # WHEN checking balances after transfer
        bob_balance = self.currency.balances["bob"]
        sys_balance = self.currency.balances[self.deployer_vk]
        # THEN the balances should reflect the transfer correctly
        self.assertEqual(bob_balance, 100)
        self.assertEqual(sys_balance, 111111111 - 100)

    def test_change_metadata(self):
        # GIVEN a non-operator trying to change metadata
        with self.assertRaises(Exception):
            self.currency.change_metadata(key="token_name", value="NEW TOKEN", signer="bob")
        # WHEN the operator changes metadata
        self.currency.change_metadata(key="token_name", value="NEW TOKEN", signer="governance")
        new_name = self.currency.metadata["token_name"]
        # THEN the metadata should be updated correctly
        self.assertEqual(new_name, "NEW TOKEN")

    def test_change_metadata_cannot_replace_sensitive_authority_keys(self):
        for key in ("operator", "total_supply"):
            with self.subTest(key=key):
                original = self.currency.metadata[key]
                with self.assertRaises(Exception):
                    self.currency.change_metadata(
                        key=key,
                        value="attacker",
                        signer="governance",
                    )
                self.assertEqual(self.currency.metadata[key], original)

    def test_governance_controls_sensitive_currency_settings(self):
        with self.assertRaisesRegex(Exception, "Only governance"):
            self.currency.set_operator(new_operator="ops", signer="mallory")
        self.currency.set_operator(new_operator="ops", signer="governance")
        self.assertEqual(self.currency.metadata["operator"], "ops")

    def test_approve_and_allowance(self):
        # GIVEN an approval setup
        self.currency.approve(amount=500, to="eve", signer="sys")
        # WHEN checking the allowance
        allowance = self.currency.approvals["sys", "eve"]
        # THEN the allowance should be set correctly
        self.assertEqual(allowance, 500)

    def test_transfer_from_without_approval(self):
        # GIVEN an attempt to transfer without approval
        # WHEN the transfer is attempted
        # THEN it should fail
        with self.assertRaises(Exception):
            self.currency.transfer_from(amount=100, to="bob", main_account="sys", signer="bob")

    def test_transfer_from_with_approval(self):
        # GIVEN a setup with approval
        self.currency.approve(amount=200, to="bob", signer=self.deployer_vk)
        # WHEN transferring with approval
        self.currency.transfer_from(
            amount=100, to="bob", main_account=self.deployer_vk, signer="bob"
        )
        bob_balance = self.currency.balances["bob"]
        sys_balance = self.currency.balances[self.deployer_vk]
        remaining_allowance = self.currency.approvals[self.deployer_vk, "bob"]
        # THEN the balances and allowance should reflect the transfer
        self.assertEqual(bob_balance, 100)
        self.assertEqual(sys_balance, 111111111 - 100)
        self.assertEqual(remaining_allowance, 100)

    def test_approve_overwrites_previous_allowance(self):
        # GIVEN an initial approval setup
        self.currency.approve(amount=500, to="eve", signer="sys")
        initial_allowance = self.currency.approvals["sys", "eve"]
        self.assertEqual(initial_allowance, 500)

        # WHEN a new approval is made
        self.currency.approve(amount=200, to="eve", signer="sys")
        new_allowance = self.currency.approvals["sys", "eve"]

        # THEN the new allowance should overwrite the old one
        self.assertEqual(new_allowance, 200)

    def test_old_authorizer_cannot_grant_allowance(self):
        self.client.submit(
            """
import currency
@export
def grant(owner: str, spender: str, amount: float):
    currency.approve_from_authorizer(owner=owner, spender=spender, amount=amount)
""",
            name="con_old_authorizer",
        )
        authorizer = self.client.get_contract_proxy("con_old_authorizer")
        self.currency.balances["alice"] = 100
        self.currency.approve(amount=7, to="bob", signer="alice")
        for enrolled in (False, True):
            if enrolled:
                self.client.raw_driver.set("_accounts.records:alice", {"version": 1})
            with self.assertRaises(Exception):
                authorizer.grant(owner="alice", spender="bob", amount=99, signer="mallory")
            self.assertEqual(self.client.raw_driver.get("currency.approvals:alice:bob"), 7)
        self.currency.transfer_from(amount=2, to="bob", main_account="alice", signer="bob")
        self.assertEqual(self.currency.balances["alice"], 98)
        self.currency.approve(amount=0, to="bob", signer="alice")
        self.assertEqual(self.currency.approvals["alice", "bob"], 0)


if __name__ == "__main__":
    unittest.main()
