"""
Oracle.benchmark.benchmark_engine
=================================
Automatic Benchmark Engine for Oracle V1.

Observer-only: records what Oracle does without modifying any existing system.
Updates benchmark.json, appends benchmark_history.json, regenerates BENCHMARKS.md
after every completed experiment. Fault-tolerant, incremental, sub-second.

Usage:
    from benchmark.benchmark_engine import BenchmarkEngine
    engine = BenchmarkEngine(storage_dir="benchmark")
    engine.record_experiment(experiment_result)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.benchmark")

ORACLE_VERSION = "1.0.0"


def _atomic_write(path: Path, content: str):
    """Write atomically: write to temp file then rename."""
    try:
        dir_path = path.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(path))
    except Exception as exc:
        log.warning("Atomic write failed for %s: %s. Falling back to direct write.", path, exc)
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as exc:
            log.error("fallback direct write also failed for %s: %s", path, exc)


def _safe_load_json(path: Path, default):
    """Load JSON with corruption recovery."""
    if not path.exists():
        return default() if callable(default) else default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if callable(default):
            expected_type = type(default())
            if not isinstance(data, expected_type):
                return default()
        return data
    except (json.JSONDecodeError, Exception) as exc:
        log.warning("BENCHMARK: corrupted %s, reinitializing. Error: %s", path.name, exc)
        return default() if callable(default) else default


class BenchmarkEngine:
    """
    Automatic benchmark tracking for Oracle V1.
    Observer-only. Does not modify evolution, Chronicle, Atlas, or Forge.
    """

    def __init__(self, storage_dir: str = "benchmark"):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._benchmark_path = self._dir / "benchmark.json"
        self._history_path = self._dir / "benchmark_history.json"
        self._markdown_path = self._dir / "BENCHMARKS.md"
        self._data: Dict[str, Any] = self._load_or_init()
        self._history: List[Dict[str, Any]] = _safe_load_json(self._history_path, list)
        log.info("BENCHMARK loaded: %d experiments tracked", self._data.get("total_experiments", 0))

    def _load_or_init(self) -> Dict[str, Any]:
        """Load benchmark.json or initialize with defaults."""
        data = _safe_load_json(self._benchmark_path, dict)
        # Ensure all required fields exist
        defaults = self._default_benchmark()
        for key, val in defaults.items():
            if key not in data:
                data[key] = val
        return data

    def _default_benchmark(self) -> Dict[str, Any]:
        return {
            "oracle_version": ORACLE_VERSION,
            "last_updated": None,

            # Experiment counts
            "total_experiments": 0,
            "accepted_experiments": 0,
            "rejected_experiments": 0,
            "champions_promoted": 0,
            "champion_promotion_rate": 0.0,

            # Performance averages
            "average_is_return": 0.0,
            "average_oos_return": 0.0,
            "average_sharpe": 0.0,
            "average_drawdown": 0.0,
            "average_trades": 0.0,
            "average_fitness": 0.0,
            "best_fitness_ever": 0.0,
            "average_confidence": 0.0,
            "generalization_score": 0.0,

            # Runtime
            "average_runtime_seconds": 0.0,
            "total_runtime_seconds": 0.0,

            # Knowledge
            "knowledge_items": 0,
            "chronicle_entries": 0,
            "publications_generated": 0,
            "atlas_hypotheses_generated": 0,
            "failure_database_size": 0,

            # Research breakdown
            "by_market": {},
            "by_regime": {},
            "by_family": {},
            "by_trend_indicator": {},
            "by_momentum_indicator": {},
            "by_volatility_filter": {},

            # Oracle Intelligence Score
            "ois": {
                "overall": 0.0,
                "research_quality": 0.0,
                "champion_quality": 0.0,
                "knowledge_growth": 0.0,
                "generalization": 0.0,
                "evolution_efficiency": 0.0,
                "scientific_confidence": 0.0,
                "learning_progress": 0.0,
            },

            # Running sums for incremental averaging
            "_sum_is_return": 0.0,
            "_sum_oos_return": 0.0,
            "_sum_sharpe": 0.0,
            "_sum_drawdown": 0.0,
            "_sum_trades": 0.0,
            "_sum_fitness": 0.0,
            "_sum_runtime": 0.0,
        }

    # ═══════════════════════════════════════════════════════════════
    # PUBLIC: Record an experiment result
    # ═══════════════════════════════════════════════════════════════

    def record_experiment(self, result: Dict[str, Any], runtime_seconds: float = 0.0):
        """
        Record a completed experiment. Called automatically after every experiment.
        Updates all statistics incrementally. Sub-second operation.
        """
        start = time.time()

        try:
            self._update_counts(result)
            self._update_performance(result, runtime_seconds)
            self._update_breakdowns(result)
            self._update_knowledge(result)
            self._compute_ois()
            self._data["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._data["oracle_version"] = ORACLE_VERSION

            # Persist
            self._save_benchmark()
            self._append_history()
            self._regenerate_markdown()

            elapsed = time.time() - start
            log.info("BENCHMARK updated in %.3fs: %d experiments, OIS=%.1f",
                     elapsed, self._data["total_experiments"], self._data["ois"]["overall"])

        except Exception as exc:
            log.error("BENCHMARK update failed (non-fatal): %s", exc)

    # ═══════════════════════════════════════════════════════════════
    # Incremental statistics updates
    # ═══════════════════════════════════════════════════════════════

    def _update_counts(self, result: Dict):
        self._data["total_experiments"] += 1
        evo = result.get("evolution", result)
        promoted = evo.get("promoted_new_champion", False)
        if promoted:
            self._data["accepted_experiments"] += 1
            self._data["champions_promoted"] += 1
        else:
            self._data["rejected_experiments"] += 1

        total = self._data["total_experiments"]
        self._data["champion_promotion_rate"] = round(
            self._data["champions_promoted"] / max(total, 1), 4
        )

    def _update_performance(self, result: Dict, runtime: float):
        evo = result.get("evolution", result)
        oos = evo.get("out_of_sample", {})
        best_genome = evo.get("best_genome", {})

        is_return = float(evo.get("in_sample_return", 0) or 0)
        oos_return = float(oos.get("total_return", 0) or 0)
        sharpe = float(oos.get("sharpe_proxy", 0) or 0)
        drawdown = float(oos.get("max_drawdown", 0) or 0)
        trades = int(oos.get("trades", 0) or 0)
        fitness = float(best_genome.get("fitness", 0) or 0)

        # Running sums for incremental averages
        self._data["_sum_is_return"] += is_return
        self._data["_sum_oos_return"] += oos_return
        self._data["_sum_sharpe"] += sharpe
        self._data["_sum_drawdown"] += drawdown
        self._data["_sum_trades"] += trades
        self._data["_sum_fitness"] += fitness
        self._data["_sum_runtime"] += runtime

        n = self._data["total_experiments"]
        self._data["average_is_return"] = round(self._data["_sum_is_return"] / n, 6)
        self._data["average_oos_return"] = round(self._data["_sum_oos_return"] / n, 6)
        self._data["average_sharpe"] = round(self._data["_sum_sharpe"] / n, 4)
        self._data["average_drawdown"] = round(self._data["_sum_drawdown"] / n, 4)
        self._data["average_trades"] = round(self._data["_sum_trades"] / n, 2)
        self._data["average_fitness"] = round(self._data["_sum_fitness"] / n, 4)
        self._data["average_runtime_seconds"] = round(self._data["_sum_runtime"] / n, 2)
        self._data["total_runtime_seconds"] = round(self._data["_sum_runtime"], 2)

        if fitness > self._data["best_fitness_ever"]:
            self._data["best_fitness_ever"] = round(fitness, 4)

        # Generalization score: OOS/IS ratio (how well strategies generalize)
        if is_return > 0:
            gen = min(2.0, oos_return / max(is_return, 0.001))
            prev_gen = self._data["generalization_score"]
            # Exponential moving average
            self._data["generalization_score"] = round(prev_gen * 0.8 + gen * 0.2, 4)

    def _update_breakdowns(self, result: Dict):
        evo = result.get("evolution", result)
        context = result.get("context", {})
        best_genome = evo.get("best_genome", {})
        modules = best_genome.get("modules", {})
        promoted = evo.get("promoted_new_champion", False)

        symbol = context.get("symbol", evo.get("symbol", "unknown"))
        regime = context.get("regime", evo.get("regime", "unknown"))
        trend_type = modules.get("trend", {}).get("logic_type", "unknown")
        mom_type = modules.get("momentum", {}).get("logic_type", "unknown")
        vol_type = modules.get("volatility", {}).get("logic_type", "default")

        # Determine family from trend type
        family = trend_type

        for category, key in [
            ("by_market", symbol),
            ("by_regime", regime),
            ("by_family", family),
            ("by_trend_indicator", trend_type),
            ("by_momentum_indicator", mom_type),
            ("by_volatility_filter", vol_type),
        ]:
            bucket = self._data.setdefault(category, {})
            entry = bucket.setdefault(key, {"experiments": 0, "promoted": 0, "avg_fitness": 0, "_sum_fitness": 0})
            entry["experiments"] += 1
            if promoted:
                entry["promoted"] += 1
            fitness = float(best_genome.get("fitness", 0) or 0)
            entry["_sum_fitness"] += fitness
            entry["avg_fitness"] = round(entry["_sum_fitness"] / entry["experiments"], 4)

    def _update_knowledge(self, result: Dict):
        hypotheses = result.get("hypotheses", [])
        self._data["atlas_hypotheses_generated"] += len(hypotheses)

        evo = result.get("evolution", result)
        if not evo.get("promoted_new_champion", False):
            self._data["failure_database_size"] += 1
        else:
            self._data["knowledge_items"] += 1
            self._data["chronicle_entries"] += 1

    # ═══════════════════════════════════════════════════════════════
    # Oracle Intelligence Score (OIS)
    # ═══════════════════════════════════════════════════════════════

    def _compute_ois(self):
        """Compute OIS (0-100) from multiple dimensions."""
        d = self._data
        n = d["total_experiments"]
        if n == 0:
            return

        # Research Quality (0-100): promotion rate * consistency
        promotion_rate = d["champion_promotion_rate"]
        research_quality = min(100, promotion_rate * 200)  # 50% rate = 100

        # Champion Quality (0-100): average OOS return + Sharpe
        oos = d["average_oos_return"]
        sharpe = d["average_sharpe"]
        champion_quality = min(100, max(0, (oos * 500 + sharpe * 20)))

        # Knowledge Growth (0-100): knowledge items relative to experiments
        knowledge = d["knowledge_items"]
        knowledge_growth = min(100, (knowledge / max(n, 1)) * 200)

        # Generalization (0-100): how well IS transfers to OOS
        gen_score = d["generalization_score"]
        generalization = min(100, max(0, gen_score * 50))

        # Evolution Efficiency (0-100): fewer experiments needed per champion
        if d["champions_promoted"] > 0:
            experiments_per_champion = n / d["champions_promoted"]
            evolution_efficiency = min(100, max(0, 100 - (experiments_per_champion - 1) * 20))
        else:
            evolution_efficiency = 0

        # Scientific Confidence (0-100): based on trade count and consistency
        avg_trades = d["average_trades"]
        scientific_confidence = min(100, avg_trades * 10)

        # Learning Progress (0-100): improvement over time
        if len(self._history) >= 2:
            recent_ois = self._history[-1].get("ois", 0) if self._history else 0
            learning_progress = min(100, max(0, 50 + (research_quality - recent_ois)))
        else:
            learning_progress = 50

        # Overall OIS (weighted average)
        overall = (
            research_quality * 0.20 +
            champion_quality * 0.20 +
            knowledge_growth * 0.10 +
            generalization * 0.20 +
            evolution_efficiency * 0.10 +
            scientific_confidence * 0.10 +
            learning_progress * 0.10
        )

        d["ois"] = {
            "overall": round(overall, 1),
            "research_quality": round(research_quality, 1),
            "champion_quality": round(champion_quality, 1),
            "knowledge_growth": round(knowledge_growth, 1),
            "generalization": round(generalization, 1),
            "evolution_efficiency": round(evolution_efficiency, 1),
            "scientific_confidence": round(scientific_confidence, 1),
            "learning_progress": round(learning_progress, 1),
        }

    # ═══════════════════════════════════════════════════════════════
    # Persistence
    # ═══════════════════════════════════════════════════════════════

    def _save_benchmark(self):
        content = json.dumps(self._data, indent=2, default=str)
        _atomic_write(self._benchmark_path, content)
        log.debug("BENCHMARK saved")

    def _append_history(self):
        snapshot = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "experiments": self._data["total_experiments"],
            "champions": self._data["champions_promoted"],
            "avg_oos": self._data["average_oos_return"],
            "avg_sharpe": self._data["average_sharpe"],
            "avg_fitness": self._data["average_fitness"],
            "ois": self._data["ois"]["overall"],
            "generalization": self._data["generalization_score"],
            "knowledge": self._data["knowledge_items"],
        }
        self._history.append(snapshot)
        content = json.dumps(self._history[-500:], indent=2)  # Keep last 500
        _atomic_write(self._history_path, content)
        log.debug("BENCHMARK history appended (%d entries)", len(self._history))

    def _regenerate_markdown(self):
        d = self._data
        ois = d.get("ois", {})

        # Find best market/regime/family
        best_market = self._best_in_category("by_market")
        best_regime = self._best_in_category("by_regime")
        best_family = self._best_in_category("by_family")
        best_trend = self._best_in_category("by_trend_indicator")
        best_momentum = self._best_in_category("by_momentum_indicator")
        most_used = self._most_used_indicator()

        md = f"""# Oracle V1 Benchmarks

