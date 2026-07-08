"""
Oracle.intelligence.strategy_genome (v2 - FIXED)
=================================================
CRITICAL FIX: genome.call() was calling analyze() on every bar inside the
backtester, which either crashed or returned "unknown" regime (blocking all
trades). Now genome has two methods:
  - call(series)         → for LIVE use (has full series context)
  - decide(closes, highs, lows) → for BACKTESTER use (fast, no analyze())

The backtester uses decide() which skips regime filtering (regime is pre-checked
before the backtest starts). This makes evolution actually produce trades.
"""
from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from intelligence.technicals import sma, ema, rsi, macd, bollinger, atr


# ── Module Definitions ────────────────────────────────────────────────────────

@dataclass
class StrategyModule:
    logic_type: str = "default"
    params: Dict[str, Any] = field(default_factory=dict)

    def mutate(self, rng: random.Random, rate: float = 0.3):
        for k, v in list(self.params.items()):
            if rng.random() < rate:
                if isinstance(v, (int, float)):
                    noise = rng.gauss(0, 0.15)
                    new_val = v * (1 + noise)
                    self.params[k] = int(round(new_val)) if isinstance(v, int) else round(new_val, 3)
                elif isinstance(v, bool):
                    self.params[k] = not v
        if rng.random() < rate * 0.5:
            new_logic = self._get_next_logic(rng)
            if new_logic != self.logic_type:
                self.logic_type = new_logic
                self.params = self._default_params_for(new_logic, rng)

    def _get_next_logic(self, rng: random.Random) -> str:
        return self.logic_type

    def _default_params_for(self, logic_type: str, rng: random.Random) -> Dict[str, Any]:
        return self.params


@dataclass
class MarketRegimeModule(StrategyModule):
    def is_allowed(self, current_regime: str, volatility: float = 0.0) -> bool:
        allowed = self.params.get("allowed_regimes",
                                  ["trending_up", "trending_down", "ranging", "high_volatility"])
        vol_limit = self.params.get("volatility_limit", 1.0)
        return current_regime in allowed and volatility <= vol_limit

    def _get_next_logic(self, rng):
        return rng.choice(["permissive", "strict", "trending_only", "ranging_only"])

    def _default_params_for(self, logic_type, rng):
        if logic_type == "trending_only":
            return {"allowed_regimes": ["trending_up", "trending_down"], "volatility_limit": 0.8}
        elif logic_type == "ranging_only":
            return {"allowed_regimes": ["ranging"], "volatility_limit": 0.5}
        elif logic_type == "strict":
            return {"allowed_regimes": [rng.choice(["trending_up", "trending_down", "ranging"])], "volatility_limit": 0.6}
        return {"allowed_regimes": ["trending_up", "trending_down", "ranging", "high_volatility"], "volatility_limit": 1.0}


