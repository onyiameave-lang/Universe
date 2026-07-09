"""
Oracle.intelligence.evolution
============================
ARCHITECTURAL REDESIGN:

1. Genome DNA always preserved (immutable result objects)
2. Champion is PARENT ONLY, never re-evaluated as candidate
3. Diversity preservation (novelty, fitness sharing, random immigrants, convergence detection)
4. New genome IDs for every child (champion ID never reused)
5. Self-aware stagnation response (auto-increase mutation, inject randoms, swap families)
6. Candidate must outperform incumbent champion to replace
7. Clean pipeline: immutable data flows through each stage
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence.strategy_genome import (StrategyGenome, random_strategy, mutate_strategy, crossover_strategy)
from intelligence.strategy_library import (
    ALL_FAMILIES, TREND_INDICATORS, MOMENTUM_INDICATORS,
    VOLATILITY_FILTERS, EXIT_STRATEGIES, select_family, generate_diverse_population,
)
from intelligence.technicals import analyze
from intelligence.genome_validator import GenomeValidator, ValidationReport, EvolutionSummary
from intelligence.certification_auditor import CertificationAuditor
from core.backtester import Backtester

log = logging.getLogger("oracle.evolution")


# ═══════════════════════════════════════════════════════════════
# Immutable Series
# ═══════════════════════════════════════════════════════════════

class SlicedSeries:
    __slots__ = ('symbol', 'bars', 'source', '_closes', '_highs', '_lows')
    def __init__(self, symbol, bars, source="sliced"):
        self.symbol = symbol; self.bars = bars; self.source = source
        self._closes = [b.close for b in bars] if bars else []
        self._highs = [b.high for b in bars] if bars else []
        self._lows = [b.low for b in bars] if bars else []
    @property
    def closes(self): return self._closes
    @property
    def highs(self): return self._highs
    @property
    def lows(self): return self._lows
    @property
    def last(self): return self._closes[-1] if self._closes else None
    def __len__(self): return len(self.bars)
    def __iter__(self): return iter(self.bars)
    def __getitem__(self, idx): return self.bars[idx]


class BarView:
    __slots__ = ('open', 'high', 'low', 'close', 'volume', 'ts')
    def __init__(self, close, high, low):
        self.open = close; self.high = high; self.low = low
        self.close = close; self.volume = 0; self.ts = ""


def make_series(closes, highs, lows, symbol="unknown"):
    bars = [BarView(c, h, l) for c, h, l in zip(closes, highs, lows)]
    s = SlicedSeries(symbol, bars)
    s._closes = list(closes); s._highs = list(highs); s._lows = list(lows)
    return s


# ═══════════════════════════════════════════════════════════════
# Constrained parameters
# ═══════════════════════════════════════════════════════════════

PARAM_RANGES = {
    "fast": (5, 30), "slow": (20, 60), "period": (5, 50),
    "multiplier": (1.5, 5.0), "tenkan": (7, 12), "kijun": (20, 30),
    "lookback": (10, 30), "threshold": (15, 35),
    "upper": (60, 85), "lower": (15, 40),
    "k_period": (5, 21), "d_period": (2, 5),
    "base_threshold": (0.12, 0.35), "regime_bonus": (0.0, 0.15),
    "sl_mult": (1.0, 4.0), "tp_mult": (1.5, 8.0),
    "trail_mult": (1.0, 3.0), "expansion_ratio": (1.05, 1.6),
}

def clamp(value, param_name, data_length=200):
    if param_name not in PARAM_RANGES: return value
    lo, hi = PARAM_RANGES[param_name]
    if param_name in ("slow", "period", "lookback"): hi = min(hi, max(15, data_length - 15))
    if param_name == "fast": hi = min(hi, max(5, data_length - 25))
    if isinstance(value, int): return max(int(lo), min(int(hi), value))
    elif isinstance(value, float): return max(float(lo), min(float(hi), round(value, 3)))
    return value

def constrained_mutate_params(params, rng, rate=0.35, data_length=200):
    result = params.copy()
    for key, value in result.items():
        if rng.random() > rate: continue
        if isinstance(value, (int, float)):
            new_val = value * rng.uniform(0.85, 1.15) if value != 0 else rng.uniform(0.1, 1.0)
            result[key] = clamp(int(round(new_val)) if isinstance(value, int) else round(new_val, 3), key, data_length)
    return result

def adaptive_threshold(base, regime, volatility):
    adj = {"trending_up": -0.05, "trending_down": -0.05, "ranging": 0.0,
           "high_volatility": -0.03, "unknown": 0.0}.get(regime, 0.0)
    return max(0.08, min(0.40, base + adj + (-0.02 if volatility > 0.015 else 0.0)))


# ═══════════════════════════════════════════════════════════════
# FRESH ID GENERATION (Issue 2: never reuse champion ID)
# ═══════════════════════════════════════════════════════════════

def fresh_id(prefix: str = "gen") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


# ═══════════════════════════════════════════════════════════════
# DIVERSITY METRICS (Issue 3)
# ═══════════════════════════════════════════════════════════════

def structural_fingerprint(genome: StrategyGenome) -> str:
    """Unique structural fingerprint (ignoring parameters)."""
    return f"{genome.trend.logic_type}|{genome.momentum.logic_type}|{genome.volatility.logic_type}|{genome.exit.logic_type}"


def population_diversity(population: List[StrategyGenome]) -> float:
    """Measure diversity as ratio of unique structures."""
    if len(population) < 2: return 1.0
    fingerprints = set(structural_fingerprint(g) for g in population)
    return len(fingerprints) / len(population)


def structural_distance(a: StrategyGenome, b: StrategyGenome) -> float:
    score = 0.0
    if a.trend.logic_type != b.trend.logic_type: score += 3.0
    if a.momentum.logic_type != b.momentum.logic_type: score += 2.0
    if a.volatility.logic_type != b.volatility.logic_type: score += 1.5
    if a.exit.logic_type != b.exit.logic_type: score += 1.5
    return score


def novelty_score(genome: StrategyGenome, population: List[StrategyGenome]) -> float:
    """Reward genomes that are structurally different from the population."""
    if len(population) < 2: return 0.0
    distances = sorted([structural_distance(genome, o) for o in population if o is not genome], reverse=True)
    return sum(distances[:5]) / min(5, len(distances)) * 0.1 if distances else 0.0


def fitness_sharing(fitness: float, genome: StrategyGenome, population: List[StrategyGenome], sigma: float = 3.0) -> float:
    """Reduce fitness of genomes in crowded niches."""
    if len(population) < 2: return fitness
    niche_count = sum(1 for o in population if o is not genome and structural_distance(genome, o) < sigma)
    if niche_count > 2:
        return fitness / (1 + niche_count * 0.15)
    return fitness


# ═══════════════════════════════════════════════════════════════
# MUTATIONS (always produce fresh IDs)
# ═══════════════════════════════════════════════════════════════

def hierarchical_mutate(genome, rng, regime, data_length, mutation_pressure=1.0):
    """Always produces a NEW genome with a FRESH ID."""
    child = StrategyGenome.from_dict(genome.to_dict())
    child.generation += 1
    child.parents = [genome.genome_id]
    child.genome_id = fresh_id("mut")  # ALWAYS new ID

    # Scale rates by mutation pressure (increases during stagnation)
    family_rate = 0.08 * mutation_pressure
    module_rate = 0.22 * mutation_pressure

    roll = rng.random()
    if roll < family_rate:
        family = select_family(rng, regime)
        tmpl = family.template(rng, regime)
        _apply_template(child, tmpl, data_length)
        child.genome_id = fresh_id(family.name[:6])
        return child

    if roll < family_rate + module_rate:
        for mod_name in rng.sample(["trend", "momentum", "volatility", "exit"], rng.randint(1, 2)):
            pool = {"trend": TREND_INDICATORS, "momentum": MOMENTUM_INDICATORS,
                    "volatility": VOLATILITY_FILTERS, "exit": EXIT_STRATEGIES}[mod_name]
            new_mod = rng.choice(pool)
            mod = getattr(child, mod_name)
            mod.logic_type = new_mod["logic_type"]
            mod.params = {k: clamp(v, k, data_length) for k, v in new_mod["params"].items()}
        return child

    child.trend.params = constrained_mutate_params(child.trend.params, rng, 0.35 * mutation_pressure, data_length)
    child.momentum.params = constrained_mutate_params(child.momentum.params, rng, 0.30 * mutation_pressure, data_length)
    child.entry.params = constrained_mutate_params(child.entry.params, rng, 0.25 * mutation_pressure, data_length)
    child.exit.params = constrained_mutate_params(child.exit.params, rng, 0.30 * mutation_pressure, data_length)
    sl = child.exit.params.get("sl_mult", 2.0); tp = child.exit.params.get("tp_mult", 3.0)
    if tp <= sl: child.exit.params["tp_mult"] = round(sl * rng.uniform(1.3, 2.0), 1)
    return child


def _apply_template(genome, template, data_length):
    for key in ("trend", "momentum", "volatility", "entry", "exit"):
        if key in template:
            mod = getattr(genome, key)
            mod.logic_type = template[key].get("logic_type", mod.logic_type)
            mod.params = {k: clamp(v, k, data_length) for k, v in template[key].get("params", {}).items()}
    if "market_regime" in template:
        genome.market_regime.params = template["market_regime"]["params"].copy()


# ═══════════════════════════════════════════════════════════════
# STAGNATION DETECTION + AUTO-RESPONSE (Issue 6)
# ═══════════════════════════════════════════════════════════════

class StagnationDetector:
    """Detects convergence and adjusts mutation pressure automatically."""

    def __init__(self):
        self.mutation_pressure = 1.0
        self.last_best_fitness = None
        self.stagnant_gens = 0

    def check(self, best_fitness: float, diversity: float) -> Dict[str, Any]:
        """Returns action to take based on stagnation analysis."""
        action = {"stagnant": False, "pressure": self.mutation_pressure, "inject_randoms": 0}

        if self.last_best_fitness is not None:
            improvement = best_fitness - self.last_best_fitness
            if improvement < 0.01:
                self.stagnant_gens += 1
            else:
                self.stagnant_gens = max(0, self.stagnant_gens - 1)

        self.last_best_fitness = best_fitness

        # Low diversity = convergence
        if diversity < 0.3:
            self.mutation_pressure = min(3.0, self.mutation_pressure + 0.3)
            action["stagnant"] = True
            action["inject_randoms"] = 5
            log.info("STAGNATION: low diversity (%.2f). Pressure → %.1f, injecting %d randoms",
                     diversity, self.mutation_pressure, action["inject_randoms"])

        # Repeated stagnation
        elif self.stagnant_gens >= 3:
            self.mutation_pressure = min(3.0, self.mutation_pressure + 0.5)
            action["stagnant"] = True
            action["inject_randoms"] = 4
            log.info("STAGNATION: %d gens without improvement. Pressure → %.1f",
                     self.stagnant_gens, self.mutation_pressure)

        # Recovery: reduce pressure when things are working
        elif self.stagnant_gens == 0 and self.mutation_pressure > 1.0:
            self.mutation_pressure = max(1.0, self.mutation_pressure - 0.1)

        action["pressure"] = self.mutation_pressure
        return action


# ═══════════════════════════════════════════════════════════════
# FITNESS (multi-objective with sharing)
# ═══════════════════════════════════════════════════════════════

def compute_fitness(result, genome, population, data_length):
    trades = result.get("trades", 0)
    if result.get("status") != "complete": return -0.7
    if trades == 0: return -0.4
    adaptive_min = max(1, data_length // 40)
    trade_mult = 1.0 if trades >= adaptive_min else 0.3 + (trades / adaptive_min) * 0.5

    ret = result.get("total_return", 0.0)
    sharpe = result.get("sharpe_proxy", 0.0)
    sortino = result.get("sortino_proxy", 0.0)
    consistency = result.get("consistency", 0.0)
    dd = result.get("max_drawdown", 1.0)
    pf = result.get("profit_factor", 0.0) or 0.0
    expectancy = result.get("expectancy", 0.0) or 0.0

    raw = (ret * 2.0 + sharpe * 0.3 + sortino * 0.2 + consistency * 0.25 +
           min(pf, 3.0) / 3.0 * 0.15 + min(1.0, trades / 6.0) * 0.2 +
           max(0, expectancy) * 0.1)
    novelty = novelty_score(genome, population)
    dd_penalty = 1.0 - min(dd, 0.8)
    fitness = (raw + novelty) * dd_penalty * trade_mult

    # Fitness sharing to prevent niche crowding
    fitness = fitness_sharing(fitness, genome, population)
    return round(fitness, 4)


# ═══════════════════════════════════════════════════════════════
# CHAMPION COMPARISON (Issue 7)
# ═══════════════════════════════════════════════════════════════

def weighted_oos_score(oos: Dict) -> float:
    """Weighted multi-metric score for comparing candidates vs champion."""
    ret = float(oos.get("total_return", 0) or 0)
    sharpe = float(oos.get("sharpe_proxy", 0) or 0)
    pf = float(oos.get("profit_factor", 0) or 0)
    dd = float(oos.get("max_drawdown", 0) or 0)
    win = float(oos.get("win_rate", 0) or 0)
    trades = int(oos.get("trades", 0) or 0)
    return ret * 3.0 + sharpe * 1.5 + min(pf, 3.0) * 0.5 + win * 0.5 + min(trades, 10) * 0.1 - dd * 2.0


# ═══════════════════════════════════════════════════════════════
# MAIN EVOLUTION LAB
# ═══════════════════════════════════════════════════════════════

class EvolutionLab:
    def __init__(self, chronicle=None, atlas=None, storage_dir="memory", population=20, seed=7):
        self.chronicle = chronicle; self.atlas = atlas
        self.rng = random.Random(seed)
        self.population_size = population
        self.backtester = Backtester()
        self.validator = GenomeValidator()
        self.auditor = CertificationAuditor(backtester=self.backtester)
        self.stagnation = StagnationDetector()
        self._path = Path(storage_dir) / "evolved_strategies.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._champions: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try: self._champions = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception: self._champions = {}

    def _persist(self):
        try: self._path.write_text(json.dumps(self._champions, indent=2), encoding="utf-8")
        except Exception: pass

    def _regime_for(self, series):
        try: return (analyze(series).get("regime") or {}).get("regime", "unknown")
        except Exception: return "unknown"

    def _champion_key(self, symbol, regime): return f"{symbol.upper()}::{regime}"

    # ---- Population creation (Issue 2: champion is parent only) ----

    def _build_population(self, regime, data_length, champion_genome=None, seed_genomes=None):
        """
        Build population. Champion is used as PARENT material only.
        It is NEVER included directly (gets new children with fresh IDs).
        """
        templates = generate_diverse_population(self.rng, self.population_size, regime)
        population = []

        for tmpl in templates:
            g = StrategyGenome(genome_id=fresh_id("init"))
            _apply_template(g, tmpl, data_length)
            g.entry.params["base_threshold"] = round(self.rng.uniform(0.12, 0.28), 3)
            g.entry.params["threshold"] = g.entry.params["base_threshold"]
            g.market_regime.params["allowed_regimes"] = ["trending_up", "trending_down", "ranging", "high_volatility"]
            population.append(g)

        # Seed from champion + history (as MUTATED CHILDREN, not copies)
        seeds = seed_genomes or []
        if champion_genome:
            seeds = [champion_genome] + seeds

        for i, seed in enumerate(seeds[:self.population_size * 6 // 10]):
            if i < len(population):
                # Create a CHILD of the seed, not a copy
                child = StrategyGenome.from_dict(seed)
                child.genome_id = fresh_id("seed")  # Fresh ID!
                child.parents = [seed.get("genome_id", "unknown")]
                child.generation += 1
                # Light mutation to differentiate
                child = hierarchical_mutate(child, self.rng, regime, data_length, 0.8)
                population[i] = child

        return population

    # ---- Backtest ----

    def _backtest_genome(self, genome, series, warmup=None):
        symbol = getattr(series, 'symbol', 'unknown')
        n = len(series)
        if warmup is None: warmup = min(10, max(3, n - 12))
        if n < warmup + 20: warmup = max(2, n - 20)
        if n < 22: return {"status": "error", "message": f"Too short ({n})", "trades": 0, "total_return": 0.0}

        regime = self._regime_for(series)
        base_thresh = genome.entry.params.get("base_threshold", genome.entry.params.get("threshold", 0.25))
        try: vol = (analyze(series).get("regime") or {}).get("volatility", 0.01)
        except Exception: vol = 0.01
        eff_thresh = adaptive_threshold(base_thresh, regime, vol)

        def decide(closes, highs, lows, **kwargs):
            s = make_series(closes, highs, lows, symbol)
            v = genome.vote(s)
            if abs(v) >= eff_thresh: return {"call": "buy" if v > 0 else "sell"}
            return {"call": "hold"}

        try: return self.backtester.run(series, decide, warmup=warmup)
        except Exception as exc: return {"status": "error", "message": str(exc), "trades": 0, "total_return": 0.0}

    # ---- Fitness ----

    def _fitness(self, genome, series, population, collect_reports=None):
        result = self._backtest_genome(genome, series)
        fitness = compute_fitness(result, genome, population, len(series))
        if collect_reports is not None:
            report = ValidationReport()
            report.genome_id = genome.genome_id
            report.final_score = fitness
            report.regime = self._regime_for(series)
            self.validator.diagnose_backtest(result, report)
            collect_reports.append(report.to_dict())
        return {"fitness": fitness, "result": result}

    # ---- Main evolution ----

    def evolve(self, series, generations=5, planned_candidates=None):
        symbol = series.symbol
        regime = self._regime_for(series)
        champion_key = self._champion_key(symbol, regime)
        total_bars = len(series.bars)

        split = int(total_bars * 0.60)
        split = max(40, min(split, total_bars - 25))
        in_sample = _slice_series(series, 0, split)
        out_sample = _slice_series(series, split, total_bars)

        log.info("EVOLUTION: %s | %s | total=%d IS=%d OOS=%d", symbol, regime, total_bars, len(in_sample), len(out_sample))

        if len(in_sample) < 35 or len(out_sample) < 12:
            return {"status": "error", "message": f"insufficient: IS={len(in_sample)} OOS={len(out_sample)}"}

        data_length = len(in_sample)

        # Get current champion genome (for SEEDING only, not evaluation)
        incumbent = self._champions.get(champion_key)
        champion_genome = incumbent.get("genome") if incumbent else None

        # Build population (champion becomes parent material with fresh IDs)
        seed_genomes = []
        if planned_candidates:
            seed_genomes = [c.to_dict() if hasattr(c, 'to_dict') else c for c in planned_candidates]

        population = self._build_population(regime, data_length, champion_genome, seed_genomes)

        # Pre-repair
        for g in population: self.validator.repair_genome(g, data_length)

        history = []; all_reports = []; best = None

        for gen in range(generations):
            scored = []; gen_reports = []

            for g in population:
                fit = self._fitness(g, in_sample, population, collect_reports=gen_reports)
                g.fitness = fit["fitness"]
                g.best_return = fit["result"].get("total_return", 0.0)
                g.best_sharpe = fit["result"].get("sharpe_proxy", 0.0)
                g.backtests += 1
                scored.append((g, fit["fitness"]))

            scored.sort(key=lambda x: x[1], reverse=True)
            best = scored[0][0]

            # Diversity measurement
            diversity = population_diversity(population)
            avg_trades = sum(r.get("total_trades", 0) for r in gen_reports) / max(len(gen_reports), 1)
            zero_pct = sum(1 for r in gen_reports if r.get("total_trades", 0) == 0) / max(len(gen_reports), 1) * 100

            # Stagnation detection + auto-response (Issue 6)
            stag_action = self.stagnation.check(scored[0][1], diversity)

            history.append({
                "generation": gen, "best_fitness": round(scored[0][1], 4),
                "best_return": best.best_return, "diversity": round(diversity, 3),
                "avg_trades": round(avg_trades, 1), "zero_trade_pct": round(zero_pct, 0),
                "mutation_pressure": round(stag_action["pressure"], 2),
                "best_family": best.trend.logic_type,
            })

            log.info("Gen %d: best=%.4f div=%.2f trades=%.1f pressure=%.1f [%s]",
                     gen, scored[0][1], diversity, avg_trades, stag_action["pressure"],
                     best.trend.logic_type)

            if gen == generations - 1: all_reports = gen_reports

            # ---- Build next generation ----
            survivors = [g for g, _ in scored[:max(3, self.population_size // 3)]]
            next_pop = list(survivors[:2])  # Only top 2 elites (with THEIR existing IDs)

            # Inject random immigrants if stagnant
            randoms_to_inject = stag_action.get("inject_randoms", 0)
            for _ in range(randoms_to_inject):
                family = select_family(self.rng, regime)
                tmpl = family.template(self.rng, regime)
                immigrant = StrategyGenome(genome_id=fresh_id("imm"))
                _apply_template(immigrant, tmpl, data_length)
                immigrant.entry.params["base_threshold"] = round(self.rng.uniform(0.10, 0.25), 3)
                immigrant.entry.params["threshold"] = immigrant.entry.params["base_threshold"]
                immigrant.market_regime.params["allowed_regimes"] = ["trending_up", "trending_down", "ranging", "high_volatility"]
                next_pop.append(immigrant)

            # Fill remaining with crossover + mutation (all get FRESH IDs)
            while len(next_pop) < self.population_size:
                a = scored[self.rng.randint(0, min(5, len(scored)-1))][0]
                b = scored[self.rng.randint(0, min(8, len(scored)-1))][0]
                child = crossover_strategy(a, b, self.rng)
                child.genome_id = fresh_id("xov")  # Fresh ID!
                child = hierarchical_mutate(child, self.rng, regime, data_length, stag_action["pressure"])
                self.validator.repair_genome(child, data_length)
                next_pop.append(child)

            population = next_pop

        # ═══════════════════════════════════════════════════════
        # CERTIFICATION + CHAMPION COMPARISON (Issue 7)
        # ═══════════════════════════════════════════════════════
        log.info("=" * 60)
        log.info("CERTIFICATION: %s (OOS=%d bars)", best.genome_id, len(out_sample))

        oos_result = self._backtest_genome(best, out_sample, warmup=min(8, len(out_sample) - 12))
        oos_trades = oos_result.get("trades", 0) or 0
        oos_return = oos_result.get("total_return", 0.0) or 0.0
        oos_min_trades = max(1, len(out_sample) // 30)

        promoted = False
        comparison = None

        if oos_result.get("status") == "complete" and oos_return > 0 and oos_trades >= oos_min_trades:
            # Compare against incumbent (Issue 7)
            candidate_score = weighted_oos_score(oos_result)
            if incumbent:
                incumbent_score = weighted_oos_score(incumbent.get("out_of_sample", {}))
                comparison = {
                    "candidate_score": round(candidate_score, 4),
                    "incumbent_score": round(incumbent_score, 4),
                    "candidate_id": best.genome_id,
                    "incumbent_id": incumbent.get("genome", {}).get("genome_id", "none"),
                }
                if candidate_score > incumbent_score:
                    comparison["decision"] = "promote"
                    promoted = True
                    log.info("COMPARISON: candidate %.4f > incumbent %.4f → PROMOTE",
                             candidate_score, incumbent_score)
                else:
                    comparison["decision"] = "keep_incumbent"
                    log.info("COMPARISON: incumbent %.4f >= candidate %.4f → KEEP",
                             incumbent_score, candidate_score)
            else:
                comparison = {"decision": "promote", "reason": "no incumbent"}
                promoted = True
        else:
            log.info("CERTIFICATION FAILED: status=%s return=%.4f trades=%d (min=%d)",
                     oos_result.get("status"), oos_return, oos_trades, oos_min_trades)

        # Promote if approved
        if promoted:
            self._champions[champion_key] = {
                "symbol": symbol, "regime": regime,
                "genome": best.to_dict(),  # ISSUE 1: Full genome preserved
                "in_sample": {"return": best.best_return, "fitness": best.fitness},
                "out_of_sample": oos_result,
                "certified_at": time.time(),
                "generation": best.generation,
                "parents": best.parents,
            }
            self._persist()
            log.info("✅ CHAMPION PROMOTED: %s (OOS=%.4f, trades=%d)",
                     best.genome_id, oos_return, oos_trades)

        return {
            "status": "complete", "symbol": symbol, "regime": regime,
            "generations": generations, "history": history,
            "best_genome": best.to_dict(),  # ISSUE 1: Always full genome
            "in_sample_return": best.best_return,
            "out_of_sample": oos_result,
            "promoted_new_champion": promoted,
            "champion": self._champions.get(champion_key, {}).get("genome", {}).get("genome_id"),
            "champion_comparison": comparison,
            "validation_summary": self._build_summary(all_reports, promoted).to_dict(),
            "diversity_final": round(population_diversity(population), 3),
            "mutation_pressure": round(self.stagnation.mutation_pressure, 2),
        }

    def _build_summary(self, reports, promoted):
        summary = EvolutionSummary()
        summary.total_genomes = len(reports); summary.champion_promoted = promoted
        for r in reports:
            if r.get("structure_valid", True): summary.valid_genomes += 1
            if r.get("total_trades", 0) >= 1: summary.backtested_genomes += 1
            else: summary.rejected_zero_trades += 1
            if r.get("fitness_breakdown", {}).get("final_score", r.get("final_score", -1)) > 0:
                summary.certified_candidates += 1
            else: summary.rejected_low_fitness += 1
        return summary

    def champion(self, symbol, regime=None):
        rec = self.champion_info(symbol, regime)
        return StrategyGenome.from_dict(rec["genome"]) if rec else None

    def champion_info(self, symbol, regime=None):
        symbol = symbol.upper()
        if regime: return self._champions.get(self._champion_key(symbol, regime))
        candidates = [c for c in self._champions.values() if c.get("symbol", "").upper() == symbol]
        if not candidates: return self._champions.get(symbol)
        return max(candidates, key=lambda c: c.get("out_of_sample", {}).get("total_return", -999))

    def stats(self):
        return {"champion_keys": list(self._champions.keys()),
                "champions": {s: {"id": c.get("genome", {}).get("genome_id"),
                                   "return": c.get("out_of_sample", {}).get("total_return")}
                              for s, c in self._champions.items()},
                "mutation_pressure": round(self.stagnation.mutation_pressure, 2)}


def _slice_series(series, start, end):
    return SlicedSeries(series.symbol, series.bars[start:end])
