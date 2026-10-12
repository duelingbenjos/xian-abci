"""Consensus-backed account authorization without changing account identities.

Reads are side-effect free. Only the native registry transaction executor returns
registry writes, which the normal block commit persists with balances and nonces.
"""

from dataclasses import dataclass

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from xian_accounts import hybrid

from xian.exceptions import TransactionException
from xian.utils.encoding import canonical_json_text

POLICY_KEY = "_accounts.policy"
RECORD_PREFIX = "_accounts.records:"
SYSTEM_CONTRACT = "_accounts"
OPERATIONS = frozenset({"enroll", "rotate", "recover"})


def is_registry_transaction(tx: dict) -> bool:
    return (
        tx.get("metadata", {}).get("tx_format") == hybrid.TX_FORMAT
        and tx.get("payload", {}).get("contract") == SYSTEM_CONTRACT
    )


@dataclass(frozen=True)
class RegistryPolicy:
    enrollment_open: bool
    transition_chi: int

    @classmethod
    def parse(cls, value):
        hybrid.exact_dict(value, {"version", "enrollment_open", "transition_chi"})
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported registry policy")
        if type(value["enrollment_open"]) is not bool:
            raise ValueError("invalid enrollment policy")
        cost = hybrid.uint64(value["transition_chi"])
        if cost == 0:
            raise ValueError("registry transition cost must be positive")
        return cls(value["enrollment_open"], cost)


class AccountRegistry:
    def __init__(self, driver, chain_id: str):
        self.driver = driver
        self.chain_id = chain_id
        self._transitioned: set[str] = set()

    def policy(self) -> RegistryPolicy | None:
        value = self.driver.get(POLICY_KEY, save=False)
        return None if value is None else RegistryPolicy.parse(value)

    def record(self, sender: str) -> hybrid.AccountRecord | None:
        value = self.driver.get(RECORD_PREFIX + sender, save=False)
        return None if value is None else hybrid.AccountRecord.from_dict(value)

    def accept(self, tx: dict) -> None:
        """Advance a proposal/block barrier only after fees and nonce checks pass."""
        if is_registry_transaction(tx):
            self._transitioned.add(tx["payload"]["sender"])

    def authorize(self, tx: dict) -> hybrid.AccountRecord | None:
        try:
            return self._authorize(tx)
        except (ValueError, BadSignatureError) as exc:
            raise TransactionException(f"Account authorization failed: {exc}") from exc

    def _authorize(self, tx: dict) -> hybrid.AccountRecord | None:
        payload = tx["payload"]
        sender = payload["sender"]
        if sender in self._transitioned:
            raise ValueError("account key change requires the next committed block")
        current = self.record(sender)
        metadata = tx["metadata"]
        if "tx_format" not in metadata:
            if current is not None:
                raise ValueError("enrolled accounts require hybrid authorization")
            return None  # Legacy signature is checked by the existing validator.

        policy = self.policy()
        if policy is None:
            raise ValueError("account registry is not activated")
        if payload["chain_id"] != self.chain_id:
            raise ValueError("wrong chain id")
        auth = metadata["auth"]
        registry_tx = is_registry_transaction(tx)
        operation = payload["function"] if registry_tx else None
        fields = {"scheme", "signature"}
        if registry_tx:
            fields |= {"active_proof", "recovery_proof"}
        hybrid.exact_dict(auth, fields)
        if not registry_tx:
            if current is None or auth["scheme"] != hybrid.SCHEME:
                raise ValueError("hybrid account required")
            signature = hybrid.hex_bytes(auth["signature"], hybrid.AUTH_BYTES)
            message = _payload_bytes(payload)
            if not hybrid.verify(current.active, current.epoch, message, signature):
                raise ValueError("invalid hybrid transaction signature")
            return None

        if operation not in OPERATIONS:
            raise ValueError("unsupported registry operation")
        if payload["chi_supplied"] < policy.transition_chi:
            raise ValueError("insufficient registry chi budget")
        # Reject closed enrollment before parsing candidate curve points or
        # serializing a potentially large payload supplied by an unauthenticated caller.
        if current is None and (operation != "enroll" or not policy.enrollment_open):
            raise ValueError("legacy enrollment is unavailable")
        hybrid.exact_dict(payload["kwargs"], {"record"})
        candidate = hybrid.AccountRecord.from_dict(payload["kwargs"]["record"])
        if current is None:
            if auth["scheme"] != hybrid.ENROLL_SCHEME or candidate.epoch != 0:
                raise ValueError("invalid enrollment scheme or epoch")
            old_ed, old_pq = {bytes.fromhex(sender)}, set()
            authority = None
            authority_signature = hybrid.hex_bytes(auth["signature"], 64)
        else:
            if operation == "enroll" or auth["scheme"] != hybrid.SCHEME:
                raise ValueError("no re-enrollment or downgrade")
            if candidate.epoch != current.epoch + 1:
                raise ValueError("invalid next key epoch")
            old_ed = {current.active.ed25519, current.recovery.ed25519}
            old_pq = {current.active.ml_dsa_65, current.recovery.ml_dsa_65}
            authority = current.recovery if operation == "recover" else current.active
            authority_signature = hybrid.hex_bytes(auth["signature"], hybrid.AUTH_BYTES)
        for keys in (candidate.active, candidate.recovery):
            if keys.ed25519 in old_ed or keys.ml_dsa_65 in old_pq:
                raise ValueError("replacement keys must be fresh")
        # Bound and decode every proof before doing expensive verification.
        active_proof = hybrid.hex_bytes(auth["active_proof"], hybrid.AUTH_BYTES)
        recovery_proof = hybrid.hex_bytes(auth["recovery_proof"], hybrid.AUTH_BYTES)
        message = _payload_bytes(payload)
        if authority is None:
            VerifyKey(bytes.fromhex(sender)).verify(
                hybrid.enrollment_transcript(message), authority_signature
            )
        elif not hybrid.verify(
            authority, current.epoch, message, authority_signature, role="authority"
        ):
            raise ValueError("invalid current authority")
        if not hybrid.verify(
            candidate.active, candidate.epoch, message, active_proof, role="new_active"
        ):
            raise ValueError("invalid active possession proof")
        if not hybrid.verify(
            candidate.recovery, candidate.epoch, message, recovery_proof, role="new_recovery"
        ):
            raise ValueError("invalid recovery possession proof")
        return candidate


