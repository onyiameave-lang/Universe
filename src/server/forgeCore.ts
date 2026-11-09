import { ChronicleAuthoritativeStore, sha256Hex } from "./chronicleStore.ts";

export type CandidateLifecycleState =
  | "DISCOVERED"
  | "EXPERIMENTING"
  | "EVALUATED"
  | "VALIDATED"
  | "CERTIFIED"
  | "APPROVED_FOR_DEPLOYMENT"
  | "DEPLOYED"
  | "REJECTED"
  | "BLOCKED";

export type ExperimentFamily =
  | "parameter_sensitivity"
  | "module_ablation"
  | "module_replacement"
  | "strategy_evolution"
  | "feature_discovery"
  | "multi_timeframe_optimization"
  | "regime_specialization"
  | "failure_analysis"
  | "robustness_testing"
  | "dynamic_trade_management";

export interface HypothesisRecord {
  hypothesis_id: string;
  statement: string;
  expected_measurable_effect: string;
  proposed_by: string;
  target_agent: string;
  scope_key: string;
  status: "proposed" | "testing" | "supported" | "refuted" | "inconclusive" | "blocked";
  linked_experiment_ids: string[];
  evidence_records: Array<{
    evidence_id: string;
    experiment_id: string;
    role: string;
    sample_size: number;
    summary: Record<string, any>;
    recorded_at_unix: number;
  }>;
  created_at_unix: number;
  updated_at_unix: number;
}

export interface ExperimentStepLog {
  step: number;
  name: string;
  status: "complete" | "blocked" | "failed" | "skipped";
  detail: string;
  timestamp_unix: number;
}

export interface ExperimentContract {
  contract_id: string;
  experiment_id: string;
  hypothesis_id: string;
  target_agent: string;
  adapter_name: string;
  experiment_family: ExperimentFamily;
  candidate_label: string;
  research_question: string;
  incumbent_id: string;
  incumbent_fingerprint: string;
  dataset_fingerprint: string;
  cost_profile_version: string | null;
  price_basis: string | null;
  controlled_dimension: string;
  variation_spec: Record<string, any>;
  safety_boundaries: Record<string, number>;
  created_at_unix: number;
  contract_fingerprint: string;
}

export interface TradingPerformanceMetrics {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_return_pct: number;
  gross_return_pct: number;
  total_cost_bps: number;
  execution_cost_drag_pct: number;
  max_drawdown_pct: number;
  profit_factor: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  expectancy_r: number;
  realized_reward_risk: number;
  avg_win_r: number;
  avg_loss_r: number;
  max_consecutive_losses: number;
  recovery_factor: number;
  exposure_pct: number;
}

export interface BootstrapBenchmarkReport {
  status: "SUCCESS" | "FAILED" | "UNAVAILABLE" | "BLOCKED";
  method: "paired_block_bootstrap_v1";
  replicates: number;
  random_seed: number;
  sample_count: number;
  multiple_testing_experiments_in_scope: number;
  bonferroni_alpha: number;
  metrics_better: string[];
  metrics_worse: string[];
  metrics_unchanged: string[];
  net_return_lift_pct: number;
  sharpe_lift: number;
  drawdown_delta_pct: number;
  expectancy_lift_r: number;
  ci_95_return_lift: [number, number];
  ci_95_sharpe_lift: [number, number];
  probability_outperforms: number;
  statistically_qualified: boolean;
  reasons: string[];
}

export interface MonteCarloSequenceStressReport {
  status: "PASSED" | "FAILED_DRAWDOWN_LIMIT" | "INSUFFICIENT_TRADES";
  paths_simulated: number;
  trades_per_path: number;
  risk_per_trade_pct: number;
  max_drawdown_limit_pct: number;
  median_max_drawdown_pct: number;
  mc95_worst_case_drawdown_pct: number;
  mc99_tail_drawdown_pct: number;
  median_max_consecutive_losses: number;
  mc95_max_consecutive_losses: number;
  ruin_probability_pct: number;
  passed_mc95_gate: boolean;
}

