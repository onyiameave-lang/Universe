"""
Oracle.intelligence.scientific_lab (v2 - PATCHED)
=================================================
Fixes:
1. evolution_fn error handling - catches and logs exceptions instead of silent None
2. Handles evolution returning {"status": "error"} gracefully
3. Adds comprehensive logging at every stage for debugging
4. Fixes run_scientific_cycle to always return a complete result dict
"""
from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from intelligence.technicals import analyze
from intelligence.strategy_planner import StrategyPlanner

log = logging.getLogger("oracle.lab")


class StagnationDetector:
    """Multi-signal stagnation detection (6 signals, any 2 = stagnant)."""

    def detect(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        if len(history) < 3:
            return {"stagnant": False, "reasons": [], "severity": 0}

        fitness = [float(h.get("best_fitness", 0.0)) for h in history]
        reasons = []

        delta = max(fitness) - fitness[0]
        if delta < 0.01:
            reasons.append(f"fitness_plateau: total improvement only {delta:.4f}")

        tail = fitness[-3:]
        if max(tail) - min(tail) < 0.002:
            reasons.append("convergence: identical best fitness across last 3 generations")

        if max(fitness) < 0.1:
            reasons.append(f"low_absolute_fitness: best is {max(fitness):.4f}")

        if len(fitness) >= 4 and fitness[-1] < fitness[-3]:
            reasons.append("declining: fitness is dropping")

        best_returns = [h.get("best_return", 0) for h in history]
        if len(set(round(r, 6) for r in best_returns[-3:])) == 1:
            reasons.append("static_champion: same individual dominates")

        severity = len(reasons)
        return {
            "stagnant": severity >= 2,
            "reasons": reasons,
            "severity": severity,
            "best_fitness_delta": round(delta, 4),
            "fitness_path": fitness,
        }


class ChampionLibrary:
    """Specialized champions per regime + trading family."""

    def __init__(self, storage_path: Path):
        self._path = storage_path
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

    def key(self, symbol: str, regime: str, family: str = "general") -> str:
        return f"{symbol.upper()}::{regime}::{family}"

    def record(self, symbol, regime, family, genome, evidence, hypotheses, research=None):
        k = self.key(symbol, regime, family)
        self._champions[k] = {
            "symbol": symbol.upper(), "regime": regime, "family": family,
            "genome": genome, "evidence": evidence,
            "confidence": evidence.get("score", 0),
            "updated_at": time.time(),
        }
        self._persist()
        return k

    def get(self, symbol: str, regime: str = None, family: str = None):
        symbol = symbol.upper()
        if regime and family:
            return self._champions.get(self.key(symbol, regime, family))
        candidates = [c for c in self._champions.values() if c.get("symbol") == symbol]
        if regime:
            candidates = [c for c in candidates if c.get("regime") == regime]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.get("confidence", 0))

    def stats(self):
        return {"total_champions": len(self._champions), "keys": list(self._champions.keys())}


