"""
Oracle.intelligence.strategy_library
====================================
The Strategy Library: 17+ fundamentally different trading families.

Each family produces a complete genome TEMPLATE with pre-configured module
architectures. Atlas research influences which families get higher
initialization probability. Regime detection biases family selection.

This replaces the old approach of generating random parameter combinations
with intelligent architectural diversity.
"""
from __future__ import annotations

import random
import uuid
from typing import Any, Dict, List, Optional, Tuple


# ---- Indicator Registry (expanded search space) ----

TREND_INDICATORS = [
    {"logic_type": "sma_crossover", "params": {"fast": 20, "slow": 50}},
    {"logic_type": "ema_slope", "params": {"period": 20}},
    {"logic_type": "price_above_sma", "params": {"period": 50}},
    {"logic_type": "supertrend", "params": {"period": 10, "multiplier": 3.0}},
    {"logic_type": "donchian_trend", "params": {"period": 20}},
    {"logic_type": "ichimoku_cloud", "params": {"tenkan": 9, "kijun": 26}},
    {"logic_type": "vwap_trend", "params": {"period": 20}},
    {"logic_type": "adx_trend", "params": {"period": 14, "threshold": 25}},
    {"logic_type": "hma_slope", "params": {"period": 14}},
    {"logic_type": "market_structure", "params": {"lookback": 20}},
]

MOMENTUM_INDICATORS = [
    {"logic_type": "rsi", "params": {"period": 14, "upper": 70, "lower": 30}},
    {"logic_type": "macd_hist", "params": {"fast": 12, "slow": 26, "threshold": 0}},
    {"logic_type": "stochastic", "params": {"k_period": 14, "d_period": 3, "upper": 80, "lower": 20}},
    {"logic_type": "adx_strength", "params": {"period": 14, "threshold": 20}},
    {"logic_type": "cci", "params": {"period": 20, "upper": 100, "lower": -100}},
    {"logic_type": "williams_r", "params": {"period": 14, "upper": -20, "lower": -80}},
    {"logic_type": "roc", "params": {"period": 12, "threshold": 0}},
    {"logic_type": "price_action", "params": {"lookback": 5}},
    {"logic_type": "volume_momentum", "params": {"period": 20}},
]

VOLATILITY_FILTERS = [
    {"logic_type": "atr_expansion", "params": {"period": 14, "expansion_ratio": 1.2}},
    {"logic_type": "bollinger_width", "params": {"period": 20, "threshold": 0.04}},
    {"logic_type": "atr_contraction", "params": {"period": 14, "contraction_ratio": 0.7}},
    {"logic_type": "keltner_squeeze", "params": {"period": 20}},
    {"logic_type": "historical_vol", "params": {"period": 20, "threshold": 0.015}},
    {"logic_type": "default", "params": {}},
]

EXIT_STRATEGIES = [
    {"logic_type": "atr_stops", "params": {"sl_mult": 2.0, "tp_mult": 3.0}},
    {"logic_type": "atr_stops", "params": {"sl_mult": 1.5, "tp_mult": 4.0}},
    {"logic_type": "atr_stops", "params": {"sl_mult": 2.5, "tp_mult": 5.0}},
    {"logic_type": "trailing_atr", "params": {"sl_mult": 2.0, "tp_mult": 6.0, "trail_mult": 1.5}},
    {"logic_type": "swing_stops", "params": {"lookback": 10, "tp_mult": 3.0}},
    {"logic_type": "time_exit", "params": {"max_bars": 20, "sl_mult": 2.0}},
]

# ---- Strategy Family Templates ----

class StrategyFamily:
    """Base class for a strategy family."""
    name: str = "base"
    description: str = ""
    preferred_regimes: List[str] = []

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        """Generate a genome template for this family."""
        raise NotImplementedError


class TrendFollowing(StrategyFamily):
    name = "trend_following"
    description = "Follow established trends with momentum confirmation"
    preferred_regimes = ["trending_up", "trending_down"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        trend = rng.choice([
            {"logic_type": "sma_crossover", "params": {"fast": rng.randint(10, 25), "slow": rng.randint(35, 60)}},
            {"logic_type": "ema_slope", "params": {"period": rng.randint(15, 30)}},
            {"logic_type": "supertrend", "params": {"period": rng.randint(8, 14), "multiplier": round(rng.uniform(2.0, 4.0), 1)}},
            {"logic_type": "adx_trend", "params": {"period": 14, "threshold": rng.randint(20, 30)}},
        ])
        return {
            "family": self.name,
            "trend": trend,
            "momentum": rng.choice([
                {"logic_type": "macd_hist", "params": {"fast": 12, "slow": 26, "threshold": 0}},
                {"logic_type": "adx_strength", "params": {"period": 14, "threshold": rng.randint(20, 30)}},
                {"logic_type": "roc", "params": {"period": rng.randint(8, 14), "threshold": 0}},
            ]),
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.3, "regime_bonus": 0.1}},
            "exit": rng.choice([
                {"logic_type": "atr_stops", "params": {"sl_mult": 2.0, "tp_mult": round(rng.uniform(3.0, 6.0), 1)}},
                {"logic_type": "trailing_atr", "params": {"sl_mult": 2.0, "tp_mult": 5.0, "trail_mult": 1.5}},
            ]),
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down"]}},
        }


