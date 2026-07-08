"""
Oracle.intelligence.genome_validator
====================================
Genome Validation & Certification Pipeline.

Every genome passes through structured validation before it can be rejected.
No genome is ever silently discarded. Every rejection includes explicit reasons.
Every fitness score includes a complete breakdown of contributing factors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("oracle.validator")


@dataclass
class ValidationReport:
    """Complete diagnostic report for a single genome evaluation."""
    genome_id: str = ""
    family: str = "unknown"
    regime: str = "unknown"

    # Stage 1: Structure
    structure_valid: bool = False
    missing_modules: List[str] = field(default_factory=list)
    module_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Stage 2: Compilation
    compiled: bool = False
    compilation_errors: List[str] = field(default_factory=list)
    effective_warmup: int = 0
    max_indicator_period: int = 0
    tradeable_bars: int = 0

    # Stage 3: Backtest Diagnostics
    backtest_status: str = "not_run"
    total_trades: int = 0
    long_trades: int = 0
    short_trades: int = 0
    signals_generated: int = 0
    signals_hold: int = 0
    signals_filtered_regime: int = 0
    signals_filtered_volatility: int = 0
    signals_filtered_entry: int = 0
    profit: float = 0.0
    drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    recovery: float = 0.0
    consistency: float = 0.0
    zero_trade_reason: str = ""

    # Stage 4: Fitness Breakdown
    profit_score: float = 0.0
    risk_score: float = 0.0
    consistency_score: float = 0.0
    trade_frequency_score: float = 0.0
    generalization_score: float = 0.0
    certification_score: float = 0.0
    final_score: float = -1.0

    # Stage 5: Rejection
    rejected: bool = True
    rejection_reasons: List[str] = field(default_factory=list)

    # Stage 6: Recovery Actions
    recovery_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "genome_id": self.genome_id,
            "family": self.family,
            "regime": self.regime,
            "structure_valid": self.structure_valid,
            "missing_modules": self.missing_modules,
            "compiled": self.compiled,
            "compilation_errors": self.compilation_errors,
            "effective_warmup": self.effective_warmup,
            "max_indicator_period": self.max_indicator_period,
            "tradeable_bars": self.tradeable_bars,
            "backtest_status": self.backtest_status,
            "total_trades": self.total_trades,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "signals_generated": self.signals_generated,
            "signals_hold": self.signals_hold,
            "signals_filtered_regime": self.signals_filtered_regime,
            "signals_filtered_volatility": self.signals_filtered_volatility,
            "signals_filtered_entry": self.signals_filtered_entry,
            "zero_trade_reason": self.zero_trade_reason,
            "profit": self.profit,
            "drawdown": self.drawdown,
            "win_rate": self.win_rate,
            "sharpe": self.sharpe,
            "fitness_breakdown": {
                "profit_score": self.profit_score,
                "risk_score": self.risk_score,
                "consistency_score": self.consistency_score,
                "trade_frequency_score": self.trade_frequency_score,
                "generalization_score": self.generalization_score,
                "certification_score": self.certification_score,
                "final_score": self.final_score,
            },
            "rejected": self.rejected,
            "rejection_reasons": self.rejection_reasons,
            "recovery_actions": self.recovery_actions,
        }


class GenomeValidator:
    """
    Complete genome validation pipeline.
    Validates structure, compiles strategy, runs diagnostics,
    computes detailed fitness, and provides rejection reports.
    """

    REQUIRED_MODULES = [
        "trend", "momentum", "entry", "exit",
        "market_regime", "volatility", "risk",
        "position", "trade_management", "execution"
    ]

    def __init__(self):
        pass

    # ---- Stage 1: Validate Genome Structure ----

    def validate_structure(self, genome, report: ValidationReport) -> ValidationReport:
        """Check that all required modules exist and have valid configuration."""
        report.genome_id = getattr(genome, "genome_id", "unknown")

        # Detect family from genome ID or trend logic
        gid = report.genome_id.lower()
        if "trend" in gid:
            report.family = "trend_following"
        elif "revert" in gid:
            report.family = "mean_reversion"
        elif "break" in gid:
            report.family = "breakout"
        elif "short" in gid:
            report.family = "failed_rally"
        elif "scalp" in gid:
            report.family = "scalping"
        else:
            report.family = getattr(genome, "trend", None) and genome.trend.logic_type or "generic"

        missing = []
        for mod_name in self.REQUIRED_MODULES:
            mod = getattr(genome, mod_name, None)
            if mod is None:
                missing.append(mod_name)
            else:
                report.module_details[mod_name] = {
                    "logic_type": getattr(mod, "logic_type", "default"),
                    "params": getattr(mod, "params", {}),
                }

        report.missing_modules = missing
        report.structure_valid = len(missing) == 0

        if missing:
            report.rejection_reasons.append(f"Missing modules: {', '.join(missing)}")

        return report

    # ---- Stage 2: Compile Strategy (pre-backtest validation) ----

    def compile_strategy(self, genome, series_length: int, report: ValidationReport) -> ValidationReport:
        """Verify the genome can produce signals given the available data."""
        errors = []

        # Determine maximum indicator period needed
        max_period = 30  # minimum for analyze()

        trend = getattr(genome, "trend", None)
        if trend:
            slow = int(trend.params.get("slow", 50))
            fast = int(trend.params.get("fast", 20))
            period = int(trend.params.get("period", 20))
            max_period = max(max_period, slow, fast, period)

        momentum = getattr(genome, "momentum", None)
        if momentum:
            mom_period = int(momentum.params.get("period", 14))
            max_period = max(max_period, mom_period + 1)

        volatility = getattr(genome, "volatility", None)
        if volatility:
            vol_period = int(volatility.params.get("period", 14))
            max_period = max(max_period, vol_period + 1)

        report.max_indicator_period = max_period

        # Calculate effective warmup (must cover indicator lookback)
        effective_warmup = max(max_period + 5, 30)  # +5 buffer for stability
        report.effective_warmup = effective_warmup

        # Check if there are enough bars for meaningful backtesting
        tradeable_bars = series_length - effective_warmup
        report.tradeable_bars = tradeable_bars

        if tradeable_bars < 20:
            errors.append(
                f"Insufficient tradeable bars: {tradeable_bars} (need 20+). "
                f"Series has {series_length} bars but genome needs {effective_warmup} warmup "
                f"(max indicator period: {max_period})"
            )
            report.recovery_actions.append(
                f"Reduce slow period from {max_period} to max {series_length - 25}"
            )

        # Validate entry threshold is reachable
        entry = getattr(genome, "entry", None)
        if entry:
            threshold = entry.params.get("threshold", 0.5)
            # vote() returns trend*0.7 + momentum*0.3, max possible = 1.0
            if threshold > 1.0:
                errors.append(f"Entry threshold {threshold} exceeds max possible vote (1.0)")
                report.recovery_actions.append("Set threshold to 0.5")

        # Validate exit logic
        exit_mod = getattr(genome, "exit", None)
        if exit_mod:
            sl = exit_mod.params.get("sl_mult", 2.0)
            tp = exit_mod.params.get("tp_mult", 3.0)
            if sl <= 0 or tp <= 0:
                errors.append(f"Invalid stop/target multipliers: SL={sl}, TP={tp}")
            if tp < sl:
                errors.append(f"Take profit ({tp}) < stop loss ({sl}): negative expectancy by design")
                report.recovery_actions.append("Set tp_mult >= sl_mult * 1.5")

        # Validate regime filter isn't too restrictive
        regime_mod = getattr(genome, "market_regime", None)
        if regime_mod:
            allowed = regime_mod.params.get("allowed_regimes", [])
            if allowed and len(allowed) == 0:
                errors.append("Market regime filter allows no regimes")
                report.recovery_actions.append("Allow at least the current regime")

        report.compilation_errors = errors
        report.compiled = len(errors) == 0

        if errors:
            for err in errors:
                report.rejection_reasons.append(f"Compilation: {err}")

        return report

    # ---- Stage 3: Backtest Diagnostics ----

    def diagnose_backtest(self, result: Dict[str, Any], report: ValidationReport) -> ValidationReport:
        """Extract detailed diagnostics from backtest results."""
        report.backtest_status = result.get("status", "unknown")

        if result.get("status") == "error":
            report.rejection_reasons.append(f"Backtest error: {result.get('message', 'unknown')}")
            return report

        report.total_trades = result.get("trades", 0)
        report.profit = result.get("total_return", 0.0)
        report.drawdown = result.get("max_drawdown", 0.0)
        report.win_rate = result.get("win_rate", 0.0)
        report.profit_factor = result.get("profit_factor", 0.0) or 0.0
        report.sharpe = result.get("sharpe_proxy", 0.0)
        report.sortino = result.get("sortino_proxy", 0.0)
        report.recovery = result.get("recovery_factor", 0.0)
        report.consistency = result.get("consistency", 0.0)

        # Count trade directions
        if "long_trades" in result:
            report.long_trades = result["long_trades"]
            report.short_trades = result["short_trades"]

        if report.total_trades == 0:
            report.zero_trade_reason = self._diagnose_zero_trades(result, report)
            report.rejection_reasons.append(f"Zero trades: {report.zero_trade_reason}")
        elif report.total_trades < 3:
            report.rejection_reasons.append(
                f"Insufficient trades: {report.total_trades} (minimum 3 required)"
            )

        return report

    def _diagnose_zero_trades(self, result: Dict[str, Any], report: ValidationReport) -> str:
        """Determine why a genome generated zero trades."""
        if report.signals_filtered_regime > 0 and report.signals_generated == 0:
            return "All signals filtered by market regime (regime not in allowed list)"
        if report.signals_filtered_volatility > 0 and report.signals_generated == 0:
            return "All signals filtered by volatility module (ATR expansion not met)"
        if report.signals_filtered_entry > 0:
            return f"Entry threshold too high: {report.signals_filtered_entry} votes below threshold"
        if report.tradeable_bars < 20:
            return f"Only {report.tradeable_bars} tradeable bars after warmup (indicators need {report.max_indicator_period} bars)"
        if report.max_indicator_period > report.tradeable_bars:
            return f"Indicator period ({report.max_indicator_period}) exceeds available data"
        return "No entry conditions met (strategy too conservative for market conditions)"

    # ---- Stage 4: Fitness Breakdown ----

    def compute_fitness(self, result: Dict[str, Any], report: ValidationReport) -> ValidationReport:
        """Compute detailed fitness with full breakdown instead of flat -1.0."""
        if report.backtest_status != "complete" or report.total_trades < 3:
            # Still compute partial scores for diagnostics
            report.profit_score = 0.0
            report.risk_score = 0.0
            report.consistency_score = 0.0
            report.trade_frequency_score = max(0.0, report.total_trades / 3.0) * 0.2
            report.generalization_score = 0.0
            report.certification_score = 0.0
            report.final_score = -1.0
            return report

        ret = result.get("total_return", 0.0)
        sharpe = result.get("sharpe_proxy", 0.0)
        sortino = result.get("sortino_proxy", 0.0)
        recovery = result.get("recovery_factor", 0.0)
        consistency = result.get("consistency", 0.0)
        dd = result.get("max_drawdown", 1.0)
        trades = result.get("trades", 0)

        # Individual score components (normalized 0-1 where possible)
        report.profit_score = round(max(0.0, ret * 2.0), 4)
        report.risk_score = round(max(0.0, 1.0 - min(dd, 1.0)), 4)
        report.consistency_score = round(consistency, 4)
        report.trade_frequency_score = round(min(1.0, trades / 10.0), 4)
        report.generalization_score = round(
            max(0.0, sharpe * 0.35 + sortino * 0.25 + recovery * 0.15), 4
        )

        # Composite fitness (same formula as before but decomposed)
        raw = (ret * 2 + sharpe * 0.35 + sortino * 0.25 + recovery * 0.15
               + consistency * 0.2)
        dd_penalty = 1 - min(dd, 0.9)
        report.certification_score = round(raw * dd_penalty, 4)
        report.final_score = round(raw * dd_penalty, 4)

        # Determine if rejected
        if report.final_score > 0:
            report.rejected = False
        else:
            report.rejected = True
            # Diagnose WHY the score is negative
            if ret <= 0:
                report.rejection_reasons.append(f"Negative return: {ret:.4f}")
            if dd > 0.5:
                report.rejection_reasons.append(f"Excessive drawdown: {dd:.4f} (50%+ loss)")
            if sharpe < 0:
                report.rejection_reasons.append(f"Negative Sharpe: {sharpe:.3f}")
            if consistency < 0.4:
                report.rejection_reasons.append(f"Low consistency (win rate): {consistency:.3f}")

        return report

    # ---- Stage 5: Full Rejection Report ----

    def finalize_report(self, report: ValidationReport) -> ValidationReport:
        """Ensure every rejected genome has at least one explicit reason."""
        if report.rejected and not report.rejection_reasons:
            report.rejection_reasons.append("Score below promotion threshold (0.0)")

        if not report.rejected:
            report.rejection_reasons = []  # Clear if not rejected

        return report

    # ---- Stage 6: Repair Genome ----

    def repair_genome(self, genome, series_length: int):
        """Attempt to repair a genome that fails compilation."""
        repaired = False

        # Fix 1: Cap slow period to fit available data
        trend = getattr(genome, "trend", None)
        if trend:
            slow = int(trend.params.get("slow", 50))
            max_allowed = max(20, series_length - 30)
            if slow > max_allowed:
                trend.params["slow"] = max_allowed
                fast = int(trend.params.get("fast", 20))
                if fast >= max_allowed:
                    trend.params["fast"] = max(5, max_allowed // 3)
                repaired = True
                log.info("Repaired genome %s: capped slow period to %d",
                         genome.genome_id, max_allowed)

        # Fix 2: Ensure entry threshold is reachable
        entry = getattr(genome, "entry", None)
        if entry:
            threshold = entry.params.get("threshold", 0.5)
            if threshold > 0.8:
                entry.params["threshold"] = 0.5
                repaired = True

        # Fix 3: Ensure exit logic is valid
        exit_mod = getattr(genome, "exit", None)
        if exit_mod:
            sl = exit_mod.params.get("sl_mult", 2.0)
            tp = exit_mod.params.get("tp_mult", 3.0)
            if tp < sl:
                exit_mod.params["tp_mult"] = sl * 1.5
                repaired = True
            if sl <= 0:
                exit_mod.params["sl_mult"] = 2.0
                repaired = True
            if tp <= 0:
                exit_mod.params["tp_mult"] = 3.0
                repaired = True

        # Fix 4: Broaden regime filter
        regime_mod = getattr(genome, "market_regime", None)
        if regime_mod:
            allowed = regime_mod.params.get("allowed_regimes", [])
            if len(allowed) == 1:
                # Add at least ranging as fallback
                regime_mod.params["allowed_regimes"] = [
                    "trending_up", "trending_down", "ranging", "high_volatility"
                ]
                repaired = True

        # Fix 5: Ensure volatility filter isn't too strict
        vol_mod = getattr(genome, "volatility", None)
        if vol_mod and vol_mod.logic_type == "atr_expansion":
            ratio = vol_mod.params.get("expansion_ratio", 1.0)
            if ratio > 1.5:
                vol_mod.params["expansion_ratio"] = 1.1
                repaired = True

        return repaired


@dataclass
class EvolutionSummary:
    """Stage 6: Champion Candidate Report."""
    total_genomes: int = 0
    valid_genomes: int = 0
    rejected_structure: int = 0
    rejected_compilation: int = 0
    rejected_zero_trades: int = 0
    rejected_low_fitness: int = 0
    backtested_genomes: int = 0
    certified_candidates: int = 0
    champion_promoted: bool = False
    elimination_stage: str = ""
    reports: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_genomes": self.total_genomes,
            "valid_genomes": self.valid_genomes,
            "rejected_structure": self.rejected_structure,
            "rejected_compilation": self.rejected_compilation,
            "rejected_zero_trades": self.rejected_zero_trades,
            "rejected_low_fitness": self.rejected_low_fitness,
            "backtested_genomes": self.backtested_genomes,
            "certified_candidates": self.certified_candidates,
            "champion_promoted": self.champion_promoted,
            "elimination_stage": self.elimination_stage,
            "top_reports": self.reports[:5],  # Top 5 for brevity
        }
