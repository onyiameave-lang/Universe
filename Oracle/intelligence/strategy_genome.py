"""
Oracle.intelligence.strategy_genome
==================================
Constitutional Strategy Architecture. (Book I Part IV Article XIII Evolution; 
Book VI Capital Sovereignty.)

Rearchitected from a flat "bag of indicators" into a modular, research-driven 
trading laboratory. Strategies are now composed of 10 constitutional modules:
Market Regime, Trend, Momentum, Volatility, Entry, Exit, Risk, Position, 
Trade Management, and Execution.

Mutation occurs INSIDE these modules, preserving the structural integrity 
of the trading strategy while allowing parameter and logic evolution.
"""
from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from intelligence.technicals import (sma, ema, rsi, macd, bollinger, atr, 
                                     returns_stats)

# ---- Module Definitions ----

@dataclass
class StrategyModule:
    """Base class for constitutional modules."""
    logic_type: str = "default"
    params: Dict[str, Any] = field(default_factory=dict)
    
    def mutate(self, rng: random.Random, rate: float = 0.3):
        """Mutates parameters and occasionally swaps logic_type."""
        for k, v in self.params.items():
            if rng.random() < rate:
                if isinstance(v, (int, float)):
                    # Perturb numerical values
                    self.params[k] = round(v * rng.uniform(0.7, 1.3), 3)
                elif isinstance(v, bool):
                    self.params[k] = not v
        
        # Swapping logic_type is a structural mutation
        if rng.random() < rate * 0.2:
            self.logic_type = self._get_next_logic(rng)

    def _get_next_logic(self, rng: random.Random) -> str:
        # Override in subclasses
        return self.logic_type

@dataclass
class MarketRegimeModule(StrategyModule):
    """Filters activity based on market regime (Trending, Ranging, etc)."""
    def is_allowed(self, current_regime: str, volatility: float) -> bool:
        allowed = self.params.get("allowed_regimes", ["trending_up", "trending_down", "ranging", "high_volatility"])
        vol_limit = self.params.get("volatility_limit", 1.0)
        return current_regime in allowed and volatility <= vol_limit

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["default", "strict", "permissive"])

@dataclass
class TrendModule(StrategyModule):
    """Determines the primary directional bias."""
    def bias(self, closes: List[float]) -> float:
        if self.logic_type == "sma_crossover":
            fast = sma(closes, int(self.params.get("fast", 20))) or closes[-1]
            slow = sma(closes, int(self.params.get("slow", 50))) or closes[-1]
            return 1.0 if fast > slow else -1.0
        elif self.logic_type == "ema_slope":
            period = int(self.params.get("period", 20))
            e = ema(closes, period)
            prev_e = ema(closes[:-1], period)
            if e and prev_e:
                return 1.0 if e > prev_e else -1.0
        elif self.logic_type == "price_above_sma":
            s = sma(closes, int(self.params.get("period", 200))) or closes[-1]
            return 1.0 if closes[-1] > s else -1.0
        return 0.0

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["sma_crossover", "ema_slope", "price_above_sma"])

@dataclass
class MomentumModule(StrategyModule):
    """Confirms the velocity of the move."""
    def confirm(self, closes: List[float]) -> float:
        if self.logic_type == "rsi":
            r = rsi(closes, int(self.params.get("period", 14))) or 50
            if r > self.params.get("upper", 70): return 1.0
            if r < self.params.get("lower", 30): return -1.0
        elif self.logic_type == "macd_hist":
            m = macd(closes, int(self.params.get("fast", 12)), int(self.params.get("slow", 26)))
            if m:
                return 1.0 if m["histogram"] > self.params.get("threshold", 0) else -1.0
        return 0.0

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["rsi", "macd_hist"])

@dataclass
class VolatilityModule(StrategyModule):
    """Filters out noise or prevents trading in dangerous volatility."""
    def filter(self, series) -> bool:
        if self.logic_type == "atr_expansion":
            a = atr(series.highs, series.lows, series.closes, int(self.params.get("period", 14)))
            prev_a = atr(series.highs[:-1], series.lows[:-1], series.closes[:-1], int(self.params.get("period", 14)))
            if a and prev_a:
                return a > prev_a * self.params.get("expansion_ratio", 1.0)
        return True

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["atr_expansion", "default"])

@dataclass
class EntryModule(StrategyModule):
    """Defines the final execution trigger."""
    def should_enter(self, vote: float) -> bool:
        threshold = self.params.get("threshold", 0.5)
        return abs(vote) >= threshold

@dataclass
class ExitModule(StrategyModule):
    """Defines take-profit and stop-loss logic."""
    def get_stops(self, price: float, atr_val: float, direction: int) -> Tuple[float, float]:
        sl_mult = self.params.get("sl_mult", 2.0)
        tp_mult = self.params.get("tp_mult", 3.0)
        return (price - direction * sl_mult * atr_val, price + direction * tp_mult * atr_val)