class MeanReversion(StrategyFamily):
    name = "mean_reversion"
    description = "Fade extremes expecting reversion to mean"
    preferred_regimes = ["ranging"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": rng.choice([
                {"logic_type": "price_above_sma", "params": {"period": rng.randint(50, 200)}},
                {"logic_type": "vwap_trend", "params": {"period": 20}},
            ]),
            "momentum": rng.choice([
                {"logic_type": "rsi", "params": {"period": rng.randint(7, 21), "upper": rng.randint(70, 80), "lower": rng.randint(20, 30)}},
                {"logic_type": "cci", "params": {"period": 20, "upper": 100, "lower": -100}},
                {"logic_type": "williams_r", "params": {"period": 14, "upper": -20, "lower": -80}},
            ]),
            "volatility": {"logic_type": "bollinger_width", "params": {"period": 20, "threshold": 0.03}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.4, "regime_bonus": 0.15}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": round(rng.uniform(1.0, 2.0), 1), "tp_mult": round(rng.uniform(1.5, 3.0), 1)}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["ranging", "trending_up", "trending_down"]}},
        }


class Breakout(StrategyFamily):
    name = "breakout"
    description = "Enter on volatility expansion after compression"
    preferred_regimes = ["ranging", "high_volatility"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": rng.choice([
                {"logic_type": "donchian_trend", "params": {"period": rng.randint(15, 30)}},
                {"logic_type": "ema_slope", "params": {"period": rng.randint(10, 20)}},
            ]),
            "momentum": rng.choice([
                {"logic_type": "adx_strength", "params": {"period": 14, "threshold": rng.randint(20, 30)}},
                {"logic_type": "volume_momentum", "params": {"period": 20}},
            ]),
            "volatility": rng.choice([
                {"logic_type": "atr_expansion", "params": {"period": 14, "expansion_ratio": round(rng.uniform(1.1, 1.5), 2)}},
                {"logic_type": "keltner_squeeze", "params": {"period": 20}},
            ]),
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.35, "regime_bonus": 0.1}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": 2.0, "tp_mult": round(rng.uniform(4.0, 8.0), 1)}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["ranging", "high_volatility", "trending_up", "trending_down"]}},
        }


class Momentum(StrategyFamily):
    name = "momentum"
    description = "Ride accelerating price moves"
    preferred_regimes = ["trending_up", "trending_down"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "ema_slope", "params": {"period": rng.randint(10, 20)}},
            "momentum": rng.choice([
                {"logic_type": "roc", "params": {"period": rng.randint(8, 14), "threshold": 0}},
                {"logic_type": "macd_hist", "params": {"fast": 8, "slow": 21, "threshold": 0}},
                {"logic_type": "stochastic", "params": {"k_period": 14, "d_period": 3, "upper": 80, "lower": 20}},
            ]),
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.3, "regime_bonus": 0.15}},
            "exit": {"logic_type": "trailing_atr", "params": {"sl_mult": 1.5, "tp_mult": 5.0, "trail_mult": 1.5}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down"]}},
        }


class VolatilityExpansion(StrategyFamily):
    name = "volatility_expansion"
    description = "Trade after volatility compression resolves"
    preferred_regimes = ["high_volatility", "ranging"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "ema_slope", "params": {"period": 10}},
            "momentum": {"logic_type": "adx_strength", "params": {"period": 14, "threshold": 20}},
            "volatility": {"logic_type": "atr_expansion", "params": {"period": 14, "expansion_ratio": round(rng.uniform(1.2, 1.6), 2)}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.25, "regime_bonus": 0.1}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": 2.5, "tp_mult": round(rng.uniform(5.0, 8.0), 1)}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["high_volatility", "ranging", "trending_up", "trending_down"]}},
        }