@dataclass
class TrendModule(StrategyModule):
    def bias(self, closes: List[float]) -> float:
        if not closes or len(closes) < 5:
            return 0.0
        try:
            if self.logic_type == "sma_crossover":
                fast_p = max(2, int(self.params.get("fast", 20)))
                slow_p = max(fast_p + 1, int(self.params.get("slow", 50)))
                if len(closes) < slow_p:
                    return 0.0
                fast = sma(closes, fast_p)
                slow = sma(closes, slow_p)
                if fast is None or slow is None or slow == 0:
                    return 0.0
                ratio = fast / slow
                return min(1.0, max(-1.0, (ratio - 1.0) * 20))
            elif self.logic_type == "ema_slope":
                period = max(2, int(self.params.get("period", 20)))
                if len(closes) < period + 1:
                    return 0.0
                e = ema(closes, period)
                prev_e = ema(closes[:-1], period)
                if e and prev_e and prev_e != 0:
                    slope = (e - prev_e) / prev_e
                    return min(1.0, max(-1.0, slope * 100))
            elif self.logic_type == "price_above_sma":
                period = max(2, int(self.params.get("period", 200)))
                if len(closes) < period:
                    return 0.0
                s = sma(closes, period)
                if s and s != 0:
                    distance = (closes[-1] - s) / s
                    return min(1.0, max(-1.0, distance * 10))
            elif self.logic_type == "dual_ema":
                fast_p = max(2, int(self.params.get("fast", 8)))
                slow_p = max(fast_p + 1, int(self.params.get("slow", 21)))
                if len(closes) < slow_p:
                    return 0.0
                fast = ema(closes, fast_p)
                slow = ema(closes, slow_p)
                if fast and slow and slow != 0:
                    return min(1.0, max(-1.0, (fast / slow - 1.0) * 30))
            elif self.logic_type == "linear_regression":
                n = max(5, int(self.params.get("period", 20)))
                if len(closes) < n:
                    return 0.0
                segment = closes[-n:]
                x_mean = (n - 1) / 2.0
                y_mean = sum(segment) / n
                num = sum((i - x_mean) * (segment[i] - y_mean) for i in range(n))
                den = sum((i - x_mean) ** 2 for i in range(n))
                if den == 0 or y_mean == 0:
                    return 0.0
                slope = num / den
                return min(1.0, max(-1.0, (slope / y_mean) * 100))
        except Exception:
            return 0.0
        return 0.0

    def _get_next_logic(self, rng):
        return rng.choice(["sma_crossover", "ema_slope", "price_above_sma", "dual_ema", "linear_regression"])

    def _default_params_for(self, logic_type, rng):
        if logic_type == "sma_crossover":
            return {"fast": rng.randint(10, 30), "slow": rng.randint(40, 120)}
        elif logic_type == "ema_slope":
            return {"period": rng.choice([8, 13, 20, 34])}
        elif logic_type == "price_above_sma":
            return {"period": rng.choice([50, 100, 150, 200])}
        elif logic_type == "dual_ema":
            return {"fast": rng.choice([5, 8, 13]), "slow": rng.choice([21, 34, 55])}
        elif logic_type == "linear_regression":
            return {"period": rng.choice([10, 20, 30])}
        return {"fast": 20, "slow": 50}


@dataclass
class MomentumModule(StrategyModule):
    def confirm(self, closes: List[float]) -> float:
        if not closes or len(closes) < 5:
            return 0.0
        try:
            if self.logic_type == "rsi":
                period = max(2, int(self.params.get("period", 14)))
                if len(closes) < period + 1:
                    return 0.0
                r = rsi(closes, period)
                if r is None:
                    return 0.0
                upper = float(self.params.get("upper", 70))
                lower = float(self.params.get("lower", 30))
                mid = (upper + lower) / 2
                if r > upper:
                    return min(1.0, (r - upper) / 30)
                if r < lower:
                    return max(-1.0, (r - lower) / 30)
                diff = upper - mid
                return ((r - mid) / diff * 0.3) if diff != 0 else 0.0
            elif self.logic_type == "macd_hist":
                fast_p = max(2, int(self.params.get("fast", 12)))
                slow_p = max(fast_p + 1, int(self.params.get("slow", 26)))
                if len(closes) < slow_p + 9:
                    return 0.0
                m = macd(closes, fast_p, slow_p)
                if m and closes[-1] != 0:
                    normalized = m["histogram"] / closes[-1] * 1000
                    return min(1.0, max(-1.0, normalized))
            elif self.logic_type == "rate_of_change":
                period = max(2, int(self.params.get("period", 10)))
                if len(closes) < period + 1:
                    return 0.0
                old = closes[-(period + 1)]
                if old == 0:
                    return 0.0
                roc = (closes[-1] - old) / old
                return min(1.0, max(-1.0, roc * 5))
            elif self.logic_type == "stochastic":
                period = max(2, int(self.params.get("period", 14)))
                if len(closes) < period:
                    return 0.0
                window = closes[-period:]
                high = max(window)
                low = min(window)
                if high == low:
                    return 0.0
                k = (closes[-1] - low) / (high - low) * 100
                if k > float(self.params.get("upper", 80)):
                    return min(1.0, (k - 80) / 20)
                if k < float(self.params.get("lower", 20)):
                    return max(-1.0, (k - 20) / 20)
                return 0.0
        except Exception:
            return 0.0
        return 0.0

    def _get_next_logic(self, rng):
        return rng.choice(["rsi", "macd_hist", "rate_of_change", "stochastic"])

    def _default_params_for(self, logic_type, rng):
        if logic_type == "rsi":
            return {"period": rng.choice([7, 9, 14, 21]), "upper": rng.randint(65, 80), "lower": rng.randint(20, 35)}
        elif logic_type == "macd_hist":
            return {"fast": rng.choice([8, 12]), "slow": rng.choice([21, 26]), "threshold": 0}
        elif logic_type == "rate_of_change":
            return {"period": rng.choice([5, 10, 14])}
        elif logic_type == "stochastic":
            return {"period": rng.choice([9, 14]), "upper": 80, "lower": 20}
        return {"period": 14}


