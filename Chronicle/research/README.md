# Oracle and Forge research records

Durable artifacts produced by Oracle's Forge research workflow live under
`Chronicle/memory/research/`. This source directory is retained for documentation
and legacy artifacts. The Oracle research runner copies existing artifacts into
the memory tree on first use without deleting the old copies. Forge treats
research results as durable records; its in-process working state is temporary.

- `oracle_strategy_catalogs/<sha256>.json` contains an immutable snapshot of
  Oracle's persisted strategy genomes and registered family templates. The
  fingerprint covers the canonical snapshot body.
- `oracle_forge_experiments/` contains one report per scheduled or manually
  requested research cycle, including data fingerprints, execution-cost
  profile versions, experiment outcomes, and blocked/skipped reasons.
- `oracle_ablation_learning/` contains scoped, versioned Forge checkpoints for
  Oracle's qualified ablation lessons and idempotent scheduled selections.
  These checkpoints let the selector recover its learned trade-win-rate,
  drawdown, and Sharpe evidence when a hosted Actions runner starts with an
  empty local Chronicle state store.
- `oracle_state/forge_oracle_state.json` snapshots Chronicle's `forge.oracle.*`
  current values, revisions, deletion markers, and histories with a SHA-256
  integrity fingerprint. The Actions
  workflow restores this namespace before Forge starts and exports it after
  the run, so queued no-trade jobs, research cases, hypotheses, cost-profile
  pointers, and experiment state survive the runner. Other agents' state is
  excluded.
- `oracle_market_data/<SYMBOL>/<TIMEFRAME>.csv` contains the current five-year
  completed OHLCV history for each top-down timeframe. A content-addressed
  manifest under `oracle_market_data/manifests/` identifies the exact seven
  datasets used for each evaluation. Git history retains prior snapshots.

The Git branch is the current persistence bridge for hosted Actions, whose
runners are temporary. The state snapshot contains Chronicle state values;
the workflow credentials remain GitHub Actions secrets and are never included.
When Chronicle's approved durable artifact store is available, migrate these
versioned artifacts there and retain their content fingerprints and migration
references in Chronicle records. Raw market bars are kept as dataset artifacts,
separate from vector-memory documents.

These records are research evidence only. They do not authorize strategy
promotion or live trading.
