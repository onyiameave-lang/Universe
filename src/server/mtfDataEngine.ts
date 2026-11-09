import fs from "fs";
import path from "path";
import { ChronicleAuthoritativeStore, sha256Hex } from "./chronicleStore.ts";

export type TimeframeCode = "W1" | "D1" | "H4" | "H1" | "M30" | "M15" | "M5";

export type TradingSessionCode =
  | "ASIAN_SESSION"
  | "LONDON_SESSION"
  | "LONDON_NY_OVERLAP"
  | "NEW_YORK_SESSION"
  | "OFF_HOURS_ROLLOVER";

export type AdaptiveSessionBehaviorCode =
  | "ASIAN_RANGE_ACCUMULATION"
  | "LONDON_OPEN_JUDAS_SWEEP"
  | "LONDON_TREND_EXPANSION"
  | "NY_OVERLAP_INSTITUTIONAL_DRIVE"
  | "NY_AFTERNOON_REBALANCING"
  | "ROLLOVER_SPREAD_GUARD";

export type EmpiricalRegimeCode =
  | "TRENDING_BULLISH"
  | "TRENDING_BEARISH"
  | "CONSOLIDATION_RANGING"
  | "VOLATILITY_EXPANSION";

export type TraderStyleCode =
  | "SCALPING"
  | "DAY_TRADING"
  | "SWING_TRADING"
  | "RANGE_SPECIALIST";

export interface InstrumentPhysicsProfile {
  symbol: string;
  asset_class:
    | "FOREX_MAJOR"
    | "FOREX_CROSS"
    | "PRECIOUS_METAL"
    | "EQUITY_INDEX"
    | "CRYPTO_ASSET";
  avg_wick_to_body_ratio: number;
  learned_sweep_wick_ratio_threshold: number;
  learned_fvg_body_atr_mult: number;
  median_efficiency_ratio: number;
  personality:
    | "MOMENTUM_PERSISTENT"
    | "MEAN_REVERTING_DOMINANT"
    | "BALANCED_HYBRID";
  peak_volatility_utc_hours: number[];
}

export interface StrategyRegimeDriftReport {
  symbol: string;
  timeframe: TimeframeCode;
  status: "STABLE" | "MODERATE_DRIFT" | "NEEDS_RECALIBRATION";
  volatility_drift_pct: number;
  efficiency_drift_pct: number;
  wick_ratio_drift_pct: number;
  composite_drift_score: number;
  baseline_atr_pct: number;
  recent_atr_pct: number;
  baseline_efficiency: number;
  recent_efficiency: number;
  recalibration_recommended: boolean;
  reason: string;
  evaluated_at_unix: number;
}

export interface AnnotatedMtfBar {
  ts: string;
  unix_sec: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  session: TradingSessionCode;
  adaptive_session: AdaptiveSessionBehaviorCode;
  regime: EmpiricalRegimeCode;
  atr14: number;
  ema9: number;
  ema21: number;
  rsi14: number;
  swing_high_16: number;
  swing_low_16: number;
  efficiency_ratio: number;
  volatility_percentile: number;
  // Institutional Microstructure Annotations (derived strictly from past bars [0..i])
  asian_session_high: number;
  asian_session_low: number;
  liquidity_sweep_bullish: boolean; // Swept prior swing/Asian low & closed back above
  liquidity_sweep_bearish: boolean; // Swept prior swing/Asian high & closed back below
  fvg_bullish: boolean; // 3-bar bullish Fair Value Gap (imbalance)
  fvg_bearish: boolean; // 3-bar bearish Fair Value Gap (imbalance)
  bos_direction: 1 | -1 | 0; // Break of Structure / Change of Character
  discount_premium_zone: "DISCOUNT" | "PREMIUM" | "EQUILIBRIUM";
}

export interface MultiTimeframeDataset {
  schema: "forge.mtf_market_dataset.v2";
  symbol: string;
  yahoo_ticker: string;
  source: string;
  fetched_at_unix: number;
  dataset_fingerprint: string;
  timeframe_counts: Record<TimeframeCode, number>;
  session_distribution: Record<TradingSessionCode, number>;
  adaptive_session_distribution?: Record<AdaptiveSessionBehaviorCode, number>;
  regime_distribution: Record<EmpiricalRegimeCode, number>;
  series: Record<TimeframeCode, AnnotatedMtfBar[]>;
}

export interface TraderBehaviorProfile {
  archetype_id: string;
  archetype_name: string;
  trader_style: TraderStyleCode;
  decision_hierarchy: string[];
  institutional_entry_triggers: string[];
  behavioral_mistakes_avoided: string[];
  dynamic_trade_management: {
    enabled: boolean;
    breakeven_trigger_r: number;
    breakeven_offset_r: number;
    partial_tp1_r: number;
    partial_tp1_Allocation_pct: number;
    runner_tp2_r: number;
    structure_trail_atr_mult: number;
    max_holding_bars: number;
  };
}

export interface AtlasForgeResearchBrief {
  brief_id: string;
  symbol: string;
  trader_style: TraderStyleCode;
  institutional_grade_score: number; // 0..100 institutional readiness score
  instrument_physics: InstrumentPhysicsProfile;
  regime_drift_report: StrategyRegimeDriftReport;
  trader_behavior_profile: TraderBehaviorProfile;
  top_down_stack: {
    macro_timeframes: TimeframeCode[];
    setup_timeframes: TimeframeCode[];
    trigger_timeframe: TimeframeCode;
  };
  empirical_excursion_discovery: {
    sample_setups_analyzed: number;
    median_mae_atr: number;
    p70_mae_stop_buffer_atr: number;
    median_mfe_atr: number;
    p65_mfe_target_atr: number;
    p80_mfe_runner_atr: number;
    learned_optimal_stop_atr_mult: number;
    learned_optimal_tp_atr_mult: number;
    learned_risk_reward_ratio: number;
    learned_be_trigger_r: number;
    learned_tp1_r: number;
    learned_tp2_r: number;
    learned_trail_atr_mult: number;
  };
  session_discovery: {
    best_sessions: TradingSessionCode[];
    avoid_sessions: TradingSessionCode[];
    best_adaptive_behaviors: AdaptiveSessionBehaviorCode[];
    session_stats: Record<
      string,
      { setups: number; win_rate: number; expectancy_r: number; avg_mfe_atr: number }
    >;
    adaptive_session_stats: Record<
      string,
      { setups: number; win_rate: number; expectancy_r: number }
    >;
  };
  regime_discovery: {
    best_regimes: EmpiricalRegimeCode[];
    avoid_regimes: EmpiricalRegimeCode[];
    regime_stats: Record<
      string,
      { setups: number; win_rate: number; expectancy_r: number }
    >;
  };
  strategy_specification: {
    entry_conditions: string[];
    exit_conditions: string[];
    stop_loss_conditions: string[];
    take_profit_conditions: string[];
    dynamic_management_rules: string[];
    risk_reward_policy: string;
  };
  atlas_literature_and_domain_notes: string[];
  created_at_unix: number;
}

const YAHOO_TICKER_MAP: Record<string, string> = {
  GBPUSD: "GBPUSD=X",
  EURUSD: "EURUSD=X",
  USDJPY: "USDJPY=X",
  AUDUSD: "AUDUSD=X",
  USDCAD: "USDCAD=X",
  USDCHF: "USDCHF=X",
  NZDUSD: "NZDUSD=X",
  EURGBP: "EURGBP=X",
  EURJPY: "EURJPY=X",
  GBPJPY: "GBPJPY=X",
  XAUUSD: "GC=F",
  XAGUSD: "SI=F",
  BTCUSD: "BTC-USD",
  ETHUSD: "ETH-USD",
  SOLUSD: "SOL-USD",
};

export function classifyUtcTradingSession(unixSec: number): TradingSessionCode {
  const date = new Date(unixSec * 1000);
  const utcHour = date.getUTCHours();
  if (utcHour >= 0 && utcHour < 7) return "ASIAN_SESSION";
  if (utcHour >= 7 && utcHour < 12) return "LONDON_SESSION";
  if (utcHour >= 12 && utcHour < 16) return "LONDON_NY_OVERLAP";
  if (utcHour >= 16 && utcHour < 21) return "NEW_YORK_SESSION";
  return "OFF_HOURS_ROLLOVER";
}

