"""
Aegis.core.policy
================
Policy-as-data: the constitution's rules expressed as versioned, updatable
DATA rather than buried in code. (Book III Part II URS; Book IV Part II; Book VI
Human Constitution; SOX-style control frameworks.)

An institutional control function never hardcodes its rulebook. Policies are
declarative records with an id, severity weight, a checkable predicate, a Book
reference, and a version. They can be amended (by human authority) without
touching enforcement code, and every amendment is recorded.

Each policy carries:
  * likelihood_weight / impact_weight  -> feeds risk scoring (likelihood x impact)
  * predicate  -> a named check the enforcement layer runs
  * remediable -> whether a violation is self-healable or must escalate
"""
from __future__ import annotations

import json
import re
import time
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Policy:
    policy_id: str
    title: str
    predicate: str                 # name of the check (resolved by enforcement)
    reference: str                 # Book citation
    severity: str = "medium"       # low | medium | high | critical
    likelihood_weight: float = 0.5  # 0..1 prior probability this is violated
    impact_weight: float = 0.5      # 0..1 damage if violated
    remediable: bool = True
    enabled: bool = True
    version: int = 1
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def base_risk(self) -> float:
        return round(self.likelihood_weight * self.impact_weight, 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["base_risk"] = self.base_risk
        return d


# The constitutional policy set as DATA. Amendable, versioned.
DEFAULT_POLICIES: List[Dict[str, Any]] = [
    {"policy_id": "ENG-001", "title": "No silent failures", "predicate": "no_silent_failures",
     "reference": "Book II Ch VII", "severity": "high", "likelihood_weight": 0.4,
     "impact_weight": 0.8, "remediable": True},
    {"policy_id": "ENG-002", "title": "No hardcoded secrets", "predicate": "no_hardcoded_secrets",
     "reference": "Book II Ch VII", "severity": "critical", "likelihood_weight": 0.3,
     "impact_weight": 1.0, "remediable": False},
    {"policy_id": "ENG-003", "title": "No unlogged agent creation", "predicate": "logged_creation",
     "reference": "Book II Ch VII", "severity": "high", "likelihood_weight": 0.3,
     "impact_weight": 0.7, "remediable": True},
    {"policy_id": "SEC-001", "title": "Security by design not bypassed", "predicate": "no_security_bypass",
     "reference": "Book III Principle VI", "severity": "critical", "likelihood_weight": 0.2,
     "impact_weight": 1.0, "remediable": False},
    {"policy_id": "GOV-001", "title": "Constitution changes need human authority",
     "predicate": "human_authorized_constitution_change", "reference": "Book VI Part I",
     "severity": "critical", "likelihood_weight": 0.1, "impact_weight": 1.0, "remediable": False},
    {"policy_id": "URS-001", "title": "Repository declares all mandatory directories",
     "predicate": "urs_directories_present", "reference": "Book III Part II Ch II",
     "severity": "medium", "likelihood_weight": 0.5, "impact_weight": 0.4, "remediable": True},
    {"policy_id": "URS-002", "title": "Manifest declares required fields",
     "predicate": "manifest_fields_present", "reference": "Book IV Part II Ch I",
     "severity": "medium", "likelihood_weight": 0.5, "impact_weight": 0.4, "remediable": True},
    {"policy_id": "AGT-001", "title": "Agent declares mission and capabilities",
     "predicate": "agent_attrs_present", "reference": "Book I Part IV Article III-VI",
     "severity": "medium", "likelihood_weight": 0.4, "impact_weight": 0.5, "remediable": True},
]


class PolicyStore:
    """Versioned, amendable policy set persisted to disk."""

    def __init__(self, storage_dir: str = "security"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "policies.json"
        self._amendments_path = self.storage_dir / "policy_amendments.jsonl"
        self._lock = threading.RLock()
        self._policies: Dict[str, Policy] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                for item in json.loads(self._path.read_text(encoding="utf-8")):
                    p = Policy(**{k: v for k, v in item.items() if k != "base_risk"})
                    self._policies[p.policy_id] = p
                return
            except Exception:
                pass
        for item in DEFAULT_POLICIES:
            p = Policy(**item)
            self._policies[p.policy_id] = p
        self._persist()

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps([p.to_dict() for p in self._policies.values()],
                                             indent=2), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    def all(self, enabled_only: bool = True) -> List[Policy]:
        with self._lock:
            return [p for p in self._policies.values() if p.enabled or not enabled_only]

    def get(self, policy_id: str) -> Optional[Policy]:
        return self._policies.get(policy_id)

    def amend(self, policy_id: str, changes: Dict[str, Any], human_authorized: bool) -> Dict[str, Any]:
        """Amend a policy. Requires human authority (Book VI). Amendment is logged."""
        if not human_authorized:
            return {"status": "error", "message": "policy amendment requires human authority (Book VI)"}
        with self._lock:
            p = self._policies.get(policy_id)
            if not p:
                return {"status": "error", "message": "unknown policy"}
            for k, v in changes.items():
                if hasattr(p, k) and k not in ("policy_id",):
                    setattr(p, k, v)
            p.version += 1
            self._persist()
            with self._amendments_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"policy_id": policy_id, "changes": changes,
                                   "version": p.version, "ts": time.time()}) + "\n")
            return {"status": "complete", "policy": p.to_dict()}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_sev: Dict[str, int] = {}
            for p in self._policies.values():
                by_sev[p.severity] = by_sev.get(p.severity, 0) + 1
            return {"total_policies": len(self._policies), "by_severity": by_sev,
                   "enabled": sum(1 for p in self._policies.values() if p.enabled)}
