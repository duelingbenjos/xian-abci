# Docs

This folder contains the repo-local design notes, architecture references, and
planning documents for `xian-abci`.

## Start Here

- [`ARCHITECTURE.md`](ARCHITECTURE.md): current repo boundaries and major runtime
  flows
- [`BACKLOG.md`](BACKLOG.md): active follow-up work and maintenance items

## Index

- [`API.md`](API.md): current query-surface map for the runtime
- [`CHAIN_ASSETS.md`](CHAIN_ASSETS.md): ownership policy for committed chain
  assets and fixtures
- [`Governance.md`](Governance.md): governance-specific runtime integration
  notes
- [`LEGACY_MIGRATION.md`](LEGACY_MIGRATION.md): building a genesis from a
  legacy chain state export
- [`SAFETY_INVARIANTS.md`](SAFETY_INVARIANTS.md): release-blocking execution
  and replay properties
- [`SNAPSHOTS_AND_PRUNING.md`](SNAPSHOTS_AND_PRUNING.md): snapshot, restore,
  BDS snapshot, and pruning behavior
- [`TIME_SEMANTICS.md`](TIME_SEMANTICS.md): deterministic time and block-time
  handling

## Notes

- These docs are mostly internal engineering references, not the primary
  operator documentation surface.
- Planning items that are still open should live in `BACKLOG.md`, not in stale
  historical implementation plans.