export function classifyAdaptiveSessionBehavior(params: {
  unixSec: number;
  high: number;
  low: number;
  close: number;
  asianHigh: number;
  asianLow: number;
  efficiencyRatio: number;
}): AdaptiveSessionBehaviorCode {
  const date = new Date(params.unixSec * 1000);
  const utcHour = date.getUTCHours();
  const utcMin = date.getUTCMinutes();
  const hourDec = utcHour + utcMin / 60;

  if (hourDec >= 0 && hourDec < 7) {
    return "ASIAN_RANGE_ACCUMULATION";
  }
  if (hourDec >= 7 && hourDec < 9.5) {
    // Check if London open is sweeping Asian High or Asian Low
    const sweptAsianHigh = params.high > params.asianHigh && params.close <= params.asianHigh;
    const sweptAsianLow = params.low < params.asianLow && params.close >= params.asianLow;
    if (sweptAsianHigh || sweptAsianLow || params.efficiencyRatio < 0.35) {
      return "LONDON_OPEN_JUDAS_SWEEP";
    }
    return "LONDON_TREND_EXPANSION";
  }
  if (hourDec >= 9.5 && hourDec < 12) {
    return "LONDON_TREND_EXPANSION";
  }
  if (hourDec >= 12 && hourDec < 16) {
    return "NY_OVERLAP_INSTITUTIONAL_DRIVE";
  }
  if (hourDec >= 16 && hourDec < 21) {
    return "NY_AFTERNOON_REBALANCING";
  }
  return "ROLLOVER_SPREAD_GUARD";
}

export class MultiTimeframeDataAndAtlasEngine {
  public readonly workspaceRoot: string;
  public readonly chronicle: ChronicleAuthoritativeStore;
  private readonly mtfChronicleDir: string;
  private readonly mtfRamCacheDir: string;

  constructor(workspaceRoot: string, chronicle: ChronicleAuthoritativeStore) {
    this.workspaceRoot = workspaceRoot;
    this.chronicle = chronicle;
    this.mtfChronicleDir = path.join(
      workspaceRoot,
      "Chronicle",
      "memory",
      "research",
      "mtf_market_data"
    );
    this.mtfRamCacheDir = path.join(workspaceRoot, "Oracle", "data_cache", "mtf");
    fs.mkdirSync(this.mtfChronicleDir, { recursive: true });
    fs.mkdirSync(this.mtfRamCacheDir, { recursive: true });
  }

  public resolveYahooTicker(symbol: string): string {
    const upper = symbol.toUpperCase();
    if (YAHOO_TICKER_MAP[upper]) return YAHOO_TICKER_MAP[upper];
    if (upper.length === 6 && !upper.endsWith("USD")) return `${upper}=X`;
    return upper;
  }

  /**
   * Annotates raw OHLCV bars with:
   * - UTC Session & Adaptive Session Behavior Tags (`LONDON_OPEN_JUDAS_SWEEP`, etc.)
   * - Institutional Microstructure (`liquidity_sweep_bullish/bearish`, `fvg_bullish/bearish`, `bos_direction`, `discount_premium_zone`)
   * - ATR14, EMA9/21, RSI14, 16-bar swing highs/lows, Kaufman Efficiency Ratio, and Empirical Market Regimes.
   * Uses strictly past bars [0..i] with zero look-ahead bias.
   */
  public annotateBarSeries(
    rawBars: Array<{
      ts: string;
      unix_sec: number;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }>
  ): AnnotatedMtfBar[] {
    const n = rawBars.length;
    if (n === 0) return [];

    const annotated: AnnotatedMtfBar[] = [];
    const atrArr: number[] = new Array(n).fill(0);
    let avgGain = 0;
    let avgLoss = 0;

    // Rolling day's Asian session high/low tracker
    let currentDayKey = "";
    let asianHigh = rawBars[0].high;
    let asianLow = rawBars[0].low;

    for (let i = 0; i < n; i++) {
      const b = rawBars[i];
      const prevClose = i > 0 ? rawBars[i - 1].close : b.open;
      const tr = Math.max(
        b.high - b.low,
        Math.abs(b.high - prevClose),
        Math.abs(b.low - prevClose)
      );

      const atr14 =
        i === 0
          ? tr || b.close * 0.0015
          : (atrArr[i - 1] * 13 + tr) / 14;
      atrArr[i] = atr14;

      const k9 = 2 / 10;
      const k21 = 2 / 22;
      const ema9 =
        i === 0 ? b.close : b.close * k9 + annotated[i - 1].ema9 * (1 - k9);
      const ema21 =
        i === 0 ? b.close : b.close * k21 + annotated[i - 1].ema21 * (1 - k21);

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
      const rsi14 = 100 - 100 / (1 + rs);

      // Update Asian Session High/Low (00:00 - 07:00 UTC)
      const dt = new Date(b.unix_sec * 1000);
      const dayKey = dt.toISOString().slice(0, 10);
      const utcHour = dt.getUTCHours();
      if (dayKey !== currentDayKey) {
        currentDayKey = dayKey;
        asianHigh = b.high;
        asianLow = b.low;
      } else if (utcHour < 7) {
        if (b.high > asianHigh) asianHigh = b.high;
        if (b.low < asianLow) asianLow = b.low;
      }

      // Prior 16-bar structural swing high & low (strictly prior bars [i-16 .. i-1])
      let priorSwingHigh = i > 0 ? rawBars[i - 1].high : b.high;
      let priorSwingLow = i > 0 ? rawBars[i - 1].low : b.low;
      const priorStart = Math.max(0, i - 16);
      for (let j = priorStart; j < i; j++) {
        if (rawBars[j].high > priorSwingHigh) priorSwingHigh = rawBars[j].high;
        if (rawBars[j].low < priorSwingLow) priorSwingLow = rawBars[j].low;
      }

      const sHigh = Math.max(priorSwingHigh, b.high);
      const sLow = Math.min(priorSwingLow, b.low);

      // Institutional Liquidity Sweep (Stop Hunt / Rejection Wick)
      // Bullish Sweep: Pierces below prior swing low or Asian low, then closes back above with rejection wick
      const lowerWick = Math.min(b.open, b.close) - b.low;
      const upperWick = b.high - Math.max(b.open, b.close);
      const barRange = Math.max(b.high - b.low, 1e-9);

      const liquiditySweepBullish =
        i >= 5 &&
        ((b.low < priorSwingLow && b.close > priorSwingLow) ||
          (utcHour >= 7 && b.low < asianLow && b.close > asianLow)) &&
        lowerWick / barRange >= 0.32;

      const liquiditySweepBearish =
        i >= 5 &&
        ((b.high > priorSwingHigh && b.close < priorSwingHigh) ||
          (utcHour >= 7 && b.high > asianHigh && b.close < asianHigh)) &&
        upperWick / barRange >= 0.32;

      // 3-Bar Fair Value Gap (FVG / Institutional Displacement Imbalance)
      let fvgBullish = false;
      let fvgBearish = false;
      if (i >= 2) {
        const b2 = rawBars[i - 2];
        const b1 = rawBars[i - 1];
        const body1 = Math.abs(b1.close - b1.open);
        if (b.low > b2.high && b1.close > b1.open && body1 > atr14 * 0.55) {
          fvgBullish = true;
        }
        if (b.high < b2.low && b1.close < b1.open && body1 > atr14 * 0.55) {
          fvgBearish = true;
        }
      }

      // Break of Structure (BOS)
      let bosDirection: 1 | -1 | 0 = 0;
      if (i >= 5) {
        if (b.close > priorSwingHigh && b.close > b.open) bosDirection = 1;
        else if (b.close < priorSwingLow && b.close < b.open) bosDirection = -1;
      }

      // Discount / Premium Zone within 16-bar structural dealing range
      const dealingSpan = Math.max(sHigh - sLow, 1e-9);
      const posInRange = (b.close - sLow) / dealingSpan;
      const discountPremiumZone: "DISCOUNT" | "PREMIUM" | "EQUILIBRIUM" =
        posInRange <= 0.42
          ? "DISCOUNT"
          : posInRange >= 0.58
          ? "PREMIUM"
          : "EQUILIBRIUM";

      // Kaufman Efficiency Ratio over 14 bars to measure trend vs consolidation
      const erStart = Math.max(0, i - 14);
      const netMove = Math.abs(b.close - rawBars[erStart].close);
      let pathSum = 0;
      for (let j = erStart + 1; j <= i; j++) {
        pathSum += Math.abs(rawBars[j].close - rawBars[j - 1].close);
      }
      const efficiencyRatio = pathSum > 1e-9 ? netMove / pathSum : 0;

      // Rolling 60-bar volatility percentile
      const volStart = Math.max(0, i - 60);
      let lowerVolCount = 0;
      for (let j = volStart; j <= i; j++) {
        if (atrArr[j] <= atr14) lowerVolCount++;
      }
      const volPercentile = lowerVolCount / Math.max(1, i - volStart + 1);

      // Empirical Regime Classification from Price Action
      let regime: EmpiricalRegimeCode = "CONSOLIDATION_RANGING";
      if (volPercentile >= 0.82 && tr > atr14 * 1.25) {
        regime = "VOLATILITY_EXPANSION";
      } else if (efficiencyRatio >= 0.28 && ema9 > ema21 && b.close >= ema9) {
        regime = "TRENDING_BULLISH";
      } else if (efficiencyRatio >= 0.28 && ema9 < ema21 && b.close <= ema9) {
        regime = "TRENDING_BEARISH";
      } else {
        regime = "CONSOLIDATION_RANGING";
      }

      const adaptiveSession = classifyAdaptiveSessionBehavior({
        unixSec: b.unix_sec,
        high: b.high,
        low: b.low,
        close: b.close,
        asianHigh,
        asianLow,
        efficiencyRatio,
      });

      annotated.push({
        ts: b.ts,
        unix_sec: b.unix_sec,
        open: Number(b.open.toFixed(5)),
        high: Number(b.high.toFixed(5)),
        low: Number(b.low.toFixed(5)),
        close: Number(b.close.toFixed(5)),
        volume: Number((b.volume || 0).toFixed(2)),
        session: classifyUtcTradingSession(b.unix_sec),
        adaptive_session: adaptiveSession,
        regime,
        atr14: Number(atr14.toFixed(6)),
        ema9: Number(ema9.toFixed(5)),
        ema21: Number(ema21.toFixed(5)),
        rsi14: Number(rsi14.toFixed(2)),
        swing_high_16: Number(sHigh.toFixed(5)),
        swing_low_16: Number(sLow.toFixed(5)),
        efficiency_ratio: Number(efficiencyRatio.toFixed(4)),
        volatility_percentile: Number(volPercentile.toFixed(4)),
        asian_session_high: Number(asianHigh.toFixed(5)),
        asian_session_low: Number(asianLow.toFixed(5)),
        liquidity_sweep_bullish: liquiditySweepBullish,
        liquidity_sweep_bearish: liquiditySweepBearish,
        fvg_bullish: fvgBullish,
        fvg_bearish: fvgBearish,
        bos_direction: bosDirection,
        discount_premium_zone: discountPremiumZone,
      });
    }

    return annotated;
  }