@dataclass
class VolatilityModule(StrategyModule):
    def filter(self, closes: List[float], highs: List[float] = None, lows: List[float] = None) -> bool:
        """FIXED: accepts raw lists, doesn't require a series object."""
        if self.logic_type == "default":
            return True
        if self.logic_type == "atr_expansion" and highs and lows and closes:
            period = max(2, int(self.params.get("period", 14)))
            if len(closes) < period + 2:
                return True  # Not enough data = allow trading
            a = atr(highs, lows, closes, period)
            prev_a = atr(highs[:-1], lows[:-1], closes[:-1], period)
            if a and prev_a and prev_a != 0:
                ratio = a / prev_a
                required = float(self.params.get("expansion_ratio", 1.0))
                mode = self.params.get("mode", "expansion_required")
                if mode == "contraction_required":
                    return ratio < required
                return ratio >= required
        return True

    def _get_next_logic(self, rng):
        return rng.choice(["default", "atr_expansion"])

    def _default_params_for(self, logic_type, rng):
        if logic_type == "atr_expansion":
            return {"period": 14, "expansion_ratio": round(rng.uniform(0.8, 1.5), 2), "mode": "expansion_required"}
        return {"period": 14}


@dataclass
class EntryModule(StrategyModule):
    def should_enter(self, vote: float) -> bool:
        threshold = float(self.params.get("threshold", 0.5))
        return abs(vote) >= threshold


@dataclass
class ExitModule(StrategyModule):
    def get_stops(self, price: float, atr_val: float, direction: int) -> Tuple[float, float]:
        sl_mult = float(self.params.get("sl_mult", 2.0))
        tp_mult = float(self.params.get("tp_mult", 3.0))
        return (price - direction * sl_mult * atr_val, price + direction * tp_mult * atr_val)


@dataclass
class RiskModule(StrategyModule):
    def check(self, current_dd: float) -> bool:
        return current_dd < float(self.params.get("max_dd_limit", 0.2))


@dataclass
class PositionModule(StrategyModule):
    def get_size(self, equity: float, risk_per_trade: float, sl_dist: float) -> float:
        if sl_dist == 0:
            return 0.0
        return (equity * risk_per_trade) / sl_dist


@dataclass
class TradeManagementModule(StrategyModule):
    def update_stop(self, current_price, current_stop, direction, atr_val):
        if self.logic_type == "trailing_stop":
            trail_mult = float(self.params.get("trail_mult", 2.0))
            new_stop = current_price - direction * trail_mult * atr_val
            if direction == 1:
                return max(current_stop, new_stop)
            else:
                return min(current_stop, new_stop)
        return current_stop

    def _get_next_logic(self, rng):
        return rng.choice(["default", "trailing_stop"])


@dataclass
class ExecutionModule(StrategyModule):
    pass


# ── Strategy Genome ───────────────────────────────────────────────────────────

