"""
Aegis.agents.auditor_agent
=========================
Aegis (formerly Auditor): an institutional control function, on the
constitutional BaseAgent. (Book I Article VI/XIII; Book VI Human Constitution.)

Institutional capabilities:
  * POLICY-AS-DATA        versioned, human-amendable rulebook (core.policy).
  * RISK REGISTER         time-decayed per-entity exposure + trend detection.
  * ANOMALY DETECTION     statistical baselines catch unknown bad behavior.
  * CONTINUOUS MONITORING background sweeps + proactive alerts.
  * RISK-WEIGHTED RESPONSE threat response driven by EXPOSURE, not raw counts,
    via the reasoning loop (warn / quarantine / self-heal / escalate).
  * LEARNED THRESHOLDS    escalation thresholds adapt from outcomes.
  * SELF-HEALING          chronic failure -> remediation ladder (research, code
    fix proposal, new-agent, human), fixes never auto-applied.
  * HASH-CHAINED AUDIT     tamper-evident, verifiable history.
"""
from __future__ import annotations

import json
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

from core.audit_log import AuditLog                                # type: ignore
from core.policy import PolicyStore                                # type: ignore
from core.risk_register import RiskRegister                        # type: ignore
from core.monitor import ContinuousMonitor                         # type: ignore
from intelligence.governance import ComplianceChecker, SecurityScanner, GovernanceReviewer  # type: ignore
from intelligence.anomaly import AnomalyDetector                   # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "aegis")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        def remember(self, **kw): return None
        has_brain = False
        def on_start(self): ...
        def on_stop(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self.on_stop(); self._started = False

try:
    from shared.remediation import RemediationEngine
    _HAS_REMEDIATION = True
except Exception:
    _HAS_REMEDIATION = False

log = logging.getLogger("aegis")

HIGH_RISK_ACTIONS = {"deploy", "delete", "retire", "modify_constitution",
                     "override_security", "spawn_agent"}


class AegisAgent(BaseAgent):
    name = "aegis"
    repository = "Aegis"
    domain = "governance"
    description = "Institutional governance: policy, risk, anomaly, monitoring, self-healing."
    capabilities = ["audit.action", "audit.log", "audit.verify", "security.scan",
                    "security.scan_directory", "compliance.check_repository", "compliance.check_agent",
                    "governance.review", "policy.list", "policy.amend", "risk.register",
                    "risk.exposure", "anomaly.observe", "anomaly.recent", "monitor.start",
                    "monitor.alerts", "remediation.trigger", "remediation.approve",
                    "remediation.apply", "ecosystem.health"]
    channels = ["ecosystem.audit", "ecosystem.security", "ecosystem.governance", "ecosystem.broadcast"]
    memory_namespace = "aegis_memory"
    security_level = "constitutional"
    mission = {"purpose": "Continuously govern, score risk, detect anomalies, and self-heal."}

    def __init__(self, chronicle_client=None, atlas_client=None, genesis_client=None,
                 storage_dir: Optional[str] = None, monitor: bool = False, **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                        storage_dir=storage_dir or str(_REPO_ROOT / "memory"), **kw)
        sdir = storage_dir or str(_REPO_ROOT / "security")
        self.audit_log = AuditLog(storage_dir=sdir)
        # Constitutional fix: wire Chronicle into AuditLog for cross-ecosystem
        # mirroring (Principles 2 & 6 — Everything Communicates / Nothing Dies).
        if chronicle_client is not None:
            self.audit_log.set_chronicle(chronicle_client)
        self.policies = PolicyStore(storage_dir=sdir)
        self.risk = RiskRegister(storage_dir=sdir)
        self.anomaly = AnomalyDetector()
        self.compliance = ComplianceChecker()
        self.security = SecurityScanner()
        self.reviewer = GovernanceReviewer()
        self._incidents: Dict[str, int] = {}
        self._quarantined: set = set()
        # LEARNED THRESHOLDS: adapt from outcomes rather than hardcoded 3/5.
        self._thresholds = {"quarantine_exposure": 1.5, "heal_exposure": 3.0, "escalate_exposure": 5.0}
        self.remediation = (RemediationEngine(chronicle=chronicle_client, atlas=atlas_client,
                            genesis=genesis_client, aegis=self, llm=self.llm,
                            staging_dir=str(_REPO_ROOT / "remediation_staging"))
                          if _HAS_REMEDIATION else None)
        self.monitor = ContinuousMonitor(self.risk, self.audit_log, self._scheduled_sweep)
        self._monitor_enabled = monitor
        self._sweep_repos: List[str] = []
        self.task_handlers = {
            "audit.action": self._handle_audit_action,
            "audit.log": self._handle_audit_log,
            "audit.verify": self._handle_audit_verify,
            "security.scan": self._handle_security_scan,
            "security.scan_directory": self._handle_security_scan_directory,
            "compliance.check_repository": self._handle_compliance_check_repository,
            "compliance.check_agent": self._handle_compliance_check_agent,
            "governance.review": self._handle_governance_review,
            "governance.approve": self._handle_governance_review,
            "policy.list": self._handle_policy_list,
            "policy.amend": self._handle_policy_amend,
            "risk.register": self._handle_risk_register,
            "risk.exposure": self._handle_risk_exposure,
            "anomaly.observe": self._handle_anomaly_observe,
            "anomaly.recent": self._handle_anomaly_recent,
            "monitor.start": self._handle_monitor_start,
            "monitor.alerts": self._handle_monitor_alerts,
            "remediation.trigger": self._handle_remediation_trigger,
            "remediation.approve": self._handle_remediation_approve,
            "remediation.apply": self._handle_remediation_apply,
            "ecosystem.health": self._handle_ecosystem_health,
        }

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("threat_response", "warn_and_monitor", "_strat_warn",
            reasons_for=["proportional for low exposure"], reasons_against=["weak for repeat offenders"])
        self.reasoning.register_strategy("threat_response", "quarantine", "_strat_quarantine",
            reasons_for=["stops active harm at moderate exposure"], reasons_against=["disruptive"])
        self.reasoning.register_strategy("threat_response", "self_heal", "_strat_self_heal",
            reasons_for=["fixes root cause at high exposure"], reasons_against=["needs research/LLM"])
        self.reasoning.register_strategy("threat_response", "escalate_human", "_strat_escalate",
            reasons_for=["human sovereignty for critical exposure"], reasons_against=["slow"])

    def on_start(self) -> None:
        integ = self.audit_log.verify_integrity()
        log.info("Aegis institutional online. Chain intact: %s | policies: %d | self-heal: %s",
                 integ.get("intact"), self.policies.stats()["total_policies"], self.remediation is not None)
        if self._monitor_enabled:
            self.monitor.start()

    def on_stop(self) -> None:
        self.monitor.stop()

    def set_sweep_targets(self, repos: List[str]) -> None:
        self._sweep_repos = repos
        self.monitor.watch(repos)

    # ---- risk-weighted enforcement ----

    def _consult_chronicle_for_entity(self, agent: str) -> Optional[Dict[str, Any]]:
        """Query Chronicle for prior audit history on *agent* before risk scoring.

        Constitutional basis: Principle 3 — "Memory First — retrieve before
        generating."  Aegis should know an entity's full audit history before
        assigning a risk score, not just the current incident.

        Returns a dict with 'prior_incidents' and 'prior_severity_max' if
        Chronicle has history, or None if Chronicle is unavailable / no history.
        """
        if self.chronicle is None:
            return None
        try:
            results = self.chronicle.search(
                query=f"AEGIS AUDIT agent={agent}",
                domain="audit",
                limit=10,
            )
            hits = results if isinstance(results, list) else (results or {}).get("results", [])
            if not hits:
                return None
            severity_order = {"info": 0, "warning": 1, "violation": 2, "critical": 3}
            max_sev = max(
                (severity_order.get(h.get("severity", "info"), 0) for h in hits),
                default=0,
            )
            sev_names = {v: k for k, v in severity_order.items()}
            return {
                "prior_incidents": len(hits),
                "prior_severity_max": sev_names.get(max_sev, "info"),
                "chronicle_history": True,
            }
        except Exception:
            return None  # Chronicle unavailable — proceed without history

    def audit_action(self, repository: str, agent: str, action: str,
                    context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = context or {}
        # ---- Principle 3: Memory First — consult Chronicle before scoring ----
        prior_history = self._consult_chronicle_for_entity(agent)
        if prior_history:
            context = {**context, **prior_history}
        violated = self._evaluate_policies(action, context)
        # accumulate risk exposure from each violated policy
        for pol in violated:
            self.risk.add_risk(agent, pol["risk"], pol["policy_id"])
        exposure = self.risk.exposure(agent)
        severity = ("critical" if any(v["severity"] == "critical" for v in violated)
                    else "violation" if violated
                    else "warning" if action in HIGH_RISK_ACTIONS else "info")
        entry = self.audit_log.append(repository, agent, action, severity=severity,
                                     context=context, violations=[v["policy_id"] for v in violated],
                                     detail="; ".join(v["title"] for v in violated) if violated else "recorded")
        response = None
        if violated:
            self._incidents[agent] = self._incidents.get(agent, 0) + 1
            if self.reasoning is not None:
                response = self.solve("threat_response",
                    {"agent": agent, "repository": repository, "severity": severity,
                     "exposure": exposure, "incident_count": self._incidents[agent],
                     "violations": [v["title"] for v in violated],
                     "error": context.get("error", ""), "target_file": context.get("target_file"),
                     "domain": context.get("domain", repository.lower())})
        return {"audit_id": entry["audit_id"], "severity": severity,
               "violated_policies": violated, "risk_exposure": exposure,
               "response": response, "quarantined": agent in self._quarantined}

    def _evaluate_policies(self, action: str, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Check the action against the policy-as-data set; return violated policies + risk."""
        violated = []
        for p in self.policies.all():
            if self._check_predicate(p.predicate, action, ctx):
                sev_scale = {"low": 0.5, "medium": 1.0, "high": 1.5, "critical": 2.0}.get(p.severity, 1.0)
                violated.append({"policy_id": p.policy_id, "title": p.title,
                               "severity": p.severity, "reference": p.reference,
                               "risk": round(p.base_risk * sev_scale, 4),
                               "remediable": p.remediable})
        return violated

    def _check_predicate(self, predicate: str, action: str, ctx: Dict[str, Any]) -> bool:
        """Return True if the policy is VIOLATED. Real checks per predicate name."""
        if predicate == "no_silent_failures":
            return bool(ctx.get("silent") or action == "silent_failure")
        if predicate == "no_hardcoded_secrets":
            return bool(ctx.get("hardcoded_secret"))
        if predicate == "logged_creation":
            return action == "spawn_agent" and not ctx.get("logged", True)
        if predicate == "no_security_bypass":
            return bool(ctx.get("bypasses_security"))
        if predicate == "human_authorized_constitution_change":
            return action == "modify_constitution" and not ctx.get("human_authorized")
        return False  # structural predicates handled by compliance sweeps, not per-action

    # ---- risk-weighted threat response (thresholds are LEARNED) ----

    def _strat_warn(self, c):
        exp = c.get("exposure", 0)
        ok = exp < self._thresholds["quarantine_exposure"] and c.get("severity") != "critical"
        self.audit_log.append(c.get("agent", ""), "aegis", "threat.warn", "warning",
                             detail=f"warned (exposure={exp})")
        self._adapt_threshold("warn", ok)
        return {"status": "complete" if ok else "error", "action": "warned", "exposure": exp}

    def _strat_quarantine(self, c):
        exp = c.get("exposure", 0)
        ok = self._thresholds["quarantine_exposure"] <= exp < self._thresholds["heal_exposure"]
        if ok:
            self._quarantined.add(c.get("agent", ""))
            self.audit_log.append(c.get("agent", ""), "aegis", "threat.quarantine", "critical",
                                 detail=f"quarantined (exposure={exp})")
        self._adapt_threshold("quarantine", ok)
        return {"status": "complete" if ok else "error",
               "action": "quarantined" if ok else "not_warranted", "exposure": exp}

    def _strat_self_heal(self, c):
        exp = c.get("exposure", 0)
        if self.remediation is None or exp < self._thresholds["heal_exposure"]:
            return {"status": "error", "action": "self_heal_not_warranted", "exposure": exp}
        case = self.remediation.remediate(repository=c.get("repository", ""), agent=c.get("agent", ""),
            failure_signature="; ".join(c.get("violations", [])) or "chronic failure",
            failure_count=c.get("incident_count", 0), error=c.get("error", ""),
            target_file=c.get("target_file"), context={"domain": c.get("domain")})
        resolved = case.get("resolution") in ("proposed_fix_awaiting_approval", "new_agent_recommended")
        self._adapt_threshold("self_heal", resolved)
        return {"status": "complete" if resolved else "error", "action": "self_heal", "case": case}

    def _strat_escalate(self, c):
        exp = c.get("exposure", 0)
        ok = c.get("severity") == "critical" or exp >= self._thresholds["escalate_exposure"]
        if ok:
            self.audit_log.append(c.get("agent", ""), "aegis", "threat.escalate", "critical",
                                 detail=f"escalated to human (exposure={exp})")
            self.remember(content=f"Escalated {c.get('agent')} to human oversight (exposure={exp}).",
                         memory_type="constitutional", domain="governance", tags=["aegis", "escalation"])
        return {"status": "complete" if ok else "error", "action": "escalated" if ok else "not_critical"}

    def _adapt_threshold(self, strategy: str, effective: bool) -> None:
        """Learned thresholds: nudge toward what worked (bounded)."""
        # if a response was NOT warranted (ineffective), tighten toward escalation
        key = {"warn": "quarantine_exposure", "quarantine": "heal_exposure",
               "self_heal": "escalate_exposure"}.get(strategy)
        if not key:
            return
        if not effective:
            self._thresholds[key] = max(0.5, round(self._thresholds[key] * 0.97, 3))
        else:
            self._thresholds[key] = min(10.0, round(self._thresholds[key] * 1.01, 3))

    # ---- continuous sweep callback ----

    def _scheduled_sweep(self) -> Dict[str, Any]:
        if not self._sweep_repos:
            return {"avg_compliance": None}
        total, n = 0.0, 0
        for repo in self._sweep_repos:
            repo_path = _ECO_ROOT / repo
            if not repo_path.exists():
                continue
            present = [d.name for d in repo_path.iterdir() if d.is_dir()]
            manifest = {}
            mp = repo_path / "repository.json"
            if mp.exists():
                try:
                    manifest = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    manifest = {}
            report = self.compliance.check_repository(repo, manifest, present)
            total += report["score"]; n += 1
        return {"avg_compliance": round(total / n, 3) if n else None, "repos": n}

    # ---- BaseAgent contract ----

    def _handle_audit_action(self, ctx, sender):
        return {"status": "complete", **self.audit_action(
            ctx.get("repository", sender), ctx.get("agent", sender),
            ctx.get("action", ""), ctx.get("action_context", {}))}

    def _handle_audit_log(self, ctx, sender):
        return {"status": "complete", "entries": self.audit_log.query(
            repository=ctx.get("repository"), severity=ctx.get("severity"),
            agent=ctx.get("agent"), limit=ctx.get("limit", 100))}

    def _handle_audit_verify(self, ctx, sender):
        return {"status": "complete", "integrity": self.audit_log.verify_integrity()}

    def _handle_security_scan(self, ctx, sender):
        return {"status": "complete", "scan": self.security.scan_source(
            ctx.get("source", ""), ctx.get("filename", ""))}

    def _handle_security_scan_directory(self, ctx, sender):
        return {"status": "complete", "scan": self.security.scan_directory(ctx.get("path", ""))}

    def _handle_compliance_check_repository(self, ctx, sender):
        report = self.compliance.check_repository(ctx.get("target", ""),
                                                    ctx.get("manifest", {}), ctx.get("present_dirs"))
        self.audit_log.append(report["target"], "aegis", "compliance.check", "info",
                                detail=f"score={report['score']}")
        return {"status": "complete", "report": report}

    def _handle_compliance_check_agent(self, ctx, sender):
        return {"status": "complete", "report": self.compliance.check_agent(
            ctx.get("target", ""), ctx.get("config", {}))}

    def _handle_governance_review(self, ctx, sender):
        decision = self.reviewer.review(ctx.get("request_type", ""), sender,
                                        ctx.get("target", ""), ctx.get("evidence", {}))
        self.audit_log.append(decision["target"], sender, f"governance.{decision['request_type']}",
                                "warning" if not decision["approved"] else "info", detail=decision["reasoning"])
        return {"status": "complete", "decision": decision}

    def _handle_policy_list(self, ctx, sender):
        return {"status": "complete", "policies": [p.to_dict() for p in self.policies.all(enabled_only=False)]}

    def _handle_policy_amend(self, ctx, sender):
        return {"status": "complete", **self.policies.amend(
            ctx.get("policy_id", ""), ctx.get("changes", {}), ctx.get("human_authorized", False))}

    def _handle_risk_register(self, ctx, sender):
        return {"status": "complete", "register": self.risk.register()}

    def _handle_risk_exposure(self, ctx, sender):
        return {"status": "complete", **self.risk.snapshot_trend(ctx.get("entity", sender))}

    def _handle_anomaly_observe(self, ctx, sender):
        record = self.anomaly.observe(ctx.get("agent", sender),
                                    ctx.get("latency_ms", 0.0), ctx.get("success", True))
        if record:
            self.risk.add_risk(ctx.get("agent", sender), 0.3, "ANOMALY")
            self.audit_log.append(ctx.get("agent", sender), "aegis", "anomaly.detected",
                                    "warning", detail=str(record["anomalies"]))
        return {"status": "complete", "anomaly": record}

    def _handle_anomaly_recent(self, ctx, sender):
        return {"status": "complete", "anomalies": self.anomaly.recent_anomalies(ctx.get("agent"))}

    def _handle_monitor_start(self, ctx, sender):
        self.set_sweep_targets(ctx.get("repos", []))
        self.monitor.start()
        return {"status": "complete", "monitor": self.monitor.stats()}

    def _handle_monitor_alerts(self, ctx, sender):
        return {"status": "complete", "alerts": self.monitor.recent_alerts()}

    def _handle_remediation_trigger(self, ctx, sender):
        if self.remediation is None: return {"status": "error", "message": "remediation unavailable"}
        return {"status": "complete", "case": self.remediation.remediate(
            repository=ctx.get("repository", ""), agent=ctx.get("agent", ""),
            failure_signature=ctx.get("signature", "manual"), failure_count=ctx.get("count", 5),
            error=ctx.get("error", ""), target_file=ctx.get("target_file"),
            context={"domain": ctx.get("domain")})}

    def _handle_remediation_approve(self, ctx, sender):
        if self.remediation is None: return {"status": "error", "message": "unavailable"}
        return {"status": "complete", **self.remediation.approve_fix(
            ctx.get("fix_id", ""), ctx.get("approver", "aegis"))}

    def _handle_remediation_apply(self, ctx, sender):
        if self.remediation is None: return {"status": "error", "message": "unavailable"}
        return {"status": "complete", **self.remediation.apply_fix(
            ctx.get("fix_id", ""), human_confirm=ctx.get("human_confirm", False))}

    def _handle_ecosystem_health(self, ctx, sender):
        return {"status": "complete", "health": self._health()}

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        sender = ctx.get("_sender", "unknown")

        if task == "audit.action":
            return {"status": "complete", **self.audit_action(
                ctx.get("repository", sender), ctx.get("agent", sender),
                ctx.get("action", ""), ctx.get("action_context", {}))}
        if task == "audit.log":
            return {"status": "complete", "entries": self.audit_log.query(
                repository=ctx.get("repository"), severity=ctx.get("severity"),
                agent=ctx.get("agent"), limit=ctx.get("limit", 100))}
        if task == "audit.verify":
            return {"status": "complete", "integrity": self.audit_log.verify_integrity()}
        if task == "security.scan":
            return {"status": "complete", "scan": self.security.scan_source(
                ctx.get("source", ""), ctx.get("filename", ""))}
        if task == "security.scan_directory":
            return {"status": "complete", "scan": self.security.scan_directory(ctx.get("path", ""))}
        if task == "compliance.check_repository":
            report = self.compliance.check_repository(ctx.get("target", ""),
                                                     ctx.get("manifest", {}), ctx.get("present_dirs"))
            self.audit_log.append(report["target"], "aegis", "compliance.check", "info",
                                 detail=f"score={report['score']}")
            return {"status": "complete", "report": report}
        if task == "compliance.check_agent":
            return {"status": "complete", "report": self.compliance.check_agent(
                ctx.get("target", ""), ctx.get("config", {}))}
        if task in ("governance.review", "governance.approve"):
            decision = self.reviewer.review(ctx.get("request_type", ""), sender,
                                           ctx.get("target", ""), ctx.get("evidence", {}))
            self.audit_log.append(decision["target"], sender, f"governance.{decision['request_type']}",
                                 "warning" if not decision["approved"] else "info", detail=decision["reasoning"])
            return {"status": "complete", "decision": decision}
        if task == "policy.list":
            return {"status": "complete", "policies": [p.to_dict() for p in self.policies.all(enabled_only=False)]}
        if task == "policy.amend":
            return {"status": "complete", **self.policies.amend(
                ctx.get("policy_id", ""), ctx.get("changes", {}), ctx.get("human_authorized", False))}
        if task == "risk.register":
            return {"status": "complete", "register": self.risk.register()}
        if task == "risk.exposure":
            return {"status": "complete", **self.risk.snapshot_trend(ctx.get("entity", sender))}
        if task == "anomaly.observe":
            record = self.anomaly.observe(ctx.get("agent", sender),
                                        ctx.get("latency_ms", 0.0), ctx.get("success", True))
            if record:
                self.risk.add_risk(ctx.get("agent", sender), 0.3, "ANOMALY")
                self.audit_log.append(ctx.get("agent", sender), "aegis", "anomaly.detected",
                                     "warning", detail=str(record["anomalies"]))
            return {"status": "complete", "anomaly": record}
        if task == "anomaly.recent":
            return {"status": "complete", "anomalies": self.anomaly.recent_anomalies(ctx.get("agent"))}
        if task == "monitor.start":
            self.set_sweep_targets(ctx.get("repos", []))
            self.monitor.start()
            return {"status": "complete", "monitor": self.monitor.stats()}
        if task == "monitor.alerts":
            return {"status": "complete", "alerts": self.monitor.recent_alerts()}
        if task == "remediation.trigger":
            if self.remediation is None:
                return {"status": "error", "message": "remediation unavailable"}
            return {"status": "complete", "case": self.remediation.remediate(
                repository=ctx.get("repository", ""), agent=ctx.get("agent", ""),
                failure_signature=ctx.get("signature", "manual"), failure_count=ctx.get("count", 5),
                error=ctx.get("error", ""), target_file=ctx.get("target_file"),
                context={"domain": ctx.get("domain")})}
        if task == "remediation.approve":
            if self.remediation is None:
                return {"status": "error", "message": "unavailable"}
            self.remediation.approve_fix(ctx.get("fix_id", ""), "aegis")
            return {"status": "complete", **self.remediation.approve_fix(
                ctx.get("fix_id", ""), ctx.get("approver", "aegis"))}
        if task == "remediation.apply":
            if self.remediation is None:
                return {"status": "error", "message": "unavailable"}
            return {"status": "complete", **self.remediation.apply_fix(
                ctx.get("fix_id", ""), human_confirm=ctx.get("human_confirm", False))}
        if task == "ecosystem.health":
            return {"status": "complete", "health": self._health()}
        handler = self.task_handlers.get(task)
        if handler:
            return handler(ctx, sender)
        return {"status": "error", "message": f"Unknown task: {task}"}

    def _health(self) -> Dict[str, Any]:
        stats = self.audit_log.stats()
        integ = self.audit_log.verify_integrity()
        violations = stats["by_severity"].get("violation", 0) + stats["by_severity"].get("critical", 0)
        return {"audit_entries": stats["total"], "violations": violations,
               "chain_intact": integ.get("intact"), "quarantined_agents": list(self._quarantined),
               "risk_register": self.risk.register(), "anomaly": self.anomaly.stats(),
               "monitor": self.monitor.stats(), "learned_thresholds": self._thresholds,
               "policies": self.policies.stats(),
               "remediation": self.remediation.stats() if self.remediation else None}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["health"] = self._health()
        return base
