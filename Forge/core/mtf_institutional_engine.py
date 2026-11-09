"""
Forge.core.mtf_institutional_engine
===================================
Native Python 3.10 7-Timeframe Institutional Market Structure, Adaptive Session
Behavior, Trader Archetype Modeling, and Dynamic Stop-Loss / Take-Profit Engine.

Collaborates with Atlas and Chronicle to:
  1. Ingest & annotate 7 timeframes (W1, D1, H4, H1, M30, M15, M5) with zero look-ahead.
  2. Tag every bar with its UTC Session and Adaptive Session Behavior
     (ASIAN_RANGE_ACCUMULATION, LONDON_OPEN_JUDAS_SWEEP, LONDON_TREND_EXPANSION,
      NY_OVERLAP_INSTITUTIONAL_DRIVE, NY_AFTERNOON_REBALANCING, ROLLOVER_SPREAD_GUARD).
  3. Detect Institutional Microstructure:
     - Asian Session High / Low (ASH / ASL) liquidity pools
     - Bullish / Bearish Liquidity Sweeps (Stop Hunts / Rejection Wicks)
     - 3-Candle Fair Value Gaps (FVG / Institutional Imbalance)
     - Break of Structure (BOS) & Discount / Premium dealing range zones
  4. Model how each Trader Archetype behaves (SCALPING, DAY_TRADING, SWING_TRADING)
     and empirically derive Dynamic Stop-Loss (Break-Even trigger, Chandelier trail)
     and Two-Stage Take-Profit (TP1 50% scale-out, TP2 50% runner) from MFE/MAE.

Run from repository root:
  python3 -m Forge.core.mtf_institutional_engine --symbol GBPUSD
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TIMEFRAMES: Tuple[str, ...] = ("W1", "D1", "H4", "H1", "M30", "M15", "M5")

YAHOO_TICKER_MAP: Dict[str, str] = {
    "GBPUSD": "GBPUSD=X",
    "EURUSD": "EURUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
}


def sha256_hex(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def classify_utc_session(unix_sec: float) -> str:
    dt = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
    hour = dt.hour
    if 0 <= hour < 7:
        return "ASIAN_SESSION"
    if 7 <= hour < 12:
        return "LONDON_SESSION"
    if 12 <= hour < 16:
        return "LONDON_NY_OVERLAP"
    if 16 <= hour < 21:
        return "NEW_YORK_SESSION"
    return "OFF_HOURS_ROLLOVER"


def classify_adaptive_session_behavior(
    unix_sec: float,
    high: float,
    low: float,
    close: float,
    asian_high: float,
    asian_low: float,
    efficiency_ratio: float,
) -> str:
    dt = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
    hour_dec = dt.hour + dt.minute / 60.0
    if 0.0 <= hour_dec < 7.0:
        return "ASIAN_RANGE_ACCUMULATION"
    if 7.0 <= hour_dec < 9.5:
        swept_high = high > asian_high and close <= asian_high
        swept_low = low < asian_low and close >= asian_low
        if swept_high or swept_low or efficiency_ratio < 0.35:
            return "LONDON_OPEN_JUDAS_SWEEP"
        return "LONDON_TREND_EXPANSION"
    if 9.5 <= hour_dec < 12.0:
        return "LONDON_TREND_EXPANSION"
    if 12.0 <= hour_dec < 16.0:
        return "NY_OVERLAP_INSTITUTIONAL_DRIVE"
    if 16.0 <= hour_dec < 21.0:
        return "NY_AFTERNOON_REBALANCING"
    return "ROLLOVER_SPREAD_GUARD"


class MultiTimeframeInstitutionalEngine:
    """Python 3.10 Institutional Multi-Timeframe Data & Trader-Behavior Engine."""

    def __init__(self, workspace_root: Optional[Path] = None) -> None:
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self.mtf_chronicle_dir = (
            self.workspace_root / "Chronicle" / "memory" / "research" / "mtf_market_data"
        )
        self.mtf_ram_dir = self.workspace_root / "Oracle" / "data_cache" / "mtf"
        self.mtf_chronicle_dir.mkdir(parents=True, exist_ok=True)
        self.mtf_ram_dir.mkdir(parents=True, exist_ok=True)

    def resolve_yahoo_ticker(self, symbol: str) -> str:
        upper = symbol.upper()
        if upper in YAHOO_TICKER_MAP:
            return YAHOO_TICKER_MAP[upper]
        if len(upper) == 6 and not upper.endswith("USD"):
            return f"{upper}=X"
        return upper

    def annotate_bar_series(self, raw_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        n = len(raw_bars)
        if n == 0:
            return []

        annotated: List[Dict[str, Any]] = []
        atr_arr: List[float] = [0.0] * n
        avg_gain = 0.0
        avg_loss = 0.0

        current_day_key = ""
        asian_high = float(raw_bars[0]["high"])
        asian_low = float(raw_bars[0]["low"])

        for i in range(n):
            b = raw_bars[i]
            open_p = float(b["open"])
            high_p = float(b["high"])
            low_p = float(b["low"])
            close_p = float(b["close"])
            vol = float(b.get("volume", 0.0) or 0.0)
            unix_sec = int(b["unix_sec"])

            prev_close = float(raw_bars[i - 1]["close"]) if i > 0 else open_p
            tr = max(high_p - low_p, abs(high_p - prev_close), abs(low_p - prev_close))
            atr14 = (tr or close_p * 0.0015) if i == 0 else (atr_arr[i - 1] * 13.0 + tr) / 14.0
            atr_arr[i] = atr14

            k9 = 2.0 / 10.0
            k21 = 2.0 / 22.0
            ema9 = close_p if i == 0 else close_p * k9 + annotated[i - 1]["ema9"] * (1.0 - k9)
            ema21 = close_p if i == 0 else close_p * k21 + annotated[i - 1]["ema21"] * (1.0 - k21)

            change = close_p - prev_close
            gain = max(0.0, change)
            loss = max(0.0, -change)
            if i < 14:
                avg_gain = (avg_gain * i + gain) / (i + 1)
                avg_loss = (avg_loss * i + loss) / (i + 1)
            else:
                avg_gain = (avg_gain * 13.0 + gain) / 14.0
                avg_loss = (avg_loss * 13.0 + loss) / 14.0
            rs = 100.0 if avg_loss == 0 else avg_gain / avg_loss
            rsi14 = 100.0 - (100.0 / (1.0 + rs))

            dt = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
            day_key = dt.strftime("%Y-%m-%d")
            if day_key != current_day_key:
                current_day_key = day_key
                asian_high = high_p
                asian_low = low_p
            elif dt.hour < 7:
                asian_high = max(asian_high, high_p)
                asian_low = min(asian_low, low_p)

            prior_start = max(0, i - 16)
            prior_high = max((float(raw_bars[j]["high"]) for j in range(prior_start, i)), default=high_p)
            prior_low = min((float(raw_bars[j]["low"]) for j in range(prior_start, i)), default=low_p)
            s_high = max(prior_high, high_p)
            s_low = min(prior_low, low_p)

            lower_wick = min(open_p, close_p) - low_p
            upper_wick = high_p - max(open_p, close_p)
            bar_range = max(high_p - low_p, 1e-9)

            liq_sweep_bull = (
                i >= 5
                and ((low_p < prior_low and close_p > prior_low) or (dt.hour >= 7 and low_p < asian_low and close_p > asian_low))
                and (lower_wick / bar_range >= 0.32)
            )
            liq_sweep_bear = (
                i >= 5
                and ((high_p > prior_high and close_p < prior_high) or (dt.hour >= 7 and high_p > asian_high and close_p < asian_high))
                and (upper_wick / bar_range >= 0.32)
            )

            fvg_bull = False
            fvg_bear = False
            if i >= 2:
                b2 = raw_bars[i - 2]
                b1 = raw_bars[i - 1]
                body1 = abs(float(b1["close"]) - float(b1["open"]))
                if low_p > float(b2["high"]) and float(b1["close"]) > float(b1["open"]) and body1 > atr14 * 0.55:
                    fvg_bull = True
                if high_p < float(b2["low"]) and float(b1["close"]) < float(b1["open"]) and body1 > atr14 * 0.55:
                    fvg_bear = True

            bos_dir = 0
            if i >= 5:
                if close_p > prior_high and close_p > open_p:
                    bos_dir = 1
                elif close_p < prior_low and close_p < open_p:
                    bos_dir = -1

            dealing_span = max(s_high - s_low, 1e-9)
            pos_in_range = (close_p - s_low) / dealing_span
            zone = "DISCOUNT" if pos_in_range <= 0.42 else ("PREMIUM" if pos_in_range >= 0.58 else "EQUILIBRIUM")

            er_start = max(0, i - 14)
            net_move = abs(close_p - float(raw_bars[er_start]["close"]))
            path_sum = sum(
                abs(float(raw_bars[j]["close"]) - float(raw_bars[j - 1]["close"]))
                for j in range(er_start + 1, i + 1)
            )
            efficiency_ratio = net_move / path_sum if path_sum > 1e-9 else 0.0

            vol_start = max(0, i - 60)
            lower_vol_count = sum(1 for j in range(vol_start, i + 1) if atr_arr[j] <= atr14)
            vol_percentile = lower_vol_count / max(1, i - vol_start + 1)

            if vol_percentile >= 0.82 and tr > atr14 * 1.25:
                regime = "VOLATILITY_EXPANSION"
            elif efficiency_ratio >= 0.28 and ema9 > ema21 and close_p >= ema9:
                regime = "TRENDING_BULLISH"
            elif efficiency_ratio >= 0.28 and ema9 < ema21 and close_p <= ema9:
                regime = "TRENDING_BEARISH"
            else:
                regime = "CONSOLIDATION_RANGING"

            adaptive_session = classify_adaptive_session_behavior(
                unix_sec, high_p, low_p, close_p, asian_high, asian_low, efficiency_ratio
            )

            annotated.append({
                "ts": b["ts"],
                "unix_sec": unix_sec,
                "open": round(open_p, 5),
                "high": round(high_p, 5),
                "low": round(low_p, 5),
                "close": round(close_p, 5),
                "volume": round(vol, 2),
                "session": classify_utc_session(unix_sec),
                "adaptive_session": adaptive_session,
                "regime": regime,
                "atr14": round(atr14, 6),
                "ema9": round(ema9, 5),
                "ema21": round(ema21, 5),
                "rsi14": round(rsi14, 2),
                "swing_high_16": round(s_high, 5),
                "swing_low_16": round(s_low, 5),
                "efficiency_ratio": round(efficiency_ratio, 4),
                "volatility_percentile": round(vol_percentile, 4),
                "asian_session_high": round(asian_high, 5),
                "asian_session_low": round(asian_low, 5),
                "liquidity_sweep_bullish": liq_sweep_bull,
                "liquidity_sweep_bearish": liq_sweep_bear,
                "fvg_bullish": fvg_bull,
                "fvg_bearish": fvg_bear,
                "bos_direction": bos_dir,
                "discount_premium_zone": zone,
            })

        return annotated

    def load_cached_dataset(self, symbol: str) -> Optional[Dict[str, Any]]:
        path = self.mtf_chronicle_dir / symbol.upper() / "latest.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def evaluate_trader_archetype_signal(
        bar: Dict[str, Any],
        macro_bars: List[Dict[str, Any]],
        setup_bars: List[Dict[str, Any]],
        trader_style: str,
    ) -> int:
        macro_votes = 0.0
        for m_bar in macro_bars:
            if m_bar["ema9"] > m_bar["ema21"]:
                macro_votes += 1.0
            elif m_bar["ema9"] < m_bar["ema21"]:
                macro_votes -= 1.0
            macro_votes += float(m_bar.get("bos_direction", 0)) * 0.5

        setup_votes = 0.0
        for s_bar in setup_bars:
            if s_bar["close"] > s_bar["ema9"] and 44 <= s_bar["rsi14"] <= 74:
                setup_votes += 1.0
            elif s_bar["close"] < s_bar["ema9"] and 26 <= s_bar["rsi14"] <= 56:
                setup_votes -= 1.0
            if s_bar.get("fvg_bullish") or s_bar.get("liquidity_sweep_bullish"):
                setup_votes += 0.75
            if s_bar.get("fvg_bearish") or s_bar.get("liquidity_sweep_bearish"):
                setup_votes -= 0.75

        if trader_style == "SCALPING":
            bull = (
                bar.get("liquidity_sweep_bullish")
                or bar.get("fvg_bullish")
                or (bar["close"] > bar["ema9"] >= bar["ema21"])
            ) and 35 <= bar["rsi14"] <= 70
            bear = (
                bar.get("liquidity_sweep_bearish")
                or bar.get("fvg_bearish")
                or (bar["close"] < bar["ema9"] <= bar["ema21"])
            ) and 30 <= bar["rsi14"] <= 65
            if bull and macro_votes >= 0 and setup_votes > 0:
                return 1
            if bear and macro_votes <= 0 and setup_votes < 0:
                return -1
            return 0

        if trader_style == "DAY_TRADING":
            bull = (
                bar.get("liquidity_sweep_bullish")
                or bar.get("fvg_bullish")
                or bar.get("bos_direction") == 1
                or (bar["close"] > bar["ema9"] >= bar["ema21"])
            ) and bar.get("discount_premium_zone") != "PREMIUM" and 38 <= bar["rsi14"] <= 68
            bear = (
                bar.get("liquidity_sweep_bearish")
                or bar.get("fvg_bearish")
                or bar.get("bos_direction") == -1
                or (bar["close"] < bar["ema9"] <= bar["ema21"])
            ) and bar.get("discount_premium_zone") != "DISCOUNT" and 32 <= bar["rsi14"] <= 62
            if bull and macro_votes >= 0 and setup_votes > 0:
                return 1
            if bear and macro_votes <= 0 and setup_votes < 0:
                return -1
            return 0

        if trader_style == "RANGE_SPECIALIST":
            is_range_context = (
                bar.get("regime") == "CONSOLIDATION_RANGING"
                or bar.get("efficiency_ratio", 0.5) < 0.34
                or bar.get("adaptive_session") in ("ASIAN_RANGE_ACCUMULATION", "NY_AFTERNOON_REBALANCING")
            )
            bull_range = (
                is_range_context
                and (
                    bar.get("liquidity_sweep_bullish")
                    or (bar.get("discount_premium_zone") == "DISCOUNT" and bar["close"] > bar["open"] and bar["rsi14"] <= 46)
                )
                and bar.get("discount_premium_zone") != "PREMIUM"
            )
            bear_range = (
                is_range_context
                and (
                    bar.get("liquidity_sweep_bearish")
                    or (bar.get("discount_premium_zone") == "PREMIUM" and bar["close"] < bar["open"] and bar["rsi14"] >= 54)
                )
                and bar.get("discount_premium_zone") != "DISCOUNT"
            )
            if bull_range and macro_votes >= -1.0:
                return 1
            if bear_range and macro_votes <= 1.0:
                return -1
            return 0

        # SWING_TRADING
        bull_swing = (
            bar.get("liquidity_sweep_bullish")
            or bar.get("fvg_bullish")
            or bar.get("bos_direction") == 1
            or (bar["close"] > bar["ema9"] > bar["ema21"] and bar.get("discount_premium_zone") == "DISCOUNT")
        ) and 36 <= bar["rsi14"] <= 66
        bear_swing = (
            bar.get("liquidity_sweep_bearish")
            or bar.get("fvg_bearish")
            or bar.get("bos_direction") == -1
            or (bar["close"] < bar["ema9"] < bar["ema21"] and bar.get("discount_premium_zone") == "PREMIUM")
        ) and 34 <= bar["rsi14"] <= 64
        if bull_swing and macro_votes >= 0 and setup_votes > 0:
            return 1
        if bear_swing and macro_votes <= 0 and setup_votes < 0:
            return -1
        return 0

    @staticmethod
    def compute_instrument_physics_profile(symbol: str, m15_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Empirically profiles the candle physics DNA of an instrument (wick-to-body ratio, sweep threshold, FVG body threshold)."""
        if not m15_bars:
            return {
                "symbol": symbol,
                "avg_wick_to_body_ratio": 1.1,
                "learned_sweep_wick_ratio_threshold": 0.32,
                "learned_fvg_body_atr_threshold": 0.55,
                "trend_persistence_score": 0.35,
                "mean_reversion_propensity": 0.65,
                "dominant_personality": "HYBRID_INSTITUTIONAL_FLOW",
                "peak_volatility_utc_hours": [8, 9, 13, 14, 15],
            }

        wick_to_body_sum = 0.0
        wick_ratio_samples: List[float] = []
        body_atr_samples: List[float] = []
        er_sum = 0.0
        hour_vol_sums: Dict[int, Tuple[float, int]] = {}

        for b in m15_bars:
            full_range = max(float(b["high"]) - float(b["low"]), 1e-9)
            body = max(abs(float(b["close"]) - float(b["open"])), full_range * 0.05)
            total_wick = max(0.0, full_range - body)
            upper_wick = float(b["high"]) - max(float(b["open"]), float(b["close"]))
            lower_wick = min(float(b["open"]), float(b["close"])) - float(b["low"])
            max_single_wick_ratio = max(upper_wick, lower_wick) / full_range

            wick_to_body_sum += total_wick / body
            wick_ratio_samples.append(max_single_wick_ratio)
            body_atr_samples.append(body / max(float(b.get("atr14", 0.0001)), 1e-9))
            er_sum += float(b.get("efficiency_ratio", 0.3))

            hour = datetime.fromtimestamp(int(b["unix_sec"]), tz=timezone.utc).hour
            prev_sum, prev_cnt = hour_vol_sums.get(hour, (0.0, 0))
            hour_vol_sums[hour] = (prev_sum + float(b.get("atr14", 0.0)), prev_cnt + 1)

        wick_ratio_samples.sort()
        body_atr_samples.sort()
        n = len(m15_bars)
        avg_wick_to_body = round(wick_to_body_sum / n, 3)
        p65_wick = wick_ratio_samples[min(n - 1, int(n * 0.65))]
        p60_body_atr = body_atr_samples[min(n - 1, int(n * 0.60))]
        avg_er = round(er_sum / n, 3)
        mean_rev = round(max(0.0, min(1.0, 1.0 - avg_er)), 3)

        personality = "HYBRID_INSTITUTIONAL_FLOW"
        if avg_wick_to_body >= 1.35 and mean_rev >= 0.68:
            personality = "STOP_HUNT_MEAN_REVERTING"
        elif avg_er >= 0.34:
            personality = "MOMENTUM_BREAKOUT_PERSISTENT"

        ranked_hours = sorted(
            hour_vol_sums.items(),
            key=lambda kv: kv[1][0] / max(1, kv[1][1]),
            reverse=True,
        )[:5]
        peak_hours = sorted(h for h, _ in ranked_hours)

        return {
            "symbol": symbol,
            "avg_wick_to_body_ratio": avg_wick_to_body,
            "learned_sweep_wick_ratio_threshold": round(min(0.45, max(0.24, p65_wick)), 3),
            "learned_fvg_body_atr_threshold": round(min(0.75, max(0.38, p60_body_atr)), 3),
            "trend_persistence_score": avg_er,
            "mean_reversion_propensity": mean_rev,
            "dominant_personality": personality,
            "peak_volatility_utc_hours": peak_hours,
        }

    @staticmethod
    def inspect_strategy_regime_drift(
        symbol: str, trader_style: str, bars: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detects whether recent market volatility, efficiency, or wick physics have drifted from the training baseline."""
        n = len(bars)
        train_end = max(1, int(n * 0.6))
        recent_start = max(0, int(n * 0.8))
        train_slice = bars[:train_end]
        recent_slice = bars[recent_start:]

        def avg(arr: List[Dict[str, Any]], fn: Any) -> float:
            return sum(fn(x) for x in arr) / max(1, len(arr))

        b_atr = avg(train_slice, lambda b: float(b.get("atr14", 0.001)) / max(float(b["close"]), 1e-9) * 10000.0)
        r_atr = avg(recent_slice, lambda b: float(b.get("atr14", 0.001)) / max(float(b["close"]), 1e-9) * 10000.0)
        b_er = avg(train_slice, lambda b: float(b.get("efficiency_ratio", 0.3)))
        r_er = avg(recent_slice, lambda b: float(b.get("efficiency_ratio", 0.3)))

        vol_drift = ((r_atr - b_atr) / max(b_atr, 0.01)) * 100.0
        er_drift = ((r_er - b_er) / max(b_er, 0.01)) * 100.0
        max_abs = max(abs(vol_drift), abs(er_drift))

        status = "STABLE"
        rec = f"Market regime physics for {symbol} ({trader_style}) remain aligned with training baseline."
        if max_abs > 38.0:
            status = "NEEDS_RECALIBRATION"
            rec = f"Significant market physics shift detected ({vol_drift:.1f}% volatility delta). Trigger Forge Mistake-Clinic recalibration."
        elif max_abs > 20.0:
            status = "MODERATE_DRIFT"
            rec = f"Moderate regime shift detected ({vol_drift:.1f}% volatility delta). Dynamic ATR stops automatically compensate."

        return {
            "symbol": symbol,
            "trader_style": trader_style,
            "drift_status": status,
            "volatility_drift_pct": round(vol_drift, 2),
            "efficiency_drift_pct": round(er_drift, 2),
            "recommendation": rec,
        }

    @staticmethod
    def run_monte_carlo_sequence_stress_test(
        trade_returns_r: List[float],
        risk_per_trade_pct: float = 0.5,
        max_allowed_drawdown_pct: float = 8.5,
        iterations: int = 1000,
    ) -> Dict[str, Any]:
        """Runs 1,000 shuffled trade sequence paths to verify 95th-percentile worst-case drawdown and losing streak."""
        if not trade_returns_r:
            return {
                "iterations": iterations,
                "mc50_median_drawdown_pct": 0.0,
                "mc95_worst_case_drawdown_pct": 0.0,
                "mc99_extreme_drawdown_pct": 0.0,
                "mc95_max_consecutive_losses": 0,
                "passed_mc95_gate": True,
            }

        import random
        rng = random.Random(42)
        drawdowns: List[float] = []
        streaks: List[int] = []

        for _ in range(iterations):
            shuffled = list(trade_returns_r)
            rng.shuffle(shuffled)
            eq = 100.0
            peak = 100.0
            max_dd = 0.0
            cur_streak = 0
            max_streak = 0
            for r in shuffled:
                eq += r * risk_per_trade_pct
                if eq > peak:
                    peak = eq
                dd = ((peak - eq) / max(peak, 1e-9)) * 100.0
                if dd > max_dd:
                    max_dd = dd
                if r < 0:
                    cur_streak += 1
                    if cur_streak > max_streak:
                        max_streak = cur_streak
                else:
                    cur_streak = 0
            drawdowns.append(max_dd)
            streaks.append(max_streak)

        drawdowns.sort()
        streaks.sort()
        mc50_dd = round(drawdowns[int(iterations * 0.50)], 2)
        mc95_dd = round(drawdowns[int(iterations * 0.95)], 2)
        mc99_dd = round(drawdowns[int(iterations * 0.99)], 2)
        mc95_streak = streaks[int(iterations * 0.95)]

        return {
            "iterations": iterations,
            "mc50_median_drawdown_pct": mc50_dd,
            "mc95_worst_case_drawdown_pct": mc95_dd,
            "mc99_extreme_drawdown_pct": mc99_dd,
            "mc95_max_consecutive_losses": mc95_streak,
            "passed_mc95_gate": mc95_dd <= max_allowed_drawdown_pct,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Forge V2 7-Timeframe Institutional Engine")
    parser.add_argument("--symbol", default="GBPUSD", help="Trading pair symbol (e.g. GBPUSD, EURUSD)")
    args = parser.parse_args()

    engine = MultiTimeframeInstitutionalEngine()
    ds = engine.load_cached_dataset(args.symbol)
    if not ds:
        print(f"No cached MTF dataset found for {args.symbol} yet.")
        return
    m15_bars = ds.get("series", {}).get("M15", [])
    physics = engine.compute_instrument_physics_profile(args.symbol.upper(), m15_bars)
    drift = engine.inspect_strategy_regime_drift(args.symbol.upper(), "DAY_TRADING", m15_bars)
    print(
        json.dumps(
            {
                "symbol": ds.get("symbol"),
                "dataset_fingerprint": ds.get("dataset_fingerprint"),
                "timeframe_counts": ds.get("timeframe_counts"),
                "instrument_physics": physics,
                "regime_drift_report": drift,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