@dataclass
class StrategyGenome:
    """Complete Strategy DNA with 10 modules + evolvable vote weights."""
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

    trend_weight: float = 0.6
    momentum_weight: float = 0.4

    generation: int = 0
    parents: List[str] = field(default_factory=list)
    fitness: float = 0.0
    backtests: int = 0
    best_return: float = 0.0
    best_sharpe: float = 0.0

    def vote(self, closes: List[float]) -> float:
        """Compute directional vote from trend + momentum. Takes raw closes list."""
        t_bias = self.trend.bias(closes)
        m_conf = self.momentum.confirm(closes)
        total_w = self.trend_weight + self.momentum_weight
        if total_w == 0:
            return 0.0
        return (t_bias * self.trend_weight + m_conf * self.momentum_weight) / total_w

    def decide(self, closes: List[float], highs: List[float], lows: List[float]) -> str:
        """
        FAST decision for backtester. NO analyze() call. NO regime check.
        
        This is what the backtester calls on every bar. It only uses the
        trend + momentum + volatility + entry modules. Regime filtering
        happens BEFORE the backtest starts (in evolution.py).
        """
        # Volatility filter (uses raw lists, not a series object)
        if not self.volatility.filter(closes, highs, lows):
            return "hold"

        # Vote (trend + momentum)
        v = self.vote(closes)

        # Entry threshold
        if self.entry.should_enter(v):
            return "buy" if v > 0 else "sell"
        return "hold"

    def call(self, series) -> str:
        """
        Full decision for LIVE use (with regime filtering).
        Uses series object that has .closes, .highs, .lows.
        """
        closes = series.closes if hasattr(series, 'closes') else []
        highs = series.highs if hasattr(series, 'highs') else []
        lows = series.lows if hasattr(series, 'lows') else []

        if not closes or len(closes) < 10:
            return "hold"

        # In live mode, we CAN check regime (we have full context)
        # But for safety, if analyze fails, still allow the trade
        try:
            from intelligence.technicals import analyze as _an
            t = _an(series)
            regime = (t.get("regime") or {}).get("regime", "unknown")
            vol = (t.get("regime") or {}).get("volatility", 0.0)
            if not self.market_regime.is_allowed(regime, vol):
                return "hold"
        except Exception:
            pass  # If regime check fails, proceed without it

        return self.decide(closes, highs, lows)

    def fingerprint(self) -> str:
        return (f"{self.trend.logic_type}|{self.momentum.logic_type}|"
                f"{self.volatility.logic_type}|{self.trade_management.logic_type}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "genome_id": self.genome_id,
            "generation": self.generation,
            "parents": self.parents,
            "fitness": self.fitness,
            "backtests": self.backtests,
            "best_return": self.best_return,
            "best_sharpe": self.best_sharpe,
            "trend_weight": self.trend_weight,
            "momentum_weight": self.momentum_weight,
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
            },
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> StrategyGenome:
        mods = d.get("modules", {})
        def _mod(klass, key):
            data = mods.get(key, {})
            return klass(logic_type=data.get("logic_type", "default"), params=data.get("params", {}))
        return cls(
            genome_id=d.get("genome_id", f"strat-{uuid.uuid4().hex[:8]}"),
            generation=d.get("generation", 0),
            parents=d.get("parents", []),
            fitness=d.get("fitness", 0.0),
            backtests=d.get("backtests", 0),
            best_return=d.get("best_return", 0.0),
            best_sharpe=d.get("best_sharpe", 0.0),
            trend_weight=d.get("trend_weight", 0.6),
            momentum_weight=d.get("momentum_weight", 0.4),
            market_regime=_mod(MarketRegimeModule, "market_regime"),
            trend=_mod(TrendModule, "trend"),
            momentum=_mod(MomentumModule, "momentum"),
            volatility=_mod(VolatilityModule, "volatility"),
            entry=_mod(EntryModule, "entry"),
            exit=_mod(ExitModule, "exit"),
            risk=_mod(RiskModule, "risk"),
            position=_mod(PositionModule, "position"),
            trade_management=_mod(TradeManagementModule, "trade_management"),
            execution=_mod(ExecutionModule, "execution"),
        )


# ── Genetic Operators ─────────────────────────────────────────────────────────

