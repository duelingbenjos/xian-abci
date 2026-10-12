# xian

## Purpose

This package contains the main Xian node runtime and the backend-oriented
subsystems around it.

## Contents

- `xian_abci.py`: runtime assembly, startup wiring, and ABCI request dispatch
- `methods/`: request handlers for `CheckTx`, `FinalizeBlock`, query, proposal
  validation, and related ABCI methods
- `processor.py`, `execution_engine.py`, `execution_policy.py`,
  `parallel_executor.py`, and `parallel_planner.py`: transaction execution and
  block-processing flow
- `account_registry.py`: consensus-backed account authorization, enrollment,
  rotation and recovery
- `simulator.py`, `simulator_worker.py`, `metrics.py`, and
  `vm_observability.py`: simulation and observability helpers
- `services/`: BDS and state-sync-related services
- `cli/`: backend entrypoints owned by this repo
- `dashboard/`: optional HTTP dashboard surface
- `node_setup.py` and `node_admin.py`: node-home creation and configuration
  helpers
- `genesis_builder.py`, `genesis_migration.py` and `state_export.py`: genesis,
  balance-only import, export, and restore
  helpers
- `utils/` and `tools/`: shared helpers plus transition-area data such as state
  patches and genesis-upgrade assets

```mermaid
flowchart LR
  ABCI["xian_abci.py"] --> Methods["methods"]
  Methods --> Processor["processor and execution engine"]
  Processor --> Policy["execution policy and planner"]
  Processor --> Contracting["contracting runtime"]
  Methods --> Services["services"]
  Services --> BDS["BDS and state sync"]
  ABCI --> Dashboard["dashboard"]
  CLI["cli entrypoints"] --> ABCI
```

## Notes

- This is the main architectural boundary in the repo. Changes here often
  affect node behavior directly.
- `methods/`, execution flow, rewards, validators, and state export/import are
  especially consensus-sensitive.
- `services/` contains optional but important runtime layers such as BDS and
  snapshot/state-sync support.

## Next

- Open `xian_abci.py` and `methods/` for transaction and request flow.
- Open `services/` for BDS or snapshot/state-sync work.
- Open `dashboard/` for the optional HTTP dashboard surface.
- Open `cli/` when changing backend-oriented commands exposed by this repo.
