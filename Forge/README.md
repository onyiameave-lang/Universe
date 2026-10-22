# Forge

*Training Engine -> Forge* | **training** | security: elevated

## Mission
Train with rigor and continually adopt better methods through evidence.

## Capabilities
- `training.run`
- `training.evolve`
- `method.discover`
- `model.drift`
- `backends.catalog`

## V2 Promotion Gate

Champion promotion is an explicit two-step operation through
`Forge.core.promotion.PromotionGate`: `evaluate()` checks challenge evidence
against a policy, and `commit()` checks authorization and stale registry state
before asking `ChampionRegistry` to perform the transition. Evaluation requires
an injected authoritative challenge-evidence provider and persists the exact
decision in the champion registry. Commit reloads that decision, rejects
caller-created or altered decision objects, and rechecks a digest of the
authoritative challenge result and both reports before consuming authorization.
Aegis claims bind to that exact evidence digest. Champion state, the challenge
replay marker, and promotion decision status commit in one champion-registry
SQLite transaction. Every champion transition requires signed Aegis approval;
domain or risk overrides cannot make approval optional. An
`AuthorizationToken` is a claim container, not proof: commit requires an
injected trusted Aegis verifier to validate the issuer/signature, bind the
one-time authorization to the exact promotion decision, and consume its nonce.
Forge's CLI loads verifiers only when `FORGE_AEGIS_TRUSTED_ISSUERS_JSON` is
configured. It must contain a JSON object mapping issuer IDs to base64url
Ed25519 public keys; the keys are scoped to separate `forge.promotion`,
`forge.deployment`, `forge.training`, `forge.reuse`, and `forge.operations` audiences. Forge loads no private
signing keys and trusts no default issuer. Without configured keys, governed
actions remain fail-closed.

The champion promotion ledger's operational status checks more than SQLite's
page integrity: it validates champion JSON against indexed IDs and revisions,
ensures tenure IDs are unique, and checks that persisted promotion decisions,
commit results, replay markers, and bootstrap results agree. A mismatch makes
the ledger not ready and blocks governed evolution until the records are
reconciled. This detects inconsistent records; it does not make the local
SQLite file tamper-proof or replace independently protected backups.

For supervised ML, V2 records the split implementation, seed, dataset sample
count, and SHA-256 fingerprints of the exact train and validation row-index
orders. Baseline and treatment must report the same partition, and promotion
requires that partition to match the incumbent benchmark evidence. Hyperparameter
search remains confined to the training rows; the validation rows supply the
paired evaluation draws. Exact duplicate feature rows are assigned as one group
so copies cannot appear in both training and validation; if no disjoint grouped
split can be formed, the experiment fails instead of silently reverting to a
row-wise split.

ChampionRegistry repeats the exact claim checks and verifies/consumes the Aegis
nonce inside its serialized promotion transaction, after checking the persisted
decision and current champion revision. The nonce ledger is a separate SQLite
store, so a later champion-database persistence failure can still consume the
nonce; Forge records that fact and requires a fresh authorization. Promotion is
never retried automatically.

Blocked and failed promotion attempts are retained in the champion ledger with
the decision/challenge IDs, failure reason, authorization ID, and whether the
nonce had already been consumed. Token signatures and claims are not copied into
this attempt log. `evolution.status` reports the cumulative failure count and
the most recent failed attempt, including bounded failure detail, so operators
can investigate stale-state races or persistence failures after restart.

PowerShell configuration example (replace the placeholder with an operator-
managed public key):

```powershell
$env:FORGE_AEGIS_TRUSTED_ISSUERS_JSON = '{"aegis-prod":"BASE64URL_ED25519_PUBLIC_KEY"}'
```

Champion state and committed-challenge replay markers are stored transactionally
in `Forge/challenger/champions.sqlite3`. On first startup, the registry imports
the legacy `champions.json` and `committed_challenges.json` files in one
transaction; malformed legacy data prevents startup rather than being silently
discarded. The JSON files are retained as migration/recovery inputs and are not
the live write path. Promotion updates the champion and its replay marker in a
single transaction. Champion founding follows a separate governed bootstrap:
`evolution.bootstrap.prepare` runs a V2 experiment and persists its evidence,
`evolution.bootstrap.authorization_request` returns exact claims bound to that
evidence, owner identity, and empty slot revision, and
`evolution.bootstrap.commit` requires
a trusted signed Aegis authorization before atomically creating the first
tenure. ChampionRegistry verifies the exact token against the persisted
bootstrap claims at the mutation boundary; the translator does not consume the
nonce separately. Direct initialization helpers are disabled. Multiple empty slots for
the same context require an explicit `champion_id`; only one prepared bootstrap
may be active for a slot. Failed founding attempts are retained with the
authorization ID and whether nonce consumption occurred; token claims and
signatures are excluded from that audit record.

