"""
Oracle.intelligence.evolution
============================
The strategy evolution lab with FULL GENOME VALIDATION PIPELINE.

Every genome is validated, compiled, backtested with diagnostics, scored with
a detailed breakdown, and rejected only with explicit reasons. No genome is
ever silently assigned -1.0 without a traceable diagnostic report.
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence.strategy_genome import (StrategyGenome, random_strategy, mutate_strategy, crossover_strategy)  # type: ignore
from intelligence.technicals import analyze  # type: ignore
from intelligence.genome_validator import GenomeValidator, ValidationReport, EvolutionSummary  # type: ignore
from core.backtester import Backtester  # type: ignore

log = logging.getLogger("oracle.evolution")


class SlicedSeries:
    """
    A lightweight series view that is FULLY compatible with the real Series class.
    Supports len(), iteration, and all attribute access that any module might need.
    This fixes: TypeError: object of type '_S' has no len()
    """
    __slots__ = ('symbol', 'bars', 'source', '_closes', '_highs', '_lows')

    def __init__(self, symbol: str, bars: list, source: str = "sliced"):
        self.symbol = symbol
        self.bars = bars
        self.source = source
        self._closes = [b.close for b in bars] if bars else []
        self._highs = [b.high for b in bars] if bars else []
        self._lows = [b.low for b in bars] if bars else []

    @property
    def closes(self) -> List[float]:
        return self._closes

    @property
    def highs(self) -> List[float]:
        return self._highs

    @property
    def lows(self) -> List[float]:
        return self._lows

    @property
    def last(self) -> Optional[float]:
        return self._closes[-1] if self._closes else None

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self):
        return iter(self.bars)

    def __getitem__(self, idx):
        return self.bars[idx]


class BarView:
    """Minimal bar-like object for constructing sub-series from raw lists."""
    __slots__ = ('open', 'high', 'low', 'close', 'volume', 'ts')

    def __init__(self, close: float, high: float, low: float):
        self.open = close
        self.high = high
        self.low = low
        self.close = close
        self.volume = 0
        self.ts = ""


def make_series_from_lists(closes: List[float], highs: List[float], lows: List[float], symbol: str = "unknown") -> SlicedSeries:
    """Create a full SlicedSeries from raw price lists (used inside decide_fn)."""
    bars = [BarView(c, h, l) for c, h, l in zip(closes, highs, lows)]
    s = SlicedSeries(symbol, bars, source="backtest_view")
    # Override with actual lists for performance (avoid recomputing from bars)
    s._closes = list(closes)
    s._highs = list(highs)
    s._lows = list(lows)
    return s


class EvolutionLab:
    def __init__(self, chronicle=None, atlas=None, storage_dir: str = "memory",
                 population: int = 16, seed: int = 7):
        self.chronicle = chronicle
        self.atlas = atlas
        self.rng = random.Random(seed)
        self.population_size = population
        self.backtester = Backtester()
        self.validator = GenomeValidator()
        self._path = Path(storage_dir) / "evolved_strategies.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._champions: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._champions = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._champions = {}

    def _persist(self):
        try:
            self._path.write_text(json.dumps(self._champions, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---- VALIDATED FITNESS with full diagnostic pipeline ----

    def _fitness(self, genome: StrategyGenome, series, collect_reports: Optional[List] = None) -> Dict[str, Any]:
        """
        Evaluate a genome with FULL VALIDATION PIPELINE.
        Never silently returns -1.0. Always produces a diagnostic report.
        """
        report = ValidationReport()
        report.regime = self._regime_for(series)

        # Stage 1: Validate Structure
        self.validator.validate_structure(genome, report)
        if not report.structure_valid:
            self.validator.repair_genome(genome, len(series.bars))
            self.validator.validate_structure(genome, report)

        # Stage 2: Compile Strategy
        self.validator.compile_strategy(genome, len(series.bars), report)
        if not report.compiled:
            repaired = self.validator.repair_genome(genome, len(series.bars))
            if repaired:
                report.compilation_errors = []
                report.rejection_reasons = [r for r in report.rejection_reasons if "Compilation" not in r]
                self.validator.compile_strategy(genome, len(series.bars), report)

        # Stage 3: Backtest with diagnostics
        effective_warmup = max(report.effective_warmup, 15)
        symbol = getattr(series, 'symbol', 'unknown')

        def decide(closes, highs, lows, **kwargs):
            """Create a FULL series object that supports len() and all attributes."""
            s = make_series_from_lists(closes, highs, lows, symbol)
            return {"call": genome.call(s)}

        try:
            result = self.backtester.run(series, decide, warmup=effective_warmup)
        except Exception as exc:
            log.warning("Genome %s backtest crashed: %s", genome.genome_id, exc)
            result = {"status": "error", "message": str(exc), "trades": 0}

        # Diagnose the backtest
        self.validator.diagnose_backtest(result, report)

        # If zero trades, run signal diagnostics
        if report.total_trades == 0 and result.get("status") != "error":
            self._diagnose_signals(genome, series, effective_warmup, report)

        # Stage 4: Compute detailed fitness
        self.validator.compute_fitness(result, report)

        # Stage 5: Finalize rejection report
        self.validator.finalize_report(report)

        # Collect report for summary
        if collect_reports is not None:
            collect_reports.append(report.to_dict())

        # Log diagnostic for non-trivial failures
        if report.rejected and report.total_trades > 0:
            log.info("Genome %s rejected: score=%.4f, trades=%d, reasons=%s",
                     genome.genome_id, report.final_score, report.total_trades,
                     report.rejection_reasons[:2])
        elif report.total_trades == 0:
            log.info("Genome %s: zero trades. Reason: %s",
                     genome.genome_id, report.zero_trade_reason)

        return {
            "fitness": report.final_score,
            "result": result,
            "report": report.to_dict(),
        }

    def _diagnose_signals(self, genome, series, warmup: int, report: ValidationReport):
        """Count how many bars generated each signal type to diagnose zero-trade issues."""
        closes = series.closes
        highs = series.highs
        lows = series.lows
        n = len(closes)
        symbol = getattr(series, 'symbol', 'unknown')

        vol_filtered = 0
        regime_filtered = 0
        entry_filtered = 0
        signals = 0

        sample_end = min(n, warmup + 50)
        for i in range(warmup, sample_end):
            # Build a proper series view for this bar
            s = make_series_from_lists(closes[:i + 1], highs[:i + 1], lows[:i + 1], symbol)

            # Check volatility filter
            try:
                if not genome.volatility.filter(s):
                    vol_filtered += 1
                    continue
            except Exception:
                vol_filtered += 1
                continue

            # Check regime filter
            try:
                t = analyze(s)
                regime = (t.get("regime") or {}).get("regime", "unknown")
                vol = (t.get("regime") or {}).get("volatility", 0.0)
                if not genome.market_regime.is_allowed(regime, vol):
                    regime_filtered += 1
                    continue
            except Exception:
                regime_filtered += 1
                continue

            # Check vote + entry threshold
            try:
                v = genome.vote(s)
                if genome.entry.should_enter(v):
                    signals += 1
                else:
                    entry_filtered += 1
            except Exception:
                entry_filtered += 1

        report.signals_generated = signals
        report.signals_filtered_regime = regime_filtered
        report.signals_filtered_volatility = vol_filtered
        report.signals_filtered_entry = entry_filtered

        # Determine zero trade reason
        sampled = sample_end - warmup
        if vol_filtered > sampled * 0.6:
            report.zero_trade_reason = (
                f"Volatility filter rejected {vol_filtered}/{sampled} bars "
                f"(expansion_ratio={genome.volatility.params.get('expansion_ratio', 1.0)})"
            )
        elif regime_filtered > sampled * 0.6:
            allowed = genome.market_regime.params.get("allowed_regimes", [])
            report.zero_trade_reason = (
                f"Regime filter rejected {regime_filtered}/{sampled} bars "
                f"(allowed: {allowed}, actual regime: {report.regime})"
            )
        elif entry_filtered > sampled * 0.6:
            report.zero_trade_reason = (
                f"Entry threshold too high: {entry_filtered}/{sampled} votes below "
                f"threshold {genome.entry.params.get('threshold', 0.5)}"
            )
        elif signals == 0:
            report.zero_trade_reason = (
                f"No signals after all filters (vol:{vol_filtered} regime:{regime_filtered} "
                f"entry:{entry_filtered} of {sampled} bars)"
            )
        else:
            report.zero_trade_reason = (
                f"Signals generated ({signals}) but backtester didn't open trades "
                f"(possible ATR/position sizing issue)"
            )

    def _champion_key(self, symbol: str, regime: str) -> str:
        return f"{symbol.upper()}::{regime}"

    def _regime_for(self, series) -> str:
        try:
            return (analyze(series).get("regime") or {}).get("regime", "unknown")
        except Exception:
            return "unknown"

    # ---- the evolutionary run with full validation ----

    def evolve(self, series, generations: int = 5, planned_candidates: List[StrategyGenome] = None) -> Dict[str, Any]:
        symbol = series.symbol
        regime = self._regime_for(series)
        champion_key = self._champion_key(symbol, regime)
        split = int(len(series.bars) * 0.7)
        in_sample = _slice_series(series, 0, split)
        out_sample = _slice_series(series, split, len(series.bars))

        if len(in_sample) < 70 or len(out_sample) < 30:
            return {"status": "error", "message": "insufficient history to evolve safely",
                    "bars_available": len(series.bars), "in_sample": len(in_sample),
                    "out_sample": len(out_sample)}

        # Build population
        population = [random_strategy(self.rng) for _ in range(self.population_size)]

        if planned_candidates:
            for i, cand in enumerate(planned_candidates[:self.population_size // 2]):
                population[i + 1] = cand

        if champion_key in self._champions:
            population[0] = StrategyGenome.from_dict(self._champions[champion_key]["genome"])

        # Pre-validate and repair all genomes BEFORE evolution
        for g in population:
            self.validator.repair_genome(g, len(in_sample))

        # Evolution loop with diagnostic collection
        history = []
        all_reports: List[Dict[str, Any]] = []
        best = None

        for gen in range(generations):
            scored = []
            gen_reports: List[Dict[str, Any]] = []

            for g in population:
                fit = self._fitness(g, in_sample, collect_reports=gen_reports)
                g.fitness = fit["fitness"]
                g.best_return = fit["result"].get("total_return", 0.0)
                g.best_sharpe = fit["result"].get("sharpe_proxy", 0.0)
                g.backtests += 1
                scored.append((g, fit["fitness"]))

            scored.sort(key=lambda x: x[1], reverse=True)
            best = scored[0][0]

            valid_count = sum(1 for _, f in scored if f > -1.0)
            history.append({
                "generation": gen,
                "best_fitness": round(scored[0][1], 4),
                "best_return": best.best_return,
                "valid_genomes": valid_count,
                "total_genomes": len(scored),
            })

            if gen == generations - 1:
                all_reports = gen_reports

            # Next generation
            survivors = [g for g, _ in scored[:max(2, self.population_size // 3)]]
            next_pop = list(survivors[:2])
            while len(next_pop) < self.population_size:
                a = self._tournament(scored); b = self._tournament(scored)
                child = crossover_strategy(a, b, self.rng)
                child = mutate_strategy(child, self.rng)
                self.validator.repair_genome(child, len(in_sample))
                next_pop.append(child)
            population = next_pop

        # ---- CERTIFY the best on OUT-OF-SAMPLE ----
        oos_reports: List[Dict[str, Any]] = []
        oos = self._fitness(best, out_sample, collect_reports=oos_reports)
        incumbent = self._champions.get(champion_key)
        incumbent_oos = incumbent["out_of_sample"]["total_return"] if incumbent else None
        oos_return = oos["result"].get("total_return", -1.0)

        promoted = False
        if (oos["result"].get("status") == "complete" and oos_return > 0
                and oos["result"].get("trades", 0) >= 3
                and (incumbent_oos is None or oos_return > incumbent_oos)):
            self._champions[champion_key] = {
                "symbol": symbol, "regime": regime,
                "activation_rules": {"symbol": symbol, "regime": regime},
                "genome": best.to_dict(),
                "in_sample": {"return": best.best_return, "fitness": best.fitness},
                "out_of_sample": oos["result"],
                "failure_conditions": _failure_conditions(oos["result"]),
                "certified_at": time.time(),
            }
            self._persist()
            promoted = True
            self._preserve(symbol, regime, best, oos["result"])

        summary = self._build_summary(all_reports, promoted)

        return {
            "status": "complete", "symbol": symbol, "regime": regime,
            "generations": generations, "history": history,
            "best_genome": best.to_dict(),
            "in_sample_return": best.best_return,
            "out_of_sample": oos["result"],
            "promoted_new_champion": promoted,
            "champion": self._champions.get(champion_key, {}).get("genome", {}).get("genome_id"),
            "validation_summary": summary.to_dict(),
            "oos_report": oos_reports[0] if oos_reports else {},
        }

    def _build_summary(self, reports: List[Dict[str, Any]], promoted: bool) -> EvolutionSummary:
        summary = EvolutionSummary()
        summary.total_genomes = len(reports)
        summary.champion_promoted = promoted
        summary.reports = sorted(
            reports,
            key=lambda r: r.get("fitness_breakdown", {}).get("final_score", -99),
            reverse=True
        )[:5]

        for r in reports:
            if r.get("structure_valid"):
                summary.valid_genomes += 1
            else:
                summary.rejected_structure += 1
                continue

            if r.get("compiled"):
                pass
            else:
                summary.rejected_compilation += 1
                continue

            if r.get("total_trades", 0) >= 3:
                summary.backtested_genomes += 1
            else:
                summary.rejected_zero_trades += 1
                continue

            score = r.get("fitness_breakdown", {}).get("final_score", -1.0)
            if score > 0:
                summary.certified_candidates += 1
            else:
                summary.rejected_low_fitness += 1

        if summary.rejected_structure > summary.total_genomes * 0.5:
            summary.elimination_stage = "structure_validation"
        elif summary.rejected_compilation > summary.total_genomes * 0.3:
            summary.elimination_stage = "compilation (indicator periods exceed data)"
        elif summary.rejected_zero_trades > summary.total_genomes * 0.5:
            summary.elimination_stage = "backtesting (zero trades generated)"
        elif summary.rejected_low_fitness > summary.total_genomes * 0.5:
            summary.elimination_stage = "fitness (negative returns or high drawdown)"
        elif summary.certified_candidates == 0:
            summary.elimination_stage = "certification (no genome profitable on OOS)"
        else:
            summary.elimination_stage = "none (candidates exist)"

        return summary

    def _tournament(self, scored, k: int = 3) -> StrategyGenome:
        contenders = self.rng.sample(scored, min(k, len(scored)))
        return max(contenders, key=lambda x: x[1])[0]

    def champion(self, symbol: str, regime: Optional[str] = None) -> Optional[StrategyGenome]:
        rec = self.champion_info(symbol, regime)
        return StrategyGenome.from_dict(rec["genome"]) if rec else None

    def champion_info(self, symbol: str, regime: Optional[str] = None) -> Optional[Dict[str, Any]]:
        symbol = symbol.upper()
        if regime:
            return self._champions.get(self._champion_key(symbol, regime))
        candidates = [c for c in self._champions.values() if c.get("symbol", "").upper() == symbol]
        if not candidates:
            legacy = self._champions.get(symbol)
            return legacy
        return max(candidates, key=lambda c: c.get("out_of_sample", {}).get("total_return", -999))

    def _preserve(self, symbol, regime: str, genome: StrategyGenome, oos_result):
        if self.chronicle is None:
            return
        try:
            mods = genome.to_dict().get("modules", {})
            desc = f"Trend:{mods.get('trend', {}).get('logic_type')}, Mom:{mods.get('momentum', {}).get('logic_type')}"
            content = (f"Oracle evolved a certified {regime} strategy for {symbol}: [{desc}]. "
                       f"Out-of-sample return {oos_result.get('total_return')}, "
                       f"win rate {oos_result.get('win_rate')}, max_dd {oos_result.get('max_drawdown')}.")
            tags = ["oracle", "evolved_strategy", symbol, regime]
            store_fn = getattr(self.chronicle, "store", None)
            if callable(store_fn):
                store_fn(content=content, memory_type="evolutionary", domain="trading",
                         tags=tags, source="oracle")
            elif hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {
                    "content": content,
                    "pillar": "episodic",
                    "domain": "trading",
                    "tags": tags,
                    "_sender": "oracle",
                })
        except Exception:
            pass

    def stats(self) -> Dict[str, Any]:
        return {"champion_keys": list(self._champions.keys()),
                "champions": {s: {"symbol": c.get("symbol"), "regime": c.get("regime"),
                                   "genome_id": c["genome"]["genome_id"],
                                   "oos_return": c["out_of_sample"].get("total_return")}
                              for s, c in self._champions.items()}}


def _slice_series(series, start: int, end: int) -> SlicedSeries:
    """Return a SlicedSeries that supports len() and all Series attributes."""
    bars = series.bars[start:end]
    return SlicedSeries(series.symbol, bars, source="sliced")


def _failure_conditions(result: Dict[str, Any]) -> List[str]:
    failures = []
    if result.get("total_return", 0) <= 0:
        failures.append("non-positive out-of-sample return")
    if result.get("max_drawdown", 0) > 0.15:
        failures.append("drawdown exceeds tolerance")
    if result.get("win_rate", 1) < 0.4:
        failures.append("win rate remains poor")
    if result.get("trades", 0) < 3:
        failures.append("insufficient trade frequency")
    return failures or ["regime shift", "liquidity deterioration", "news shock"]
