# Oracle V1 to Forge V2 local bridge

Oracle V1 and Forge V2 run as separate processes. The cTrader demo trader in
V1 sends identified trade outcomes and structured per-symbol decision
observations to the authenticated V2 API endpoint at
`http://127.0.0.1:8000/internal/forge/act`. Forge V2 persists accepted records
through its Chronicle client. V1's bundled Forge is not loaded. The V1 cTrader
trader also reports a heartbeat after each completed scan cycle through the
same fsynced outbox.

Forge keeps hypothesis objects in its working memory and persists each
hypothesis, status change, experiment link, and evidence update to Chronicle.
The former local JSON hypothesis queue is imported once on first Chronicle
startup; after that, Chronicle is authoritative.

The V2 API can run in bridge-only mode. It starts Chronicle, Atlas, and Forge,
binds to loopback, and does not initialize a second Oracle agent. The bridge
accepts Oracle research, demo outcome, cost observation, catalog, drift, and
decision-observation and trader-heartbeat tasks. It exposes no order execution
or promotion task.
For an improvement request, Oracle V1 supplies the active genome, regime,
performance evidence, and authoritative risk policy. Forge V2 fetches its own
read-only cTrader history for W1/D1/H4/H1/M45/M30/M15 and persists the dataset
manifest through Chronicle. Forge does not infer absent costs or risk limits.

## Configure

Generate one random token and put the same value in both repository `.env`
files as `ORACLE_FORGE_BRIDGE_TOKEN`. Keep it local and do not commit it.

In the V2 `.env`, set:

```dotenv
ORACLE_FORGE_BRIDGE_ONLY=true
ORACLE_FORGE_BRIDGE_TOKEN=<same-random-token>
# Optional local-only path when cTrader credentials remain in the V1 .env:
ORACLE_FORGE_CTRADER_ENV_FILE=C:\Users\HP\Documents\Universe\Universal_AI\.env
# Forge V2 also needs cTrader API credentials. Its read-only collector records
# commission metadata, spread snapshots, and eligible fill/slippage evidence.
# Incomplete profiles keep strategy reviews blocked.
```

In the V1 `.env`, set:

```dotenv
ORACLE_FORGE_BRIDGE_URL=http://127.0.0.1:8000
ORACLE_FORGE_BRIDGE_TOKEN=<same-random-token>
ORACLE_FORGE_BRIDGE_TIMEOUT_SEC=3
```

The V1 client writes outcomes and decision observations to
`Oracle/memory/forge_bridge_outbox.jsonl` before delivery. It retries pending
events in the background. Forge V2 deduplicates outcomes by `close_event_id`
and decision observations by `event_id`, so delivery retries do not count
the same event twice. Per-symbol summaries expose counts for no setup, risk
rejection, market closure, position limits, data or execution errors, and
filled trades. They also retain a bounded set of recent decision details
(reason codes, hold or rejection message, confidence, regime, and strategy
identity, compact fused signal/threshold values, market context, stream
direction/confidence and evidence health, plus risk-floor comparisons) so Atlas
can investigate the observed failure pattern instead of receiving outcome
counts alone. Raw news, posts, and provider payloads are excluded. The cTrader
trader reports these outcomes to the local outbox;
Forge and Atlas are not called synchronously while Oracle decides or places an
order.

Malformed or unsupported outbox rows, and HTTP 400/413/422 validation failures,
are retained in `Oracle/memory/forge_bridge_dead_letter.jsonl` and removed from
the delivery queue only after the dead-letter write is flushed. Network errors,
authentication failures, and Forge/Chronicle failures stay at the head of the
FIFO queue and are retried, so transient outages do not discard events.

After at least 10 strategy-level decisions (`no_setup` or `risk_rejected`),
14 days without a fill, and a recent strategy evaluation (within 48 hours by
default), Forge persists a Chronicle research job before dispatching Atlas
work. Market-closed, mapping, execution, and other operational outcomes do not
count as strategy evaluations. Startup recovery resumes queued jobs and jobs
whose worker lease expired; concurrent diagnostic workers are bounded. Forge
selects a registered genome-component ablation using its Chronicle-backed
performance history for the same strategy, symbol, and regime. A cold-start
selection is recorded as exploration. The choice is independent of whether the
most common outcome was `no_setup` or `risk_rejected`; that outcome alone does
not identify a causal component. The ablation is a strategy-performance probe,
not proof of why Oracle did not trade. The development screen uses the first
40% for fitting and the next 20% for exploration, leaving
the final 40% untouched. If Chronicle can replay the same fingerprinted dataset,
Forge then evaluates that exact registered ablation once on the lockbox. The
lockbox claim is durable and retry-safe. Only a complete lockbox with sufficient
base and stress trade samples and complete statistical evidence can confirm or
reject the hypothesis. Development screens remain inconclusive. Neither phase
changes live risk settings or promotes a genome. Missing V1 API, active genome,
qualified account-matched cost evidence, or complete market history leaves an
explicit blocked status; Forge retries blocked experiments no more than hourly.
If Atlas research is unavailable or lacks a usable summary and cited sources,
Forge records it as partial and refreshes the research on retry before launching
an experiment. Atlas report identity, query and Oracle-observation fingerprints,
citations, findings, limitations, and contradictions are attached to the
Chronicle-backed hypothesis as research evidence. This evidence is append-only
and cannot settle the hypothesis; only the verified experiment lockbox can.
Other blockers are
researched and recorded without automatically disabling risk or execution
controls.

