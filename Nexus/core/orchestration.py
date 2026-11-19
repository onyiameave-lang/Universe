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
from Nexus.core.institutional_contracts import (  # type: ignore
    classify_chronicle_writeback,
    compute_coordination_quality_signal,
    deduplicate_and_resolve_claims,
    evaluate_aegis_policy_gate,
    extract_claim_envelope,
)

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
        self._recent_sessions: List[Dict[str, Any]] = []

    # ---- decompose (bounded multi-clause & intent-aware) ----

    def decompose(self, query: str) -> List[Dict[str, Any]]:
        import re
        clauses = [p.strip() for p in re.split(
            r"\b(?:and|given|considering|based on|then|while|plus|also|with)\b|[,;]",
            query, flags=re.IGNORECASE) if p and len(p.strip()) > 3]
        subtasks, seen = [], set()
        for clause in clauses:
            cls = self.classifier.classify(clause)
            d = cls["domain"]
            scores = cls.get("scores") or {}
            # Stage 2: Accept clause domain if intent signal fired or domain score > 0
            if d in ("general", "coordination") or (cls["confidence"] < 0.08 and not scores) or d in seen:
                continue
            seen.add(d)
            subtasks.append({
                "clause": clause,
                "domain": d,
                "repository": cls["repository"],
                "confidence": cls["confidence"],
                "via": "clause_intent" if cls.get("intent_signal") else "clause_classifier",
            })
        whole = self.classifier.classify(query)
        if whole["domain"] not in ("general", "coordination") and whole["domain"] not in seen:
            subtasks.append({
                "clause": query,
                "domain": whole["domain"],
                "repository": whole["repository"],
                "confidence": whole["confidence"],
                "via": "whole_query_classifier",
            })
            seen.add(whole["domain"])

        # Also inspect secondary high-scoring domains in the whole-query classification
        for sec_dom, sec_score in (whole.get("scores") or {}).items():
            if len(subtasks) >= 4:
                break
            if sec_dom not in ("general", "coordination") and sec_dom not in seen and float(sec_score) >= 2.5:
                from Nexus.intelligence.domain_classifier import DOMAIN_TO_REPO  # type: ignore
                subtasks.append({
                    "clause": query,
                    "domain": sec_dom,
                    "repository": DOMAIN_TO_REPO.get(sec_dom, "atlas"),
                    "confidence": round(min(0.85, float(sec_score) / 10.0), 3),
                    "via": "multi_intent_secondary",
                })
                seen.add(sec_dom)

        for extra in self.graph.suggest_collaborators(list(seen)):
            if len(subtasks) >= 4:
                break
            if extra not in seen and extra not in ("general", "coordination", "governance"):
                from Nexus.intelligence.domain_classifier import DOMAIN_TO_REPO  # type: ignore
                subtasks.append({
                    "clause": query,
                    "domain": extra,
                    "repository": DOMAIN_TO_REPO.get(extra, "nexus"),
                    "confidence": 0.4,
                    "via": "learned_affinity",
                })
                seen.add(extra)
        return subtasks[:4]

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

    # ---- run with parallelism + provenance deduplication + independent Aegis gate ----

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
        claim_envelopes: List[Dict[str, Any]] = []
        per_agent_success: Dict[str, bool] = {}
        missing: List[str] = []
        ordering_domains: List[str] = []

        for lvl_idx, level in enumerate(levels):
            jobs, level_meta = [], []
            for st in level:
                repo = st["repository"]
                if self.registry.get(repo) is None:
                    missing.append(repo)
                    per_agent_success[st["domain"]] = False
                    transcript.append({
                        "level": lvl_idx + 1,
                        "domain": st["domain"],
                        "repository": repo,
                        "status": "unavailable",
                    })
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
                task_name = PRIMARY_TASK.get(repo, "")
                out = results.get(repo, {"status": "error", "message": "no result"})
                envelope = extract_claim_envelope(
                    agent_name=repo,
                    domain=st["domain"],
                    task=task_name,
                    raw_output=out,
                    latency_ms=out.get("_latency_ms") if isinstance(out, dict) else None,
                )
                claim_envelopes.append(envelope)

                ok = (
                    isinstance(out, dict)
                    and out.get("status") != "error"
                    and int(envelope.get("provenance_count") or 0) > 0
                )
                per_agent_success[st["domain"]] = ok
                key = CONTEXT_KEY.get(st["domain"])
                if key and ok:
                    shared_context[key] = self._extract_signal(out)
                transcript.append({
                    "level": lvl_idx + 1,
                    "domain": st["domain"],
                    "repository": repo,
                    "via": st.get("via", "classifier"),
                    "output": out,
                    "claim_envelope": envelope,
                    "confidence": envelope["calibrated_reliability"],
                    "raw_confidence": envelope["raw_confidence"],
                    "provenance_count": envelope["provenance_count"],
                    "latency_ms": envelope["latency_ms"],
                    "cached": out.get("_cached", False) if isinstance(out, dict) else False,
                })

        # Stage 4A: Provenance deduplication & calibrated conflict resolution
        consensus_info = deduplicate_and_resolve_claims(claim_envelopes)
        conflicts = self._resolve_conflicts(transcript, consensus_info=consensus_info)

        # Stage 4B: Independent Aegis Policy & State-Integrity Gate (never a voter)
        primary_dom = ordering_domains[0] if ordering_domains else "general"
        aegis_gate = evaluate_aegis_policy_gate(
            query=query,
            primary_domain=primary_dom,
            participating_domains=ordering_domains,
            envelopes=consensus_info["envelopes"],
        )

        duration_ms = round((time.time() - started) * 1000, 1)

        # Stage 3: Multi-factor coordination quality signal for CollaborationGraph learning
        quality_signal = compute_coordination_quality_signal(
            envelopes=consensus_info["envelopes"],
            consensus_info=consensus_info,
            aegis_gate=aegis_gate,
            duration_ms=duration_ms,
        )

        synthesis = self._synthesize(query, transcript, conflicts, consensus_info=consensus_info, aegis_gate=aegis_gate)
        overall = bool(synthesis) and quality_signal["passed"] and not missing
        self.graph.record_session(
            ordering_domains,
            per_agent_success,
            overall,
            quality_score=quality_signal["quality_score"],
        )

        # Proposal 4: Two-Tier Chronicle Write-Back Hygiene
        writeback_policy = classify_chronicle_writeback(
            query=query,
            envelopes=consensus_info["envelopes"],
            consensus_info=consensus_info,
            aegis_gate=aegis_gate,
            quality_signal=quality_signal,
        )

        session = {
            "session_id": session_id,
            "multi_agent": True,
            "query": query,
            "levels": [[s["domain"] for s in lvl] for lvl in levels],
            "parallelism": max((len(lvl) for lvl in levels), default=1),
            "transcript": transcript,
            "claim_envelopes": consensus_info["envelopes"],
            "provenance_deduplication": {
                "unique_sources_count": consensus_info["unique_provenance_sources_count"],
                "shared_sources_deduplicated_count": consensus_info["shared_sources_deduplicated_count"],
                "shared_sources_details": consensus_info["shared_sources_details"],
            },
            "conflicts": conflicts,
            "consensus": {
                "resolution": consensus_info["resolution"],
                "rationale": consensus_info["resolution_rationale"],
                "calibrated_conviction_pct": consensus_info["calibrated_conviction_pct"],
                "directional_weights": consensus_info["directional_weights"],
            },
            "aegis_policy_gate": aegis_gate,
            "quality_signal": quality_signal,
            "chronicle_writeback": writeback_policy,
            "synthesis": synthesis,
            "missing_agents": missing,
            "overall_success": overall,
            "duration_ms": duration_ms,
            "timestamp": time.time(),
        }
        self._recent_sessions.append(session)
        if len(self._recent_sessions) > 50:
            del self._recent_sessions[:-50]
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

    def _resolve_conflicts(
        self,
        transcript: List[Dict[str, Any]],
        consensus_info: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Stage 4A: Provenance-deduplicated & empirically calibrated conflict resolution.
        When two specialists express opposing directional signals, weight each stance by its
        deduplicated provenance weight x empirical domain calibration x live health, and refer
        to Atlas for evidence tie-breaking when the margin is thin (< 0.20).
        """
        if consensus_info and not consensus_info.get("conflict_detected"):
            return []

        stances = []
        for t in transcript:
            env = t.get("claim_envelope") or {}
            direction = env.get("directional_stance")
            if direction is None:
                sig = self._extract_signal(t.get("output", {}))
                direction = self._direction(sig)
            if direction != 0:
                health = self.registry.get(t["repository"])
                hscore = 1.0
                if health:
                    hscore = 0.5 + 0.5 * (1.0 if health.healthy else 0.0)
                base_weight = float(env.get("effective_weight") or t.get("confidence", 0.5))
                weight = base_weight * hscore
                stances.append({
                    "domain": t["domain"],
                    "agent": t["repository"],
                    "direction": direction,
                    "weight": round(weight, 3),
                    "independent_provenance_factor": env.get("independent_provenance_factor", 1.0),
                })
        pos = [s for s in stances if s["direction"] > 0]
        neg = [s for s in stances if s["direction"] < 0]
        if not (pos and neg):
            return []
        pos_w = sum(s["weight"] for s in pos)
        neg_w = sum(s["weight"] for s in neg)
        margin = round(abs(pos_w - neg_w), 3)
        winner = "contested_hold" if margin < 0.15 else ("positive" if pos_w >= neg_w else "negative")
        conflict = {
            "positions": {"positive": pos, "negative": neg},
            "weighted": {"positive": round(pos_w, 3), "negative": round(neg_w, 3)},
            "resolution": winner,
            "margin": margin,
            "shared_sources_deduplicated": (consensus_info or {}).get("shared_sources_deduplicated_count", 0),
        }
        # Refer to Atlas for an evidence tie-break when margin is thin
        if conflict["margin"] < 0.20 and self.atlas is not None:
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

    def _synthesize(
        self,
        query: str,
        transcript: List[Dict[str, Any]],
        conflicts: List[Dict],
        consensus_info: Optional[Dict[str, Any]] = None,
        aegis_gate: Optional[Dict[str, Any]] = None,
    ) -> str:
        contribs = []
        for t in transcript:
            if t.get("output") and isinstance(t["output"], dict) and t["output"].get("status") != "error":
                env = t.get("claim_envelope") or {}
                summary_text = env.get("summary") or str(self._extract_signal(t["output"]))[:220]
                contribs.append(
                    f"{t['repository'].title()} [{t['domain']}, calibrated rel {round(float(t.get('confidence', 0.75))*100, 1)}%, "
                    f"{t.get('provenance_count', 1)} source(s)]: {summary_text[:220]}"
                )
        if not contribs:
            return "No specialist produced a usable result."
        gate_note = ""
        if aegis_gate and aegis_gate.get("applicable"):
            gate_note = f" | Aegis Policy Gate ({aegis_gate.get('policy_version')}): {aegis_gate.get('status')}"
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                conflict_note = ("\nResolved conflict: " + str(conflicts[0]["resolution"])
                               + f" (weighted {conflicts[0]['weighted']})") if conflicts else ""
                r = self.llm.complete(system_prompt("nexus"),
                    f"Question: {query}\n\nFindings:\n" + "\n".join(f"- {c}" for c in contribs)
                    + conflict_note + gate_note + "\n\nSynthesize ONE answer. If specialists disagreed, state the "
                    "calibrated provenance-deduplicated resolution and why.", temperature=0.3, max_tokens=350)
                if r.ok and r.text.strip():
                    return r.text.strip()
            except Exception:
                pass
        base = "Combined view -> " + " | ".join(contribs)
        if conflicts:
            base += f"  [conflict resolved: {conflicts[0]['resolution']}, weighted {conflicts[0]['weighted']}]"
        if gate_note:
            base += gate_note
        return base

    def _preserve(self, session: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        writeback = session.get("chronicle_writeback") or {}
        tags = ["nexus", "multi_agent", "tier1_trace"] + [d for lvl in session["levels"] for d in lvl]
        # Tier 1: Always store the auditable episodic routing trace
        try:
            self.chronicle.store_memory(
                content=(
                    f"[NEXUS_DAG_TRACE {session['session_id']}] query={session['query']!r} "
                    f"levels={session['levels']} quality={session.get('quality_signal', {}).get('quality_score')} "
                    f"aegis_gate={session.get('aegis_policy_gate', {}).get('status')}"
                ),
                pillar="episodic",
                domain="coordination_trace",
                summary=f"Nexus DAG Trace {session['session_id']} ({session['duration_ms']}ms)",
                tags=tags,
                source_repository="Nexus",
                source_agent="nexus",
            )
        except Exception:
            pass

        # Tier 2: Promote into reusable semantic knowledge ONLY when verified
        if writeback.get("tier2_promoted_to_reusable_knowledge"):
            try:
                self.chronicle.store_memory(
                    content=f"Verified Multi-Agent Synthesis for '{session['query']}': {session['synthesis']}",
                    pillar="semantic",
                    domain="collective_knowledge",
                    summary=f"Validated Collective Knowledge: {session['query'][:110]}",
                    tags=["nexus", "tier2_validated_knowledge"] + [d for lvl in session["levels"] for d in lvl],
                    source_repository="Nexus",
                    source_agent="nexus",
                )
            except Exception:
                pass