def _payload_bytes(payload: dict) -> bytes:
    message = canonical_json_text(payload).encode("utf-8")
    if len(message) > hybrid.MAX_PAYLOAD_BYTES:
        raise ValueError("hybrid payload exceeds size limit")
    return message


def registry_for_app(app) -> AccountRegistry:
    return AccountRegistry(app.client.raw_driver, app.chain_id)


def execute_registry_transaction(driver, tx: dict, *, chi_cost, charge_fees: bool) -> dict:
    """Return a native execution result; never write outside the block batch."""
    from xian_runtime_types.decimal import ContractingDecimal

    from xian.execution_engine import vm_metering_writes

    registry = AccountRegistry(driver, tx["payload"]["chain_id"])
    candidate = registry.authorize(tx)
    if candidate is None:
        raise TransactionException("registry transition required")
    policy = registry.policy()
    sender = tx["payload"]["sender"]
    writes = {}
    status, result = 0, "Account authorization updated"
    if charge_fees:
        if chi_cost <= 0:
            raise TransactionException("invalid registry chi rate")
        balance = driver.get(f"currency.balances:{sender}", save=False) or 0
        if balance < ContractingDecimal(policy.transition_chi) / ContractingDecimal(chi_cost):
            status, result = 1, "Insufficient balance for account transition"
        writes.update(
            vm_metering_writes(
                driver, sender=sender, chi_used=policy.transition_chi, chi_cost=chi_cost
            )
        )
    if status == 0:
        writes[RECORD_PREFIX + sender] = candidate.to_dict()
    return {
        "status_code": status,
        "result": result,
        "writes": writes,
        "events": [],
        "chi_used": policy.transition_chi,
        "reads": {},
        "prefix_reads": frozenset(),
        "contract_costs": {},
    }
