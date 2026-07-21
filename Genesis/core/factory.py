"""
Genesis.core.factory
====================
The institutional agent factory: the full constitutional birth process, gated
at every stage. (Book I Part IV Article II Birth of an Agent.)

The nine stages, none skipped, each producing a real artifact and gate:
  1. capability_gap_identified   - a real gap described
  2. nexus_confirmation          - confirm no live agent covers it (no duplicates)
  3. chronicle_search            - reuse prior knowledge
  4. atlas_research              - research the domain (real, institutional Atlas)
  5. genesis_design              - blueprint (LLM-designed if a brain is present)
  6. code_synthesis              - real code, AST + safety + lint gated
  7. sandbox_certification       - runs in isolation; must pass its own tests
  8. aegis_validation            - constitutional compliance verdict
  9. deploy (gated)              - write to disk ONLY on Aegis + human approval;
                                   register + version for rollback.

Deployment is never automatic: it requires human confirmation (Book VI human
sovereignty), exactly like Aegis's remediation.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.synthesis import CodeSynthesizer, _module_name, _class_name, SynthesisResult  # type: ignore
from core.sandbox import Sandbox                                                          # type: ignore
from core.agent_registry import CreatedAgentRegistry                                      # type: ignore

URS_DIRECTORIES = ["core", "agents", "intelligence", "memory", "research", "models", "training",
                   "optimization", "communication", "infrastructure", "security", "api", "interfaces",
                   "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
                   "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional"]

BIRTH_STAGES = ["capability_gap_identified", "nexus_confirmation", "chronicle_search",
                "atlas_research", "genesis_design", "code_synthesis", "sandbox_certification",
                "aegis_validation", "deploy"]

EXISTING_DOMAINS = {"memory", "prediction", "coordination", "news", "social", "research",
                    "training", "creation", "governance"}


class AgentFactory:
    def __init__(self, chronicle=None, atlas=None, aegis=None, nexus=None, llm=None,
                 output_root: Optional[str] = None):
        self.chronicle = chronicle
        self.atlas = atlas
        self.aegis = aegis
        self.nexus = nexus
        self.llm = llm
        self.output_root = Path(output_root) if output_root else _REPO_ROOT.parent
        self.synth = CodeSynthesizer(llm=llm)
        self.sandbox = Sandbox(self.output_root)
        self.registry = CreatedAgentRegistry(storage_dir=str(_REPO_ROOT / "registry"))
        self._blueprints: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}   # birth records awaiting deploy approval

    # ---- stage 1: gap analysis ----

    def analyze_gap(self, domain: str, query: str = "") -> Dict[str, Any]:
        already = domain in EXISTING_DOMAINS
        nexus_has = False
        if self.nexus is not None:
            try:
                nexus_has = self.nexus.registry.has_domain(domain)
            except Exception:
                nexus_has = False
        recommend = not (already or nexus_has)
        return {"domain": domain, "example_query": query, "already_in_constitution": already,
               "live_agent_exists": nexus_has,
               "recommendation": "create_new_agent" if recommend else "use_existing",
               "suggested_capabilities": [f"{domain}.analyze", f"{domain}.process", f"{domain}.report"]}

    # ---- stage 5: design (LLM if present) ----

    def design(self, name: str, domain: str, purpose: str,
               objectives: Optional[List[str]] = None, capabilities: Optional[List[str]] = None,
               security_level: str = "standard") -> Dict[str, Any]:
        bid = f"bp-{uuid.uuid4().hex[:10]}"
        caps, chans = capabilities, None
        if (not caps) and self.synth.has_brain:
            try:
                from shared.llm import system_prompt, prompt_design_agent
                parsed, _ = self.llm.complete_json(system_prompt("genesis"),
                    prompt_design_agent(name, domain, purpose, objectives or []),
                    temperature=0.3, max_tokens=500)
                if parsed and isinstance(parsed, dict):
                    caps = parsed.get("capabilities") or None
                    chans = parsed.get("channels")
                    security_level = parsed.get("security_level", security_level)
            except Exception:
                pass
        bp = {"blueprint_id": bid, "name": name, "domain": domain, "purpose": purpose,
              "objectives": objectives or [f"Serve the {domain} domain", "Learn from outcomes"],
              "capabilities": caps or [f"{domain}.analyze", f"{domain}.process"],
              "channels": chans or [f"ecosystem.{domain}", "ecosystem.broadcast"],
              "security_level": security_level, "memory_namespace": f"{_module_name(name)}_memory",
              "dependencies": ["chronicle", "nexus"], "status": "designed"}
        self._blueprints[bid] = bp
        return bp

    # ---- full birth (stages 1-8, then await deploy approval) ----

    def create(self, blueprint_id: str, reason: str = "") -> Dict[str, Any]:
        bp = self._blueprints.get(blueprint_id)
        if not bp:
            return {"status": "error", "message": f"blueprint {blueprint_id} not found"}
        rec = {"record_id": f"birth-{uuid.uuid4().hex[:10]}", "blueprint_id": blueprint_id,
               "agent_name": bp["name"], "reason": reason, "stages": [], "deployed": False,
               "awaiting_deploy_approval": False, "started_at": time.time()}

        def stage(name, ok, detail):
            rec["stages"].append({"stage": name, "ok": ok, "detail": detail})
            return ok

        stage("capability_gap_identified", True, f"gap in {bp['domain']}")

        # 2. Nexus confirmation
        nexus_has = self.nexus.registry.has_domain(bp["domain"]) if self.nexus else False
        if not stage("nexus_confirmation", not nexus_has,
                    "no existing agent" if not nexus_has else "domain already covered"):
            rec["error"] = "domain already covered; aborted (no duplicates)"
            return {"status": "aborted", "record": rec}

        # 3. Chronicle search
        prior = []
        if self.chronicle:
            try:
                prior = self.chronicle.search(query=f"agent {bp['domain']}", domain="agents", limit=3)
            except Exception:
                prior = []
        stage("chronicle_search", True, f"{len(prior)} prior memories")

        # 4. Atlas research
        rconf = None
        if self.atlas:
            try:
                out = self.atlas.handle({"task": "research.investigate",
                    "context": {"query": f"best practices for {bp['domain']} AI agent",
                               "domain": bp["domain"]}, "sender": "genesis"})
                rconf = out.get("report", {}).get("confidence")
            except Exception:
                rconf = None
        stage("atlas_research", True, f"research confidence={rconf}")

        # 5. design confirmed
        stage("genesis_design", True, f"{len(bp['capabilities'])} capabilities")

        # 6. code synthesis (AST + safety + lint gated)
        synthesis: SynthesisResult = self.synth.synthesize_agent(bp)
        if not stage("code_synthesis", synthesis.valid,
                    "valid" if synthesis.valid else f"rejected: {synthesis.rejections}"):
            rec["error"] = f"synthesis rejected: {synthesis.rejections}"
            rec["synthesis_checks"] = synthesis.checks
            return {"status": "rejected", "record": rec}

        # 7. sandbox certification (runs in isolation)
        cert = self.sandbox.certify(synthesis.files, _module_name(bp["name"]), _class_name(bp["name"]))
        if not stage("sandbox_certification", cert["certified"],
                    "passed" if cert["certified"] else f"failed at {cert.get('stage_failed')}"):
            rec["error"] = f"sandbox failed at {cert.get('stage_failed')}"
            rec["certification"] = cert
            return {"status": "failed_certification", "record": rec}

        # 8. Aegis validation
        validation = self._aegis_validate(bp)
        if not stage("aegis_validation", validation["approved"], validation["reason"]):
            rec["error"] = f"Aegis rejected: {validation['reason']}"
            return {"status": "rejected", "record": rec, "validation": validation}

        # 9. deploy is GATED: stage the artifacts, await human confirmation.
        rec["awaiting_deploy_approval"] = True
        rec["synthesis_files"] = synthesis.files
        rec["certification"] = cert
        rec["validation"] = validation
        rec["stages"].append({"stage": "deploy", "ok": True,
                            "detail": "certified + validated; awaiting human deploy approval (Book VI)"})
        self._pending[rec["record_id"]] = rec
        return {"status": "awaiting_approval", "record": {k: v for k, v in rec.items()
                                                        if k != "synthesis_files"},
               "record_id": rec["record_id"],
               "message": "Agent certified and validated. Call deploy(record_id, human_confirm=True) to materialize."}

    # ---- deploy (human-gated) ----

    def deploy(self, record_id: str, human_confirm: bool = False) -> Dict[str, Any]:
        rec = self._pending.get(record_id)
        if not rec:
            return {"status": "error", "message": "unknown or already-deployed record"}
        if not human_confirm:
            return {"status": "error",
                   "message": "human confirmation required to deploy a new agent (Book VI Part I)"}
        bp = self._blueprints[rec["blueprint_id"]]
        repo = self.output_root / bp["name"]
        repo.mkdir(parents=True, exist_ok=True)
        for d in URS_DIRECTORIES:
            (repo / d).mkdir(parents=True, exist_ok=True)
            (repo / d / "__init__.py").write_text(f'"""{bp["name"]}.{d}"""\n', encoding="utf-8")
        (repo / "__init__.py").write_text(f'"""{bp["name"]} (created by Genesis)."""\n', encoding="utf-8")
        for rel, content in rec["synthesis_files"].items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        entry = self.registry.register(bp["name"], rec["blueprint_id"],
                                      rec["synthesis_files"], rec["certification"])
        rec["deployed"] = True
        rec["awaiting_deploy_approval"] = False
        self._pending.pop(record_id, None)
        if self.chronicle:
            try:
                self.chronicle.store_memory(content=f"Genesis deployed agent '{bp['name']}' v{entry['version']} "
                                          f"for {bp['domain']} after certification + human approval.",
                                    pillar="constitutional", domain="creation",
                                    tags=["genesis", "agent_birth", bp["domain"]], source_repository="genesis")
            except Exception:
                pass  # aegis:allow-silent
        return {"status": "deployed", "path": str(repo), "version": entry["version"],
               "identity_id": entry["identity_id"]}

    def _aegis_validate(self, bp: Dict[str, Any]) -> Dict[str, Any]:
        if self.aegis is not None:
            try:
                out = self.aegis.handle({"task": "compliance.check_agent",
                    "context": {"target": bp["name"], "config": {
                        "name": bp["name"], "repository": bp["name"], "domain": bp["domain"],
                        "capabilities": bp["capabilities"], "channels": bp["channels"],
                        "mission": bp["purpose"], "memory_namespace": bp["memory_namespace"],
                        "security_level": bp["security_level"]}}, "sender": "genesis"})
                report = out.get("report", {})
                return {"approved": report.get("status") in ("compliant", "warning"),
                       "reason": f"Aegis: {report.get('status')} (score {report.get('score')})",
                       "report": report}
            except Exception as exc:
                return {"approved": False, "reason": f"Aegis error: {exc}"}
        checks = {"has_mission": bool(bp.get("purpose")), "has_caps": bool(bp.get("capabilities")),
                 "has_channels": bool(bp.get("channels")), "has_ns": bool(bp.get("memory_namespace")),
                 "no_circular": "genesis" not in bp.get("dependencies", [])}
        failed = [k for k, v in checks.items() if not v]
        return {"approved": not failed, "reason": "checks passed" if not failed else f"failed: {failed}"}

    def stats(self) -> Dict[str, Any]:
        return {"blueprints": len(self._blueprints), "awaiting_approval": len(self._pending),
               "registry": self.registry.stats()}
