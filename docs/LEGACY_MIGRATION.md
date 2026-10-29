# Legacy Chain Migration

`xian-legacy-genesis` builds a `genesis.json` for this runtime from the state of
a legacy (pre-VM, CPython-engine) Xian chain. The legacy node's
`xian/tools/export_state.py` writes the input, `exported_state.json`: every
contract key/value pair (`genesis`) plus every sender nonce (`nonces`).

```bash
uv run xian-legacy-genesis \
  --export exported_state.json \
  --export-sha256 <sha256 of the export, published by whoever took it> \
  --chain-id xian-2 \
  --genesis-time 2026-11-01T00:00:00Z \
  --network mainnet \
  --output genesis.json \
  --sources-dir migrated-sources/
```

The command writes the genesis, a `genesis.report.json` covering every
decision, and (optionally) the deployed source of every migrated contract for
review. It exits non-zero without writing anything if any step fails.

## Reproducibility

Every input is pinned: the export (by SHA-256), the network bundle, the genesis
time (which also becomes every genesis contract's submission time), and the
options. Two builds from the same inputs produce byte-identical files. Each
validator should rebuild the genesis independently and compare the
`app_hash` before launch.

## What Happens To Each Part Of The State

### User contracts

The legacy engine stored a transformed module under `<name>.__code__`, not the
authored source. `xian.legacy_source.recover_legacy_source` inverts that
transform exactly (export decorators, the `____` constructor, `__`-prefixed
private functions and ORM objects, injected `contract=`/`name=` arguments). The
recovered source is compiled with the normal linter into `xian_vm_v1`
artifacts and installed with the legacy `__owner__`, `__developer__` and
`__submitted__` metadata. Constructors are not re-run; legacy state is copied
verbatim. Validation against the legacy `xian-1` export: re-running the legacy
compiler on the recovered source reproduces the stored code exactly for 341 of
345 contracts. The other 4 are the name collisions described below.

Two situations need an explicit, reported adjustment:

- **Compatibility rewrite `fstring_string_literals`.** The current parser
  rejects string literals inside f-string replacement fields
  (`f"{data[k, 'x']}"`, `f"{s.strip('a')}"`). Such f-strings are rewritten
  to `'text ' + str(expr) + ...`, which evaluates identically for fields
  without conversions or format specs. Disable with `--no-compat-rewrites`.
- **Name collisions.** Legacy privatization let a private function or ORM
  object share a name with a parameter or exported function. Restoring the
  name would rebind references, so the privatized binding gets a fresh name
  instead (`<name>_private` for functions, `<name>_store` for ORM objects,
  whose storage keys move with it). Disable with `--no-collision-renames`.

A contract that still does not compile stops the build. Fix it with
`--source-override NAME=PATH` or drop it, together with its state, using
`--skip-contract NAME`.

Format specifications in f-strings (`f"{x:02d}"`, `f"{v:.2f}"`) are not
rewritten: the compiler rejects them by design and there is no exact
equivalent. On `xian-1` seven contracts use them (`con_multi_psv1`,
`con_price_feed_102025`, `con_ps000`-`con_ps003`, `con_test_pv`) and need an
override or an explicit skip.

### System contracts

The target network bundle's system contracts are deployed first, exactly as on
a fresh network. Legacy state is then layered on top:

| Legacy contract | Treatment |
| --- | --- |
| `currency` | Bundle code. The legacy ledger replaces the bundle allocation; allowances move from `balances:<owner>:<spender>` to `approvals:<owner>:<spender>`; `metadata:total_supply` is recomputed. Other legacy keys (e.g. `streams`) are copied as inert state. Total supply must match the legacy ledger exactly or the build fails. |
| `foundation` | Bundle code; owner taken from the legacy `foundation.owner`. |
| `dao`, `rewards` | Bundle code; legacy keys fill gaps, and the bundle value wins on conflicts (reported). |
| `masternodes` | Retired. Registration holdings are refunded from the `masternodes` currency balance to their depositors (`--keep-masternode-holdings` to skip). `masternodes.nodes` becomes the default genesis validator set. |
| `stamp_cost`, `members`, `submission` | Retired; replaced by `chi_cost`, `validators` and the built-in submission contract. |

### Nonces

Each legacy nonce becomes `__n.<sender>:` with the same value. Both runtimes
store the last *used* nonce, so the next valid nonce is unchanged. This also
blocks replay of any pre-migration transaction. One behaviour changes: the
legacy chain accepted any nonce greater than the last, while this runtime
requires exactly the next one. Wallets must query the next nonce rather than
skipping ahead.

### Heights

By default the new chain continues the legacy numbering:
`abci_genesis.number` is the export height and CometBFT `initial_height` is
that height plus one. Use `--initial-height` to choose otherwise. The genesis
records the legacy app hash, height and export SHA-256 under
`abci_genesis.migration`.

## Verification

Before writing output, the tool replays the genesis the way `InitChain` does
(after a JSON round trip) and checks the state root, every nonce, every user
contract's artifacts and state values, and every balance and allowance.

## Follow-Ups The Report Flags

- **Streams.** The current `currency` has no stream functions. Active legacy
  streams and the balances backing them (e.g. `dao_funding_stream`) are
  preserved but frozen until governance migrates them.
- **Allowance readers.** Contracts that read `currency.balances` through
  `ForeignHash` no longer see allowances there.
- **Validator keys.** The default validator set is the legacy
  `masternodes.nodes` list, with equal power. Pass `--validator PUBKEY[:POWER]`
  to choose explicitly. Every validator needs its consensus key in place
  before launch.
- **Genesis size.** Contract IR makes the genesis large (about 62 MB for
  `xian-1`). CometBFT's `/genesis` RPC refuses files over 16 MB, so clients
  must use `/genesis_chunked`.