> Auto-generated. Do not edit manually.
> Last updated: {d.get('last_updated', 'never')}

## Summary

| Metric | Value |
|--------|-------|
| Oracle Version | {d['oracle_version']} |
| Total Experiments | {d['total_experiments']} |
| Accepted | {d['accepted_experiments']} |
| Rejected | {d['rejected_experiments']} |
| Champions Promoted | {d['champions_promoted']} |
| Promotion Rate | {d['champion_promotion_rate']:.1%} |

## Performance

| Metric | Value |
|--------|-------|
| Avg IS Return | {d['average_is_return']:.4f} |
| Avg OOS Return | {d['average_oos_return']:.4f} |
| Avg Sharpe | {d['average_sharpe']:.3f} |
| Avg Drawdown | {d['average_drawdown']:.4f} |
| Avg Trades | {d['average_trades']:.1f} |
| Avg Fitness | {d['average_fitness']:.4f} |
| Best Fitness Ever | {d['best_fitness_ever']:.4f} |
| Generalization Score | {d['generalization_score']:.4f} |

## Oracle Intelligence Score (OIS)

| Component | Score |
|-----------|-------|
| **Overall** | **{ois.get('overall', 0):.1f} / 100** |
| Research Quality | {ois.get('research_quality', 0):.1f} |
| Champion Quality | {ois.get('champion_quality', 0):.1f} |
| Knowledge Growth | {ois.get('knowledge_growth', 0):.1f} |
| Generalization | {ois.get('generalization', 0):.1f} |
| Evolution Efficiency | {ois.get('evolution_efficiency', 0):.1f} |
| Scientific Confidence | {ois.get('scientific_confidence', 0):.1f} |
| Learning Progress | {ois.get('learning_progress', 0):.1f} |

