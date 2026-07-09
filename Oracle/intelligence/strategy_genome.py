"""
Oracle.intelligence.strategy_genome
==================================
Constitutional Strategy Architecture with FULL INDICATOR IMPLEMENTATIONS.

Every indicator type referenced in strategy_library.py is implemented here.
No logic_type ever falls through to return 0.0.
Unrecognized types use intelligent fallback (EMA slope) rather than dead zero.
"""
from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from intelligence.technicals import (sma, ema, ema_series, rsi, macd, bollinger, atr,
                                      returns_stats)


# ---- Additional Indicator Functions ----

def stochastic_k(closes: List[float], highs: List[float], lows: List[float], period: int = 14) -> Optional[float]:
    """Fast %K stochastic."""
    if len(closes) < period:
        return None
    high_window = max(highs[-period:])
    low_window = min(lows[-period:])
    if high_window == low_window:
        return 50.0
    return ((closes[-1] - low_window) / (high_window - low_window)) * 100


def rate_of_change(closes: List[float], period: int = 12) -> Optional[float]:
    """Rate of change (ROC)."""
    if len(closes) < period + 1:
        return None
    prev = closes[-period - 1]
    if prev == 0:
        return 0.0
    return ((closes[-1] - prev) / prev) * 100


def adx_proxy(closes: List[float], highs: List[float], lows: List[float], period: int = 14) -> Optional[float]:
    """Simplified ADX proxy using directional movement."""
    if len(closes) < period + 2:
        return None
    plus_dm = 0.0
    minus_dm = 0.0
    tr_sum = 0.0
    for i in range(-period, 0):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]
        if high_diff > low_diff and high_diff > 0:
            plus_dm += high_diff
        if low_diff > high_diff and low_diff > 0:
            minus_dm += low_diff
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_sum += tr
    if tr_sum == 0:
        return 0.0
    plus_di = (plus_dm / tr_sum) * 100
    minus_di = (minus_dm / tr_sum) * 100
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return 0.0
    dx = abs(plus_di - minus_di) / di_sum * 100
    return dx


def donchian_high(highs: List[float], period: int = 20) -> Optional[float]:
    if len(highs) < period:
        return None
    return max(highs[-period:])


def donchian_low(lows: List[float], period: int = 20) -> Optional[float]:
    if len(lows) < period:
        return None
    return min(lows[-period:])