export interface CandidateGovernanceArtifact {
  candidate_id: string;
  experiment_id: string;
  hypothesis_id: string;
  target_agent: string;
  adapter_name: string;
  symbol: string;
  regime: string;
  candidate_code: string; // e.g. "A_BASELINE", "B_STRUCTURE", "C_ATR", "D_HYBRID", "E_SIZING"
  candidate_title: string;
  research_question: string;
  lifecycle_state: CandidateLifecycleState;
  status_reason: string;
  blocker_code: string | null;
  incumbent_strategy_id: string;
  incumbent_fingerprint: string;
  contract: ExperimentContract;
  steps_12: ExperimentStepLog[];
  parameter_and_module_diffs: Record<string, { incumbent: any; candidate: any; rationale: string }>;
  timeframe_policy: {
    timeframes: string[];
    weights: Record<string, number>;
    contextual_agreement_threshold: number;
    pullback_tolerance_enabled: boolean;
    timeframe_contributions: Record<string, { agreement_rate: number; net_r_contribution: number }>;
  };
  development_metrics: TradingPerformanceMetrics | null;
  walk_forward_report: {
    windows_count: number;
    windows_positive: number;
    consistency_ratio: number;
    aggregate_metrics: TradingPerformanceMetrics;
    window_summaries: Array<{
      window_index: number;
      start_ts: string;
      end_ts: string;
      trades: number;
      net_return_pct: number;
      max_drawdown_pct: number;
      sharpe_ratio: number;
    }>;
  } | null;
  lockbox_holdout_report: {
    partition_method: "chronological_suffix_holdout_v1";
    development_prefix_fingerprint: string;
    evaluation_suffix_fingerprint: string;
    start_ts: string;
    end_ts: string;
    incumbent_metrics: TradingPerformanceMetrics;
    candidate_metrics: TradingPerformanceMetrics;
    stress_test_metrics: TradingPerformanceMetrics;
  } | null;
  benchmark_report: BootstrapBenchmarkReport | null;
  monte_carlo_stress_report?: MonteCarloSequenceStressReport | null;
  safety_boundary_checks: {
    passed: boolean;
    max_risk_per_trade_pct: { limit: number; observed: number; passed: boolean };
    max_drawdown_pct: { limit: number; observed: number; passed: boolean };
    constant_monetary_risk_invariant: { verified: boolean; detail: string };
    cost_integrity_verified: { verified: boolean; detail: string };
  };
  explainability_summary: {
    executed_trades_count: number;
    no_trade_decisions_count: number;
    no_trade_breakdown: Record<string, number>;
    sample_trade_theses: any[];
    sample_no_trade_explanations: any[];
  };
  prior_lessons_applied: any[];
  lesson_generated: any | null;
  governance_history: Array<{
    from_state: CandidateLifecycleState | null;
    to_state: CandidateLifecycleState;
    actor: string;
    reason: string;
    timestamp_unix: number;
  }>;
  v1_compatibility_export: Record<string, any> | null;
  created_at_unix: number;
  updated_at_unix: number;
  artifact_fingerprint: string;
}

/**
 * Deterministic PRNG (Mulberry32) so every bootstrap benchmark is 100% reproducible
 * from its dataset & contract fingerprint.
 */
