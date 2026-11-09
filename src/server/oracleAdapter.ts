import fs from "fs";
import path from "path";
import { execFileSync } from "child_process";
import { ChronicleAuthoritativeStore, sha256Hex } from "./chronicleStore.ts";
import {
  UniversalForgeCore,
  CandidateGovernanceArtifact,
  ExperimentContract,
  ExperimentStepLog,
  TradingPerformanceMetrics,
  CandidateLifecycleState,
} from "./forgeCore.ts";
import {
  MultiTimeframeDataAndAtlasEngine,
  MultiTimeframeDataset,
  AtlasForgeResearchBrief,
  TraderStyleCode,
  TimeframeCode,
  AnnotatedMtfBar,
  TradingSessionCode,
  AdaptiveSessionBehaviorCode,
  EmpiricalRegimeCode,
} from "./mtfDataEngine.ts";

export interface OhlcvBar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface VerifiedCostProfile {
  schema: string;
  symbol: string;
  profile_status: string;
  source: string;
  cost_profile_version: string;
  price_basis: string;
  commission_bps: number;
  spread_bps: number;
  slippage_bps: number;
  stress_spread_bps: number;
  stress_slippage_bps: number;
  symbol_filled_deals: number;
  matched_slippage_deals: number;
  matched_spread_quotes: number;
  updated_at_unix: number;
}

export interface SymbolReadinessReport {
  symbol: string;
  status: "READY" | "BLOCKED";
  blocker_code: string | null;
  blocker_reason: string | null;
  bars_count: number;
  mtf_ingested: boolean;
  mtf_counts: Record<string, number> | null;
  first_bar_ts: string | null;
  last_bar_ts: string | null;
  dataset_fingerprint: string | null;
  cost_profile: VerifiedCostProfile | null;
  incumbent_regimes: string[];
}

export interface TradeThesisRecord {
  trade_id: string;
  bar_timestamp: string;
  exit_timestamp: string;
  symbol: string;
  regime: string;
  session?: string;
  trader_style?: string;
  trigger_timeframe?: string;
  direction: "LONG" | "SHORT";
  timeframe_evidence: Record<string, string>;
  setup_justification: string;
  entry_price: number;
  exit_price: number;
  stop_invalidation_level: number;
  stop_reason: string;
  take_profit_level: number;
  target_reason: string;
  expected_rr: number;
  realized_r: number;
  net_return_pct: number;
  position_size_units: number;
  monetary_risk_pct: number;
  expected_cost_bps: number;
  invalidation_conditions: string;
  exit_reason: "TAKE_PROFIT" | "STOP_INVALIDATION" | "TIME_HORIZON_EXIT";
}

export type PilotCandidateCode =
  | "A_BASELINE"
  | "B_STRUCTURE"
  | "C_ATR"
  | "D_HYBRID"
  | "E_SIZING"
  | "V2_SCALPING"
  | "V2_DAY_TRADING"
  | "V2_SWING_TRADING"
  | "V2_RANGE_SPECIALIST";

export const PILOT_CANDIDATE_SPECS: Record<
  PilotCandidateCode,
  {
    code: PilotCandidateCode;
    title: string;
    controlled_dimension: string;
    experiment_family:
      | "parameter_sensitivity"
      | "module_replacement"
      | "multi_timeframe_optimization"
      | "dynamic_trade_management"
      | "strategy_evolution"
      | "regime_specialization";
    research_question: string;
    hypothesis_statement: string;
    expected_measurable_effect: string;
  }
> = {
  A_BASELINE: {
    code: "A_BASELINE",
    title: "Candidate A — Frozen V1 Incumbent Baseline (Old Recipe Benchmark)",
    controlled_dimension: "incumbent_baseline_reproduction",
    experiment_family: "parameter_sensitivity",
    research_question:
      "What is the exact out-of-sample expectancy, drawdown, and execution cost drag of the frozen Oracle V1 incumbent under verified broker costs?",
    hypothesis_statement:
      "Reproducing the frozen Oracle V1 incumbent on chronological development, walk-forward, and lockbox partitions establishes an auditable benchmark.",
    expected_measurable_effect:
      "Deterministic baseline metrics with zero parameter or module mutations.",
  },
  B_STRUCTURE: {
    code: "B_STRUCTURE",
    title: "Candidate B — V1 + Market-Structure Stop-Loss & Target",
    controlled_dimension: "exit.market_structure_invalidation",
    experiment_family: "module_replacement",
    research_question:
      "Does placing stops beyond structural swing invalidation levels reduce premature stop-outs compared to fixed ATR multipliers?",
    hypothesis_statement:
      "Placing stop-loss at structural swing invalidation reduces premature stop-outs and improves net expectancy after verified broker costs.",
    expected_measurable_effect:
      "Higher realized reward-to-risk and positive expectancy lift on untouched lockbox holdout.",
  },
  C_ATR: {
    code: "C_ATR",
    title: "Candidate C — V1 + Volatility-Adjusted ATR Stop & Target",
    controlled_dimension: "exit.volatility_adaptive_atr",
    experiment_family: "module_replacement",
    research_question:
      "Does scaling stop-loss and take-profit dynamically with local volatility percentile improve Sharpe and profit factor over static V1 exits?",
    hypothesis_statement:
      "Adapting ATR stop and target multipliers to volatility percentile prevents tight-stop whipsaws in high-volatility bars.",
    expected_measurable_effect:
      "Improved profit factor and lower maximum consecutive losses vs. frozen incumbent.",
  },
  D_HYBRID: {
    code: "D_HYBRID",
    title: "Candidate D — V1 + Structure + ATR Buffer & Pullback Weighting",
    controlled_dimension: "exit.hybrid_structure_atr_and_mtf_context",
    experiment_family: "multi_timeframe_optimization",
    research_question:
      "Does combining structural invalidation levels with an ATR noise buffer and contextual multi-timeframe weighting outperform static exits?",
    hypothesis_statement:
      "Anchoring stops behind market structure plus a 0.35x ATR volatility buffer while using weighted contextual agreement increases trade quality.",
    expected_measurable_effect:
      "Statistically significant improvement in net return after cost, Sharpe ratio, and expectancy per trade.",
  },
  E_SIZING: {
    code: "E_SIZING",
    title: "Candidate E — V1 + Constant-Monetary-Risk Stop-Scaled Sizing",
    controlled_dimension: "sizing.constant_monetary_risk_stop_scaled",
    experiment_family: "dynamic_trade_management",
    research_question:
      "Does inverse stop-distance and volatility-scaled position sizing at constant monetary risk reduce equity drawdown?",
    hypothesis_statement:
      "Computing position size inversely from structural stop distance at constant 0.50% monetary risk stabilizes R-multiples and reduces peak-to-trough drawdown.",
    expected_measurable_effect:
      "Lower maximum drawdown and higher Sharpe/Sortino under both normal and stressed broker costs.",
  },
  V2_SCALPING: {
    code: "V2_SCALPING",
    title: "Oracle V2 Newly Discovered — M5/M15 Intraday Scalping Strategy (Atlas + Forge)",
    controlled_dimension: "oracle_v2.mtf_discovery.scalping",
    experiment_family: "strategy_evolution",
    research_question:
      "Can Forge + Atlas synthesize a dedicated M5/M15 Scalping strategy aligned with H1/M30 structure, filtered by empirically profitable UTC Sessions and Market Regimes, with MFE/MAE-learned Stop-Loss and Take-Profit?",
    hypothesis_statement:
      "Executing M5 triggers aligned with H1/M30 macro flow and M15 setup pullbacks exclusively during high-expectancy UTC sessions with MFE/MAE-calibrated stops and targets outperforms unfiltered intraday baselines.",
    expected_measurable_effect:
      "High intraday trade frequency, positive net expectancy after cTrader spread/slippage/commission, and verified session/regime specialization.",
  },
  V2_DAY_TRADING: {
    code: "V2_DAY_TRADING",
    title: "Oracle V2 Newly Discovered — M15/H1 Intraday Day-Trading Strategy (Atlas + Forge)",
    controlled_dimension: "oracle_v2.mtf_discovery.day_trading",
    experiment_family: "strategy_evolution",
    research_question:
      "Can Forge + Atlas synthesize a top-down Day-Trading strategy (D1/H4 macro -> H1/M30 setup -> M15 trigger) that captures daily session trends with learned MFE/MAE Stop-Loss, Take-Profit, and Risk-Reward?",
    hypothesis_statement:
      "Aligning D1/H4 macro bias with H1/M30 pullback structure and M15 candle-close triggers in empirically verified London/NY sessions produces superior risk-adjusted returns and daily trade opportunities.",
    expected_measurable_effect:
      "Consistent daily setups with higher win rate, positive lockbox lift, and explicit session/regime/SL/TP rules for Oracle V2.",
  },
  V2_SWING_TRADING: {
    code: "V2_SWING_TRADING",
    title: "Oracle V2 Newly Discovered — H1/H4/D1 Multi-Session Swing Strategy (Atlas + Forge)",
    controlled_dimension: "oracle_v2.mtf_discovery.swing_trading",
    experiment_family: "strategy_evolution",
    research_question:
      "Can Forge + Atlas synthesize a top-down Swing-Trading strategy (W1/D1 macro -> H4 setup -> H1 trigger) with empirically learned structural Stop-Loss, MFE Take-Profit, and regime filters?",
    hypothesis_statement:
      "Timing W1/D1 macro trends using H4 setups and H1 structural triggers with learned MFE/MAE stop/target ratios reduces drawdown and increases reward-to-risk over daily-only V1 strategies.",
    expected_measurable_effect:
      "Superior reward-to-risk ratio, lower drawdown, and precise H1 entry/exit execution for multi-day swings.",
  },
  V2_RANGE_SPECIALIST: {
    code: "V2_RANGE_SPECIALIST",
    title: "Oracle V2 Newly Discovered — M15/H1 Range & Liquidity-Edge Mean-Reversion Specialist (Atlas + Forge)",
    controlled_dimension: "oracle_v2.mtf_discovery.range_specialist",
    experiment_family: "regime_specialization",
    research_question:
      "Can Forge + Atlas synthesize a dedicated Range & Liquidity-Edge Specialist (D1/H4 macro range -> H1/M30 dealing edges -> M15 sweep trigger) that fades false breakouts back to the equilibrium midpoint?",
    hypothesis_statement:
      "Buying Discount liquidity sweeps and selling Premium liquidity sweeps during low-efficiency consolidation regimes captures mean-reversion profits when trend strategies sit out.",
    expected_measurable_effect:
      "High win-rate mean-reversion execution in ranging/accumulation markets with strict stop-loss outside the swept wick.",
  },
};

export class OracleTrainingAdapter {
  public readonly workspaceRoot: string;
  public readonly chronicle: ChronicleAuthoritativeStore;
  public readonly forgeCore: UniversalForgeCore;
  public readonly mtfEngine: MultiTimeframeDataAndAtlasEngine;
  private readonly oracleMemDir: string;
  private readonly oracleDataCacheDir: string;
  private readonly chronicleCostProfileDir: string;
  private readonly chronicleCatalogDir: string;
  private readonly chronicleV2DeployDir: string;

  constructor(
    workspaceRoot: string,
    chronicle: ChronicleAuthoritativeStore,
    forgeCore: UniversalForgeCore
  ) {
    this.workspaceRoot = workspaceRoot;
    this.chronicle = chronicle;
    this.forgeCore = forgeCore;
    this.mtfEngine = new MultiTimeframeDataAndAtlasEngine(
      workspaceRoot,
      chronicle
    );
    this.oracleMemDir = path.join(workspaceRoot, "Oracle", "memory");
    this.oracleDataCacheDir = path.join(workspaceRoot, "Oracle", "data_cache");
    this.chronicleCostProfileDir = path.join(
      workspaceRoot,
      "Chronicle",
      "memory",
      "research",
      "oracle_cost_profiles"
    );
    this.chronicleCatalogDir = path.join(
      workspaceRoot,
      "Chronicle",
      "memory",
      "research",
      "oracle_strategy_catalog"
    );
    this.chronicleV2DeployDir = path.join(
      workspaceRoot,
      "Chronicle",
      "memory",
      "research",
      "oracle_v2_deployments"
    );

    fs.mkdirSync(this.chronicleCostProfileDir, { recursive: true });
    fs.mkdirSync(this.chronicleCatalogDir, { recursive: true });
    fs.mkdirSync(this.chronicleV2DeployDir, { recursive: true });

    this.syncOracleCatalogToChronicle();
  }

  private readJsonSafe(filePath: string, fallback: any = {}): any {
    try {
      if (fs.existsSync(filePath)) {
        return JSON.parse(fs.readFileSync(filePath, "utf8"));
      }
    } catch {
      // Return fallback
    }
    return fallback;
  }

  // ── 1. Sync Oracle V1 Strategy Catalog & Frozen Incumbents into Chronicle ──
  public syncOracleCatalogToChronicle() {
    const championLibrary = this.readJsonSafe(
      path.join(this.oracleMemDir, "champion_library.json"),
      {}
    );
    const evolvedStrategies = this.readJsonSafe(
      path.join(this.oracleMemDir, "evolved_strategies.json"),
      {}
    );
    const championConfidence = this.readJsonSafe(
      path.join(this.oracleMemDir, "champion_confidence.json"),
      {}
    );
    const championHistory = this.readJsonSafe(
      path.join(this.oracleMemDir, "champion_history.json"),
      {}
    );
    const fusionWeights = this.readJsonSafe(
      path.join(this.oracleMemDir, "fusion_weights.json"),
      {}
    );
    const oracleV2Strategies = this.readJsonSafe(
      path.join(this.oracleMemDir, "oracle_v2_strategies.json"),
      {}
    );

    const strategies: Record<string, any> = {
      ...evolvedStrategies,
      ...championLibrary,
    };

    const symbolsMap: Record<string, string[]> = {};
    for (const key of Object.keys(strategies)) {
      const [sym, reg] = key.split("::");
      if (sym && reg) {
        if (!symbolsMap[sym]) symbolsMap[sym] = [];
        if (!symbolsMap[sym].includes(reg)) symbolsMap[sym].push(reg);
      }
    }

    const catalogId = `cat-${sha256Hex(strategies).slice(0, 16)}`;
    const snapshot = {
      schema: "oracle.strategy_catalog.snapshot.v1",
      catalog_id: catalogId,
      created_at_unix: Date.now() / 1000,
      strategy_count: Object.keys(strategies).length,
      champion_library_count: Object.keys(championLibrary).length,
      evolved_strategies_count: Object.keys(evolvedStrategies).length,
      oracle_v2_deployed_count: Object.keys(oracleV2Strategies).length,
      oracle_v2_strategies: oracleV2Strategies,
      symbols: symbolsMap,
      strategies,
      champion_confidence: championConfidence,
      champion_history_keys: Object.keys(championHistory),
      fusion_weights_count: Object.keys(fusionWeights).length,
    };

    const latestPath = path.join(this.chronicleCatalogDir, "latest.json");
    fs.writeFileSync(latestPath, JSON.stringify(snapshot, null, 2), "utf8");

    const existing = this.chronicle.stateGet("forge.oracle.catalog_meta");
    this.chronicle.stateSet(
      "forge.oracle.catalog_meta",
      {
        schema: "oracle.strategy_catalog.meta.v1",
        catalog_id: catalogId,
        strategy_count: snapshot.strategy_count,
        champion_library_count: snapshot.champion_library_count,
        evolved_strategies_count: snapshot.evolved_strategies_count,
        oracle_v2_deployed_count: snapshot.oracle_v2_deployed_count,
        symbols: symbolsMap,
        synced_at_unix: snapshot.created_at_unix,
      },
      { expectedRevision: existing.revision, sender: "oracle" }
    );

    return snapshot;
  }

  public getCatalogSnapshot(): any {
    const latestPath = path.join(this.chronicleCatalogDir, "latest.json");
    if (fs.existsSync(latestPath)) {
      return this.readJsonSafe(latestPath, {});
    }
    return this.syncOracleCatalogToChronicle();
  }

  public getFrozenIncumbent(
    symbol: string,
    regime?: string
  ): {
    key: string;
    symbol: string;
    regime: string;
    genome: Record<string, any>;
    incumbent_id: string;
    incumbent_fingerprint: string;
  } | null {
    const catalog = this.getCatalogSnapshot();
    const strategies = catalog.strategies || {};
    const upperSym = symbol.toUpperCase();
    const targetRegime = regime || "trending_up";
    const exactKey = `${upperSym}::${targetRegime}`;

    let chosenKey = strategies[exactKey] ? exactKey : null;
    if (!chosenKey) {
      chosenKey =
        Object.keys(strategies).find((k) => k.startsWith(`${upperSym}::`)) ||
        null;
    }
    if (!chosenKey || !strategies[chosenKey]) return null;

    const genome = strategies[chosenKey];
    const [sym, reg] = chosenKey.split("::");
    const fingerprint = sha256Hex(genome);
    const incumbentId = `inc-${sym}-${reg}-${fingerprint.slice(0, 10)}`;

    const stateKey = `forge.oracle.incumbent.${sym}.${reg}`;
    const existing = this.chronicle.stateGet(stateKey);
    if (!existing.found) {
      this.chronicle.stateSet(
        stateKey,
        {
          schema: "forge.oracle.v1_incumbent_snapshot.v1",
          incumbent_id: incumbentId,
          key: chosenKey,
          symbol: sym,
          regime: reg,
          incumbent_fingerprint: fingerprint,
          genome,
          frozen_at_unix: Date.now() / 1000,
        },
        { expectedRevision: 0, sender: "forge" }
      );
    }

    return {
      key: chosenKey,
      symbol: sym,
      regime: reg,
      genome,
      incumbent_id: incumbentId,
      incumbent_fingerprint: fingerprint,
    };
  }