Every V2 champion position has an immutable `owner_agent`. Built-in model,
prompt, workflow, configuration, and evaluation domains map to `forge`;
`ORACLE_STRATEGY` maps to `oracle`, and `CHRONICLE_MEMORY` maps to `chronicle`.
Custom domains must declare an owner. Challenge requests must name the same owner as the slot.
Promotion decisions and signed authorization claims include that owner, and the
registry rechecks it in the promotion transaction. Legacy champion files infer
ownership only for those known domains; an unrecognized legacy owner blocks
bootstrap and promotion pending explicit ownership repair. The Aegis policy
must independently validate the owner/domain mapping before issuing a token.
Forge also enforces that mapping from operator configuration; a caller's
`owner_agent` field alone cannot register or challenge a custom domain. Set
`FORGE_DOMAIN_OWNERS_JSON` to a JSON object for custom domains, for example:

```powershell
$env:FORGE_DOMAIN_OWNERS_JSON = '{"SENTINEL_CORROBORATION":"sentinel"}'
```

Ownership mappings are stored in the champion registry and cannot be silently
changed or removed by changing the environment. The operational snapshot lists
the configured map and any champion ownership mismatches. A legacy custom
champion with unresolved ownership remains blocked until its ownership is
reconciled through a separately reviewed, operator controlled migration.

## V2 Drift Monitoring

`Forge.core.drift.DriftMonitor` wraps the existing model registry drift
detector and reports policy-classified observations. The current V1 statistic
is a standardized mean shift, not PSI; each feature report identifies that
method. Caller-supplied observation windows must use timezone-aware timestamps
and provide both their start and end. Reports with fewer than the effective
policy's minimum observations are classified as `INSUFFICIENT_DATA`.

`Forge.core.drift_orchestrator.DriftOrchestrator` turns reports into
investigation suggestions. `execute_investigation()` requires authorization
matching the exact persisted report and suggestion, verifies its signature
through the trusted `forge.promotion` Aegis verifier, consumes its nonce, and
rechecks cooldown and active-investigation limits inside a SQLite write
transaction. Reports and investigations are stored in
`Forge/drift/investigations.sqlite3`; prior JSON investigation state is imported
once. `drift.assess` and the `drift.investigation.*` tasks expose assessment,
authorization requests, execution, and completion. This records/queues an
investigation only; it does not submit an experiment or promote a champion.
Promotion remains the responsibility of the V2 Promotion Gate.

## V2 Evaluation Integrity

`Evaluator` accepts only a completed result whose experiment ID matches the
contract; `Benchmark` additionally requires the evaluation report's experiment,
run, and result IDs to match the supplied result. Metric values must be finite
numbers; missing, malformed, NaN, or infinite values are recorded as
incomparable instead of becoming apparent improvements or regressions. ML
experiments now retain deterministic paired-bootstrap metric draws from the
held-out validation rows. Only aggregate metric draws are persisted; raw labels,
predictions, and features are not added to the experiment ledger. The evaluation
contract records the confidence level, minimum validation sample count,
bootstrap replicate count, and minimum effect per metric. Metric intervals use a
Bonferroni-adjusted confidence level across the declared metrics and the
candidate family in one discovery batch. Evaluation reports preserve the
nominal and adjusted confidence levels, family size, declared metric count,
sample requirement, observed sample count, bootstrap method, and replicate
count. Promotion rechecks that this design matches the per-metric evidence and
incumbent benchmark before accepting it. Discovery contracts carry a family ID
and the exact unique candidate member list; promotion requires the evaluated
candidate to belong to that list. This preserves the declared correction scope
in durable evidence; the holdout ledger separately rejects reuse by a later
candidate family.

