# Snapshots And Pruning

`xian-abci` has three separate snapshot workflows, and they solve different
problems:

1. full-home archive restore
2. CometBFT state sync with Xian application snapshots
3. BDS snapshot export/import for indexed Postgres state

## Full-Home Archive Restore

This is the existing operator bootstrap path used by `xian-cli` and
`apply_snapshot_archive(...)`.

It restores a `.tar` or `.tar.gz` archive into the node home by replacing:

- `data/`
- `xian/`

Only archive members under `data/` and `xian/` are restored. Anything else in
the archive (for example `config/priv_validator_key.json`, `config/node_key.json`
or `config/config.toml`) is ignored, and the node keeps its existing
`data/priv_validator_state.json`, so a snapshot can never replace the node's
keys or rewind its signing high-water mark.

This is useful when you already have a prepared node-home archive and want to
bootstrap a node directly from that archive.

Important properties:

- restores full local node state from a file or URL
- does not use CometBFT state sync
- does not require peer-served snapshot chunks
- remote restore should use either an explicit archive SHA256 or a signed
  snapshot manifest validated against trusted signing keys

## CometBFT State Sync Snapshots

Xian also implements the ABCI snapshot lifecycle required for CometBFT state
sync:

- `list_snapshots`
- `offer_snapshot`
- `load_snapshot_chunk`
- `apply_snapshot_chunk`

The snapshot payload is an application-state archive, not a full node-home
archive. It contains:

- `metadata.json`
- `exported_state.json`

`exported_state.json` is the canonical exported Xian state:

- current application hash
- current height
- contract key/value state
- nonce state

The current application hash is a raw 32-byte `state-root-v2` Merkle root over
the canonical Xian consensus key/value state. The root includes contract state
and committed nonce keys such as `__n.<sender>`, and excludes local runtime keys
that are not part of consensus state.

State-root leaves are domain-separated and hash the canonical ABCI JSON
encoding of each value. They are organized in a deterministic Merkle treap keyed
by state key with cryptographic priorities derived from those keys. During
block finalization, Xian updates an in-memory root cache from the block's
pending consensus writes instead of scanning the whole database. The cache is
rebuilt from committed state on startup, genesis, and state-sync import.

On import, Xian rebuilds:

- LMDB state
- nonce keys
- latest block height/hash metadata

Before import, Xian recomputes the state root from `exported_state.json` and
rejects the snapshot if it does not match the advertised application hash.

Imported or exported application snapshots can then be served back to peers
through the CometBFT snapshot lifecycle.

## Operator Tooling

Use the dedicated snapshot CLI to manage application snapshots:

```bash
uv run xian-state-snapshot list
uv run xian-state-snapshot export
uv run xian-state-snapshot export --output-path ./xian-state-snapshot.tar.gz
uv run xian-state-snapshot import --input-path ./xian-state-snapshot.tar.gz
```

Use `configure_node` state-sync settings when the node should consume snapshots
from peers:

- `--statesync-enable`
- `--statesync-rpc-server`
- `--statesync-trust-height`
- `--statesync-trust-hash`
- `--statesync-trust-period`

Peer state sync still depends on CometBFT's trusted height, trusted hash, and
RPC trust-period model. Once that trusted header is accepted, Xian verifies that
the downloaded snapshot contents recompute to the trusted CometBFT `app_hash`.
Operator-distributed snapshots can still add signed manifests for provenance
and transport integrity, but snapshot contents are no longer trusted solely
because a provider served them.

## Current State-Sync Model

The current implementation is intentionally conservative:

- one supported snapshot format: `1`
- manual snapshot export
- chunked peer serving from locally exported or imported snapshots
- strict full-state import
- deterministic full-state Merkle-root verification
- incremental per-block state-root updates
- no automatic periodic snapshot generation yet
- no compact Merkle inclusion proofs yet

This keeps the application state-sync path explicit and auditable.

## BDS Snapshots

BDS snapshots are a separate operator tool for the indexed Postgres state.
They are not used by CometBFT state sync and they do not replace LMDB or full
node-home restore.

Use the dedicated BDS snapshot CLI:

```bash
uv run xian-bds-snapshot export
uv run xian-bds-snapshot export --output-path ./xian-bds-snapshot.tar.gz
uv run xian-bds-snapshot import --input-path ./xian-bds-snapshot.tar.gz
uv run xian-bds-snapshot import --input-path ./xian-bds-snapshot.tar.gz --clear-spool
```

Important properties:

- exports and imports the BDS schema and indexed chain data
- helps bootstrap or recover the local index faster
- validates imported indexed-head metadata against a trusted RPC block source
- can optionally clear the local spool before import
- does not affect consensus or the application snapshot lifecycle

## Pruning

Current Xian pruning is block-history pruning through `retain_height` in
`ResponseCommit`.

What pruning affects:

- local CometBFT history retained for RPC and replay

What pruning does not separately affect:

- the current LMDB application state

Operationally that means:

- pruned nodes still keep the latest Xian application state
- pruned nodes lose older block history over time
- local historical rebuilds become limited to the retained history window

## Snapshot And Pruning Interaction

- state sync lets a node bootstrap from a recent application snapshot
- pruning controls how much historical block data a node keeps afterward
- local reindex or replay workflows only work for heights the node still
  retains, unless an archival RPC source is available

If a network wants fast bootstrap plus minimal local history retention, the
recommended pattern is:

1. bootstrap from a recent application snapshot
2. use CometBFT state sync for the recent trusted height
3. enable pruning if the node is not meant to be archival
