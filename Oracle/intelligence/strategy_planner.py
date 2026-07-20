"""
Oracle.intelligence.strategy_planner (v2 - Rearchitected)
=========================================================
The bridge between Atlas research and executable strategy genomes.

AUDIT FINDINGS ADDRESSED:
- Old planner used keyword matching and IGNORED actual research content
- Old planner produced IDENTICAL templates regardless of input
- Research parameter `research` was passed but never read

NEW ARCHITECTURE:
Atlas Research → Extract Insights → Map to Module Configs → Build Diverse Genomes

The planner now:
1. Parses Atlas output for specific indicators, timeframes, risk models
2. Maps extracted concepts to module logic_types and parameters
3. Generates DIVERSE candidates (not templates) with randomized variants
4. Each planned genome is structurally different (not just param-different)
5. Populates ALL 10 modules (not just trend + momentum)
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from intelligence.strategy_genome import (
        StrategyGenome, MarketRegimeModule, TrendModule, MomentumModule,
        VolatilityModule, EntryModule, ExitModule, RiskModule, PositionModule,
        TradeManagementModule, ExecutionModule
    )
except ImportError:
    from Oracle.intelligence.strategy_genome import (  # type: ignore
        StrategyGenome, MarketRegimeModule, TrendModule, MomentumModule,
        VolatilityModule, EntryModule, ExitModule, RiskModule, PositionModule,
        TradeManagementModule, ExecutionModule
    )

log = logging.getLogger("oracle.planner")


# ── Concept Extraction ────────────────────────────────────────────────────────

class ConceptExtractor:
    """Extracts actionable trading concepts from Atlas research text."""
    
    # Indicator families and their aliases (what Atlas might say → what we can build)
    INDICATOR_MAP = {
        # Trend indicators
        "moving average": "sma", "sma": "sma", "ema": "ema",
        "exponential moving average": "ema", "simple moving average": "sma",
        "crossover": "sma_crossover", "golden cross": "sma_crossover",
        "death cross": "sma_crossover", "price above": "price_above_sma",
        "200-day": "price_above_sma", "200 day": "price_above_sma",
        # Momentum indicators
        "rsi": "rsi", "relative strength": "rsi", "overbought": "rsi",
        "oversold": "rsi", "macd": "macd_hist", "histogram": "macd_hist",
        "momentum": "macd_hist", "divergence": "macd_hist",
        # Volatility indicators
        "atr": "atr_expansion", "average true range": "atr_expansion",
        "bollinger": "bollinger", "volatility expansion": "atr_expansion",
        "squeeze": "atr_expansion", "compression": "atr_expansion",
        # Risk concepts
        "drawdown": "max_dd", "risk per trade": "fixed_fractional",
        "position sizing": "volatility_adjusted", "kelly": "kelly_criterion",
        "fixed fractional": "fixed_fractional",
        # Entry/Exit patterns
        "pullback": "pullback", "breakout": "breakout",
        "failed rally": "failed_rally", "mean reversion": "mean_reversion",
        "reversal": "mean_reversion", "contrarian": "mean_reversion",
        "trend continuation": "trend_continuation",
        "trailing stop": "trailing_stop", "trail": "trailing_stop",
    }
    
    # Strategy family detection (broader concepts)
    FAMILY_PATTERNS = {
        "trend_following": ["trend follow", "momentum", "trend continuation", "moving average cross",
                          "directional", "trend trading", "breakout continuation"],
        "mean_reversion": ["mean revert", "reversion", "contrarian", "overbought", "oversold",
                          "range bound", "bollinger", "reversal"],
        "breakout": ["breakout", "range expansion", "volatility breakout", "compression",
                    "squeeze", "range break", "channel break"],
        "momentum": ["momentum", "relative strength", "rate of change", "acceleration"],
        "volatility_trade": ["volatility", "straddle", "vix", "vol expansion", "vol compression"],
        "carry_trade": ["carry", "interest rate", "yield", "swap"],
        "failed_rally": ["failed rally", "bear flag", "lower high", "rejection"],
        "scalping": ["scalp", "short term", "high frequency", "tick"],
        "swing": ["swing", "multi-day", "intermediate", "weekly"],
        "positional": ["position", "long term", "monthly", "macro"],
    }
    
    # Timeframe extraction
    TIMEFRAME_MAP = {
        "1 minute": 1, "5 minute": 5, "15 minute": 15, "1 hour": 60,
        "4 hour": 240, "daily": 1440, "weekly": 10080,
        "short term": 15, "medium term": 240, "long term": 1440,
        "intraday": 60, "swing": 1440, "scalp": 5,
    }
    
    def extract(self, research: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all actionable concepts from an Atlas research report."""
        text = self._flatten_to_text(research).lower()
        
        return {
            "families": self._extract_families(text),
            "indicators": self._extract_indicators(text),
            "timeframes": self._extract_timeframes(text),
            "risk_params": self._extract_risk_params(text),
            "entry_patterns": self._extract_patterns(text, "entry"),
            "exit_patterns": self._extract_patterns(text, "exit"),
            "regime_filters": self._extract_regimes(text),
            "numerical_params": self._extract_numbers(text),
            "raw_text": text[:2000],
        }
    
    def _flatten_to_text(self, obj: Any) -> str:
        """Recursively flatten any structure to searchable text."""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            parts = []
            for k, v in obj.items():
                parts.append(str(k))
                parts.append(self._flatten_to_text(v))
            return " ".join(parts)
        if isinstance(obj, (list, tuple)):
            return " ".join(self._flatten_to_text(item) for item in obj)
        return str(obj)
    
    def _extract_families(self, text: str) -> List[str]:
        found = []
        for family, patterns in self.FAMILY_PATTERNS.items():
            for pattern in patterns:
                if pattern in text:
                    found.append(family)
                    break
        return list(set(found)) or ["trend_following"]  # Always return at least one
    
    def _extract_indicators(self, text: str) -> Dict[str, List[str]]:
        """Group detected indicators by role."""
        trend, momentum, volatility = [], [], []
        for phrase, indicator in self.INDICATOR_MAP.items():
            if phrase in text:
                if indicator in ("sma", "ema", "sma_crossover", "price_above_sma"):
                    trend.append(indicator)
                elif indicator in ("rsi", "macd_hist"):
                    momentum.append(indicator)
                elif indicator in ("atr_expansion", "bollinger"):
                    volatility.append(indicator)
        return {
            "trend": list(set(trend)) or ["sma_crossover"],
            "momentum": list(set(momentum)) or ["rsi"],
            "volatility": list(set(volatility)) or ["atr_expansion"],
        }
    
    def _extract_timeframes(self, text: str) -> List[int]:
        found = []
        for phrase, minutes in self.TIMEFRAME_MAP.items():
            if phrase in text:
                found.append(minutes)
        return list(set(found)) or [60, 240]
    
    def _extract_risk_params(self, text: str) -> Dict[str, Any]:
        params = {}
        if "1%" in text or "one percent" in text:
            params["risk_per_trade"] = 0.01
        elif "2%" in text or "two percent" in text:
            params["risk_per_trade"] = 0.02
        if "trailing" in text:
            params["trailing_stop"] = True
        if any(w in text for w in ("tight stop", "close stop")):
            params["sl_mult_range"] = (1.0, 2.0)
        elif any(w in text for w in ("wide stop", "loose stop")):
            params["sl_mult_range"] = (2.5, 4.0)
        return params
    
    def _extract_patterns(self, text: str, context: str) -> List[str]:
        entry_words = ["pullback", "breakout", "crossover", "reversal", "bounce",
                      "rejection", "exhaustion", "continuation", "retest"]
        exit_words = ["trailing", "target", "time-based", "reversal signal",
                     "fixed ratio", "atr multiple", "breakeven"]
        words = entry_words if context == "entry" else exit_words
        return [w for w in words if w in text]
    
    def _extract_regimes(self, text: str) -> List[str]:
        regimes = []
        if any(w in text for w in ("bull", "uptrend", "trending up")):
            regimes.append("trending_up")
        if any(w in text for w in ("bear", "downtrend", "trending down")):
            regimes.append("trending_down")
        if any(w in text for w in ("range", "sideways", "consolidat")):
            regimes.append("ranging")
        if any(w in text for w in ("volatile", "high vol", "turbulent")):
            regimes.append("high_volatility")
        return regimes or ["trending_up", "trending_down", "ranging"]
    
    def _extract_numbers(self, text: str) -> Dict[str, Any]:
        """Extract specific numerical parameters mentioned in research."""
        import re
        params = {}
        # RSI levels
        rsi_match = re.findall(r"rsi\s*(?:above|over|>)\s*(\d+)", text)
        if rsi_match:
            params["rsi_upper"] = int(rsi_match[0])
        rsi_match = re.findall(r"rsi\s*(?:below|under|<)\s*(\d+)", text)
        if rsi_match:
            params["rsi_lower"] = int(rsi_match[0])
        # SMA periods
        sma_match = re.findall(r"(\d+)\s*(?:period|day|bar)\s*(?:sma|ema|moving average)", text)
        for m in sma_match:
            val = int(m)
            if val < 30:
                params.setdefault("fast_periods", []).append(val)
            else:
                params.setdefault("slow_periods", []).append(val)
        # ATR multipliers
        atr_match = re.findall(r"(\d+\.?\d*)\s*(?:x|times|×)\s*atr", text)
        if atr_match:
            params["atr_multiplier"] = float(atr_match[0])
        return params