Promotion requires complete statistical evidence both against the experiment's
baseline and against the current champion. The champion comparison is accepted
only when the incumbent and candidate evidence match on dataset fingerprint,
holdout sample count, seed, bootstrap method, and replicate count. A champion
without that evidence cannot be challenged until it is re-established with new
governed evidence; no legacy evidence migration is currently provided. Missing
or mismatched evidence blocks promotion. Paired percentile bootstrap intervals are an
approximation; they do not establish causal improvement or protect against a
mis-specified holdout, or replace domain-specific risk limits and independent
validation. New ML runs fingerprint the exact raw validation observations;
Oracle runs fingerprint the chronological evaluation suffix. The ledger uses
that content fingerprint to detect reuse even when a dataset is extended or
repackaged, while retaining the dataset and partition fingerprints as provenance.
One declared family may evaluate its listed candidates on that holdout; a later
family reusing it is recorded as reused and its evidence cannot be promoted.
Holdout family and use records are stored transactionally with completed runs
and exposed in experiment-ledger status. The additive schema 3 migration
preserves earlier experiment evidence. Older tracked records retain their former
dataset-plus-partition reuse key, so they cannot detect reuse across changed
dataset packaging; untracked legacy results must be re-evaluated before promotion.

V1 hyperparameter search now uses cross-validation on the training partition
only. The held-out partition remains outside tuning and is used for final
measurement. This reduces validation leakage but does not replace temporal,
group-aware, or externally maintained holdouts where the domain requires them.

`ChallengerEngine` persists each challenge request, benchmark context, result,
evaluation report, and benchmark report in `Forge/challenger/challenges.sqlite3`
as one evidence record. It assigns stable report IDs, exposes database health,
running challenge IDs and elapsed times, and claims a challenge ID transactionally so two
processes cannot execute the same challenge concurrently. Reusing an ID with
different input is rejected. A process crash leaves a visible `RUNNING`
challenge. `evolution.challenge.reconciliation.authorization_request` and
`evolution.challenge.reconciliation.reconcile` can close one only when no
experiment run was ever persisted, and require a signed `forge.operations`
authorization bound to the persisted request/context digests and operator's
worker-terminated attestation. If a run exists, this path refuses to discard
the challenge. `evolution.challenge.resume.authorization_request` and
`evolution.challenge.resume` instead resume from one selected terminal
experiment run/result pair. Their authorization binds both evidence digests;
Forge reruns evaluation and benchmarking from the persisted result without
rerunning training. A still-`RUNNING` experiment must first be reconciled
through the experiment-run flow. The engine does not assume an underlying
worker stopped. The legacy
challenge-results JSON is retained as an atomic compatibility snapshot.

## Experiment Run Ledger

`ExperimentRunner` stores experiment contracts, run lifecycle records, and
results in `Forge/experiments/experiments.sqlite3`. A run is durably recorded
as `RUNNING` before adapter execution, and its terminal run state and result
are committed together. SQLite is authoritative; `experiments.json` and
`results.json` are atomically refreshed compatibility snapshots for older
diagnostics. Existing JSON ledgers are imported once and malformed input fails
startup rather than being silently discarded. A process crash can leave a run
marked `RUNNING`; that state is visible for operator reconciliation, and the
runner does not guess whether work from another process is safe to abort or
retry. Stale runs are never cleared automatically. Operators can request an
exact-claim `forge.operations` authorization for a run only after confirming
that its worker process has terminated; the signed claims bind the run record
digest, reason, operator identity, and termination attestation. Reconciliation
records an `ABORTED` result with the authorization and claims digests in the
same SQLite transaction as the terminal run state. This does not terminate a
blocked thread or cancel underlying work. The two agent tasks are
`experiment.reconciliation.authorization_request` and
`experiment.reconciliation.reconcile`; the latter fails closed unless a trusted
`forge.operations` verifier is configured. Before an ML experiment is accepted,
Forge resolves the dataset path,
hashes the exact file bytes, and binds that fingerprint and the experiment
configuration to the durable contract. Execution rechecks the dataset hash
before baseline, treatment, and completion. Each run records the random seed,
Python/platform/package versions, and a fingerprint of the Forge source files
that implement training and evaluation. V2 training propagates the contract
seed through the split, CV tuning, and supported backends. Identical evidence is
therefore reproducible within a compatible runtime; bit-for-bit equality across
different hardware or numerical-library builds is not guaranteed.
The runner admits at most two experiment executions concurrently per process
and across all processes by default. `FORGE_MAX_CONCURRENT_RUNS_LOCAL` sets the
local limit; `FORGE_MAX_CONCURRENT_RUNS_TOTAL` sets the shared limit. A
`BEGIN IMMEDIATE` transaction checks all durable `RUNNING` records and reads the
current shared limit before inserting a run, so processes sharing the ledger
cannot claim the same final slot. All processes must start with the same total
limit. Operators can change it with an audited ledger operation:

