"""
shared.remediation
==================
The self-healing engine. When a repository or agent fails chronically, the
ecosystem does not just log it: it tries to FIX it. (Book I Part IV Article II
Birth of an Agent; Article XIII Evolution; Article XIV Retirement; Book II
Ch IV Research Before Assumption; Book VI Part I Human Sovereignty.)

The remediation ladder (each rung tried in order, escalating):

  0. SELF-ADJUST     the agent's own reasoning already retries different
                     strategies (shared.reasoning). Remediation begins only
                     when that is exhausted (a "failure to learn").
  1. RECALL + RESEARCH  ask Chronicle for past fixes and Atlas to research the
                     root cause and known remedies. Evidence first.
  2. PROPOSE CODE FIX   use the LLM to synthesize a candidate patch for the
                     failing code, AST-validate it, and (optionally) verify it
                     in a sandbox. The fix is PROPOSED, never silently applied.
  3. NEW AGENT       if the capability itself is missing or the code is beyond
                     repair, ask Genesis to design a replacement/complement agent.
  4. ESCALATE HUMAN  if none of the above resolves it, hand it to human authority.

CONSTITUTIONAL SAFETY (non-negotiable, Book VI Part I / Aegis):
  Generated code is NEVER auto-written into a running module. It is validated,
  staged as a proposed patch, and requires explicit approval (Aegis + human)
  before application. Autonomy has limits; humans hold final authority.
"""
from __future__ import annotations

import ast
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProposedFix:
    """A candidate code fix, validated but NOT yet applied."""

    def __init__(self, target_file: str, original: str, patched: str,
                 rationale: str, source: str):
        self.fix_id = f"fix-{uuid.uuid4().hex[:10]}"
        self.target_file = target_file
        self.original = original
        self.patched = patched
        self.rationale = rationale
        self.source = source          # "llm" | "heuristic"
        self.valid_python = False
        self.validation_detail = ""
        self.approved_by: List[str] = []
        self.applied = False
        self.created_at = time.time()

    def to_dict(self, include_code: bool = False) -> Dict[str, Any]:
        d = {"fix_id": self.fix_id, "target_file": self.target_file,
             "rationale": self.rationale, "source": self.source,
             "valid_python": self.valid_python, "validation_detail": self.validation_detail,
             "approved_by": self.approved_by, "applied": self.applied}
        if include_code:
            d["patched"] = self.patched
        return d