Forge records the latest heartbeat and writes a Chronicle operational incident
when a previously observed trader becomes silent. A stale heartbeat means
Oracle telemetry is missing; it cannot by itself distinguish a stopped trader
from a failed bridge or an undelivered V1 outbox. The incident is marked
recovered when a later heartbeat arrives. No heartbeat ever received means
Forge has no evidence that this trader instance was expected to be online.
Newer V1 heartbeats include per-symbol decision counts. If a configured symbol
is absent from repeated completed scans, Forge stores a separate Chronicle
coverage record and asks Atlas to investigate the scan path; it does not label
that as a strategy failure or run a strategy experiment. Defaults require 12
missed cycles and 24 hours. Older heartbeat formats retain liveness monitoring
but are marked as lacking per-symbol coverage. The authenticated status
endpoint reports both strategy no-trade cases and no-evaluation cases.
The latest trader, diagnosis-case and durable-job summaries are available from
the authenticated loopback `GET /internal/forge/oracle/training-status` endpoint.

The automatic V1 incumbent lookup uses `ORACLE_V1_API_URL` (default
`http://127.0.0.1:8001`) with the same local bridge token. Keep the V1 API,
V2 Forge API, and V1 cTrader demo trader running for the complete loop.
Configure `FORGE_ORACLE_NO_TRADE_DAYS`,
`FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS`,
`FORGE_ORACLE_NO_TRADE_MAX_EVALUATION_GAP_HOURS`,
`FORGE_ORACLE_NO_EVALUATION_MIN_CYCLES`, and
`FORGE_ORACLE_NO_EVALUATION_HOURS` in the V2 `.env` to adjust these thresholds.
Forge records the sourced research and a testable hypothesis; it
does not change Oracle's incumbent strategy or risk settings. Outcomes without an
identified strategy genome, or from a non-demo account, are not sent as
training feedback. P&L is currently expressed in initial-stop R and marked
provisional because the V1 outcome does not normalize broker costs.

Improvement requests must include `symbol`, `regime`, `baseline_genome`,
`performance_metrics`, and `risk_policy`. The request may omit market bars;
Forge obtains and fingerprints the seven timeframe datasets itself. Cost data
must be a versioned, sample-qualified cTrader-observed profile. Forge reads its
latest saved record under Chronicle's research area. Public/manual estimates
may be retained as reference data, but do not authorize a strategy experiment.
Five-year coverage, cost-profile, trade-sample, and Oracle risk checks still
gate every review. Research results remain review-only; Forge cannot promote
or activate a strategy.

## Collect and inspect cTrader cost evidence

Restart the V2 API after code changes. From the V2 `Universe` directory, run
`Oracle/tools/forge_experiment_runner.py` with `ORACLE_FORGE_EXPERIMENT_TYPE`
set to `cost-observations`. It calls V2 Forge directly; the V1 Oracle agent does
not collect or interpret the broker costs. The cTrader API can report a symbol's
commission schedule and a live spread snapshot, but observed execution
slippage requires actual fills. Forge requires at least 20 actual fills and
100 matched historical spread quotes before a profile can be used. Symbols
without enough fills remain blocked.

The latest per-symbol evidence and blockers are stored under
`Chronicle/memory/research/oracle_cost_profiles/<SYMBOL>/latest.json`. The collection
summary is `Chronicle/memory/research/oracle_cost_profiles/collection_summary.json`;
the workflow also prints its profile, commission, spread, and fill counts.
Historical account cost samples are evidence for that demo account and should
not be treated as guarantees of future execution costs.

After a successful collection, ordinary local experiment runs can reuse the
saved qualifying profile without repeating fill-history collection. They still
fetch a fresh Forge-owned market-data window. The Forge API must be restarted
after code updates so the fingerprint and improvement endpoints use this
Forge-owned data path.

The scheduled and manually dispatched GitHub workflow now defaults to EURUSD
and collects its empirical cost evidence each run. Other symbols can still be
selected explicitly. Forge evaluates risk-adjusted performance, net profit
after costs, drawdown, and sample sufficiency; win rate alone is not a safe
promotion objective because a high hit rate can still lose money when losses
are larger than wins.

## Run a local V1-to-V2 research cycle

The local research runner needs both APIs: V2 hosts Forge and Chronicle, while
V1 provides its persisted incumbent strategy and catalog. Start the V2 API in
bridge-only mode on port 8000. In a separate terminal, start the V1 API from
`C:\Users\HP\Documents\Universe\Universal_AI` on port 8001:

```powershell
$env:ORACLE_API_HOST = "127.0.0.1"
$env:ORACLE_API_PORT = "8001"
python api.py
```

The runner detects that V2 has no Oracle agent, requests the current V1
incumbent over the loopback token-authenticated endpoint, and sends the
catalog and review request to V2 Forge. It fetches the seven-timeframe market
window from Forge. V1 and V2 must use the same `ORACLE_FORGE_BRIDGE_TOKEN` in
their local `.env` files. Restart both APIs after code changes.

The separate V1 cTrader demo trader can remain running. The V1 API reads the
persisted V1 champion and confidence files so it does not need to share the
trader process's memory. The V2 API must remain active while the runner works.

The API must remain running for V1 to deliver events. If it is offline, V1
keeps outcomes in its local outbox and retries after the API returns. The
scheduled GitHub Actions workflow is separate and is not the live bridge.