```powershell
python -m Forge.core.experimentation set-capacity 4 --reason "Approved capacity increase"
$env:FORGE_MAX_CONCURRENT_RUNS_TOTAL = "4"
```

The command rejects a lower limit than the count of active durable runs and
records the operator, reason, old and new limits, and active-run count. Run it
before starting processes with the new environment setting. A crashed worker's
`RUNNING` record continues to consume capacity until an operator safely
reconciles it; Forge never assumes that a recorded worker stopped. When a
challenger hits this capacity limit before a run is created, its durable
challenge returns to `PENDING` and can be retried with the same unchanged inputs.

Domain-neutral contracts are available through `experiment.contract.submit`,
`experiment.contract.execute`, `experiment.contract.evaluate`, and
`experiment.contract.report`, also exposed by `ForgeClient`. The contract
domain must have a registered adapter; `ForgeAgent(experiment_adapters=[...])`
accepts explicitly injected adapters while preserving one adapter per domain.
Forge persists the first evaluation report for each completed run in the same
SQLite ledger (schema 1 is upgraded additively to schema 2). Repeated evaluation
returns that stored report. These endpoints report evidence and comparisons;
they never promote or mutate the requesting agent's champion. Integrators must
provide independently controlled, domain-appropriate data, adapters, and metric
specifications. Dataset files are fingerprinted when the contract is prepared
and rechecked before and after execution. The run also records Forge and adapter
source fingerprints; an adapter without an inspectable source file must declare
a 64-character SHA-256 `code_fingerprint` or Forge records a failed run without
executing it.

`ForgeAgent` registers the included `OracleExperimentAdapter` by default. It
uses Oracle's strategy genome and backtester over a fingerprinted local OHLCV
CSV, without starting the trading agent or fetching live data. The adapter
evaluates each signal after a completed bar and fills it at the next bar's open,
so a signal that uses a close cannot fill at that same close. Oracle's shared
backtester uses next-open execution by default; `legacy_close` exists only to
reproduce historical results and is unsuitable for promotion evidence. The
adapter requires a predeclared `evaluation_start_index`, evaluates only the
chronological suffix, and fingerprints the development prefix and evaluation
suffix separately. It emits paired circular block bootstrap draws over aligned per-bar
mark-to-market returns. Use `period_total_return`, `period_sharpe`,
`period_max_drawdown`, `period_win_rate`, or `period_expectancy` in the contract
to request paired inference. These period metrics are distinct from Oracle's
legacy trade-level aggregates. The adapter records the seed, block length,
sample count, method, and cost assumptions. Oracle contracts must declare
`evaluation_start_index`, `commission_bps`, and `slippage_bps` per side (zero is allowed only when justified
by the declared execution assumptions); the adapter applies both to entries and exits.
Zero values remain explicit assumptions, not proof of frictionless execution. The contract
may pin `bootstrap_block_length`, or
Forge uses a deterministic square-root-of-sample-count default. Forge records the method on each evaluation metric
and marks unsupported or incomplete metrics statistically unavailable, which
blocks promotion. Oracle's own evolution flow separately stages candidates and
requires an Oracle-scoped Aegis authorization before changing its champion
library.

## Evolution Entry Point

`ForgeAgent`'s `training.evolve` task is discovery-only. It returns runnable
method proposals and does not call the legacy `MethodDiscovery.challenge`
auto-adoption path, which could change the preferred backend from a single CV
score without V2 promotion authorization. The V2 challenger and promotion
flow is exposed through `evolution.discover`, `evolution.challenge.run`,
`evolution.promotion.evaluate`, `evolution.promotion.authorization_request`,
and `evolution.promotion.commit`. Initial champion establishment uses
`evolution.bootstrap.prepare`, `evolution.bootstrap.authorization_request`,
and `evolution.bootstrap.commit`. These tasks consume the durable challenge
ledger; commit reloads the persisted decision rather than trusting a request
body. Training reads a backend preference only from an established V2 champion.
The current repository has empty V2 champion positions. Aegis keys can be configured at startup;
rollout advancement also requires an injected runtime health-gate adapter.