# ── Genome Builder ────────────────────────────────────────────────────────────

class GenomeBuilder:
    """Constructs fully-populated strategy genomes from extracted concepts."""
    
    def __init__(self, rng: random.Random = None):
        self.rng = rng or random.Random()
    
    def build_from_concepts(self, concepts: Dict[str, Any], symbol: str, 
                           regime: str, family: str) -> StrategyGenome:
        """Build a complete genome with ALL modules populated."""
        g = StrategyGenome(genome_id=f"plan-{family[:6]}-{symbol}-{uuid.uuid4().hex[:4]}")
        
        indicators = concepts.get("indicators", {})
        risk_params = concepts.get("risk_params", {})
        numerical = concepts.get("numerical_params", {})
        regimes = concepts.get("regime_filters", [regime])
        
        # 1. Market Regime Module (always populated)
        g.market_regime = self._build_regime_module(family, regimes)
        
        # 2. Trend Module (from extracted indicators, with variation)
        g.trend = self._build_trend_module(family, indicators, numerical)
        
        # 3. Momentum Module (from extracted indicators)
        g.momentum = self._build_momentum_module(family, indicators, numerical)
        
        # 4. Volatility Module (always active, family-appropriate)
        g.volatility = self._build_volatility_module(family, indicators)
        
        # 5. Entry Module (from family + extracted patterns)
        g.entry = self._build_entry_module(family, concepts.get("entry_patterns", []))
        
        # 6. Exit Module (from risk params + extracted patterns)
        g.exit = self._build_exit_module(family, risk_params)
        
        # 7. Risk Module (from extracted risk params)
        g.risk = self._build_risk_module(family, risk_params)
        
        # 8. Position Module (family-appropriate sizing)
        g.position = self._build_position_module(family, risk_params)
        
        # 9. Trade Management (trailing stops, etc.)
        g.trade_management = self._build_trade_management(family, risk_params)
        
        # 10. Execution Module
        g.execution = ExecutionModule(params={"slippage_pips": self.rng.uniform(0.5, 2.0)})
        
        return g
    
    def _build_regime_module(self, family: str, regimes: List[str]) -> MarketRegimeModule:
        regime_map = {
            "trend_following": ["trending_up", "trending_down"],
            "mean_reversion": ["ranging"],
            "breakout": ["ranging", "high_volatility"],
            "momentum": ["trending_up", "trending_down"],
            "failed_rally": ["trending_down"],
            "volatility_trade": ["high_volatility"],
            "scalping": ["ranging", "trending_up", "trending_down"],
            "swing": ["trending_up", "trending_down", "ranging"],
            "positional": ["trending_up", "trending_down"],
        }
        allowed = regime_map.get(family, regimes)
        vol_limit = 0.5 if family in ("mean_reversion", "scalping") else 1.0
        return MarketRegimeModule(
            logic_type="strict" if len(allowed) <= 2 else "permissive",
            params={"allowed_regimes": allowed, "volatility_limit": vol_limit}
        )
    
    def _build_trend_module(self, family: str, indicators: Dict, numerical: Dict) -> TrendModule:
        trend_indicators = indicators.get("trend", ["sma_crossover"])
        logic = self.rng.choice(trend_indicators)
        
        fast_options = numerical.get("fast_periods", [10, 15, 20, 25])
        slow_options = numerical.get("slow_periods", [50, 100, 150, 200])
        
        if family == "mean_reversion":
            # Reversion uses trend as a FILTER (trade against short-term trend)
            logic = "price_above_sma"
            return TrendModule(logic_type=logic, params={
                "period": self.rng.choice(slow_options),
                "invert": True,  # Signal against the trend
            })
        elif family == "positional":
            return TrendModule(logic_type="sma_crossover", params={
                "fast": self.rng.choice([50, 100]),
                "slow": self.rng.choice([150, 200, 250]),
            })
        elif family == "scalping":
            return TrendModule(logic_type="ema_slope", params={
                "period": self.rng.choice([5, 8, 10, 13]),
            })
        else:
            return TrendModule(logic_type=logic, params={
                "fast": self.rng.choice(fast_options),
                "slow": self.rng.choice(slow_options),
            })
    
    def _build_momentum_module(self, family: str, indicators: Dict, numerical: Dict) -> MomentumModule:
        mom_indicators = indicators.get("momentum", ["rsi", "macd_hist"])
        logic = self.rng.choice(mom_indicators)
        
        if logic == "rsi":
            upper = numerical.get("rsi_upper", self.rng.randint(65, 80))
            lower = numerical.get("rsi_lower", self.rng.randint(20, 35))
            period = self.rng.choice([7, 9, 14, 21])
            
            if family == "mean_reversion":
                # Extreme RSI for reversion entries
                upper = self.rng.randint(75, 85)
                lower = self.rng.randint(15, 25)
            
            return MomentumModule(logic_type="rsi", params={
                "period": period, "upper": upper, "lower": lower
            })
        else:
            return MomentumModule(logic_type="macd_hist", params={
                "fast": self.rng.choice([8, 12, 16]),
                "slow": self.rng.choice([21, 26, 30]),
                "threshold": round(self.rng.uniform(-0.001, 0.001), 4),
            })
    
    def _build_volatility_module(self, family: str, indicators: Dict) -> VolatilityModule:
        if family in ("breakout", "volatility_trade"):
            return VolatilityModule(logic_type="atr_expansion", params={
                "period": 14, "expansion_ratio": round(self.rng.uniform(1.2, 1.8), 2)
            })
        elif family in ("mean_reversion", "scalping"):
            return VolatilityModule(logic_type="atr_expansion", params={
                "period": 14, "expansion_ratio": round(self.rng.uniform(0.5, 0.9), 2),
                "mode": "contraction_required",
            })
        else:
            return VolatilityModule(logic_type="default", params={
                "period": 14, "expansion_ratio": 1.0
            })
    
    def _build_entry_module(self, family: str, patterns: List[str]) -> EntryModule:
        thresholds = {
            "trend_following": (0.3, 0.5),
            "mean_reversion": (0.5, 0.7),
            "breakout": (0.4, 0.6),
            "momentum": (0.3, 0.5),
            "scalping": (0.6, 0.8),
            "failed_rally": (0.3, 0.5),
            "swing": (0.3, 0.5),
            "positional": (0.2, 0.4),
        }
        low, high = thresholds.get(family, (0.3, 0.6))
        return EntryModule(params={
            "threshold": round(self.rng.uniform(low, high), 2),
            "confirmation_required": family in ("mean_reversion", "breakout"),
        })
    
    def _build_exit_module(self, family: str, risk_params: Dict) -> ExitModule:
        sl_range = risk_params.get("sl_mult_range", None)
        
        profiles = {
            "trend_following": {"sl_mult": (1.5, 2.5), "tp_mult": (3.0, 6.0)},
            "mean_reversion": {"sl_mult": (1.0, 2.0), "tp_mult": (1.5, 3.0)},
            "breakout": {"sl_mult": (1.5, 2.5), "tp_mult": (4.0, 8.0)},
            "scalping": {"sl_mult": (0.5, 1.5), "tp_mult": (1.0, 2.0)},
            "positional": {"sl_mult": (2.0, 4.0), "tp_mult": (5.0, 12.0)},
            "failed_rally": {"sl_mult": (1.5, 2.5), "tp_mult": (2.5, 4.0)},
        }
        profile = profiles.get(family, {"sl_mult": (1.5, 3.0), "tp_mult": (2.0, 5.0)})
        
        sl = sl_range or profile["sl_mult"]
        tp = profile["tp_mult"]
        
        atr_mult = risk_params.get("atr_multiplier")
        
        return ExitModule(params={
            "sl_mult": round(self.rng.uniform(*sl), 1),
            "tp_mult": round(self.rng.uniform(*tp), 1),
            "atr_override": atr_mult,
        })
    
    def _build_risk_module(self, family: str, risk_params: Dict) -> RiskModule:
        max_dd = {
            "scalping": 0.05, "mean_reversion": 0.10, "trend_following": 0.15,
            "breakout": 0.12, "positional": 0.20,
        }.get(family, 0.15)
        
        return RiskModule(params={
            "max_dd_limit": max_dd,
            "risk_per_trade": risk_params.get("risk_per_trade", 0.01),
            "max_correlated_positions": 3,
        })
    
    def _build_position_module(self, family: str, risk_params: Dict) -> PositionModule:
        sizing = "volatility_adjusted" if family in ("trend_following", "breakout") else "fixed_fractional"
        return PositionModule(logic_type=sizing, params={
            "base_risk": risk_params.get("risk_per_trade", 0.01),
        })
    
    def _build_trade_management(self, family: str, risk_params: Dict) -> TradeManagementModule:
        if family in ("trend_following", "breakout", "positional") or risk_params.get("trailing_stop"):
            return TradeManagementModule(logic_type="trailing_stop", params={
                "trail_mult": round(self.rng.uniform(1.5, 3.0), 1),
                "activate_at_r": round(self.rng.uniform(1.0, 2.0), 1),
            })
        return TradeManagementModule(logic_type="default", params={})


