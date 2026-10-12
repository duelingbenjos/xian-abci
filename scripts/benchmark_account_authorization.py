"""Local CPU/allocation benchmark; no RPC, storage, signing keys or genesis output.

Run with the node development environment. This measures static admission, not
block execution or network capacity. Keys and the open enrollment policy exist
only in this process. Timing assertions do not belong in consensus tests.
"""

import argparse
import copy
import hashlib
import importlib.metadata
import json
import platform
import statistics
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from nacl.signing import SigningKey
from xian_accounts import hybrid

from xian.account_registry import POLICY_KEY, RECORD_PREFIX, AccountRegistry
from xian.exceptions import TransactionException
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes
from xian.utils.tx import decode_and_validate_transaction_static_bytes


class FixtureDriver:
    def __init__(self, state):
        self.state = state

    def get(self, key, *, save=False):
        return self.state.get(key)


def pair():
    ed = SigningKey.generate()
    public, secret = hybrid.backend().generate_keypair()
    return hybrid.PublicKeys(bytes(ed.verify_key), public), (bytes(ed), secret)


def new_record(epoch):
    active, a = pair()
    recovery, r = pair()
    return hybrid.AccountRecord(epoch, active, recovery), a, r


def proof(keys, epoch, payload, secret, role="transaction"):
    return hybrid.sign(
        keys,
        epoch,
        canonical_json_text(payload).encode(),
        ed_seed=secret[0],
        pq_secret=secret[1],
        role=role,
    ).hex()


def fixtures():
    old = SigningKey.generate()
    sender = old.verify_key.encode().hex()
    current, active, recovery = new_record(0)
    next_record, next_active, next_recovery = new_record(1)
    policy = {"version": 1, "enrollment_open": False, "transition_chi": 100000}
    enrolled = AccountRegistry(
        FixtureDriver({POLICY_KEY: policy, RECORD_PREFIX + sender: current.to_dict()}),
        "local-benchmark",
    )
    closed = AccountRegistry(FixtureDriver({POLICY_KEY: policy}), "local-benchmark")
    opened = AccountRegistry(
        FixtureDriver({POLICY_KEY: {**policy, "enrollment_open": True}}), "local-benchmark"
    )
    payload = {
        "sender": sender,
        "chain_id": "local-benchmark",
        "nonce": 0,
        "chi_supplied": 100000,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"to": "receiver", "amount": 1},
    }
    legacy = {
        "payload": payload,
        "metadata": {"signature": old.sign(canonical_json_text(payload).encode()).signature.hex()},
    }
    spend = {
        "payload": payload,
        "metadata": {
            "tx_format": hybrid.TX_FORMAT,
            "auth": {
                "scheme": hybrid.SCHEME,
                "signature": proof(current.active, 0, payload, active),
            },
        },
    }
    cases = [("valid_legacy", closed, legacy, True), ("valid_hybrid", enrolled, spend, True)]
    large = copy.deepcopy(spend)
    large["payload"]["kwargs"]["padding"] = ""
    large["payload"]["kwargs"]["padding"] = "x" * (
        hybrid.MAX_PAYLOAD_BYTES - len(canonical_json_text(large["payload"]).encode())
    )
    large["metadata"]["auth"]["signature"] = proof(current.active, 0, large["payload"], active)
    cases.append(("valid_max_payload", enrolled, large, True))
    for operation in ("enroll", "rotate", "recover"):
        candidate, a, r = (
            (current, active, recovery)
            if operation == "enroll"
            else (next_record, next_active, next_recovery)
        )
        p = {
            **payload,
            "contract": "_accounts",
            "function": operation,
            "kwargs": {"record": candidate.to_dict()},
        }
        if operation == "enroll":
            signature = old.sign(
                hybrid.enrollment_transcript(canonical_json_text(p).encode())
            ).signature.hex()
        else:
            authority, secret = (
                (current.active, active) if operation == "rotate" else (current.recovery, recovery)
            )
            signature = proof(authority, 0, p, secret, "authority")
        tx = {
            "payload": p,
            "metadata": {
                "tx_format": hybrid.TX_FORMAT,
                "auth": {
                    "scheme": hybrid.ENROLL_SCHEME if operation == "enroll" else hybrid.SCHEME,
                    "signature": signature,
                    "active_proof": proof(candidate.active, candidate.epoch, p, a, "new_active"),
                    "recovery_proof": proof(
                        candidate.recovery, candidate.epoch, p, r, "new_recovery"
                    ),
                },
            },
        }
        cases.append(
            ("valid_" + operation, opened if operation == "enroll" else enrolled, tx, True)
        )
        if operation == "enroll":
            cases.append(("closed_enrollment", closed, tx, False))
    for label, offset in (
        ("invalid_ed25519", hybrid.HEADER_BYTES),
        ("invalid_ml_dsa", hybrid.AUTH_BYTES - 1),
    ):
        tx = copy.deepcopy(spend)
        sig = bytearray.fromhex(tx["metadata"]["auth"]["signature"])
        sig[offset] ^= 1
        tx["metadata"]["auth"]["signature"] = sig.hex()
        cases.append((label, enrolled, tx, False))
    for label, size in (
        ("payload_limit", hybrid.MAX_PAYLOAD_BYTES),
        ("wire_limit", hybrid.MAX_WIRE_BYTES),
    ):
        tx = copy.deepcopy(spend)
        tx["payload"]["kwargs"]["padding"] = "x" * size
        cases.append((label, enrolled, tx, False))
    result = [
        (label, registry, encode_transaction_bytes(canonical_json_text(tx)), valid)
        for label, registry, tx, valid in cases
    ]
    result.append(("configured_wire_limit", closed, b"a" * (4 * 1024 * 1024 + 1), False))
    result.append(("deep_json", closed, encode_transaction_bytes("[" * 10000 + "]" * 10000), False))
    # Near-default-wire-limit allocation adversary, without expensive crypto.
    flat = copy.deepcopy(spend)
    flat["payload"]["kwargs"]["padding"] = [None] * 400000
    result.append(
        (
            "flat_json_allocation",
            enrolled,
            encode_transaction_bytes(canonical_json_text(flat)),
            False,
        )
    )
    return result


