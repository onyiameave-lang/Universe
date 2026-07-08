"""
Oracle.intelligence.evolution
============================
The strategy evolution lab. (Book I Part IV Article XIII Evolution; Article IX
Evolutionary Optimization; Book III Ch XV Certification; Book II Principle IV
Research Before Assumption.)

This is what makes Oracle EVOLVE past an institutional desk: it breeds new
trading strategies and keeps only those that survive rigorous validation.

The evolutionary loop (all on real data, no lookahead):
 1. SEED a population of random + Atlas-informed genomes.
 2. EVALUATE each genome via WALK-FORWARD backtest on real history; fitness =
    risk-adjusted (Sharpe-weighted return, penalized by drawdown).
 3. SELECT tournament selection of the fittest.
 4. BREED crossover + mutation produce the next generation.
 5. CERTIFY a champion is promoted ONLY if it beats the incumbent AND passes
    OUT-OF-SAMPLE validation on a held-out period (guards overfitting).
 6. PRESERVE champions + their full rule DNA persist to disk and to Chronicle
    (auditable: every evolved rule is human-readable).

Overfitting guard: fitness uses in-sample; promotion requires separate
out-of-sample confirmation. A genome that only shines in-sample is discarded.
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
from core.backtester import Backtester  # type: ignore

log = logging.getLogger("oracle.evolution")


class EvolutionLab:
    def __init__(self, chronicle=None, atlas=None, storage_dir: str = "memory",
                 population: int = 16, seed: int = 7):
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

    # ---- fitness = risk-adjusted, in-sample ----

    def _fitness(self, genome: StrategyGenome, series) -> Dict[str, Any]:
        def decide(closes, highs, lows, **kwargs):  # FIX: added **kwargs
            class _S:
                pass
            s = _S()
            s.closes = closes; s.highs = highs; s.lows = lows
            return {"call": genome.call(s)}

        try:  # FIX: defensive try/except
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
            fitness = (ret * 2 + sharpe * 0.35 + sortino * 0.25 + recovery * 0.15
                       + consistency * 0.2) * (1 - min(dd, 0.9))
            return {"fitness": round(fitness, 4), "result": result}
        except Exception as exc:
            log.warning("Genome %s fitness eval failed: %s", genome.genome_id, exc)
            return {"fitness": -1.0, "result": {"status": "error", "message": str(exc)}}

    def _champion_key(self, symbol: str, regime: str) -> str:
        return f"{symbol.upper()}::{regime}"

    def _regime_for(self, series) -> str:
        try:
            return (analyze(series).get("regime") or {}).get("regime", "unknown")
        except Exception:
            return "unknown"

    # ---- the evolutionary run ----

    def evolve(self, series, generations: int = 5, planned_candidates: List[StrategyGenome] = None) -> Dict[str, Any]:
        symbol = series.symbol
        regime = self._regime_for(series)
        champion_key = self._champion_key(symbol, regime)
        split = int(len(series.bars) * 0.7)
        in_sample = _slice_series(series, 0, split)
        out_sample = _slice_series(series, split, len(series.bars))
        if len(in_sample.bars) < 70 or len(out_sample.bars) < 30:
            return {"status": "error", "message": "insufficient history to evolve safely"}

        population = [random_strategy(self.rng) for _ in range(self.population_size)]

        if planned_candidates:
            for i, cand in enumerate(planned_candidates[:self.population_size // 2]):
                population[i + 1] = cand

        if champion_key in self._champions:
            population[0] = StrategyGenome.from_dict(self._champions[champion_key]["genome"])

        history = []
        best = None
        for gen in range(generations):
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
            history.append({"generation": gen, "best_fitness": round(scored[0][1], 4),
                            "best_return": best.best_return})
            survivors = [g for g, _ in scored[:max(2, self.population_size // 3)]]
            next_pop = list(survivors[:2])
            while len(next_pop) < self.population_size:
                a = self._tournament(scored); b = self._tournament(scored)
                child = crossover_strategy(a, b, self.rng)
                child = mutate_strategy(child, self.rng)
                next_pop.append(child)
            population = next_pop

        # ---- CERTIFY the best on OUT-OF-SAMPLE ----
        oos = self._fitness(best, out_sample)
        incumbent = self._champions.get(champion_key)
        incumbent_oos = incumbent["out_of_sample"]["total_return"] if incumbent else None
        oos_return = oos["result"].get("total_return", -1.0)

        promoted = False
        if (oos["result"].get("status") == "complete" and oos_return > 0
                and oos["result"].get("trades", 0) >= 3
                and (incumbent_oos is None or oos_return > incumbent_oos)):
            self._champions[champion_key] = {"symbol": symbol, "regime": regime,
                                             "activation_rules": {"symbol": symbol, "regime": regime},
                                             "genome": best.to_dict(),
                                             "in_sample": {"return": best.best_return, "fitness": best.fitness},
                                             "out_of_sample": oos["result"],
                                             "failure_conditions": _failure_conditions(oos["result"]),
                                             "certified_at": time.time()}
            self._persist()
            promoted = True
            self._preserve(symbol, regime, best, oos["result"])

        return {"status": "complete", "symbol": symbol, "regime": regime, "generations": generations,
                "history": history, "best_genome": best.to_dict(),
                "in_sample_return": best.best_return,
                "out_of_sample": oos["result"], "promoted_new_champion": promoted,
                "champion": self._champions.get(champion_key, {}).get("genome", {}).get("genome_id")}

    def _tournament(self, scored, k: int = 3) -> StrategyGenome:
        contenders = self.rng.sample(scored, min(k, len(scored)))
        return max(contenders, key=lambda x: x[1])[0]

    # ---- champion access for live signals ----

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


def _slice_series(series, start: int, end: int):
    class _S:
        pass
    s = _S()
    bars = series.bars[start:end]
    s.symbol = series.symbol
    s.bars = bars
    s.closes = [b.close for b in bars]
    s.highs = [b.high for b in bars]
    s.lows = [b.low for b in bars]
    s.last = bars[-1].close if bars else None
    return s


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
