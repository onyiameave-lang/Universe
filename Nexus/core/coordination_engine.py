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
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from Nexus.intelligence.domain_classifier import DomainClassifier  # type: ignore
from Nexus.core.agent_registry import AgentRegistry                # type: ignore


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
            result = self._dispatch(agent.name, task, {"query": query, "symbol": query.upper(),
                                                       "domain": domain, "user_id": user_id,
                                                       "memory_context": memory})
        record = {"route_id": f"route-{uuid.uuid4().hex[:8]}", "query": query, "domain": domain,
                  "target": target, "confidence": classification["confidence"],
                  "duration_ms": round((time.time() - started) * 1000, 1)}
        self._routes.append(record)
        return {"query": query, "classification": classification, "routed_to": target,
                "result": result, "memory_consulted": bool(memory), "route": record}

    def _dispatch(self, agent_name: str, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        agent = self.registry.get(agent_name)
        if not agent:
            return {"status": "error", "message": f"{agent_name} not registered"}
        start = time.time()
        success = True
        try:
            out = agent.handle.handle({"sender": "nexus", "receiver": agent_name,
                                      "task": task, "context": context}) \
                if hasattr(agent.handle, "handle") else \
                agent.handle({"sender": "nexus", "receiver": agent_name, "task": task, "context": context})
            success = out.get("status") != "error" if isinstance(out, dict) else True
        except Exception as exc:
            success = False
            out = {"status": "error", "message": str(exc)}
        self.registry.record_call(agent_name, time.time() - start, success)
        return out

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
        if self.chronicle is None:
            return []
        try:
            res = self.chronicle.search(query=query, domain=domain, limit=3, requester="nexus")
            return res if isinstance(res, list) else []
        except Exception:
            return []

    def _preserve_session(self, session: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            self.chronicle.store(content=f"Collaboration: {session['objective']} -> {session['conclusion']}",
                                memory_type="episodic", domain="coordination",
                                tags=["nexus", "collaboration"], source="nexus")
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        return {"routes": len(self._routes), "sessions": len(self._sessions),
               "capability_gaps": len(self._gaps), "classifier": self.classifier.stats(),
               "registry": self.registry.health_summary()}