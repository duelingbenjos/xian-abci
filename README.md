# xian-abci

`xian-abci` is the CometBFT-facing Xian node runtime. It owns deterministic
chain execution, ABCI request handling, state export and snapshot flows, and
the node-adjacent services (BDS indexing, metrics, the optional dashboard) that
run alongside a Xian validator or full node.

The published PyPI package is `xian-tech-abci`. Console entrypoints
(`xian-abci`, `xian-dashboard`, `xian-configure-node`, and the BDS / state
helpers) are installed by the package and used both directly and from the
operator-facing `xian-cli`.

## Runtime Shape

```mermaid
flowchart LR
  CometBFT["CometBFT"] --> ABCI["ABCI handlers"]
  ABCI --> Executor["Transaction processor"]
  Executor --> Contracting["xian-contracting runtime"]
  Executor --> State["Chain state"]
  State --> BDS["BDS indexer"]
  State --> Snapshots["Export and snapshot tools"]
  ABCI --> Metrics["Metrics"]
  ABCI --> Dashboard["Optional dashboard"]
  CLI["xian-cli and xian-stack"] --> Entrypoints["xian-abci entrypoints"]
```

## Quick Start

Bootstrap the development environment with `uv`:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
```

Inspect the available command surface:

```bash
uv run xian-abci --help
uv run xian-dashboard --help
uv run xian-configure-node --help
uv run xian-export-state --help
uv run xian-state-snapshot --help
uv run xian-bds-reindex --help
uv run xian-bds-snapshot --help
uv run xian-bds-spool --help
uv run xian-parallel-estimator-report --help
```

The node installs the hybrid account cryptography backend, but account enrollment
requires explicit consensus policy and is disabled in default networks. See
[account authorization](docs/ARCHITECTURE.md#account-authorization).

Optional extras:

- `vm` — enables the `xian_vm_v1` bindings.
- `native` — enables native admission helpers for development and release
  validation.
- `zk` — enables the native zk verifier required by nodes on chains with the
  `zk` runtime feature.

For a ready-made local network instead of wiring the node yourself, use
[`xian-stack`](../xian-stack). This repo focuses on the runtime and backend
tooling, not the full localnet UX.

## Principles

- **Deterministic chain execution.** The ABCI handlers, transaction execution,
  rewards, validators, and query behavior live here and must be deterministic
  across nodes.
- **Backend, not UX.** Operator-facing lifecycle commands belong in
  [`xian-cli`](../xian-cli). This repo exposes importable helpers and
  backend-oriented CLIs only.
- **Universal node runtime.** No network-specific genesis files, seeds, or
  snapshots. Committed chain assets live in
  [`xian-configs`](../xian-configs).
- **Contract semantics live elsewhere.** Changes to contract execution
  semantics belong in [`xian-contracting`](../xian-contracting), not here.
- **No Compose / orchestration.** Container lifecycle and local-stack
  orchestration belong in [`xian-stack`](../xian-stack).

Local `uv` development requires a sibling checkout of
`../xian-contracting`. Full validation also uses sibling checkouts of
`../xian-contracts`, `../xian-configs`, and `../xian-stack`.

## Key Directories

- `src/xian/` — repo-owned node runtime: ABCI handlers, services, utilities,
  and CLI entrypoints.
  - `methods/` — ABCI request handlers (CheckTx, FinalizeBlock, Commit, …).
  - `services/` — background services such as the simulator and BDS support.
  - `cli/` — backend and developer entrypoints (configure-node, export-state,
    BDS helpers, …) wrapping importable helpers.
  - `tools/` — one-off upgrade data and state-patch payloads. Treated as a
    transition area; new code should live as importable helpers under
    `src/xian/`.
  - `dashboard/` — the optional operator dashboard.
- `src/abci/` — lower-level ABCI server and protocol glue.
- `protos/` — vendored CometBFT schemas plus supporting protobuf dependencies.
- `build_proto.py` — regenerates checked-in protobuf Python stubs under `src/`.
- `scripts/` — repo-level validation and manual benchmark entrypoints.
- `tests/` — unit, ABCI-method, integration, governance, tools, and system
  coverage.
- `docs/` — architecture notes, API surface, safety invariants, snapshots /
  pruning model, governance, and backlog.

## Validation

Preferred full validation entrypoint, used for releases:

```bash
./scripts/validate-release.sh
```

It defaults to Python `3.14` (override with `XIAN_ABCI_VALIDATE_PYTHON`) and
runs:

- `./scripts/validate-repo.sh`
- protobuf regeneration / stale-stub checks
- the Python-vs-native processor fuzz parity coverage
  (`tests/integration/test_vm_processor_fuzz.py`)

Faster local loops:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/unit/test_node_setup.py
```

CI provisions Postgres for the BDS-backed paths. To mirror that locally, expose
Postgres at `postgres://postgres:1234@localhost:5432/xian`.

## Related Docs

- [AGENTS.md](AGENTS.md) — repo-specific guidance for AI agents and contributors
- [docs/README.md](docs/README.md) — index of internal design notes
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — major components and dependency direction
- [docs/BACKLOG.md](docs/BACKLOG.md) — open work and follow-ups
- [docs/API.md](docs/API.md) — ABCI and helper API surface
- [docs/SAFETY_INVARIANTS.md](docs/SAFETY_INVARIANTS.md) — invariants the runtime must preserve
- [docs/SNAPSHOTS_AND_PRUNING.md](docs/SNAPSHOTS_AND_PRUNING.md) — snapshot, pruning, and BDS retention model
- [docs/CHAIN_ASSETS.md](docs/CHAIN_ASSETS.md) — split between this repo and `xian-configs`
- [docs/TIME_SEMANTICS.md](docs/TIME_SEMANTICS.md) — block-time and timestamp rules
- [docs/Governance.md](docs/Governance.md) — on-chain governance behavior

## Balance-only genesis migration

`xian.genesis_migration` owns exact balance reconciliation and legacy state-row
conversion. Operator commands live in `xian-cli`. The genesis builder validates
input/source/configuration hashes and resulting balances, pins closed enrollment,
and refuses pending mainnet allocations. See the
[migration specification](../xian-meta/docs/SNAPSHOT_GENESIS_MIGRATION.md).