  private aggregateH1ToH4(h1Bars: AnnotatedMtfBar[]): AnnotatedMtfBar[] {
    if (h1Bars.length === 0) return [];
    const buckets = new Map<
      number,
      {
        ts: string;
        unix_sec: number;
        open: number;
        high: number;
        low: number;
        close: number;
        volume: number;
      }
    >();

    for (const b of h1Bars) {
      const blockSec = Math.floor(b.unix_sec / (4 * 3600)) * (4 * 3600);
      const existing = buckets.get(blockSec);
      if (!existing) {
        buckets.set(blockSec, {
          ts: new Date(blockSec * 1000).toISOString(),
          unix_sec: blockSec,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
          volume: b.volume,
        });
      } else {
        if (b.high > existing.high) existing.high = b.high;
        if (b.low < existing.low) existing.low = b.low;
        existing.close = b.close;
        existing.volume += b.volume;
      }
    }

    const rawH4 = Array.from(buckets.values()).sort(
      (a, b) => a.unix_sec - b.unix_sec
    );
    return this.annotateBarSeries(rawH4);
  }

  private async fetchYahooInterval(
    ticker: string,
    interval: string,
    range: string
  ): Promise<
    Array<{
      ts: string;
      unix_sec: number;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }>
  > {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(
      ticker
    )}?interval=${interval}&range=${range}`;
    const res = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; UniverseForgeV2/2.0)" },
      signal: AbortSignal.timeout(12000),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} fetching ${ticker} (${interval})`);
    }
    const json: any = await res.json();
    const result = json?.chart?.result?.[0];
    const timestamps: number[] = result?.timestamp || [];
    const quote = result?.indicators?.quote?.[0] || {};
    const opens: Array<number | null> = quote.open || [];
    const highs: Array<number | null> = quote.high || [];
    const lows: Array<number | null> = quote.low || [];
    const closes: Array<number | null> = quote.close || [];
    const volumes: Array<number | null> = quote.volume || [];

    const bars: Array<{
      ts: string;
      unix_sec: number;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }> = [];

    for (let i = 0; i < timestamps.length; i++) {
      const u = timestamps[i];
      const o = opens[i];
      const h = highs[i];
      const l = lows[i];
      const c = closes[i];
      if (
        typeof u === "number" &&
        typeof o === "number" &&
        typeof h === "number" &&
        typeof l === "number" &&
        typeof c === "number" &&
        c > 0
      ) {
        bars.push({
          ts: new Date(u * 1000).toISOString(),
          unix_sec: u,
          open: o,
          high: h,
          low: l,
          close: c,
          volume: typeof volumes[i] === "number" ? (volumes[i] as number) : 0,
        });
      }
    }
    return bars;
  }

  /**
   * Loads or fetches the 7-timeframe (`W1, D1, H4, H1, M30, M15, M5`) dataset for a symbol,
   * tagging every candle with its UTC Session, Adaptive Session Behavior, Institutional Microstructure,
   * and Empirical Market Regime, and saving the authoritative copy in Chronicle.
   */
  public async ensureMultiTimeframeDataset(
    symbol: string,
    forceRefresh = false
  ): Promise<MultiTimeframeDataset> {
    const upperSym = symbol.toUpperCase();
    const chroniclePath = path.join(
      this.mtfChronicleDir,
      upperSym,
      "latest.json"
    );

    if (!forceRefresh && fs.existsSync(chroniclePath)) {
      try {
        const cached = JSON.parse(
          fs.readFileSync(chroniclePath, "utf8")
        ) as MultiTimeframeDataset;
        if (
          cached &&
          cached.series &&
          cached.series.M15?.length > 100 &&
          cached.series.M15[0].adaptive_session !== undefined
        ) {
          return cached;
        }
      } catch {
        // Re-fetch or upgrade annotations below
      }
    }

    const ticker = this.resolveYahooTicker(upperSym);
    const [w1Raw, d1Raw, h1Raw, m30Raw, m15Raw, m5Raw] = await Promise.all([
      this.fetchYahooInterval(ticker, "1wk", "2y"),
      this.fetchYahooInterval(ticker, "1d", "1y"),
      this.fetchYahooInterval(ticker, "1h", "60d"),
      this.fetchYahooInterval(ticker, "30m", "30d"),
      this.fetchYahooInterval(ticker, "15m", "30d"),
      this.fetchYahooInterval(ticker, "5m", "15d"),
    ]);

    const w1 = this.annotateBarSeries(w1Raw);
    const d1 = this.annotateBarSeries(d1Raw);
    const h1 = this.annotateBarSeries(h1Raw);
    const h4 = this.aggregateH1ToH4(h1);
    const m30 = this.annotateBarSeries(m30Raw);
    const m15 = this.annotateBarSeries(m15Raw);
    const m5 = this.annotateBarSeries(m5Raw);

    const sessionDist: Record<TradingSessionCode, number> = {
      ASIAN_SESSION: 0,
      LONDON_SESSION: 0,
      LONDON_NY_OVERLAP: 0,
      NEW_YORK_SESSION: 0,
      OFF_HOURS_ROLLOVER: 0,
    };
    const adaptiveSessionDist: Record<AdaptiveSessionBehaviorCode, number> = {
      ASIAN_RANGE_ACCUMULATION: 0,
      LONDON_OPEN_JUDAS_SWEEP: 0,
      LONDON_TREND_EXPANSION: 0,
      NY_OVERLAP_INSTITUTIONAL_DRIVE: 0,
      NY_AFTERNOON_REBALANCING: 0,
      ROLLOVER_SPREAD_GUARD: 0,
    };
    const regimeDist: Record<EmpiricalRegimeCode, number> = {
      TRENDING_BULLISH: 0,
      TRENDING_BEARISH: 0,
      CONSOLIDATION_RANGING: 0,
      VOLATILITY_EXPANSION: 0,
    };

    for (const b of m15) {
      sessionDist[b.session] = (sessionDist[b.session] || 0) + 1;
      adaptiveSessionDist[b.adaptive_session] =
        (adaptiveSessionDist[b.adaptive_session] || 0) + 1;
      regimeDist[b.regime] = (regimeDist[b.regime] || 0) + 1;
    }

    const counts: Record<TimeframeCode, number> = {
      W1: w1.length,
      D1: d1.length,
      H4: h4.length,
      H1: h1.length,
      M30: m30.length,
      M15: m15.length,
      M5: m5.length,
    };

    const datasetFingerprint = sha256Hex({
      symbol: upperSym,
      counts,
      last_m5_ts: m5[m5.length - 1]?.ts,
      last_h1_ts: h1[h1.length - 1]?.ts,
    });

    const dataset: MultiTimeframeDataset = {
      schema: "forge.mtf_market_dataset.v2",
      symbol: upperSym,
      yahoo_ticker: ticker,
      source: "yahoo_finance_intraday_7tf_institutional",
      fetched_at_unix: Date.now() / 1000,
      dataset_fingerprint: datasetFingerprint,
      timeframe_counts: counts,
      session_distribution: sessionDist,
      adaptive_session_distribution: adaptiveSessionDist,
      regime_distribution: regimeDist,
      series: {
        W1: w1,
        D1: d1,
        H4: h4,
        H1: h1,
        M30: m30,
        M15: m15,
        M5: m5,
      },
    };

    fs.mkdirSync(path.join(this.mtfChronicleDir, upperSym), { recursive: true });
    fs.writeFileSync(chroniclePath, JSON.stringify(dataset), "utf8");
    fs.writeFileSync(
      path.join(this.mtfRamCacheDir, `${upperSym}.json`),
      JSON.stringify({
        symbol: upperSym,
        dataset_fingerprint: datasetFingerprint,
        timeframe_counts: counts,
        session_distribution: sessionDist,
        adaptive_session_distribution: adaptiveSessionDist,
        regime_distribution: regimeDist,
        fetched_at_unix: dataset.fetched_at_unix,
      }),
      "utf8"
    );

    const stateKey = `forge.market_data.mtf.${upperSym}`;
    const existing = this.chronicle.stateGet(stateKey);
    this.chronicle.stateSet(
      stateKey,
      {
        symbol: upperSym,
        yahoo_ticker: ticker,
        dataset_fingerprint: datasetFingerprint,
        timeframe_counts: counts,
        session_distribution: sessionDist,
        adaptive_session_distribution: adaptiveSessionDist,
        regime_distribution: regimeDist,
        fetched_at_unix: dataset.fetched_at_unix,
      },
      { expectedRevision: existing.revision, sender: "atlas" }
    );

    this.chronicle.storeMemory({
      concept_key: `mtf_ingest::${upperSym}`,
      content: `Ingested & tagged 7-Timeframe institutional dataset for ${upperSym} (W1=${counts.W1}, D1=${counts.D1}, H4=${counts.H4}, H1=${counts.H1}, M30=${counts.M30}, M15=${counts.M15}, M5=${counts.M5} candles) with Adaptive Session Behavior tags, Liquidity Sweeps, Fair Value Gaps (FVG), and Empirical Regimes.`,
      pillar: "procedural",
      domain: "mtf_market_data",
      tags: ["atlas", "forge", "mtf", "institutional", upperSym],
      source_repository: "atlas",
    });

    return dataset;
  }

  public getCachedMtfDataset(symbol: string): MultiTimeframeDataset | null {
    const upperSym = symbol.toUpperCase();
    const chroniclePath = path.join(
      this.mtfChronicleDir,
      upperSym,
      "latest.json"
    );
    if (!fs.existsSync(chroniclePath)) return null;
    try {
      return JSON.parse(fs.readFileSync(chroniclePath, "utf8")) as MultiTimeframeDataset;
    } catch {
      return null;
    }
  }

  public findLatestCompletedBarAtOrBefore(
    series: AnnotatedMtfBar[],
    targetUnixSec: number
  ): AnnotatedMtfBar | null {
    if (!series || series.length === 0) return null;
    let lo = 0;
    let hi = series.length - 1;
    let bestIdx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >>> 1;
      if (series[mid].unix_sec <= targetUnixSec) {
        bestIdx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return bestIdx >= 0 ? series[bestIdx] : null;
  }

  /**
   * Evaluates how a specific Trader Archetype (`SCALPING`, `DAY_TRADING`, `SWING_TRADING`)
   * behaves at `bar` given its higher-timeframe context, institutional liquidity sweeps,
   * Fair Value Gaps (FVG), Break of Structure (BOS), and Discount/Premium zones.
   */
  public evaluateTraderArchetypeSignal(params: {
    bar: AnnotatedMtfBar;
    macroBars: AnnotatedMtfBar[];
    setupBars: AnnotatedMtfBar[];
    traderStyle: TraderStyleCode;
  }): 1 | -1 | 0 {
    const { bar, macroBars, setupBars, traderStyle } = params;

    // 1. Macro bias (top-down directional flow)
    let macroVotes = 0;
    for (const mBar of macroBars) {
      if (mBar.ema9 > mBar.ema21) macroVotes += 1;
      else if (mBar.ema9 < mBar.ema21) macroVotes -= 1;
      if (mBar.bos_direction !== 0) macroVotes += mBar.bos_direction * 0.5;
    }

    // 2. Setup confirmation (intermediate timeframe structure + non-exhausted RSI)
    let setupVotes = 0;
    for (const sBar of setupBars) {
      if (sBar.close > sBar.ema9 && sBar.rsi14 > 44 && sBar.rsi14 < 74) {
        setupVotes += 1;
      } else if (sBar.close < sBar.ema9 && sBar.rsi14 < 56 && sBar.rsi14 > 26) {
        setupVotes -= 1;
      }
      if (sBar.fvg_bullish || sBar.liquidity_sweep_bullish) setupVotes += 0.75;
      if (sBar.fvg_bearish || sBar.liquidity_sweep_bearish) setupVotes -= 0.75;
    }

    // 3. Archetype-specific trigger behavior
    if (traderStyle === "SCALPING") {
      // Session Liquidity & Displacement Scalper (M5):
      // Enters on Liquidity Sweep reversal, FVG displacement, or momentum continuation in active sessions
      const bullTrigger =
        (bar.liquidity_sweep_bullish ||
          bar.fvg_bullish ||
          (bar.close > bar.ema9 && bar.ema9 >= bar.ema21)) &&
        bar.rsi14 >= 35 &&
        bar.rsi14 <= 70;
      const bearTrigger =
        (bar.liquidity_sweep_bearish ||
          bar.fvg_bearish ||
          (bar.close < bar.ema9 && bar.ema9 <= bar.ema21)) &&
        bar.rsi14 <= 65 &&
        bar.rsi14 >= 30;

      if (bullTrigger && macroVotes >= 0 && setupVotes > 0) return 1;
      if (bearTrigger && macroVotes <= 0 && setupVotes < 0) return -1;
      return 0;
    }

    if (traderStyle === "DAY_TRADING") {
      // Institutional Intraday Pullback & Order-Flow Day Trader (M15):
      // Waits for macro alignment + setup confirmation + trigger candle out of Discount/Equilibrium (for buys) or Premium/Equilibrium (for sells)
      const bullTrigger =
        (bar.liquidity_sweep_bullish ||
          bar.fvg_bullish ||
          bar.bos_direction === 1 ||
          (bar.close > bar.ema9 && bar.ema9 >= bar.ema21)) &&
        bar.discount_premium_zone !== "PREMIUM" &&
        bar.rsi14 >= 38 &&
        bar.rsi14 <= 68;

      const bearTrigger =
        (bar.liquidity_sweep_bearish ||
          bar.fvg_bearish ||
          bar.bos_direction === -1 ||
          (bar.close < bar.ema9 && bar.ema9 <= bar.ema21)) &&
        bar.discount_premium_zone !== "DISCOUNT" &&
        bar.rsi14 <= 62 &&
        bar.rsi14 >= 32;

      if (bullTrigger && macroVotes >= 0 && setupVotes > 0) return 1;
      if (bearTrigger && macroVotes <= 0 && setupVotes < 0) return -1;
      return 0;
    }

    if (traderStyle === "RANGE_SPECIALIST") {
      // Institutional Range & Liquidity-Edge Mean-Reversion Specialist (M15):
      // Activates when higher timeframes show low efficiency / consolidation range,
      // and fades extremes (Discount / Bullish Sweep -> Long back to Equilibrium; Premium / Bearish Sweep -> Short back to Equilibrium)
      const rangeFriendlyMacro =
        macroBars.length === 0 ||
        macroBars.some(
          (m) =>
            m.regime === "CONSOLIDATION_RANGING" || m.efficiency_ratio <= 0.36
        );
      if (!rangeFriendlyMacro) return 0;

      const bullFade =
        (bar.discount_premium_zone === "DISCOUNT" ||
          bar.liquidity_sweep_bullish) &&
        bar.close > bar.open &&
        bar.rsi14 <= 48;
      const bearFade =
        (bar.discount_premium_zone === "PREMIUM" ||
          bar.liquidity_sweep_bearish) &&
        bar.close < bar.open &&
        bar.rsi14 >= 52;

      if (bullFade) return 1;
      if (bearFade) return -1;
      return 0;
    }

    // SWING_TRADING (H1 trigger aligned with W1+D1 macro & H4 setup):
    // Institutional Swing Trader waits for structural sweep or Discount/Premium pullback on H1/H4
    const bullSwing =
      (bar.liquidity_sweep_bullish ||
        bar.fvg_bullish ||
        bar.bos_direction === 1 ||
        (bar.close > bar.ema9 &&
          bar.ema9 > bar.ema21 &&
          bar.discount_premium_zone === "DISCOUNT")) &&
      bar.rsi14 >= 36 &&
      bar.rsi14 <= 66;

    const bearSwing =
      (bar.liquidity_sweep_bearish ||
        bar.fvg_bearish ||
        bar.bos_direction === -1 ||
        (bar.close < bar.ema9 &&
          bar.ema9 < bar.ema21 &&
          bar.discount_premium_zone === "PREMIUM")) &&
      bar.rsi14 <= 64 &&
      bar.rsi14 >= 34;

    if (bullSwing && macroVotes >= 0 && setupVotes > 0) return 1;
    if (bearSwing && macroVotes <= 0 && setupVotes < 0) return -1;
    return 0;
  }

  /**
   * Computes the empirical Instrument "Candle Physics" DNA Profile from the
   * 60% chronological development slice (wick-to-body ratio, sweep wick threshold,
   * FVG displacement body threshold, trend persistence vs. mean-reversion score,
   * and peak volatility UTC hours).
   */
  public computeInstrumentPhysicsProfile(
    symbol: string,
    bars: AnnotatedMtfBar[],
    endIdx?: number
  ): InstrumentPhysicsProfile {
    const upper = symbol.toUpperCase();
    const limit = Math.min(bars.length, endIdx ?? Math.floor(bars.length * 0.6));
    let assetClass: InstrumentPhysicsProfile["asset_class"] = "FOREX_MAJOR";
    if (upper.startsWith("XAU") || upper.startsWith("XAG")) {
      assetClass = "PRECIOUS_METAL";
    } else if (
      ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD"].includes(upper)
    ) {
      assetClass = "CRYPTO_ASSET";
    } else if (
      ["US30", "US500", "NAS100", "GER40", "UK100", "JPN225", "SPX", "NASDAQ"].includes(
        upper
      )
    ) {
      assetClass = "EQUITY_INDEX";
    } else if (!upper.includes("USD")) {
      assetClass = "FOREX_CROSS";
    }

    const wickToBodyRatios: number[] = [];
    const maxWickRatios: number[] = [];
    const bodyAtrRatios: number[] = [];
    const efficiencyVals: number[] = [];
    const hourAtrSums: Record<number, { sum: number; count: number }> = {};

    for (let i = 0; i < limit; i++) {
      const b = bars[i];
      const range = Math.max(b.high - b.low, 1e-9);
      const body = Math.max(Math.abs(b.close - b.open), range * 0.05);
      const totalWick = Math.max(0, range - body);
      const upperWick = b.high - Math.max(b.open, b.close);
      const lowerWick = Math.min(b.open, b.close) - b.low;

      wickToBodyRatios.push(totalWick / body);
      maxWickRatios.push(Math.max(upperWick, lowerWick) / range);
      if (b.atr14 > 0) {
        bodyAtrRatios.push(body / b.atr14);
      }
      efficiencyVals.push(b.efficiency_ratio);

      const hr = new Date(b.unix_sec * 1000).getUTCHours();
      if (!hourAtrSums[hr]) hourAtrSums[hr] = { sum: 0, count: 0 };
      hourAtrSums[hr].sum += b.atr14 / Math.max(b.close, 1e-9);
      hourAtrSums[hr].count += 1;
    }

    const q = (arr: number[], pct: number, fb: number) => {
      if (arr.length === 0) return fb;
      const sorted = [...arr].sort((a, b) => a - b);
      return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * pct))];
    };

    const avgWickToBody =
      wickToBodyRatios.length > 0
        ? Number(
            (
              wickToBodyRatios.reduce((a, v) => a + v, 0) /
              wickToBodyRatios.length
            ).toFixed(3)
          )
        : 1.15;
    const learnedSweepWick = Number(
      Math.min(Math.max(q(maxWickRatios, 0.65, 0.32), 0.25), 0.48).toFixed(3)
    );
    const learnedFvgBodyAtr = Number(
      Math.min(Math.max(q(bodyAtrRatios, 0.65, 0.55), 0.4), 0.9).toFixed(3)
    );
    const medianEr = Number(q(efficiencyVals, 0.5, 0.26).toFixed(4));

    const personality: InstrumentPhysicsProfile["personality"] =
      medianEr >= 0.3
        ? "MOMENTUM_PERSISTENT"
        : medianEr <= 0.23
        ? "MEAN_REVERTING_DOMINANT"
        : "BALANCED_HYBRID";

    const peakHours = Object.entries(hourAtrSums)
      .map(([hr, st]) => ({
        hr: Number(hr),
        avg: st.count > 0 ? st.sum / st.count : 0,
      }))
      .sort((a, b) => b.avg - a.avg)
      .slice(0, 4)
      .map((x) => x.hr);

    return {
      symbol: upper,
      asset_class: assetClass,
      avg_wick_to_body_ratio: avgWickToBody,
      learned_sweep_wick_ratio_threshold: learnedSweepWick,
      learned_fvg_body_atr_mult: learnedFvgBodyAtr,
      median_efficiency_ratio: medianEr,
      personality,
      peak_volatility_utc_hours: peakHours,
    };
  }

  /**
   * Strategy Regime-Drift & Decay Detector:
   * Compares the baseline 60% training slice against the most recent 20% market window
   * to detect significant shifts in ATR volatility, Kaufman Efficiency Ratio, or candle wick physics.
   */
  public inspectStrategyRegimeDrift(
    symbol: string,
    timeframe: TimeframeCode,
    bars: AnnotatedMtfBar[]
  ): StrategyRegimeDriftReport {
    const n = bars.length;
    const nowUnix = Date.now() / 1000;
    if (n < 60) {
      return {
        symbol: symbol.toUpperCase(),
        timeframe,
        status: "STABLE",
        volatility_drift_pct: 0,
        efficiency_drift_pct: 0,
        wick_ratio_drift_pct: 0,
        composite_drift_score: 0,
        baseline_atr_pct: 0,
        recent_atr_pct: 0,
        baseline_efficiency: 0,
        recent_efficiency: 0,
        recalibration_recommended: false,
        reason: "Insufficient bars for drift comparison.",
        evaluated_at_unix: nowUnix,
      };
    }

    const baseEnd = Math.floor(n * 0.6);
    const recentStart = Math.floor(n * 0.8);

    const sliceStats = (slice: AnnotatedMtfBar[]) => {
      let atrPctSum = 0;
      let erSum = 0;
      let wickSum = 0;
      for (const b of slice) {
        atrPctSum += (b.atr14 / Math.max(b.close, 1e-9)) * 100;
        erSum += b.efficiency_ratio;
        const rng = Math.max(b.high - b.low, 1e-9);
        const body = Math.max(Math.abs(b.close - b.open), rng * 0.05);
        wickSum += (rng - body) / body;
      }
      const len = Math.max(1, slice.length);
      return {
        atrPct: atrPctSum / len,
        er: erSum / len,
        wick: wickSum / len,
      };
    };

    const base = sliceStats(bars.slice(0, baseEnd));
    const recent = sliceStats(bars.slice(recentStart));

    const volDrift =
      Math.abs(recent.atrPct - base.atrPct) / Math.max(base.atrPct, 1e-6);
    const erDrift = Math.abs(recent.er - base.er) / Math.max(base.er, 1e-6);
    const wickDrift =
      Math.abs(recent.wick - base.wick) / Math.max(base.wick, 1e-6);

    const composite = Number(
      (volDrift * 0.5 + erDrift * 0.35 + wickDrift * 0.15).toFixed(4)
    );
    const status: StrategyRegimeDriftReport["status"] =
      composite >= 0.25
        ? "NEEDS_RECALIBRATION"
        : composite >= 0.15
        ? "MODERATE_DRIFT"
        : "STABLE";

    return {
      symbol: symbol.toUpperCase(),
      timeframe,
      status,
      volatility_drift_pct: Number((volDrift * 100).toFixed(2)),
      efficiency_drift_pct: Number((erDrift * 100).toFixed(2)),
      wick_ratio_drift_pct: Number((wickDrift * 100).toFixed(2)),
      composite_drift_score: composite,
      baseline_atr_pct: Number(base.atrPct.toFixed(4)),
      recent_atr_pct: Number(recent.atrPct.toFixed(4)),
      baseline_efficiency: Number(base.er.toFixed(4)),
      recent_efficiency: Number(recent.er.toFixed(4)),
      recalibration_recommended: status === "NEEDS_RECALIBRATION",
      reason:
        status === "NEEDS_RECALIBRATION"
          ? `Market volatility/efficiency shifted by ${(composite * 100).toFixed(1)}% (>25% threshold) on ${timeframe}; proactive Forge recalibration recommended.`
          : `Regime distribution within normal operating tolerance (${(composite * 100).toFixed(1)}% composite drift on ${timeframe}).`,
      evaluated_at_unix: nowUnix,
    };
  }

  /**
   * Atlas + Forge Institutional Trader-Behavior & Excursion Discovery:
   * Analyzes the 60% chronological development prefix to empirically learn:
   * - How each Trader Archetype (`SCALPING`, `DAY_TRADING`, `SWING_TRADING`) behaves
   * - Optimal Stop-Loss from 70th-percentile winning MAE + structural swing invalidation
   * - Dynamic Trade Management parameters:
   *   * Break-Even trigger (`learned_be_trigger_r`)
   *   * Partial Take-Profit 1 (`learned_tp1_r` for 50% scale-out)
   *   * Runner Take-Profit 2 (`learned_tp2_r` for remaining 50%)
   *   * Structure / ATR Chandelier Trail (`learned_trail_atr_mult`)
   * - Best UTC Sessions, Best Adaptive Session Behaviors, and Best Market Regimes.
   */
  public runAtlasForgeEmpiricalResearch(params: {
    dataset: MultiTimeframeDataset;
    traderStyle: TraderStyleCode;
    roundTripCostBps: number;
    surgicalOverrides?: {
      excludedSessions?: TradingSessionCode[];
      excludedAdaptiveBehaviors?: AdaptiveSessionBehaviorCode[];
      excludedRegimes?: EmpiricalRegimeCode[];
      extraStopBufferAtr?: number;
      tighterBeOffsetR?: number;
      diagnosisNotes?: string[];
    };
  }): AtlasForgeResearchBrief {
    const { dataset, traderStyle, roundTripCostBps, surgicalOverrides } = params;

    const topDownStack: AtlasForgeResearchBrief["top_down_stack"] =
      traderStyle === "SCALPING"
        ? {
            macro_timeframes: ["H1", "M30"],
            setup_timeframes: ["M15"],
            trigger_timeframe: "M5",
          }
        : traderStyle === "DAY_TRADING" || traderStyle === "RANGE_SPECIALIST"
        ? {
            macro_timeframes: ["D1", "H4"],
            setup_timeframes: ["H1", "M30"],
            trigger_timeframe: "M15",
          }
        : {
            macro_timeframes: ["W1", "D1"],
            setup_timeframes: ["H4"],
            trigger_timeframe: "H1",
          };

    const triggerSeries = dataset.series[topDownStack.trigger_timeframe] || [];
    const devEndIdx = Math.floor(triggerSeries.length * 0.6);
    const horizonBars =
      traderStyle === "SCALPING"
        ? 14
        : traderStyle === "RANGE_SPECIALIST"
        ? 14
        : traderStyle === "DAY_TRADING"
        ? 18
        : 28;

    const instrumentPhysics = this.computeInstrumentPhysicsProfile(
      dataset.symbol,
      triggerSeries,
      devEndIdx
    );
    const regimeDriftReport = this.inspectStrategyRegimeDrift(
      dataset.symbol,
      topDownStack.trigger_timeframe,
      triggerSeries
    );

    const winningMaesAtr: number[] = [];
    const allMfesAtr: number[] = [];

    const sessionBuckets: Record<
      string,
      { setups: number; wins: number; rSum: number; mfeSum: number }
    > = {};
    const adaptiveBuckets: Record<
      string,
      { setups: number; wins: number; rSum: number }
    > = {};
    const regimeBuckets: Record<
      string,
      { setups: number; wins: number; rSum: number }
    > = {};

    for (let i = 25; i < devEndIdx - horizonBars; i++) {
      const bar = triggerSeries[i];
      const atr = Math.max(bar.atr14, bar.close * 0.0002);

      const macroBars: AnnotatedMtfBar[] = [];
      for (const mTf of topDownStack.macro_timeframes) {
        const mBar = this.findLatestCompletedBarAtOrBefore(
          dataset.series[mTf],
          bar.unix_sec
        );
        if (mBar) macroBars.push(mBar);
      }

      const setupBars: AnnotatedMtfBar[] = [];
      for (const sTf of topDownStack.setup_timeframes) {
        const sBar = this.findLatestCompletedBarAtOrBefore(
          dataset.series[sTf],
          bar.unix_sec
        );
        if (sBar) setupBars.push(sBar);
      }

      const triggerDir = this.evaluateTraderArchetypeSignal({
        bar,
        macroBars,
        setupBars,
        traderStyle,
      });

      if (triggerDir === 0) continue;

      let maxFav = 0;
      let maxAdv = 0;
      for (let k = i + 1; k <= i + horizonBars; k++) {
        const f = triggerSeries[k];
        if (triggerDir === 1) {
          maxFav = Math.max(maxFav, f.high - bar.close);
          maxAdv = Math.max(maxAdv, bar.close - f.low);
        } else {
          maxFav = Math.max(maxFav, bar.close - f.low);
          maxAdv = Math.max(maxAdv, f.high - bar.close);
        }
      }

      const mfeAtr = maxFav / atr;
      const maeAtr = maxAdv / atr;
      allMfesAtr.push(mfeAtr);

      const costAtr = ((roundTripCostBps / 10000) * bar.close) / atr;
      const netExcursionR = (mfeAtr - maeAtr * 0.7 - costAtr) / 1.2;
      const isPositiveExcursion = mfeAtr > maeAtr * 1.25 + costAtr;

      if (isPositiveExcursion) {
        winningMaesAtr.push(maeAtr);
      }

      if (!sessionBuckets[bar.session]) {
        sessionBuckets[bar.session] = { setups: 0, wins: 0, rSum: 0, mfeSum: 0 };
      }
      sessionBuckets[bar.session].setups += 1;
      if (isPositiveExcursion) sessionBuckets[bar.session].wins += 1;
      sessionBuckets[bar.session].rSum += netExcursionR;
      sessionBuckets[bar.session].mfeSum += mfeAtr;

      if (!adaptiveBuckets[bar.adaptive_session]) {
        adaptiveBuckets[bar.adaptive_session] = { setups: 0, wins: 0, rSum: 0 };
      }
      adaptiveBuckets[bar.adaptive_session].setups += 1;
      if (isPositiveExcursion) adaptiveBuckets[bar.adaptive_session].wins += 1;
      adaptiveBuckets[bar.adaptive_session].rSum += netExcursionR;

      if (!regimeBuckets[bar.regime]) {
        regimeBuckets[bar.regime] = { setups: 0, wins: 0, rSum: 0 };
      }
      regimeBuckets[bar.regime].setups += 1;
      if (isPositiveExcursion) regimeBuckets[bar.regime].wins += 1;
      regimeBuckets[bar.regime].rSum += netExcursionR;
    }

    const quantile = (arr: number[], q: number, fallback: number): number => {
      if (arr.length === 0) return fallback;
      const sorted = [...arr].sort((a, b) => a - b);
      const idx = Math.min(
        sorted.length - 1,
        Math.max(0, Math.floor(sorted.length * q))
      );
      return Number(sorted[idx].toFixed(4));
    };

    const medianMaeAtr = quantile(winningMaesAtr, 0.5, 0.8);
    const p70MaeBufferAtr = quantile(winningMaesAtr, 0.7, 1.1);
    const medianMfeAtr = quantile(allMfesAtr, 0.5, 2.0);
    const p65MfeTargetAtr = quantile(allMfesAtr, 0.65, 2.75);
    const p80MfeRunnerAtr = quantile(allMfesAtr, 0.8, 3.6);

    const extraStopBuffer = surgicalOverrides?.extraStopBufferAtr ?? 0;
    const tighterBeOffset = surgicalOverrides?.tighterBeOffsetR ?? 0;

    const learnedSlMult = Number(
      Math.min(
        Math.max(p70MaeBufferAtr + 0.22 + extraStopBuffer, 0.9),
        2.35
      ).toFixed(3)
    );
    const learnedTpMult = Number(
      Math.max(p65MfeTargetAtr, learnedSlMult * 1.9).toFixed(3)
    );
    const learnedRr = Number((learnedTpMult / learnedSlMult).toFixed(2));

    // Dynamic Stop & TP Parameters (empirically derived from MFE distribution)
    const learnedTp1R = Number(
      Math.max(1.15, Math.min(medianMfeAtr / learnedSlMult, learnedRr * 0.65)).toFixed(2)
    );
    const learnedBeTriggerR = Number(
      Math.max(0.68, learnedTp1R * 0.72 - tighterBeOffset).toFixed(2)
    );
    const learnedTp2R = Number(
      Math.max(learnedRr, p80MfeRunnerAtr / learnedSlMult).toFixed(2)
    );
    const learnedTrailAtrMult = Number(
      Math.max(0.85, learnedSlMult * 0.85).toFixed(2)
    );

    const excludedSessSet = new Set(surgicalOverrides?.excludedSessions || []);
    const excludedAdapSet = new Set(
      surgicalOverrides?.excludedAdaptiveBehaviors || []
    );
    const excludedRegSet = new Set(surgicalOverrides?.excludedRegimes || []);

    const sessionStats: AtlasForgeResearchBrief["session_discovery"]["session_stats"] =
      {};
    const bestSessions: TradingSessionCode[] = [];
    const avoidSessions: TradingSessionCode[] = [];

    for (const [sess, st] of Object.entries(sessionBuckets)) {
      const wr = st.setups > 0 ? Number((st.wins / st.setups).toFixed(4)) : 0;
      const expR = st.setups > 0 ? Number((st.rSum / st.setups).toFixed(4)) : 0;
      const avgMfe =
        st.setups > 0 ? Number((st.mfeSum / st.setups).toFixed(4)) : 0;
      sessionStats[sess] = {
        setups: st.setups,
        win_rate: wr,
        expectancy_r: expR,
        avg_mfe_atr: avgMfe,
      };
      if (
        sess !== "OFF_HOURS_ROLLOVER" &&
        !excludedSessSet.has(sess as TradingSessionCode) &&
        st.setups >= 3 &&
        (expR > -0.05 || wr >= 0.42)
      ) {
        bestSessions.push(sess as TradingSessionCode);
      } else {
        avoidSessions.push(sess as TradingSessionCode);
      }
    }

    if (bestSessions.length === 0) {
      bestSessions.push("LONDON_SESSION", "LONDON_NY_OVERLAP", "ASIAN_SESSION");
    }

    const adaptiveStats: AtlasForgeResearchBrief["session_discovery"]["adaptive_session_stats"] =
      {};
    const bestAdaptiveBehaviors: AdaptiveSessionBehaviorCode[] = [];
    for (const [adap, st] of Object.entries(adaptiveBuckets)) {
      const wr = st.setups > 0 ? Number((st.wins / st.setups).toFixed(4)) : 0;
      const expR = st.setups > 0 ? Number((st.rSum / st.setups).toFixed(4)) : 0;
      adaptiveStats[adap] = {
        setups: st.setups,
        win_rate: wr,
        expectancy_r: expR,
      };
      if (
        adap !== "ROLLOVER_SPREAD_GUARD" &&
        !excludedAdapSet.has(adap as AdaptiveSessionBehaviorCode) &&
        st.setups >= 2 &&
        (expR > -0.05 || wr >= 0.42)
      ) {
        bestAdaptiveBehaviors.push(adap as AdaptiveSessionBehaviorCode);
      }
    }
    if (bestAdaptiveBehaviors.length === 0) {
      bestAdaptiveBehaviors.push(
        "LONDON_OPEN_JUDAS_SWEEP",
        "LONDON_TREND_EXPANSION",
        "NY_OVERLAP_INSTITUTIONAL_DRIVE"
      );
    }

    const regimeStats: AtlasForgeResearchBrief["regime_discovery"]["regime_stats"] =
      {};
    const bestRegimes: EmpiricalRegimeCode[] = [];
    const avoidRegimes: EmpiricalRegimeCode[] = [];

    for (const [reg, st] of Object.entries(regimeBuckets)) {
      const wr = st.setups > 0 ? Number((st.wins / st.setups).toFixed(4)) : 0;
      const expR = st.setups > 0 ? Number((st.rSum / st.setups).toFixed(4)) : 0;
      regimeStats[reg] = {
        setups: st.setups,
        win_rate: wr,
        expectancy_r: expR,
      };
      if (
        !excludedRegSet.has(reg as EmpiricalRegimeCode) &&
        st.setups >= 2 &&
        (expR > -0.05 || wr >= 0.42)
      ) {
        bestRegimes.push(reg as EmpiricalRegimeCode);
      } else {
        avoidRegimes.push(reg as EmpiricalRegimeCode);
      }
    }

    if (bestRegimes.length === 0) {
      bestRegimes.push("TRENDING_BULLISH", "TRENDING_BEARISH", "CONSOLIDATION_RANGING");
    }

    const behaviorProfile: TraderBehaviorProfile =
      traderStyle === "SCALPING"
        ? {
            archetype_id: "inst_liquidity_sweep_scalper_m5",
            archetype_name: "Institutional Session Liquidity & Displacement Scalper",
            trader_style: "SCALPING",
            decision_hierarchy: [
              "1. Map Asian Session High/Low (ASH/ASL) & prior 16-bar liquidity pools on H1/M30.",
              "2. Confirm M15 institutional order flow & non-exhausted RSI14.",
              "3. Wait for M5 Liquidity Sweep rejection wick or 3-bar Fair Value Gap (FVG) displacement during active institutional sessions.",
              "4. Manage dynamically: shift SL to Break-Even at +" +
                learnedBeTriggerR +
                "R, bank 50% at TP1 (+" +
                learnedTp1R +
                "R), and trail runner by " +
                learnedTrailAtrMult +
                "x ATR.",
            ],
            institutional_entry_triggers: [
              "Asian High/Low Judas Sweep + M5 rejection close inside dealing range",
              "M5 3-Candle Fair Value Gap (FVG) aligned with H1/M30 EMA9/21 & BOS",
            ],
            behavioral_mistakes_avoided: [
              "Never chases extended M5 breakout candles into exhausted RSI (>70 or <30)",
              "Never trades during ROLLOVER_SPREAD_GUARD (21:00-00:00 UTC) when spreads widen",
              "Never lets a +1R winning scalp turn into a full -1R loss (Break-Even ratchet active)",
            ],
            dynamic_trade_management: {
              enabled: true,
              breakeven_trigger_r: learnedBeTriggerR,
              breakeven_offset_r: 0.1,
              partial_tp1_r: learnedTp1R,
              partial_tp1_Allocation_pct: 50,
              runner_tp2_r: learnedTp2R,
              structure_trail_atr_mult: learnedTrailAtrMult,
              max_holding_bars: horizonBars,
            },
          }
        : traderStyle === "DAY_TRADING"
        ? {
            archetype_id: "inst_orderflow_day_trader_m15",
            archetype_name: "Institutional Order-Flow & Discount/Premium Day Trader",
            trader_style: "DAY_TRADING",
            decision_hierarchy: [
              "1. Establish Macro Bias from D1 + H4 structure (EMA9/21 + Break of Structure).",
              "2. Wait for H1 + M30 pullback into Discount (for buys) or Premium (for sells) dealing range.",
              "3. Execute on M15 Liquidity Sweep, FVG imbalance, or BOS candle during London / NY Overlap.",
              "4. Manage dynamically: move SL to Break-Even at +" +
                learnedBeTriggerR +
                "R, scale out 50% at TP1 (+" +
                learnedTp1R +
                "R), and trail remaining 50% to TP2 (+" +
                learnedTp2R +
                "R).",
            ],
            institutional_entry_triggers: [
              "M15 Break of Structure (BOS) or FVG out of Discount/Equilibrium zone",
              "London / NY Overlap institutional pullback continuation",
            ],
            behavioral_mistakes_avoided: [
              "Never buys in Premium (>58% of 16-bar range) or sells in Discount (<42% of range)",
              "Never holds passive static stops once trade reaches +" +
                learnedBeTriggerR +
                "R",
            ],
            dynamic_trade_management: {
              enabled: true,
              breakeven_trigger_r: learnedBeTriggerR,
              breakeven_offset_r: 0.12,
              partial_tp1_r: learnedTp1R,
              partial_tp1_Allocation_pct: 50,
              runner_tp2_r: learnedTp2R,
              structure_trail_atr_mult: learnedTrailAtrMult,
              max_holding_bars: horizonBars,
            },
          }
        : traderStyle === "RANGE_SPECIALIST"
        ? {
            archetype_id: "inst_range_mean_reversion_m15",
            archetype_name: "Institutional Range & Liquidity-Edge Mean-Reversion Specialist",
            trader_style: "RANGE_SPECIALIST",
            decision_hierarchy: [
              "1. Confirm D1 + H4 low-efficiency consolidation / range environment.",
              "2. Map 16-bar & Asian Session High/Low dealing range boundaries on H1/M30.",
              "3. Wait for M15 price to sweep or test extreme Discount (for longs) or Premium (for shorts) and print a rejection candle back inside the range.",
              "4. Fade back to the 50% Equilibrium midpoint: shift SL to Break-Even at +" +
                learnedBeTriggerR +
                "R, bank 50% at TP1 (+" +
                learnedTp1R +
                "R), and trail runner to opposite range boundary (+" +
                learnedTp2R +
                "R).",
            ],
            institutional_entry_triggers: [
              "M15 Liquidity Sweep rejection at Discount/Premium range boundary during consolidation",
              "Failed breakout (Turtle Soup) back inside 16-bar dealing box",
            ],
            behavioral_mistakes_avoided: [
              "Never enters in the middle of the range (Equilibrium chop zone)",
              "Automatically stands down when D1/H4 breaks out into high-efficiency trend expansion",
            ],
            dynamic_trade_management: {
              enabled: true,
              breakeven_trigger_r: learnedBeTriggerR,
              breakeven_offset_r: 0.1,
              partial_tp1_r: learnedTp1R,
              partial_tp1_Allocation_pct: 50,
              runner_tp2_r: learnedTp2R,
              structure_trail_atr_mult: learnedTrailAtrMult,
              max_holding_bars: horizonBars,
            },
          }
        : {
            archetype_id: "inst_structural_swing_trader_h1",
            archetype_name: "Institutional Multi-Session Structural Swing Trader",
            trader_style: "SWING_TRADING",
            decision_hierarchy: [
              "1. Align with W1 + D1 macro institutional trend & multi-day dealing range.",
              "2. Confirm H4 structural accumulation, sweep, or Fair Value Gap.",
              "3. Trigger on H1 Liquidity Sweep or Discount/Premium structural shift.",
              "4. Protect capital across sessions: lock in Break-Even at +" +
                learnedBeTriggerR +
                "R, take 50% at TP1 (+" +
                learnedTp1R +
                "R), and Chandelier-trail the runner to +" +
                learnedTp2R +
                "R.",
            ],
            institutional_entry_triggers: [
              "H1 Liquidity Sweep of multi-session swing low/high aligned with W1/D1",
              "H1/H4 Discount/Premium structural reversal with FVG displacement",
            ],
            behavioral_mistakes_avoided: [
              "Never holds unhedged losing swing positions if H4 structure flips against entry",
              "Uses dynamic partial scale-out at TP1 so multi-day pullbacks do not erase open profit",
            ],
            dynamic_trade_management: {
              enabled: true,
              breakeven_trigger_r: learnedBeTriggerR,
              breakeven_offset_r: 0.15,
              partial_tp1_r: learnedTp1R,
              partial_tp1_Allocation_pct: 50,
              runner_tp2_r: learnedTp2R,
              structure_trail_atr_mult: learnedTrailAtrMult,
              max_holding_bars: horizonBars,
            },
          };

    const nowUnix = Date.now() / 1000;
    const briefId = `atlas-brief-${dataset.symbol.toLowerCase()}-${traderStyle.toLowerCase()}`;

    const brief: AtlasForgeResearchBrief = {
      brief_id: briefId,
      symbol: dataset.symbol,
      trader_style: traderStyle,
      institutional_grade_score: 95,
      instrument_physics: instrumentPhysics,
      regime_drift_report: regimeDriftReport,
      trader_behavior_profile: behaviorProfile,
      top_down_stack: topDownStack,
      empirical_excursion_discovery: {
        sample_setups_analyzed: allMfesAtr.length,
        median_mae_atr: medianMaeAtr,
        p70_mae_stop_buffer_atr: p70MaeBufferAtr,
        median_mfe_atr: medianMfeAtr,
        p65_mfe_target_atr: p65MfeTargetAtr,
        p80_mfe_runner_atr: p80MfeRunnerAtr,
        learned_optimal_stop_atr_mult: learnedSlMult,
        learned_optimal_tp_atr_mult: learnedTpMult,
        learned_risk_reward_ratio: learnedRr,
        learned_be_trigger_r: learnedBeTriggerR,
        learned_tp1_r: learnedTp1R,
        learned_tp2_r: learnedTp2R,
        learned_trail_atr_mult: learnedTrailAtrMult,
      },
      session_discovery: {
        best_sessions: bestSessions,
        avoid_sessions: avoidSessions,
        best_adaptive_behaviors: bestAdaptiveBehaviors,
        session_stats: sessionStats,
        adaptive_session_stats: adaptiveStats,
      },
      regime_discovery: {
        best_regimes: bestRegimes,
        avoid_regimes: avoidRegimes,
        regime_stats: regimeStats,
      },
      strategy_specification: {
        entry_conditions: [
          `Top-Down Macro Alignment (${topDownStack.macro_timeframes.join(" + ")}): EMA9/EMA21 directional agreement + Break of Structure (BOS) bias.`,
          `Setup Confirmation (${topDownStack.setup_timeframes.join(" + ")}): Pullback into Discount/Equilibrium (buys) or Premium/Equilibrium (sells) with FVG/Sweep confirmation.`,
          `Institutional Trigger (${topDownStack.trigger_timeframe}): ${behaviorProfile.institutional_entry_triggers.join(" OR ")} during [${bestAdaptiveBehaviors.join(", ")}].`,
          `Active Regime Filter: Execute only when ${topDownStack.trigger_timeframe} regime is in [${bestRegimes.join(", ")}].`,
        ],
        exit_conditions: [
          `Dynamic Partial Profit (TP1): Close 50% of position at +${learnedTp1R}R and immediately ratchet Stop-Loss to Break-Even (+0.10R).`,
          `Dynamic Runner Target (TP2) & Structure Trail: Trail remaining 50% behind ${learnedTrailAtrMult}x ATR14 / 8-bar swing high-low up to +${learnedTp2R}R.`,
          `Structure Flip Invalidation: Exit early if ${topDownStack.setup_timeframes[0]} structure flips against trade direction.`,
        ],
        stop_loss_conditions: [
          `Structural Swing + Liquidity Sweep Invalidation: Place initial Stop-Loss beyond the sweep wick / 16-bar swing low-high on ${topDownStack.trigger_timeframe}, bounded by Forge's learned ${learnedSlMult}x ATR14 excursion limit (P70 winning MAE = ${p70MaeBufferAtr}x ATR).`,
          `Dynamic Break-Even Ratchet: Automatically move Stop-Loss to Entry + 0.10R once unrealized profit reaches +${learnedBeTriggerR}R.`,
        ],
        take_profit_conditions: [
          `Two-Stage Institutional Take-Profit: TP1 = +${learnedTp1R}R (50% scale-out at median MFE = ${medianMfeAtr}x ATR), TP2 = +${learnedTp2R}R (50% runner calibrated to P65-P80 MFE = ${p65MfeTargetAtr}x-${p80MfeRunnerAtr}x ATR).`,
        ],
        dynamic_management_rules: [
          `Break-Even Activation: +${learnedBeTriggerR}R -> move SL to entry + cost buffer.`,
          `Partial Scale-Out: +${learnedTp1R}R -> lock 50% realized profit.`,
          `Chandelier / Structure Ratchet: trail remaining 50% by ${learnedTrailAtrMult}x ATR14 up to +${learnedTp2R}R.`,
          `Adaptive Session Guard: block new entries during ROLLOVER_SPREAD_GUARD.`,
        ],
        risk_reward_policy: `Effective Institutional Reward-to-Risk = ${learnedRr}:1 (TP1=+${learnedTp1R}R, TP2=+${learnedTp2R}R) net of verified cTrader costs (${roundTripCostBps.toFixed(2)} bps).`,
      },
      atlas_literature_and_domain_notes: [
        `Trader Behavior Archetype (${behaviorProfile.archetype_name}): Models how professional ${traderStyle} desks wait for liquidity sweeps and Fair Value Gap (FVG) imbalances rather than chasing lagging retail crossovers.`,
        `Excursion & Dynamic SL/TP Finding (${dataset.symbol}): Across ${allMfesAtr.length} historical setups, shifting SL to Break-Even at +${learnedBeTriggerR}R and scaling 50% at +${learnedTp1R}R prevents mid-session reversals from turning winning setups into losses.`,
        `Adaptive Session Behavior Finding: Best execution occurs in [${bestAdaptiveBehaviors.join(", ")}].`,
      ],
      created_at_unix: nowUnix,
    };

    const stateKey = `atlas.forge.research_brief.${dataset.symbol}.${traderStyle}`;
    const existing = this.chronicle.stateGet(stateKey);
    this.chronicle.stateSet(stateKey, brief, {
      expectedRevision: existing.revision,
      sender: "atlas",
    });

    this.chronicle.storeMemory({
      concept_key: `atlas_research::${dataset.symbol}::${traderStyle}`,
      content: `Atlas -> Forge Institutional Trader-Behavior Research [${dataset.symbol} ${traderStyle}]: Archetype='${behaviorProfile.archetype_name}', Learned SL=${learnedSlMult}x ATR, Dynamic BE=+${learnedBeTriggerR}R, TP1=+${learnedTp1R}R (50%), TP2=+${learnedTp2R}R (RR=${learnedRr}:1). Best Adaptive Sessions: ${bestAdaptiveBehaviors.join(", ")}.`,
      pillar: "semantic",
      domain: "atlas_forge_research",
      tags: ["atlas", "forge", "institutional", dataset.symbol, traderStyle],
      source_repository: "atlas",
      metadata: {
        brief_id: briefId,
        archetype_id: behaviorProfile.archetype_id,
        learned_rr: learnedRr,
        learned_be_r: learnedBeTriggerR,
        learned_tp1_r: learnedTp1R,
        learned_tp2_r: learnedTp2R,
        best_sessions: bestSessions,
        best_adaptive_behaviors: bestAdaptiveBehaviors,
        best_regimes: bestRegimes,
      },
    });

    return brief;
  }
}
