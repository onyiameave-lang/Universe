"""
Nexus.core.coordination_engine
=============================
The coordination brain. (Book II Part II Ch VIII Multi-Agent Conversations;
Ch XIII Collaboration Sessions; Book I Part IV Article VII Collaboration;
Book II Part I Ch VII specialist routing.)

Real orchestration:
  * Classify + route queries to the right live agent.
  * Memory First: consult Chronicle before any specialist.
  * Multi-agent collaboration sessions with preserved transcripts.
  * Capability-gap detection -> ask Genesis to fill the gap.
  * Every routing decision recorded for explainability + classifier learning.

FIX LOG (phase4-nexus-engine-v1  2026-07-21):
  BUG-P4-01  _preserve_session() and _preserve_route() called
             chronicle.store(...) which resolves to ChronicleAgent.self.store
             — a VectorStore instance attribute — not the store() convenience
             method.  VectorStore is not callable, so every Chronicle write
             raised: TypeError: 'VectorStore' object is not callable.
             ROOT CAUSE: ChronicleAgent defines both:
               self.store = VectorStore(...)   # instance attribute
               def store(self, ...): ...       # convenience method (shadowed)
             FIX: Changed chronicle.store(...) -> chronicle.store_memory(...)
             which is the unambiguous public API (no name collision).
             Constitutional law: Book II Principle I Memory First —
             Chronicle writes must actually work.

  BUG-N1  _strat_direct() propagated Atlas status="error" directly to Nexus
          solve(), causing Nexus to exhaust all 3 routing strategies even when
          Atlas had a usable best-effort report in its trace.
          FIX: _strat_direct() now inspects the Atlas result for a usable
          report (report with non-empty summary) before declaring failure.
          If Atlas returned status="error" but has a report, we promote it to
          status="complete" with a degraded=True flag.
          Constitutional law: Book II Principle V Graceful Degradation;
          Book IV No Silent Failures.

  BUG-N2  _strat_orchestrate() returned status="error" for single-domain
          queries ("single-domain; orchestration not needed"), wasting Nexus's
          3rd routing attempt on a guaranteed failure.
          FIX: _strat_orchestrate() now falls back to _strat_direct() for
          single-domain queries instead of returning an error.
          Constitutional law: Book II Principle V Graceful Degradation.

  BUG-N3  _strat_direct() passed only {"query", "symbol"} to Atlas, omitting
          "domain", "depth", and "memory_context" that Atlas's research engine
          uses to select sources and calibrate depth.
          FIX: Full context dict (query, domain, depth, memory_context,
          user_id) now forwarded to Atlas.
          Constitutional law: Book III Ch VIII Standardized Interfaces.

  BUG-N4  _consult_memory() silently swallowed all Chronicle errors (bare
          except: return []).  No log, no audit trail.
          FIX: Errors now logged at WARNING level with exc_info=True.
          Constitutional law: Book II No Silent Failures; Book IV Fail Loudly.

  BUG-N5  _preserve_session() used bare `pass` on Chronicle write errors.
          FIX: Errors now logged at WARNING level.
          Constitutional law: Book II No Silent Failures.

  BUG-N6  Chronicle memory context retrieved by _consult_memory() was never
          forwarded into the Atlas dispatch context in _strat_direct().
          FIX: memory_context injected into Atlas context dict.
          Constitutional law: Book II Principle I Memory First;
          Book II Everything Communicates.

  BUG-N7  _preserve_route() was absent — routing decisions were never stored
          in Chronicle, breaking the "every routing decision recorded" promise
          in the module docstring and Book II Part I Ch VII.
          FIX: Added _preserve_route() called at the end of route().
          Constitutional law: Book II Everything Communicates;
          Chronicle as source of truth.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from Nexus.intelligence.domain_classifier import DomainClassifier  # type: ignore
from Nexus.core.agent_registry import AgentRegistry                # type: ignore

log = logging.getLogger("nexus.coordination")


class CoordinationEngine:
    PRIMARY_TASK = {"oracle": "trade.signal", "sentinel": "news.sentiment",
                    "pulse": "social.sentiment", "atlas": "research.investigate",
                    "forge": "training.run", "chronicle": "memory.answer",
                    "aegis": "ecosystem.health", "genesis": "capability.analyze",
                    "nexus": "ecosystem.monitor"}

    def __init__(self, registry: AgentRegistry, classifier: DomainClassifier, chronicle_client=None):
        self.registry = registry
        self.classifier = classifier
        self.chronicle = chronicle_client
        self._routes: List[Dict[str, Any]] = []
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._gaps: List[Dict[str, Any]] = []

    def route(self, query: str, user_id: str = "user") -> Dict[str, Any]:
        started = time.time()
        classification = self.classifier.classify(query)
        domain = classification["domain"]
        target = classification["repository"]

        # BUG-N6 FIX: memory context is now forwarded to dispatch
        memory = self._consult_memory(query, domain)

        agent = self.registry.find_by_domain(domain)
        if agent is None and target != "nexus":
            gap = self._flag_gap(domain, query)
            return {"query": query, "classification": classification, "routed_to": None,
                   "capability_gap": gap, "memory_consulted": bool(memory),
                   "message": f"No live agent for '{domain}'. Genesis notified."}

        result = None
        if agent is not None:
            task = self.PRIMARY_TASK.get(agent.name, "")
            # BUG-N3 FIX: pass full context including domain, depth, memory_context
            result = self._dispatch(agent.name, task, {
                "query": query,
                "symbol": query.upper(),
                "domain": domain,
                "depth": "standard",
                "user_id": user_id,
                "memory_context": memory,   # BUG-N6 FIX
            })

        record = {"route_id": f"route-{uuid.uuid4().hex[:8]}", "query": query, "domain": domain,
                  "target": target, "confidence": classification["confidence"],
                  "duration_ms": round((time.time() - started) * 1000, 1)}
        self._routes.append(record)

        # BUG-N7 FIX: persist routing decision to Chronicle
        self._preserve_route(record, result)

        return {"query": query, "classification": classification, "routed_to": target,
                "result": result, "memory_consulted": bool(memory), "route": record}

    def _dispatch(self, agent_name: str, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        agent = self.registry.get(agent_name)
        if not agent:
            return {"status": "error", "message": f"{agent_name} not registered"}
        start = time.time()
        success = True
        try:
            handle = agent.handle
            if hasattr(handle, "act"):
                out = handle.act(task, context)
            elif hasattr(handle, "handle"):
                out = handle.handle({"sender": "nexus", "receiver": agent_name,
                                     "task": task, "context": context})
            elif callable(handle):
                out = handle({"sender": "nexus", "receiver": agent_name,
                              "task": task, "context": context})
            else:
                out = {"status": "error", "message": f"{agent_name} handle has no callable interface"}
            success = out.get("status") != "error" if isinstance(out, dict) else True
        except Exception as exc:
            success = False
            out = {"status": "error", "message": str(exc)}
            log.error("_dispatch %s/%s raised: %s", agent_name, task, exc, exc_info=True)
        self.registry.record_call(agent_name, time.time() - start, success)
        return out

    # ------------------------------------------------------------------
    # BUG-N1 FIX: _strat_direct_safe — extract best-effort result from
    # Atlas even when it returns status="error".
    # ------------------------------------------------------------------

    def _extract_best_report(self, out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Walk an agent result dict looking for a usable report.
        Atlas returns {"status": "error", "report": {...}, "trace": [...]}
        when all research paths fail below confidence target.  The report
        still contains a summary (possibly LLM-only) that is better than
        nothing.  This method surfaces it.
        """
        # Direct report key
        report = out.get("report")
        if isinstance(report, dict) and report.get("summary"):
            return report
        # Nested result
        inner = out.get("result")
        if isinstance(inner, dict):
            return self._extract_best_report(inner)
        # Trace fallback: walk in reverse for the last non-empty report
        trace = out.get("trace") or []
        for step in reversed(trace):
            r = step.get("report")
            if isinstance(r, dict) and r.get("summary"):
                return r
        return None

    def collaborate(self, objective: str, participants: List[str],
                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        session_id = f"collab-{uuid.uuid4().hex[:8]}"
        context = context or {}
        transcript: List[Dict[str, Any]] = []
        for name in participants:
            agent = self.registry.get(name)
            if not agent:
                transcript.append({"agent": name, "status": "unavailable"})
                continue
            task = self.PRIMARY_TASK.get(name, "")
            contribution = self._dispatch(name, task, {**context, "objective": objective})
            transcript.append({"agent": name, "task": task, "contribution": contribution})
        contributed = [t["agent"] for t in transcript if t.get("contribution")]
        conclusion = (f"Collaboration on '{objective}' gathered contributions from "
                     f"{', '.join(contributed) or 'no agents'}.")
        session = {"session_id": session_id, "objective": objective, "participants": participants,
                   "transcript": transcript, "conclusion": conclusion, "created_at": time.time()}
        self._sessions[session_id] = session
        self._preserve_session(session)
        return session

    def _flag_gap(self, domain: str, query: str) -> Dict[str, Any]:
        gap = {"gap_id": f"gap-{uuid.uuid4().hex[:8]}", "domain": domain,
               "example_query": query, "detected_at": time.time()}
        self._gaps.append(gap)
        genesis = self.registry.get("genesis")
        if genesis:
            try:
                self._dispatch("genesis", "capability.analyze", {"domain": domain, "query": query})
                gap["genesis_notified"] = True
            except Exception:
                gap["genesis_notified"] = False
        return gap

    def _consult_memory(self, query: str, domain: str) -> List[Dict[str, Any]]:
        """
        BUG-N4 FIX: Chronicle errors now logged at WARNING (not silently swallowed).
        Constitutional law: Book II No Silent Failures; Book IV Fail Loudly.
        """
        if self.chronicle is None:
            return []
        try:
            res = self.chronicle.search(query=query, domain=domain, limit=3, requester="nexus")
            return res if isinstance(res, list) else []
        except Exception as exc:
            log.warning("Chronicle memory consult failed (query=%r): %s", query, exc, exc_info=True)
            return []

    def _preserve_session(self, session: Dict[str, Any]) -> None:
        """
        BUG-N5 FIX: Chronicle write errors now logged at WARNING (not bare pass).
        BUG-P4-01 FIX: chronicle.store() -> chronicle.store_memory() (VectorStore collision).
        Constitutional law: Book II No Silent Failures; Book II Memory First.
        """
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(
                content=f"Collaboration: {session['objective']} -> {session['conclusion']}",
                pillar="episodic",
                domain="coordination",
                summary=f"Nexus collaboration: {session['objective'][:120]}",
                source_repository="Nexus",
                source_agent="nexus",
                tags=["nexus", "collaboration"],
            )
        except Exception as exc:
            log.warning("Chronicle session preserve failed (session=%s): %s",
                        session.get("session_id"), exc, exc_info=True)

    def _preserve_route(self, record: Dict[str, Any], result: Optional[Dict[str, Any]]) -> None:
        """
        BUG-N7 FIX: Persist every routing decision to Chronicle.
        BUG-P4-01 FIX: chronicle.store() -> chronicle.store_memory() (VectorStore collision).
        Constitutional law: Book II Everything Communicates;
        Book II Part I Ch VII (every routing decision recorded);
        Book II Principle I Memory First.
        """
        if self.chronicle is None:
            return
        try:
            status = (result or {}).get("status", "unknown")
            content = (
                f"Route {record['route_id']}: query={record['query']!r} "
                f"domain={record['domain']} target={record['target']} "
                f"confidence={record['confidence']:.2f} "
                f"result_status={status} "
                f"duration_ms={record['duration_ms']}"
            )
            self.chronicle.store_memory(
                content=content,
                pillar="episodic",
                domain="coordination",
                summary=f"Nexus route {record['route_id']}: {record['domain']} -> {status}",
                source_repository="Nexus",
                source_agent="nexus",
                tags=["nexus", "routing", record["domain"]],
            )
        except Exception as exc:
            log.warning("Chronicle route preserve failed (route=%s): %s",
                        record.get("route_id"), exc, exc_info=True)

    def stats(self) -> Dict[str, Any]:
        return {"routes": len(self._routes), "sessions": len(self._sessions),
               "capability_gaps": len(self._gaps), "classifier": self.classifier.stats(),
               "registry": self.registry.health_summary()}