`evolution.status` and `ForgeAgent.get_status()` expose per-ledger integrity,
counts, active work, and capability-specific readiness. The experiment ledger
includes each `RUNNING` run's ID, experiment ID, start time, and elapsed time so
operators can investigate work that outlives its expected duration. An old
`RUNNING` record does not prove its worker was cancelled: Python cannot safely
terminate a thread blocked in an adapter, so the status endpoint never clears or
retries such work automatically. The response separates
training, evolution, and deployment readiness and lists blockers such as missing
promotion authorization, deployment authorization, or a trusted health gate.
It also reports separately whether elevated and critical cross-agent training
requests can be authorized; standard and low-risk training readiness does not
imply that those higher-risk request types are enabled.
Forge training lessons enter a SQLite outbox in the model registry database in
the same transaction that records their experiment. This closes the crash gap
between saving a training result and queuing its memory event. Forge retries
queued entries at startup, after new lessons, and through the
`knowledge.outbox.drain` task. Each entry uses a stable Chronicle memory ID, so
retry after a delivery/acknowledgment interruption is idempotent. Workers claim
one entry at a time under a SQLite write transaction; a five-minute lease makes
uncompleted claims available again after a worker crash. Outbox readiness also
checks event/payload identity and delivery-state consistency. The outbox
database is included in the durable-state backup. Training readiness requires a
running Chronicle client and a healthy, empty outbox; pending delivery is visible
as a readiness blocker. The one-shot `--status-json` probe does not start
Chronicle, so it reports this dependency as unavailable when no running Chronicle
client is attached to that process.
Evolution readiness also requires at least one incumbent with complete bootstrap
evidence for every recorded metric; legacy scalar-only incumbents are reported
as not challenge-ready.
An operational query can succeed while a capability is correctly reported as
not ready; missing controls do not get represented as healthy.

Cross-agent training requests marked `ELEVATED` or `CRITICAL` are durably held
in `PENDING_AUTHORIZATION`. An operator obtains exact claims through
`training.request.authorization_request`; Forge binds the signed `forge.training`
authorization to the request content digest, risk category, target type, and
operator identity, verifies and consumes its nonce, then queues the request.
Dispatch rechecks that the persisted lifecycle contains authorization metadata.
Configure the same trusted issuer keys for the `forge.training` audience using
the existing Aegis key configuration. If no verifier is configured, requests
remain pending and cannot run. `STANDARD` and `LOW` requests use the ordinary
queue path. The request ledger is authoritative for queued/running state;
startup restores queued records, and a SQLite transaction claims each request
once across processes while enforcing shared global and per-agent concurrency
limits. Accepted timestamps and UTC-day per-agent request quotas are enforced
inside that same transaction. The ledger also rejects an equivalent request
digest while the earlier request is pending authorization, queued, running, or
being evaluated, including submissions from separate Forge processes. It
records a scheduler-policy fingerprint
and rejects workers configured with different limits or quotas.
Terminal lifecycle status and its training report are committed in one SQLite
transaction, so a crash cannot leave a completed request without its report.
Operational readiness cross-checks immutable request digests, indexed and
serialized lifecycle status, and exactly one matching report for every terminal
request; logical inconsistencies block request-ledger readiness.
`training.request.scheduler_status` reports both the local scheduler view and
the durable ledger. A process crash leaves a visible `RUNNING` record that
continues to consume capacity. Forge does not retry it automatically. After an
operator confirms its worker has terminated, the
`training.request.reconciliation.authorization_request` and
`training.request.reconciliation.reconcile` tasks close that exact record with
a `forge.operations` authorization bound to the request, lifecycle digest, and
worker identity. This reconciliation records an `ABORTED` state; it cannot stop
worker code. Cancellation is durable and race-safe for queued requests. A
running request returns its worker identity and remains running; Python cannot
forcibly stop adapter code, so operators must verify termination before using
the reconciliation flow. The status snapshot reports elapsed time and whether
the configured timeout has been exceeded. A blocked worker remains `RUNNING`
and retains its capacity; if it later returns, Forge records `TIMED_OUT`, keeps
the returned evidence for diagnosis, and marks the report non-promotable. The
in-process `ResourceScheduler` follows the same rule: `check_timeouts()` reports
a timeout once while retaining the running slot, and `cancel()` on running work
only records a cancellation request. Capacity is released when execution
returns or after the authorized reconciliation path has verified worker
termination. Global and per-agent queue-depth limits are checked inside the
shared SQLite admission transaction, so multiple Forge workers cannot each
fill their own queue beyond the configured shared cap. Daily submission quotas
are likewise enforced from the durable ledger. The status snapshot reports
durable global and per-agent queue use. Elevated requests count against the
daily quota when submitted; authorization later queues that same request
without counting it a second time. If capacity fills before authorization is
committed, Forge records a terminal rejection and the consumed authorization
in the report rather than leaving the request in an ambiguous pending state.