_DIVERSE_TEMPLATES = [
    # 1. Trend following: EMA + MACD
    lambda rng: _build(rng, "trend",
        trend=("ema_slope", {"period": rng.choice([13, 20, 34])}),
        momentum=("macd_hist", {"fast": 12, "slow": 26, "threshold": 0}),
        vol=("default", {}),
        entry={"threshold": round(rng.uniform(0.2, 0.4), 2)},
        exit={"sl_mult": round(rng.uniform(1.5, 2.5), 1), "tp_mult": round(rng.uniform(3.0, 6.0), 1)},
        weights=(rng.uniform(0.5, 0.7), rng.uniform(0.3, 0.5))),
    # 2. Mean reversion: SMA200 + RSI extremes
    lambda rng: _build(rng, "revert",
        trend=("price_above_sma", {"period": rng.choice([100, 150, 200])}),
        momentum=("rsi", {"period": rng.choice([7, 9, 14]), "upper": rng.randint(75, 85), "lower": rng.randint(15, 25)}),
        vol=("default", {}),
        entry={"threshold": round(rng.uniform(0.3, 0.5), 2)},
        exit={"sl_mult": round(rng.uniform(1.0, 2.0), 1), "tp_mult": round(rng.uniform(1.5, 3.0), 1)},
        weights=(rng.uniform(0.3, 0.5), rng.uniform(0.5, 0.7))),
    # 3. Breakout: Dual EMA + ROC
    lambda rng: _build(rng, "break",
        trend=("dual_ema", {"fast": rng.choice([5, 8]), "slow": rng.choice([21, 34])}),
        momentum=("rate_of_change", {"period": rng.choice([5, 10, 14])}),
        vol=("atr_expansion", {"period": 14, "expansion_ratio": round(rng.uniform(1.1, 1.5), 2), "mode": "expansion_required"}),
        entry={"threshold": round(rng.uniform(0.3, 0.5), 2)},
        exit={"sl_mult": round(rng.uniform(1.5, 2.5), 1), "tp_mult": round(rng.uniform(4.0, 8.0), 1)},
        weights=(rng.uniform(0.4, 0.6), rng.uniform(0.4, 0.6))),
    # 4. Momentum: SMA crossover + Stochastic
    lambda rng: _build(rng, "mom",
        trend=("sma_crossover", {"fast": rng.randint(10, 20), "slow": rng.randint(40, 60)}),
        momentum=("stochastic", {"period": rng.choice([9, 14]), "upper": 80, "lower": 20}),
        vol=("default", {}),
        entry={"threshold": round(rng.uniform(0.2, 0.4), 2)},
        exit={"sl_mult": round(rng.uniform(2.0, 3.0), 1), "tp_mult": round(rng.uniform(3.0, 5.0), 1)},
        weights=(rng.uniform(0.5, 0.7), rng.uniform(0.3, 0.5))),
    # 5. Regression trend + MACD
    lambda rng: _build(rng, "regr",
        trend=("linear_regression", {"period": rng.choice([10, 20, 30])}),
        momentum=("macd_hist", {"fast": 8, "slow": 21, "threshold": 0}),
        vol=("default", {}),
        entry={"threshold": round(rng.uniform(0.2, 0.4), 2)},
        exit={"sl_mult": round(rng.uniform(1.5, 2.5), 1), "tp_mult": round(rng.uniform(3.0, 5.0), 1)},
        weights=(rng.uniform(0.5, 0.7), rng.uniform(0.3, 0.5))),
    # 6. Fast scalp: EMA slope + stochastic
    lambda rng: _build(rng, "scalp",
        trend=("ema_slope", {"period": rng.choice([5, 8, 10])}),
        momentum=("stochastic", {"period": rng.choice([5, 7]), "upper": 85, "lower": 15}),
        vol=("default", {}),
        entry={"threshold": round(rng.uniform(0.3, 0.5), 2)},
        exit={"sl_mult": round(rng.uniform(0.8, 1.5), 1), "tp_mult": round(rng.uniform(1.0, 2.0), 1)},
        weights=(rng.uniform(0.4, 0.6), rng.uniform(0.4, 0.6))),
    # 7. Slow positional: long SMA + RSI
    lambda rng: _build(rng, "pos",
        trend=("sma_crossover", {"fast": rng.choice([50, 100]), "slow": rng.choice([150, 200])}),
        momentum=("rsi", {"period": 21, "upper": 70, "lower": 30}),
        vol=("default", {}),
        entry={"threshold": round(rng.uniform(0.15, 0.3), 2)},
        exit={"sl_mult": round(rng.uniform(3.0, 5.0), 1), "tp_mult": round(rng.uniform(6.0, 10.0), 1)},
        weights=(rng.uniform(0.6, 0.8), rng.uniform(0.2, 0.4))),
]