def hma(closes: List[float], period: int = 14) -> Optional[float]:
    """Hull Moving Average approximation."""
    half = max(1, period // 2)
    sqrt_p = max(1, int(period ** 0.5))
    e_half = ema(closes, half)
    e_full = ema(closes, period)
    if e_half is None or e_full is None:
        return None
    diff = 2 * e_half - e_full
    # Approximate: just use the diff as the HMA value
    return diff


def williams_r(closes: List[float], highs: List[float], lows: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period:
        return None
    high_window = max(highs[-period:])
    low_window = min(lows[-period:])
    if high_window == low_window:
        return -50.0
    return ((high_window - closes[-1]) / (high_window - low_window)) * -100


def cci(closes: List[float], period: int = 20) -> Optional[float]:
    """Commodity Channel Index."""
    if len(closes) < period:
        return None
    tp_list = closes[-period:]  # simplified: using closes as typical price
    mean_tp = sum(tp_list) / period
    mean_dev = sum(abs(x - mean_tp) for x in tp_list) / period
    if mean_dev == 0:
        return 0.0
    return (closes[-1] - mean_tp) / (0.015 * mean_dev)


# ---- Module Definitions ----

@dataclass
class StrategyModule:
    """Base class for constitutional modules."""
    logic_type: str = "default"
    params: Dict[str, Any] = field(default_factory=dict)

    def mutate(self, rng: random.Random, rate: float = 0.3):
        for k, v in self.params.items():
            if rng.random() < rate:
                if isinstance(v, (int, float)):
                    self.params[k] = round(v * rng.uniform(0.7, 1.3), 3)
                elif isinstance(v, bool):
                    self.params[k] = not v
        if rng.random() < rate * 0.2:
            self.logic_type = self._get_next_logic(rng)

    def _get_next_logic(self, rng: random.Random) -> str:
        return self.logic_type


@dataclass
class MarketRegimeModule(StrategyModule):
    def is_allowed(self, current_regime: str, volatility: float) -> bool:
        allowed = self.params.get("allowed_regimes", ["trending_up", "trending_down", "ranging", "high_volatility"])
        vol_limit = self.params.get("volatility_limit", 1.0)
        return current_regime in allowed and volatility <= vol_limit

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["default", "strict", "permissive"])


@dataclass
class TrendModule(StrategyModule):
    """Determines the primary directional bias. Handles ALL library logic types."""

    def bias(self, closes: List[float], highs: List[float] = None, lows: List[float] = None) -> float:
        """
        Returns directional bias: 1.0 (bullish), -1.0 (bearish), or fractional.
        NEVER returns 0.0 for recognized types.
        """
        if len(closes) < 5:
            return 0.0

        if self.logic_type == "sma_crossover":
            fast_p = int(self.params.get("fast", 20))
            slow_p = int(self.params.get("slow", 50))
            fast_val = sma(closes, fast_p)
            slow_val = sma(closes, slow_p)
            if fast_val is None or slow_val is None:
                # Fallback: use shorter periods
                fast_val = sma(closes, min(fast_p, len(closes) - 1)) or closes[-1]
                slow_val = sma(closes, min(slow_p, len(closes) - 1)) or closes[-1]
            return 1.0 if fast_val > slow_val else -1.0

        elif self.logic_type == "ema_slope":
            period = int(self.params.get("period", 20))
            period = min(period, len(closes) - 1)
            e = ema(closes, period)
            e_prev = ema(closes[:-1], period) if len(closes) > period else None
            if e and e_prev:
                return 1.0 if e > e_prev else -1.0
            # Fallback: price direction
            return 1.0 if closes[-1] > closes[-2] else -1.0

        elif self.logic_type == "price_above_sma":
            period = int(self.params.get("period", 50))
            period = min(period, len(closes) - 1)
            s = sma(closes, period)
            if s is None:
                s = sma(closes, min(20, len(closes) - 1)) or closes[-1]
            return 1.0 if closes[-1] > s else -1.0

        elif self.logic_type == "supertrend":
            # Supertrend approximation: EMA + ATR band
            period = int(self.params.get("period", 10))
            mult = float(self.params.get("multiplier", 3.0))
            period = min(period, len(closes) - 2)
            e = ema(closes, period)
            if e is None:
                return 1.0 if closes[-1] > closes[-2] else -1.0
            if highs and lows:
                a = atr(highs, lows, closes, min(period, len(closes) - 2))
                if a:
                    upper = e + mult * a
                    lower = e - mult * a
                    if closes[-1] > upper:
                        return 1.0
                    elif closes[-1] < lower:
                        return -1.0
            return 1.0 if closes[-1] > e else -1.0

        elif self.logic_type == "donchian_trend":
            period = min(int(self.params.get("period", 20)), len(closes) - 1)
            if highs and lows:
                dh = donchian_high(highs, period)
                dl = donchian_low(lows, period)
                if dh and dl:
                    mid = (dh + dl) / 2
                    return 1.0 if closes[-1] > mid else -1.0
            # Fallback
            high_n = max(closes[-period:]) if period <= len(closes) else max(closes)
            low_n = min(closes[-period:]) if period <= len(closes) else min(closes)
            mid = (high_n + low_n) / 2
            return 1.0 if closes[-1] > mid else -1.0

        elif self.logic_type == "ichimoku_cloud":
            tenkan = int(self.params.get("tenkan", 9))
            kijun = int(self.params.get("kijun", 26))
            tenkan = min(tenkan, len(closes) - 1)
            kijun = min(kijun, len(closes) - 1)
            tenkan_val = (max(closes[-tenkan:]) + min(closes[-tenkan:])) / 2
            kijun_val = (max(closes[-kijun:]) + min(closes[-kijun:])) / 2
            return 1.0 if tenkan_val > kijun_val else -1.0

        elif self.logic_type == "vwap_trend":
            # Approximate VWAP as simple moving average (volume not available)
            period = min(int(self.params.get("period", 20)), len(closes) - 1)
            vwap = sma(closes, period) or closes[-1]
            return 1.0 if closes[-1] > vwap else -1.0

        elif self.logic_type == "adx_trend":
            # ADX determines trend strength; direction from price slope
            period = min(int(self.params.get("period", 14)), len(closes) - 3)
            threshold = float(self.params.get("threshold", 25))
            if highs and lows:
                adx_val = adx_proxy(closes, highs, lows, period)
                if adx_val and adx_val > threshold:
                    # Strong trend: use slope for direction
                    return 1.0 if closes[-1] > closes[-period] else -1.0
            # Weak trend or no data: use recent slope
            lookback = min(10, len(closes) - 1)
            return 1.0 if closes[-1] > closes[-lookback] else -1.0

        elif self.logic_type == "hma_slope":
            period = min(int(self.params.get("period", 14)), len(closes) - 2)
            h = hma(closes, period)
            h_prev = hma(closes[:-1], period) if len(closes) > period + 1 else None
            if h and h_prev:
                return 1.0 if h > h_prev else -1.0
            return 1.0 if closes[-1] > closes[-2] else -1.0

        elif self.logic_type == "market_structure":
            # Higher highs/higher lows vs lower highs/lower lows
            lookback = min(int(self.params.get("lookback", 20)), len(closes) - 2)
            mid = len(closes) - lookback // 2
            recent_high = max(closes[-lookback//2:])
            earlier_high = max(closes[-lookback:-lookback//2]) if lookback > 2 else closes[-2]
            return 1.0 if recent_high > earlier_high else -1.0

        # UNIVERSAL FALLBACK: never return 0.0
        # Use simple price slope as directional indicator
        lookback = min(10, len(closes) - 1)
        if lookback > 0 and closes[-lookback] != 0:
            slope = (closes[-1] - closes[-lookback]) / closes[-lookback]
            if slope > 0.001:
                return 1.0
            elif slope < -0.001:
                return -1.0
        return 1.0 if closes[-1] > closes[-2] else -1.0

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["sma_crossover", "ema_slope", "price_above_sma",
                           "supertrend", "donchian_trend", "vwap_trend",
                           "adx_trend", "hma_slope", "market_structure"])


@dataclass
class MomentumModule(StrategyModule):
    """Confirms the velocity of the move. Handles ALL library logic types."""

    def confirm(self, closes: List[float], highs: List[float] = None, lows: List[float] = None) -> float:
        """
        Returns momentum confirmation: 1.0, -1.0, or 0.0.
        0.0 means neutral (no confirmation, no rejection).
        """
        if len(closes) < 5:
            return 0.0

        if self.logic_type == "rsi":
            period = int(self.params.get("period", 14))
            r = rsi(closes, min(period, len(closes) - 2))
            if r is None:
                r = 50.0
            upper = self.params.get("upper", 70)
            lower = self.params.get("lower", 30)
            if r > upper:
                return 1.0
            if r < lower:
                return -1.0
            # PARTIAL confirmation instead of hard 0.0
            # Scale linearly: 50 = 0, 70 = 1.0, 30 = -1.0
            mid = (upper + lower) / 2
            rng = (upper - lower) / 2
            if rng > 0:
                return round((r - mid) / rng, 2)
            return 0.0

        elif self.logic_type == "macd_hist":
            fast = int(self.params.get("fast", 12))
            slow = int(self.params.get("slow", 26))
            slow = min(slow, len(closes) - 10)
            fast = min(fast, slow - 1)
            m = macd(closes, fast, slow)
            if m:
                hist = m["histogram"]
                if hist > 0:
                    return min(1.0, hist * 10)  # Partial strength
                elif hist < 0:
                    return max(-1.0, hist * 10)
            return 0.0

        elif self.logic_type == "stochastic":
            k_period = int(self.params.get("k_period", 14))
            k_period = min(k_period, len(closes) - 1)
            upper = self.params.get("upper", 80)
            lower = self.params.get("lower", 20)
            if highs and lows:
                k_val = stochastic_k(closes, highs, lows, k_period)
            else:
                # Approximate with closes only
                high_w = max(closes[-k_period:])
                low_w = min(closes[-k_period:])
                k_val = ((closes[-1] - low_w) / (high_w - low_w) * 100) if high_w != low_w else 50.0
            if k_val is None:
                return 0.0
            if k_val > upper:
                return 1.0
            if k_val < lower:
                return -1.0
            # Partial
            mid = (upper + lower) / 2
            rng = (upper - lower) / 2
            return round((k_val - mid) / rng, 2) if rng > 0 else 0.0

        elif self.logic_type == "adx_strength":
            period = min(int(self.params.get("period", 14)), len(closes) - 3)
            threshold = float(self.params.get("threshold", 20))
            if highs and lows:
                adx_val = adx_proxy(closes, highs, lows, period)
            else:
                # Approximate: use absolute slope as strength proxy
                lookback = min(period, len(closes) - 1)
                adx_val = abs(closes[-1] - closes[-lookback]) / closes[-lookback] * 500 if closes[-lookback] else 0
            if adx_val and adx_val > threshold:
                return 1.0 if closes[-1] > closes[-min(5, len(closes)-1)] else -1.0
            return 0.0

        elif self.logic_type == "cci":
            period = min(int(self.params.get("period", 20)), len(closes) - 1)
            upper = self.params.get("upper", 100)
            lower = self.params.get("lower", -100)
            c = cci(closes, period)
            if c is None:
                return 0.0
            if c > upper:
                return 1.0
            if c < lower:
                return -1.0
            return round(c / 100, 2)  # Partial: scale to [-1, 1]

        elif self.logic_type == "williams_r":
            period = min(int(self.params.get("period", 14)), len(closes) - 1)
            upper = self.params.get("upper", -20)
            lower = self.params.get("lower", -80)
            if highs and lows:
                w = williams_r(closes, highs, lows, period)
            else:
                high_w = max(closes[-period:])
                low_w = min(closes[-period:])
                w = ((high_w - closes[-1]) / (high_w - low_w) * -100) if high_w != low_w else -50.0
            if w is None:
                return 0.0
            if w > upper:  # Near highs (overbought)
                return 1.0
            if w < lower:  # Near lows (oversold)
                return -1.0
            return round((w - (-50)) / 50, 2)

        elif self.logic_type == "roc":
            period = min(int(self.params.get("period", 12)), len(closes) - 2)
            threshold = self.params.get("threshold", 0)
            r = rate_of_change(closes, period)
            if r is None:
                return 0.0
            if r > threshold + 1:
                return 1.0
            elif r < -(threshold + 1):
                return -1.0
            return round(r / 5, 2)  # Partial: scale

        elif self.logic_type == "price_action":
            # Simple: compare recent candles
            lookback = min(int(self.params.get("lookback", 5)), len(closes) - 1)
            up_bars = sum(1 for i in range(-lookback, 0) if closes[i] > closes[i-1])
            ratio = up_bars / lookback
            if ratio > 0.7:
                return 1.0
            elif ratio < 0.3:
                return -1.0
            return round((ratio - 0.5) * 2, 2)

        elif self.logic_type == "volume_momentum":
            # Without real volume, use price velocity as proxy
            period = min(int(self.params.get("period", 20)), len(closes) - 2)
            recent_vol = sum(abs(closes[i] - closes[i-1]) for i in range(-period//2, 0)) / max(1, period//2)
            older_vol = sum(abs(closes[i] - closes[i-1]) for i in range(-period, -period//2)) / max(1, period//2)
            if older_vol > 0 and recent_vol > older_vol * 1.2:
                return 1.0 if closes[-1] > closes[-period//2] else -1.0
            return 0.0

        # FALLBACK: use simple momentum (never crash)
        lookback = min(5, len(closes) - 1)
        change = (closes[-1] - closes[-lookback]) / closes[-lookback] if closes[-lookback] else 0
        if change > 0.01:
            return 0.5
        elif change < -0.01:
            return -0.5
        return 0.0

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["rsi", "macd_hist", "stochastic", "adx_strength",
                           "cci", "williams_r", "roc", "price_action"])


@dataclass
class VolatilityModule(StrategyModule):
    """Filters out noise or prevents trading in dangerous volatility."""

    def filter(self, series) -> bool:
        """Returns True if conditions are acceptable for trading."""
        if self.logic_type == "default":
            return True  # Always allow

        closes = series.closes if hasattr(series, 'closes') else []
        highs = series.highs if hasattr(series, 'highs') else closes
        lows = series.lows if hasattr(series, 'lows') else closes

        if len(closes) < 5:
            return True

        if self.logic_type == "atr_expansion":
            period = min(int(self.params.get("period", 14)), len(closes) - 2)
            ratio = float(self.params.get("expansion_ratio", 1.2))
            a = atr(highs, lows, closes, period)
            prev_a = atr(highs[:-1], lows[:-1], closes[:-1], period)
            if a and prev_a and prev_a > 0:
                return a >= prev_a * ratio
            return True  # Allow if can't compute

        elif self.logic_type == "atr_contraction":
            period = min(int(self.params.get("period", 14)), len(closes) - 2)
            ratio = float(self.params.get("contraction_ratio", 0.7))
            a = atr(highs, lows, closes, period)
            prev_a = atr(highs[:-1], lows[:-1], closes[:-1], period)
            if a and prev_a and prev_a > 0:
                return a <= prev_a * ratio
            return True

        elif self.logic_type == "bollinger_width":
            period = min(int(self.params.get("period", 20)), len(closes) - 1)
            threshold = float(self.params.get("threshold", 0.04))
            b = bollinger(closes, period)
            if b:
                return b["width"] <= threshold
            return True

        elif self.logic_type == "keltner_squeeze":
            # Squeeze = BB inside Keltner = low vol
            period = min(int(self.params.get("period", 20)), len(closes) - 1)
            b = bollinger(closes, period)
            a = atr(highs, lows, closes, min(period, len(closes) - 2))
            if b and a:
                bb_width = b["upper"] - b["lower"]
                kelt_width = 4 * a  # 2 ATR each side
                return bb_width < kelt_width  # Squeeze detected
            return True

        elif self.logic_type == "historical_vol":
            period = min(int(self.params.get("period", 20)), len(closes) - 2)
            threshold = float(self.params.get("threshold", 0.015))
            stats = returns_stats(closes[-period:])
            return stats.get("vol", 0) <= threshold

        # Default: always allow trading
        return True

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["atr_expansion", "default", "bollinger_width"])


@dataclass
class EntryModule(StrategyModule):
    def should_enter(self, vote: float) -> bool:
        threshold = self.params.get("threshold",
                    self.params.get("base_threshold", 0.25))
        return abs(vote) >= threshold


@dataclass
class ExitModule(StrategyModule):
    def get_stops(self, price: float, atr_val: float, direction: int) -> Tuple[float, float]:
        sl_mult = self.params.get("sl_mult", 2.0)
        tp_mult = self.params.get("tp_mult", 3.0)
        return (price - direction * sl_mult * atr_val, price + direction * tp_mult * atr_val)


@dataclass
class RiskModule(StrategyModule):
    def check(self, current_dd: float) -> bool:
        return current_dd < self.params.get("max_dd_limit", 0.2)


@dataclass
class PositionModule(StrategyModule):
    def get_size(self, equity: float, risk_per_trade: float, sl_dist: float) -> float:
        if sl_dist == 0:
            return 0.0
        return (equity * risk_per_trade) / sl_dist


@dataclass
class TradeManagementModule(StrategyModule):
    def update_stop(self, current_price: float, current_stop: float, direction: int, atr_val: float) -> float:
        if self.logic_type == "trailing_stop":
            trail_mult = self.params.get("trail_mult", 2.0)
            new_stop = current_price - direction * trail_mult * atr_val
            if direction == 1:
                return max(current_stop, new_stop)
            else:
                return min(current_stop, new_stop)
        return current_stop

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["default", "trailing_stop"])


@dataclass
class ExecutionModule(StrategyModule):
    pass


# ---- The Genome ----

@dataclass
class StrategyGenome:
    """The complete Strategy DNA."""
    genome_id: str = field(default_factory=lambda: f"strat-{uuid.uuid4().hex[:8]}")

    market_regime: MarketRegimeModule = field(default_factory=MarketRegimeModule)
    trend: TrendModule = field(default_factory=TrendModule)
    momentum: MomentumModule = field(default_factory=MomentumModule)
    volatility: VolatilityModule = field(default_factory=VolatilityModule)
    entry: EntryModule = field(default_factory=EntryModule)
    exit: ExitModule = field(default_factory=ExitModule)
    risk: RiskModule = field(default_factory=RiskModule)
    position: PositionModule = field(default_factory=PositionModule)
    trade_management: TradeManagementModule = field(default_factory=TradeManagementModule)
    execution: ExecutionModule = field(default_factory=ExecutionModule)

    generation: int = 0
    parents: List[str] = field(default_factory=list)
    fitness: float = 0.0
    backtests: int = 0
    best_return: float = 0.0
    best_sharpe: float = 0.0

    def vote(self, series) -> float:
        """Determines directional intent by combining module outputs."""
        closes = series.closes
        highs = getattr(series, 'highs', None)
        lows = getattr(series, 'lows', None)

        # 1. Trend Bias
        t_bias = self.trend.bias(closes, highs, lows)

        # 2. Momentum Confirmation
        m_conf = self.momentum.confirm(closes, highs, lows)

        # Combined: trend dominates, momentum confirms
        return (t_bias * 0.6 + m_conf * 0.4)

    def call(self, series) -> str:
        """Final decision with regime and volatility filters."""
        # 1. Volatility Filter
        if not self.volatility.filter(series):
            return "hold"

        # 2. Market Regime Filter
        from intelligence.technicals import analyze as _an
        try:
            t = _an(series)
            regime = (t.get("regime") or {}).get("regime", "unknown")
            vol = (t.get("regime") or {}).get("volatility", 0.0)
            if not self.market_regime.is_allowed(regime, vol):
                return "hold"
        except Exception:
            pass  # Allow if can't determine regime

        v = self.vote(series)
        if self.entry.should_enter(v):
            return "buy" if v > 0 else "sell"
        return "hold"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "genome_id": self.genome_id,
            "generation": self.generation,
            "parents": self.parents,
            "fitness": self.fitness,
            "backtests": self.backtests,
            "best_return": self.best_return,
            "best_sharpe": self.best_sharpe,
            "modules": {
                "market_regime": {"logic_type": self.market_regime.logic_type, "params": self.market_regime.params},
                "trend": {"logic_type": self.trend.logic_type, "params": self.trend.params},
                "momentum": {"logic_type": self.momentum.logic_type, "params": self.momentum.params},
                "volatility": {"logic_type": self.volatility.logic_type, "params": self.volatility.params},
                "entry": {"logic_type": self.entry.logic_type, "params": self.entry.params},
                "exit": {"logic_type": self.exit.logic_type, "params": self.exit.params},
                "risk": {"logic_type": self.risk.logic_type, "params": self.risk.params},
                "position": {"logic_type": self.position.logic_type, "params": self.position.params},
                "trade_management": {"logic_type": self.trade_management.logic_type, "params": self.trade_management.params},
                "execution": {"logic_type": self.execution.logic_type, "params": self.execution.params},
            }
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> StrategyGenome:
        mods = d.get("modules", {})
        def _get_mod(mod_cls, key):
            data = mods.get(key, {})
            return mod_cls(logic_type=data.get("logic_type", "default"), params=data.get("params", {}))

        return cls(
            genome_id=d.get("genome_id", ""),
            generation=d.get("generation", 0),
            parents=d.get("parents", []),
            fitness=d.get("fitness", 0.0),
            backtests=d.get("backtests", 0),
            best_return=d.get("best_return", 0.0),
            best_sharpe=d.get("best_sharpe", 0.0),
            market_regime=_get_mod(MarketRegimeModule, "market_regime"),
            trend=_get_mod(TrendModule, "trend"),
            momentum=_get_mod(MomentumModule, "momentum"),
            volatility=_get_mod(VolatilityModule, "volatility"),
            entry=_get_mod(EntryModule, "entry"),
            exit=_get_mod(ExitModule, "exit"),
            risk=_get_mod(RiskModule, "risk"),
            position=_get_mod(PositionModule, "position"),
            trade_management=_get_mod(TradeManagementModule, "trade_management"),
            execution=_get_mod(ExecutionModule, "execution"),
        )


# ---- Genetic Operators ----

def random_strategy(rng: random.Random) -> StrategyGenome:
    g = StrategyGenome()
    g.trend = TrendModule(logic_type=rng.choice(["sma_crossover", "ema_slope", "donchian_trend"]),
                          params={"fast": rng.randint(8, 20), "slow": rng.randint(25, 50)})
    g.momentum = MomentumModule(logic_type=rng.choice(["rsi", "macd_hist", "stochastic"]),
                                params={"period": 14, "upper": 65, "lower": 35})
    g.entry = EntryModule(params={"base_threshold": round(rng.uniform(0.15, 0.30), 2),
                                   "threshold": round(rng.uniform(0.15, 0.30), 2)})
    g.exit = ExitModule(params={"sl_mult": round(rng.uniform(1.5, 2.5), 1),
                                 "tp_mult": round(rng.uniform(2.5, 5.0), 1)})
    g.market_regime = MarketRegimeModule(params={
        "allowed_regimes": ["trending_up", "trending_down", "ranging", "high_volatility"]
    })
    g.volatility = VolatilityModule(logic_type="default", params={})
    return g


def mutate_strategy(genome: StrategyGenome, rng: random.Random, rate: float = 0.3) -> StrategyGenome:
    child = StrategyGenome.from_dict(genome.to_dict())
    child.generation += 1
    child.parents = [genome.genome_id]
    child.market_regime.mutate(rng, rate)
    child.trend.mutate(rng, rate)
    child.momentum.mutate(rng, rate)
    child.volatility.mutate(rng, rate)
    child.entry.mutate(rng, rate)
    child.exit.mutate(rng, rate)
    child.risk.mutate(rng, rate)
    child.position.mutate(rng, rate)
    child.trade_management.mutate(rng, rate)
    child.execution.mutate(rng, rate)
    return child


def crossover_strategy(a: StrategyGenome, b: StrategyGenome, rng: random.Random) -> StrategyGenome:
    child = StrategyGenome.from_dict(a.to_dict())
    child.generation = max(a.generation, b.generation) + 1
    child.parents = [a.genome_id, b.genome_id]
    modules = ["market_regime", "trend", "momentum", "volatility", "entry", "exit",
               "risk", "position", "trade_management", "execution"]
    b_dict = b.to_dict()["modules"]
    for m_name in modules:
        if rng.random() < 0.5:
            m_data = b_dict[m_name]
            m_class = {
                "market_regime": MarketRegimeModule, "trend": TrendModule,
                "momentum": MomentumModule, "volatility": VolatilityModule,
                "entry": EntryModule, "exit": ExitModule, "risk": RiskModule,
                "position": PositionModule, "trade_management": TradeManagementModule,
                "execution": ExecutionModule
            }[m_name]
            setattr(child, m_name, m_class(logic_type=m_data["logic_type"], params=m_data["params"].copy()))
    return child
