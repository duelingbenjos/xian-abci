"""Admission parity between the Python validator and optional Rust fastpath."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import nacl.encoding
import nacl.signing
import pytest

import xian.utils.encoding as encoding
import xian.utils.tx as tx_utils
from xian.utils.encoding import encode_transaction_bytes
from xian.utils.tx import format_dictionary

xian_fastpath_core = pytest.importorskip("xian_fastpath_core")
_native_decode_static = xian_fastpath_core.decode_and_validate_transaction_static
_native_extract_payload = xian_fastpath_core.extract_payload_string

CHAIN_ID = "xian-local"
SEED = bytes(range(32))
SIGNING_KEY = nacl.signing.SigningKey(SEED)
SENDER = SIGNING_KEY.verify_key.encode(encoder=nacl.encoding.HexEncoder).decode("ascii")

SUBMISSION_NAMES = json.loads(
    (Path(__file__).parents[1] / "fixtures/submission_names.json").read_text()
)


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    value: dict | None = None
    error: str | None = None


def _canonical_json(value: dict) -> str:
    return tx_utils.canonical_json(format_dictionary(deepcopy(value)))


def _signed_tx_bytes(
    *,
    payload_overrides: dict | None = None,
    kwargs: dict | None = None,
    wire_json_dumps: bool = False,
    mutate_signature: bool = False,
) -> bytes:
    payload = {
        "chain_id": CHAIN_ID,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": 1, "memo": "ascii memo", "to": SENDER},
        "nonce": 40,
        "sender": SENDER,
        "chi_supplied": 10,
    }
    if kwargs is not None:
        payload["kwargs"] = kwargs
    if payload_overrides is not None:
        payload.update(payload_overrides)

    signature = SIGNING_KEY.sign(_canonical_json(payload).encode("utf-8")).signature.hex()
    if mutate_signature:
        signature = ("00" * 64)[: len(signature)]

    tx = {"metadata": {"signature": signature}, "payload": payload}
    tx_json = json.dumps(tx) if wire_json_dumps else _canonical_json(tx)
    return encode_transaction_bytes(tx_json)


def _duplicate_payload_tx_bytes() -> bytes:
    tx_json = (
        '{"payload":{"chain_id":"xian-local","contract":"currency",'
        '"function":"transfer","kwargs":{"amount":999,"to":"'
        + SENDER
        + '"},"nonce":40,"sender":"'
        + SENDER
        + '","chi_supplied":10},"metadata":{"signature":"deadbeef"},'
        '"payload":{"chain_id":"xian-local","contract":"currency",'
        '"function":"transfer","kwargs":{"amount":1,"to":"'
        + SENDER
        + '"},"nonce":40,"sender":"'
        + SENDER
        + '","chi_supplied":10}}'
    )
    return encode_transaction_bytes(tx_json)


def _nested_kwargs(depth: int) -> dict:
    value: object = "leaf"
    for _ in range(depth):
        value = {"next": value}
    return {"nested": value, "to": SENDER}


@contextmanager
def _validation_mode(*, native: bool):
    original_decode_static = tx_utils._native_decode_and_validate_transaction_static
    original_extract_payload = encoding._native_extract_payload_string
    tx_utils._native_decode_and_validate_transaction_static = (
        _native_decode_static if native else None
    )
    encoding._native_extract_payload_string = _native_extract_payload if native else None
    try:
        yield
    finally:
        tx_utils._native_decode_and_validate_transaction_static = original_decode_static
        encoding._native_extract_payload_string = original_extract_payload


def _validate(raw_tx: bytes, *, native: bool) -> ValidationResult:
    with _validation_mode(native=native):
        try:
            value = tx_utils.decode_and_validate_transaction_static_bytes(
                raw_tx,
                chain_id=CHAIN_ID,
            )
        except Exception as exc:
            return ValidationResult(accepted=False, error=str(exc))
    return ValidationResult(accepted=True, value=value)


@pytest.mark.parametrize("case", SUBMISSION_NAMES, ids=lambda case: str(case["name"]))
def test_submission_name_parity(case):
    raw_tx = _signed_tx_bytes(
        payload_overrides={"contract": "submission", "function": "submit_contract"},
        kwargs={"name": case["name"], "code": "@export\ndef value():\n    return 1"},
    )
    python_result = _validate(raw_tx, native=False)
    native_result = _validate(raw_tx, native=True)
    assert python_result.accepted == case["accepted"]
    assert native_result == python_result


@pytest.mark.parametrize(
    ("name", "raw_tx", "expected_accepted"),
    [
        ("canonical_int_transfer", _signed_tx_bytes(), True),
        (
            "default_json_wire_spacing",
            _signed_tx_bytes(wire_json_dumps=True),
            False,
        ),
        (
            "nested_ascii_kwargs",
            _signed_tx_bytes(
                kwargs={
                    "amount": 1,
                    "nested": {
                        "items": [{"label": "one"}, {"label": "two"}],
                        "text": "ascii only",
                    },
                    "to": SENDER,
                }
            ),
            True,
        ),
        (
            "invalid_signature",
            _signed_tx_bytes(mutate_signature=True),
            False,
        ),
        (
            "float_kwargs_are_rejected",
            _signed_tx_bytes(kwargs={"amount": 0.00000252, "to": SENDER}),
            False,
        ),
        (
            "unicode_kwargs_use_clean_canonical_json",
            _signed_tx_bytes(kwargs={"memo": "snowman: \u2603", "to": SENDER}),
            True,
        ),
        (
            "trailing_backslash_payload_string",
            _signed_tx_bytes(kwargs={"memo": "ends with \\", "to": SENDER}),
            True,
        ),
        (
            "boolean_kwargs_are_allowed",
            _signed_tx_bytes(kwargs={"flag": True, "to": SENDER}),
            True,
        ),
        (
            "float_matrix_is_rejected",
            _signed_tx_bytes(
                kwargs={
                    "emoji": "\U0001f600",
                    "large": 1e20,
                    "line": "first\nsecond",
                    "plain": 1000000.0,
                    "small": 1e-7,
                    "to": SENDER,
                }
            ),
            False,
        ),
        (
            "max_u64_nonce_is_accepted",
            _signed_tx_bytes(payload_overrides={"nonce": 2**64 - 1}),
            True,
        ),
        (
            "overflowing_nonce_is_rejected",
            _signed_tx_bytes(payload_overrides={"nonce": 2**64}),
            False,
        ),
        (
            "max_u64_kwargs_integer_is_accepted",
            _signed_tx_bytes(kwargs={"amount": 2**64 - 1, "to": SENDER}),
            True,
        ),
        (
            "overflowing_kwargs_integer_is_rejected",
            _signed_tx_bytes(kwargs={"amount": 2**64, "to": SENDER}),
            False,
        ),
        (
            "runtime_big_int_wrapper_is_accepted",
            _signed_tx_bytes(
                kwargs={
                    "amount": {"__big_int__": str(2**64)},
                    "to": SENDER,
                }
            ),
            True,
        ),
        (
            "runtime_fixed_wrapper_is_accepted",
            _signed_tx_bytes(kwargs={"amount": {"__fixed__": "0.5"}, "to": SENDER}),
            True,
        ),
        (
            "non_object_kwargs_are_rejected",
            _signed_tx_bytes(kwargs=[]),
            False,
        ),
        (
            "boolean_nonce_is_rejected",
            _signed_tx_bytes(payload_overrides={"nonce": True}),
            False,
        ),
        (
            "boolean_chi_supplied_is_rejected",
            _signed_tx_bytes(payload_overrides={"chi_supplied": True}),
            False,
        ),
        (
            "wrong_chain_id",
            _signed_tx_bytes(payload_overrides={"chain_id": "wrong-chain"}),
            False,
        ),
        (
            "excessive_json_depth",
            _signed_tx_bytes(kwargs=_nested_kwargs(129)),
            False,
        ),
        ("duplicate_payload_wire_format", _duplicate_payload_tx_bytes(), False),
    ],
)
def test_native_fastpath_matches_python_static_validation(
    name,
    raw_tx,
    expected_accepted,
):
    python_result = _validate(raw_tx, native=False)
    native_result = _validate(raw_tx, native=True)

    assert python_result.accepted == expected_accepted, name
    assert native_result.accepted == python_result.accepted, name
    if python_result.accepted:
        assert native_result.value == python_result.value, name
    else:
        assert native_result.error == python_result.error, name


@pytest.mark.parametrize(
    "public_key",
    [
        "01" + "00" * 31,  # identity
        "00" * 32,  # order four
        "ec" + "ff" * 30 + "7f",  # order two
        "ee" + "ff" * 30 + "7f",  # non-canonical identity
    ],
)
def test_rejects_low_order_signature_forgery(public_key):
    tx = json.loads(bytes.fromhex(_signed_tx_bytes().decode()).decode())
    tx["payload"]["sender"] = public_key
    tx["metadata"]["signature"] = "01" + "00" * 63
    raw = encode_transaction_bytes(_canonical_json(tx))
    python_result = _validate(raw, native=False)
    native_result = _validate(raw, native=True)
    assert not python_result.accepted
    assert native_result == python_result


def test_rejects_noncanonical_signature_scalar():
    tx = json.loads(bytes.fromhex(_signed_tx_bytes().decode()).decode())
    signature = bytes.fromhex(tx["metadata"]["signature"])
    order = 2**252 + 27742317777372353535851937790883648493
    scalar = int.from_bytes(signature[32:], "little") + order
    tx["metadata"]["signature"] = (signature[:32] + scalar.to_bytes(32, "little")).hex()
    raw = encode_transaction_bytes(_canonical_json(tx))
    python_result = _validate(raw, native=False)
    assert not python_result.accepted
    assert _validate(raw, native=True) == python_result