def _build(rng, tag, trend, momentum, vol, entry, exit, weights):
    g = StrategyGenome(genome_id=f"rand-{tag}-{uuid.uuid4().hex[:4]}")
    g.trend = TrendModule(logic_type=trend[0], params=trend[1])
    g.momentum = MomentumModule(logic_type=momentum[0], params=momentum[1])
    g.volatility = VolatilityModule(logic_type=vol[0], params=vol[1])
    g.market_regime = MarketRegimeModule(logic_type="permissive",
        params={"allowed_regimes": ["trending_up", "trending_down", "ranging", "high_volatility"], "volatility_limit": 1.0})
    g.entry = EntryModule(params=entry)
    g.exit = ExitModule(params=exit)
    g.risk = RiskModule(params={"max_dd_limit": 0.15})
    g.position = PositionModule(params={"base_risk": 0.01})
    g.trade_management = TradeManagementModule(logic_type="default", params={})
    g.execution = ExecutionModule(params={})
    g.trend_weight = round(weights[0], 3)
    g.momentum_weight = round(weights[1], 3)
    return g


def random_diverse_strategy(rng: random.Random) -> StrategyGenome:
    """Generate a structurally diverse genome from 7 distinct templates."""
    return rng.choice(_DIVERSE_TEMPLATES)(rng)


def random_strategy(rng: random.Random) -> StrategyGenome:
    """Legacy interface → delegates to diverse version."""
    return random_diverse_strategy(rng)


def mutate_strategy(genome: StrategyGenome, rng: random.Random, rate: float = 0.3) -> StrategyGenome:
    child = StrategyGenome.from_dict(genome.to_dict())
    child.genome_id = f"mut-{uuid.uuid4().hex[:6]}"
    child.generation += 1
    child.parents = [genome.genome_id]
    child.market_regime.mutate(rng, rate)
    child.trend.mutate(rng, rate)
    child.momentum.mutate(rng, rate)
    child.volatility.mutate(rng, rate)
    child.entry.mutate(rng, rate)
    child.exit.mutate(rng, rate)
    child.risk.mutate(rng, rate)
    child.trade_management.mutate(rng, rate)
    if rng.random() < rate:
        child.trend_weight = round(max(0.1, min(0.9, child.trend_weight + rng.gauss(0, 0.1))), 3)
    if rng.random() < rate:
        child.momentum_weight = round(max(0.1, min(0.9, child.momentum_weight + rng.gauss(0, 0.1))), 3)
    return child


def crossover_strategy(a: StrategyGenome, b: StrategyGenome, rng: random.Random) -> StrategyGenome:
    child = StrategyGenome.from_dict(a.to_dict())
    child.genome_id = f"cross-{uuid.uuid4().hex[:6]}"
    child.generation = max(a.generation, b.generation) + 1
    child.parents = [a.genome_id, b.genome_id]
    b_dict = b.to_dict()["modules"]
    module_map = {
        "market_regime": MarketRegimeModule, "trend": TrendModule,
        "momentum": MomentumModule, "volatility": VolatilityModule,
        "entry": EntryModule, "exit": ExitModule, "risk": RiskModule,
        "position": PositionModule, "trade_management": TradeManagementModule,
        "execution": ExecutionModule,
    }
    for m_name, m_class in module_map.items():
        if rng.random() < 0.5:
            m_data = b_dict[m_name]
            setattr(child, m_name, m_class(logic_type=m_data["logic_type"], params=m_data["params"].copy()))
    alpha = rng.random()
    child.trend_weight = round(a.trend_weight * alpha + b.trend_weight * (1 - alpha), 3)
    child.momentum_weight = round(a.momentum_weight * alpha + b.momentum_weight * (1 - alpha), 3)
    return child
