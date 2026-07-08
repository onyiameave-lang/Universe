"""
Aegis.intelligence.governance
=============================
Governance intelligence: compliance checking, security scanning, and
governance review. (Book III Part II Ch XIV Certification; Book IV Part II
Ch XI Testing; Book VI Human Constitution.)

Deterministic, explainable verdicts over real inputs. Every score is the
ratio of concrete checks passed, with the Book reference for each rule.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.constitution import (  # type: ignore
    RuleResult, check_manifest_fields, check_urs_directories, check_agent_attrs,
    scan_source_for_prohibitions, VALID_SECURITY_LEVELS,
)


class ComplianceChecker:
    def check_repository(self, name: str, manifest: Dict[str, Any],
                        present_dirs: Optional[List[str]] = None) -> Dict[str, Any]:
        results: List[RuleResult] = list(check_manifest_fields(manifest))
        if present_dirs is not None:
            results.extend(check_urs_directories(present_dirs))
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        score = passed / total if total else 0.0
        violations = [r.to_dict() for r in results if not r.passed]
        status = ("compliant" if score >= 0.9 and not violations
                  else "warning" if score >= 0.7 else "non_compliant")
        return {"target": name, "target_type": "repository", "score": round(score, 3),
               "status": status, "checks_passed": passed, "checks_total": total,
               "violations": violations,
               "recommendations": [f"Resolve {v['rule']}: {v['detail']}" for v in violations[:10]]}

    def check_agent(self, name: str, agent_config: Dict[str, Any]) -> Dict[str, Any]:
        results = check_agent_attrs(agent_config)
        sec = agent_config.get("security_level", "")
        results.append(RuleResult("agent.security_level.valid", sec in VALID_SECURITY_LEVELS,
                                 f"'{sec}'", "Book III Part II Ch I"))
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        score = passed / total if total else 0.0
        violations = [r.to_dict() for r in results if not r.passed]
        status = ("compliant" if score >= 0.9 else "warning" if score >= 0.7 else "non_compliant")
        return {"target": name, "target_type": "agent", "score": round(score, 3),
               "status": status, "checks_passed": passed, "checks_total": total,
               "violations": violations}


class SecurityScanner:
    def scan_source(self, source: str, filename: str = "") -> Dict[str, Any]:
        results = scan_source_for_prohibitions(source, filename)
        ast_findings = self._ast_scan(source)
        passed = sum(1 for r in results if r.passed)
        findings = [r.to_dict() for r in results if not r.passed] + ast_findings
        risk = ("high" if any("secret" in f.get("detail", "").lower() for f in findings)
                else "medium" if findings else "low")
        return {"filename": filename, "checks_passed": passed, "checks_total": len(results),
               "findings": findings, "risk_level": risk, "passed": not findings}

    def scan_directory(self, path: str) -> Dict[str, Any]:
        root = Path(path)
        if not root.exists():
            return {"error": f"path not found: {path}"}
        file_reports = []
        total_findings = 0
        scanned = 0
        for py in root.rglob("*.py"):
            scanned += 1
            try:
                src = py.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            report = self.scan_source(src, str(py.relative_to(root)))
            if report["findings"]:
                file_reports.append(report)
                total_findings += len(report["findings"])
        return {"path": path, "files_scanned": scanned,
               "files_with_findings": len(file_reports), "total_findings": total_findings,
               "reports": file_reports[:50],
               "risk_level": "high" if total_findings > 5 else "medium" if total_findings else "low"}

    def _ast_scan(self, source: str) -> List[Dict[str, Any]]:
        findings = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return findings
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                only_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                if only_pass and node.type is None:
                    findings.append({"rule": "engineering.no_silent_failures", "passed": False,
                                   "detail": f"bare except: pass at line {node.lineno}",
                                   "reference": "Book II Ch VII"})
        return findings


class GovernanceReviewer:
    def review(self, request_type: str, requestor: str, target: str,
              evidence: Dict[str, Any]) -> Dict[str, Any]:
        criteria: Dict[str, bool] = {
            "has_documentation": bool(evidence.get("documentation")),
            "has_tests": bool(evidence.get("tests_passed")),
            "compliant": float(evidence.get("compliance_score", 0)) >= 0.7,
            "reasoning_provided": bool(evidence.get("reasoning")),
        }
        if request_type == "deploy":
            criteria["security_passed"] = bool(evidence.get("security_passed"))
            criteria["benchmark_passed"] = bool(evidence.get("benchmark_passed"))
            criteria["certified"] = float(evidence.get("compliance_score", 0)) >= 0.9
        if request_type == "create":
            criteria["aegis_validated"] = bool(evidence.get("aegis_validated"))
        if request_type == "retire":
            criteria["knowledge_preserved"] = bool(evidence.get("knowledge_preserved"))
            criteria["dependents_migrated"] = bool(evidence.get("dependents_migrated"))
        failed = [k for k, v in criteria.items() if not v]
        if not failed:
            approved, reason = True, f"All {len(criteria)} governance criteria satisfied."
        elif request_type != "deploy" and len(failed) <= 1:
            approved, reason = True, f"Conditionally approved; resolve: {failed[0]}"
        else:
            approved, reason = False, f"Rejected. Unmet: {', '.join(failed)}"
        return {"request_type": request_type, "requestor": requestor, "target": target,
               "criteria": criteria, "failed_criteria": failed,
               "approved": approved, "reasoning": reason}
