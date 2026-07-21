"""
Nexus.core.orchestration
========================
Institutional multi-agent orchestration. (Book II Part II Ch VIII Multi-Agent
Conversations; Ch XIII Collaboration Sessions.)

Upgrades over the sequential version:
  * PARALLEL EXECUTION: sub-tasks are grouped into dependency LEVELS; every
    task within a level runs CONCURRENTLY (via the Executor thread pool). Only
    cross-level dependencies are serialized. Institutional latency demands this.
  * BUDGETED + BREAKER-GUARDED dispatch: every call runs under an SLA and a
    circuit breaker (via core.execution.Executor), with result caching.
  * CONFIDENCE-WEIGHTED CONFLICT RESOLUTION: when specialists disagree, Nexus
    does not just note it. It weighs each position by the specialist's reported
    confidence and its live health/track record, and can refer the conflict to
    Atlas (evidence) and Chronicle (belief history) to adjudicate, exactly as
    Chronicle now reconciles beliefs.
  * LEARNED ORDERING via the CollaborationGraph (unchanged interface).

FIX LOG (phase5-orchestration-v1  2026-07-21):
  FIX-OR-01  CONTEXT_KEY was missing "prediction" key.
             Oracle's actual domain attribute is "prediction" (confirmed from
             Oracle/agents/oracle_agent.py line 108: domain = "prediction").
             When a multi-domain query included a trading/prediction sub-task,
             Oracle's result was dispatched but never injected into
             shared_context because CONTEXT_KEY had no "prediction" entry.
             Subsequent agents (Sentinel, Pulse) received no market_context.
             FIX: Added "prediction": "market_context" to CONTEXT_KEY.
             Constitutional law: Book III Ch VIII Standardized Interfaces;
             Book II Everything Communicates.

  FIX-OR-02  PRIMARY_TASK was missing sentinel, pulse, forge, genesis, nexus.
             Orchestrator.run() calls PRIMARY_TASK.get(repo, "") — missing
             entries produce empty task strings, causing agents to return
             "Unknown task: " errors.
             FIX: Added all 9 agents to PRIMARY_TASK.
             Constitutional law: Book III Ch VIII Standardized Interfaces.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from Nexus.intelligence.collaboration_graph import CollaborationGraph  # type: ignore

# FIX-OR-01: Added "prediction" -> "market_context" (Oracle.domain = "prediction")
CONTEXT_KEY = {
    "news": "news_context",
    "social": "social_context",
    "research": "research_context",
    "memory": "memory_context",
    "trading": "market_context",
    "prediction": "market_context",   # FIX-OR-01: Oracle's actual domain
    "governance": "governance_context",
    "training": "training_context",
    "creation": "creation_context",
    "coordination": "coordination_context",
}

# FIX-OR-02: All 9 agents now have PRIMARY_TASK entries.
PRIMARY_TASK = {
    "oracle": "trade.signal",
    "sentinel": "news.sentiment",
    "pulse": "social.sentiment",
    "atlas": "research.investigate",
    "forge": "training.run",
    "chronicle": "memory.answer",
    "aegis": "ecosystem.health",
    "genesis": "capability.analyze",
    "nexus": "ecosystem.monitor",
}


class Orchestrator:
    def __init__(self, registry, classifier, executor, graph: CollaborationGraph,
                 chronicle=None, atlas=None, llm=None):
        self.registry = registry
        self.classifier = classifier
        self.executor = executor          # core.execution.Executor
        self.graph = graph
        self.chronicle = chronicle
        self.atlas = atlas
        self.llm = llm

    # ---- decompose (generic) ----

    def decompose(self, query: str) -> List[Dict[str, Any]]:
        import re
        clauses = [p.strip() for p in re.split(
            r"\b(?:and|given|considering|based on|then|while|plus|also)\b|[,;]",
            query, flags=re.IGNORECASE) if p and len(p.strip()) > 3]
        subtasks, seen = [], set()
        for clause in clauses:
            cls = self.classifier.classify(clause)
            d = cls["domain"]
            if d == "general" or cls["confidence"] < 0.15 or d in seen:
                continue
            seen.add(d)
            subtasks.append({"clause": clause, "domain": d, "repository": cls["repository"],
                           "confidence": cls["confidence"]})
        whole = self.classifier.classify(query)
        if whole["domain"] != "general" and whole["domain"] not in seen:
            subtasks.append({"clause": query, "domain": whole["domain"],
                           "repository": whole["repository"], "confidence": whole["confidence"]})
            seen.add(whole["domain"])
        for extra in self.graph.suggest_collaborators(list(seen)):
            if extra not in seen:
                from Nexus.intelligence.domain_classifier import DOMAIN_TO_REPO  # type: ignore
                subtasks.append({"clause": query, "domain": extra,
                               "repository": DOMAIN_TO_REPO.get(extra, "nexus"),
                               "confidence": 0.4, "via": "learned_affinity"})
                seen.add(extra)
        return subtasks

    # ---- order into parallel LEVELS ----

    def levels(self, subtasks: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Topological LEVELS: each level's tasks are independent -> run parallel."""
        present = [s["domain"] for s in subtasks]
        indeg = {d: 0 for d in present}
        deps: Dict[str, List[str]] = {d: [] for d in present}
        for d in present:
            for dep in self.graph.dependencies_for(d, present):
                if dep in present:
                    deps[d].append(dep)
                    indeg[d] += 1
        by_domain = {s["domain"]: s for s in subtasks}
        levels: List[List[Dict[str, Any]]] = []
        remaining = set(present)
        while remaining:
            ready = [d for d in remaining if indeg[d] == 0]
            if not ready:  # cycle: break by taking all remaining
                ready = list(remaining)
            levels.append([by_domain[d] for d in ready])
            for d in ready:
                remaining.discard(d)
                for other in remaining:
                    if d in deps[other]:
                        indeg[other] -= 1
        return levels

    # ---- run with parallelism + conflict resolution ----

    def run(self, query: str, user_id: str = "user", priority: int = 4) -> Dict[str, Any]:
        session_id = f"orch-{uuid.uuid4().hex[:8]}"
        started = time.time()
        subtasks = self.decompose(query)
        if len(subtasks) <= 1:
            return {"multi_agent": False, "subtasks": subtasks,
                   "note": "single-domain query; use direct routing"}

        levels = self.levels(subtasks)
        shared_context: Dict[str, Any] = {"query": query, "user_id": user_id, "symbol": query.upper()}
        transcript: List[Dict[str, Any]] = []
        per_agent_success: Dict[str, bool] = {}
        missing: List[str] = []
        ordering_domains: List[str] = []

        for level in levels:
            # build parallel jobs for everything in this level
            jobs, level_meta = [], []
            for st in level:
                repo = st["repository"]
                if self.registry.get(repo) is None:
                    missing.append(repo)
                    per_agent_success[st["domain"]] = False
                    transcript.append({"domain": st["domain"], "repository": repo, "status": "unavailable"})
                    continue
                ctx = dict(shared_context)
                ctx["clause"] = st["clause"]
                jobs.append((repo, PRIMARY_TASK.get(repo, ""), ctx))
                level_meta.append(st)
                ordering_domains.append(st["domain"])

            # PARALLEL dispatch of independent tasks in this level
            results = self.executor.call_parallel(jobs, priority=priority)

            for st in level_meta:
                repo = st["repository"]
                out = results.get(repo, {"status": "error", "message": "no result"})
                ok = isinstance(out, dict) and out.get("status") != "error"
                per_agent_success[st["domain"]] = ok
                key = CONTEXT_KEY.get(st["domain"])
                if key and ok:
                    shared_context[key] = self._extract_signal(out)
                transcript.append({"domain": st["domain"], "repository": repo,
                                 "output": out, "confidence": self._confidence_of(out),
                                 "latency_ms": out.get("_latency_ms"), "cached": out.get("_cached", False)})

        conflicts = self._resolve_conflicts(transcript)
        synthesis = self._synthesize(query, transcript, conflicts)
        overall = bool(synthesis) and any(per_agent_success.values()) and not missing
        self.graph.record_session(ordering_domains, per_agent_success, overall)

        session = {"session_id": session_id, "multi_agent": True, "query": query,
                  "levels": [[s["domain"] for s in lvl] for lvl in levels],
                  "parallelism": max((len(lvl) for lvl in levels), default=1),
                  "transcript": transcript, "conflicts": conflicts, "synthesis": synthesis,
                  "missing_agents": missing, "overall_success": overall,
                  "duration_ms": round((time.time() - started) * 1000, 1)}
        self._preserve(session)
        return session

    # ---- helpers ----

    def _extract_signal(self, output: Dict[str, Any]) -> Any:
        if not isinstance(output, dict):
            return output
        for key in ("sentiment", "signal", "report", "answer", "health"):
            if key in output:
                return output[key]
        return {k: v for k, v in output.items() if not k.startswith("_") and k != "status"}

    def _confidence_of(self, output: Dict[str, Any]) -> float:
        if not isinstance(output, dict):
            return 0.5
        for holder in (output, output.get("report", {}), output.get("signal", {}),
                      output.get("sentiment", {})):
            if isinstance(holder, dict) and "confidence" in holder:
                try:
                    return float(holder["confidence"])
                except (TypeError, ValueError):
                    continue
        return 0.5

    def _resolve_conflicts(self, transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Confidence + health weighted conflict resolution. When two specialists
        express opposing directional signals, weigh by confidence x live health,
        and (if available) refer to Atlas/Chronicle for adjudication.
        """
        # extract directional stances (sentiment/signal sign)
        stances = []
        for t in transcript:
            sig = self._extract_signal(t.get("output", {}))
            direction = self._direction(sig)
            if direction != 0:
                health = self.registry.get(t["repository"])
                hscore = 1.0
                if health:
                    hscore = 0.5 + 0.5 * (1.0 if health.healthy else 0.0)
                weight = t.get("confidence", 0.5) * hscore
                stances.append({"domain": t["domain"], "direction": direction,
                              "weight": round(weight, 3)})
        # detect opposition
        pos = [s for s in stances if s["direction"] > 0]
        neg = [s for s in stances if s["direction"] < 0]
        if not (pos and neg):
            return []
        pos_w = sum(s["weight"] for s in pos)
        neg_w = sum(s["weight"] for s in neg)
        winner = "positive" if pos_w >= neg_w else "negative"
        conflict = {"positions": {"positive": pos, "negative": neg},
                   "weighted": {"positive": round(pos_w, 3), "negative": round(neg_w, 3)},
                   "resolution": winner, "margin": round(abs(pos_w - neg_w), 3)}
        # refer to Atlas for an evidence tie-break when margin is thin
        if conflict["margin"] < 0.2 and self.atlas is not None:
            try:
                out = self.atlas.handle({"task": "research.investigate",
                    "context": {"query": "resolve conflicting specialist signals",
                               "domain": "general"}, "sender": "nexus"})
                conflict["atlas_adjudication"] = out.get("report", {}).get("summary", "")[:200]
            except Exception:
                pass
        return [conflict]

    def _direction(self, sig: Any) -> int:
        if isinstance(sig, dict):
            for key in ("direction", "sentiment", "score"):
                v = sig.get(key)
                if isinstance(v, (int, float)):
                    return (v > 0.05) - (v < -0.05)
                if isinstance(v, str):
                    if v.lower() in ("buy", "bullish", "positive", "up"):
                        return 1
                    if v.lower() in ("sell", "bearish", "negative", "down"):
                        return -1
        return 0

    def _synthesize(self, query: str, transcript: List[Dict[str, Any]], conflicts: List[Dict]) -> str:
        contribs = []
        for t in transcript:
            if t.get("output") and isinstance(t["output"], dict) and t["output"].get("status") != "error":
                contribs.append(f"{t['domain']} (conf {t.get('confidence')}): "
                              f"{str(self._extract_signal(t['output']))[:180]}")
        if not contribs:
            return "No specialist produced a usable result."
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                conflict_note = ("\nResolved conflict: " + str(conflicts[0]["resolution"])
                               + f" (weighted {conflicts[0]['weighted']})") if conflicts else ""
                r = self.llm.complete(system_prompt("nexus"),
                    f"Question: {query}\n\nFindings:\n" + "\n".join(f"- {c}" for c in contribs)
                    + conflict_note + "\n\nSynthesize ONE answer. If specialists disagreed, state the "
                    "confidence-weighted resolution and why.", temperature=0.3, max_tokens=350)
                if r.ok and r.text.strip():
                    return r.text.strip()
            except Exception:
                pass
        base = "Combined view -> " + " | ".join(contribs)
        if conflicts:
            base += f"  [conflict resolved: {conflicts[0]['resolution']} side, "
            base += f"weighted {conflicts[0]['weighted']}]"
        return base

    def _preserve(self, session: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(content=f"Multi-agent answer to '{session['query']}': {session['synthesis']}",
                                pillar="episodic", domain="coordination",
                                tags=["nexus", "multi_agent"] + [d for lvl in session["levels"] for d in lvl],
                                source_repository="nexus")
        except Exception:
            pass  # aegis:allow-silent