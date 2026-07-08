"""
Chronicle.intelligence.improvement
==================================
Active knowledge improvement: Chronicle does not merely store strategies, it
studies WHEN and WHY they worked, researches better versions, and OFFERS the
improvement to Atlas to validate. (Book II Part III Ch VI Memory Evolution;
Ch VIII "Chronicle continuously refines its understanding"; Book II Principle
IV Research Before Assumption; Book I Article VII Collaboration.)

This is the behavior you described for the original AI Memory System, using
the shared reasoning loop:

  1. When a strategy outcome is stored (e.g. from Oracle), Chronicle records
     the CONTEXT it fit in and WHY (the conditions + the result).
  2. It builds a "fit profile": under which conditions this strategy succeeds
     vs fails, learned from real stored outcomes.
  3. Using the reasoning engine, it proposes an IMPROVED variant (a different
     approach or refined conditions) as a candidate.
  4. It OFFERS that variant to Atlas to research/validate. If Atlas's evidence
     supports it, Chronicle promotes the improved knowledge; if not, it keeps
     the current one and records why the idea was rejected.

Nothing is asserted without evidence: improvements must earn their place.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from core.memory_record import MemoryRecord, MemoryPillar  # type: ignore


class ImprovementEngine:
    """Studies stored strategies and actively proposes better versions."""

    def __init__(self, store, graph, reasoning=None, atlas=None, chronicle_agent=None):
        self.store = store
        self.graph = graph
        self.reasoning = reasoning     # shared ReasoningEngine (from BaseAgent)
        self.atlas = atlas             # Atlas agent for validation
        self.chronicle = chronicle_agent

    # ---- 1: record WHEN + WHY a strategy fit ----

    def record_strategy_fit(self, strategy_name: str, domain: str, conditions: Dict[str, Any],
                           outcome: str, success: bool, reason: str = "") -> Dict[str, Any]:
        """
        Store a rich strategy-fit memory: the conditions it was used under, the
        outcome, and why it fit. This is far richer than a bare store().
        """
        cond_str = ", ".join(f"{k}={v}" for k, v in conditions.items())
        content = (f"Strategy '{strategy_name}' [{domain}] under conditions ({cond_str}) "
                  f"-> {outcome} ({'success' if success else 'failure'}). Why: {reason}")
        rec = MemoryRecord(
            pillar=MemoryPillar.EPISODIC if not success else MemoryPillar.PROCEDURAL,
            domain=domain, content=content,
            summary=f"{strategy_name} {'worked' if success else 'failed'} when {cond_str}",
            source_repository="chronicle", source_agent="improvement",
            evidence=[reason] if reason else [],
            tags=["strategy_fit", strategy_name, domain, "success" if success else "failure"],
            lesson=reason)
        if self.chronicle:
            rec.embedding = self.chronicle.embedder.encode(rec.summary)
        self.store.add(rec)
        return {"stored": rec.memory_id, "confidence": rec.confidence}

    # ---- 2: build the fit profile from real stored outcomes ----

    def fit_profile(self, strategy_name: str, domain: str) -> Dict[str, Any]:
        """Learn under which conditions a strategy succeeds vs fails."""
        fits = [r for r in self.store.all()
               if strategy_name in r.tags and r.domain == domain and "strategy_fit" in r.tags]
        succ_conditions = defaultdict(int)
        fail_conditions = defaultdict(int)
        for r in fits:
            bucket = succ_conditions if "success" in r.tags else fail_conditions
            for token in r.summary.lower().split():
                if "=" in token:
                    bucket[token] += 1
        return {
            "strategy": strategy_name, "domain": domain, "samples": len(fits),
            "succeeds_when": dict(sorted(succ_conditions.items(), key=lambda x: -x[1])[:5]),
            "fails_when": dict(sorted(fail_conditions.items(), key=lambda x: -x[1])[:5]),
        }

    # ---- 3+4: propose an improvement and offer it to Atlas ----

    def propose_improvement(self, strategy_name: str, domain: str) -> Dict[str, Any]:
        """
        Use evidence to propose a better version of a strategy, then OFFER it to
        Atlas for validation. Promote only if research supports it.
        """
        profile = self.fit_profile(strategy_name, domain)
        if profile["samples"] < 2:
            return {"status": "insufficient_evidence",
                   "message": f"need more outcomes for '{strategy_name}' before improving",
                   "profile": profile}

        # Formulate a concrete hypothesis for improvement from the fit profile.
        succeeds = ", ".join(profile["succeeds_when"].keys()) or "unclear conditions"
        fails = ", ".join(profile["fails_when"].keys()) or "no clear failure pattern"
        hypothesis = (f"Restricting '{strategy_name}' in {domain} to conditions like "
                     f"[{succeeds}] and avoiding [{fails}] will improve its success rate.")

        # OFFER to Atlas to research/validate (real collaboration).
        validation = None
        supported = False
        if self.atlas is not None:
            try:
                out = self.atlas.handle({"task": "research.investigate",
                                        "context": {"query": hypothesis, "domain": domain},
                                        "sender": "chronicle"})
                report = out.get("report", {})
                validation = {"confidence": report.get("confidence"),
                             "summary": report.get("summary", "")[:200]}
                supported = (report.get("confidence", 0) or 0) >= 0.5
            except Exception:
                validation = {"error": "atlas unavailable"}

        # If reasoning engine present, register the improved approach as a candidate.
        if self.reasoning is not None and supported:
            self.reasoning.register_strategy(
                problem_type=f"{domain}_strategy",
                name=f"{strategy_name}_improved",
                handler="_apply_improved_strategy",
                description=hypothesis,
                reasons_for=[f"research supported (conf {validation.get('confidence')})",
                            f"succeeds when {succeeds}"],
                reasons_against=[f"historically fails when {fails}"])

        # Preserve the improvement decision as evolutionary memory.
        verdict = "adopted" if supported else "rejected"
        improved_mem = MemoryRecord(
            pillar=MemoryPillar.EVOLUTIONARY, domain=domain,
            content=f"Improvement for '{strategy_name}': {hypothesis} -> {verdict}. "
                   f"Atlas: {validation}",
            summary=f"Improved {strategy_name}: {verdict}",
            source_repository="chronicle", source_agent="improvement",
            tags=["improvement", strategy_name, domain, verdict],
            verified=supported)
        if self.chronicle:
            improved_mem.embedding = self.chronicle.embedder.encode(improved_mem.summary)
        self.store.add(improved_mem)

        return {"status": "complete", "strategy": strategy_name, "domain": domain,
               "hypothesis": hypothesis, "atlas_validation": validation,
               "supported": supported, "verdict": verdict, "profile": profile}

    def stats(self) -> Dict[str, Any]:
        improvements = [r for r in self.store.all() if "improvement" in r.tags]
        adopted = sum(1 for r in improvements if "adopted" in r.tags)
        return {"improvements_proposed": len(improvements), "adopted": adopted}
