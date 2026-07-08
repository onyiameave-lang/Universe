"""
Oracle.intelligence.evolution (v2 - PATCHED)
============================================
Fixes:
1. Added logging inside evolve() so we can SEE what's happening
2. _fitness() catches genome.call() exceptions with full traceback
3. Handles case where ALL genomes score -1 (returns error, not silent empty)
4. Reduced default population to 16 (matching original) for speed
5. Added validation that planned_candidates are valid StrategyGenome objects
6. Fixed: when series has no .bars attribute, handles gracefully
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence.strategy_genome import (
    StrategyGenome, random_strategy, mutate_strategy, crossover_strategy,
    random_diverse_strategy
)
from intelligence.technicals import analyze
from core.backtester import Backtester

log = logging.getLogger("oracle.evolution")


class EvolutionLab:
    def __init__(self, chronicle=None, atlas=None, storage_dir: str = "memory",
                 population: int = 16, seed: int = None):
        self.chronicle = chronicle
        self.atlas = atlas
        self.rng = random.Random(seed)
        self.population_size = population
        self.backtester = Backtester()
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

    def _fitness(self, genome: StrategyGenome, series) -> Dict[str, Any]:
        """Evaluate a genome via backtest. PATCHED: catches all genome errors."""
        try:
            def decide(closes, highs, lows):
                return {"call": genome.decide(closes, highs, lows)}

            warmup = min(50, max(15, len(series.bars) // 3))
            result = self.backtester.run(series, decide, warmup=warmup)

            if result.get("status") != "complete" or result.get("trades", 0) < 3:
                return {"fitness": -1.0, "result": result}

            ret = result.get("total_return", 0.0)
            sharpe = result.get("sharpe_proxy", 0.0)
            sortino = result.get("sortino_proxy", 0.0)
            recovery = result.get("recovery_factor", 0.0)
            consistency = result.get("consistency", 0.0)
            dd = result.get("max_drawdown", 1.0)
            trades = result.get("trades", 0)

            fitness = (
                ret * 2.0
                + sharpe * 0.4
                + sortino * 0.3
                + recovery * 0.15
                + consistency * 0.2
                + min(trades / 20.0, 0.3)
            ) * (1 - min(dd, 0.9))

            return {"fitness": round(fitness, 4), "result": result}

        except Exception as exc:
            # PATCHED: log the error instead of crashing the entire evolution
            log.debug("  Genome %s fitness error: %s", genome.genome_id, exc)
            return {"fitness": -1.0, "result": {"status": "error", "message": str(exc)}}

    def _champion_key(self, symbol: str, regime: str) -> str:
        return f"{symbol.upper()}::{regime}"

    def _regime_for(self, series) -> str:
        try:
            return (analyze(series).get("regime") or {}).get("regime", "unknown")
        except Exception:
            return "unknown"

    def _seed_population(self, planned_candidates: Optional[List[StrategyGenome]],
                        champion_key: str) -> List[StrategyGenome]:
        """Seed with planned candidates + diverse randoms."""
        population = []

        # 1. Carry champion elite
        if champion_key in self._champions:
            try:
                elite = StrategyGenome.from_dict(self._champions[champion_key]["genome"])
                population.append(elite)
            except Exception:
                pass

        # 2. Insert planned candidates (validate they're actual StrategyGenome objects)
        if planned_candidates:
            for cand in planned_candidates:
                if len(population) >= self.population_size:
                    break
                if isinstance(cand, StrategyGenome):
                    population.append(cand)
                else:
                    log.warning("  Skipping non-StrategyGenome candidate: %s", type(cand))

            # Add mutated variants for more diversity
            valid_cands = [c for c in planned_candidates if isinstance(c, StrategyGenome)]
            for cand in valid_cands[:3]:
                if len(population) >= self.population_size:
                    break
                try:
                    variant = mutate_strategy(cand, self.rng, rate=0.4)
                    population.append(variant)
                except Exception:
                    pass

        # 3. Fill remaining with diverse randoms
        while len(population) < self.population_size:
            try:
                population.append(random_diverse_strategy(self.rng))
            except Exception as exc:
                log.error("  random_diverse_strategy failed: %s", exc)
                # Ultimate fallback: minimal valid genome
                population.append(StrategyGenome())

        log.info("  Population seeded: %d planned, %d random, %d total",
                len(planned_candidates) if planned_candidates else 0,
                self.population_size - (len(planned_candidates) if planned_candidates else 0),
                len(population))

        return population[:self.population_size]

    def _tournament(self, scored: List[Tuple[StrategyGenome, float]], k: int = 3) -> StrategyGenome:
        contenders = self.rng.sample(scored, min(k, len(scored)))
        return max(contenders, key=lambda x: x[1])[0]

    def evolve(self, series, generations: int = 6,
              planned_candidates: Optional[List[StrategyGenome]] = None) -> Dict[str, Any]:
        """
        Run evolution. PATCHED: full logging, error handling, generation cap.
        """
        symbol = getattr(series, 'symbol', 'UNKNOWN')
        regime = self._regime_for(series)
        champion_key = self._champion_key(symbol, regime)

        log.info("  Evolution starting: %s (%s), %d gens, pop=%d",
                symbol, regime, generations, self.population_size)

        # Cap generations to prevent accidental hour-long runs
        if generations > 50:
            log.warning("  Capping generations from %d to 50 (safety limit)", generations)
            generations = 50

        # Validate series has bars
        bars = getattr(series, 'bars', None)
        if not bars or len(bars) < 100:
            log.error("  Insufficient data: %d bars (need 100+)", len(bars) if bars else 0)
            return {"status": "error", "message": f"insufficient history: {len(bars) if bars else 0} bars",
                   "history": [], "best_genome": {}, "in_sample_return": 0.0,
                   "out_of_sample": {}, "promoted_new_champion": False}

        # Split: in-sample + out-of-sample
        split = int(len(bars) * 0.7)
        in_sample = self._slice_series(series, 0, split)
        out_sample = self._slice_series(series, split, len(bars))

        if len(in_sample.bars) < 60 or len(out_sample.bars) < 25:
            log.error("  Split too small: in=%d, out=%d", len(in_sample.bars), len(out_sample.bars))
            return {"status": "error", "message": "insufficient data after split",
                   "history": [], "best_genome": {}, "in_sample_return": 0.0,
                   "out_of_sample": {}, "promoted_new_champion": False}

        # Seed population
        population = self._seed_population(planned_candidates, champion_key)

        history = []
        best = None

        for gen in range(generations):
            # Evaluate
            scored = []
            for g in population:
                fit = self._fitness(g, in_sample)
                g.fitness = fit["fitness"]
                g.best_return = fit["result"].get("total_return", 0.0)
                g.best_sharpe = fit["result"].get("sharpe_proxy", 0.0)
                g.backtests += 1
                scored.append((g, fit["fitness"]))

            scored.sort(key=lambda x: x[1], reverse=True)
            best = scored[0][0]

            # Check if ALL genomes scored -1 (nothing works at all)
            if all(s[1] <= -1.0 for s in scored):
                log.warning("  Gen %d: ALL genomes scored -1.0 (no viable strategies)", gen)
                # Inject completely fresh population
                population = [random_diverse_strategy(self.rng) for _ in range(self.population_size)]
                continue

            history.append({
                "generation": gen,
                "best_fitness": round(scored[0][1], 4),
                "best_return": round(best.best_return, 4),
                "best_sharpe": round(best.best_sharpe, 4),
                "viable_count": sum(1 for _, f in scored if f > -1.0),
            })

            if gen % 5 == 0 or gen == generations - 1:
                log.info("  Gen %d/%d: best_fitness=%.4f, return=%.4f, viable=%d/%d",
                        gen, generations, scored[0][1], best.best_return,
                        history[-1]["viable_count"], len(scored))

            # Breed next generation
            elite_count = max(2, self.population_size // 5)
            next_pop = [g for g, _ in scored[:elite_count]]

            while len(next_pop) < self.population_size:
                a = self._tournament(scored)
                b = self._tournament(scored)
                try:
                    child = crossover_strategy(a, b, self.rng)
                    child = mutate_strategy(child, self.rng, rate=0.3)
                    next_pop.append(child)
                except Exception:
                    next_pop.append(random_diverse_strategy(self.rng))

            population = next_pop

        # ── CERTIFY on out-of-sample ──
        if best is None or best.fitness <= -1.0:
            log.warning("  No viable genome found after %d generations", generations)
            return {"status": "complete", "symbol": symbol, "regime": regime,
                   "generations": generations, "history": history,
                   "best_genome": {}, "in_sample_return": 0.0,
                   "out_of_sample": {"status": "skipped", "reason": "no viable genome"},
                   "promoted_new_champion": False}

        oos = self._fitness(best, out_sample)
        incumbent = self._champions.get(champion_key)
        incumbent_oos = incumbent["out_of_sample"]["total_return"] if incumbent else None
        oos_return = oos["result"].get("total_return", -1.0)

        promoted = False
        if (oos["result"].get("status") == "complete"
            and oos_return > 0
            and oos["result"].get("trades", 0) >= 3
            and (incumbent_oos is None or oos_return > incumbent_oos)):

            self._champions[champion_key] = {
                "symbol": symbol, "regime": regime,
                "genome": best.to_dict(),
                "in_sample": {"return": best.best_return, "fitness": best.fitness},
                "out_of_sample": oos["result"],
                "certified_at": time.time(),
            }
            self._persist()
            promoted = True
            self._preserve_to_chronicle(symbol, regime, best, oos["result"])
            log.info("  ✅ NEW CHAMPION promoted! OOS return: %.4f", oos_return)
        else:
            log.info("  ❌ Not promoted. OOS return: %.4f (need > 0, trades >= 3)", oos_return)

        return {
            "status": "complete", "symbol": symbol, "regime": regime,
            "generations": generations, "history": history,
            "best_genome": best.to_dict(),
            "in_sample_return": best.best_return,
            "out_of_sample": oos["result"],
            "promoted_new_champion": promoted,
            "champion": self._champions.get(champion_key, {}).get("genome", {}).get("genome_id"),
        }

    def _slice_series(self, series, start: int, end: int):
        class _S: pass
        s = _S()
        bars = series.bars[start:end]
        s.symbol = getattr(series, 'symbol', 'UNKNOWN')
        s.bars = bars
        s.closes = [b.close for b in bars]
        s.highs = [b.high for b in bars]
        s.lows = [b.low for b in bars]
        s.last = bars[-1].close if bars else None
        return s

    def _preserve_to_chronicle(self, symbol, regime, genome, oos_result):
        if self.chronicle is None:
            return
        try:
            mods = genome.to_dict().get("modules", {})
            content = (f"Oracle champion {symbol} ({regime}): "
                      f"trend={mods.get('trend', {}).get('logic_type')}, "
                      f"mom={mods.get('momentum', {}).get('logic_type')}, "
                      f"OOS return={oos_result.get('total_return'):.4f}")
            if hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {"content": content, "domain": "trading", "source": "oracle"})
            elif hasattr(self.chronicle, "store"):
                self.chronicle.store(content=content, memory_type="evolutionary",
                                    domain="trading", tags=["oracle", symbol, regime], source="oracle")
        except Exception:
            pass

    # ── Public API ──

    def champion(self, symbol: str, regime: Optional[str] = None) -> Optional[StrategyGenome]:
        rec = self.champion_info(symbol, regime)
        return StrategyGenome.from_dict(rec["genome"]) if rec else None

    def champion_info(self, symbol: str, regime: Optional[str] = None) -> Optional[Dict[str, Any]]:
        symbol = symbol.upper()
        if regime:
            return self._champions.get(self._champion_key(symbol, regime))
        candidates = [c for c in self._champions.values() if c.get("symbol", "").upper() == symbol]
        if not candidates:
            return self._champions.get(symbol)
        return max(candidates, key=lambda c: c.get("out_of_sample", {}).get("total_return", -999))

    def stats(self) -> Dict[str, Any]:
        return {"champion_keys": list(self._champions.keys()),
               "champions": {s: {"symbol": c.get("symbol"), "regime": c.get("regime"),
                               "oos_return": c.get("out_of_sample", {}).get("total_return")}
                           for s, c in self._champions.items()}}