class RemediationEngine:
    """Self-healing: research, propose fixes, build agents, escalate to humans."""

    def __init__(self, chronicle=None, atlas=None, genesis=None, aegis=None,
                 llm=None, staging_dir: str = "remediation_staging"):
        self.chronicle = chronicle
        self.atlas = atlas
        self.genesis = genesis
        self.aegis = aegis
        self.llm = llm
        self.staging = Path(staging_dir)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._fixes: Dict[str, ProposedFix] = {}
        self._cases: List[Dict[str, Any]] = []

    # ============================================================
    # Entry: a chronic failure needs remediation
    # ============================================================

    def remediate(self, repository: str, agent: str, failure_signature: str,
                 failure_count: int, error: str = "",
                 target_file: Optional[str] = None,
                 context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Run the remediation ladder for a chronically failing agent/repo.
        Returns a case report describing what was tried and the recommended action.
        """
        context = context or {}
        case = {"case_id": f"case-{uuid.uuid4().hex[:10]}", "repository": repository,
               "agent": agent, "failure_signature": failure_signature,
               "failure_count": failure_count, "error": error,
               "rungs": [], "resolution": None, "started_at": time.time()}

        # Rung 1: RECALL past fixes + RESEARCH root cause.
        research = self._research_rung(repository, agent, failure_signature, error, context)
        case["rungs"].append({"rung": "research", **research})

        # Rung 2: PROPOSE a code fix (validated, staged, NOT applied).
        if target_file:
            fix = self._propose_fix_rung(target_file, error, research, context)
            case["rungs"].append({"rung": "code_fix", **fix})
            if fix.get("fix_valid"):
                case["resolution"] = "proposed_fix_awaiting_approval"
                case["fix_id"] = fix.get("fix_id")
                self._record_case(case)
                return case

        # Rung 3: recommend a NEW agent via Genesis (capability gap / beyond repair).
        newagent = self._new_agent_rung(repository, agent, failure_signature, research, context)
        case["rungs"].append({"rung": "new_agent", **newagent})
        if newagent.get("recommended"):
            case["resolution"] = "new_agent_recommended"
            self._record_case(case)
            return case

        # Rung 4: ESCALATE to human authority (Book VI).
        esc = self._escalate_rung(repository, agent, failure_signature, error)
        case["rungs"].append({"rung": "escalate_human", **esc})
        case["resolution"] = "escalated_to_human"
        self._record_case(case)
        return case

    # ============================================================
    # Rung implementations
    # ============================================================

    def _research_rung(self, repository, agent, signature, error, context) -> Dict[str, Any]:
        prior_fixes = []
        if self.chronicle is not None:
            try:
                prior_fixes = self.chronicle.search(
                    query=f"fix for {signature} {error}", domain="remediation",
                    limit=3, requester="remediation")
            except Exception:
                prior_fixes = []
        research_report = None
        if self.atlas is not None:
            try:
                out = self.atlas.handle({"task": "research.investigate",
                    "context": {"query": f"how to fix: {error or signature} in a {repository} "
                                        f"({agent}) Python component",
                               "domain": "engineering"}, "sender": "remediation"})
                report = out.get("report", {})
                research_report = {"confidence": report.get("confidence"),
                                  "summary": report.get("summary", "")[:300],
                                  "key_terms": report.get("key_terms", [])}
            except Exception:
                research_report = {"error": "atlas unavailable"}
        return {"prior_fixes_found": len(prior_fixes), "research": research_report,
               "evidence_available": bool(prior_fixes or (research_report and
                                          research_report.get("confidence", 0) or 0) >= 0.4)}

    def _propose_fix_rung(self, target_file: str, error: str,
                         research: Dict, context: Dict) -> Dict[str, Any]:
        """
        Use the LLM to synthesize a candidate fix, AST-validate it, and STAGE it.
        Never applied here: application requires approval (Book VI).
        """
        path = Path(target_file)
        if not path.exists():
            return {"fix_valid": False, "message": f"target file not found: {target_file}"}
        try:
            original = path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"fix_valid": False, "message": f"cannot read file: {exc}"}

        if self.llm is None or not getattr(self.llm, "has_any", False):
            return {"fix_valid": False,
                   "message": "no LLM brain available to synthesize a code fix (honest)",
                   "recommendation": "escalate or install a provider key"}

        research_hint = ""
        if research.get("research") and isinstance(research["research"], dict):
            research_hint = research["research"].get("summary", "")

        prompt = (
            f"A Python file is failing. Propose a MINIMAL, correct fix.\n\n"
            f"File: {target_file}\nError/failure: {error}\n"
            f"Research context: {research_hint}\n\n"
            f"Current file content:\n```python\n{original[:6000]}\n```\n\n"
            f"Return ONLY the complete corrected file content. No prose, no fences.")
        try:
            from shared.llm import system_prompt
            result = self.llm.complete(system_prompt("genesis"), prompt,
                                      temperature=0.1, max_tokens=4000)
        except Exception as exc:
            return {"fix_valid": False, "message": f"llm error: {exc}"}
        if not result.ok:
            return {"fix_valid": False, "message": f"llm failed: {result.reason}"}

        patched = self._clean_code(result.text)
        fix = ProposedFix(target_file, original, patched,
                         rationale=f"LLM-proposed fix for: {error}", source="llm")

        # AST validation: never stage code that will not parse.
        try:
            ast.parse(patched)
            fix.valid_python = True
            fix.validation_detail = "AST parse OK"
        except SyntaxError as exc:
            fix.valid_python = False
            fix.validation_detail = f"syntax error: {exc}"

        self._fixes[fix.fix_id] = fix
        # stage to disk (a .proposed file next to nothing live)
        staged = self.staging / f"{fix.fix_id}_{path.name}.proposed"
        try:
            staged.write_text(patched, encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent (staging is best-effort)

        return {"fix_valid": fix.valid_python, "fix_id": fix.fix_id,
               "validation": fix.validation_detail, "staged_at": str(staged),
               "applied": False, "note": "proposed fix requires approval before it is applied"}

    def _new_agent_rung(self, repository, agent, signature, research, context) -> Dict[str, Any]:
        """Ask Genesis whether a new/replacement agent should be created."""
        if self.genesis is None:
            return {"recommended": False, "message": "Genesis unavailable"}
        domain = context.get("domain", repository.lower())
        try:
            analysis = self.genesis.handle({"task": "capability.analyze",
                "context": {"domain": domain,
                           "query": f"replacement for failing {agent}: {signature}"},
                "sender": "remediation"})
            gap = analysis.get("analysis", {})
            recommend = gap.get("recommendation") == "create_new_agent"
            return {"recommended": recommend, "analysis": gap,
                   "message": "Genesis recommends a new agent" if recommend
                             else "Genesis says existing coverage suffices"}
        except Exception as exc:
            return {"recommended": False, "message": f"Genesis error: {exc}"}

    def _escalate_rung(self, repository, agent, signature, error) -> Dict[str, Any]:
        detail = (f"Remediation exhausted for {repository}/{agent} "
                 f"(signature: {signature}). Human authority required (Book VI).")
        if self.aegis is not None:
            try:
                self.aegis.handle({"task": "audit.action",
                    "context": {"repository": repository, "agent": agent,
                               "action": "remediation_escalation",
                               "action_context": {"signature": signature, "error": error}},
                    "sender": "remediation"})
            except Exception:
                pass  # aegis:allow-silent
        if self.chronicle is not None:
            try:
                self.chronicle.store(content=detail, memory_type="constitutional",
                                    domain="remediation", tags=["escalation", repository],
                                    source="remediation")
            except Exception:
                pass  # aegis:allow-silent
        return {"escalated": True, "detail": detail}

    # ============================================================
    # Approval + application (the ONLY path to changing live code)
    # ============================================================

    def approve_fix(self, fix_id: str, approver: str) -> Dict[str, Any]:
        """
        Record an approval. A fix needs BOTH Aegis and a human approver before
        it may be applied (Book VI: no unbounded autonomy).
        """
        fix = self._fixes.get(fix_id)
        if not fix:
            return {"status": "error", "message": "unknown fix"}
        if approver not in fix.approved_by:
            fix.approved_by.append(approver)
        ready = "aegis" in fix.approved_by and any(a.startswith("human") for a in fix.approved_by)
        return {"status": "complete", "fix_id": fix_id, "approved_by": fix.approved_by,
               "ready_to_apply": ready and fix.valid_python}

    def apply_fix(self, fix_id: str, human_confirm: bool = False) -> Dict[str, Any]:
        """
        Apply a proposed fix to the live file ONLY if it is valid, approved by
        Aegis, and explicitly confirmed by a human. Backs up the original first.
        """
        fix = self._fixes.get(fix_id)
        if not fix:
            return {"status": "error", "message": "unknown fix"}
        if not fix.valid_python:
            return {"status": "error", "message": "fix failed validation; refusing to apply"}
        if "aegis" not in fix.approved_by:
            return {"status": "error", "message": "Aegis approval required before applying"}
        if not human_confirm:
            return {"status": "error",
                   "message": "human confirmation required (Book VI human sovereignty)"}
        path = Path(fix.target_file)
        try:
            backup = path.with_suffix(path.suffix + f".backup-{fix.fix_id}")
            backup.write_text(fix.original, encoding="utf-8")  # nothing dies without record
            path.write_text(fix.patched, encoding="utf-8")
            fix.applied = True
            if self.chronicle is not None:
                self.chronicle.store(
                    content=f"Applied approved fix {fix.fix_id} to {fix.target_file}. "
                           f"Backup at {backup.name}.",
                    memory_type="evolutionary", domain="remediation",
                    tags=["fix_applied", fix.target_file], source="remediation")
            return {"status": "complete", "applied": True, "backup": str(backup)}
        except Exception as exc:
            return {"status": "error", "message": f"apply failed: {exc}"}

    def _clean_code(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            for p in parts:
                if "def " in p or "import " in p or "class " in p:
                    text = p
                    break
            text = text.replace("python", "", 1)
        return text.strip("`\n ")

    def _record_case(self, case: Dict[str, Any]) -> None:
        case["completed_at"] = time.time()
        self._cases.append(case)
        if self.chronicle is not None:
            try:
                self.chronicle.store(
                    content=f"Remediation {case['case_id']} for {case['repository']}/"
                           f"{case['agent']}: {case['resolution']}",
                    memory_type="evolutionary", domain="remediation",
                    tags=["remediation", case["repository"], case["resolution"] or "unresolved"],
                    source="remediation")
            except Exception:
                pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        resolutions: Dict[str, int] = {}
        for c in self._cases:
            r = c.get("resolution") or "unresolved"
            resolutions[r] = resolutions.get(r, 0) + 1
        return {"cases": len(self._cases), "proposed_fixes": len(self._fixes),
               "applied_fixes": sum(1 for f in self._fixes.values() if f.applied),
               "by_resolution": resolutions}
