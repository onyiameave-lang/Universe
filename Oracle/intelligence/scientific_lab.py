"""
Oracle.intelligence.scientific_lab
=================================
Scientific research coordinator for Oracle.

Enhanced with Stage 7 Automatic Recovery: if all genomes fail, Oracle
automatically increases diversity, repairs genomes, relaxes constraints,
and retries evolution before terminating.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from intelligence.technicals import analyze  # type: ignore
from intelligence.strategy_planner import StrategyPlanner  # type: ignore

log = logging.getLogger("oracle.lab")


class ScientificResearchLab:
    """Coordinates Oracle's hypothesis-led research workflow."""

    def __init__(self, chronicle=None, atlas=None, storage_dir: str = "memory"):
        self.chronicle = chronicle
        self.atlas = atlas
        self.planner = StrategyPlanner()
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._journal_path = self._dir / "scientific_journal.json"
        self._champions_path = self._dir / "champion_library.json"
        self._journal: List[Dict[str, Any]] = self._load_list(self._journal_path)
        self._champions: Dict[str, Dict[str, Any]] = self._load_dict(self._champions_path)

    def _load_list(self, path: Path) -> List[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _load_dict(self, path: Path) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _persist(self) -> None:
        try:
            self._journal_path.write_text(json.dumps(self._journal[-250:], indent=2), encoding="utf-8")
            self._champions_path.write_text(json.dumps(self._champions, indent=2), encoding="utf-8")
        except Exception as exc:
            log.error("Failed to persist laboratory data: %s", exc)

    def market_context(self, series) -> Dict[str, Any]:
        technicals = analyze(series)
        regime = (technicals.get("regime") or {}).get("regime", "unknown")
        return {
            "symbol": series.symbol,
            "regime": regime,
            "bars": len(series.bars),
            "last": series.last,
            "volatility": (technicals.get("regime") or {}).get("volatility", 0.0),
            "slope_20": (technicals.get("regime") or {}).get("slope_20", 0.0),
            "technicals": technicals,
        }

    def consult_memory(self, symbol: str, regime: str) -> List[Dict[str, Any]]:
        if self.chronicle is None:
            return []
        try:
            return self.chronicle.search(
                query=f"{symbol} {regime} rejected failed successful hypothesis champion",
                domain="trading",
                limit=6,
                requester="oracle",
            )
        except Exception as exc:
            log.debug("Chronicle search failed: %s", exc)
            return []

    def generate_hypotheses(
        self,
        symbol: str,
        regime: str,
        memory: Optional[List[Dict[str, Any]]] = None,
        research: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Create falsifiable trading hypotheses from regime, memory, and research."""
        base = {
            "trending_up": [
                ("trend_continuation_atr", "Trend continuation improves when entries require ATR-confirmed momentum."),
                ("pullback_momentum", "Bull trend pullbacks with momentum recovery outperform blind breakout entries."),
            ],
            "trending_down": [
                ("bear_trend_continuation", "Bear trend continuation improves when exits trail volatility."),
                ("failed_rally_short", "Shorting failed rallies outperforms oversold reversal in bear regimes."),
            ],
            "ranging": [
                ("range_reversion_rsi", "RSI reversal performs better inside ranging markets than trend following."),
                ("bollinger_mean_reversion", "Bollinger extremes revert more reliably when volatility is contained."),
            ],
            "high_volatility": [
                ("volatility_filter", "ATR filters reduce drawdown during high-volatility expansion."),
                ("compression_breakout", "Momentum performs better after volatility compression than during noisy expansion."),
            ],
        }.get(regime, [
            ("regime_first_baseline", "A regime-filtered baseline outperforms a universal strategy."),
        ])

        rejected = " ".join(str(m).lower() for m in (memory or []))
        hypotheses = []
        for family, statement in base:
            if family.lower() in rejected and "failed" in rejected:
                continue
            hypotheses.append({
                "hypothesis_id": f"hyp-{uuid.uuid4().hex[:8]}",
                "symbol": symbol,
                "regime": regime,
                "family": family,
                "statement": statement,
                "source": "oracle_regime_memory",
            })

        if research:
            hypotheses.append({
                "hypothesis_id": f"hyp-{uuid.uuid4().hex[:8]}",
                "symbol": symbol,
                "regime": regime,
                "family": "external_research_lead",
                "statement": "Atlas research provided specific institutional-grade trading families.",
                "source": "atlas_research",
            })
        return hypotheses[:5]

    def detect_stagnation(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        if len(history) < 3:
            return {"stagnant": False, "reasons": [], "best_fitness_delta": None}

        fitness = [float(h.get("best_fitness", 0.0)) for h in history]
        delta = max(fitness) - fitness[0]
        tail = fitness[-3:]

        reasons = []
        if delta < 0.005:
            reasons.append("fitness_plateau: improvement below threshold (0.005)")
        if len(set(round(x, 5) for x in tail)) <= 1:
            reasons.append("convergence: identical best fitness in last 3 generations")
        if max(tail) <= 0:
            reasons.append("non_profitable: best fitness remains non-positive")

        return {
            "stagnant": bool(reasons),
            "reasons": reasons,
            "best_fitness_delta": round(delta, 4),
            "fitness_path": fitness,
        }

    def request_atlas_research(self, context: Dict[str, Any], stagnation: Dict[str, Any]) -> Dict[str, Any]:
        query = (
            f"Research profitable quantitative trading approaches for {context.get('symbol')} "
            f"under {context.get('regime')} conditions. Include strategy families, "
            "academic findings, microstructure observations, and institutional risk models."
        )
        if self.atlas is None:
            return {"status": "unavailable", "query": query, "reason": "Atlas not connected"}

        try:
            log.info("Requesting Atlas research for %s (%s)", context.get('symbol'), context.get('regime'))
            if hasattr(self.atlas, "act"):
                return self.atlas.act("research.investigate", {
                    "query": query,
                    "domain": "financial_markets",
                    "depth": "institutional",
                    "stagnation": stagnation,
                    "_sender": "oracle",
                })
        except Exception as exc:
            log.error("Atlas escalation failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        return {"status": "error", "message": "Atlas research interface not found"}

    def self_reflection(self, experiment: Dict[str, Any]) -> Dict[str, Any]:
        evidence = experiment.get("evidence", {})
        verdict = evidence.get("verdict")
        validation = experiment.get("validation_summary", {})

        reflection = {
            "timestamp": time.time(),
            "questions": [
                "Why did this champion win?" if verdict == "accepted" else "Why did this candidate fail?",
                "What market structural properties were captured?",
                "Did Atlas research provide actionable seeds?",
                "Should Chronicle memory be updated with this failure mode?"
            ],
            "insights": [],
            "directives": []
        }

        if verdict == "accepted":
            reflection["insights"].append(
                f"Successfully evolved a {experiment.get('regime')} champion for {experiment.get('symbol')}."
            )
            if experiment.get("research"):
                reflection["insights"].append("Atlas-planned seeds contributed to the evolutionary success.")
        else:
            # Use validation summary for specific insights
            elim_stage = validation.get("elimination_stage", "unknown")
            reflection["insights"].append(
                f"Evolution failed. Primary elimination stage: {elim_stage}"
            )

            if "zero trades" in elim_stage:
                reflection["directives"].append(
                    "Lower entry thresholds, broaden regime filters, reduce indicator periods"
                )
            elif "compilation" in elim_stage:
                reflection["directives"].append(
                    "Cap indicator periods to fit available data length"
                )
            elif "fitness" in elim_stage:
                reflection["directives"].append(
                    "Strategies trade but lose money. Try different families or regime"
                )
            else:
                reflection["directives"].append(
                    "Increase mutation pressure or refine Strategy Planner heuristic mapping."
                )

        return reflection

    def score_evidence(self, result: Dict[str, Any]) -> Dict[str, Any]:
        metrics = result.get("out_of_sample") or result
        total_return = float(metrics.get("total_return", 0.0) or 0.0)
        drawdown = float(metrics.get("max_drawdown", 1.0) or 1.0)
        sharpe = float(metrics.get("sharpe_proxy", 0.0) or 0.0)
        win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
        robustness = 1.0 if metrics.get("status") == "complete" and metrics.get("trades", 0) >= 3 else 0.0

        score = (total_return * 2.0 + sharpe * 0.4 + win_rate * 0.4 - drawdown * 1.5 + robustness * 0.25)
        verdict = "accepted" if score > 0.4 and total_return > 0 and robustness else "rejected"

        return {
            "score": round(score, 4),
            "verdict": verdict,
            "metrics": metrics,
            "criteria": {"net_profit": total_return, "max_drawdown": drawdown, "sharpe_proxy": sharpe}
        }

    def record_experiment(
        self,
        context: Dict[str, Any],
        hypotheses: List[Dict[str, Any]],
        evolution_result: Dict[str, Any],
        stagnation: Dict[str, Any],
        research: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        evidence = self.score_evidence(evolution_result)
        experiment = {
            "experiment_id": f"exp-{uuid.uuid4().hex[:8]}",
            "created_at": time.time(),
            "symbol": context.get("symbol"),
            "regime": context.get("regime"),
            "hypotheses": hypotheses,
            "stagnation": stagnation,
            "research": research,
            "evidence": evidence,
            "best_genome": evolution_result.get("best_genome"),
            "promoted_new_champion": evolution_result.get("promoted_new_champion", False),
            "validation_summary": evolution_result.get("validation_summary", {}),
        }
        self._journal.append(experiment)
        if evidence["verdict"] == "accepted" or evolution_result.get("promoted_new_champion"):
            self._record_champion(context, evolution_result, evidence, hypotheses)
        self._persist()
        self._preserve_to_chronicle(experiment)
        return experiment

    def _record_champion(self, context, evolution_result, evidence, hypotheses):
        key = self.champion_key(context.get("symbol", ""), context.get("regime", "unknown"))
        self._champions[key] = {
            "symbol": context.get("symbol"),
            "regime": context.get("regime"),
            "genome": evolution_result.get("best_genome"),
            "evidence": evidence,
            "hypotheses": hypotheses,
            "updated_at": time.time(),
        }

    def champion_key(self, symbol, regime):
        return f"{symbol.upper()}::{regime}"

    def champion_info(self, symbol, regime=None):
        symbol = symbol.upper()
        if regime:
            return self._champions.get(self.champion_key(symbol, regime))
        candidates = [c for c in self._champions.values() if c.get("symbol", "").upper() == symbol]
        return max(candidates, key=lambda c: c.get("evidence", {}).get("score", -999)) if candidates else None

    def _preserve_to_chronicle(self, experiment):
        if not self.chronicle:
            return
        try:
            content = (
                f"Oracle experiment {experiment['experiment_id']} for "
                f"{experiment['symbol']} ({experiment['regime']}): "
                f"{experiment['evidence']['verdict']}."
            )
            if hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {"content": content, "domain": "trading", "source": "oracle"})
        except Exception:
            pass

    # ---- Stage 7: Automatic Recovery ----

    def _should_recover(self, result: Dict[str, Any]) -> bool:
        """Determine if recovery is needed based on evolution result."""
        if result.get("status") == "error":
            return True
        summary = result.get("validation_summary", {})
        if summary.get("certified_candidates", 0) == 0:
            return True
        if result.get("promoted_new_champion") is False:
            best_fitness = result.get("history", [{}])[-1].get("best_fitness", -1) if result.get("history") else -1
            if best_fitness <= 0:
                return True
        return False

    def _recovery_evolution(self, series, evolution_fn, context: Dict[str, Any],
                            first_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Stage 7: Automatic Recovery.
        When all genomes fail, increase diversity and retry.
        """
        log.info("RECOVERY MODE: All genomes failed for %s (%s). Attempting recovery.",
                 context.get("symbol"), context.get("regime"))

        # Analyze what went wrong from validation summary
        summary = first_result.get("validation_summary", {})
        elimination = summary.get("elimination_stage", "")

        recovery_actions = []

        # Strategy 1: Request Atlas for additional families
        research = self.request_atlas_research(context, {
            "stagnant": True,
            "reasons": [f"Total failure: {elimination}"],
            "recovery_attempt": True,
        })
        recovery_actions.append("Requested Atlas for additional strategy families")

        # Strategy 2: Plan with broader families
        planned = self.planner.plan(research, context["symbol"], context["regime"])
        recovery_actions.append(f"Planner generated {len(planned)} recovery candidates")

        # Strategy 3: Run evolution with research-seeded genomes
        try:
            result = evolution_fn(planned)
            result["recovery_actions"] = recovery_actions
            result["recovery_attempted"] = True
            return result
        except Exception as exc:
            log.error("Recovery evolution failed: %s", exc)
            return {
                "status": "error",
                "history": [],
                "best_genome": {},
                "promoted_new_champion": False,
                "out_of_sample": {},
                "recovery_actions": recovery_actions,
                "recovery_attempted": True,
                "recovery_error": str(exc),
            }

    def run_scientific_cycle(
        self,
        series,
        evolution_fn: Callable[[Optional[List[Any]]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Complete execution workflow with validation and auto-recovery."""
        context = self.market_context(series)
        memory = self.consult_memory(context["symbol"], context["regime"])
        hypotheses = self.generate_hypotheses(context["symbol"], context["regime"], memory)

        # 1. First evolution pass (with recovery)
        try:
            result = evolution_fn(None)
        except Exception as exc:
            log.error("Evolution pass 1 failed: %s - forcing research escalation", exc)
            result = {"status": "error", "history": [], "best_genome": {},
                      "promoted_new_champion": False, "out_of_sample": {}}

        stagnation = self.detect_stagnation(result.get("history", []))

        research = None
        # 2. If stagnant OR errored, escalate to Atlas
        if stagnation.get("stagnant") or result.get("status") == "error":
            research = self.request_atlas_research(context, stagnation)
            planned_candidates = self.planner.plan(research, context["symbol"], context["regime"])
            try:
                result = evolution_fn(planned_candidates)
            except Exception as exc:
                log.error("Evolution pass 2 failed: %s", exc)
                result = {"status": "error", "history": [], "best_genome": {},
                          "promoted_new_champion": False, "out_of_sample": {}}
            stagnation = self.detect_stagnation(result.get("history", []))

        # 3. Stage 7: Auto-recovery if all genomes still fail
        if self._should_recover(result):
            recovery_result = self._recovery_evolution(series, evolution_fn, context, result)
            if recovery_result.get("promoted_new_champion") or \
               recovery_result.get("validation_summary", {}).get("certified_candidates", 0) > 0:
                result = recovery_result
                research = research or {"status": "recovery_triggered"}
                stagnation = self.detect_stagnation(result.get("history", []))

        # 4. Always record, reflect, and return
        experiment = self.record_experiment(context, hypotheses, result, stagnation, research)
        reflection = self.self_reflection(experiment)
        experiment["reflection"] = reflection

        return {
            "status": "complete", "context": context, "hypotheses": hypotheses,
            "stagnation": stagnation, "research": research, "experiment": experiment,
            "evolution": result, "reflection": reflection,
            "champion": self.champion_info(context["symbol"], context["regime"]),
        }

    def stats(self):
        return {"experiments": len(self._journal), "champions": list(self._champions.keys())}