# ── Strategy Planner (v2) ─────────────────────────────────────────────────────

class StrategyPlanner:
    """
    Converts Atlas research into diverse, fully-populated strategy genomes.
    
    Architecture:
        Atlas Research Report
              │
              ▼
        ConceptExtractor  (extracts indicators, families, params, regimes)
              │
              ▼
        GenomeBuilder     (constructs complete 10-module genomes)
              │
              ▼
        List[StrategyGenome]  (diverse candidates for evolution)
    """
    
    def __init__(self, seed: int = None):
        self.extractor = ConceptExtractor()
        self.rng = random.Random(seed)
        self.builder = GenomeBuilder(self.rng)
    
    def plan(self, research_report: Optional[Dict[str, Any]], 
             symbol: str, regime: str) -> List[StrategyGenome]:
        """
        Parse research and generate diverse candidate genomes.
        
        If research is None or empty, generates regime-appropriate defaults
        with structural diversity (not identical templates).
        """
        # Extract concepts from research (or use empty defaults)
        if research_report and research_report.get("status") not in ("error", "unavailable"):
            concepts = self.extractor.extract(research_report)
            log.info("Planner extracted %d families, %d indicators from Atlas research",
                    len(concepts["families"]), 
                    sum(len(v) for v in concepts["indicators"].values()))
        else:
            # No research available: generate from regime knowledge
            concepts = self._default_concepts(regime)
            log.info("Planner using regime-based defaults for %s (%s)", symbol, regime)
        
        # Build diverse genomes (one per detected family, each fully populated)
        candidates = []
        families = concepts["families"]
        
        # Ensure at least 3 diverse candidates
        if len(families) < 3:
            extras = ["trend_following", "mean_reversion", "breakout", "momentum"]
            for e in extras:
                if e not in families:
                    families.append(e)
                if len(families) >= 4:
                    break
        
        for family in families:
            try:
                genome = self.builder.build_from_concepts(concepts, symbol, regime, family)
                candidates.append(genome)
                log.info("  Planned genome: %s [%s] trend=%s mom=%s",
                        genome.genome_id, family,
                        genome.trend.logic_type, genome.momentum.logic_type)
            except Exception as exc:
                log.error("Failed to plan %s genome: %s", family, exc)
        
        # Add a variant of the best-matching family with different params
        if candidates and len(candidates) < 6:
            for _ in range(min(2, 6 - len(candidates))):
                family = self.rng.choice(families)
                variant = self.builder.build_from_concepts(concepts, symbol, regime, family)
                candidates.append(variant)
        
        return candidates
    
    def plan_from_hypotheses(self, hypotheses: List[Dict[str, Any]], 
                            symbol: str, regime: str) -> List[StrategyGenome]:
        """Convert hypothesis statements into testable genomes."""
        candidates = []
        for hyp in hypotheses:
            family = hyp.get("family", "trend_following")
            statement = hyp.get("statement", "")
            
            # Build concepts from the hypothesis statement
            concepts = self.extractor.extract({"statement": statement, "family": family})
            concepts["families"] = [family]  # Force the specified family
            
            try:
                genome = self.builder.build_from_concepts(concepts, symbol, regime, family)
                genome.genome_id = f"hyp-{hyp.get('hypothesis_id', uuid.uuid4().hex[:6])}"
                candidates.append(genome)
            except Exception as exc:
                log.error("Failed to convert hypothesis to genome: %s", exc)
        
        return candidates
    
    def _default_concepts(self, regime: str) -> Dict[str, Any]:
        """Generate reasonable defaults when no research is available."""
        regime_families = {
            "trending_up": ["trend_following", "momentum", "breakout"],
            "trending_down": ["failed_rally", "trend_following", "momentum"],
            "ranging": ["mean_reversion", "breakout", "scalping"],
            "high_volatility": ["breakout", "volatility_trade", "mean_reversion"],
            "unknown": ["trend_following", "mean_reversion", "breakout"],
        }
        return {
            "families": regime_families.get(regime, ["trend_following", "mean_reversion"]),
            "indicators": {"trend": ["sma_crossover", "ema_slope", "price_above_sma"],
                          "momentum": ["rsi", "macd_hist"],
                          "volatility": ["atr_expansion"]},
            "timeframes": [60, 240],
            "risk_params": {"risk_per_trade": 0.01},
            "entry_patterns": [],
            "exit_patterns": [],
            "regime_filters": [regime],
            "numerical_params": {},
            "raw_text": "",
        }
