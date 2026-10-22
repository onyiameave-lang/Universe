# Oracle and Forge research records

This directory holds Chronicle-owned, versioned artifacts produced by Oracle's
Forge research workflow. Forge treats them as durable records; its in-process
working state is temporary.

- `oracle_strategy_catalogs/<sha256>.json` contains an immutable snapshot of
  Oracle's persisted strategy genomes and registered family templates. The
  fingerprint covers the canonical snapshot body.
- `oracle_forge_experiments/` contains one report per scheduled or manually
  requested research cycle, including data fingerprints, execution-cost
  profile versions, experiment outcomes, and blocked/skipped reasons.
- `oracle_market_data/<SYMBOL>/<TIMEFRAME>.csv` contains the current five-year
  completed OHLCV history for each top-down timeframe. A content-addressed
  manifest under `oracle_market_data/manifests/` identifies the exact seven
  datasets used for each evaluation. Git history retains prior snapshots.

The Git branch is the current persistence bridge for hosted Actions, whose
runners are temporary. When Chronicle's approved durable artifact store is
available, migrate these versioned artifacts there and retain their content
fingerprints and migration references in Chronicle records. Raw market bars
are kept as dataset artifacts, separate from vector-memory documents.

These records are research evidence only. They do not authorize strategy
promotion or live trading.