## Model Registry and Deployment Boundary

The V1 `ModelRegistry` uses `Forge/models/registry.sqlite3` when started through
`ForgeAgent` as its transactional, process-safe source of truth for model and
experiment metadata. On first startup it imports the legacy JSON registries in
one transaction and fails closed on
malformed input. The JSON files remain as recovery inputs. Model artifacts are
loaded only when their digest matches the registry and the resolved path remains
inside the artifact directory; legacy artifacts without a recorded digest remain
unloadable until reviewed and re-registered. Legacy `production` statuses are
imported as candidates with the old status retained as an unverified audit field.
Registry startup upgrades schema version 1 to 2 by adding the durable Chronicle
knowledge-outbox table; the migration is additive and preserves existing model
and experiment records.
Direct production status changes
are disabled: the legacy `benchmark_passed` boolean is not proof of a benchmark
or deployment authorization.

`Forge.core.authorization.Ed25519AuthorizationVerifier` verifies canonical
Ed25519-signed artifacts against explicitly configured issuer public keys and
audiences, then consumes the nonce in a durable SQLite ledger. It creates no
keys and trusts no default issuer; an Aegis signer and operator-managed key
configuration must be provisioned before the verifier can authorize changes.

`Forge.deployment.controller.DeploymentController` now records an authorized
shadow → canary → production state machine, deterministic canary routing, and
rollback to the exact prior active model. Each transition requires a prepared,
decision-bound authorization request and a trusted health assessment no older
than five minutes at both authorization preparation and transition commit.
Expired health evidence expires the pending request and is added to the audit
chain. The
controller exposes a hash-chained local event ledger and integrity check; the
serving path `model.predict_active` consumes its route. Signed nonces are
consumed after locked state checks at each mutation boundary. Rejected and
failed authorized operations are also written to the hash-chained ledger with
the authorization ID, failure type, and nonce-consumption state; token contents
are excluded. Deployments remain
fail-closed until trusted Aegis public keys are configured and a runtime
health-gate adapter is injected at startup. The controller records routes but cannot
enforce them in external serving systems that bypass Forge's agent API. Within
Forge's agent API, `model.predict` now rejects any model that is not currently
active for its domain; `model.predict_active` applies shadow/canary routing.
Both serving tasks load model bytes only when the registry digest matches the
artifact digest recorded in the active or in-progress authorized deployment.
Forge permits one in-progress rollout per domain; if legacy or corrupted state
contains multiple rollouts, routing fails closed until it is reconciled.
Consumers that call `ModelRegistry.load_model()` directly remain responsible
for enforcing the deployment route themselves. V2 ML experiment artifacts keep
their original V1 registry experiment ID and receive separate V2 experiment,
run, result, and optional dataset-fingerprint lineage. Rollout validation
checks those V2 IDs against the benchmark report before authorization.

Cross-agent reuse checks fail closed when target types, requested data
permissions, runtime compatibility, or license compatibility are unknown.
Runtime and license decisions require an operator-owned policy injected through
`ForgeAgent(reuse_compatibility_policy=...)`; Forge does not infer them from an
artifact's self-description. Reuse approval is refused for unchecked evidence
or if the registered artifact changes after the compatibility check. Conditional
adaptation or revalidation approvals must record conditions.
After a compatibility check, reuse approval requires a one-time signed
`forge.reuse` authorization. Its claims bind the check and artifact digests,
requesting agent, target domain, conditions, and authorizer. The verified
authorization receipt is retained with the reuse record; changed evidence or a
replayed nonce blocks approval. Compatibility checks expire for approval after
five minutes, and expiration is rechecked inside the approval transaction.
The console loads the policy from the operator configured
`FORGE_REUSE_COMPATIBILITY_POLICY=package.module:factory`; it must implement
`operational_status()`, `version_compatible(artifact, target_domain)`, and
`licensing_clear(artifact, requesting_agent, target_domain)`. The policy status
must report `{"ready": true}` for governed reuse readiness. Reuse readiness and
its blockers are included in the operational snapshot.

