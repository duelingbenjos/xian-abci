"""Adversarial input checks use call counts, never machine-specific time limits."""

import copy
from unittest.mock import Mock, patch

import pytest
from nacl.signing import SigningKey
from xian_accounts import hybrid

from xian.account_registry import AccountRegistry, POLICY_KEY, RECORD_PREFIX
from xian.exceptions import TransactionException
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes
from xian.utils.tx import decode_and_validate_transaction_static_bytes


def pair():
    ed = SigningKey.generate()
    public, secret = hybrid.backend().generate_keypair()
    return hybrid.PublicKeys(bytes(ed.verify_key), public), (bytes(ed), secret)


@pytest.fixture
def account():
    active, secret = pair()
    recovery, _ = pair()
    record = hybrid.AccountRecord(0, active, recovery)
    sender = SigningKey.generate().verify_key.encode().hex()
    state = {POLICY_KEY: {"version": 1, "enrollment_open": False, "transition_chi": 100000},
             RECORD_PREFIX + sender: record.to_dict()}
    driver = Mock()
    driver.get.side_effect = lambda key, **kw: state.get(key)
    payload = {"sender": sender, "chain_id": "local-limits", "nonce": 0,
               "chi_supplied": 100000, "contract": "currency", "function": "transfer",
               "kwargs": {"to": "receiver", "amount": 1}}
    signature = hybrid.sign(active, 0, canonical_json_text(payload).encode(),
                            ed_seed=secret[0], pq_secret=secret[1]).hex()
    tx = {"payload": payload, "metadata": {"tx_format": hybrid.TX_FORMAT,
          "auth": {"scheme": hybrid.SCHEME, "signature": signature}}}
    return AccountRegistry(driver, "local-limits"), tx, state


def test_closed_enrollment_does_not_parse_candidate_or_serialize_payload(account):
    registry, tx, state = account
    state.pop(RECORD_PREFIX + tx["payload"]["sender"])
    tx["payload"].update(contract="_accounts", function="enroll", kwargs={"record": {}})
    tx["metadata"]["auth"].update(active_proof="", recovery_proof="")
    with patch.object(hybrid.AccountRecord, "from_dict", side_effect=AssertionError("curve parse")), \
         patch("xian.account_registry.canonical_json_text", side_effect=AssertionError("serialize")), \
         patch.object(hybrid, "backend", side_effect=AssertionError("PQ")):
        with pytest.raises(TransactionException, match="enrollment is unavailable"):
            registry.authorize(tx)


@pytest.mark.parametrize("mutation", ["wrong_chain", "wrong_epoch", "bad_ed", "short_signature", "oversized_signature"])
def test_cheap_rejections_never_invoke_pq(account, mutation):
    registry, tx, state = account
    before = copy.deepcopy(state)
    auth = tx["metadata"]["auth"]
    if mutation == "wrong_chain":
        tx["payload"]["chain_id"] = "other"
    elif mutation == "short_signature":
        auth["signature"] = "00"
    elif mutation == "oversized_signature":
        auth["signature"] = "00" * (hybrid.AUTH_BYTES + 1)
    else:
        sig = bytearray.fromhex(auth["signature"])
        sig[hybrid.HEADER_BYTES if mutation == "bad_ed" else hybrid.HEADER_BYTES - 1] ^= 1
        auth["signature"] = sig.hex()
    with patch.object(hybrid, "backend", side_effect=AssertionError("PQ must not run")):
        with pytest.raises(TransactionException):
            registry.authorize(tx)
    assert state == before
    registry.driver.set.assert_not_called()


def test_valid_ed_bad_pq_is_rejected_after_one_pq_verification(account):
    registry, tx, _ = account
    auth = tx["metadata"]["auth"]
    sig = bytearray.fromhex(auth["signature"])
    sig[-1] ^= 1
    auth["signature"] = sig.hex()
    backend = hybrid.backend()
    with patch.object(backend, "verify", wraps=backend.verify) as verify:
        with pytest.raises(TransactionException):
            registry.authorize(tx)
        assert verify.call_count == 1


def test_payload_and_wire_limits_precede_pq(account):
    registry, tx, _ = account
    tx["payload"]["kwargs"]["padding"] = "a" * hybrid.MAX_PAYLOAD_BYTES
    with patch.object(hybrid, "backend", side_effect=AssertionError("PQ must not run")):
        with pytest.raises(TransactionException, match="payload exceeds"):
            registry.authorize(tx)
    tx["metadata"]["padding"] = "a" * hybrid.MAX_WIRE_BYTES
    raw = encode_transaction_bytes(canonical_json_text(tx))
    with patch.object(hybrid, "backend", side_effect=AssertionError("PQ must not run")), \
         patch("xian.utils.encoding.canonical_json_text", side_effect=AssertionError("canonicalization")):
        with pytest.raises(TransactionException, match="wire size"):
            decode_and_validate_transaction_static_bytes(raw, chain_id=registry.chain_id, account_registry=registry)
    with patch("xian.utils.tx.decode_transaction_bytes", side_effect=AssertionError("decode")):
        with pytest.raises(TransactionException, match="maximum configured size"):
            decode_and_validate_transaction_static_bytes(raw, chain_id=registry.chain_id,
                max_raw_tx_bytes=hybrid.MAX_WIRE_BYTES, account_registry=registry)


def test_deep_and_noncanonical_wire_rejects_before_crypto(account):
    registry, tx, _ = account
    canonical = canonical_json_text(tx)
    invalid = ["[" * 10000 + "]" * 10000,
               canonical.replace('"nonce":0', '"nonce":0,"nonce":0'),
               canonical.replace('"nonce":0', '"nonce":NaN')]
    with patch.object(hybrid, "backend", side_effect=AssertionError("PQ must not run")):
        for text in invalid:
            with pytest.raises((ValueError, RecursionError, TransactionException)):
                decode_and_validate_transaction_static_bytes(encode_transaction_bytes(text),
                    chain_id=registry.chain_id, account_registry=registry)
