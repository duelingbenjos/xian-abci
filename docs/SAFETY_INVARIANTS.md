# Safety Invariants

This file records the runtime properties that must hold before an
`xian-abci` release is considered safe.

## Determinism

- The same transaction sequence must produce the same transaction results,
  events, and committed state under the Python and native execution paths.
- Validator replay must converge on the same app hash and contract state after
  restart, catch-up, and proposal processing.
- Block time, block hash, and chain-id handling must remain deterministic across
  nodes.

## Failure Semantics

- Failed transactions must not persist contract writes or events.
- Replay after failure must leave the same committed state on every node and in
  every execution engine.
- A committed block's height and app hash must become durable in the same LMDB
  transaction as its state writes. `Info` must never advertise metadata for
  state that was not committed, and startup must repair the compatibility JSON
  mirror from the authoritative LMDB commit marker after a crash. Mirror I/O
  failure must be logged but must not invalidate an authoritative LMDB commit.
- Fee-accounting balance writes may still apply on failure when the runtime
  policy intentionally charges failed execution, but they must remain
  deterministic across nodes and runtimes.

## Native Runtime Rollout

- Native-authoritative execution must continue to produce shadow-comparison data
  for mismatch detection.
- Any native/shadow mismatch is release-blocking until explained and accepted.
- Release validation must include a real localnet run that enforces a zero
  mismatch budget.

## Release Gate

Use `./scripts/validate-release.sh` for repo-local release validation and
`xian-stack/scripts/release-safety.sh` for the full cross-repo localnet gate.

The enforcement surface in this repo includes:

- `tests/integration/test_vm_replay_parity.py`
- `tests/integration/test_vm_processor_fuzz.py`
- `tests/unit/test_processor_shadow.py`
- `tests/unit/test_vm_observability.py`
- the ABCI-method integration tests under `tests/abci_methods/`

## Account Registry

- A registry record changes authority, never the sender's state identity.
- Absent records retain legacy access; invalid/present records cannot fall back
  to legacy verification, including when activation policy is missing.
- Enrollment proves existing ownership and possession of both new key pairs.
- Every proof binds the complete payload, including nonce, chain, fee budget,
  operation, and candidate keys; role domains are distinct.
- Proposal and block checks enforce a per-sender barrier after key transitions.
- Registry operations stage no durable writes outside normal Commit. A failed
  processor write restores pending state; failed LMDB commits remain retryable.
- No default genesis or local node option activates hybrid enrollment.
- Reject oversized account wire before canonicalization and closed enrollment
  before candidate key parsing; malformed Ed25519 must not invoke ML-DSA.
- Invalid admission bursts must not reserve nonces or alter funds/authority.
  See [local security and resource testing](ACCOUNT_SECURITY_REVIEW.md) for
  evidence and the remaining production load-test gates.