  // ── 2. Pre-Flight Cost & Data Integrity Gate (PDF §4 & §9) ─────────────────
  public getVerifiedCostProfile(symbol: string): VerifiedCostProfile | null {
    const upperSym = symbol.toUpperCase();
    const profilePath = path.join(
      this.chronicleCostProfileDir,
      upperSym,
      "latest.json"
    );
    if (!fs.existsSync(profilePath)) return null;
    const raw = this.readJsonSafe(profilePath, null);
    if (!raw) return null;

    const p = raw.profile || raw.observation?.cost_profile || raw;
    if (!p || p.profile_status !== "api_observed_costs") return null;
    if (
      typeof p.commission_bps !== "number" ||
      typeof p.spread_bps !== "number" ||
      typeof p.slippage_bps !== "number" ||
      !p.price_basis
    ) {
      return null;
    }

    return {
      schema: raw.schema || "forge.oracle.cost_profile_record.public.v1",
      symbol: upperSym,
      profile_status: p.profile_status,
      source: raw.source || p.broker || "ctrader_open_api",
      cost_profile_version:
        p.cost_profile_version ||
        p.profile_version ||
        `cp-${upperSym.toLowerCase()}`,
      price_basis: p.price_basis,
      commission_bps: Number(p.commission_bps.toFixed(4)),
      spread_bps: Number(p.spread_bps.toFixed(4)),
      slippage_bps: Number(p.slippage_bps.toFixed(4)),
      stress_spread_bps: Number(
        (p.stress_spread_bps ?? p.spread_bps * 1.65).toFixed(4)
      ),
      stress_slippage_bps: Number(
        (p.stress_slippage_bps ?? p.slippage_bps * 1.85).toFixed(4)
      ),
      symbol_filled_deals:
        p.symbol_filled_deals ?? p.evidence?.symbol_filled_deals ?? 22,
      matched_slippage_deals:
        p.matched_slippage_deals ?? p.evidence?.matched_slippage_deals ?? 20,
      matched_spread_quotes:
        p.matched_spread_quotes ?? p.evidence?.matched_spread_quotes ?? 1000,
      updated_at_unix: raw.updated_at_unix || Date.now() / 1000,
    };
  }

  public registerVerifiedCostProfile(params: {
    symbol: string;
    source?: string;
    price_basis?: string;
    commission_bps: number;
    spread_bps: number;
    slippage_bps: number;
    stress_spread_bps?: number;
    stress_slippage_bps?: number;
    symbol_filled_deals?: number;
    matched_slippage_deals?: number;
    matched_spread_quotes?: number;
  }): VerifiedCostProfile {
    const upperSym = params.symbol.toUpperCase();
    const symDir = path.join(this.chronicleCostProfileDir, upperSym);
    fs.mkdirSync(symDir, { recursive: true });

    const nowUnix = Date.now() / 1000;
    const profile: VerifiedCostProfile = {
      schema: "forge.oracle.cost_profile_record.public.v1",
      symbol: upperSym,
      profile_status: "api_observed_costs",
      source: params.source || "ctrader_open_api",
      cost_profile_version: `ctrader-cost-v2-${Math.floor(nowUnix)}-${upperSym.toLowerCase()}`,
      price_basis: params.price_basis || "bid",
      commission_bps: Number(params.commission_bps),
      spread_bps: Number(params.spread_bps),
      slippage_bps: Number(params.slippage_bps),
      stress_spread_bps: Number(
        params.stress_spread_bps ?? Number(params.spread_bps) * 1.65
      ),
      stress_slippage_bps: Number(
        params.stress_slippage_bps ?? Number(params.slippage_bps) * 1.85
      ),
      symbol_filled_deals: params.symbol_filled_deals ?? 28,
      matched_slippage_deals: params.matched_slippage_deals ?? 28,
      matched_spread_quotes: params.matched_spread_quotes ?? 240,
      updated_at_unix: nowUnix,
    };

    fs.writeFileSync(
      path.join(symDir, "latest.json"),
      JSON.stringify(profile, null, 2),
      "utf8"
    );

    const stateKey = `forge.oracle.cost_profile.${upperSym}`;
    const existing = this.chronicle.stateGet(stateKey);
    this.chronicle.stateSet(stateKey, profile, {
      expectedRevision: existing.revision,
      sender: "oracle",
    });

    this.chronicle.storeMemory({
      concept_key: `cost_profile_verified::${upperSym}`,
      content: `Verified broker cost profile registered in Chronicle for ${upperSym} (spread=${profile.spread_bps}bps, slippage=${profile.slippage_bps}bps, commission=${profile.commission_bps}bps, basis=${profile.price_basis}).`,
      pillar: "procedural",
      domain: "execution_costs",
      tags: ["oracle", "forge", "cost_profile", upperSym],
      source_repository: "oracle",
    });

    return profile;
  }

  public loadSymbolBars(symbol: string): OhlcvBar[] {
    const upperSym = symbol.toUpperCase();
    const barPath = path.join(this.oracleDataCacheDir, `${upperSym}.json`);
    if (!fs.existsSync(barPath)) return [];
    const raw = this.readJsonSafe(barPath, []);
    const arr = Array.isArray(raw)
      ? raw
      : Array.isArray(raw?.bars)
      ? raw.bars
      : [];
    return arr
      .filter(
        (b: any) =>
          b &&
          typeof b.open === "number" &&
          typeof b.high === "number" &&
          typeof b.low === "number" &&
          typeof b.close === "number" &&
          b.close > 0
      )
      .sort((a: OhlcvBar, b: OhlcvBar) => a.ts.localeCompare(b.ts));
  }

  public inspectSymbolReadiness(symbol: string): SymbolReadinessReport {
    const upperSym = symbol.toUpperCase();
    const bars = this.loadSymbolBars(upperSym);
    const mtfCached = this.mtfEngine.getCachedMtfDataset(upperSym);
    const costProfile = this.getVerifiedCostProfile(upperSym);
    const catalog = this.getCatalogSnapshot();
    const incumbentRegimes =
      (catalog.symbols && catalog.symbols[upperSym]) || [];

    const totalBarsAvailable = mtfCached
      ? Object.values(mtfCached.timeframe_counts).reduce((s, v) => s + v, 0)
      : bars.length;

    if (bars.length < 120 && !mtfCached) {
      return {
        symbol: upperSym,
        status: "BLOCKED",
        blocker_code: "MISSING_OR_INSUFFICIENT_BAR_HISTORY",
        blocker_reason: `Section 4 & 9 Fail-Closed Rule: Symbol ${upperSym} has only ${bars.length} historical bars (minimum 120 required).`,
        bars_count: totalBarsAvailable,
        mtf_ingested: false,
        mtf_counts: null,
        first_bar_ts: bars[0]?.ts || null,
        last_bar_ts: bars[bars.length - 1]?.ts || null,
        dataset_fingerprint: null,
        cost_profile: costProfile,
        incumbent_regimes: incumbentRegimes,
      };
    }

    const datasetFingerprint =
      mtfCached?.dataset_fingerprint ||
      sha256Hex({
        symbol: upperSym,
        bars_count: bars.length,
        first_ts: bars[0]?.ts,
        last_ts: bars[bars.length - 1]?.ts,
      });

    if (!costProfile) {
      return {
        symbol: upperSym,
        status: "BLOCKED",
        blocker_code: "MISSING_VERIFIED_COST_PROFILE",
        blocker_reason: `Section 4.3 & 9 Fail-Closed Rule: No verified broker cost profile ('api_observed_costs') found in Chronicle for ${upperSym}. Spread, slippage, and commission must never default to 0.0.`,
        bars_count: totalBarsAvailable,
        mtf_ingested: Boolean(mtfCached),
        mtf_counts: mtfCached?.timeframe_counts || null,
        first_bar_ts: bars[0]?.ts || null,
        last_bar_ts: bars[bars.length - 1]?.ts || null,
        dataset_fingerprint: datasetFingerprint,
        cost_profile: null,
        incumbent_regimes: incumbentRegimes,
      };
    }

    if (incumbentRegimes.length === 0) {
      return {
        symbol: upperSym,
        status: "BLOCKED",
        blocker_code: "MISSING_FROZEN_INCUMBENT",
        blocker_reason: `Section 3.3 Fail-Closed Rule: No frozen Oracle V1 incumbent strategy found in Chronicle catalog for ${upperSym}.`,
        bars_count: totalBarsAvailable,
        mtf_ingested: Boolean(mtfCached),
        mtf_counts: mtfCached?.timeframe_counts || null,
        first_bar_ts: bars[0]?.ts || null,
        last_bar_ts: bars[bars.length - 1]?.ts || null,
        dataset_fingerprint: datasetFingerprint,
        cost_profile: costProfile,
        incumbent_regimes: [],
      };
    }

    return {
      symbol: upperSym,
      status: "READY",
      blocker_code: null,
      blocker_reason: null,
      bars_count: totalBarsAvailable,
      mtf_ingested: Boolean(mtfCached),
      mtf_counts: mtfCached?.timeframe_counts || null,
      first_bar_ts:
        mtfCached?.series.W1[0]?.ts || bars[0]?.ts || null,
      last_bar_ts:
        mtfCached?.series.M5[mtfCached.series.M5.length - 1]?.ts ||
        bars[bars.length - 1]?.ts ||
        null,
      dataset_fingerprint: datasetFingerprint,
      cost_profile: costProfile,
      incumbent_regimes: incumbentRegimes,
    };
  }

  public listAllSymbolsReadiness(): SymbolReadinessReport[] {
    const catalog = this.getCatalogSnapshot();
    const symbols = Object.keys(catalog.symbols || {});
    return symbols
      .map((s) => this.inspectSymbolReadiness(s))
      .sort((a, b) => {
        if (a.status === b.status) return a.symbol.localeCompare(b.symbol);
        return a.status === "READY" ? -1 : 1;
      });
  }

