# Scripts

## Purpose

This folder contains the deliberately small script surface that sits outside the
importable runtime code.

## Contents

- `validate-release.sh`: release-grade validation wrapper for the repo
- `validate-repo.sh`: the preferred full validation entrypoint for local and CI
  use
- `validate-native-fastpath.sh`: focused validation for the native admission
  fast path
- `validate-sdk-transaction-parity.sh`: cross-checks SDK-built transactions
  against runtime admission
- `benchmark_shielded_chi.py`: manual shielded-fee benchmark harness for the
  shielded note-token path
- `benchmark_state_root.py`: manual state-root performance benchmark
- `benchmark_account_authorization.py`: offline hybrid/legacy admission and
  malformed-input CPU/allocation benchmark; see `docs/ACCOUNT_SECURITY_REVIEW.md`

## Notes

- Keep this folder deliberately small.
- Prefer importable helpers in `src/xian/` for reusable logic, and keep scripts
  here only when they are truly repo-level maintenance entrypoints.
- Protobuf generation does not live here. Use the repo-root `build_proto.py`
  entrypoint instead.
- The `benchmark_*.py` scripts are manual benchmarks, not part of the default
  CI or `make validate` path; `benchmark_shielded_chi.py` expects sibling
  `xian-contracting` and `xian-contracts` checkouts.