def measure(registry, raw, expected, iterations):
    def attempt():
        try:
            decode_and_validate_transaction_static_bytes(
                raw,
                chain_id=registry.chain_id,
                max_raw_tx_bytes=4 * 1024 * 1024,
                account_registry=registry,
            )
            accepted = True
        except (ValueError, RecursionError, TransactionException):
            accepted = False
        if accepted != expected:
            raise AssertionError("unexpected admission verdict")

    for _ in range(5):
        attempt()
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        attempt()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    # Allocation measurements are separate so tracing does not distort timings.
    tracemalloc.start()
    for _ in range(5):
        attempt()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    samples.sort()
    return {
        "accepted": expected,
        "wire_bytes": len(raw),
        "iterations": iterations,
        "p50_ms": statistics.median(samples),
        "p95_ms": samples[(95 * iterations - 1) // 100],
        "max_ms": max(samples),
        "mean_ms": statistics.mean(samples),
        "serial_attempts_per_second": 1000 / statistics.mean(samples),
        "peak_traced_python_bytes_5_attempts": peak,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 20 <= args.iterations <= 10000:
        parser.error("iterations must be between 20 and 10000")
    if args.output.exists():
        parser.error("output must not already exist")
    root = Path(__file__).resolve().parents[1]
    paths = [
        Path(__file__),
        root / "src/xian/account_registry.py",
        root / "src/xian/utils/tx.py",
        root / "src/xian/utils/encoding.py",
        Path(hybrid.__file__),
    ]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "dependencies": {p: importlib.metadata.version(p) for p in ("pqcrypto", "PyNaCl")},
        "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "scope": "static admission with in-memory read-only state; excludes consensus, LMDB, execution and RPC",
        "limitations": "serial CPU benchmark; traced memory excludes native allocations; no production capacity claim",
        "launch_approved": False,
        "cases": {
            name: measure(registry, raw, valid, args.iterations)
            for name, registry, raw, valid in fixtures()
        },
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    for name, result in report["cases"].items():
        print(
            f"{name}: p50={result['p50_ms']:.3f}ms p95={result['p95_ms']:.3f}ms wire={result['wire_bytes']}"
        )


if __name__ == "__main__":
    main()
