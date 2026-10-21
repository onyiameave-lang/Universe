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

Forge blocks a strategy experiment unless the symbol has an explicit, versioned
execution-cost profile. The runner prefers a valid cTrader-observed profile;
otherwise it can use a checked-in Pepperstone public-spread profile for
research-only analysis. Each profile must include:

- `broker` and `account_type`, recorded from the connected cTrader account;
- `profile_version`, generated for the observation window;
- `commission_bps`, estimated per side from actual deal commission and USD notional;
- `slippage_bps`, estimated per side by matching fills to historical quotes;
- `spread_bps`, the full quoted spread, not a per-side value;
- `stress_slippage_bps` and `stress_spread_bps`, derived from p99 observations;
- `price_basis`, exactly `mid`, `bid`, or `ask`, verified for the supplied OHLC data.

The empirical cost-sampling policy uses p95 for base costs and p99 for stress,
requires at least 100 matched spread quotes and 20 commission and slippage
observations, and blocks truncated or incomplete data. Commission normalization
currently requires a USD account. Symbols with a broker minimum commission are
blocked because the current bps backtester cannot model a per-trade fee floor.
`ORACLE_FORGE_MINIMUM_EXECUTED_TRADES` defaults to 100; values below 100 are
rejected.

Values must be expressed as basis points relative to price. Forge never fills
missing values with zero. The backtester derives the opposite executable quote
from the declared price basis and applies spread once; slippage is then applied
adversely to the fill, with commission charged separately. Incumbent and
candidate receive the same profile.

Profiles record the sample window and counts. They are blocked when the
observations cannot be matched or normalized safely. If the market data does
not provide the declared price basis, do not present the evaluation as
broker-validated.

## Profile source and action setup

The GitHub workflow requires repository Actions secrets named
`CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`, and
`CTRADER_ACCOUNT_ID`. Each scheduled run derives profiles from authenticated
cTrader account data and passes them directly to the review request. The
profile declares `price_basis: "bid"` to match the current cTrader trendbar
source.
Never add credential values to a commit or workflow log.

The checked-in `Oracle/config/forge_cost_profiles.json` records Pepperstone's
published August 2026 average spreads for selected FX, index, metal and crypto
symbols, together with explicit modeled slippage assumptions. The current
profiles cover the directly named instruments in that file plus canonical
aliases defined in `Oracle/config/ctrader_research_symbol_aliases.json`.
Symbols without either a valid empirical profile or a manual profile remain
blocked. Manual-profile results are labeled research-only, identify the
published-data version and unit conversion, and cannot produce a promotion
recommendation. cTrader does not publish a reliable per-symbol slippage rate;
manual slippage is an assumption, not measured execution evidence.

Scheduled runs use manual profiles by default and skip the slow all-symbol
fill-history collection. When manually dispatching the workflow, enable
`collect_empirical_costs` to request cTrader fill and quote evidence; a valid
empirical profile takes precedence over a manual estimate. Drift observations do
not require execution-cost profiles. Public spread data should be refreshed
when Pepperstone publishes a newer monthly table.

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