@dataclass
class RiskModule(StrategyModule):
    """Global strategy risk constraints."""
    def check(self, current_dd: float) -> bool:
        return current_dd < self.params.get("max_dd_limit", 0.2)

@dataclass
class PositionModule(StrategyModule):
    """Calculates position sizing."""
    def get_size(self, equity: float, risk_per_trade: float, sl_dist: float) -> float:
        if sl_dist == 0: return 0.0
        return (equity * risk_per_trade) / sl_dist

@dataclass
class TradeManagementModule(StrategyModule):
    """Manages active trades (trailing stops, etc)."""
    def update_stop(self, current_price: float, current_stop: float, direction: int, atr_val: float) -> float:
        """Implements a basic trailing stop based on ATR."""
        if self.logic_type == "trailing_stop":
            trail_mult = self.params.get("trail_mult", 2.0)
            new_stop = current_price - direction * trail_mult * atr_val
            if direction == 1: # Long
                return max(current_stop, new_stop)
            else: # Short
                return min(current_stop, new_stop)
        return current_stop

    def _get_next_logic(self, rng: random.Random) -> str:
        return rng.choice(["default", "trailing_stop"])

@dataclass
class ExecutionModule(StrategyModule):
    """Parameters for order execution (slippage, latency)."""
    pass

@dataclass
class StrategyGenome:
    """The complete Strategy DNA, composed of 10 Constitutional Modules."""
    genome_id: str = field(default_factory=lambda: f"strat-{uuid.uuid4().hex[:8]}")
    
    # Modules
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
        """Determines directional intent by combining Module outputs."""
        closes = series.closes
        # 1. Trend Bias (-1 to 1)
        t_bias = self.trend.bias(closes)
        
        # 2. Momentum Confirmation (-1 to 1)
        m_conf = self.momentum.confirm(closes)
        
        # Combined intent
        return (t_bias * 0.7 + m_conf * 0.3)

    def call(self, series) -> str:
        """Final decision: buy, sell, or hold, subject to regime and volatility filters."""
        # 1. Volatility Filter
        if not self.volatility.filter(series):
            return "hold"

        # 2. Market Regime Filter
        from intelligence.technicals import analyze as _an
        t = _an(series)
        regime = (t.get("regime") or {}).get("regime", "unknown")
        vol = (t.get("regime") or {}).get("volatility", 0.0)
        
        if not self.market_regime.is_allowed(regime, vol):
            return "hold"
        
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
        def _get_mod(cls, key):
            data = mods.get(key, {})
            return cls(logic_type=data.get("logic_type", "default"), params=data.get("params", {}))

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
    """Generates a random but structurally sound StrategyGenome."""
    # Seed with reasonable defaults
    g = StrategyGenome()
    g.trend = TrendModule(logic_type=rng.choice(["sma_crossover", "ema_slope"]), 
                          params={"fast": rng.randint(10, 30), "slow": rng.randint(40, 100)})
    g.momentum = MomentumModule(logic_type="rsi", params={"period": 14, "upper": 70, "lower": 30})
    g.entry = EntryModule(params={"threshold": round(rng.uniform(0.3, 0.7), 2)})
    g.exit = ExitModule(params={"sl_mult": round(rng.uniform(1.5, 3.0), 1), "tp_mult": round(rng.uniform(2.0, 5.0), 1)})
    return g

def mutate_strategy(genome: StrategyGenome, rng: random.Random, rate: float = 0.3) -> StrategyGenome:
    """Mutates the StrategyGenome by mutating individual modules."""
    child = StrategyGenome.from_dict(genome.to_dict())
    child.generation += 1
    child.parents = [genome.genome_id]
    
    # Mutate each module
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
    """Modular crossover: swaps entire modules between parents."""
    child = StrategyGenome.from_dict(a.to_dict())
    child.generation = max(a.generation, b.generation) + 1
    child.parents = [a.genome_id, b.genome_id]
    
    # For each module, pick from A or B
    modules = ["market_regime", "trend", "momentum", "volatility", "entry", "exit", 
               "risk", "position", "trade_management", "execution"]
    
    b_dict = b.to_dict()["modules"]
    for m_name in modules:
        if rng.random() < 0.5:
            # Reconstruct module from B's data to ensure deep copy
            m_data = b_dict[m_name]
            # Map module name to its class
            m_class = {
                "market_regime": MarketRegimeModule, "trend": TrendModule,
                "momentum": MomentumModule, "volatility": VolatilityModule,
                "entry": EntryModule, "exit": ExitModule, "risk": RiskModule,
                "position": PositionModule, "trade_management": TradeManagementModule,
                "execution": ExecutionModule
            }[m_name]
            setattr(child, m_name, m_class(logic_type=m_data["logic_type"], params=m_data["params"].copy()))
            
    return child