class ScientificResearchLab:
    """Oracle's research-first strategy discovery workflow."""

    def __init__(self, chronicle=None, atlas=None, storage_dir: str = "memory"):
        self.chronicle = chronicle
        self.atlas = atlas
        self.planner = StrategyPlanner()
        self.stagnation_detector = StagnationDetector()
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._journal_path = self._dir / "scientific_journal.json"
        self.champions = ChampionLibrary(self._dir / "champion_library.json")
        self._journal: List[Dict[str, Any]] = self._load_journal()

    def _load_journal(self) -> List:
        try:
            data = json.loads(self._journal_path.read_text()) if self._journal_path.exists() else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _persist_journal(self):
        try:
            self._journal_path.write_text(json.dumps(self._journal[-500:], indent=2))
        except Exception:
            pass

    def market_context(self, series) -> Dict[str, Any]:
        technicals = analyze(series)
        regime = (technicals.get("regime") or {}).get("regime", "unknown")
        return {
            "symbol": series.symbol,
            "regime": regime,
            "bars": len(series.bars),
            "last": series.last,
            "volatility": (technicals.get("regime") or {}).get("volatility", 0.0),
            "technicals": technicals,
        }

    def consult_memory(self, symbol: str, regime: str) -> Dict[str, Any]:
        if self.chronicle is None:
            return {"memories": [], "past_experiments": [], "known_failures": [], "memory_count": 0}
        try:
            memories = self.chronicle.search(
                query=f"{symbol} {regime} strategy champion experiment",
                domain="trading", limit=6, requester="oracle"
            ) or []
            known_failures = []
            past_experiments = []
            for m in memories:
                txt = (m.get("summary", "") if isinstance(m, dict) else str(m)).lower()
                if "rejected" in txt or "failed" in txt:
                    known_failures.append(txt)
                if "experiment" in txt:
                    past_experiments.append(txt)
            return {"memories": memories, "past_experiments": past_experiments,
                   "known_failures": known_failures, "memory_count": len(memories)}
        except Exception as exc:
            log.debug("Chronicle consult failed: %s", exc)
            return {"memories": [], "past_experiments": [], "known_failures": [], "memory_count": 0}

    def request_research(self, context: Dict[str, Any], memory: Dict[str, Any],
                        depth: str = "standard") -> Dict[str, Any]:
        symbol = context.get("symbol", "?")
        regime = context.get("regime", "unknown")
        query = (
            f"Research quantitative trading strategies for {symbol} in {regime} conditions. "
            f"Include strategy families, indicators, risk management, and failure modes."
        )
        if self.atlas is None:
            log.info("  Atlas not connected. Using regime-based planning.")
            return {"status": "unavailable", "query": query}
        try:
            if hasattr(self.atlas, "act"):
                result = self.atlas.act("research.investigate", {
                    "query": query, "domain": "financial_markets",
                    "depth": depth, "_sender": "oracle",
                })
                if result and result.get("status") != "error":
                    return result
                log.warning("  Atlas returned: %s", result)
                return result or {"status": "error", "message": "empty response"}
        except Exception as exc:
            log.error("  Atlas research failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        return {"status": "unavailable", "query": query}

    def generate_hypotheses(self, context, memory, research) -> List[Dict[str, Any]]:
        symbol = context["symbol"]
        regime = context["regime"]
        hypotheses = []

        # Regime-based hypotheses (always available)
        regime_hyps = {
            "trending_up": [("trend_following", "Trend continuation with momentum confirmation."),
                           ("momentum", "Strong momentum persistence creates profitable entries."),
                           ("breakout", "Volatility breakouts align with trend.")],
            "trending_down": [("failed_rally", "Shorting failed recovery attempts in bear trends."),
                             ("trend_following", "Bear trend continuation with trailing exits."),
                             ("momentum", "Negative momentum persistence is exploitable.")],
            "ranging": [("mean_reversion", "RSI extremes revert when range is intact."),
                       ("breakout", "Range compression precedes expansion."),
                       ("mean_reversion", "Bollinger extremes revert in low vol.")],
            "high_volatility": [("breakout", "Vol compression followed by expansion."),
                               ("volatility_trade", "Extreme vol creates sharp reversions.")],
        }
        for family, statement in regime_hyps.get(regime, regime_hyps["trending_up"]):
            hypotheses.append({
                "hypothesis_id": f"hyp-{uuid.uuid4().hex[:6]}",
                "symbol": symbol, "regime": regime,
                "family": family, "statement": statement,
                "source": "regime_knowledge",
            })

        # From Atlas research
        if research and research.get("status") not in ("error", "unavailable", None):
            research_text = json.dumps(research, default=str).lower()
            for family in ["trend_following", "mean_reversion", "breakout", "momentum", "failed_rally"]:
                if family.replace("_", " ") in research_text or family in research_text:
                    hypotheses.append({
                        "hypothesis_id": f"hyp-atlas-{uuid.uuid4().hex[:6]}",
                        "symbol": symbol, "regime": regime,
                        "family": family,
                        "statement": f"Atlas identified {family} as viable for {regime}.",
                        "source": "atlas_research",
                    })

        return hypotheses[:8]

    def run_scientific_cycle(
        self,
        series,
        evolution_fn: Callable[[Optional[List]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Complete research-first scientific cycle.
        PATCHED: comprehensive error handling at every stage.
        """
        # 1. Market context
        context = self.market_context(series)
        symbol = context["symbol"]
        regime = context["regime"]
        log.info("═══ Scientific Cycle: %s (%s) ═══", symbol, regime)

        # 2. Consult Chronicle
        memory = self.consult_memory(symbol, regime)
        log.info("  Chronicle: %d memories, %d past experiments, %d known failures",
                memory["memory_count"], len(memory["past_experiments"]), len(memory["known_failures"]))

        # 3. Request Atlas research (ALWAYS)
        research = self.request_research(context, memory)
        log.info("  Atlas research: %s", research.get("status", "unknown"))

        # 4. Generate hypotheses
        hypotheses = self.generate_hypotheses(context, memory, research)
        log.info("  Hypotheses: %d generated", len(hypotheses))

        # 5. Plan genomes from research + hypotheses
        try:
            research_candidates = self.planner.plan(research, symbol, regime)
            hypothesis_candidates = self.planner.plan_from_hypotheses(hypotheses, symbol, regime)
            all_candidates = research_candidates + hypothesis_candidates
            log.info("  Planned genomes: %d (research: %d, hypothesis: %d)",
                    len(all_candidates), len(research_candidates), len(hypothesis_candidates))
        except Exception as exc:
            log.error("  ❌ Strategy Planner FAILED: %s", exc)
            log.error("  %s", traceback.format_exc())
            all_candidates = []

        # 6. Run evolution (with FULL error handling)
        result = None
        try:
            log.info("  Starting evolution with %d planned candidates...", len(all_candidates))
            result = evolution_fn(all_candidates if all_candidates else None)
            log.info("  Evolution returned: status=%s, promoted=%s",
                    result.get("status"), result.get("promoted_new_champion"))
        except Exception as exc:
            log.error("  ❌ EVOLUTION FAILED with exception: %s", exc)
            log.error("  %s", traceback.format_exc())
            result = {
                "status": "error",
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "history": [],
                "best_genome": {},
                "in_sample_return": 0.0,
                "out_of_sample": {},
                "promoted_new_champion": False,
            }

        # Handle evolution error result
        if result is None:
            log.error("  ❌ evolution_fn returned None!")
            result = {"status": "error", "message": "evolution returned None",
                     "history": [], "best_genome": {}, "in_sample_return": 0.0,
                     "out_of_sample": {}, "promoted_new_champion": False}

        if result.get("status") == "error":
            log.error("  ❌ Evolution error: %s", result.get("message"))

        # 7. Stagnation detection
        stagnation = self.stagnation_detector.detect(result.get("history", []))
        if stagnation["stagnant"]:
            log.warning("  Stagnation: %s", stagnation["reasons"])

            # If stagnant AND first pass had candidates, try deeper research
            if research.get("status") not in ("error", "unavailable"):
                try:
                    deep_research = self.request_research(context, memory, depth="institutional")
                    if deep_research.get("status") not in ("error", "unavailable"):
                        deeper_candidates = self.planner.plan(deep_research, symbol, regime)
                        log.info("  Deep research: %d new candidates, retrying evolution...", len(deeper_candidates))
                        result = evolution_fn(deeper_candidates)
                        stagnation = self.stagnation_detector.detect(result.get("history", []))
                except Exception as exc:
                    log.error("  Deep research retry failed: %s", exc)

        # 8. Score evidence
        evidence = self._score_evidence(result)
        log.info("  Evidence: verdict=%s, score=%s", evidence["verdict"], evidence["score"])

        # 9. Record champion if accepted
        promoted = result.get("promoted_new_champion", False)
        if promoted or evidence["verdict"] == "accepted":
            best_genome = result.get("best_genome", {})
            family = self._determine_family(best_genome)
            self.champions.record(symbol=symbol, regime=regime, family=family,
                                 genome=best_genome, evidence=evidence,
                                 hypotheses=hypotheses, research=research)
            log.info("  ✅ Champion: %s::%s::%s", symbol, regime, family)

        # 10. Self-reflection
        reflection = self._self_reflect(context, evidence, stagnation, research, result)

        # 11. Record experiment
        experiment = {
            "experiment_id": f"exp-{uuid.uuid4().hex[:8]}",
            "created_at": time.time(),
            "symbol": symbol,
            "regime": regime,
            "hypotheses": hypotheses,
            "stagnation": stagnation,
            "evidence": evidence,
            "promoted": promoted or evidence["verdict"] == "accepted",
            "reflection": reflection,
        }
        self._journal.append(experiment)
        self._persist_journal()
        self._preserve_to_chronicle(experiment)

        # 12. Return COMPLETE result (never None)
        return {
            "status": "complete",
            "context": {k: v for k, v in context.items() if k != "technicals"},
            "hypotheses": hypotheses,
            "stagnation": stagnation,
            "research": {"status": research.get("status"), "families_found": len(research_candidates) if 'research_candidates' in dir() else 0},
            "experiment": experiment,
            "evolution": result,
            "reflection": reflection,
            "champion": self.champions.get(symbol, regime),
        }

    def _score_evidence(self, result: Dict[str, Any]) -> Dict[str, Any]:
        oos = result.get("out_of_sample") or {}
        total_return = float(oos.get("total_return", 0.0) or 0.0)
        drawdown = float(oos.get("max_drawdown", 1.0) or 1.0)
        sharpe = float(oos.get("sharpe_proxy", 0.0) or 0.0)
        win_rate = float(oos.get("win_rate", 0.0) or 0.0)
        trades = int(oos.get("trades", 0) or 0)
        robustness = 1.0 if oos.get("status") == "complete" and trades >= 3 else 0.0

        score = (total_return * 2.0 + sharpe * 0.5 + win_rate * 0.3 - drawdown * 2.0) * robustness
        verdict = "accepted" if score > 0.3 and total_return > 0 and trades >= 3 else "rejected"
        return {"score": round(score, 4), "verdict": verdict, "metrics": oos}

    def _determine_family(self, genome: Dict) -> str:
        mods = genome.get("modules", {})
        allowed = mods.get("market_regime", {}).get("params", {}).get("allowed_regimes", [])
        if "ranging" in allowed and len(allowed) == 1:
            return "mean_reversion"
        if "trending_down" in allowed and len(allowed) == 1:
            return "failed_rally"
        vol = mods.get("volatility", {}).get("params", {}).get("expansion_ratio", 1.0)
        if vol > 1.2:
            return "breakout"
        return "trend_following"

    def _self_reflect(self, context, evidence, stagnation, research, result):
        insights = []
        directives = []
        if evidence["verdict"] == "accepted":
            insights.append(f"Champion found for {context['regime']}.")
        else:
            insights.append(f"No champion. Best fitness: {result.get('history', [{}])[-1].get('best_fitness', 0):.4f}")
            if stagnation.get("stagnant"):
                directives.append("Inject new trading families")
            if result.get("status") == "error":
                directives.append(f"EVOLUTION ERROR: {result.get('message', 'unknown')}")
        return {"insights": insights, "directives": directives}

    def _preserve_to_chronicle(self, experiment):
        if not self.chronicle:
            return
        try:
            content = f"Oracle exp {experiment['experiment_id']} ({experiment['regime']}): {experiment['evidence']['verdict']}"
            if hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {"content": content, "domain": "trading", "source": "oracle"})
        except Exception:
            pass

    def champion_info(self, symbol, regime=None):
        return self.champions.get(symbol, regime)

    def stats(self):
        return {"experiments": len(self._journal), "champions": self.champions.stats()}
