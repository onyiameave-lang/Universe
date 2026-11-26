"""
Genesis.agents.creator_agent
===========================
Genesis (formerly Agent Factory): institutional agent creation, on the
constitutional BaseAgent. (Book I Part IV Article II; Book III Part II Ch IV.)

Genesis reasons about HOW to fill a capability gap via `solve("gap_response", ...)`:
  * evolve_existing   - if a live agent nearly covers it, recommend evolution
                        (cheaper, preserves identity) instead of a new agent.
  * create_new        - synthesize + certify + (human-)deploy a new agent.
  * defer_to_research - if the domain is poorly understood, send Atlas to
                        research first, then decide.

It learns which response actually resolves gaps. Creation itself is fully gated:
synthesis (AST+safety+lint) -> sandbox certification -> Aegis -> human deploy.
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

from core.factory import AgentFactory  # type: ignore
from core.control_plane import GenesisControlPlane  # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "genesis")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("genesis")


class GenesisAgent(BaseAgent):
    name = "genesis"
    repository = "Genesis"
    domain = "creation"
    description = "Institutional agent factory & durable capability control plane: design, synthesize, certify, gate, deploy."
    capabilities = [
        "capability.analyze", "agent.design", "agent.create", "agent.deploy",
        "agent.rollback", "agent.retire", "gap.respond", "registry.list",
        "case.open", "case.advance", "case.approve_release", "case.rollback",
        "case.list", "case.get", "control_plane.snapshot",
    ]
    channels = ["ecosystem.creation", "ecosystem.agents", "ecosystem.broadcast"]
    memory_namespace = "genesis_memory"
    security_level = "elevated"
    mission = {"purpose": "Create, certify, and responsibly deploy new autonomous agents under the 8-stage durable control plane."}

    def __init__(self, chronicle_client=None, atlas_client=None, aegis_client=None,
                 nexus_client=None, forge_client=None, output_root=None, **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                        storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.factory = AgentFactory(chronicle=chronicle_client, atlas=atlas_client,
                                   aegis=aegis_client, nexus=nexus_client, llm=self.llm,
                                   output_root=output_root or str(_ECO_ROOT))
        self.control_plane = GenesisControlPlane(
            factory=self.factory,
            chronicle=chronicle_client,
            atlas=atlas_client,
            aegis=aegis_client,
            nexus=nexus_client,
            forge=forge_client,
        )

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("gap_response", "create_new", "_strat_create_new",
            reasons_for=["fills a genuinely uncovered domain"],
            reasons_against=["most expensive; new surface to maintain"])
        self.reasoning.register_strategy("gap_response", "evolve_existing", "_strat_evolve",
            reasons_for=["cheaper; preserves identity"],
            reasons_against=["only viable if an agent nearly covers it"])
        self.reasoning.register_strategy("gap_response", "defer_to_research", "_strat_defer",
            reasons_for=["avoids building on a poorly-understood domain"],
            reasons_against=["adds latency before resolution"])

    def on_start(self) -> None:
        log.info("Genesis institutional factory online. Brain: %s | created: %d",
                 self.has_brain, self.factory.registry.stats()["total_created"])

    # ---- gap-response strategy handlers ----

    def _strat_create_new(self, c) -> Dict[str, Any]:
        analysis = self.factory.analyze_gap(c.get("domain", ""), c.get("query", ""))
        if analysis["recommendation"] != "create_new_agent":
            return {"status": "error", "message": "domain already covered", "analysis": analysis}
        bp = self.factory.design(c.get("name") or f"{c.get('domain','X').title()}Agent",
                               c.get("domain", ""), c.get("purpose", f"Handle {c.get('domain')} tasks"),
                               c.get("objectives"), c.get("capabilities"))
        result = self.factory.create(bp["blueprint_id"], reason=c.get("reason", "gap response"))
        ok = result.get("status") in ("awaiting_approval", "deployed")
        return {"status": "complete" if ok else "error", "creation": result, "blueprint": bp}

    def _strat_evolve(self, c) -> Dict[str, Any]:
        # viable only if a live agent shares the domain (near-coverage)
        if self.factory.nexus and self.factory.nexus.registry.has_domain(c.get("domain", "")):
            return {"status": "complete", "action": "recommend_evolution",
                   "message": f"a live agent covers {c.get('domain')}; recommend evolving it"}
        return {"status": "error", "message": "no near-coverage agent to evolve"}

    def _strat_defer(self, c) -> Dict[str, Any]:
        if self.atlas is None:
            return {"status": "error", "message": "Atlas unavailable to research the domain"}
        report = self.research(f"how should an AI agent handle {c.get('domain')} tasks",
                             domain=c.get("domain", "general"))
        ok = bool(report) and (report or {}).get("confidence", 0) >= 0.4
        return {"status": "complete" if ok else "error", "action": "researched_first",
               "research": report}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context or {}
        if task == "capability.analyze":
            return {"status": "complete", "analysis": self.factory.analyze_gap(
                ctx.get("domain", ""), ctx.get("query", ""))}
        if task == "gap.respond":
            if self.reasoning is not None:
                return self.solve("gap_response", {"domain": ctx.get("domain", ""),
                    "query": ctx.get("query", ""), "name": ctx.get("name"),
                    "purpose": ctx.get("purpose"), "capabilities": ctx.get("capabilities"),
                    "objectives": ctx.get("objectives"), "reason": ctx.get("reason", "gap")})
            return self._strat_create_new(ctx)
        if task == "agent.design":
            return {"status": "complete", "blueprint": self.factory.design(
                ctx.get("name", ""), ctx.get("domain", ""), ctx.get("purpose", ""),
                ctx.get("objectives"), ctx.get("capabilities"),
                ctx.get("security_level", "standard"))}
        if task == "agent.create":
            return self.factory.create(ctx.get("blueprint_id", ""), ctx.get("reason", ""))
        if task == "agent.deploy":
            return self.factory.deploy(ctx.get("record_id", ""),
                                     human_confirm=ctx.get("human_confirm", False))
        if task == "agent.rollback":
            target = Path(self.factory.output_root) / ctx.get("name", "")
            return self.factory.registry.rollback(ctx.get("name", ""),
                                                 ctx.get("to_version", 1), target)
        if task == "agent.retire":
            return self.factory.registry.retire(ctx.get("name", ""), ctx.get("reason", ""))
        if task == "registry.list":
            return {"status": "complete", "registry": self.factory.registry.stats()}
        if task == "case.open":
            return self.control_plane.open_case(
                title=ctx.get("title", ""),
                domain=ctx.get("domain", ""),
                trigger_type=ctx.get("trigger_type", "unmet_user_request"),
                problem_statement=ctx.get("problem_statement", ""),
                requested_by=ctx.get("requested_by", ctx.get("_sender", "operator")),
                parent_case_id=ctx.get("parent_case_id"),
                preferred_decision=ctx.get("preferred_decision", "auto"),
                proposed_name=ctx.get("proposed_name"),
                capabilities=ctx.get("capabilities"),
                security_level=ctx.get("security_level", "standard"),
                worker_step=int(ctx.get("worker_step", 2)),
                auto_advance_to_review=bool(ctx.get("auto_advance_to_review", True)),
            )
        if task == "case.advance":
            return self.control_plane.advance_case(
                ctx.get("case_id", ""),
                target_stage=ctx.get("target_stage"),
                override_decision=ctx.get("override_decision"),
                worker_step=ctx.get("worker_step"),
            )
        if task == "case.approve_release":
            return self.control_plane.approve_and_deploy_case(
                ctx.get("case_id", ""),
                human_approver=ctx.get("human_approver", "operator"),
                release_scope=ctx.get("release_scope", "staged_canary"),
                human_confirm=bool(ctx.get("human_confirm", True)),
            )
        if task == "case.rollback":
            return self.control_plane.rollback_case(
                ctx.get("case_id", ""),
                reason=ctx.get("reason", "Operator triggered canary rollback"),
            )
        if task == "case.list":
            return {"status": "complete", "cases": self.control_plane.list_cases()}
        if task == "case.get":
            c = self.control_plane.get_case(ctx.get("case_id", ""))
            return {"status": "complete" if c else "unavailable", "case": c}
        if task == "control_plane.snapshot":
            return self.control_plane.get_dashboard_snapshot()
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["factory"] = self.factory.stats()
        base["control_plane"] = {
            "active_cases": len(self.control_plane.list_cases()),
            "recent_handoffs": len(self.control_plane.get_recent_handoffs(limit=10)),
        }
        return base
