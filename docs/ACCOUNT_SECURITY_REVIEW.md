# Account authorization security and resource testing

Review date: 2026-10-12. This is a local implementation review and adversarial
test run, not an independent audit or approval to enable enrollment. All shipped
genesis policies remain closed; final snapshot selection and genesis are deferred.

## Findings and fixes

| Finding | Effect | Fix and evidence |
| --- | --- | --- |
| Hybrid wire size checked after canonicalization | A roughly 4 MB malformed transaction incurred recursive copying, sorting and serialization before rejection. | Check the format-specific limit immediately after JSON parsing. The configured outer limit still rejects before hex/JSON decoding. A regression test makes canonicalization fail if called for oversized account transactions. |
| Closed enrollment parsed candidate keys first | Unavailable enrollment performed avoidable curve parsing and payload serialization. | Reject unavailable enrollment first; assert that candidate parsing, serialization and PQ verification are never reached. |
| SDK canonicalization copied every subtree at every depth | Deep/cyclic inputs bypassed the intended early depth guard and repeated traversal work. | Sort only the current object's keys; assert bounded rejection of cyclic/10,000-level input and exactly one visit per nested value. Canonical Unicode and integer-key ordering is preserved. |
| Archived candidate omitted from downgrade protection | An absent/stale RPC record could prompt legacy signing after an uncertain enrollment was archived. The node still rejects old keys after enrollment. | Include retained candidates in SDK downgrade protection. Tests reject old-key signing, then restore hybrid spending when the RPC reports the committed candidate. |
| Wallet lock raced asynchronous session storage/restoration | A delayed session write/read could restore credentials after a lock. | Serialize session saves/clears and fence delayed work with session generations in browser and mobile controllers. Fault tests delay writes and reads across locking, restart, and normal password unlock. Account activation also checks the generation. |
| Legacy export read twice for conversion/provenance | A replaced export file could produce an audit hash for bytes other than those converted. | Convert and hash the same byte buffer; regression replaces the hypothetical second read. |

## Local measurements

Run from `xian-abci` using its development environment:

```bash
.venv/bin/python scripts/benchmark_account_authorization.py \
  --iterations 100 --output /tmp/account-authorization.json
```

The output path must be new. The harness performs no RPC, storage mutation or
genesis generation; test-only enrollment exists in a process-local fixture.
It checks every expected verdict, reports timings separately from allocation
tracing, and records source hashes and dependency/runtime versions.

[Raw before/after evidence](ACCOUNT_AUTHORIZATION_BENCHMARK.json) records macOS
arm64, Python 3.14.8, pqcrypto 0.4.0 and PyNaCl 1.6.2. Each case used 100 measured
attempts after five warmups. Both runs include the other fixes above; the
comparison isolates moving the hybrid wire check ahead of canonicalization.

| Static admission case | Wire bytes | After p95 | Before p95 where relevant |
| --- | ---: | ---: | ---: |
| Legacy transfer | 776 | 0.60 ms | — |
| Hybrid transfer | 14,266 | 2.19 ms | — |
| Valid maximum-size hybrid payload | 275,970 | 6.16 ms | — |
| Enrollment in isolated open-policy fixture | 44,238 | 4.24 ms | — |
| Closed enrollment | 44,238 | 0.16 ms | — |
| Rotation / recovery | 57,592 / 57,594 | 6.00 / 5.32 ms | — |
| Oversized hybrid wire | 1,062,868 | 1.01 ms | 3.72 ms |
| Flat JSON allocation adversary | 4,014,290 | 6.69 ms | 113.79 ms |

For the flat JSON case, peak traced Python allocations across five attempts fell
from 18.68 MB to 11.33 MB. This excludes native allocations. The configured 4 MiB
outer-wire rejection occurs before decoding; identifying a smaller hybrid wire
limit still requires JSON parsing. Deep invalid JSON is rejected before crypto.

These are serial CPU measurements with in-memory registry reads. They exclude
LMDB, consensus, execution, networking and browser/mobile cryptography. They are
not a TPS limit or production sizing result. Hardware load affects timings;
regression tests assert rejection order and call counts, not latency thresholds.

## Adversarial and recovery coverage

- Wrong chain/epoch, short/oversized proofs and bad Ed25519 never invoke ML-DSA.
  A valid Ed25519 component with a bad ML-DSA component is still rejected.
- Payload limits, wire limits, duplicate JSON fields, non-finite JSON numbers
  and excessive nesting reject without account writes.
- A 128-request mixed invalid admission burst against actual ABCI/LMDB preserves
  balances, authority and pending nonce. A valid enrollment and hybrid admission
  succeed immediately afterwards in the disposable open-policy fixture.
- Existing ABCI recovery tests retain crash/commit failure, insufficient funds,
  stale proofs, replay, restart, snapshot import and closed-enrollment spending.
- SDK backup-envelope tests reject malformed salts, IVs, ciphertext, extra fields
  and altered KDF work factors before password derivation.
- Browser/mobile lock-race tests prove interrupted session writes cannot reopen
  a locked wallet and stale session reads cannot restore credentials. The saved
  encrypted wallet remains unlockable with its original password.

## Validation

Validation for this review passed: the node release suite (707 tests; 7 skipped)
and native admission parity (41 tests); a final combined native/VM account run
(69 tests and 11 subtests); SDK validation (144 tests, with 3 optional tests
skipped), followed by the explicit Python/node interoperability run (9 tests);
browser validation (114 unit tests, 6 release checks, 17 Playwright checks),
followed by all 53 controller tests after the final KDF lock-race regression;
mobile type checking and all 90 tests, followed by all 25 controller tests after
the same additional regression. Documentation builds, importer CLI integration,
workspace conventions and whitespace checks passed. Optional SDK compiler tests
are outside this account review; the separately enabled account interoperability
test passed. These counts describe local working sources, not released packages.

## Remaining activation gates

Keep enrollment disabled. Repeat sustained mixed valid/invalid load against the
exact release validator hardware and package/image set, including mempool
rechecks, proposal verification, block execution, peer/RPC admission controls,
CPU/RSS, disk usage and logging. Invalid requests are not charged fees; fee
budgets alone do not provide ingress protection. Legacy transactions also still
use their larger configured wire bound.

Independent protocol/implementation review, browser service-worker interruption,
physical Android/iOS storage/termination/file-provider testing, growing retained
vault measurements, and production fee/block-budget calibration remain open.
Keep the previously documented multi-validator restart/recovery rehearsal as a
release gate; this microbenchmark does not replace it. Final allocation/genesis
and selected-contract/custodian acceptance are separate deferred launch gates.