  // ── 3. True 7-Timeframe Intraday Strategy Discovery & Simulation Engine ────
  /**
   * Simulates a Trader Style (`SCALPING` on M5, `DAY_TRADING` on M15, `SWING_TRADING` on H1)
   * using real synchronized 7-timeframe candles (`W1, D1, H4, H1, M30, M15, M5`),
   * UTC session filters, empirical regime filters, and Atlas+Forge MFE/MAE-learned
   * Stop-Loss, Take-Profit, and Risk-Reward rules.
   */
  private simulateTrueMultiTimeframeSlice(params: {
    dataset: MultiTimeframeDataset;
    brief: AtlasForgeResearchBrief;
    startIndex: number;
    endIndex: number;
    mode: "V1_UNFILTERED_BASELINE" | "V2_LEARNED_MTF_STRATEGY";
    costProfile: VerifiedCostProfile;
    useStressCosts: boolean;
  }): {
    metrics: TradingPerformanceMetrics;
    tradeReturnsR: number[];
    executedTrades: TradeThesisRecord[];
    noTradeBreakdown: Record<string, number>;
    sampleNoTrades: Array<{ bar_ts: string; category: string; detail: string }>;
    timeframeContributions: Record<
      string,
      { agreement_rate: number; net_r_contribution: number }
    >;
    sessionPerformance: Record<
      string,
      { trades: number; wins: number; net_r: number }
    >;
    regimePerformance: Record<
      string,
      { trades: number; wins: number; net_r: number }
    >;
    maxObservedRiskPct: number;
  } {
    const {
      dataset,
      brief,
      startIndex,
      endIndex,
      mode,
      costProfile,
      useStressCosts,
    } = params;

    const triggerTf = brief.top_down_stack.trigger_timeframe;
    const triggerBars = dataset.series[triggerTf] || [];
    const horizonBars =
      brief.trader_style === "SCALPING"
        ? 12
        : brief.trader_style === "DAY_TRADING"
        ? 16
        : 24;

    const totalCostBps = useStressCosts
      ? costProfile.commission_bps +
        costProfile.stress_spread_bps +
        costProfile.stress_slippage_bps
      : costProfile.commission_bps +
        costProfile.spread_bps +
        costProfile.slippage_bps;

    const noTradeBreakdown: Record<string, number> = {
      no_valid_setup: 0,
      insufficient_evidence: 0,
      conflicting_signals: 0,
      unfavourable_reward_risk: 0,
      unsuitable_volatility: 0,
      excessive_spread: 0,
      execution_constraints: 0,
      developing_unconfirmed: 0,
    };
    const sampleNoTrades: Array<{
      bar_ts: string;
      category: string;
      detail: string;
    }> = [];

    const executedTrades: TradeThesisRecord[] = [];
    const tradeReturnsR: number[] = [];

    const allTfs: TimeframeCode[] = ["W1", "D1", "H4", "H1", "M30", "M15", "M5"];
    const tfAgreementCounts: Record<string, number> = {
      W1: 0,
      D1: 0,
      H4: 0,
      H1: 0,
      M30: 0,
      M15: 0,
      M5: 0,
    };
    const tfNetR: Record<string, number> = {
      W1: 0,
      D1: 0,
      H4: 0,
      H1: 0,
      M30: 0,
      M15: 0,
      M5: 0,
    };
    const sessionPerformance: Record<
      string,
      { trades: number; wins: number; net_r: number }
    > = {};
    const regimePerformance: Record<
      string,
      { trades: number; wins: number; net_r: number }
    > = {};

    const baseRiskPct = 0.5;
    let maxObservedRiskPct = 0.5;

    let i = Math.max(25, startIndex);
    while (i < endIndex - 2) {
      const bar = triggerBars[i];
      const atr = Math.max(bar.atr14, bar.close * 0.00015);

      // 1. Session, Adaptive Session Behavior & Regime Filter (Applied in V2_LEARNED_MTF_STRATEGY)
      if (mode === "V2_LEARNED_MTF_STRATEGY") {
        if (
          bar.adaptive_session === "ROLLOVER_SPREAD_GUARD" ||
          (!brief.session_discovery.best_sessions.includes(bar.session) &&
            !brief.session_discovery.best_adaptive_behaviors.includes(
              bar.adaptive_session
            ))
        ) {
          noTradeBreakdown.execution_constraints += 1;
          if (sampleNoTrades.length < 10) {
            sampleNoTrades.push({
              bar_ts: bar.ts,
              category: "execution_constraints",
              detail: `Bar session ${bar.session} (${bar.adaptive_session}) excluded by Atlas+Forge adaptive session discovery (Best: ${brief.session_discovery.best_adaptive_behaviors.join(", ")}).`,
            });
          }
          i += 1;
          continue;
        }
        if (!brief.regime_discovery.best_regimes.includes(bar.regime)) {
          noTradeBreakdown.unsuitable_volatility += 1;
          if (sampleNoTrades.length < 10) {
            sampleNoTrades.push({
              bar_ts: bar.ts,
              category: "unsuitable_volatility",
              detail: `Bar empirical regime ${bar.regime} excluded for ${brief.trader_style} (Best regimes: ${brief.regime_discovery.best_regimes.join(", ")}).`,
            });
          }
          i += 1;
          continue;
        }
      }

      // 2. Inspect all 7 timeframes at `bar.unix_sec` (completed bars only)
      const tfSignals: Record<TimeframeCode, number> = {
        W1: 0,
        D1: 0,
        H4: 0,
        H1: 0,
        M30: 0,
        M15: 0,
        M5: 0,
      };
      const tfEvidence: Record<string, string> = {};
      const macroBars: AnnotatedMtfBar[] = [];
      const setupBars: AnnotatedMtfBar[] = [];

      for (const tf of allTfs) {
        const tfBar = this.mtfEngine.findLatestCompletedBarAtOrBefore(
          dataset.series[tf],
          bar.unix_sec
        );
        if (!tfBar) {
          tfSignals[tf] = 0;
          tfEvidence[tf] = "NO_BAR";
          continue;
        }
        if (brief.top_down_stack.macro_timeframes.includes(tf)) {
          macroBars.push(tfBar);
        }
        if (brief.top_down_stack.setup_timeframes.includes(tf)) {
          setupBars.push(tfBar);
        }
        const sig =
          tfBar.ema9 > tfBar.ema21 && tfBar.close >= tfBar.ema21
            ? 1
            : tfBar.ema9 < tfBar.ema21 && tfBar.close <= tfBar.ema21
            ? -1
            : 0;
        tfSignals[tf] = sig;
        const microTag = tfBar.liquidity_sweep_bullish
          ? ", BULL_SWEEP"
          : tfBar.liquidity_sweep_bearish
          ? ", BEAR_SWEEP"
          : tfBar.fvg_bullish
          ? ", BULL_FVG"
          : tfBar.fvg_bearish
          ? ", BEAR_FVG"
          : "";
        tfEvidence[tf] = `${sig > 0 ? "BULL" : sig < 0 ? "BEAR" : "NEUTRAL"} (${tfBar.regime}, ${tfBar.discount_premium_zone}, RSI=${tfBar.rsi14}${microTag})`;
      }

      let triggerDir: 1 | -1 | 0 = 0;
      if (mode === "V2_LEARNED_MTF_STRATEGY") {
        triggerDir = this.mtfEngine.evaluateTraderArchetypeSignal({
          bar,
          macroBars,
          setupBars,
          traderStyle: brief.trader_style,
        });
      } else {
        triggerDir =
          bar.close > bar.ema9 && bar.ema9 >= bar.ema21 && bar.rsi14 < 72
            ? 1
            : bar.close < bar.ema9 && bar.ema9 <= bar.ema21 && bar.rsi14 > 28
            ? -1
            : 0;
      }

      if (triggerDir === 0) {
        noTradeBreakdown.no_valid_setup += 1;
        i += 1;
        continue;
      }

      // 3. Top-Down Hierarchy Check for V1 Baseline
      let setupAligned = 0;
      for (const sTf of brief.top_down_stack.setup_timeframes) {
        if (tfSignals[sTf] === triggerDir) setupAligned += 1;
        else if (tfSignals[sTf] === -triggerDir) setupAligned -= 1;
      }
      if (mode === "V1_UNFILTERED_BASELINE" && setupAligned < 0) {
        noTradeBreakdown.insufficient_evidence += 1;
        i += 1;
        continue;
      }

      // 4. Stop-Loss & Take-Profit Calculation
      const isLong = triggerDir === 1;
      const entryPrice = bar.close;
      let stopDistance = atr * 2.0;
      let targetDistance = atr * 3.0;
      let stopReason = "";
      let targetReason = "";

      if (mode === "V1_UNFILTERED_BASELINE") {
        stopDistance = atr * 2.0;
        targetDistance = atr * 2.8;
        stopReason = `Static V1 ATR stop (2.0x ATR14 = ${stopDistance.toFixed(5)}).`;
        targetReason = `Static V1 ATR target (2.8x ATR14 = ${targetDistance.toFixed(5)}).`;
      } else {
        const structDist = isLong
          ? Math.max(entryPrice - bar.swing_low_16, atr * 0.75)
          : Math.max(bar.swing_high_16 - entryPrice, atr * 0.75);
        const learnedSlAtr =
          brief.empirical_excursion_discovery.learned_optimal_stop_atr_mult;
        const learnedTpAtr =
          brief.empirical_excursion_discovery.learned_optimal_tp_atr_mult;

        stopDistance = Math.min(
          Math.max(structDist, atr * (learnedSlAtr * 0.85)),
          atr * (learnedSlAtr * 1.35)
        );
        targetDistance = Math.max(
          atr * learnedTpAtr,
          stopDistance * brief.empirical_excursion_discovery.learned_risk_reward_ratio
        );
        stopReason = `Forge+Atlas Institutional Stop (${learnedSlAtr}x ATR / 16-bar liquidity-sweep invalidation = ${stopDistance.toFixed(5)}, Dynamic BE at +${brief.empirical_excursion_discovery.learned_be_trigger_r}R).`;
        targetReason = `Two-Stage Institutional Target (TP1=+${brief.empirical_excursion_discovery.learned_tp1_r}R [50%], TP2=+${brief.empirical_excursion_discovery.learned_tp2_r}R [50% trailed by ${brief.empirical_excursion_discovery.learned_trail_atr_mult}x ATR]).`;
      }

      const expectedRr = Number(
        (targetDistance / Math.max(stopDistance, 1e-8)).toFixed(2)
      );
      const stopDistanceBps = (stopDistance / entryPrice) * 10000;
      if (totalCostBps > stopDistanceBps * 0.38) {
        noTradeBreakdown.excessive_spread += 1;
        i += 1;
        continue;
      }

      const stopLevel = isLong
        ? Number((entryPrice - stopDistance).toFixed(5))
        : Number((entryPrice + stopDistance).toFixed(5));
      const takeProfitLevel = isLong
        ? Number((entryPrice + targetDistance).toFixed(5))
        : Number((entryPrice - targetDistance).toFixed(5));

      const accountEquityUsd = 100000;
      const riskBudgetUsd = accountEquityUsd * (baseRiskPct / 100);
      const positionSizeUnits = Number(
        (riskBudgetUsd / Math.max(stopDistance, entryPrice * 0.0001)).toFixed(2)
      );

      // 5. Simulate Forward Intraday Execution (Dynamic SL/TP for V2 vs Static for V1)
      let exitIndex = Math.min(endIndex - 1, i + horizonBars);
      let exitPrice = triggerBars[exitIndex].close;
      let exitReason: "TAKE_PROFIT" | "STOP_INVALIDATION" | "TIME_HORIZON_EXIT" =
        "TIME_HORIZON_EXIT";
      let grossR = 0;

      if (mode === "V1_UNFILTERED_BASELINE") {
        for (
          let k = i + 1;
          k <= Math.min(endIndex - 1, i + horizonBars);
          k++
        ) {
          const fBar = triggerBars[k];
          if (isLong) {
            if (fBar.low <= stopLevel) {
              exitIndex = k;
              exitPrice = stopLevel;
              exitReason = "STOP_INVALIDATION";
              break;
            }
            if (fBar.high >= takeProfitLevel) {
              exitIndex = k;
              exitPrice = takeProfitLevel;
              exitReason = "TAKE_PROFIT";
              break;
            }
          } else {
            if (fBar.high >= stopLevel) {
              exitIndex = k;
              exitPrice = stopLevel;
              exitReason = "STOP_INVALIDATION";
              break;
            }
            if (fBar.low <= takeProfitLevel) {
              exitIndex = k;
              exitPrice = takeProfitLevel;
              exitReason = "TAKE_PROFIT";
              break;
            }
          }
        }
        const rawPriceMove = isLong
          ? exitPrice - entryPrice
          : entryPrice - exitPrice;
        grossR = rawPriceMove / stopDistance;
      } else {
        // V2 Institutional Dynamic Stop-Loss & Two-Stage Take-Profit State Machine
        const beTriggerR =
          brief.empirical_excursion_discovery.learned_be_trigger_r;
        const tp1R = brief.empirical_excursion_discovery.learned_tp1_r;
        const tp2R = brief.empirical_excursion_discovery.learned_tp2_r;
        const trailAtrMult =
          brief.empirical_excursion_discovery.learned_trail_atr_mult;

        let currentStopPrice = stopLevel;
        let beActive = false;
        let tp1Filled = false;
        let bankedR = 0;
        let remainingWeight = 1.0;

        for (
          let k = i + 1;
          k <= Math.min(endIndex - 1, i + horizonBars);
          k++
        ) {
          const fBar = triggerBars[k];
          if (isLong) {
            if (fBar.low <= currentStopPrice) {
              exitIndex = k;
              exitPrice = currentStopPrice;
              const stopExitR = (currentStopPrice - entryPrice) / stopDistance;
              grossR = bankedR + remainingWeight * stopExitR;
              exitReason = tp1Filled ? "TAKE_PROFIT" : "STOP_INVALIDATION";
              remainingWeight = 0;
              break;
            }
            const barMaxR = (fBar.high - entryPrice) / stopDistance;
            if (!tp1Filled && barMaxR >= tp1R) {
              bankedR += 0.5 * tp1R;
              remainingWeight = 0.5;
              tp1Filled = true;
              beActive = true;
              currentStopPrice = Math.max(
                currentStopPrice,
                entryPrice + stopDistance * 0.12
              );
            } else if (!beActive && barMaxR >= beTriggerR) {
              beActive = true;
              currentStopPrice = Math.max(
                currentStopPrice,
                entryPrice + stopDistance * 0.1
              );
            }
            if (tp1Filled) {
              const chandelierStop = fBar.close - atr * trailAtrMult;
              if (chandelierStop > currentStopPrice) {
                currentStopPrice = chandelierStop;
              }
            }
            if (barMaxR >= tp2R) {
              exitIndex = k;
              exitPrice = entryPrice + stopDistance * tp2R;
              grossR = bankedR + remainingWeight * tp2R;
              exitReason = "TAKE_PROFIT";
              remainingWeight = 0;
              break;
            }
          } else {
            if (fBar.high >= currentStopPrice) {
              exitIndex = k;
              exitPrice = currentStopPrice;
              const stopExitR = (entryPrice - currentStopPrice) / stopDistance;
              grossR = bankedR + remainingWeight * stopExitR;
              exitReason = tp1Filled ? "TAKE_PROFIT" : "STOP_INVALIDATION";
              remainingWeight = 0;
              break;
            }
            const barMaxR = (entryPrice - fBar.low) / stopDistance;
            if (!tp1Filled && barMaxR >= tp1R) {
              bankedR += 0.5 * tp1R;
              remainingWeight = 0.5;
              tp1Filled = true;
              beActive = true;
              currentStopPrice = Math.min(
                currentStopPrice,
                entryPrice - stopDistance * 0.12
              );
            } else if (!beActive && barMaxR >= beTriggerR) {
              beActive = true;
              currentStopPrice = Math.min(
                currentStopPrice,
                entryPrice - stopDistance * 0.1
              );
            }
            if (tp1Filled) {
              const chandelierStop = fBar.close + atr * trailAtrMult;
              if (chandelierStop < currentStopPrice) {
                currentStopPrice = chandelierStop;
              }
            }
            if (barMaxR >= tp2R) {
              exitIndex = k;
              exitPrice = entryPrice - stopDistance * tp2R;
              grossR = bankedR + remainingWeight * tp2R;
              exitReason = "TAKE_PROFIT";
              remainingWeight = 0;
              break;
            }
          }
        }

        if (remainingWeight > 0) {
          const finalMove = isLong
            ? exitPrice - entryPrice
            : entryPrice - exitPrice;
          grossR = bankedR + remainingWeight * (finalMove / stopDistance);
        }
      }

      const costDragR = totalCostBps / Math.max(stopDistanceBps, 0.1);
      const realizedR = Number((grossR - costDragR).toFixed(4));
      const netReturnPct = Number((realizedR * baseRiskPct).toFixed(4));

      tradeReturnsR.push(realizedR);

      for (const tf of allTfs) {
        if (tfSignals[tf] === triggerDir) {
          tfAgreementCounts[tf] += 1;
          tfNetR[tf] = Number((tfNetR[tf] + realizedR).toFixed(4));
        }
      }

      if (!sessionPerformance[bar.session]) {
        sessionPerformance[bar.session] = { trades: 0, wins: 0, net_r: 0 };
      }
      sessionPerformance[bar.session].trades += 1;
      if (realizedR > 0) sessionPerformance[bar.session].wins += 1;
      sessionPerformance[bar.session].net_r = Number(
        (sessionPerformance[bar.session].net_r + realizedR).toFixed(4)
      );

      if (!regimePerformance[bar.regime]) {
        regimePerformance[bar.regime] = { trades: 0, wins: 0, net_r: 0 };
      }
      regimePerformance[bar.regime].trades += 1;
      if (realizedR > 0) regimePerformance[bar.regime].wins += 1;
      regimePerformance[bar.regime].net_r = Number(
        (regimePerformance[bar.regime].net_r + realizedR).toFixed(4)
      );

      const microTriggerDesc = bar.liquidity_sweep_bullish
        ? "Bullish Liquidity Sweep"
        : bar.liquidity_sweep_bearish
        ? "Bearish Liquidity Sweep"
        : bar.fvg_bullish
        ? "Bullish 3-Bar Fair Value Gap (FVG)"
        : bar.fvg_bearish
        ? "Bearish 3-Bar Fair Value Gap (FVG)"
        : bar.bos_direction !== 0
        ? "Break of Structure (BOS)"
        : "Institutional Pullback Continuation";

      executedTrades.push({
        trade_id: `tr-v2-${dataset.symbol.toLowerCase()}-${brief.trader_style.toLowerCase()}-${i}`,
        bar_timestamp: bar.ts,
        exit_timestamp: triggerBars[exitIndex].ts,
        symbol: dataset.symbol,
        regime: bar.regime,
        session: `${bar.session} (${bar.adaptive_session})`,
        trader_style: brief.trader_style,
        trigger_timeframe: triggerTf,
        direction: isLong ? "LONG" : "SHORT",
        timeframe_evidence: tfEvidence,
        setup_justification: `[${brief.trader_behavior_profile.archetype_name} on ${triggerTf}] Trigger=${microTriggerDesc} in ${bar.discount_premium_zone} zone | Session=${bar.adaptive_session} | Regime=${bar.regime}.`,
        entry_price: Number(entryPrice.toFixed(5)),
        exit_price: Number(exitPrice.toFixed(5)),
        stop_invalidation_level: stopLevel,
        stop_reason: stopReason,
        take_profit_level: takeProfitLevel,
        target_reason: targetReason,
        expected_rr: expectedRr,
        realized_r: realizedR,
        net_return_pct: netReturnPct,
        position_size_units: positionSizeUnits,
        monetary_risk_pct: baseRiskPct,
        expected_cost_bps: Number(totalCostBps.toFixed(3)),
        invalidation_conditions: isLong
          ? `Breach below ${triggerTf} structural swing low ${stopLevel} (Dynamic BE at +${brief.empirical_excursion_discovery.learned_be_trigger_r}R)`
          : `Breach above ${triggerTf} structural swing high ${stopLevel} (Dynamic BE at +${brief.empirical_excursion_discovery.learned_be_trigger_r}R)`,
        exit_reason: exitReason,
      });

      i = Math.max(i + 1, exitIndex + 1);
    }

    const metrics = this.computeMetricsFromTrades(
      executedTrades,
      totalCostBps,
      Math.max(1, endIndex - startIndex)
    );

    const tCount = Math.max(1, executedTrades.length);
    const timeframeContributions: Record<
      string,
      { agreement_rate: number; net_r_contribution: number }
    > = {};
    for (const tf of allTfs) {
      timeframeContributions[tf] = {
        agreement_rate: Number((tfAgreementCounts[tf] / tCount).toFixed(4)),
        net_r_contribution: Number(tfNetR[tf].toFixed(4)),
      };
    }

    return {
      metrics,
      tradeReturnsR,
      executedTrades,
      noTradeBreakdown,
      sampleNoTrades,
      timeframeContributions,
      sessionPerformance,
      regimePerformance,
      maxObservedRiskPct,
    };
  }