The console entry point loads a deployment health observer only when the
operator sets `FORGE_DEPLOYMENT_HEALTH_GATE` to a trusted installed factory,
formatted as `package.module:factory`. The factory must return an object with
`operational_status()` and `assess(deployment, target_phase)` methods. The
assessment must provide a `HealthAssessment` with non-empty, JSON-safe evidence.
Its `operational_status()` must return promptly with `{"ready": true}` only
when the telemetry source is reachable and usable; use bounded I/O timeouts.
If the adapter owns clients or worker threads, it may expose `close()`; Forge
calls it during startup-failure cleanup, console shutdown, and after the
one-shot status probe. `close()` should use bounded waits and return promptly.
Forge does not ship a pretend local observer: without a configured and healthy
runtime adapter, deployment remains not ready and rollout advancement fails
closed. The adapter must measure the actual serving system's domain-specific
SLOs and candidate health; configuring an import path alone does not establish
those controls.

```powershell
$env:FORGE_DEPLOYMENT_HEALTH_GATE = 'company_runtime.forge_health:create_gate'
```

Deployment readiness also verifies the SQLite audit hash chain and cross-checks
each serialized deployment record against its indexed columns and lifecycle
state. Active model pointers must resolve to a completed production deployment
for the same domain and model. A failed integrity or consistency check makes
the controller not ready; investigate and reconcile durable state before
allowing rollout transitions. These checks detect inconsistent stored state,
but the hash chain is not a signed or externally anchored audit log and does
not protect against an attacker who can rewrite the database and recompute the
chain.

## Durable-State Backup and Recovery

`Forge.core.recovery` creates a new backup directory for Forge's durable state:
the champion and challenge ledgers, deployments, drift investigations,
experiments, model registry and artifacts, authorization nonces, reuse registry,
training requests, and Forge's local memory and hypothesis records. SQLite
databases are copied with SQLite's online backup API, including committed
transactions in WAL files. The manifest records the
size and SHA-256 digest of every saved file; `verify` checks the inventory,
digests, and SQLite integrity. The tool refuses to overwrite a previous backup,
follow symbolic links, or create a backup inside Forge's state directory.

```powershell
python -m Forge.core.recovery backup D:\ForgeBackups\forge-2026-10-19
python -m Forge.core.recovery verify D:\ForgeBackups\forge-2026-10-19
python -m Forge.core.recovery restore D:\ForgeBackups\forge-2026-10-19 D:\ForgeRestore\Forge
```

Each SQLite store is transactionally consistent on its own. Pause Forge writes
while creating a backup when recovery requires champion, challenge, experiment,
and deployment records to represent one point in time. Copy completed backups
to access-controlled storage on a separate machine or service; a local copy does
not protect against disk loss or host compromise. Before production use, define
retention and encryption controls, and perform documented restore drills into
an isolated Forge checkout: restore into a new state directory, copy those
verified stores into an isolated checkout, then start Forge there and verify
its ledgers and readiness output. Restore refuses to overwrite an existing
destination. The manifest digest detects accidental changes but does not
authenticate who created a backup. Datasets referenced by experiment contracts may live outside Forge;
their retention and access controls must be managed by the dataset owner. The
inventory follows Forge's default local paths; custom storage directories must
be added to the backup procedure by the deployment operator.

## Run
```bash
python main.py
python main.py --status-json
```

`--status-json` emits a versioned, point-in-time readiness snapshot. It checks
Forge's local ledgers and configured integrations without starting the Forge
agent loop, Chronicle, or Atlas; it does not report liveness of a separately
running Forge process. It exits `0` when the full platform is ready, `1` when
the snapshot reports one or more readiness blockers, and `2` when the probe
itself cannot be completed. Supervisors can use it for readiness checks, not as
a continuous service monitor.

Built on the constitutional BaseAgent (reasoning, learning, and memory). See
`constitutional/COMPLIANCE.md` for the governing requirements; the V2 controls
above describe current capabilities and known limitations, not a certification.
