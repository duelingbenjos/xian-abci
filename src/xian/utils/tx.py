import hashlib
import json
from collections.abc import Callable
from copy import deepcopy

from loguru import logger
from xian_accounts import hybrid, verify_message
from xian_runtime_types.encoding import convert_dict, encode

from xian.exceptions import TransactionException
from xian.formatting import (
    TRANSACTION_PAYLOAD_RULES,
    TRANSACTION_RULES,
    contract_name_is_formatted,
)
from xian.utils.encoding import decode_transaction_bytes

try:
    from xian_fastpath_core import (
        decode_and_validate_transaction_static as _native_decode_and_validate_transaction_static,
    )
except ImportError:  # pragma: no cover - exercised through fallback path
    _native_decode_and_validate_transaction_static = None


def verify(vk: str, msg: str, signature: str):
    return verify_message(vk, msg, signature)


def canonical_json(value: dict) -> str:
    return json.dumps(
        format_dictionary(deepcopy(value)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def unpack_transaction(tx):
    timestamp = tx["metadata"].get("timestamp", None)
    if timestamp:
        logger.info("Please remove timestamp from metadata")
    chain_id = tx["payload"].get("chain_id", "")
    if not chain_id:
        logger.debug("Please add chain_id to payload")

    sender = tx["payload"]["sender"]
    signature = tx["metadata"]["signature"]
    tx_for_verification = {
        "chain_id": chain_id,
        "contract": tx["payload"]["contract"],
        "function": tx["payload"]["function"],
        "kwargs": tx["payload"]["kwargs"],
        "nonce": tx["payload"]["nonce"],
        "sender": tx["payload"]["sender"],
        "chi_supplied": tx["payload"]["chi_supplied"],
    }
    tx_for_verification = canonical_json(tx_for_verification)
    return sender, signature, tx_for_verification


def tx_hash_from_tx(tx):
    h = hashlib.sha3_256()
    tx_dict = format_dictionary(tx)
    encoded_tx = encode(tx_dict).encode()
    h.update(encoded_tx)
    return h.hexdigest()


def canonical_transaction_size_bytes(tx: dict) -> int:
    tx_dict = format_dictionary(
        {
            "metadata": deepcopy(tx.get("metadata", {})),
            "payload": deepcopy(tx.get("payload", {})),
        }
    )
    return len(encode(tx_dict).encode())


def recurse_rules(d: dict, rule: dict | Callable):
    if callable(rule):
        return rule(d)

    for key, subrule in rule.items():
        if key not in d:
            return False
        arg = d[key]

        if callable(subrule):
            if not subrule(arg):
                return False

        elif isinstance(arg, dict):
            if not recurse_rules(arg, subrule):
                return False

        elif isinstance(arg, list):
            for a in arg:
                if not recurse_rules(a, subrule):
                    return False

        else:
            return False

    return True


def check_enough_chi(
    balance: object,
    chi_per_tau: object,
    chi_supplied: object,
    contract: object = None,
    function: object = None,
    amount: object = 0,
):

    if balance * chi_per_tau < chi_supplied:
        raise TransactionException("Transaction sender has too few chi for this transaction")

    # Prevent people from sending their entire balances for free by checking if that is what they are doing.
    if contract == "currency" and function == "transfer":
        # If you have less than 2 transactions worth of native token after trying to send your amount, fail.
        if ((balance - amount) * chi_per_tau) / 6 < 2:
            raise TransactionException("Transaction sender has too few chi for this transaction")


def check_format(d: dict, rule: dict):
    expected_keys = set(rule.keys())

    if not dict_has_keys(d, expected_keys):
        raise TransactionException("Transaction has unexpected or missing keys")
    if not recurse_rules(d, rule):
        raise TransactionException("Transaction has wrongly formatted dictionary")


def check_tx_keys(tx):
    metadata = tx.get("metadata")

    if not isinstance(metadata, dict) or not metadata:
        raise TransactionException("Metadata is missing")
    if len(metadata.keys()) != 1:
        raise TransactionException("Wrong number of metadata entries")

    payload = tx.get("payload")

    if not isinstance(payload, dict) or not payload:
        raise TransactionException("Payload is missing")

    expected_payload_keys = set(TRANSACTION_PAYLOAD_RULES)
    payload_keys = set(payload)
    unexpected_payload_keys = payload_keys - expected_payload_keys
    if unexpected_payload_keys:
        raise TransactionException("Payload keys are not valid")

    required_payload_keys = ("sender", "contract", "function", "chi_supplied")
    for key in required_payload_keys:
        if key not in payload or payload[key] in (None, ""):
            raise TransactionException(f"Payload key '{key}' is missing")


def check_tx_formatting(tx: dict):
    if (
        isinstance(tx, dict)
        and isinstance(tx.get("metadata"), dict)
        and "tx_format" in tx["metadata"]
    ):
        try:
            hybrid.exact_dict(tx, {"metadata", "payload"})
            hybrid.exact_dict(tx["metadata"], {"tx_format", "auth"})
            if tx["metadata"]["tx_format"] != hybrid.TX_FORMAT:
                raise ValueError("unsupported transaction format")
            rules = dict(TRANSACTION_PAYLOAD_RULES)
            rules["contract"] = lambda value: (
                value == "_accounts" or TRANSACTION_PAYLOAD_RULES["contract"](value)
            )
            check_format(tx["payload"], rules)
            if not isinstance(tx["metadata"]["auth"], dict):
                raise ValueError("invalid authorization")
        except ValueError as exc:
            raise TransactionException(str(exc)) from exc
        return
    check_tx_keys(tx)
    check_format(tx, TRANSACTION_RULES)


def check_contract_name(contract, function, name):
    if (
        contract == "submission"
        and function == "submit_contract"
        and not contract_name_is_formatted(name)
    ):
        raise TransactionException("Transaction contract name is invalid")


def validate_transaction(
    client,
    nonce_storage,
    tx,
    *,
    tx_hash: str,
    chain_id: str,
    fee_policy=None,
):
    from xian.account_registry import AccountRegistry

    validate_transaction_static(
        tx, chain_id=chain_id, account_registry=AccountRegistry(client.raw_driver, chain_id)
    )
    validate_transaction_after_static(
        client,
        nonce_storage,
        tx,
        tx_hash=tx_hash,
        fee_policy=fee_policy,
    )


def validate_transaction_after_static(
    client,
    nonce_storage,
    tx,
    *,
    tx_hash: str,
    fee_policy=None,
):
    if fee_policy is not None:
        fee_policy.validate_tx(tx)
        if not fee_policy.require_chi_balance:
            nonce_storage.check_nonce(tx, tx_hash=tx_hash)
            return

    # Get the senders balance and the current chi rate
    try:
        balance = client.get_var(
            contract="currency",
            variable="balances",
            arguments=[tx["payload"]["sender"]],
            mark=False,
        )
    except Exception as e:
        raise TransactionException(f"Failed to retrieve 'currency' balance for sender: {e}")

    try:
        chi_rate = client.get_var(
            contract="chi_cost", variable="S", arguments=["value"], mark=False
        )
        if chi_rate is None:
            chi_rate = 20
    except Exception as e:
        raise TransactionException(f"Failed to get chi cost: {e}")

    contract = tx["payload"]["contract"]
    func = tx["payload"]["function"]
    chi_supplied = tx["payload"]["chi_supplied"]

    if chi_supplied is None:
        chi_supplied = 0

    if balance is None:
        balance = 0

    # Get how much they are sending
    amount = tx["payload"]["kwargs"].get("amount")
    amount = 0 if amount is None else amount

    if isinstance(amount, dict):
        amount = convert_dict(amount)

    # Check if they have enough chi for the operation
    check_enough_chi(
        balance,
        chi_rate,
        chi_supplied,
        contract=contract,
        function=func,
        amount=amount,
    )

    # Reserve the local mempool nonce only after all admission checks pass.
    nonce_storage.check_nonce(tx, tx_hash=tx_hash)


def decode_and_validate_transaction_static_bytes(
    raw_tx: bytes,
    *,
    chain_id: str,
    max_raw_tx_bytes: int | None = None,
    account_registry=None,
) -> dict:
    if max_raw_tx_bytes is not None and len(raw_tx) > max_raw_tx_bytes:
        raise TransactionException(
            f"Transaction exceeds maximum configured size of {max_raw_tx_bytes} bytes"
        )
    if account_registry is not None:
        tx, _ = decode_transaction_bytes(raw_tx, max_account_wire_bytes=hybrid.MAX_WIRE_BYTES)
        validate_transaction_static(tx, chain_id=chain_id, account_registry=account_registry)
        return tx
    if _native_decode_and_validate_transaction_static is not None:
        try:
            return _native_decode_and_validate_transaction_static(
                raw_tx,
                chain_id,
            )
        except Exception as exc:
            raise TransactionException(str(exc)) from exc

    tx, _ = decode_transaction_bytes(raw_tx)
    validate_transaction_static(tx, chain_id=chain_id)
    return tx


class SequentialNonceTracker:
    """Deterministic per-block/proposal nonce tracker.

    This intentionally does not use node-local mempool pending nonce state.
    It starts from committed nonce state and advances only for transactions
    accepted into the current proposal/block validation pass.
    """

    def __init__(self, committed_nonce_getter: Callable[[str], int | None]):
        self._committed_nonce_getter = committed_nonce_getter
        self._latest_nonces: dict[str, int] = {}

    def expected_nonce(self, sender: str) -> int:
        current_nonce = self._latest_nonces.get(sender)
        if current_nonce is None:
            current_nonce = self._committed_nonce_getter(sender)
        if current_nonce is None:
            return 0
        return current_nonce + 1

    def validate_and_advance(self, tx: dict) -> None:
        sender = tx["payload"]["sender"]
        tx_nonce = tx["payload"]["nonce"]
        expected_nonce = self.expected_nonce(sender)
        if tx_nonce != expected_nonce:
            raise TransactionException(
                f"Transaction nonce is invalid. Expected {expected_nonce}, got {tx_nonce}"
            )
        self._latest_nonces[sender] = tx_nonce


def validate_transaction_static(tx: dict, *, chain_id: str, account_registry=None) -> None:
    check_tx_formatting(tx)

    if "tx_format" in tx["metadata"]:
        if account_registry is None:
            raise TransactionException("Registry transactions require trusted account state")
    else:
        sender, signature, payload = unpack_transaction(tx)
        if not verify(sender, payload, signature):
            raise TransactionException("Bad signature")
    if account_registry is not None:
        account_registry.authorize(tx)

    tx_chain_id = tx["payload"].get("chain_id", "")
    if tx_chain_id != chain_id:
        raise TransactionException("Wrong chain_id")

    name = tx["payload"]["kwargs"].get("name")
    check_contract_name(
        tx["payload"]["contract"],
        tx["payload"]["function"],
        name,
    )


def validate_consensus_transaction(
    tx: dict,
    *,
    chain_id: str,
    nonce_tracker: SequentialNonceTracker,
) -> None:
    validate_transaction_static(tx, chain_id=chain_id)
    validate_consensus_transaction_after_static(
        tx,
        nonce_tracker=nonce_tracker,
    )


def validate_consensus_transaction_after_static(
    tx: dict,
    *,
    nonce_tracker: SequentialNonceTracker,
) -> None:
    nonce_tracker.validate_and_advance(tx)


def dict_has_keys(d: dict, keys: set) -> bool:
    return set(d) == keys


def format_dictionary(d: dict) -> dict:
    return _format_value(d)


def _format_value(value):
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            assert isinstance(key, str), "Non-string key types not allowed."
            items.append((key, _format_value(item)))
        return dict(sorted(items))
    if isinstance(value, list):
        return [_format_value(item) for item in value]
    return value