class Pullback(StrategyFamily):
    name = "pullback"
    description = "Enter on retracement within established trend"
    preferred_regimes = ["trending_up", "trending_down"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "sma_crossover", "params": {"fast": 20, "slow": 50}},
            "momentum": {"logic_type": "rsi", "params": {"period": 14, "upper": 60, "lower": 40}},
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.25, "regime_bonus": 0.2}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": 1.5, "tp_mult": round(rng.uniform(3.0, 5.0), 1)}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down"]}},
        }


class RangeTrading(StrategyFamily):
    name = "range_trading"
    description = "Trade between support and resistance levels"
    preferred_regimes = ["ranging"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "price_above_sma", "params": {"period": 50}},
            "momentum": {"logic_type": "rsi", "params": {"period": rng.randint(7, 14), "upper": 75, "lower": 25}},
            "volatility": {"logic_type": "bollinger_width", "params": {"period": 20, "threshold": 0.06}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.4, "regime_bonus": 0.15}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": 1.5, "tp_mult": 2.0}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["ranging"]}},
        }


class DonchianBreakout(StrategyFamily):
    name = "donchian_breakout"
    description = "Classic channel breakout (Turtle Trading style)"
    preferred_regimes = ["trending_up", "trending_down", "high_volatility"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "donchian_trend", "params": {"period": rng.randint(15, 30)}},
            "momentum": {"logic_type": "adx_strength", "params": {"period": 14, "threshold": 20}},
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.2, "regime_bonus": 0.1}},
            "exit": {"logic_type": "trailing_atr", "params": {"sl_mult": 2.0, "tp_mult": 6.0, "trail_mult": 2.0}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down", "high_volatility"]}},
        }


class Supertrend(StrategyFamily):
    name = "supertrend"
    description = "ATR-based trend following with dynamic stops"
    preferred_regimes = ["trending_up", "trending_down"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "supertrend", "params": {"period": rng.randint(8, 14), "multiplier": round(rng.uniform(2.0, 4.0), 1)}},
            "momentum": {"logic_type": "macd_hist", "params": {"fast": 12, "slow": 26, "threshold": 0}},
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.3, "regime_bonus": 0.1}},
            "exit": {"logic_type": "trailing_atr", "params": {"sl_mult": 2.0, "tp_mult": 5.0, "trail_mult": 2.0}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down"]}},
        }


class MarketStructure(StrategyFamily):
    name = "market_structure"
    description = "Trade based on higher highs/lower lows structure"
    preferred_regimes = ["trending_up", "trending_down"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "market_structure", "params": {"lookback": rng.randint(15, 30)}},
            "momentum": {"logic_type": "roc", "params": {"period": 10, "threshold": 0}},
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.3, "regime_bonus": 0.15}},
            "exit": {"logic_type": "swing_stops", "params": {"lookback": 10, "tp_mult": 3.0}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down"]}},
        }


class AdaptiveHybrid(StrategyFamily):
    name = "adaptive_hybrid"
    description = "Combine multiple families with regime switching"
    preferred_regimes = ["trending_up", "trending_down", "ranging", "high_volatility"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": rng.choice(TREND_INDICATORS),
            "momentum": rng.choice(MOMENTUM_INDICATORS),
            "volatility": rng.choice(VOLATILITY_FILTERS),
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": round(rng.uniform(0.2, 0.5), 2), "regime_bonus": 0.1}},
            "exit": rng.choice(EXIT_STRATEGIES),
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["trending_up", "trending_down", "ranging", "high_volatility"]}},
        }


class Scalping(StrategyFamily):
    name = "scalping"
    description = "Quick trades on small moves with tight stops"
    preferred_regimes = ["ranging", "high_volatility"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "ema_slope", "params": {"period": rng.randint(5, 12)}},
            "momentum": {"logic_type": "stochastic", "params": {"k_period": 7, "d_period": 3, "upper": 80, "lower": 20}},
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.2, "regime_bonus": 0.1}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": 1.0, "tp_mult": 1.5}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["ranging", "high_volatility", "trending_up", "trending_down"]}},
        }


class VWAPReversion(StrategyFamily):
    name = "vwap_reversion"
    description = "Trade deviations from VWAP"
    preferred_regimes = ["ranging"]

    def template(self, rng: random.Random, regime: str) -> Dict[str, Any]:
        return {
            "family": self.name,
            "trend": {"logic_type": "vwap_trend", "params": {"period": 20}},
            "momentum": {"logic_type": "rsi", "params": {"period": 7, "upper": 70, "lower": 30}},
            "volatility": {"logic_type": "bollinger_width", "params": {"period": 20, "threshold": 0.04}},
            "entry": {"logic_type": "adaptive", "params": {"base_threshold": 0.35, "regime_bonus": 0.15}},
            "exit": {"logic_type": "atr_stops", "params": {"sl_mult": 1.5, "tp_mult": 2.0}},
            "market_regime": {"logic_type": "default", "params": {"allowed_regimes": ["ranging", "trending_up"]}},
        }