## Research Insights

| Category | Best |
|----------|------|
| Best Market | {best_market} |
| Best Regime | {best_regime} |
| Best Strategy Family | {best_family} |
| Best Trend Indicator | {best_trend} |
| Best Momentum Indicator | {best_momentum} |
| Most Used Indicator | {most_used} |

## Knowledge

| Metric | Value |
|--------|-------|
| Knowledge Items | {d['knowledge_items']} |
| Chronicle Entries | {d['chronicle_entries']} |
| Failure Database | {d['failure_database_size']} |
| Hypotheses Generated | {d['atlas_hypotheses_generated']} |

## Runtime

| Metric | Value |
|--------|-------|
| Avg Runtime | {d['average_runtime_seconds']:.1f}s |
| Total Runtime | {d['total_runtime_seconds']:.0f}s |
"""
        _atomic_write(self._markdown_path, md)
        log.debug("BENCHMARK markdown regenerated")

    def _best_in_category(self, category: str) -> str:
        bucket = self._data.get(category, {})
        if not bucket:
            return "none yet"
        best = max(bucket.items(), key=lambda x: x[1].get("promoted", 0) * 10 + x[1].get("avg_fitness", 0))
        return f"{best[0]} ({best[1].get('promoted', 0)} champions, fitness={best[1].get('avg_fitness', 0):.3f})"

    def _most_used_indicator(self) -> str:
        all_indicators = {}
        for cat in ("by_trend_indicator", "by_momentum_indicator"):
            for name, data in self._data.get(cat, {}).items():
                all_indicators[name] = all_indicators.get(name, 0) + data.get("experiments", 0)
        if not all_indicators:
            return "none yet"
        best = max(all_indicators.items(), key=lambda x: x[1])
        return f"{best[0]} ({best[1]} uses)"

    # ═══════════════════════════════════════════════════════════════
    # Public getters
    # ═══════════════════════════════════════════════════════════════

    def get_ois(self) -> float:
        return self._data.get("ois", {}).get("overall", 0.0)

    def get_summary(self) -> Dict[str, Any]:
        return {
            "experiments": self._data["total_experiments"],
            "champions": self._data["champions_promoted"],
            "promotion_rate": self._data["champion_promotion_rate"],
            "avg_oos": self._data["average_oos_return"],
            "ois": self._data["ois"]["overall"],
        }