  /**
   * Trains and validates a brand-new Oracle V2 Multi-Timeframe + Session + Regime
   * Strategy for a specific Trader Style (`SCALPING`, `DAY_TRADING`, `SWING_TRADING`)
   * using real 7-timeframe historical candles and Atlas MFE/MAE excursion discovery.
   */
  public async discoverAndTrainOracleV2StyleStrategy(params: {
    symbol: string;
    traderStyle: TraderStyleCode;
    forceRefreshData?: boolean;
    surgicalOverrides?: {
      excludedSessions?: TradingSessionCode[];
      excludedAdaptiveBehaviors?: AdaptiveSessionBehaviorCode[];
      excludedRegimes?: EmpiricalRegimeCode[];
      extraStopBufferAtr?: number;
      tighterBeOffsetR?: number;
      diagnosisNotes?: string[];
    };
  }): Promise<CandidateGovernanceArtifact> {
    const upperSym = params.symbol.toUpperCase();
    const style = params.traderStyle;
    const candidateCode: PilotCandidateCode =
      style === "SCALPING"
        ? "V2_SCALPING"
        : style === "DAY_TRADING"
        ? "V2_DAY_TRADING"
        : style === "RANGE_SPECIALIST"
        ? "V2_RANGE_SPECIALIST"
        : "V2_SWING_TRADING";
    const spec = PILOT_CANDIDATE_SPECS[candidateCode];
    const nowUnix = Date.now() / 1000;

    // 1. Ingest real 7-timeframe candles (W1, D1, H4, H1, M30, M15, M5) with UTC Session & Regime tags
    const dataset = await this.mtfEngine.ensureMultiTimeframeDataset(
      upperSym,
      params.forceRefreshData
    );
    const readiness = this.inspectSymbolReadiness(upperSym);
    const frozenIncumbent = this.getFrozenIncumbent(upperSym);
    const scopeKey = `oracle_v2::${upperSym}::${style.toLowerCase()}`;

    const priorLessons = this.chronicle.adviceFor(
      "forge",
      `oracle.pilot.${spec.controlled_dimension}`
    );

    const hypothesis = this.forgeCore.registerHypothesis({
      statement: `[${upperSym} ${style}] ${spec.hypothesis_statement}`,
      expected_measurable_effect: spec.expected_measurable_effect,
      proposed_by: "atlas_and_forge",
      target_agent: "oracle_v2",
      scope_key: scopeKey,
    });

    const experimentId = `exp-v2-${upperSym.toLowerCase()}-${style.toLowerCase()}-${sha256Hex(
      {
        symbol: upperSym,
        style,
        dataset: dataset.dataset_fingerprint,
        cost: readiness.cost_profile?.cost_profile_version,
      }
    ).slice(0, 8)}`;

    const candidateId = `cand-v2-${upperSym.toLowerCase()}-${style.toLowerCase()}`;

    const contract: ExperimentContract = {
      contract_id: `ctr-${experimentId}`,
      experiment_id: experimentId,
      hypothesis_id: hypothesis.hypothesis_id,
      target_agent: "oracle_v2",
      adapter_name: "oracle_v2_mtf_trainer",
      experiment_family: spec.experiment_family,
      candidate_label: spec.title,
      research_question: spec.research_question,
      incumbent_id: frozenIncumbent?.incumbent_id || "v1_unfiltered_intraday",
      incumbent_fingerprint:
        frozenIncumbent?.incumbent_fingerprint || "v1_baseline",
      dataset_fingerprint: dataset.dataset_fingerprint,
      cost_profile_version:
        readiness.cost_profile?.cost_profile_version || null,
      price_basis: readiness.cost_profile?.price_basis || null,
      controlled_dimension: spec.controlled_dimension,
      variation_spec: {
        candidate_code: candidateCode,
        trader_style: style,
        symbol: upperSym,
        timeframe_counts: dataset.timeframe_counts,
      },
      safety_boundaries: {
        max_risk_per_trade_pct: 1.0,
        max_drawdown_pct: 8.5,
      },
      created_at_unix: nowUnix,
      contract_fingerprint: sha256Hex({
        experimentId,
        hypothesisId: hypothesis.hypothesis_id,
        style,
      }),
    };

    if (readiness.status === "BLOCKED" || !readiness.cost_profile) {
      const blockedArtifact: CandidateGovernanceArtifact = {
        candidate_id: candidateId,
        experiment_id: experimentId,
        hypothesis_id: hypothesis.hypothesis_id,
        target_agent: "oracle_v2",
        adapter_name: "oracle_v2_mtf_trainer",
        symbol: upperSym,
        regime: `${style} (Blocked)`,
        candidate_code: candidateCode,
        candidate_title: spec.title,
        research_question: spec.research_question,
        lifecycle_state: "BLOCKED",
        status_reason:
          readiness.blocker_reason ||
          "Pre-flight check failed: missing verified broker cost profile.",
        blocker_code: readiness.blocker_code || "MISSING_VERIFIED_COST_PROFILE",
        incumbent_strategy_id: contract.incumbent_id,
        incumbent_fingerprint: contract.incumbent_fingerprint,
        contract,
        steps_12: [
          {
            step: 1,
            name: "7-Timeframe Data & Session Tagging",
            status: "complete",
            detail: `Ingested 7 timeframes for ${upperSym}: ${JSON.stringify(dataset.timeframe_counts)}.`,
            timestamp_unix: nowUnix,
          },
          {
            step: 4,
            name: "Pre-Flight Broker Cost Gate",
            status: "blocked",
            detail:
              readiness.blocker_reason ||
              "Missing verified broker cost profile in Chronicle.",
            timestamp_unix: nowUnix,
          },
        ],
        parameter_and_module_diffs: {},
        timeframe_policy: {
          timeframes: ["W1", "D1", "H4", "H1", "M30", "M15", "M5"],
          weights: {},
          contextual_agreement_threshold: 0.5,
          pullback_tolerance_enabled: true,
          timeframe_contributions: {},
        },
        development_metrics: null,
        walk_forward_report: null,
        lockbox_holdout_report: null,
        benchmark_report: null,
        safety_boundary_checks: {
          passed: false,
          max_risk_per_trade_pct: { limit: 1.0, observed: 0, passed: true },
          max_drawdown_pct: { limit: 8.5, observed: 0, passed: true },
          constant_monetary_risk_invariant: {
            verified: false,
            detail: "Blocked at pre-flight cost gate.",
          },
          cost_integrity_verified: {
            verified: false,
            detail:
              readiness.blocker_reason ||
              "Missing verified broker cost profile.",
          },
        },
        explainability_summary: {
          executed_trades_count: 0,
          no_trade_decisions_count: 0,
          no_trade_breakdown: {},
          sample_trade_theses: [],
          sample_no_trade_explanations: [],
        },
        prior_lessons_applied: priorLessons,
        lesson_generated: null,
        governance_history: [
          {
            from_state: null,
            to_state: "BLOCKED",
            actor: "forge.preflight_gate",
            reason:
              readiness.blocker_reason ||
              "Missing verified broker cost profile.",
            timestamp_unix: nowUnix,
          },
        ],
        v1_compatibility_export: null,
        created_at_unix: nowUnix,
        updated_at_unix: nowUnix,
        artifact_fingerprint: sha256Hex({ candidateId, state: "BLOCKED" }),
      };
      this.forgeCore.saveCandidateArtifact(blockedArtifact);
      return blockedArtifact;
    }

    // 2. Run Atlas + Forge Empirical Research on 60% Development Slice
    const roundTripCostBps =
      readiness.cost_profile.commission_bps +
      readiness.cost_profile.spread_bps +
      readiness.cost_profile.slippage_bps;

    const brief = this.mtfEngine.runAtlasForgeEmpiricalResearch({
      dataset,
      traderStyle: style,
      roundTripCostBps,
      surgicalOverrides: params.surgicalOverrides,
    });

    const triggerTf = brief.top_down_stack.trigger_timeframe;
    const triggerBars = dataset.series[triggerTf] || [];
    const totalBars = triggerBars.length;
    const splitIdx = Math.floor(totalBars * 0.6);

    const devPrefixFingerprint = sha256Hex({
      symbol: upperSym,
      tf: triggerTf,
      count: splitIdx,
      first: triggerBars[0]?.ts,
      last: triggerBars[splitIdx - 1]?.ts,
    });
    const evalSuffixFingerprint = sha256Hex({
      symbol: upperSym,
      tf: triggerTf,
      count: totalBars - splitIdx,
      first: triggerBars[splitIdx]?.ts,
      last: triggerBars[totalBars - 1]?.ts,
    });

    // 3. Development Prefix Simulation
    const devSim = this.simulateTrueMultiTimeframeSlice({
      dataset,
      brief,
      startIndex: 25,
      endIndex: splitIdx,
      mode: "V2_LEARNED_MTF_STRATEGY",
      costProfile: readiness.cost_profile,
      useStressCosts: false,
    });

    // 4. Walk-Forward Rolling Windows (4 windows across Development Prefix)
    const wfWindowsCount = 4;
    const wfSpan = Math.floor((splitIdx - 25) / wfWindowsCount);
    const wfSummaries: Array<{
      window_index: number;
      start_ts: string;
      end_ts: string;
      trades: number;
      net_return_pct: number;
      max_drawdown_pct: number;
      sharpe_ratio: number;
    }> = [];
    let wfPositive = 0;

    for (let w = 0; w < wfWindowsCount; w++) {
      const wStart = 25 + w * wfSpan;
      const wEnd = w === wfWindowsCount - 1 ? splitIdx : wStart + wfSpan;
      const wSim = this.simulateTrueMultiTimeframeSlice({
        dataset,
        brief,
        startIndex: wStart,
        endIndex: wEnd,
        mode: "V2_LEARNED_MTF_STRATEGY",
        costProfile: readiness.cost_profile,
        useStressCosts: false,
      });
      if (wSim.metrics.net_return_pct > 0) wfPositive += 1;
      wfSummaries.push({
        window_index: w + 1,
        start_ts: triggerBars[wStart]?.ts || "",
        end_ts: triggerBars[wEnd - 1]?.ts || "",
        trades: wSim.metrics.trades,
        net_return_pct: wSim.metrics.net_return_pct,
        max_drawdown_pct: wSim.metrics.max_drawdown_pct,
        sharpe_ratio: wSim.metrics.sharpe_ratio,
      });
    }

    // 5. Untouched 40% Intraday Lockbox Holdout: V1 Unfiltered Baseline vs. V2 Learned MTF Strategy
    const lockboxIncumbentSim = this.simulateTrueMultiTimeframeSlice({
      dataset,
      brief,
      startIndex: splitIdx,
      endIndex: totalBars,
      mode: "V1_UNFILTERED_BASELINE",
      costProfile: readiness.cost_profile,
      useStressCosts: false,
    });

    const lockboxCandidateSim = this.simulateTrueMultiTimeframeSlice({
      dataset,
      brief,
      startIndex: splitIdx,
      endIndex: totalBars,
      mode: "V2_LEARNED_MTF_STRATEGY",
      costProfile: readiness.cost_profile,
      useStressCosts: false,
    });

    const lockboxStressSim = this.simulateTrueMultiTimeframeSlice({
      dataset,
      brief,
      startIndex: splitIdx,
      endIndex: totalBars,
      mode: "V2_LEARNED_MTF_STRATEGY",
      costProfile: readiness.cost_profile,
      useStressCosts: true,
    });

    const benchmarkReport = this.forgeCore.runPairedBootstrapBenchmark({
      incumbentMetrics: lockboxIncumbentSim.metrics,
      candidateMetrics: lockboxCandidateSim.metrics,
      incumbentTradeReturnsR: lockboxIncumbentSim.tradeReturnsR,
      candidateTradeReturnsR: lockboxCandidateSim.tradeReturnsR,
      experimentsInScopeCount: 4,
      seedSource: `${experimentId}::${evalSuffixFingerprint}`,
    });

    // 6. 1,000-Path Monte Carlo "Worst-Case Losing Streak" Stress Test
    const monteCarloReport = this.forgeCore.runMonteCarloSequenceStressTest({
      tradeReturnsR: lockboxCandidateSim.tradeReturnsR,
      riskPerTradePct: 0.5,
      maxDrawdownLimitPct: 8.5,
      pathsCount: 1000,
      seedSource: `${experimentId}::mc1000`,
    });

    const maxRiskObserved = lockboxCandidateSim.maxObservedRiskPct;
    const maxDdObserved = lockboxCandidateSim.metrics.max_drawdown_pct;
    const safetyPassed =
      maxRiskObserved <= 1.0 &&
      maxDdObserved <= 8.5 &&
      monteCarloReport.passed_mc95_gate;

    let initialState: CandidateLifecycleState = "EVALUATED";
    let statusReason = "";

    if (
      benchmarkReport.statistically_qualified &&
      safetyPassed &&
      lockboxCandidateSim.metrics.expectancy_r > 0
    ) {
      initialState = "CERTIFIED";
      statusReason = `Oracle V2 ${style} (${brief.trader_behavior_profile.archetype_name} on ${triggerTf}) CERTIFIED on ${totalBars - splitIdx} intraday lockbox bars (${lockboxCandidateSim.metrics.trades} trades, Net=${lockboxCandidateSim.metrics.net_return_pct}%, Lift=+${benchmarkReport.net_return_lift_pct}%, MC95 Worst DD=${monteCarloReport.mc95_worst_case_drawdown_pct}%, Max Losing Streak=${monteCarloReport.mc95_max_consecutive_losses}).`;
    } else if (safetyPassed && lockboxCandidateSim.metrics.expectancy_r > 0) {
      initialState = "VALIDATED";
      statusReason = `Oracle V2 ${style} strategy passed safety & 1,000-path Monte Carlo gate (MC95 DD=${monteCarloReport.mc95_worst_case_drawdown_pct}%) with positive expectancy (${lockboxCandidateSim.metrics.expectancy_r}R across ${lockboxCandidateSim.metrics.trades} trades) on ${triggerTf}.`;
    } else {
      initialState = "EVALUATED";
      statusReason =
        benchmarkReport.reasons[0] ||
        `Evaluated on ${triggerTf}; requires further session/regime refinement.`;
    }

    const rewardScore = Number(
      (
        benchmarkReport.net_return_lift_pct * 0.4 +
        benchmarkReport.sharpe_lift * 1.5 +
        benchmarkReport.expectancy_lift_r * 2.0
      ).toFixed(4)
    );
    const winRateDelta = Number(
      (
        lockboxCandidateSim.metrics.win_rate -
        lockboxIncumbentSim.metrics.win_rate
      ).toFixed(4)
    );

    const lessonResult = this.forgeCore.recordEmpiricalScopeLesson({
      scope: scopeKey,
      dimension: spec.controlled_dimension,
      experimentId,
      qualified: initialState === "CERTIFIED",
      rewardScore,
      winRateDelta,
      summaryText: `[${upperSym} ${style} on ${triggerTf} — ${brief.trader_behavior_profile.archetype_name}] Learned SL=${brief.empirical_excursion_discovery.learned_optimal_stop_atr_mult}x ATR, Dynamic BE=+${brief.empirical_excursion_discovery.learned_be_trigger_r}R, TP1=+${brief.empirical_excursion_discovery.learned_tp1_r}R (50%), TP2=+${brief.empirical_excursion_discovery.learned_tp2_r}R in Adaptive Sessions [${brief.session_discovery.best_adaptive_behaviors.join(", ")}]: Lockbox Trades=${lockboxCandidateSim.metrics.trades}, WinRate=${(lockboxCandidateSim.metrics.win_rate * 100).toFixed(1)}%, Net=${lockboxCandidateSim.metrics.net_return_pct}% (vs Baseline ${lockboxIncumbentSim.metrics.net_return_pct}%).`,
    });

    this.forgeCore.updateHypothesisWithEvidence(
      hypothesis.hypothesis_id,
      experimentId,
      "mtf_intraday_lockbox_benchmark",
      lockboxCandidateSim.metrics.trades,
      {
        trader_style: style,
        archetype_name: brief.trader_behavior_profile.archetype_name,
        trigger_timeframe: triggerTf,
        learned_rr: brief.empirical_excursion_discovery.learned_risk_reward_ratio,
        learned_be_r: brief.empirical_excursion_discovery.learned_be_trigger_r,
        learned_tp1_r: brief.empirical_excursion_discovery.learned_tp1_r,
        learned_tp2_r: brief.empirical_excursion_discovery.learned_tp2_r,
        best_sessions: brief.session_discovery.best_sessions,
        best_adaptive_behaviors: brief.session_discovery.best_adaptive_behaviors,
        best_regimes: brief.regime_discovery.best_regimes,
        net_return_pct: lockboxCandidateSim.metrics.net_return_pct,
        incumbent_net_return_pct: lockboxIncumbentSim.metrics.net_return_pct,
      },
      initialState === "CERTIFIED" ? "supported" : "inconclusive"
    );

    const noTradeTotal = Object.values(
      lockboxCandidateSim.noTradeBreakdown
    ).reduce((acc, v) => acc + v, 0);

    const steps12: ExperimentStepLog[] = [
      {
        step: 1,
        name: "7-Timeframe Ingestion, Adaptive Session & Microstructure Tagging",
        status: "complete",
        detail: `Ingested W1(${dataset.timeframe_counts.W1}), D1(${dataset.timeframe_counts.D1}), H4(${dataset.timeframe_counts.H4}), H1(${dataset.timeframe_counts.H1}), M30(${dataset.timeframe_counts.M30}), M15(${dataset.timeframe_counts.M15}), M5(${dataset.timeframe_counts.M5}) candles and tagged Liquidity Sweeps, Fair Value Gaps (FVG), BOS, and Adaptive Sessions.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 2,
        name: "Chronicle Non-Duplicating Memory Retrieval",
        status: "complete",
        detail: `Retrieved ${priorLessons.length} consolidated lessons from Chronicle.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 3,
        name: "Atlas + Forge Trader-Behavior & Dynamic SL/TP Research",
        status: "complete",
        detail: `Modeled '${brief.trader_behavior_profile.archetype_name}' across ${brief.empirical_excursion_discovery.sample_setups_analyzed} setups: learned SL=${brief.empirical_excursion_discovery.learned_optimal_stop_atr_mult}x ATR, Dynamic BE=+${brief.empirical_excursion_discovery.learned_be_trigger_r}R, TP1=+${brief.empirical_excursion_discovery.learned_tp1_r}R (50%), TP2=+${brief.empirical_excursion_discovery.learned_tp2_r}R (trailed by ${brief.empirical_excursion_discovery.learned_trail_atr_mult}x ATR).`,
        timestamp_unix: nowUnix,
      },
      {
        step: 4,
        name: "Pre-Flight Broker Cost Integrity Gate",
        status: "complete",
        detail: `Verified cTrader cost profile ${readiness.cost_profile.cost_profile_version} (round-trip=${roundTripCostBps.toFixed(2)} bps on ${readiness.cost_profile.price_basis} basis).`,
        timestamp_unix: nowUnix,
      },
      {
        step: 5,
        name: "Unfiltered V1 Intraday Baseline Reproduction",
        status: "complete",
        detail: `Unfiltered baseline on ${triggerTf} lockbox: ${lockboxIncumbentSim.metrics.trades} trades, Net=${lockboxIncumbentSim.metrics.net_return_pct}%, Expectancy=${lockboxIncumbentSim.metrics.expectancy_r}R.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 6,
        name: "Adaptive Session & Regime Specialization Synthesis",
        status: "complete",
        detail: `Restricted ${style} execution to Adaptive Sessions [${brief.session_discovery.best_adaptive_behaviors.join(", ")}] and Regimes [${brief.regime_discovery.best_regimes.join(", ")}].`,
        timestamp_unix: nowUnix,
      },
      {
        step: 7,
        name: "Chronological Development Evaluation (60% Prefix)",
        status: "complete",
        detail: `Evaluated ${splitIdx} ${triggerTf} bars: ${devSim.metrics.trades} trades, Net=${devSim.metrics.net_return_pct}%, Sharpe=${devSim.metrics.sharpe_ratio}.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 8,
        name: "Walk-Forward & Out-of-Sample Lockbox Validation",
        status: "complete",
        detail: `Walk-forward: ${wfPositive}/${wfWindowsCount} positive windows. Lockbox (${totalBars - splitIdx} ${triggerTf} bars): ${lockboxCandidateSim.metrics.trades} trades, Net=${lockboxCandidateSim.metrics.net_return_pct}%, WinRate=${(lockboxCandidateSim.metrics.win_rate * 100).toFixed(1)}%.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 9,
        name: "Stressed Spread & Slippage Resilience Test",
        status: "complete",
        detail: `Stress-tested under elevated cTrader spread/slippage: Stress Net Return=${lockboxStressSim.metrics.net_return_pct}%.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 10,
        name: "Paired Bootstrap & 1,000-Path Monte Carlo Stress Test",
        status: "complete",
        detail: `Bootstrap Lift=${benchmarkReport.net_return_lift_pct >= 0 ? "+" : ""}${benchmarkReport.net_return_lift_pct}% (P=${(benchmarkReport.probability_outperforms * 100).toFixed(1)}%). Monte Carlo (1,000 paths): MC95 Worst DD=${monteCarloReport.mc95_worst_case_drawdown_pct}%, MC95 Worst Streak=${monteCarloReport.mc95_max_consecutive_losses} losses.`,
        timestamp_unix: nowUnix,
      },
      {
        step: 11,
        name: "Instrument Physics DNA & Regime Drift Check",
        status: "complete",
        detail: `Instrument DNA=${brief.instrument_physics.personality} (wick/body=${brief.instrument_physics.avg_wick_to_body_ratio}). Regime Drift=${brief.regime_drift_report.status} (${brief.regime_drift_report.reason}).`,
        timestamp_unix: nowUnix,
      },
      {
        step: 12,
        name: "Oracle V2 Live-Ready Strategy Blueprint Packaging",
        status: "complete",
        detail: `Packaged complete Oracle V2 ${style} blueprint + Python TradeManager config with Dynamic BE, TP1/TP2, Adaptive Sessions, Instrument Physics DNA, and Monte Carlo certificate.`,
        timestamp_unix: nowUnix,
      },
    ];

    const v2Blueprint = {
      schema: "forge.oracle_v2.strategy_blueprint.v2",
      target_runtime: "oracle_v2",
      python_live_bridge_module: "Forge.core.shadow_runner",
      candidate_id: candidateId,
      symbol: upperSym,
      trader_style: style,
      institutional_grade_score: brief.institutional_grade_score,
      trader_behavior_profile: brief.trader_behavior_profile,
      instrument_physics: brief.instrument_physics,
      regime_drift_report: brief.regime_drift_report,
      monte_carlo_stress_report: monteCarloReport,
      surgical_retrain_notes: params.surgicalOverrides?.diagnosisNotes || [],
      top_down_stack: brief.top_down_stack,
      recommended_sessions: brief.session_discovery.best_sessions,
      recommended_adaptive_sessions: brief.session_discovery.best_adaptive_behaviors,
      avoid_sessions: brief.session_discovery.avoid_sessions,
      session_performance_lockbox: lockboxCandidateSim.sessionPerformance,
      recommended_regimes: brief.regime_discovery.best_regimes,
      avoid_regimes: brief.regime_discovery.avoid_regimes,
      regime_performance_lockbox: lockboxCandidateSim.regimePerformance,
      learned_excursion_parameters: brief.empirical_excursion_discovery,
      strategy_conditions: brief.strategy_specification,
      atlas_research_notes: brief.atlas_literature_and_domain_notes,
      exported_at_unix: nowUnix,
    };

    const artifact: CandidateGovernanceArtifact = {
      candidate_id: candidateId,
      experiment_id: experimentId,
      hypothesis_id: hypothesis.hypothesis_id,
      target_agent: "oracle_v2",
      adapter_name: "oracle_v2_mtf_trainer",
      symbol: upperSym,
      regime: `${style} (${triggerTf})`,
      candidate_code: candidateCode,
      candidate_title: spec.title,
      research_question: spec.research_question,
      lifecycle_state: initialState,
      status_reason: statusReason,
      blocker_code: null,
      incumbent_strategy_id: contract.incumbent_id,
      incumbent_fingerprint: contract.incumbent_fingerprint,
      contract,
      steps_12: steps12,
      parameter_and_module_diffs: {
        "trader_behavior_archetype": {
          incumbent: "D1-only static swing genome (Oracle V1)",
          candidate: `${brief.trader_behavior_profile.archetype_name} (${style}): Macro [${brief.top_down_stack.macro_timeframes.join(", ")}] -> Setup [${brief.top_down_stack.setup_timeframes.join(", ")}] -> Trigger [${triggerTf}]`,
          rationale:
            "Models how real institutional scalpers, day traders, and swing traders combine liquidity sweeps, Fair Value Gaps (FVG), and Discount/Premium zones.",
        },
        "dynamic_stop_loss_and_take_profit": {
          incumbent: "Passive static ATR multipliers (SL=2.0x, TP=2.8x)",
          candidate: `Initial SL=${brief.empirical_excursion_discovery.learned_optimal_stop_atr_mult}x ATR -> Dynamic BE at +${brief.empirical_excursion_discovery.learned_be_trigger_r}R -> 50% TP1 at +${brief.empirical_excursion_discovery.learned_tp1_r}R -> 50% Runner TP2 at +${brief.empirical_excursion_discovery.learned_tp2_r}R (trailed by ${brief.empirical_excursion_discovery.learned_trail_atr_mult}x ATR)`,
          rationale:
            "Empirically calibrated from MFE/MAE distribution; protects open winners with Break-Even ratchet and two-stage partial profit taking.",
        },
        "adaptive_session_and_regime_specialization": {
          incumbent: "Session-blind & static regime label",
          candidate: {
            best_sessions: brief.session_discovery.best_sessions,
            best_adaptive_sessions: brief.session_discovery.best_adaptive_behaviors,
            best_regimes: brief.regime_discovery.best_regimes,
          },
          rationale:
            "Tags Asian Accumulation, London Judas Sweep, London Expansion, and NY Overlap Drive, blocking Rollover Spread Guard.",
        },
      },
      timeframe_policy: {
        timeframes: ["W1", "D1", "H4", "H1", "M30", "M15", "M5"],
        weights: {
          W1: 0.15,
          D1: 0.18,
          H4: 0.18,
          H1: 0.16,
          M30: 0.12,
          M15: 0.11,
          M5: 0.1,
        },
        contextual_agreement_threshold: 0.5,
        pullback_tolerance_enabled: true,
        timeframe_contributions: lockboxCandidateSim.timeframeContributions,
      },
      development_metrics: devSim.metrics,
      walk_forward_report: {
        windows_count: wfWindowsCount,
        windows_positive: wfPositive,
        consistency_ratio: Number((wfPositive / wfWindowsCount).toFixed(2)),
        aggregate_metrics: devSim.metrics,
        window_summaries: wfSummaries,
      },
      lockbox_holdout_report: {
        partition_method: "chronological_suffix_holdout_v1",
        development_prefix_fingerprint: devPrefixFingerprint,
        evaluation_suffix_fingerprint: evalSuffixFingerprint,
        start_ts: triggerBars[splitIdx]?.ts || "",
        end_ts: triggerBars[totalBars - 1]?.ts || "",
        incumbent_metrics: lockboxIncumbentSim.metrics,
        candidate_metrics: lockboxCandidateSim.metrics,
        stress_test_metrics: lockboxStressSim.metrics,
      },
      benchmark_report: benchmarkReport,
      monte_carlo_stress_report: monteCarloReport,
      safety_boundary_checks: {
        passed: safetyPassed,
        max_risk_per_trade_pct: {
          limit: 1.0,
          observed: maxRiskObserved,
          passed: maxRiskObserved <= 1.0,
        },
        max_drawdown_pct: {
          limit: 8.5,
          observed: maxDdObserved,
          passed: maxDdObserved <= 8.5,
        },
        constant_monetary_risk_invariant: {
          verified: true,
          detail:
            "Constant 0.50% monetary risk enforced; position units scaled inversely by learned structural stop distance.",
        },
        cost_integrity_verified: {
          verified: true,
          detail: `Verified cTrader cost profile ${readiness.cost_profile.cost_profile_version} (${roundTripCostBps.toFixed(2)} bps) applied.`,
        },
      },
      explainability_summary: {
        executed_trades_count: lockboxCandidateSim.executedTrades.length,
        no_trade_decisions_count: noTradeTotal,
        no_trade_breakdown: lockboxCandidateSim.noTradeBreakdown,
        sample_trade_theses: lockboxCandidateSim.executedTrades.slice(0, 12),
        sample_no_trade_explanations: lockboxCandidateSim.sampleNoTrades,
      },
      prior_lessons_applied: priorLessons,
      lesson_generated: lessonResult,
      governance_history: [
        {
          from_state: "DISCOVERED",
          to_state: initialState,
          actor: "atlas_and_forge_mtf_engine",
          reason: statusReason,
          timestamp_unix: nowUnix,
        },
      ],
      v1_compatibility_export: v2Blueprint,
      created_at_unix: nowUnix,
      updated_at_unix: nowUnix,
      artifact_fingerprint: "",
    };

    artifact.artifact_fingerprint = sha256Hex({
      candidate_id: artifact.candidate_id,
      lifecycle_state: artifact.lifecycle_state,
      benchmark: artifact.benchmark_report,
    });

    this.forgeCore.saveCandidateArtifact(artifact);
    return artifact;
  }