# ---- Registry ----

ALL_FAMILIES: List[StrategyFamily] = [
    TrendFollowing(),
    MeanReversion(),
    Breakout(),
    Momentum(),
    VolatilityExpansion(),
    Pullback(),
    RangeTrading(),
    DonchianBreakout(),
    Supertrend(),
    MarketStructure(),
    AdaptiveHybrid(),
    Scalping(),
    VWAPReversion(),
]

FAMILY_MAP: Dict[str, StrategyFamily] = {f.name: f for f in ALL_FAMILIES}

# ---- Regime-weighted selection ----

REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    "trending_up": {
        "trend_following": 3.0, "momentum": 3.0, "pullback": 2.5,
        "breakout": 1.5, "supertrend": 2.5, "donchian_breakout": 2.0,
        "market_structure": 2.0, "adaptive_hybrid": 1.5,
        "mean_reversion": 0.5, "range_trading": 0.3, "scalping": 0.5,
        "vwap_reversion": 0.5, "volatility_expansion": 1.0,
    },
    "trending_down": {
        "trend_following": 3.0, "momentum": 2.5, "pullback": 2.0,
        "breakout": 1.5, "supertrend": 2.5, "donchian_breakout": 2.0,
        "market_structure": 2.0, "adaptive_hybrid": 1.5,
        "mean_reversion": 0.5, "range_trading": 0.3, "scalping": 0.5,
        "vwap_reversion": 0.5, "volatility_expansion": 1.0,
    },
    "ranging": {
        "mean_reversion": 3.0, "range_trading": 3.0, "vwap_reversion": 2.5,
        "scalping": 2.0, "breakout": 1.5, "adaptive_hybrid": 1.5,
        "bollinger_mean_reversion": 2.0,
        "trend_following": 0.3, "momentum": 0.5, "pullback": 0.5,
        "supertrend": 0.3, "donchian_breakout": 1.0,
        "market_structure": 0.5, "volatility_expansion": 1.5,
    },
    "high_volatility": {
        "volatility_expansion": 3.0, "breakout": 2.5, "donchian_breakout": 2.0,
        "scalping": 2.0, "adaptive_hybrid": 2.0, "momentum": 1.5,
        "trend_following": 1.0, "supertrend": 1.5,
        "mean_reversion": 0.5, "range_trading": 0.3, "pullback": 0.5,
        "vwap_reversion": 0.5, "market_structure": 1.0,
    },
}


def select_family(rng: random.Random, regime: str, atlas_hints: Optional[List[str]] = None) -> StrategyFamily:
    """Select a strategy family weighted by regime and Atlas research hints."""
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["ranging"])

    # Boost families mentioned in Atlas research
    if atlas_hints:
        for hint in atlas_hints:
            hint_lower = hint.lower().replace(" ", "_")
            for family_name in weights:
                if hint_lower in family_name or family_name in hint_lower:
                    weights[family_name] = weights.get(family_name, 1.0) * 2.0

    # Build weighted selection
    families_available = [(f, weights.get(f.name, 1.0)) for f in ALL_FAMILIES]
    total = sum(w for _, w in families_available)
    r = rng.uniform(0, total)
    cumulative = 0.0
    for family, weight in families_available:
        cumulative += weight
        if r <= cumulative:
            return family

    return ALL_FAMILIES[-1]  # fallback


def generate_diverse_population(
    rng: random.Random,
    size: int,
    regime: str,
    atlas_hints: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Generate a diverse population ensuring no duplicate families dominate."""
    templates = []
    family_counts: Dict[str, int] = {}
    max_per_family = max(2, size // 4)  # No family should exceed 25% of population

    for _ in range(size):
        family = select_family(rng, regime, atlas_hints)

        # Enforce diversity: if this family is overrepresented, pick another
        attempts = 0
        while family_counts.get(family.name, 0) >= max_per_family and attempts < 5:
            family = select_family(rng, regime, atlas_hints)
            attempts += 1

        template = family.template(rng, regime)
        template["genome_id"] = f"{family.name[:8]}-{uuid.uuid4().hex[:6]}"
        templates.append(template)
        family_counts[family.name] = family_counts.get(family.name, 0) + 1

    return templates
