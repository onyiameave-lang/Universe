"""
Oracle.intelligence.strategy_planner
===================================
Translates Atlas research into structured strategy DNA.

The Strategy Planner acts as a bridge between high-level research (Institutional 
Research, Academic Papers, Market Observations) and executable Genomes. It 
extracts trading family logic, indicators, and risk parameters to seed the 
evolutionary process with intelligent candidates.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from intelligence.strategy_genome import (
    StrategyGenome, MarketRegimeModule, TrendModule, MomentumModule, 
    VolatilityModule, EntryModule, ExitModule, RiskModule, PositionModule,
    TradeManagementModule, ExecutionModule
)

log = logging.getLogger("oracle.planner")

class StrategyPlanner:
    """Converts research reports and hypotheses into structured Genomes."""

    def __init__(self):
        # Map research family keywords to planner methods
        self.families = {
            "mean_reversion": self._plan_mean_reversion,
            "trend_following": self._plan_trend_following,
            "breakout": self._plan_breakout,
            "volatility_expansion": self._plan_volatility_expansion,
            "failed_rally": self._plan_failed_rally,
            "scalping": self._plan_scalping,
            "positional": self._plan_positional
        }

    def plan(self, research_report: Dict[str, Any], symbol: str, regime: str) -> List[StrategyGenome]:
        """Parses a research report and generates a list of candidate Genomes."""
        candidates = []
        # Flatten report for easier keyword searching
        report_text = json.dumps(research_report, default=str).lower()
        
        # Detect strategy families mentioned in research
        found_families = []
        for family in self.families:
            # Check for space-separated or underscored family names
            if family.replace("_", " ") in report_text or family in report_text:
                found_families.append(family)
        
        # If no family detected, fallback to regime-based defaults to ensure diversity
        if not found_families:
            if regime == "trending_up": found_families = ["trend_following", "breakout"]
            elif regime == "ranging": found_families = ["mean_reversion"]
            elif regime == "trending_down": found_families = ["failed_rally"]
            elif regime == "high_volatility": found_families = ["volatility_expansion"]
            else: found_families = ["trend_following"]

        log.info("Strategy Planner identified families: %s for %s (%s)", 
                 found_families, symbol, regime)

        for family in set(found_families): # Deduplicate
            try:
                planner_fn = self.families[family]
                candidates.append(planner_fn(symbol, regime, research_report))
            except Exception as exc:
                log.error("Failed to plan family %s: %s", family, exc)
            
        return candidates

    def _plan_trend_following(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = StrategyGenome(genome_id=f"plan-trend-{symbol}-{regime}-{uuid_hex()[:4]}")
        g.trend = TrendModule(logic_type="sma_crossover", params={"fast": 20, "slow": 50})
        g.momentum = MomentumModule(logic_type="macd_hist", params={"threshold": 0})
        g.entry = EntryModule(params={"threshold": 0.4})
        g.exit = ExitModule(params={"sl_mult": 2.5, "tp_mult": 5.0})
        g.market_regime = MarketRegimeModule(params={"allowed_regimes": ["trending_up", "trending_down"]})
        return g

    def _plan_mean_reversion(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = StrategyGenome(genome_id=f"plan-revert-{symbol}-{regime}-{uuid_hex()[:4]}")
        g.trend = TrendModule(logic_type="price_above_sma", params={"period": 200})
        g.momentum = MomentumModule(logic_type="rsi", params={"period": 14, "upper": 75, "lower": 25})
        g.entry = EntryModule(params={"threshold": 0.6})
        g.exit = ExitModule(params={"sl_mult": 1.5, "tp_mult": 2.0})
        g.market_regime = MarketRegimeModule(params={"allowed_regimes": ["ranging"]})
        return g

    def _plan_breakout(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = StrategyGenome(genome_id=f"plan-break-{symbol}-{regime}-{uuid_hex()[:4]}")
        g.volatility = VolatilityModule(logic_type="atr_expansion", params={"period": 14, "expansion_ratio": 1.3})
        g.trend = TrendModule(logic_type="ema_slope", params={"period": 20})
        g.entry = EntryModule(params={"threshold": 0.5})
        g.exit = ExitModule(params={"sl_mult": 2.0, "tp_mult": 6.0})
        return g

    def _plan_failed_rally(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = StrategyGenome(genome_id=f"plan-short-{symbol}-{regime}-{uuid_hex()[:4]}")
        g.trend = TrendModule(logic_type="sma_crossover", params={"fast": 10, "slow": 30})
        g.momentum = MomentumModule(logic_type="rsi", params={"period": 14, "upper": 60, "lower": 40})
        g.entry = EntryModule(params={"threshold": 0.3})
        g.exit = ExitModule(params={"sl_mult": 2.0, "tp_mult": 3.0})
        g.market_regime = MarketRegimeModule(params={"allowed_regimes": ["trending_down"]})
        return g

    def _plan_volatility_expansion(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = self._plan_breakout(symbol, regime, research)
        g.volatility.params["expansion_ratio"] = 1.5
        return g

    def _plan_scalping(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = StrategyGenome(genome_id=f"plan-scalp-{symbol}-{regime}-{uuid_hex()[:4]}")
        g.trend = TrendModule(logic_type="ema_slope", params={"period": 10})
        g.momentum = MomentumModule(logic_type="rsi", params={"period": 7, "upper": 80, "lower": 20})
        g.entry = EntryModule(params={"threshold": 0.7})
        g.exit = ExitModule(params={"sl_mult": 1.0, "tp_mult": 1.5})
        return g

    def _plan_positional(self, symbol: str, regime: str, research: Dict[str, Any]) -> StrategyGenome:
        g = StrategyGenome(genome_id=f"plan-positional-{symbol}-{regime}-{uuid_hex()[:4]}")
        g.trend = TrendModule(logic_type="sma_crossover", params={"fast": 50, "slow": 200})
        g.entry = EntryModule(params={"threshold": 0.2})
        g.exit = ExitModule(params={"sl_mult": 3.0, "tp_mult": 10.0})
        return g

def uuid_hex() -> str:
    return uuid.uuid4().hex