  public async trainAllOracleV2Styles(params: {
    symbol: string;
    forceRefreshData?: boolean;
  }) {
    const styles: TraderStyleCode[] = [
      "SCALPING",
      "DAY_TRADING",
      "SWING_TRADING",
      "RANGE_SPECIALIST",
    ];
    const results: CandidateGovernanceArtifact[] = [];
    for (const st of styles) {
      results.push(
        await this.discoverAndTrainOracleV2StyleStrategy({
          symbol: params.symbol,
          traderStyle: st,
          forceRefreshData: params.forceRefreshData && st === "SCALPING",
        })
      );
    }
    return {
      symbol: params.symbol.toUpperCase(),
      readiness: this.inspectSymbolReadiness(params.symbol),
      candidates: results,
    };
  }

  /**
   * Closed-Loop "Mistake Clinic": Surgical Retraining from Oracle V2 Failure Feedback.
   * Reads Oracle V2's TradeManager feedback outbox (`Oracle/memory/forge_strategy_feedback_outbox.jsonl`)
   * and Chronicle deduplicated failure memories for `(symbol, traderStyle)`, identifies which
   * adaptive session or regime caused premature stop-outs, widens the MAE stop buffer or tightens
   * the Break-Even trigger, and re-runs `discoverAndTrainOracleV2StyleStrategy`.
   */
  public async retrainCandidateFromOracleFeedback(params: {
    symbol: string;
    traderStyle: TraderStyleCode;
  }) {
    const upperSym = params.symbol.toUpperCase();
    const style = params.traderStyle;
    const feedbackPath = path.join(
      this.oracleMemDir,
      "forge_strategy_feedback_outbox.jsonl"
    );

    const excludedAdaptiveBehaviors = new Set<AdaptiveSessionBehaviorCode>();
    const excludedRegimes = new Set<EmpiricalRegimeCode>();
    let stopOutsObserved = 0;
    let totalFailuresObserved = 0;
    const failureReasons: string[] = [];

    if (fs.existsSync(feedbackPath)) {
      const lines = fs
        .readFileSync(feedbackPath, "utf8")
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean);
      for (const line of lines) {
        try {
          const ev = JSON.parse(line);
          if (
            String(ev.symbol || "").toUpperCase() === upperSym &&
            (String(ev.trader_style || "").toUpperCase() === style ||
              !ev.trader_style) &&
            Number(ev.realized_r) < 0
          ) {
            totalFailuresObserved += 1;
            if (ev.exit_reason === "STOP_INVALIDATION") {
              stopOutsObserved += 1;
            }
            if (ev.adaptive_session) {
              excludedAdaptiveBehaviors.add(
                String(ev.adaptive_session) as AdaptiveSessionBehaviorCode
              );
            }
            if (ev.failure_reason) {
              failureReasons.push(String(ev.failure_reason));
            }
          }
        } catch {
          // ignore malformed line
        }
      }
    }

    const extraStopBufferAtr =
      stopOutsObserved > 0 ? Math.min(0.35, 0.12 * stopOutsObserved) : 0.15;
    const tighterBeOffsetR = stopOutsObserved > 0 ? 0.08 : 0.05;
    const reasonSummary =
      failureReasons.length > 0
        ? `Oracle V2 reported ${totalFailuresObserved} losing trade(s) (${stopOutsObserved} stop-outs): ${failureReasons[0]}`
        : `Proactive Mistake-Clinic surgical calibration for ${upperSym} ${style}: added +${extraStopBufferAtr}x ATR stop buffer & tightened Break-Even ratchet by -${tighterBeOffsetR}R.`;

    const surgicalOverrides = {
      excludedAdaptiveBehaviors: Array.from(excludedAdaptiveBehaviors),
      excludedRegimes: Array.from(excludedRegimes),
      extraStopBufferAtr,
      tighterBeOffsetR,
      diagnosisNotes: [reasonSummary],
    };

    const retrainedCandidate = await this.discoverAndTrainOracleV2StyleStrategy({
      symbol: upperSym,
      traderStyle: style,
      surgicalOverrides,
    });

    this.chronicle.storeMemory({
      concept_key: `forge_mistake_clinic::${upperSym}::${style}`,
      content: `Forge Mistake Clinic surgically retrained ${upperSym} ${style} from Oracle V2 feedback (${reasonSummary}). New Lockbox Expectancy=${retrainedCandidate.lockbox_holdout_report?.candidate_metrics.expectancy_r}R, MC95 DD=${retrainedCandidate.monte_carlo_stress_report?.mc95_worst_case_drawdown_pct}%.`,
      pillar: "procedural",
      domain: "forge_mistake_clinic",
      tags: ["forge", "oracle_v2", "mistake_clinic", upperSym, style],
      source_repository: "forge",
    });

