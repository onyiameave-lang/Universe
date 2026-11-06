# Forge and Oracle research artifacts

This is the canonical durable research tree for Oracle/Forge. GitHub Actions
restores and commits these artifacts on the `oracle-v2` branch.

- `oracle_forge_experiments/` stores one report per scheduled or manually
  requested cycle.
- `oracle_strategy_candidates/<SYMBOL>/<EXPERIMENT_ID>.json` stores each
  completed Oracle candidate genome with its evaluation evidence and source
  report. These records are research candidates only; they do not change
  Oracle's active strategy or authorize promotion.
- `oracle_cost_profiles/<SYMBOL>/latest.json` stores the latest validated,
  account-identifier-redacted execution-cost profile available to Actions.
- `oracle_market_data/` stores content-addressed OHLCV datasets and manifests.
- `oracle_strategy_catalogs/` stores versioned snapshots of the Oracle strategy
  catalog.
- `oracle_state/forge_oracle_state.json` stores the Forge-owned Oracle Chronicle
  state needed to continue research on ephemeral Actions runners.

The older `Chronicle/research/` directory is a legacy source retained for
migration compatibility. New artifacts should be written only under this
`Chronicle/memory/research/` tree.
