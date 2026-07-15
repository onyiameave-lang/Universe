"""
Nexus.agents.coordinator_agent
=============================
Nexus (formerly Universal AI): the institutional coordinator, on the
constitutional BaseAgent. (Book II Part I; Book II Part II Ch VIII; Book I
Article IX.)

Institutional coordination:
  * EXECUTION LAYER   every dispatch runs under an SLA/budget, guarded by a
    per-agent circuit breaker, with TTL result caching (core.execution).
  * PARALLEL ORCHESTRATION  multi-agent queries run independent sub-tasks
    concurrently in dependency levels (core.orchestration).
  * CONFIDENCE-WEIGHTED CONFLICT RESOLUTION when specialists disagree.
  * LEARNED COLLABORATION GRAPH  who feeds whom, learned from outcomes.
  * ROUTING reasoning  direct / memory-first / orchestrate, with the classifier
    self-reinforcing and PRIORITY lanes for urgent queries.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

from intelligence.domain_classifier import DomainClassifier       # type: ignore
from intelligence.collaboration_graph import CollaborationGraph    # type: ignore
from core.agent_registry import AgentRegistry                      # type: ignore
from core.coordination_engine import CoordinationEngine            # type: ignore
from core.execution import Executor, PRIORITY_BUDGET               # type: ignore
from core.orchestration import Orchestrator                        # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "nexus")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("nexus")

PRIORITY_WORDS = {"urgent": 2, "critical": 2, "emergency": 1, "now": 2, "immediately": 1}


class NexusAgent(BaseAgent):
    name = "nexus"
    repository = "Nexus"
    domain = "coordination"
    description = "Institutional coordinator: SLAs, breakers, parallel orchestration, learned collaboration."
    capabilities = ["domain.classify", "ecosystem.route", "ecosystem.orchestrate", "ecosystem.monitor",
                    "collaboration.create", "collaboration.graph", "execution.stats", "ecosystem.reinforce"]
    channels = ["ecosystem.coordination", "ecosystem.broadcast", "ecosystem.health"]
    memory_namespace = "nexus_memory"
    security_level = "constitutional"
    mission = {"purpose": "Route, orchestrate in parallel under SLAs, and learn how the civilization cooperates."}

    def __init__(self, chronicle_client=None, atlas_client=None, **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                        storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.classifier = DomainClassifier(storage_dir=str(_REPO_ROOT / "memory"))
        self.graph = CollaborationGraph(storage_dir=str(_REPO_ROOT / "memory"))
        self.registry = AgentRegistry()
        self.engine = CoordinationEngine(self.registry, self.classifier, chronicle_client)
        self.executor = Executor(self.registry, self.engine._dispatch)
        self.orchestrator = Orchestrator(self.registry, self.classifier, self.executor, self.graph,
                                        chronicle=chronicle_client, atlas=atlas_client, llm=self.llm)
        if chronicle_client is not None:
            self.registry.register("chronicle", chronicle_client)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("routing", "direct_specialist", "_strat_direct",
            reasons_for=["fastest; single clear specialist"], reasons_against=["misses multi-domain nuance"])
        self.reasoning.register_strategy("routing", "memory_first", "_strat_memory_first",
            reasons_for=["grounded in prior knowledge"], reasons_against=["slightly slower"])
        self.reasoning.register_strategy("routing", "orchestrate", "_strat_orchestrate",
            reasons_for=["required for multi-domain queries; parallel + SLA-bounded"],
            reasons_against=["uses several agents"])

    def on_start(self) -> None:
        log.info("Nexus institutional online. Registered: %s", list(self.registry.all().keys()))

    def register_agent(self, name: str, handle: Any) -> Dict[str, Any]:
        ra = self.registry.register(name, handle)
        return {"registered": name, "domain": ra.domain, "capabilities": ra.capabilities}

    def _priority(self, query: str) -> int:
        q = query.lower()
        for w, p in PRIORITY_WORDS.items():
            if w in q:
                return p
        return 4

    # ---- routing strategies ----

    def _strat_direct(self, c):
        if self._needs_multiple(c.get("query", "")):
            return {"status": "error", "message": "multi-domain; not direct"}
        cls = self.classifier.classify(c.get("query", ""))
        agent = self.registry.get(cls["repository"]) or self.registry.find_by_domain(cls["domain"])
        if agent is None:
            return {"status": "error", "message": f"no agent for {cls['domain']} (repository={cls['repository']})"}
        from core.orchestration import PRIMARY_TASK
        out = self.executor.call(agent.name, PRIMARY_TASK.get(agent.name, ""),
                               {"query": c.get("query", ""), "symbol": c.get("query", "").upper()},
                               priority=c.get("priority", 4))
        status = "complete" if out.get("status") != "error" else "error"
        return {"status": status, "routed_to": agent.name, "result": out,
               "classification": cls,
               "message": out.get("message", "") if status == "error" else ""}

    def _strat_memory_first(self, c):
        if self._needs_multiple(c.get("query", "")):
            return {"status": "error", "message": "multi-domain; needs orchestration"}
        mem = self.executor.call("chronicle", "memory.answer", {"query": c.get("query", "")},
                               priority=c.get("priority", 4)) if self.registry.get("chronicle") else None
        direct = self._strat_direct(c)
        direct["memory_context"] = mem
        return direct

    def _strat_orchestrate(self, c):
        session = self.orchestrator.run(c.get("query", ""), c.get("user_id", "user"),
                                      priority=c.get("priority", 4))
        if not session.get("multi_agent"):
            return {"status": "error", "message": "single-domain; orchestration not needed"}
        return {"status": "complete", "session": session, "degraded": bool(session.get("missing_agents"))}

    def _needs_multiple(self, query: str) -> bool:
        return len(self.orchestrator.decompose(query)) >= 2

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        priority = ctx.get("priority", self._priority(ctx.get("query", "")))
        if task == "domain.classify":
            return {"status": "complete", "classification": self.classifier.classify(ctx.get("query", ""))}
        if task == "ecosystem.orchestrate":
            return {"status": "complete", "session": self.orchestrator.run(
                ctx.get("query", ""), ctx.get("user_id", "user"), priority=priority)}
        if task == "ecosystem.route":
            if self.reasoning is not None:
                solved = self.solve("routing", {"query": ctx.get("query", ""),
                                               "user_id": ctx.get("user_id", "user"), "priority": priority})
                cls = self.classifier.classify(ctx.get("query", ""))
                if cls.get("domain") and cls["domain"] != "general":
                    self.classifier.reinforce(ctx.get("query", ""), cls["domain"], weight=0.3)
                return {"status": solved.get("status", "complete"), "priority": priority, **solved}
            return {"status": "complete", **self.engine.route(ctx.get("query", ""), ctx.get("user_id", "user"))}
        if task == "collaboration.create":
            return {"status": "complete", "session": self.engine.collaborate(
                ctx.get("objective", ""), ctx.get("participants", []), ctx.get("context"))}
        if task == "collaboration.graph":
            return {"status": "complete", "graph": self.graph.stats()}
        if task == "execution.stats":
            return {"status": "complete", "execution": self.executor.stats()}
        if task == "ecosystem.monitor":
            stats = self.engine.stats()
            stats["collaboration_learning"] = self.graph.stats()
            stats["execution"] = self.executor.stats()
            return {"status": "complete", "stats": stats}
        if task == "ecosystem.reinforce":
            if ctx.get("query") and ctx.get("domain"):
                self.classifier.reinforce(ctx["query"], ctx["domain"])
            return {"status": "complete", "reinforced": True}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["engine"] = self.engine.stats()
        base["execution"] = self.executor.stats()
        base["collaboration_graph"] = self.graph.stats()
        return base

    def route(self, query: str, user_id: str = "user"):
        return self.act("ecosystem.route", {"query": query, "user_id": user_id, "_sender": "user"})