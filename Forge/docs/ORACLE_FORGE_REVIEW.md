# Oracle to Forge first review

Forge accepts an Oracle champion snapshot and performs a read-only research
review. It never changes the active champion or places a trade. The first
version creates one deterministic Forge-owned mutation, evaluates the
incumbent and candidate with the same chronological data split and execution
cost profile, then writes the evidence to Chronicle. A passing result is only a
candidate for separately governed review.

The backtest consumes each genome's entry threshold, volatility filter, stop
and target multipliers, risk fraction, drawdown gate, and trailing-stop logic.
Mutations to other genome modules must not be described as evaluated until an
adapter explicitly wires them into the research execution path.

The workflow supports two research modes: a single deterministic challenger
mutation, and a manually selected single-module ablation for trend, momentum,
volatility, entry, exit, risk, position sizing, trade management, or market
regime filtering. An ablation measures the effect of the declared disable
policy in this simulator; it does not by itself prove causal contribution in
live markets. PPO training is not mapped because Oracle's current champion
interface is a `StrategyGenome`, not a PPO policy adapter.

## Required profile

Forge blocks a recommendation unless the symbol has an explicit, versioned
execution-cost profile in `ORACLE_FORGE_COST_PROFILES_JSON`. The environment
variable must contain a JSON object keyed by Oracle symbol. Each profile must
include:

- `broker` and `account_type`, verified against the cTrader account;
- `profile_version`, changed whenever any cost or data assumption changes;
- `commission_bps`, charged per side;
- `slippage_bps`, charged per side;
- `spread_bps`, the full quoted spread, not a per-side value;
- `stress_slippage_bps` and `stress_spread_bps`, explicitly specified adverse
  assumptions no lower than base, with at least one strictly worse;
- `price_basis`, exactly `mid`, `bid`, or `ask`, verified for the supplied OHLC data.

`ORACLE_FORGE_MINIMUM_EXECUTED_TRADES` defaults to the initial policy value of
100. Values below 100 are rejected; a higher minimum is allowed. Forge does not
infer the broker, account, or numerical cost values.

Values must be expressed as basis points relative to price. Forge never fills
missing values with zero. The backtester derives the opposite executable quote
from the declared price basis and applies spread once; slippage is then applied
adversely to the fill, with commission charged separately. Incumbent and
candidate receive the same profile.

The current Oracle market-data sources do not establish a broker-specific
spread or account commission. A verified review requires the source semantics
and account schedule to be confirmed. If a source does not provide the declared
price basis, do not present the evaluation as broker-validated.

## Profile source and action setup

The GitHub workflow requires repository Actions secrets named
`CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`, and
`CTRADER_ACCOUNT_ID`. It loads the versioned, non-secret EURUSD profile from
`Oracle/config/forge_cost_profiles.json`; a private
`ORACLE_FORGE_COST_PROFILES_JSON` Actions secret can override that file. The
profile declares `price_basis: "bid"` for the current cTrader trendbar source.
Never add credential values to a commit or workflow log.

The checked-in EURUSD profile is explicitly provisional and user-approved. It
uses the proposed $3.50/standard-lot/side commission converted at a reference
EURUSD price of 1.13700, plus the proposed 0.3 pip spread (0.26 bps), 0.5 pip
slippage (0.44 bps), and 1.5 pip stress spread (1.32 bps). Those assumptions are not independently verified
Pepperstone historical execution costs. The profile records its provisional
status in every Chronicle experiment record; replace it with measured/account
values and bump `profile_version` when verified.

Every scheduled run now queries cTrader for symbol commission metadata and
captures one live bid/ask spread observation per symbol. The observations are
stored in the run report under `broker_cost_observations`. They are not silently
converted into active profiles: a single live spread is not a historical spread
distribution, and cTrader does not expose a symbol-level slippage assumption.
Oracle must record demo order reference quotes and fills to estimate slippage.
Until a full, versioned profile is available, Forge continues to block that
symbol's strategy experiment. Drift observations do not require execution-cost
profiles.

The six-hour GitHub Action checks out `oracle-v2`, rotates one research mode
across the watchlist, and commits a JSON report under
`Forge/reports/oracle_experiments/`.
Scheduled workflows must also be present on the repository default branch.
Each run uses a fresh hosted runner; Chronicle's local state is not durable
between runs, while the committed report is. This initial Action is an
operational demonstration, not a durable holdout registry. It must not support
claims of repeated independent holdout evidence or strategy promotion.

## Promotion guardrails

- The initial minimum is 100 completed trades for both incumbent and candidate
  on the holdout. This is a minimum evidence threshold, not proof of statistical
  reliability.
- The candidate must be profitable after modeled costs.
- Unrounded candidate maximum drawdown and trade win rate may not exceed the
  incumbent's corresponding values.
- Candidate maximum drawdown must also remain within the active Oracle
  `RiskManager.max_drawdown` limit.
- The candidate must remain profitable and satisfy the relative drawdown, win
  rate, absolute drawdown, and 100-trade checks under the explicit stress costs.
- Paired chronological holdout evidence must pass the configured statistical
  checks. Bar-level positive-return frequency and completed-trade win rate are
  recorded as distinct measures.
- A recommendation never promotes a genome. Oracle's incumbent remains active
  until a separate authorization and deployment path approves a change.

## Current scope

This first slice evaluates one candidate mutation per data fingerprint and
includes one explicit adverse spread/slippage stress rerun. Although the Action
runs monthly and stores reports, the Chronicle duplicate-holdout claim is
ephemeral in hosted Actions. Before treating repeated runs as institutional
training, Forge needs a durable holdout ledger and expanding walk-forward
evaluation over newly matured unseen periods. A broad candidate search also
needs development-only selection and multiple-comparison controls.
