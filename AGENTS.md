# Repository Guidelines

## Scope
- `xian-abci` owns deterministic node behavior, CometBFT-facing ABCI methods, config/genesis primitives, and state handling.
- This repo is not the operator UX surface. New node lifecycle commands belong in `xian-cli`.
- Long term, this repo should be a universal Xian node runtime, not a home for network-specific genesis data.

## Shared Convention
- Follow the shared repo convention in `xian-meta/docs/REPO_CONVENTIONS.md`.
- Keep this repo aligned with that standard for `README.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/BACKLOG.md`, and folder-level `README.md` files at major architectural boundaries.
- Follow the shared change workflow in `xian-meta/docs/CHANGE_WORKFLOW.md`.
- Before push, review whether `xian-docs-web` needs updates and run the local validation path from this file.

## Project Layout
- `src/xian/methods/`: ABCI request handlers.
- `src/xian/services/`: background services such as simulator and BDS support.
- `src/xian/cli/`: backend and developer entrypoints that wrap importable
  helpers without living in script-only `tools/`.
- `src/xian/node_setup.py`: reusable CometBFT home and config helpers.
- `src/xian/node_admin.py`: importable helpers for configuring an initialized
  CometBFT home and applying snapshots.
- `src/xian/genesis_builder.py`: importable genesis-building helpers.
- `src/xian/state_export.py`: importable state-export helpers.
- `src/xian/legacy_genesis.py`, `src/xian/legacy_source.py`: genesis from a
  legacy (pre-VM) chain export; see `docs/LEGACY_MIGRATION.md`.
- `src/xian/tools/`: state-patch and one-off upgrade data, not the main command
  surface.
- `tests/`: unit, integration, system, governance, tools, and ABCI-method coverage.

## Change Routing
- Prefer extracting importable helpers from `src/xian/tools/` instead of adding more script-only logic.
- Keep backend entrypoints in `src/xian/cli/` if they still need a command
  surface. Do not route new behavior through `src/xian/tools/`.
- Do not add network-specific genesis files, seeds, or snapshots here. Those
  assets belong in the sibling `xian-configs` repo.
- Universal bootstrap helpers are allowed here. If `xian-cli` needs local
  network creation support, add it as importable logic under `src/xian/`
  rather than as committed chain data. Local-network helpers may support one or
  more initial validators; keep that logic data-driven.
- Treat `xian-configs` as the source of truth for committed chain assets and
  fixtures. See `docs/CHAIN_ASSETS.md`.
- Changes to contract execution semantics usually belong in `xian-contracting`, not here.
- Container lifecycle or Compose changes belong in `xian-stack`, even when they are needed to run this repo.

## Validation
- Bootstrap: `UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev`
- Preferred full validation path: `./scripts/validate-repo.sh`
- Lint: `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`
- Format check: `UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .`
- Targeted node-setup tests: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/unit/test_node_setup.py`
- Full suite: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest`
- Container/runtime smoke path, when relevant: `make init`, `make up`, `make down`

## Notes
- Local `uv` development depends on a sibling checkout of `../xian-contracting`.
- If you touch genesis or config rendering, keep `xian-cli` integration in mind and avoid baking in network-specific assumptions.
- Treat `src/xian/tools/` as a transition area. Prefer new importable code under `src/xian/` when possible.

## Shared Agent Practices
- Keep changes clean, modular, and professional. Prefer small, cohesive modules, clear naming, explicit boundaries, and tests over quick patches.
- When code behavior, public APIs, user workflows, operator workflows, or configuration semantics change, check whether `../xian-docs-web` needs corresponding documentation updates. If this repo is `xian-docs-web`, update the relevant published docs in place. Write durable user/developer documentation, not a changelog entry.
- Follow `../xian-meta/docs/CODE_GRAPH_WORKFLOW.md` for graph freshness and source verification. Before relying on graph results, run `python3 ../xian-meta/scripts/graphify_workspace.py status --repo xian-abci` and refresh that repo if stale.
- For codebase questions, use the local graph first when `graphify-out/graph.json` exists: run `graphify query "<question>"`; use `graphify affected "<symbol>" --depth 1` for direct dependents, larger depths for transitive impact, and `graphify path "<A>" "<B>" --directed` for call paths and `graphify explain "<concept>"` for focused concepts.
- Dirty `graphify-out/` files are expected after hooks or incremental updates and are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- Use `graphify-out/wiki/index.md` for broad navigation when it exists. Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or when query/path/explain do not surface enough context.
- For any non-trivial code change, update the local graph before final verification when `graphify-out/graph.json` exists. Run `python3 ../xian-meta/scripts/graphify_workspace.py refresh --repo xian-abci` from the repo root; use `--force` only for inspected, intentional shrinkage.
- After updating the graph, check cross-repo impact before finishing: use `graphify affected`, inspect returned source locations, and search affected sibling repos. A missing edge or truncated result does not prove there are no other consumers.
- If graphify or dependency analysis shows affected sibling repos, update those repos in the same change when the impact is real and the fix is in scope.
- Treat `graphify-out/` as a generated local artifact. Do not commit it.