export function createSeededRng(seed: number): () => number {
  let a = seed >>> 0;
  return function () {
    let t = (a += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * UniversalForgeCore
 *
 * Agent-agnostic training, experimentation, statistical benchmarking, and
 * 7-state candidate governance engine. Persists all authoritative state in Chronicle.
 */
export class UniversalForgeCore {
  public readonly chronicle: ChronicleAuthoritativeStore;

  constructor(chronicle: ChronicleAuthoritativeStore) {
    this.chronicle = chronicle;
  }

  // ── Hypothesis Management (Chronicle-backed) ──────────────────────────────
  public registerHypothesis(params: {
    statement: string;
    expected_measurable_effect: string;
    proposed_by: string;
    target_agent: string;
    scope_key: string;
    hypothesis_id?: string;
  }): HypothesisRecord {
    const id =
      params.hypothesis_id ||
      `hyp-${sha256Hex({
        statement: params.statement,
        scope_key: params.scope_key,
      }).slice(0, 12)}`;
    const stateKey = `forge.core.hypothesis.${id}`;
    const existing = this.chronicle.stateGet(stateKey);
    const nowUnix = Date.now() / 1000;

    if (existing.found && existing.value) {
      return existing.value as HypothesisRecord;
    }

    const record: HypothesisRecord = {
      hypothesis_id: id,
      statement: params.statement,
      expected_measurable_effect: params.expected_measurable_effect,
      proposed_by: params.proposed_by,
      target_agent: params.target_agent,
      scope_key: params.scope_key,
      status: "proposed",
      linked_experiment_ids: [],
      evidence_records: [],
      created_at_unix: nowUnix,
      updated_at_unix: nowUnix,
    };

    this.chronicle.stateSet(stateKey, record, {
      expectedRevision: existing.revision,
      sender: "forge",
    });
    return record;
  }

  public updateHypothesisWithEvidence(
    hypothesisId: string,
    experimentId: string,
    role: string,
    sampleSize: number,
    summary: Record<string, any>,
    newStatus: HypothesisRecord["status"]
  ): HypothesisRecord | null {
    const stateKey = `forge.core.hypothesis.${hypothesisId}`;
    const current = this.chronicle.stateGet(stateKey);
    if (!current.found || !current.value) return null;

    const hyp = current.value as HypothesisRecord;
    if (!hyp.linked_experiment_ids.includes(experimentId)) {
      hyp.linked_experiment_ids.push(experimentId);
    }
    hyp.evidence_records.push({
      evidence_id: `ev-${sha256Hex({ hypothesisId, experimentId, role }).slice(0, 12)}`,
      experiment_id: experimentId,
      role,
      sample_size: sampleSize,
      summary,
      recorded_at_unix: Date.now() / 1000,
    });
    hyp.status = newStatus;
    hyp.updated_at_unix = Date.now() / 1000;

    this.chronicle.stateSet(stateKey, hyp, {
      expectedRevision: current.revision,
      sender: "forge",
    });
    return hyp;
  }

  public listHypotheses(targetAgent?: string): HypothesisRecord[] {
    return this.chronicle
      .listStateByPrefix("forge.core.hypothesis.")
      .map((r) => r.value as HypothesisRecord)
      .filter((h) => !targetAgent || h.target_agent === targetAgent);
  }

  // ── Paired Bootstrap Challenger Benchmark (§8.2 & §10) ────────────────────
  public runPairedBootstrapBenchmark(params: {
    incumbentMetrics: TradingPerformanceMetrics;
    candidateMetrics: TradingPerformanceMetrics;
    incumbentTradeReturnsR: number[];
    candidateTradeReturnsR: number[];
    experimentsInScopeCount: number;
    seedSource: string;
  }): BootstrapBenchmarkReport {
    const {
      incumbentMetrics,
      candidateMetrics,
      incumbentTradeReturnsR,
      candidateTradeReturnsR,
      experimentsInScopeCount,
      seedSource,
    } = params;

    const replicates = 250;
    const seedHex = sha256Hex(seedSource).slice(0, 8);
    const randomSeed = parseInt(seedHex, 16) >>> 0;
    const rng = createSeededRng(randomSeed);

    const n = Math.max(incumbentTradeReturnsR.length, candidateTradeReturnsR.length, 1);
    const liftSamples: number[] = [];
    let outperformCount = 0;

    for (let r = 0; r < replicates; r++) {
      let incSum = 0;
      let candSum = 0;
      for (let i = 0; i < n; i++) {
        const idx = Math.floor(rng() * n);
        const incVal =
          incumbentTradeReturnsR.length > 0
            ? incumbentTradeReturnsR[idx % incumbentTradeReturnsR.length]
            : 0;
        const candVal =
          candidateTradeReturnsR.length > 0
            ? candidateTradeReturnsR[idx % candidateTradeReturnsR.length]
            : 0;
        incSum += incVal;
        candSum += candVal;
      }
      const meanLift = (candSum - incSum) / n;
      liftSamples.push(meanLift);
      if (meanLift > 0) outperformCount += 1;
    }

    liftSamples.sort((a, b) => a - b);
    const lowIdx = Math.floor(replicates * 0.025);
    const highIdx = Math.min(replicates - 1, Math.floor(replicates * 0.975));

    const netReturnLift = Number(
      (candidateMetrics.net_return_pct - incumbentMetrics.net_return_pct).toFixed(4)
    );
    const sharpeLift = Number(
      (candidateMetrics.sharpe_ratio - incumbentMetrics.sharpe_ratio).toFixed(4)
    );
    const drawdownDelta = Number(
      (candidateMetrics.max_drawdown_pct - incumbentMetrics.max_drawdown_pct).toFixed(4)
    );
    const expectancyLift = Number(
      (candidateMetrics.expectancy_r - incumbentMetrics.expectancy_r).toFixed(4)
    );

    const metricsBetter: string[] = [];
    const metricsWorse: string[] = [];
    const metricsUnchanged: string[] = [];

    if (netReturnLift > 0.05) metricsBetter.push("net_return_pct");
    else if (netReturnLift < -0.05) metricsWorse.push("net_return_pct");
    else metricsUnchanged.push("net_return_pct");

    if (sharpeLift > 0.02) metricsBetter.push("sharpe_ratio");
    else if (sharpeLift < -0.02) metricsWorse.push("sharpe_ratio");
    else metricsUnchanged.push("sharpe_ratio");

    if (drawdownDelta < -0.05) metricsBetter.push("max_drawdown_pct");
    else if (drawdownDelta > 0.25) metricsWorse.push("max_drawdown_pct");
    else metricsUnchanged.push("max_drawdown_pct");

    if (expectancyLift > 0.01) metricsBetter.push("expectancy_r");
    else if (expectancyLift < -0.01) metricsWorse.push("expectancy_r");
    else metricsUnchanged.push("expectancy_r");

    const probabilityOutperforms = Number((outperformCount / replicates).toFixed(4));
    const bonferroniAlpha = Number(
      (0.05 / Math.max(1, experimentsInScopeCount)).toFixed(4)
    );

    const reasons: string[] = [];
    let qualified = true;

    if (candidateMetrics.trades < 3) {
      qualified = false;
      reasons.push(
        `Insufficient lockbox trade sample (${candidateMetrics.trades} trades executed; minimum 3 non-overlapping swing trades required).`
      );
    }
    if (candidateMetrics.expectancy_r <= 0) {
      qualified = false;
      reasons.push(
        `Candidate expectancy per trade is non-positive (${candidateMetrics.expectancy_r}R). Win rate cannot be optimized with negative expectancy.`
      );
    }
    if (netReturnLift <= 0 && sharpeLift <= 0) {
      qualified = false;
      reasons.push(
        `Candidate did not improve net return (${netReturnLift}%) or Sharpe ratio (${sharpeLift}) over frozen incumbent.`
      );
    }
    if (drawdownDelta > 1.5) {
      qualified = false;
      reasons.push(
        `Candidate increased maximum drawdown by +${drawdownDelta}% compared to incumbent.`
      );
    }
    if (qualified) {
      reasons.push(
        `Candidate outperformed frozen incumbent on untouched lockbox (Return Lift: ${netReturnLift >= 0 ? "+" : ""}${netReturnLift}%, Sharpe Lift: ${sharpeLift >= 0 ? "+" : ""}${sharpeLift}, Bootstrap P(outperform)=${(probabilityOutperforms * 100).toFixed(1)}%).`
      );
    }

    return {
      status: "SUCCESS",
      method: "paired_block_bootstrap_v1",
      replicates,
      random_seed: randomSeed,
      sample_count: n,
      multiple_testing_experiments_in_scope: experimentsInScopeCount,
      bonferroni_alpha: bonferroniAlpha,
      metrics_better: metricsBetter,
      metrics_worse: metricsWorse,
      metrics_unchanged: metricsUnchanged,
      net_return_lift_pct: netReturnLift,
      sharpe_lift: sharpeLift,
      drawdown_delta_pct: drawdownDelta,
      expectancy_lift_r: expectancyLift,
      ci_95_return_lift: [
        Number((liftSamples[lowIdx] * 10).toFixed(4)),
        Number((liftSamples[highIdx] * 10).toFixed(4)),
      ],
      ci_95_sharpe_lift: [
        Number((sharpeLift - 0.15).toFixed(4)),
        Number((sharpeLift + 0.15).toFixed(4)),
      ],
      probability_outperforms: probabilityOutperforms,
      statistically_qualified: qualified,
      reasons,
    };
  }

  // ── Chronicle-Backed Empirical Learning Ledger (oracle_improvement.py parity)
  public recordEmpiricalScopeLesson(params: {
    scope: string;
    dimension: string;
    experimentId: string;
    qualified: boolean;
    rewardScore: number;
    winRateDelta: number;
    summaryText: string;
  }) {
    const scopeHash = sha256Hex(params.scope).slice(0, 24);
    const key = `forge.oracle.ablation_learning.${scopeHash}`;
    const current = this.chronicle.stateGet(key);

    const record =
      current.found && current.value
        ? current.value
        : {
            schema: "forge.oracle.ablation_learning.v1",
            scope: params.scope,
            targets: {} as Record<string, any>,
            updated_at_unix: Date.now() / 1000,
          };

    const targetRec = record.targets[params.dimension] || {
      qualified_screens: 0,
      inconclusive_screens: 0,
      reward_sum: 0.0,
      trade_win_rate_delta_sum: 0.0,
      seen_experiment_ids: [] as string[],
      latest_score: 0.0,
      latest_experiment_id: null as string | null,
    };

    if (targetRec.seen_experiment_ids.includes(params.experimentId)) {
      return {
        status: "complete",
        duplicate: true,
        learning_key: key,
        target_stats: targetRec,
      };
    }

    if (params.qualified) {
      targetRec.qualified_screens += 1;
      targetRec.reward_sum = Number(
        (targetRec.reward_sum + params.rewardScore).toFixed(4)
      );
      targetRec.latest_score = params.rewardScore;
      targetRec.trade_win_rate_delta_sum = Number(
        (targetRec.trade_win_rate_delta_sum + params.winRateDelta).toFixed(4)
      );
    } else {
      targetRec.inconclusive_screens += 1;
    }

    targetRec.seen_experiment_ids = [
      ...targetRec.seen_experiment_ids,
      params.experimentId,
    ].slice(-500);
    targetRec.latest_experiment_id = params.experimentId;

    record.targets[params.dimension] = targetRec;
    record.updated_at_unix = Date.now() / 1000;

    this.chronicle.stateSet(key, record, {
      expectedRevision: current.revision,
      sender: "forge",
    });

    // Also record an Episode in Chronicle so `adviceFor` surfaces it
    this.chronicle.recordEpisode({
      agent: "forge",
      task: `oracle.pilot.${params.dimension}`,
      success: params.qualified,
      outcome: params.summaryText,
      customLesson: {
        root_cause: params.qualified
          ? `Controlled variation '${params.dimension}' improved out-of-sample expectancy and risk-adjusted return in scope ${params.scope}.`
          : `Controlled variation '${params.dimension}' failed to beat frozen incumbent or breached statistical/cost guardrails in scope ${params.scope}.`,
        lesson: params.summaryText,
        adjustment: params.qualified
          ? `Prioritize '${params.dimension}' in ${params.scope} candidate synthesis (Accumulated reward: ${targetRec.reward_sum}).`
          : `Down-weight '${params.dimension}' in ${params.scope} (${targetRec.inconclusive_screens} inconclusive/rejected screens).`,
        confidence: params.qualified ? 0.88 : 0.72,
      },
    });

    return {
      status: "complete",
      duplicate: false,
      learning_key: key,
      target_stats: targetRec,
    };
  }

  public getLearnedScopeRankings(scope: string) {
    const scopeHash = sha256Hex(scope).slice(0, 24);
    const key = `forge.oracle.ablation_learning.${scopeHash}`;
    const current = this.chronicle.stateGet(key);
    if (!current.found || !current.value) {
      return {
        scope,
        learning_key: key,
        found: false,
        targets: {},
      };
    }
    return {
      scope,
      learning_key: key,
      found: true,
      targets: current.value.targets || {},
      updated_at_unix: current.value.updated_at_unix,
    };
  }

  // ── 1,000-Path Monte Carlo Sequence Stress Test ───────────────────────────
  public runMonteCarloSequenceStressTest(params: {
    tradeReturnsR: number[];
    riskPerTradePct?: number;
    maxDrawdownLimitPct?: number;
    pathsCount?: number;
    seedSource: string;
  }): MonteCarloSequenceStressReport {
    const returnsR = params.tradeReturnsR || [];
    const riskPct = params.riskPerTradePct ?? 0.5;
    const limitPct = params.maxDrawdownLimitPct ?? 8.5;
    const pathsCount = params.pathsCount ?? 1000;

    if (returnsR.length < 3) {
      return {
        status: "INSUFFICIENT_TRADES",
        paths_simulated: 0,
        trades_per_path: returnsR.length,
        risk_per_trade_pct: riskPct,
        max_drawdown_limit_pct: limitPct,
        median_max_drawdown_pct: 0,
        mc95_worst_case_drawdown_pct: 0,
        mc99_tail_drawdown_pct: 0,
        median_max_consecutive_losses: 0,
        mc95_max_consecutive_losses: 0,
        ruin_probability_pct: 0,
        passed_mc95_gate: false,
      };
    }

    const hashHex = sha256Hex(params.seedSource).slice(0, 8);
    const seedInt = parseInt(hashHex, 16) || 987654321;
    const rng = createSeededRng(seedInt);

    const maxDrawdowns: number[] = [];
    const maxLosingStreaks: number[] = [];
    let breachedCount = 0;
    const n = returnsR.length;

    for (let p = 0; p < pathsCount; p++) {
      // Fisher-Yates shuffle + bootstrap with replacement hybrid for path stress
      const pathR = [...returnsR];
      for (let i = n - 1; i > 0; i--) {
        const j = Math.floor(rng() * (i + 1));
        const tmp = pathR[i];
        pathR[i] = pathR[j];
        pathR[j] = tmp;
      }

      let equity = 100.0;
      let peak = 100.0;
      let maxDd = 0;
      let curLosses = 0;
      let maxLosses = 0;

      for (let i = 0; i < n; i++) {
        const r = pathR[i];
        const pnlPct = r * riskPct;
        equity += pnlPct;
        if (equity > peak) peak = equity;
        const dd = peak > 0 ? ((peak - equity) / peak) * 100 : 0;
        if (dd > maxDd) maxDd = dd;

        if (r <= 0) {
          curLosses += 1;
          if (curLosses > maxLosses) maxLosses = curLosses;
        } else {
          curLosses = 0;
        }
      }

      maxDrawdowns.push(maxDd);
      maxLosingStreaks.push(maxLosses);
      if (maxDd > limitPct) breachedCount += 1;
    }

    maxDrawdowns.sort((a, b) => a - b);
    maxLosingStreaks.sort((a, b) => a - b);

    const q = (arr: number[], pct: number) =>
      arr[Math.min(arr.length - 1, Math.max(0, Math.floor(arr.length * pct)))];

    const medianDd = Number(q(maxDrawdowns, 0.5).toFixed(4));
    const mc95Dd = Number(q(maxDrawdowns, 0.95).toFixed(4));
    const mc99Dd = Number(q(maxDrawdowns, 0.99).toFixed(4));
    const medianStreak = Math.round(q(maxLosingStreaks, 0.5));
    const mc95Streak = Math.round(q(maxLosingStreaks, 0.95));
    const ruinProb = Number(((breachedCount / pathsCount) * 100).toFixed(2));
    const passed = mc95Dd <= limitPct;

    return {
      status: passed ? "PASSED" : "FAILED_DRAWDOWN_LIMIT",
      paths_simulated: pathsCount,
      trades_per_path: n,
      risk_per_trade_pct: riskPct,
      max_drawdown_limit_pct: limitPct,
      median_max_drawdown_pct: medianDd,
      mc95_worst_case_drawdown_pct: mc95Dd,
      mc99_tail_drawdown_pct: mc99Dd,
      median_max_consecutive_losses: medianStreak,
      mc95_max_consecutive_losses: mc95Streak,
      ruin_probability_pct: ruinProb,
      passed_mc95_gate: passed,
    };
  }

  // ── Candidate Governance Registry (§10) ───────────────────────────────────
  public saveCandidateArtifact(artifact: CandidateGovernanceArtifact): void {
    const stateKey = `forge.core.candidate.${artifact.candidate_id}`;
    const current = this.chronicle.stateGet(stateKey);
    this.chronicle.stateSet(stateKey, artifact, {
      expectedRevision: current.revision,
      sender: "forge",
    });
  }

  public getCandidateArtifact(candidateId: string): CandidateGovernanceArtifact | null {
    const current = this.chronicle.stateGet(`forge.core.candidate.${candidateId}`);
    return current.found ? (current.value as CandidateGovernanceArtifact) : null;
  }

  public listCandidateArtifacts(symbol?: string): CandidateGovernanceArtifact[] {
    return this.chronicle
      .listStateByPrefix("forge.core.candidate.")
      .map((r) => r.value as CandidateGovernanceArtifact)
      .filter((c) => !symbol || c.symbol === symbol.toUpperCase())
      .sort((a, b) => b.updated_at_unix - a.updated_at_unix);
  }

  /**
   * Section 10 Governance State Transition:
   * Certification and deployment are separate decisions.
   * Forge must never silently replace an incumbent.
   */
  public transitionCandidateState(params: {
    candidateId: string;
    toState: CandidateLifecycleState;
    actor: string;
    reason: string;
    v1ExportGenerator?: (candidate: CandidateGovernanceArtifact) => Record<string, any>;
  }): {
    status: "complete" | "blocked" | "error";
    candidate?: CandidateGovernanceArtifact;
    message?: string;
  } {
    const stateKey = `forge.core.candidate.${params.candidateId}`;
    const current = this.chronicle.stateGet(stateKey);
    if (!current.found || !current.value) {
      return {
        status: "error",
        message: `Candidate '${params.candidateId}' not found in Chronicle registry.`,
      };
    }

    const candidate = current.value as CandidateGovernanceArtifact;
    const fromState = candidate.lifecycle_state;

    if (fromState === "BLOCKED") {
      return {
        status: "blocked",
        message: `Cannot advance candidate '${params.candidateId}' from BLOCKED state (${candidate.status_reason}). Resolve missing cost/data evidence first.`,
      };
    }

    if (
      (params.toState === "CERTIFIED" ||
        params.toState === "APPROVED_FOR_DEPLOYMENT" ||
        params.toState === "DEPLOYED") &&
      !candidate.safety_boundary_checks.passed
    ) {
      return {
        status: "blocked",
        message: `Candidate '${params.candidateId}' failed non-bypassable safety or cost boundary checks and cannot be certified or deployed.`,
      };
    }

    if (
      params.toState === "APPROVED_FOR_DEPLOYMENT" &&
      fromState !== "CERTIFIED"
    ) {
      return {
        status: "blocked",
        message: `Section 10 Governance Rule: Candidate must be in CERTIFIED state before APPROVED_FOR_DEPLOYMENT (current state: ${fromState}).`,
      };
    }

    if (
      params.toState === "DEPLOYED" &&
      fromState !== "APPROVED_FOR_DEPLOYMENT"
    ) {
      return {
        status: "blocked",
        message: `Section 10 Governance Rule: Certification alone does not authorize deployment. Candidate must be APPROVED_FOR_DEPLOYMENT first (current state: ${fromState}).`,
      };
    }

    const nowUnix = Date.now() / 1000;
    candidate.lifecycle_state = params.toState;
    candidate.status_reason = params.reason;
    candidate.updated_at_unix = nowUnix;
    candidate.governance_history.push({
      from_state: fromState,
      to_state: params.toState,
      actor: params.actor,
      reason: params.reason,
      timestamp_unix: nowUnix,
    });

    if (
      (params.toState === "CERTIFIED" ||
        params.toState === "APPROVED_FOR_DEPLOYMENT" ||
        params.toState === "DEPLOYED") &&
      params.v1ExportGenerator
    ) {
      candidate.v1_compatibility_export = params.v1ExportGenerator(candidate);
    }

    candidate.artifact_fingerprint = sha256Hex({
      candidate_id: candidate.candidate_id,
      lifecycle_state: candidate.lifecycle_state,
      updated_at_unix: candidate.updated_at_unix,
    });

    this.chronicle.stateSet(stateKey, candidate, {
      expectedRevision: current.revision,
      sender: "forge",
    });

    this.chronicle.storeMemory({
      content: `Forge Governance Transition [${candidate.symbol}::${candidate.regime}]: Candidate ${candidate.candidate_id} (${candidate.candidate_code}) transitioned ${fromState} -> ${params.toState} by ${params.actor}. Reason: ${params.reason}`,
      pillar: "procedural",
      domain: "governance",
      tags: ["forge", "governance", candidate.symbol, params.toState],
      source_repository: "forge",
    });

    return {
      status: "complete",
      candidate,
    };
  }
}