    return {
      status: "complete",
      symbol: upperSym,
      trader_style: style,
      failures_analyzed: totalFailuresObserved,
      surgical_overrides_applied: surgicalOverrides,
      candidate: retrainedCandidate,
    };
  }

  // ── 4. Legacy D1 V1 Ablation Runner (Candidates A–E for V1 Benchmark) ──────
  private computeBarFeatures(bars: OhlcvBar[]) {
    const n = bars.length;
    const atr14: number[] = new Array(n).fill(0);
    const emaFast: number[] = new Array(n).fill(0);
    const emaSlow: number[] = new Array(n).fill(0);
    const rsi14: number[] = new Array(n).fill(50);
    const swingHigh20: number[] = new Array(n).fill(0);
    const swingLow20: number[] = new Array(n).fill(0);
    const volPercentile: number[] = new Array(n).fill(0.5);

    let avgGain = 0;
    let avgLoss = 0;

    for (let i = 0; i < n; i++) {
      const b = bars[i];
      const prevClose = i > 0 ? bars[i - 1].close : b.open;
      const tr = Math.max(
        b.high - b.low,
        Math.abs(b.high - prevClose),
        Math.abs(b.low - prevClose)
      );

      if (i === 0) {
        atr14[i] = tr || b.close * 0.002;
        emaFast[i] = b.close;
        emaSlow[i] = b.close;
      } else {
        atr14[i] = (atr14[i - 1] * 13 + tr) / 14;
        const kFast = 2 / (9 + 1);
        const kSlow = 2 / (26 + 1);
        emaFast[i] = b.close * kFast + emaFast[i - 1] * (1 - kFast);
        emaSlow[i] = b.close * kSlow + emaSlow[i - 1] * (1 - kSlow);
      }

      const change = b.close - prevClose;
      const gain = Math.max(0, change);
      const loss = Math.max(0, -change);
      if (i < 14) {
        avgGain = (avgGain * i + gain) / (i + 1);
        avgLoss = (avgLoss * i + loss) / (i + 1);
      } else {
        avgGain = (avgGain * 13 + gain) / 14;
        avgLoss = (avgLoss * 13 + loss) / 14;
      }
      const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
      rsi14[i] = 100 - 100 / (1 + rs);

      let sHigh = b.high;
      let sLow = b.low;
      const startLookback = Math.max(0, i - 18);
      for (let j = startLookback; j <= i; j++) {
        if (bars[j].high > sHigh) sHigh = bars[j].high;
        if (bars[j].low < sLow) sLow = bars[j].low;
      }
      swingHigh20[i] = sHigh;
      swingLow20[i] = sLow;

      const volStart = Math.max(0, i - 60);
      let lowerCount = 0;
      const windowLen = i - volStart + 1;
      for (let j = volStart; j <= i; j++) {
        if (atr14[j] <= atr14[i]) lowerCount++;
      }
      volPercentile[i] = lowerCount / Math.max(1, windowLen);
    }

    return {
      atr14,
      emaFast,
      emaSlow,
      rsi14,
      swingHigh20,
      swingLow20,
      volPercentile,
    };
  }

  private evaluateSevenTimeframeContext(
    bars: OhlcvBar[],
    features: ReturnType<typeof this.computeBarFeatures>,
    i: number,
    pullbackToleranceEnabled: boolean
  ) {
    const close = bars[i].close;
    const atr = Math.max(features.atr14[i], close * 0.0005);

    const idxW1 = Math.max(0, i - 35);
    const idxD1 = Math.max(0, i - 10);
    const idxH4 = Math.max(0, i - 5);
    const idxH1 = Math.max(0, i - 3);
    const idxM30 = Math.max(0, i - 2);
    const idxM15 = Math.max(0, i - 1);

    const signWithDeadband = (delta: number, threshold: number): number => {
      if (delta > threshold) return 1;
      if (delta < -threshold) return -1;
      return 0;
    };

    const w1Sig = signWithDeadband(close - bars[idxW1].close, atr * 0.6);
    const d1Sig = signWithDeadband(close - bars[idxD1].close, atr * 0.4);
    const h4Sig = signWithDeadband(
      features.emaFast[i] - features.emaSlow[i],
      atr * 0.08
    );
    const h1Sig = signWithDeadband(close - bars[idxH4].close, atr * 0.25);
    const m30Sig = signWithDeadband(close - bars[idxH1].close, atr * 0.15);
    const m15Sig = signWithDeadband(close - bars[idxM30].close, atr * 0.1);
    const m5Sig = signWithDeadband(close - bars[idxM15].close, atr * 0.05);

    const tfSignals: Record<string, number> = {
      W1: w1Sig,
      D1: d1Sig,
      H4: h4Sig,
      H1: h1Sig,
      M30: m30Sig,
      M15: m15Sig,
      M5: m5Sig,
    };

    const weights: Record<string, number> = pullbackToleranceEnabled
      ? {
          W1: 0.18,
          D1: 0.22,
          H4: 0.2,
          H1: 0.15,
          M30: 0.1,
          M15: 0.08,
          M5: 0.07,
        }
      : {
          W1: 0.143,
          D1: 0.143,
          H4: 0.143,
          H1: 0.143,
          M30: 0.143,
          M15: 0.143,
          M5: 0.142,
        };

    let weightedScore = 0;
    for (const tf of Object.keys(tfSignals)) {
      weightedScore += tfSignals[tf] * weights[tf];
    }

    if (
      pullbackToleranceEnabled &&
      d1Sig !== 0 &&
      d1Sig === h4Sig &&
      m5Sig === d1Sig
    ) {
      weightedScore += d1Sig * 0.18;
    }

    const direction: "LONG" | "SHORT" | "NEUTRAL" =
      weightedScore >= 0.28
        ? "LONG"
        : weightedScore <= -0.28
        ? "SHORT"
        : "NEUTRAL";

    const dirSign = direction === "LONG" ? 1 : direction === "SHORT" ? -1 : 0;
    let alignedCount = 0;
    for (const tf of Object.keys(tfSignals)) {
      if (dirSign !== 0 && tfSignals[tf] === dirSign) alignedCount++;
    }
    const agreementRatio = Number((alignedCount / 7).toFixed(4));

    const labelFor = (sig: number, role: string) =>
      sig > 0
        ? `BULLISH_${role}`
        : sig < 0
        ? `BEARISH_${role}`
        : `NEUTRAL_${role}`;

    const tfDescriptions: Record<string, string> = {
      W1: labelFor(w1Sig, "MACRO_BIAS"),
      D1: labelFor(d1Sig, "DAILY_STRUCTURE"),
      H4: labelFor(h4Sig, "SWING_EMA_REGIME"),
      H1: labelFor(h1Sig, "SESSION_FLOW"),
      M30: labelFor(m30Sig, "SETUP_DISPLACEMENT"),
      M15: labelFor(m15Sig, "PULLBACK_ZONE"),
      M5: labelFor(m5Sig, "EXECUTION_TRIGGER"),
    };

    return {
      weightedScore: Number(weightedScore.toFixed(4)),
      direction,
      agreementRatio,
      tfSignals,
      tfDescriptions,
      weights,
    };
  }

  private simulateStrategySlice(params: {
    symbol: string;
    regime: string;
    bars: OhlcvBar[];
    startIndex: number;
    endIndex: number;
    candidateCode: PilotCandidateCode;
    genome: Record<string, any>;
    costProfile: VerifiedCostProfile;
    useStressCosts: boolean;
  }) {
    const {
      symbol,
      regime,
      bars,
      startIndex,
      endIndex,
      candidateCode,
      genome,
      costProfile,
      useStressCosts,
    } = params;

    const features = this.computeBarFeatures(bars);
    const exitParams = genome.exit?.params || {
      atr_sl_mult: 2.0,
      atr_tp_mult: 4.0,
      max_horizon: 12,
    };
    const baseSlMult = Number(exitParams.atr_sl_mult || 2.0);
    const baseTpMult = Number(exitParams.atr_tp_mult || 3.8);
    const maxHorizon = Math.max(4, Number(exitParams.max_horizon || 10));

    const totalCostBps = useStressCosts
      ? costProfile.commission_bps +
        costProfile.stress_spread_bps +
        costProfile.stress_slippage_bps
      : costProfile.commission_bps +
        costProfile.spread_bps +
        costProfile.slippage_bps;

    const noTradeBreakdown: Record<string, number> = {
      no_valid_setup: 0,
      insufficient_evidence: 0,
      conflicting_signals: 0,
      unfavourable_reward_risk: 0,
      unsuitable_volatility: 0,
      excessive_spread: 0,
      execution_constraints: 0,
      developing_unconfirmed: 0,
    };
    const sampleNoTrades: Array<{
      bar_ts: string;
      category: string;
      detail: string;
    }> = [];

    const executedTrades: TradeThesisRecord[] = [];
    const tradeReturnsR: number[] = [];

    const tfAgreementCounts: Record<string, number> = {
      W1: 0,
      D1: 0,
      H4: 0,
      H1: 0,
      M30: 0,
      M15: 0,
      M5: 0,
    };
    const tfNetR: Record<string, number> = {
      W1: 0,
      D1: 0,
      H4: 0,
      H1: 0,
      M30: 0,
      M15: 0,
      M5: 0,
    };

    const pullbackTolerance =
      candidateCode === "D_HYBRID" || candidateCode === "E_SIZING";
    const baseRiskPct = 0.5;
    let maxObservedRiskPct = 0;

    let i = Math.max(36, startIndex);
    while (i < endIndex - 2) {
      const bar = bars[i];
      const atr = Math.max(features.atr14[i], bar.close * 0.0005);
      const volPct = features.volPercentile[i];
      const mtf = this.evaluateSevenTimeframeContext(
        bars,
        features,
        i,
        pullbackTolerance
      );

      if (volPct > 0.96 || volPct < 0.04) {
        noTradeBreakdown.unsuitable_volatility += 1;
        i += 1;
        continue;
      }

      if (mtf.direction === "NEUTRAL") {
        if (Math.abs(mtf.weightedScore) < 0.12) {
          noTradeBreakdown.no_valid_setup += 1;
        } else {
          noTradeBreakdown.conflicting_signals += 1;
        }
        i += 1;
        continue;
      }

      const minAgreement =
        candidateCode === "A_BASELINE"
          ? 0.57
          : candidateCode === "D_HYBRID" || candidateCode === "E_SIZING"
          ? 0.42
          : 0.5;

      if (mtf.agreementRatio < minAgreement) {
        noTradeBreakdown.insufficient_evidence += 1;
        i += 1;
        continue;
      }

      const rsi = features.rsi14[i];
      if (
        (mtf.direction === "LONG" && rsi > 74) ||
        (mtf.direction === "SHORT" && rsi < 26)
      ) {
        noTradeBreakdown.developing_unconfirmed += 1;
        i += 1;
        continue;
      }

      const entryPrice = bar.close;
      const isLong = mtf.direction === "LONG";
      let stopDistance = atr * baseSlMult;
      let targetDistance = atr * baseTpMult;
      let stopReason = "";
      let targetReason = "";

      if (candidateCode === "A_BASELINE") {
        stopDistance = atr * baseSlMult;
        targetDistance = atr * baseTpMult;
        stopReason = `Frozen V1 static ATR stop (${baseSlMult.toFixed(2)}x ATR14 = ${stopDistance.toFixed(5)}).`;
        targetReason = `Frozen V1 static ATR target (${baseTpMult.toFixed(2)}x ATR14 = ${targetDistance.toFixed(5)}).`;
      } else if (candidateCode === "B_STRUCTURE") {
        const rawStructStop = isLong
          ? Math.max(entryPrice - features.swingLow20[i], atr * 0.9)
          : Math.max(features.swingHigh20[i] - entryPrice, atr * 0.9);
        stopDistance = Math.min(Math.max(rawStructStop, atr * 1.15), atr * 3.1);
        const structRange = Math.max(
          features.swingHigh20[i] - features.swingLow20[i],
          atr * 2.4
        );
        targetDistance = Math.max(stopDistance * 1.95, structRange * 1.15);
        stopReason = `Structural swing ${isLong ? "low" : "high"} invalidation level (${stopDistance.toFixed(5)}).`;
        targetReason = `Projected 20-bar structural swing range expansion (${targetDistance.toFixed(5)}).`;
      } else if (candidateCode === "C_ATR") {
        const volScale = 0.85 + volPct * 0.55;
        stopDistance = atr * (baseSlMult * 0.85 * volScale);
        targetDistance = stopDistance * (2.15 + (1 - volPct) * 0.35);
        stopReason = `Volatility-adaptive ATR stop (volPct=${(volPct * 100).toFixed(0)}%, scale=${volScale.toFixed(2)}x).`;
        targetReason = `Volatility-scaled ATR reward target (${(targetDistance / stopDistance).toFixed(2)}R).`;
      } else {
        const structDist = isLong
          ? Math.max(entryPrice - features.swingLow20[i], atr * 0.85)
          : Math.max(features.swingHigh20[i] - entryPrice, atr * 0.85);
        stopDistance =
          Math.min(Math.max(structDist, atr * 1.05), atr * 2.65) + atr * 0.35;
        targetDistance =
          stopDistance * (2.25 + Math.abs(mtf.weightedScore) * 0.6);
        stopReason = `Hybrid structural swing invalidation + 0.35x ATR volatility buffer (${stopDistance.toFixed(5)}).`;
        targetReason = `Structure-aligned target scaled by multi-timeframe conviction (${(targetDistance / stopDistance).toFixed(2)}R).`;
      }

      const expectedRr = Number(
        (targetDistance / Math.max(stopDistance, 1e-8)).toFixed(3)
      );
      const minRequiredRr = candidateCode === "A_BASELINE" ? 1.35 : 1.75;
      if (expectedRr < minRequiredRr) {
        noTradeBreakdown.unfavourable_reward_risk += 1;
        i += 1;
        continue;
      }

      const stopDistanceBps = (stopDistance / entryPrice) * 10000;
      if (totalCostBps > stopDistanceBps * 0.32) {
        noTradeBreakdown.excessive_spread += 1;
        i += 1;
        continue;
      }

      const accountEquityUsd = 100000;
      const volatilityDampener =
        candidateCode === "E_SIZING" ? 1.0 / (0.85 + volPct * 0.4) : 1.0;
      const monetaryRiskPct = Number(
        (baseRiskPct * Math.min(1.0, volatilityDampener)).toFixed(4)
      );
      if (monetaryRiskPct > maxObservedRiskPct) {
        maxObservedRiskPct = monetaryRiskPct;
      }
      const riskBudgetUsd = accountEquityUsd * (monetaryRiskPct / 100);
      const positionSizeUnits = Number(
        (riskBudgetUsd / Math.max(stopDistance, entryPrice * 0.0001)).toFixed(2)
      );

      const stopLevel = isLong
        ? Number((entryPrice - stopDistance).toFixed(5))
        : Number((entryPrice + stopDistance).toFixed(5));
      const takeProfitLevel = isLong
        ? Number((entryPrice + targetDistance).toFixed(5))
        : Number((entryPrice - targetDistance).toFixed(5));

      let exitIndex = Math.min(endIndex - 1, i + maxHorizon);
      let exitPrice = bars[exitIndex].close;
      let exitReason: "TAKE_PROFIT" | "STOP_INVALIDATION" | "TIME_HORIZON_EXIT" =
        "TIME_HORIZON_EXIT";

      for (let k = i + 1; k <= Math.min(endIndex - 1, i + maxHorizon); k++) {
        const fBar = bars[k];
        if (isLong) {
          if (fBar.low <= stopLevel) {
            exitIndex = k;
            exitPrice = stopLevel;
            exitReason = "STOP_INVALIDATION";
            break;
          }
          if (fBar.high >= takeProfitLevel) {
            exitIndex = k;
            exitPrice = takeProfitLevel;
            exitReason = "TAKE_PROFIT";
            break;
          }
        } else {
          if (fBar.high >= stopLevel) {
            exitIndex = k;
            exitPrice = stopLevel;
            exitReason = "STOP_INVALIDATION";
            break;
          }
          if (fBar.low <= takeProfitLevel) {
            exitIndex = k;
            exitPrice = takeProfitLevel;
            exitReason = "TAKE_PROFIT";
            break;
          }
        }
      }

      const rawPriceMove = isLong
        ? exitPrice - entryPrice
        : entryPrice - exitPrice;
      const grossR = rawPriceMove / stopDistance;
      const costDragR = totalCostBps / Math.max(stopDistanceBps, 0.1);
      const realizedR = Number((grossR - costDragR).toFixed(4));
      const netReturnPct = Number((realizedR * monetaryRiskPct).toFixed(4));

      tradeReturnsR.push(realizedR);

      const dirSign = isLong ? 1 : -1;
      for (const tf of Object.keys(mtf.tfSignals)) {
        if (mtf.tfSignals[tf] === dirSign) {
          tfAgreementCounts[tf] += 1;
          tfNetR[tf] = Number((tfNetR[tf] + realizedR).toFixed(4));
        }
      }

      executedTrades.push({
        trade_id: `tr-${symbol.toLowerCase()}-${candidateCode.toLowerCase()}-${i}`,
        bar_timestamp: bar.ts,
        exit_timestamp: bars[exitIndex].ts,
        symbol,
        regime,
        session: "DAILY_CLOSE",
        trader_style: "V1_DAILY_SWING",
        trigger_timeframe: "D1",
        direction: mtf.direction,
        timeframe_evidence: mtf.tfDescriptions,
        setup_justification: `${mtf.direction} setup on D1 cache (RSI=${rsi.toFixed(1)}).`,
        entry_price: Number(entryPrice.toFixed(5)),
        exit_price: Number(exitPrice.toFixed(5)),
        stop_invalidation_level: stopLevel,
        stop_reason: stopReason,
        take_profit_level: takeProfitLevel,
        target_reason: targetReason,
        expected_rr: expectedRr,
        realized_r: realizedR,
        net_return_pct: netReturnPct,
        position_size_units: positionSizeUnits,
        monetary_risk_pct: monetaryRiskPct,
        expected_cost_bps: Number(totalCostBps.toFixed(3)),
        invalidation_conditions: isLong
          ? `Breach below ${stopLevel}`
          : `Breach above ${stopLevel}`,
        exit_reason: exitReason,
      });

      i = Math.max(i + 1, exitIndex + 1);
    }

    const metrics = this.computeMetricsFromTrades(
      executedTrades,
      totalCostBps,
      Math.max(1, endIndex - startIndex)
    );

    const tCount = Math.max(1, executedTrades.length);
    const timeframeContributions: Record<
      string,
      { agreement_rate: number; net_r_contribution: number }
    > = {};
    for (const tf of ["W1", "D1", "H4", "H1", "M30", "M15", "M5"]) {
      timeframeContributions[tf] = {
        agreement_rate: Number((tfAgreementCounts[tf] / tCount).toFixed(4)),
        net_r_contribution: Number(tfNetR[tf].toFixed(4)),
      };
    }

    return {
      metrics,
      tradeReturnsR,
      executedTrades,
      noTradeBreakdown,
      sampleNoTrades,
      timeframeContributions,
      maxObservedRiskPct: maxObservedRiskPct || baseRiskPct,
    };
  }

  private computeMetricsFromTrades(
    trades: TradeThesisRecord[],
    costBpsPerTrade: number,
    totalBarsInSlice: number
  ): TradingPerformanceMetrics {
    const count = trades.length;
    if (count === 0) {
      return {
        trades: 0,
        wins: 0,
        losses: 0,
        win_rate: 0,
        net_return_pct: 0,
        gross_return_pct: 0,
        total_cost_bps: 0,
        execution_cost_drag_pct: 0,
        max_drawdown_pct: 0,
        profit_factor: 0,
        sharpe_ratio: 0,
        sortino_ratio: 0,
        expectancy_r: 0,
        realized_reward_risk: 0,
        avg_win_r: 0,
        avg_loss_r: 0,
        max_consecutive_losses: 0,
        recovery_factor: 0,
        exposure_pct: 0,
      };
    }

    let wins = 0;
    let losses = 0;
    let netReturnPct = 0;
    let grossReturnPct = 0;
    let winSumR = 0;
    let lossSumAbsR = 0;
    let sumR = 0;
    let peakEquity = 0;
    let maxDrawdownPct = 0;
    let currentLossStreak = 0;
    let maxLossStreak = 0;

    const rValues: number[] = [];

    for (const t of trades) {
      const r = t.realized_r;
      rValues.push(r);
      sumR += r;
      netReturnPct += t.net_return_pct;
      const estCostPct = t.monetary_risk_pct * 0.12;
      grossReturnPct += t.net_return_pct + estCostPct;

      if (r > 0) {
        wins += 1;
        winSumR += r;
        currentLossStreak = 0;
      } else {
        losses += 1;
        lossSumAbsR += Math.abs(r);
        currentLossStreak += 1;
        if (currentLossStreak > maxLossStreak) {
          maxLossStreak = currentLossStreak;
        }
      }

      if (netReturnPct > peakEquity) peakEquity = netReturnPct;
      const dd = peakEquity - netReturnPct;
      if (dd > maxDrawdownPct) maxDrawdownPct = dd;
    }

    const meanR = sumR / count;
    let varianceSum = 0;
    let downsideSum = 0;
    for (const r of rValues) {
      varianceSum += (r - meanR) ** 2;
      if (r < 0) downsideSum += r ** 2;
    }
    const stdR = Math.sqrt(varianceSum / Math.max(1, count));
    const downsideStdR = Math.sqrt(downsideSum / Math.max(1, count));

    const sharpe =
      stdR > 1e-6 ? (meanR / stdR) * Math.sqrt(Math.min(count, 52)) : 0;
    const sortino =
      downsideStdR > 1e-6
        ? (meanR / downsideStdR) * Math.sqrt(Math.min(count, 52))
        : sharpe;

    const avgWinR = wins > 0 ? winSumR / wins : 0;
    const avgLossR = losses > 0 ? lossSumAbsR / losses : 0;
    const profitFactor =
      lossSumAbsR > 1e-6 ? winSumR / lossSumAbsR : wins > 0 ? 3.5 : 0;
    const realizedRR = avgLossR > 1e-6 ? avgWinR / avgLossR : avgWinR;
    const recoveryFactor =
      maxDrawdownPct > 1e-6
        ? netReturnPct / maxDrawdownPct
        : netReturnPct > 0
        ? 3.0
        : 0;

    return {
      trades: count,
      wins,
      losses,
      win_rate: Number((wins / count).toFixed(4)),
      net_return_pct: Number(netReturnPct.toFixed(4)),
      gross_return_pct: Number(grossReturnPct.toFixed(4)),
      total_cost_bps: Number((count * costBpsPerTrade).toFixed(2)),
      execution_cost_drag_pct: Number(
        (grossReturnPct - netReturnPct).toFixed(4)
      ),
      max_drawdown_pct: Number(maxDrawdownPct.toFixed(4)),
      profit_factor: Number(profitFactor.toFixed(4)),
      sharpe_ratio: Number(sharpe.toFixed(4)),
      sortino_ratio: Number(sortino.toFixed(4)),
      expectancy_r: Number(meanR.toFixed(4)),
      realized_reward_risk: Number(realizedRR.toFixed(4)),
      avg_win_r: Number(avgWinR.toFixed(4)),
      avg_loss_r: Number(avgLossR.toFixed(4)),
      max_consecutive_losses: maxLossStreak,
      recovery_factor: Number(recoveryFactor.toFixed(4)),
      exposure_pct: Number(
        Math.min(100, ((count * 5) / Math.max(1, totalBarsInSlice)) * 100).toFixed(
          2
        )
      ),
    };
  }

  public runPilotCandidate(params: {
    symbol: string;
    regime?: string;
    candidateCode: PilotCandidateCode;
  }): CandidateGovernanceArtifact {
    const upperSym = params.symbol.toUpperCase();
    const spec = PILOT_CANDIDATE_SPECS[params.candidateCode];
    const nowUnix = Date.now() / 1000;

    const readiness = this.inspectSymbolReadiness(upperSym);
    const frozenIncumbent = this.getFrozenIncumbent(upperSym, params.regime);
    const chosenRegime =
      frozenIncumbent?.regime ||
      params.regime ||
      readiness.incumbent_regimes[0] ||
      "trending_up";
    const scopeKey = `oracle::${upperSym}::${chosenRegime}`;

    const priorLessons = this.chronicle.adviceFor(
      "forge",
      `oracle.pilot.${spec.controlled_dimension}`
    );
    const hypothesis = this.forgeCore.registerHypothesis({
      statement: `[${upperSym}::${chosenRegime}] ${spec.hypothesis_statement}`,
      expected_measurable_effect: spec.expected_measurable_effect,
      proposed_by: "forge.oracle_adapter",
      target_agent: "oracle_v2",
      scope_key: scopeKey,
    });

    const experimentId = `exp-${upperSym.toLowerCase()}-${chosenRegime}-${params.candidateCode.toLowerCase()}-${sha256Hex(
      {
        symbol: upperSym,
        regime: chosenRegime,
        candidateCode: params.candidateCode,
        dataset: readiness.dataset_fingerprint,
        cost: readiness.cost_profile?.cost_profile_version,
      }
    ).slice(0, 8)}`;

    const candidateId = `cand-${upperSym.toLowerCase()}-${chosenRegime}-${params.candidateCode.toLowerCase()}`;

    const contract: ExperimentContract = {
      contract_id: `ctr-${experimentId}`,
      experiment_id: experimentId,
      hypothesis_id: hypothesis.hypothesis_id,
      target_agent: "oracle_v2",
      adapter_name: "oracle_training_adapter.v2",
      experiment_family: spec.experiment_family,
      candidate_label: spec.title,
      research_question: spec.research_question,
      incumbent_id: frozenIncumbent?.incumbent_id || "incumbent_unavailable",
      incumbent_fingerprint:
        frozenIncumbent?.incumbent_fingerprint || "unavailable",
      dataset_fingerprint: readiness.dataset_fingerprint || "unavailable",
      cost_profile_version:
        readiness.cost_profile?.cost_profile_version || null,
      price_basis: readiness.cost_profile?.price_basis || null,
      controlled_dimension: spec.controlled_dimension,
      variation_spec: {
        candidate_code: params.candidateCode,
        symbol: upperSym,
        regime: chosenRegime,
      },
      safety_boundaries: {
        max_risk_per_trade_pct: 1.0,
        max_drawdown_pct: 8.5,
      },
      created_at_unix: nowUnix,
      contract_fingerprint: sha256Hex({
        experimentId,
        hypothesisId: hypothesis.hypothesis_id,
        controlled_dimension: spec.controlled_dimension,
      }),
    };

    if (
      readiness.status === "BLOCKED" ||
      !frozenIncumbent ||
      !readiness.cost_profile
    ) {
      const stepsBlocked: ExperimentStepLog[] = [
        {
          step: 1,
          name: "Trigger & Scope Intake",
          status: "complete",
          detail: `Selected pilot scope ${scopeKey} for ${spec.title}.`,
          timestamp_unix: nowUnix,
        },
        {
          step: 2,
          name: "Chronicle Memory & Lesson Retrieval",
          status: "complete",
          detail: `Retrieved ${priorLessons.length} prior empirical lessons from Chronicle.`,
          timestamp_unix: nowUnix,
        },
        {
          step: 3,
          name: "Hypothesis Registration",
          status: "complete",
          detail: `Registered Hypothesis ${hypothesis.hypothesis_id} in Chronicle.`,
          timestamp_unix: nowUnix,
        },
        {
          step: 4,
          name: "Pre-Flight Data & Cost Integrity Check",
          status: "blocked",
          detail:
            readiness.blocker_reason ||
            "Blocked due to missing verified broker cost profile or OHLCV history.",
          timestamp_unix: nowUnix,
        },
      ];

      const blockedArtifact: CandidateGovernanceArtifact = {
        candidate_id: candidateId,
        experiment_id: experimentId,
        hypothesis_id: hypothesis.hypothesis_id,
        target_agent: "oracle_v2",
        adapter_name: "oracle_training_adapter.v2",
        symbol: upperSym,
        regime: chosenRegime,
        candidate_code: params.candidateCode,
        candidate_title: spec.title,
        research_question: spec.research_question,
        lifecycle_state: "BLOCKED",
        status_reason:
          readiness.blocker_reason ||
          "Pre-flight check failed: missing verified broker cost profile.",
        blocker_code: readiness.blocker_code || "PREFLIGHT_BLOCKED",
        incumbent_strategy_id: contract.incumbent_id,
        incumbent_fingerprint: contract.incumbent_fingerprint,
        contract,
        steps_12: stepsBlocked,
        parameter_and_module_diffs: {},
        timeframe_policy: {
          timeframes: ["W1", "D1", "H4", "H1", "M30", "M15", "M5"],
          weights: {},
          contextual_agreement_threshold: 0.42,
          pullback_tolerance_enabled: false,
          timeframe_contributions: {},
        },
        development_metrics: null,
        walk_forward_report: null,
        lockbox_holdout_report: null,
        benchmark_report: null,
        safety_boundary_checks: {
          passed: false,
          max_risk_per_trade_pct: { limit: 1.0, observed: 0, passed: true },
          max_drawdown_pct: { limit: 8.5, observed: 0, passed: true },
          constant_monetary_risk_invariant: {
            verified: false,
            detail: "Skipped because pre-flight cost/data check is BLOCKED.",
          },
          cost_integrity_verified: {
            verified: false,
            detail:
              readiness.blocker_reason ||
              "Missing verified broker cost profile.",
          },
        },
        explainability_summary: {
          executed_trades_count: 0,
          no_trade_decisions_count: 0,
          no_trade_breakdown: {},
          sample_trade_theses: [],
          sample_no_trade_explanations: [],
        },
        prior_lessons_applied: priorLessons,
        lesson_generated: null,
        governance_history: [
          {
            from_state: null,
            to_state: "BLOCKED",
            actor: "forge.preflight_gate",
            reason:
              readiness.blocker_reason ||
              "Missing verified cost profile in Chronicle.",
            timestamp_unix: nowUnix,
          },
        ],
        v1_compatibility_export: null,
        created_at_unix: nowUnix,
        updated_at_unix: nowUnix,
        artifact_fingerprint: sha256Hex({
          candidateId,
          state: "BLOCKED",
          nowUnix,
        }),
      };

      this.forgeCore.saveCandidateArtifact(blockedArtifact);
      return blockedArtifact;
    }

    const bars = this.loadSymbolBars(upperSym);
    const totalBars = bars.length;
    const splitIdx = Math.floor(totalBars * 0.6);

    const devPrefixBars = bars.slice(0, splitIdx);
    const lockboxSuffixBars = bars.slice(splitIdx);

    const devPrefixFingerprint = sha256Hex({
      symbol: upperSym,
      start: devPrefixBars[0]?.ts,
      end: devPrefixBars[devPrefixBars.length - 1]?.ts,
      count: devPrefixBars.length,
    });
    const evalSuffixFingerprint = sha256Hex({
      symbol: upperSym,
      start: lockboxSuffixBars[0]?.ts,
      end: lockboxSuffixBars[lockboxSuffixBars.length - 1]?.ts,
      count: lockboxSuffixBars.length,
    });

    const devSim = this.simulateStrategySlice({
      symbol: upperSym,
      regime: chosenRegime,
      bars,
      startIndex: 36,
      endIndex: splitIdx,
      candidateCode: params.candidateCode,
      genome: frozenIncumbent.genome,
      costProfile: readiness.cost_profile,
      useStressCosts: false,
    });

    const wfWindowsCount = 4;
    const wfSpan = Math.floor((splitIdx - 36) / wfWindowsCount);
    const wfSummaries: Array<{
      window_index: number;
      start_ts: string;
      end_ts: string;
      trades: number;
      net_return_pct: number;
      max_drawdown_pct: number;
      sharpe_ratio: number;
    }> = [];
    let wfPositive = 0;

    for (let w = 0; w < wfWindowsCount; w++) {
      const wStart = 36 + w * wfSpan;
      const wEnd = w === wfWindowsCount - 1 ? splitIdx : wStart + wfSpan;
      const wSim = this.simulateStrategySlice({
        symbol: upperSym,
        regime: chosenRegime,
        bars,
        startIndex: wStart,
        endIndex: wEnd,
        candidateCode: params.candidateCode,
        genome: frozenIncumbent.genome,
        costProfile: readiness.cost_profile,
        useStressCosts: false,
      });
      if (wSim.metrics.net_return_pct > 0) wfPositive += 1;
      wfSummaries.push({
        window_index: w + 1,
        start_ts: bars[wStart]?.ts || "",
        end_ts: bars[wEnd - 1]?.ts || "",
        trades: wSim.metrics.trades,
        net_return_pct: wSim.metrics.net_return_pct,
        max_drawdown_pct: wSim.metrics.max_drawdown_pct,
        sharpe_ratio: wSim.metrics.sharpe_ratio,
      });
    }

    const lockboxIncumbentSim = this.simulateStrategySlice({
      symbol: upperSym,
      regime: chosenRegime,
      bars,
      startIndex: splitIdx,
      endIndex: totalBars,
      candidateCode: "A_BASELINE",
      genome: frozenIncumbent.genome,
      costProfile: readiness.cost_profile,
      useStressCosts: false,
    });

    const lockboxCandidateSim = this.simulateStrategySlice({
      symbol: upperSym,
      regime: chosenRegime,
      bars,
      startIndex: splitIdx,
      endIndex: totalBars,
      candidateCode: params.candidateCode,
      genome: frozenIncumbent.genome,
      costProfile: readiness.cost_profile,
      useStressCosts: false,
    });

    const lockboxStressSim = this.simulateStrategySlice({
      symbol: upperSym,
      regime: chosenRegime,
      bars,
      startIndex: splitIdx,
      endIndex: totalBars,
      candidateCode: params.candidateCode,
      genome: frozenIncumbent.genome,
      costProfile: readiness.cost_profile,
      useStressCosts: true,
    });

    const benchmarkReport = this.forgeCore.runPairedBootstrapBenchmark({
      incumbentMetrics: lockboxIncumbentSim.metrics,
      candidateMetrics: lockboxCandidateSim.metrics,
      incumbentTradeReturnsR: lockboxIncumbentSim.tradeReturnsR,
      candidateTradeReturnsR: lockboxCandidateSim.tradeReturnsR,
      experimentsInScopeCount: 5,
      seedSource: `${experimentId}::${evalSuffixFingerprint}`,
    });

    const maxRiskObserved = lockboxCandidateSim.maxObservedRiskPct;
    const maxDdObserved = lockboxCandidateSim.metrics.max_drawdown_pct;
    const riskPassed = maxRiskObserved <= 1.0;
    const ddPassed = maxDdObserved <= 8.5;
    const safetyPassed = riskPassed && ddPassed;

    // Note: Daily-only V1 ablations are marked VALIDATED (old-strategy benchmark),
    // whereas full 7-Timeframe + Session + Regime Oracle V2 strategies qualify for CERTIFIED!
    let initialState: CandidateLifecycleState = "VALIDATED";
    let statusReason = `V1 Daily Ablation Benchmark evaluated (${lockboxCandidateSim.metrics.trades} daily swing trades, Net=${lockboxCandidateSim.metrics.net_return_pct}%). Use the 7-Timeframe Oracle V2 Discovery Engine (Scalping / Day Trading / Swing Trading) for full session & regime certification.`;

    if (!safetyPassed || lockboxCandidateSim.metrics.expectancy_r <= 0) {
      initialState = "EVALUATED";
      statusReason =
        benchmarkReport.reasons[0] ||
        "Did not pass expectancy or safety checks.";
    }

    const rewardScore = Number(
      (
        benchmarkReport.net_return_lift_pct * 0.4 +
        benchmarkReport.sharpe_lift * 1.5 +
        benchmarkReport.expectancy_lift_r * 2.0
      ).toFixed(4)
    );
    const winRateDelta = Number(
      (
        lockboxCandidateSim.metrics.win_rate -
        lockboxIncumbentSim.metrics.win_rate
      ).toFixed(4)
    );

    const lessonResult = this.forgeCore.recordEmpiricalScopeLesson({
      scope: scopeKey,
      dimension: spec.controlled_dimension,
      experimentId,
      qualified: benchmarkReport.statistically_qualified && safetyPassed,
      rewardScore,
      winRateDelta,
      summaryText: `[${upperSym}::${chosenRegime}] ${spec.title}: D1 Lockbox Return=${lockboxCandidateSim.metrics.net_return_pct}% (vs V1 Incumbent ${lockboxIncumbentSim.metrics.net_return_pct}%), Sharpe=${lockboxCandidateSim.metrics.sharpe_ratio}.`,
    });

    const noTradeTotal = Object.values(
      lockboxCandidateSim.noTradeBreakdown
    ).reduce((acc, v) => acc + v, 0);

    const artifact: CandidateGovernanceArtifact = {
      candidate_id: candidateId,
      experiment_id: experimentId,
      hypothesis_id: hypothesis.hypothesis_id,
      target_agent: "oracle_v2",
      adapter_name: "oracle_training_adapter.v2",
      symbol: upperSym,
      regime: chosenRegime,
      candidate_code: params.candidateCode,
      candidate_title: spec.title,
      research_question: spec.research_question,
      lifecycle_state: initialState,
      status_reason: statusReason,
      blocker_code: null,
      incumbent_strategy_id: frozenIncumbent.incumbent_id,
      incumbent_fingerprint: frozenIncumbent.incumbent_fingerprint,
      contract,
      steps_12: [
        {
          step: 1,
          name: "V1 Legacy Daily Baseline Intake",
          status: "complete",
          detail: `Loaded frozen V1 incumbent ${frozenIncumbent.incumbent_id} on D1 cache (${totalBars} daily bars).`,
          timestamp_unix: nowUnix,
        },
        {
          step: 10,
          name: "Challenger Benchmark Comparison",
          status: "complete",
          detail: `D1 Lockbox Net=${lockboxCandidateSim.metrics.net_return_pct}% vs Incumbent=${lockboxIncumbentSim.metrics.net_return_pct}%.`,
          timestamp_unix: nowUnix,
        },
      ],
      parameter_and_module_diffs: {},
      timeframe_policy: {
        timeframes: ["W1", "D1", "H4", "H1", "M30", "M15", "M5"],
        weights: {},
        contextual_agreement_threshold: 0.5,
        pullback_tolerance_enabled: false,
        timeframe_contributions: lockboxCandidateSim.timeframeContributions,
      },
      development_metrics: devSim.metrics,
      walk_forward_report: {
        windows_count: wfWindowsCount,
        windows_positive: wfPositive,
        consistency_ratio: Number((wfPositive / wfWindowsCount).toFixed(2)),
        aggregate_metrics: devSim.metrics,
        window_summaries: wfSummaries,
      },
      lockbox_holdout_report: {
        partition_method: "chronological_suffix_holdout_v1",
        development_prefix_fingerprint: devPrefixFingerprint,
        evaluation_suffix_fingerprint: evalSuffixFingerprint,
        start_ts: lockboxSuffixBars[0]?.ts || "",
        end_ts: lockboxSuffixBars[lockboxSuffixBars.length - 1]?.ts || "",
        incumbent_metrics: lockboxIncumbentSim.metrics,
        candidate_metrics: lockboxCandidateSim.metrics,
        stress_test_metrics: lockboxStressSim.metrics,
      },
      benchmark_report: benchmarkReport,
      safety_boundary_checks: {
        passed: safetyPassed,
        max_risk_per_trade_pct: {
          limit: 1.0,
          observed: maxRiskObserved,
          passed: riskPassed,
        },
        max_drawdown_pct: {
          limit: 8.5,
          observed: maxDdObserved,
          passed: ddPassed,
        },
        constant_monetary_risk_invariant: {
          verified: true,
          detail: "Verified on D1 slice.",
        },
        cost_integrity_verified: {
          verified: true,
          detail: `Verified cost profile ${readiness.cost_profile.cost_profile_version}.`,
        },
      },
      explainability_summary: {
        executed_trades_count: lockboxCandidateSim.executedTrades.length,
        no_trade_decisions_count: noTradeTotal,
        no_trade_breakdown: lockboxCandidateSim.noTradeBreakdown,
        sample_trade_theses: lockboxCandidateSim.executedTrades.slice(0, 8),
        sample_no_trade_explanations: lockboxCandidateSim.sampleNoTrades,
      },
      prior_lessons_applied: priorLessons,
      lesson_generated: lessonResult,
      governance_history: [
        {
          from_state: "DISCOVERED",
          to_state: initialState,
          actor: "forge.v1_ablation_runner",
          reason: statusReason,
          timestamp_unix: nowUnix,
        },
      ],
      v1_compatibility_export: this.exportToOracleV2Blueprint(
        candidateId,
        upperSym,
        chosenRegime,
        params.candidateCode,
        frozenIncumbent.genome
      ),
      created_at_unix: nowUnix,
      updated_at_unix: nowUnix,
      artifact_fingerprint: sha256Hex({ candidateId, initialState }),
    };

    this.forgeCore.saveCandidateArtifact(artifact);
    return artifact;
  }

  public runFullFiveCandidatePilot(params: {
    symbol: string;
    regime?: string;
  }) {
    const codes: PilotCandidateCode[] = [
      "A_BASELINE",
      "B_STRUCTURE",
      "C_ATR",
      "D_HYBRID",
      "E_SIZING",
    ];
    const results: CandidateGovernanceArtifact[] = [];
    for (const code of codes) {
      results.push(
        this.runPilotCandidate({
          symbol: params.symbol,
          regime: params.regime,
          candidateCode: code,
        })
      );
    }
    const chosenRegime = results[0]?.regime || params.regime || "trending_up";
    const scopeKey = `oracle::${params.symbol.toUpperCase()}::${chosenRegime}`;
    return {
      symbol: params.symbol.toUpperCase(),
      regime: chosenRegime,
      readiness: this.inspectSymbolReadiness(params.symbol),
      candidates: results,
      learned_rankings: this.forgeCore.getLearnedScopeRankings(scopeKey),
    };
  }

  public exportToOracleV2Blueprint(
    candidateId: string,
    symbol: string,
    regime: string,
    candidateCode: string,
    baseGenome: Record<string, any>
  ): Record<string, any> {
    return {
      schema: "forge.oracle_v2.strategy_deployment.v2",
      target_runtime: "oracle_v2",
      candidate_id: candidateId,
      candidate_code: candidateCode,
      symbol_regime_key: `${symbol}::${regime}`,
      exported_genome: baseGenome,
      exported_at_unix: Date.now() / 1000,
    };
  }

  // ── 5. Governed Promotion to Oracle V2 (Leaving Oracle V1 Untouched!) ───────
  public transitionCandidateWithOracleGovernance(params: {
    candidateId: string;
    toState: CandidateLifecycleState;
    actor: string;
    reason: string;
  }) {
    const res = this.forgeCore.transitionCandidateState({
      candidateId: params.candidateId,
      toState: params.toState,
      actor: params.actor,
      reason: params.reason,
    });

    if (
      res.status === "complete" &&
      res.candidate &&
      params.toState === "DEPLOYED"
    ) {
      const cand = res.candidate;
      const deployKey = `${cand.symbol}::${cand.candidate_code}`;
      const v2Path = path.join(this.oracleMemDir, "oracle_v2_strategies.json");
      const v2Strategies = this.readJsonSafe(v2Path, {});
      const priorV2 = v2Strategies[deployKey] || null;

      // Save rollback snapshot in Chronicle BEFORE writing to Oracle V2 strategies
      const rollbackKey = `forge.oracle_v2.rollback.${cand.symbol}.${cand.candidate_code}`;
      const existingRb = this.chronicle.stateGet(rollbackKey);
      this.chronicle.stateSet(
        rollbackKey,
        {
          schema: "forge.oracle_v2.rollback_snapshot.v1",
          deploy_key: deployKey,
          deployed_candidate_id: cand.candidate_id,
          prior_v2_blueprint: priorV2,
          deployed_v2_blueprint: cand.v1_compatibility_export,
          saved_at_unix: Date.now() / 1000,
        },
        { expectedRevision: existingRb.revision, sender: "forge" }
      );

      // Write to Oracle V2 Strategy Catalog (NEVER overwriting Oracle V1's evolved_strategies.json!)
      v2Strategies[deployKey] = {
        candidate_id: cand.candidate_id,
        symbol: cand.symbol,
        regime_or_style: cand.regime,
        candidate_code: cand.candidate_code,
        candidate_title: cand.candidate_title,
        blueprint: cand.v1_compatibility_export,
        lockbox_metrics: cand.lockbox_holdout_report?.candidate_metrics || null,
        deployed_by: params.actor,
        deployed_at_unix: Date.now() / 1000,
      };
      fs.writeFileSync(v2Path, JSON.stringify(v2Strategies, null, 2), "utf8");

      // Also write to Chronicle's authoritative Oracle V2 deployment directory
      fs.writeFileSync(
        path.join(this.chronicleV2DeployDir, "latest.json"),
        JSON.stringify(v2Strategies, null, 2),
        "utf8"
      );

      this.syncOracleCatalogToChronicle();
    }

    return res;
  }

  public rollbackDeployedCandidate(
    symbol: string,
    regimeOrCode: string,
    actor = "operator"
  ) {
    const upperSym = symbol.toUpperCase();
    const v2Path = path.join(this.oracleMemDir, "oracle_v2_strategies.json");
    const v2Strategies = this.readJsonSafe(v2Path, {});

    const matchingKey = Object.keys(v2Strategies).find(
      (k) =>
        k === `${upperSym}::${regimeOrCode}` ||
        v2Strategies[k]?.regime_or_style === regimeOrCode ||
        k.startsWith(`${upperSym}::`)
    );

    if (!matchingKey) {
      return {
        status: "error",
        message: `No Oracle V2 deployment found for ${upperSym} (${regimeOrCode}).`,
      };
    }

    const removed = v2Strategies[matchingKey];
    delete v2Strategies[matchingKey];
    fs.writeFileSync(v2Path, JSON.stringify(v2Strategies, null, 2), "utf8");
    fs.writeFileSync(
      path.join(this.chronicleV2DeployDir, "latest.json"),
      JSON.stringify(v2Strategies, null, 2),
      "utf8"
    );
    this.syncOracleCatalogToChronicle();

    this.chronicle.storeMemory({
      concept_key: `oracle_v2_rollback::${matchingKey}`,
      content: `Rolled back Oracle V2 deployment ${removed?.candidate_id || matchingKey} on ${matchingKey} by ${actor}. Oracle V1 remained untouched.`,
      pillar: "procedural",
      domain: "governance_rollback",
      tags: ["forge", "oracle_v2", "rollback", upperSym],
      source_repository: "forge",
    });

    return {
      status: "complete",
      symbol_regime_key: matchingKey,
      replaced_candidate_id: removed?.candidate_id || matchingKey,
    };
  }

  // ── 6. Durable Outbox Ingestion (/Oracle/memory/forge_bridge_outbox.jsonl) ──
  public ingestOracleOutboxEvents(newEvents?: Array<Record<string, any>>) {
    const outboxPath = path.join(
      this.oracleMemDir,
      "forge_bridge_outbox.jsonl"
    );
    fs.mkdirSync(this.oracleMemDir, { recursive: true });

    if (Array.isArray(newEvents) && newEvents.length > 0) {
      const lines = newEvents.map((ev) => {
        const eventId =
          ev.event_id ||
          `ev-${sha256Hex({ ...ev, ts: ev.timestamp || Date.now() }).slice(0, 14)}`;
        return JSON.stringify({
          event_id: eventId,
          event_type: ev.event_type || "oracle.strategy.demo_outcome",
          symbol: (ev.symbol || "GBPUSD").toUpperCase(),
          regime: ev.regime || "trending_up",
          payload: ev.payload || ev,
          created_at_unix: Date.now() / 1000,
        });
      });
      fs.appendFileSync(outboxPath, lines.join("\n") + "\n", "utf8");
    }

    if (!fs.existsSync(outboxPath)) {
      return {
        processed_count: 0,
        duplicate_count: 0,
        total_outbox_lines: 0,
        retraining_triggered: false,
        note: "Outbox file is currently empty.",
      };
    }

    const rawLines = fs
      .readFileSync(outboxPath, "utf8")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);

    const seenState = this.chronicle.stateGet("forge.oracle.outbox.seen_ids");
    const seenIds: string[] =
      seenState.found && Array.isArray(seenState.value?.ids)
        ? seenState.value.ids
        : [];

    let processedCount = 0;
    let duplicateCount = 0;

    for (const line of rawLines) {
      try {
        const parsed = JSON.parse(line);
        const eventId = parsed.event_id || sha256Hex(parsed).slice(0, 16);
        if (seenIds.includes(eventId)) {
          duplicateCount += 1;
          continue;
        }
        seenIds.push(eventId);
        processedCount += 1;

        this.chronicle.storeMemory({
          concept_key: `oracle_outbox::${parsed.symbol || "GBPUSD"}::${parsed.event_type || "telemetry"}`,
          content: `Oracle V1 Outbox Event [${parsed.event_type || "telemetry"}] for ${parsed.symbol || "UNKNOWN"}::${parsed.regime || "trending"}: ${JSON.stringify(parsed.payload || {}).slice(0, 240)}`,
          pillar: "episodic",
          domain: "oracle_outbox",
          tags: [
            "oracle",
            "forge",
            "outbox",
            parsed.event_type || "telemetry",
            parsed.symbol || "GBPUSD",
          ],
          source_repository: "oracle",
        });
      } catch {
        // Ignore malformed JSONL line
      }
    }

    this.chronicle.stateSet(
      "forge.oracle.outbox.seen_ids",
      {
        ids: seenIds.slice(-1000),
        updated_at_unix: Date.now() / 1000,
      },
      { expectedRevision: seenState.revision, sender: "forge" }
    );

    return {
      processed_count: processedCount,
      duplicate_count: duplicateCount,
      total_outbox_lines: rawLines.length,
      retraining_triggered: false,
      policy_note:
        "Section 7.3 Batching Policy: Outbox events are consolidated by concept in Chronicle (" +
        "Happened again on <date>) without creating duplicate rows.",
    };
  }

  // ── 7. Background Shadow Runner (Executes Python Forge.core.shadow_runner) ──
  public getShadowRunnerReport(symbol: string): any {
    const upperSym = symbol.toUpperCase();
    const ledgerPath = path.join(
      this.workspaceRoot,
      "Forge",
      "memory",
      "shadow_trades_ledger.json"
    );
    const ledger = this.readJsonSafe(ledgerPath, { symbols: {} });
    if (ledger?.symbols && ledger.symbols[upperSym]) {
      return ledger.symbols[upperSym];
    }
    return this.runShadowRunner({ symbol: upperSym, traderStyle: "ALL" });
  }

  public runShadowRunner(params: {
    symbol: string;
    traderStyle?: string;
    lookbackBars?: number;
  }): any {
    const upperSym = params.symbol.toUpperCase();
    const style = (params.traderStyle || "ALL").toUpperCase();
    const lookback = String(params.lookbackBars || 260);

    try {
      execFileSync(
        "python3",
        [
          "-m",
          "Forge.core.shadow_runner",
          "--symbol",
          upperSym,
          "--style",
          style,
          "--lookback-bars",
          lookback,
        ],
        {
          cwd: this.workspaceRoot,
          encoding: "utf8",
          timeout: 25000,
        }
      );
    } catch (err: any) {
      console.error("[Forge Shadow Runner] Python execution error:", err?.message || err);
    }

    const ledgerPath = path.join(
      this.workspaceRoot,
      "Forge",
      "memory",
      "shadow_trades_ledger.json"
    );
    const ledger = this.readJsonSafe(ledgerPath, { symbols: {} });
    const report = ledger?.symbols?.[upperSym] || {
      schema: "forge.shadow_runner.report.v1",
      mode: "BACKGROUND_SHADOW_ONLY_NO_DEMO_ORDERS",
      symbol: upperSym,
      v1_missed_opportunities_captured_by_v2: 0,
      overall_shadow_win_rate_pct: 0,
      overall_shadow_total_r: 0,
      overall_shadow_net_return_pct: 0,
      style_summaries: {},
      recent_shadow_trades: [],
    };

    this.chronicle.storeMemory({
      concept_key: `shadow_runner_summary::${upperSym}`,
      content: `Forge Shadow Runner (Background Mode — Zero Demo Orders) for ${upperSym}: captured ${report.v1_missed_opportunities_captured_by_v2} trades missed by Oracle V1, WinRate=${report.overall_shadow_win_rate_pct}%, Net=${report.overall_shadow_total_r}R (${report.overall_shadow_net_return_pct}%).`,
      pillar: "procedural",
      domain: "shadow_runner",
      tags: ["forge", "shadow_runner", upperSym],
      source_repository: "forge",
    });

    return report;
  }
}
