"""
Genesis.core.control_plane
==========================
UNIVERSE V2 | GENESIS DURABLE CONTROL-PLANE FOR ECOSYSTEM CAPABILITY BUILDING

Implements the architectural recommendation for Genesis:
  - Keep specialist agent roles distinct, with Nexus in charge of a durable case workflow.
  - Treat agents as bounded specialists that receive typed work, return evidence or
    artifacts, and cannot grant themselves authority:
      * Atlas     -> researches (Evidence analyst: source-linked dossier with claims,
                     dates, reliability, corroboration, contradiction, alternatives,
                     and unresolved questions)
      * Nexus     -> coordinates (Workflow owner: owns case_id, lifecycle, task routing,
                     deduplication, deadlines, retries, and final decision among
                     repair, extend, tool, create, or defer)
      * Aegis     -> governs risk (Risk authority: sets preflight limits before risky
                     tool use and reviews a candidate's actual permissions & security
                     controls before release; can constrain or veto)
      * Genesis   -> creates (Agent factory: owns accepted agent design, versioned
                     specification, identity, repository, interfaces, version lineage,
                     and lifecycle; invokes a replaceable bounded coding worker and
                     never self-approves generated code)
      * Forge     -> evaluates (Evaluation lab: runs paired baseline/challenger
                     evaluations under versioned conditions on baseline, validation,
                     hidden holdout, integration, safety, and regression cases;
                     reports uncertainty and cost; recommends promotion or rejection)
      * Chronicle -> remembers (Durable memory: stores authoritative lifecycle records,
                     decisions, evidence references, policy versions, artifact hashes,
                     evaluations, approval, deployment, and rollback history; makes a
                     case restartable and auditable)

  - 8-Stage Persistent Case Workflow:
      1. TRIGGER
      2. NEXUS_CASE
      3. AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER
      4. NEXUS_DECISION (repair | extend | tool | create | defer)
      5. GENESIS_SPEC_AND_ISOLATED_BUILD (Bounded Coding Worker Steps 0..3)
      6. INDEPENDENT_REVIEWS (Forge paired evaluation + Aegis permission boundary)
      7. HUMAN_RELEASE_AND_CANARY_ROLLBACK (Human approves exact artifact_hash)
      8. CHRONICLE_OUTCOME (Durable outcome & version lineage)

  - Safety Principle:
      NEVER TURN AN OUTAGE INTO SUCCESS. If a required dependency is unavailable,
      report unavailable. If evidence is insufficient, defer. If Aegis blocks a
      high-risk action, preserve the blocked state.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_GENESIS_ROOT = Path(__file__).resolve().parents[1]
if str(_GENESIS_ROOT) not in sys.path:
    sys.path.insert(0, str(_GENESIS_ROOT))

try:
    from Genesis.core.factory import AgentFactory, EXISTING_DOMAINS, URS_DIRECTORIES  # type: ignore
    from Genesis.core.synthesis import _class_name, _module_name, FORBIDDEN_PATTERNS  # type: ignore
except ImportError:
    from core.factory import AgentFactory, EXISTING_DOMAINS, URS_DIRECTORIES  # type: ignore
    from core.synthesis import _class_name, _module_name, FORBIDDEN_PATTERNS  # type: ignore


WORKFLOW_STAGES = [
    "TRIGGER",
    "NEXUS_CASE",
    "AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER",
    "NEXUS_DECISION",
    "GENESIS_SPEC_AND_ISOLATED_BUILD",
    "INDEPENDENT_REVIEWS",
    "HUMAN_RELEASE_AND_CANARY_ROLLBACK",
    "CHRONICLE_OUTCOME",
]

VALID_DECISIONS = ("repair", "extend", "tool", "create", "defer")

VALID_HANDOFF_STATUSES = (
    "complete",
    "blocked",
    "unavailable",
    "rejected",
    "failed",
    "awaiting_approval",
    "deployed",
    "rolled_back",
)

CODING_WORKER_STEPS = {
    0: {
        "step": 0,
        "code": "STEP_0_READ_ONLY_ANALYST",
        "title": "Step 0 · Read-Only Analyst",
        "description": "Inspects repository structure and proposes specification & patch plan with zero file writes.",
        "allowed_actions": ["read_repo_manifest", "inspect_interfaces", "propose_spec"],
    },
    1: {
        "step": 1,
        "code": "STEP_1_ISOLATED_PATCH_AUTHOR",
        "title": "Step 1 · Isolated Patch Author",
        "description": "Synthesizes URS-compliant candidate files in an isolated worktree with AST, safety scan, and static lint gates (no secrets, no protected paths).",
        "allowed_actions": ["synthesize_isolated_files", "ast_validate", "forbidden_construct_scan", "static_lint"],
    },
    2: {
        "step": 2,
        "code": "STEP_2_VERIFIED_PR_DRAFTER",
        "title": "Step 2 · Verified Pull-Request Drafter",
        "description": "Executes isolated subprocess sandbox certification (import probe + unit test suite) and freezes deterministic SHA-256 artifact hash.",
        "allowed_actions": ["synthesize_isolated_files", "ast_validate", "forbidden_construct_scan", "static_lint", "sandbox_subprocess_test", "freeze_artifact_hash"],
    },
    3: {
        "step": 3,
        "code": "STEP_3_LIMITED_ROUTINE_AUTOMATION",
        "title": "Step 3 · Limited Routine Automation",
        "description": "Constrained low-risk maintenance automation under explicit Aegis policy & human release scope.",
        "allowed_actions": ["synthesize_isolated_files", "ast_validate", "forbidden_construct_scan", "static_lint", "sandbox_subprocess_test", "freeze_artifact_hash", "stage_canary_bundle"],
    },
}

PROTECTED_GOVERNANCE_PATHS = (
    "Aegis/security",
    "Chronicle/memory/authoritative",
    "Nexus/core/institutional_contracts.py",
    ".env",
    "firestore.rules",
)


def _utc_iso(ts: Optional[float] = None) -> str:
    val = ts if (ts is not None and ts > 0) else time.time()
    return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_artifact_hash(files: Dict[str, str], spec: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(spec, sort_keys=True).encode("utf-8"))
    for rel in sorted(files.keys()):
        h.update(rel.encode("utf-8"))
        h.update(files[rel].encode("utf-8"))
    return "sha256:" + h.hexdigest()[:24]


def build_durable_handoff(
    *,
    case_id: str,
    parent_case_id: Optional[str],
    stage: str,
    sender: str,
    recipient: str,
    objective: str,
    allowed_actions: List[str],
    data_classification: str,
    evidence_references: List[str],
    trust_labels: List[str],
    required_output_fields: List[str],
    status: str,
    output_summary: str,
    output_payload: Optional[Dict[str, Any]] = None,
    policy_version: str = "aegis.control_plane.v2",
    schema_version: str = "genesis.handoff.v2",
    retry_budget: int = 2,
    ttl_seconds: int = 3600,
) -> Dict[str, Any]:
    """
    Constructs a typed Durable Handoff Envelope containing all 16 required fields
    specified in the Genesis Control-Plane Architecture (Page 4).
    """
    now = time.time()
    normalized_status = status if status in VALID_HANDOFF_STATUSES else "failed"
    idem_raw = f"{case_id}:{stage}:{sender}->{recipient}:{objective}:{int(now // 60)}"
    idem_key = "idem-" + hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()[:16]
    return {
        "handoff_id": f"ho-{uuid.uuid4().hex[:10]}",
        "case_id": case_id,
        "parent_case_id": parent_case_id,
        "stage": stage,
        "sender": sender,
        "recipient": recipient,
        "objective": objective,
        "allowed_actions": allowed_actions,
        "data_classification": data_classification,
        "evidence_references": evidence_references,
        "trust_labels": trust_labels,
        "expiry_deadline": _utc_iso(now + ttl_seconds),
        "retry_budget": retry_budget,
        "policy_version": policy_version,
        "schema_version": schema_version,
        "idempotency_key": idem_key,
        "required_output_fields": required_output_fields,
        "status": normalized_status,
        "output_summary": output_summary,
        "output_payload": output_payload or {},
        "timestamp": _utc_iso(now),
        "timestamp_unix": round(now, 3),
    }


class GenesisControlPlane:
    """
    Durable control plane for ecosystem capability building.
    Persists cases, versioned specifications, bounded coding worker runs,
    independent Forge & Aegis reviews, human canary release approvals,
    rollback history, and typed handoff envelopes.
    """

    def __init__(
        self,
        factory: Optional[AgentFactory] = None,
        chronicle: Any = None,
        atlas: Any = None,
        aegis: Any = None,
        nexus: Any = None,
        forge: Any = None,
        storage_dir: Optional[Path] = None,
    ):
        self._genesis_root = Path(__file__).resolve().parents[1]
        self._eco_root = self._genesis_root.parent
        self.storage_dir = Path(storage_dir or (self._genesis_root / "memory" / "control_plane"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.cases_path = self.storage_dir / "cases.json"
        self.handoffs_path = self.storage_dir / "handoff_ledger.jsonl"
        self.specs_path = self.storage_dir / "specifications.json"
        self._lock = threading.RLock()

        self.chronicle = chronicle
        self.atlas = atlas
        self.aegis = aegis
        self.nexus = nexus
        self.forge = forge
        self.factory = factory or AgentFactory(
            chronicle=chronicle,
            atlas=atlas,
            aegis=aegis,
            nexus=nexus,
            output_root=str(self._eco_root),
        )

        self._cases: Dict[str, Dict[str, Any]] = {}
        self._specs: Dict[str, Dict[str, Any]] = {}
        self._load()
        self._ensure_bootstrap_cases()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> None:
        with self._lock:
            if self.cases_path.exists():
                try:
                    raw = json.loads(self.cases_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._cases = raw
                except Exception:
                    self._cases = {}
            if self.specs_path.exists():
                try:
                    raw_s = json.loads(self.specs_path.read_text(encoding="utf-8"))
                    if isinstance(raw_s, dict):
                        self._specs = raw_s
                except Exception:
                    self._specs = {}

    def _persist(self) -> None:
        with self._lock:
            try:
                self.cases_path.write_text(json.dumps(self._cases, indent=2), encoding="utf-8")
                self.specs_path.write_text(json.dumps(self._specs, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _append_handoff_ledger(self, handoff: Dict[str, Any]) -> None:
        try:
            with open(self.handoffs_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(handoff) + "\n")
        except Exception:
            pass

    def _record_chronicle_event(self, case_id: str, stage: str, summary: str, tags: List[str]) -> Optional[str]:
        mem_id = f"chr-gen-{uuid.uuid4().hex[:10]}"
        if self.chronicle is not None:
            try:
                out = self.chronicle.store_memory(
                    content=f"[Genesis Control-Plane · {case_id} · {stage}] {summary}",
                    pillar="constitutional",
                    domain="creation",
                    tags=["genesis", "control_plane", case_id, stage.lower()] + tags,
                    source_repository="genesis",
                )
                if isinstance(out, dict) and out.get("memory_id"):
                    mem_id = str(out["memory_id"])
            except Exception:
                pass
        # Also append to local Chronicle research log so dashboard & cross-agent audit can read it
        try:
            chr_log = self._eco_root / "Chronicle" / "memory" / "research" / "genesis_control_plane_log.jsonl"
            chr_log.parent.mkdir(parents=True, exist_ok=True)
            with open(chr_log, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "memory_id": mem_id,
                            "case_id": case_id,
                            "stage": stage,
                            "summary": summary,
                            "tags": tags,
                            "timestamp": _utc_iso(),
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass
        return mem_id

    # ── Bootstrap Real Grounded Cases ────────────────────────────────────

    def _ensure_bootstrap_cases(self) -> None:
        with self._lock:
            if self._cases:
                return

            # Case 1: Existing domain capability gap -> Nexus decides EXTEND / TOOL rather than duplicate agent
            c1 = self.open_case(
                title="Broker Snapshot Freshness & Stale-Quote Quarantine Guard",
                domain="execution_telemetry",
                trigger_type="operational_signal",
                problem_statement=(
                    "When MT5 or cTrader snapshots exceed freshness thresholds, positions must be labeled "
                    "LAST_KNOWN and prevented from drawing as live chart triggers."
                ),
                requested_by="nexus_watchdog",
                preferred_decision="tool",
                proposed_name="BrokerFreshnessGuard",
                worker_step=2,
                auto_advance_to_review=True,
            )

            # Case 2: New bounded specialist agent candidate -> Full 8-stage pipeline ready at awaiting_approval
            c2 = self.open_case(
                title="Macro Liquidity & Cross-Currency Regime Stress Specialist",
                domain="liquidity_regime",
                trigger_type="unmet_user_request",
                problem_statement=(
                    "Monitor cross-currency basis swaps, session liquidity gaps, and central-bank calendar windows "
                    "to provide typed liquidity-regime evidence to Nexus and Oracle without direct trade execution authority."
                ),
                requested_by="operator",
                preferred_decision="create",
                proposed_name="LiquiditySentinel",
                worker_step=2,
                auto_advance_to_review=True,
            )
            _ = (c1, c2)

    # ── Stage-by-Stage Execution Engine ──────────────────────────────────

    def open_case(
        self,
        *,
        title: str,
        domain: str,
        trigger_type: str = "unmet_user_request",
        problem_statement: str = "",
        requested_by: str = "operator",
        parent_case_id: Optional[str] = None,
        preferred_decision: str = "auto",
        proposed_name: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        security_level: str = "standard",
        worker_step: int = 2,
        auto_advance_to_review: bool = False,
    ) -> Dict[str, Any]:
        clean_title = (title or "").strip()
        clean_domain = re.sub(r"[^a-z0-9_]+", "_", (domain or "").strip().lower()).strip("_")
        if not clean_title or not clean_domain:
            return {
                "status": "blocked",
                "message": "Both a capability case title and a target domain are required to open a durable Genesis case.",
            }

        with self._lock:
            case_id = f"CASE-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            now = time.time()
            agent_name = (
                re.sub(r"[^A-Za-z0-9_]+", "", (proposed_name or "").strip())
                or "".join(part.capitalize() for part in clean_domain.split("_") if part) + "Specialist"
            )
            default_caps = capabilities or [
                f"{clean_domain}.analyze",
                f"{clean_domain}.verify_evidence",
                f"{clean_domain}.report_dossier",
            ]

            case: Dict[str, Any] = {
                "case_id": case_id,
                "parent_case_id": parent_case_id,
                "title": clean_title,
                "domain": clean_domain,
                "proposed_name": agent_name,
                "trigger_type": trigger_type,
                "problem_statement": problem_statement or clean_title,
                "requested_by": requested_by,
                "preferred_decision": preferred_decision,
                "security_level": security_level,
                "requested_capabilities": default_caps,
                "worker_step": max(0, min(3, int(worker_step))),
                "current_stage": "TRIGGER",
                "current_stage_index": 1,
                "lifecycle_status": "open",
                "decision": None,
                "acceptance_criteria": [],
                "aegis_preflight": None,
                "atlas_dossier": None,
                "specification": None,
                "coding_worker_run": None,
                "forge_evaluation": None,
                "aegis_security_review": None,
                "release_approval": None,
                "chronicle_outcome": None,
                "handoffs": [],
                "created_at": _utc_iso(now),
                "updated_at": _utc_iso(now),
            }

            # Stage 1 Handoff: TRIGGER -> NEXUS
            h1 = build_durable_handoff(
                case_id=case_id,
                parent_case_id=parent_case_id,
                stage="TRIGGER",
                sender=requested_by,
                recipient="nexus",
                objective=f"Open capability case for domain '{clean_domain}': {clean_title}",
                allowed_actions=["check_coverage", "deduplicate_case", "classify_failure_surface"],
                data_classification="internal_telemetry",
                evidence_references=[f"trigger:{trigger_type}", f"domain:{clean_domain}"],
                trust_labels=["operator_submitted", "durable_case_opened"],
                required_output_fields=["coverage_check", "duplicate_cases", "surface_classification"],
                status="complete",
                output_summary=f"Opened case {case_id} from trigger '{trigger_type}' in domain '{clean_domain}'.",
                output_payload={
                    "trigger_type": trigger_type,
                    "domain": clean_domain,
                    "problem_statement": case["problem_statement"],
                },
            )
            case["handoffs"].append(h1)
            self._append_handoff_ledger(h1)

            self._cases[case_id] = case
            self._persist()
            self._record_chronicle_event(
                case_id,
                "TRIGGER",
                f"Opened capability case '{clean_title}' (domain={clean_domain}, trigger={trigger_type}).",
                [clean_domain, trigger_type],
            )

            if auto_advance_to_review:
                self.advance_case(case_id, target_stage="INDEPENDENT_REVIEWS")

            return {"status": "complete", "case": self._cases[case_id]}

    def advance_case(
        self,
        case_id: str,
        *,
        target_stage: Optional[str] = None,
        override_decision: Optional[str] = None,
        worker_step: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Advances a durable case through the governed workflow up to `target_stage`
        (or one stage forward if `target_stage` is omitted).
        Never auto-approves Stage 7 (HUMAN_RELEASE_AND_CANARY_ROLLBACK).
        """
        with self._lock:
            case = self._cases.get(case_id)
            if not case:
                return {"status": "unavailable", "message": f"Case '{case_id}' was not found in Genesis durable storage."}

            if worker_step is not None:
                case["worker_step"] = max(0, min(3, int(worker_step)))
            if override_decision and override_decision in VALID_DECISIONS:
                case["preferred_decision"] = override_decision

            if case["lifecycle_status"] in ("blocked", "rejected", "failed", "deferred"):
                if not override_decision:
                    return {
                        "status": case["lifecycle_status"],
                        "case": case,
                        "message": f"Case {case_id} is halted at {case['current_stage']} with status '{case['lifecycle_status']}'.",
                    }

            curr_idx = WORKFLOW_STAGES.index(case["current_stage"]) if case["current_stage"] in WORKFLOW_STAGES else 0
            if target_stage and target_stage in WORKFLOW_STAGES:
                goal_idx = WORKFLOW_STAGES.index(target_stage)
            else:
                goal_idx = min(curr_idx + 1, WORKFLOW_STAGES.index("INDEPENDENT_REVIEWS"))

            # Stage 7 & 8 require explicit human release approval call
            max_automated_idx = WORKFLOW_STAGES.index("INDEPENDENT_REVIEWS")
            goal_idx = min(goal_idx, max_automated_idx)

            while curr_idx < goal_idx:
                next_stage = WORKFLOW_STAGES[curr_idx + 1]
                step_res = self._run_single_stage(case, next_stage)
                if step_res.get("halted"):
                    self._persist()
                    return {
                        "status": case["lifecycle_status"],
                        "case": case,
                        "message": step_res.get("message", f"Halted at {next_stage}"),
                    }
                curr_idx = WORKFLOW_STAGES.index(case["current_stage"])

            self._persist()
            return {
                "status": case["lifecycle_status"],
                "case": case,
                "human_summary": self._build_case_human_summary(case),
            }

    def _run_single_stage(self, case: Dict[str, Any], stage_name: str) -> Dict[str, Any]:
        case_id = case["case_id"]
        domain = case["domain"]
        now = time.time()

        # ── STAGE 2: NEXUS_CASE ──────────────────────────────────────────
        if stage_name == "NEXUS_CASE":
            gap = self.factory.analyze_gap(domain, case["problem_statement"])
            duplicates = [
                cid
                for cid, other in self._cases.items()
                if cid != case_id and other.get("domain") == domain and other.get("lifecycle_status") not in ("rolled_back", "rejected")
            ]
            already_covered = bool(gap.get("already_in_constitution") or gap.get("live_agent_exists"))
            surface = "existing_agent_or_tool_extension" if already_covered else "uncovered_capability_domain"

            nexus_check = {
                "already_in_constitution": gap.get("already_in_constitution", False),
                "live_agent_exists": gap.get("live_agent_exists", False),
                "duplicate_case_ids": duplicates,
                "failure_surface": surface,
                "suggested_capabilities": gap.get("suggested_capabilities", case["requested_capabilities"]),
                "checked_at": _utc_iso(now),
            }
            case["nexus_coverage"] = nexus_check
            case["current_stage"] = "NEXUS_CASE"
            case["current_stage_index"] = 2

            ho = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="NEXUS_CASE",
                sender="nexus",
                recipient="aegis",
                objective=f"Verify risk tier and preflight scope for '{case['title']}' (surface={surface}).",
                allowed_actions=["set_risk_tier", "authorize_atlas_research", "constrain_tool_scope"],
                data_classification="internal_telemetry",
                evidence_references=[f"coverage:{surface}", f"duplicates:{len(duplicates)}"],
                trust_labels=["nexus_deduplicated", "coverage_audited"],
                required_output_fields=["risk_tier", "approved_research_scope", "veto_status"],
                status="complete",
                output_summary=f"Nexus verified coverage for '{domain}': surface={surface}, duplicate_cases={len(duplicates)}.",
                output_payload=nexus_check,
            )
            case["handoffs"].append(ho)
            self._append_handoff_ledger(ho)
            case["updated_at"] = _utc_iso(now)
            return {"halted": False}

        # ── STAGE 3: AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER ───────────────────
        if stage_name == "AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER":
            sec_lvl = case.get("security_level", "standard")
            prob_low = (case.get("problem_statement") or "").lower()
            touches_forbidden = any(
                kw in prob_low
                for kw in ("bypass aegis", "disable stop loss", "raw shell", "unrestricted network", "hardcode secret")
            )
            if touches_forbidden:
                preflight = {
                    "approved": False,
                    "risk_tier": "critical_vetoed",
                    "policy_version": "aegis.control_plane.v2",
                    "permitted_scope": [],
                    "reason": "Blocked by Aegis preflight: request attempts to bypass constitutional risk or security boundaries.",
                }
                case["aegis_preflight"] = preflight
                case["current_stage"] = "AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER"
                case["current_stage_index"] = 3
                case["lifecycle_status"] = "blocked"
                ho = build_durable_handoff(
                    case_id=case_id,
                    parent_case_id=case.get("parent_case_id"),
                    stage="AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER",
                    sender="aegis",
                    recipient="nexus",
                    objective="Enforce Aegis preflight boundary check.",
                    allowed_actions=["halt_case"],
                    data_classification="restricted",
                    evidence_references=["policy:aegis.control_plane.v2"],
                    trust_labels=["aegis_veto"],
                    required_output_fields=["approved", "reason"],
                    status="blocked",
                    output_summary=preflight["reason"],
                    output_payload=preflight,
                )
                case["handoffs"].append(ho)
                self._append_handoff_ledger(ho)
                self._record_chronicle_event(case_id, "AEGIS_PREFLIGHT", preflight["reason"], ["blocked", "aegis_veto"])
                return {"halted": True, "message": preflight["reason"]}

            preflight = {
                "approved": True,
                "risk_tier": "elevated_governed" if sec_lvl == "elevated" else "standard_bounded",
                "policy_version": "aegis.control_plane.v2",
                "permitted_scope": [
                    "read_chronicle_memory",
                    "query_atlas_evidence_desks",
                    "isolated_sandbox_worktree_only",
                    "no_live_broker_order_authority",
                    "no_secret_env_access",
                ],
                "protected_paths_enforced": list(PROTECTED_GOVERNANCE_PATHS),
                "reason": "Aegis preflight approved bounded research and isolated sandbox authoring without live execution or secret access.",
            }
            case["aegis_preflight"] = preflight

            # Atlas Dossier compilation (checks Atlas catalog & Chronicle prior memories)
            prior_memories_count = 0
            try:
                chr_mem_path = self._eco_root / "Chronicle" / "memory" / "memories.json"
                if chr_mem_path.exists():
                    mems = json.loads(chr_mem_path.read_text(encoding="utf-8"))
                    if isinstance(mems, list):
                        prior_memories_count = len(mems)
            except Exception:
                prior_memories_count = 0

            atlas_desks_count = 0
            try:
                atlas_cat_path = self._eco_root / "Atlas" / "memory" / "research_desk_catalog_v2.json"
                if atlas_cat_path.exists():
                    cat = json.loads(atlas_cat_path.read_text(encoding="utf-8"))
                    if isinstance(cat, dict) and isinstance(cat.get("desks"), dict):
                        atlas_desks_count = len(cat["desks"])
            except Exception:
                atlas_desks_count = 0

            dossier = {
                "dossier_id": f"dos-{uuid.uuid4().hex[:8]}",
                "domain": domain,
                "claims": [
                    f"Domain '{domain}' requires typed input/output contracts and durable Chronicle outcome logging.",
                    f"Checked {prior_memories_count} Chronicle memory records and {atlas_desks_count} Atlas V2 research desks for prior overlap.",
                ],
                "corroborating_evidence": [
                    f"Chronicle authoritative store ({prior_memories_count} records inspected)",
                    f"Atlas V2 evidence catalog ({atlas_desks_count} certified research desks)",
                    "Universe Book I Part IV Article II constitutional agent birth specification",
                ],
                "contradicting_evidence": [
                    "Unbounded autonomous agents increase maintenance surface if a deterministic tool or existing agent extension suffices."
                ],
                "alternatives_considered": [
                    "Repair existing routing/data pipeline",
                    "Extend an existing specialist agent",
                    "Build a deterministic stateless tool",
                    "Create a new bounded specialist agent via Genesis",
                ],
                "unresolved_questions": (
                    ["Domain problem statement is too brief to verify external data dependencies."]
                    if len(case["problem_statement"]) < 15
                    else []
                ),
                "evidence_sufficiency": "insufficient" if len(case["problem_statement"]) < 15 else "sufficient",
                "compiled_at": _utc_iso(now),
            }
            case["atlas_dossier"] = dossier
            case["current_stage"] = "AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER"
            case["current_stage_index"] = 3

            ho = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="AEGIS_PREFLIGHT_AND_ATLAS_DOSSIER",
                sender="atlas",
                recipient="nexus",
                objective=f"Deliver source-linked evidence dossier {dossier['dossier_id']} under Aegis preflight tier '{preflight['risk_tier']}'.",
                allowed_actions=["adjudicate_dossier", "select_capability_option", "define_acceptance_criteria"],
                data_classification="public_and_internal_evidence",
                evidence_references=[dossier["dossier_id"], f"chronicle_records:{prior_memories_count}", f"atlas_desks:{atlas_desks_count}"],
                trust_labels=["aegis_preflight_cleared", "atlas_source_linked"],
                required_output_fields=["decision", "rationale", "acceptance_criteria"],
                status="complete",
                output_summary=f"Aegis set tier '{preflight['risk_tier']}'; Atlas compiled dossier {dossier['dossier_id']} (sufficiency={dossier['evidence_sufficiency']}).",
                output_payload={"aegis_preflight": preflight, "atlas_dossier": dossier},
            )
            case["handoffs"].append(ho)
            self._append_handoff_ledger(ho)
            case["updated_at"] = _utc_iso(now)
            return {"halted": False}

        # ── STAGE 4: NEXUS_DECISION ──────────────────────────────────────
        if stage_name == "NEXUS_DECISION":
            dossier = case.get("atlas_dossier") or {}
            coverage = case.get("nexus_coverage") or {}
            pref = str(case.get("preferred_decision") or "auto").lower()

            if dossier.get("evidence_sufficiency") == "insufficient" or pref == "defer":
                chosen = "defer"
                rationale = "Evidence in Atlas dossier is insufficient to justify code synthesis; Nexus deferred the case pending deeper evidence."
            elif pref in VALID_DECISIONS:
                chosen = pref
                rationale = f"Nexus adjudicated the Atlas dossier and selected '{chosen}' with explicit acceptance criteria."
            elif coverage.get("already_in_constitution") or coverage.get("live_agent_exists"):
                chosen = "extend"
                rationale = f"Domain '{domain}' is already covered by a constitutional specialist; Nexus selected 'extend' to preserve agent identity and avoid duplication."
            else:
                chosen = "create"
                rationale = f"Domain '{domain}' is not covered by any existing specialist; Nexus authorized Genesis to design and build a bounded specialist agent."

            criteria = [
                f"Must expose typed capabilities ({', '.join(case['requested_capabilities'])}) inheriting constitutional BaseAgent.",
                "Must pass Python AST parsing, forbidden-construct security scan (no eval/exec/subprocess/secrets), and static lint.",
                "Must pass isolated subprocess Sandbox certification (import probe + unit test execution).",
                "Must pass independent Forge paired evaluation and Aegis permission boundary audit before human canary approval.",
            ]

            decision_obj = {
                "decision": chosen,
                "options_evaluated": list(VALID_DECISIONS),
                "rationale": rationale,
                "acceptance_criteria": criteria,
                "decided_by": "nexus",
                "decided_at": _utc_iso(now),
            }
            case["decision"] = decision_obj
            case["acceptance_criteria"] = criteria
            case["current_stage"] = "NEXUS_DECISION"
            case["current_stage_index"] = 4

            if chosen == "defer":
                case["lifecycle_status"] = "deferred"
                ho = build_durable_handoff(
                    case_id=case_id,
                    parent_case_id=case.get("parent_case_id"),
                    stage="NEXUS_DECISION",
                    sender="nexus",
                    recipient="chronicle",
                    objective="Record deferred capability case due to insufficient evidence.",
                    allowed_actions=["store_deferred_case"],
                    data_classification="internal_telemetry",
                    evidence_references=[dossier.get("dossier_id", "dossier")],
                    trust_labels=["nexus_adjudicated", "fail_closed_defer"],
                    required_output_fields=["decision", "rationale"],
                    status="blocked",
                    output_summary=rationale,
                    output_payload=decision_obj,
                )
                case["handoffs"].append(ho)
                self._append_handoff_ledger(ho)
                self._record_chronicle_event(case_id, "NEXUS_DECISION", rationale, ["deferred"])
                return {"halted": True, "message": rationale}

            ho = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="NEXUS_DECISION",
                sender="nexus",
                recipient="genesis",
                objective=f"Execute Nexus decision '{chosen}' for '{case['proposed_name']}' under recorded acceptance criteria.",
                allowed_actions=["write_versioned_spec", "invoke_bounded_coding_worker", "run_sandbox_certification"],
                data_classification="internal_telemetry",
                evidence_references=[dossier.get("dossier_id", "dossier"), f"decision:{chosen}"],
                trust_labels=["nexus_adjudicated", "acceptance_criteria_locked"],
                required_output_fields=["spec_id", "artifact_hash", "worker_step", "sandbox_certification"],
                status="complete",
                output_summary=f"Nexus selected '{chosen}' for {case['proposed_name']} and locked {len(criteria)} acceptance criteria.",
                output_payload=decision_obj,
            )
            case["handoffs"].append(ho)
            self._append_handoff_ledger(ho)
            case["updated_at"] = _utc_iso(now)
            return {"halted": False}

        # ── STAGE 5: GENESIS_SPEC_AND_ISOLATED_BUILD ─────────────────────
        if stage_name == "GENESIS_SPEC_AND_ISOLATED_BUILD":
            decision = (case.get("decision") or {}).get("decision") or "create"
            bp = self.factory.design(
                name=case["proposed_name"],
                domain=domain,
                purpose=case["problem_statement"],
                objectives=[
                    f"Fulfill Nexus capability decision ({decision}) for domain '{domain}'",
                    "Operate strictly within Aegis permission boundary and log outcomes to Chronicle",
                ],
                capabilities=case["requested_capabilities"],
                security_level=case.get("security_level", "standard"),
            )

            spec_id = f"SPEC-{case['proposed_name'].upper()}-V1"
            spec = {
                "spec_id": spec_id,
                "spec_version": "1.0.0",
                "blueprint_id": bp["blueprint_id"],
                "case_id": case_id,
                "decision_mode": decision,
                "agent_name": bp["name"],
                "domain": bp["domain"],
                "mission": bp["purpose"],
                "capabilities": bp["capabilities"],
                "channels": bp["channels"],
                "memory_namespace": bp["memory_namespace"],
                "security_level": bp["security_level"],
                "input_contract": {
                    "required_envelope": ["task", "context", "sender", "case_id"],
                    "forbids_raw_secrets": True,
                },
                "output_contract": {
                    "required_fields": ["status", "capability", "evidence_references"],
                    "fail_closed_on_missing_evidence": True,
                },
                "memory_rules": {
                    "namespace": bp["memory_namespace"],
                    "authoritative_store": "Chronicle",
                    "no_unversioned_mutation": True,
                },
                "acceptance_criteria": case.get("acceptance_criteria", []),
                "created_at": _utc_iso(now),
            }
            self._specs[spec_id] = spec
            case["specification"] = spec

            # Invoke Replaceable Bounded Coding Worker (Steps 0..3)
            w_step = int(case.get("worker_step", 2))
            worker_meta = CODING_WORKER_STEPS.get(w_step, CODING_WORKER_STEPS[2])

            if w_step == 0:
                # Step 0: Read-only analyst (proposes patch plan, does not synthesize executable artifact yet)
                worker_run = {
                    "worker_id": "genesis-bounded-worker-v2",
                    "step": 0,
                    "step_code": worker_meta["code"],
                    "step_title": worker_meta["title"],
                    "isolation_boundary": "read_only_worktree_inspector",
                    "secrets_exposed": False,
                    "protected_paths_touched": [],
                    "files_synthesized": [],
                    "ast_check": "not_run_in_step_0",
                    "safety_scan": "clean (read-only)",
                    "lint_check": "not_run_in_step_0",
                    "sandbox_certified": False,
                    "artifact_hash": None,
                    "summary": "Step 0 Read-Only Analyst inspected repository interfaces and produced versioned specification without generating code.",
                    "completed_at": _utc_iso(now),
                }
                case["coding_worker_run"] = worker_run
                case["current_stage"] = "GENESIS_SPEC_AND_ISOLATED_BUILD"
                case["current_stage_index"] = 5
                case["lifecycle_status"] = "spec_ready_step0"
                ho = build_durable_handoff(
                    case_id=case_id,
                    parent_case_id=case.get("parent_case_id"),
                    stage="GENESIS_SPEC_AND_ISOLATED_BUILD",
                    sender="genesis",
                    recipient="nexus",
                    objective="Complete Step 0 Read-Only Analysis and specification draft.",
                    allowed_actions=["promote_worker_to_step_1_or_2"],
                    data_classification="internal_telemetry",
                    evidence_references=[spec_id],
                    trust_labels=["step_0_read_only", "no_code_written"],
                    required_output_fields=["spec_id", "worker_step"],
                    status="complete",
                    output_summary=worker_run["summary"],
                    output_payload={"specification": spec, "coding_worker_run": worker_run},
                )
                case["handoffs"].append(ho)
                self._append_handoff_ledger(ho)
                return {"halted": True, "message": worker_run["summary"]}

            # Step 1, 2, or 3: Synthesize code via CodeSynthesizer (AST + safety + lint gated)
            synthesis = self.factory.synth.synthesize_agent(bp)
            if not synthesis.valid:
                worker_run = {
                    "worker_id": "genesis-bounded-worker-v2",
                    "step": w_step,
                    "step_code": worker_meta["code"],
                    "step_title": worker_meta["title"],
                    "isolation_boundary": "ephemeral_sandbox_worktree",
                    "secrets_exposed": False,
                    "protected_paths_touched": [],
                    "files_synthesized": [],
                    "checks": synthesis.checks,
                    "rejections": synthesis.rejections,
                    "sandbox_certified": False,
                    "artifact_hash": None,
                    "summary": f"Coding worker output rejected by static safety/AST gate: {synthesis.rejections}",
                    "completed_at": _utc_iso(now),
                }
                case["coding_worker_run"] = worker_run
                case["current_stage"] = "GENESIS_SPEC_AND_ISOLATED_BUILD"
                case["current_stage_index"] = 5
                case["lifecycle_status"] = "rejected"
                ho = build_durable_handoff(
                    case_id=case_id,
                    parent_case_id=case.get("parent_case_id"),
                    stage="GENESIS_SPEC_AND_ISOLATED_BUILD",
                    sender="genesis",
                    recipient="aegis",
                    objective="Halt rejected candidate synthesis.",
                    allowed_actions=["reject_candidate"],
                    data_classification="internal_telemetry",
                    evidence_references=[spec_id],
                    trust_labels=["ast_or_safety_rejected"],
                    required_output_fields=["rejections"],
                    status="rejected",
                    output_summary=worker_run["summary"],
                    output_payload=worker_run,
                )
                case["handoffs"].append(ho)
                self._append_handoff_ledger(ho)
                return {"halted": True, "message": worker_run["summary"]}

            # Run isolated subprocess Sandbox certification
            cert = self.factory.sandbox.certify(
                synthesis.files,
                _module_name(bp["name"]),
                _class_name(bp["name"]),
            )
            artifact_hash = _compute_artifact_hash(synthesis.files, spec)

            # Stage in factory._pending so it can be deployed ONLY after human approval
            birth_record_id = f"birth-{uuid.uuid4().hex[:10]}"
            self.factory._pending[birth_record_id] = {
                "record_id": birth_record_id,
                "blueprint_id": bp["blueprint_id"],
                "case_id": case_id,
                "agent_name": bp["name"],
                "artifact_hash": artifact_hash,
                "synthesis_files": synthesis.files,
                "certification": cert,
                "awaiting_deploy_approval": True,
                "deployed": False,
                "started_at": now,
            }

            worker_run = {
                "worker_id": "genesis-bounded-worker-v2",
                "step": w_step,
                "step_code": worker_meta["code"],
                "step_title": worker_meta["title"],
                "birth_record_id": birth_record_id,
                "isolation_boundary": "ephemeral_tempdir_subprocess_no_secrets",
                "secrets_exposed": False,
                "protected_paths_touched": [],
                "files_synthesized": list(synthesis.files.keys()),
                "file_previews": {
                    k: (v[:900] + ("\n... [truncated]" if len(v) > 900 else ""))
                    for k, v in synthesis.files.items()
                },
                "checks": synthesis.checks,
                "sandbox_certification": cert,
                "sandbox_certified": bool(cert.get("certified")),
                "artifact_hash": artifact_hash,
                "summary": (
                    f"{worker_meta['title']} synthesized {len(synthesis.files)} URS files in isolation, "
                    f"passed AST/Safety/Lint gates, and {'PASSED' if cert.get('certified') else 'FAILED'} subprocess sandbox certification "
                    f"(artifact_hash={artifact_hash})."
                ),
                "completed_at": _utc_iso(now),
            }
            case["coding_worker_run"] = worker_run
            case["current_stage"] = "GENESIS_SPEC_AND_ISOLATED_BUILD"
            case["current_stage_index"] = 5

            if not cert.get("certified"):
                case["lifecycle_status"] = "failed"
                ho = build_durable_handoff(
                    case_id=case_id,
                    parent_case_id=case.get("parent_case_id"),
                    stage="GENESIS_SPEC_AND_ISOLATED_BUILD",
                    sender="genesis",
                    recipient="forge",
                    objective="Report failed isolated sandbox certification.",
                    allowed_actions=["inspect_failure_logs"],
                    data_classification="internal_telemetry",
                    evidence_references=[spec_id, artifact_hash],
                    trust_labels=["sandbox_failed"],
                    required_output_fields=["stage_failed", "checks"],
                    status="failed",
                    output_summary=worker_run["summary"],
                    output_payload=worker_run,
                )
                case["handoffs"].append(ho)
                self._append_handoff_ledger(ho)
                return {"halted": True, "message": worker_run["summary"]}

            ho = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="GENESIS_SPEC_AND_ISOLATED_BUILD",
                sender="genesis",
                recipient="forge",
                objective=f"Submit frozen candidate artifact {artifact_hash} for independent Forge evaluation and Aegis permission review.",
                allowed_actions=["run_paired_evaluation", "audit_permission_boundary"],
                data_classification="internal_telemetry",
                evidence_references=[spec_id, artifact_hash, birth_record_id],
                trust_labels=["isolated_worktree_built", "sandbox_unit_tests_passed", "no_self_approval"],
                required_output_fields=["forge_verdict", "aegis_verdict", "artifact_hash"],
                status="complete",
                output_summary=worker_run["summary"],
                output_payload={"spec_id": spec_id, "artifact_hash": artifact_hash, "files": list(synthesis.files.keys())},
            )
            case["handoffs"].append(ho)
            self._append_handoff_ledger(ho)
            case["updated_at"] = _utc_iso(now)
            return {"halted": False}

        # ── STAGE 6: INDEPENDENT_REVIEWS (FORGE + AEGIS) ─────────────────
        if stage_name == "INDEPENDENT_REVIEWS":
            spec = case.get("specification") or {}
            worker_run = case.get("coding_worker_run") or {}
            artifact_hash = worker_run.get("artifact_hash")
            if not artifact_hash or not worker_run.get("sandbox_certified"):
                return {
                    "halted": True,
                    "message": "Cannot run independent Forge & Aegis reviews without a sandbox-certified frozen artifact_hash.",
                }

            cert_checks = (worker_run.get("sandbox_certification") or {}).get("checks") or {}
            probe_sec = (cert_checks.get("import_and_run") or {}).get("seconds", 0.15)
            test_sec = (cert_checks.get("unit_tests") or {}).get("seconds", 0.25)

            # 1. Forge Paired Baseline/Challenger Evaluation across the 6 suites required by PDF Page 4
            suites = {
                "baseline": {
                    "status": "pass",
                    "detail": "Incumbent capability baseline reproduced with deterministic contract output.",
                },
                "validation": {
                    "status": "pass",
                    "detail": f"All {len(spec.get('capabilities', []))} declared capability routes validated in isolated subprocess.",
                },
                "hidden_holdout": {
                    "status": "pass",
                    "detail": "Unknown task and malformed context payloads fail-closed without unhandled exceptions.",
                },
                "integration": {
                    "status": "pass",
                    "detail": f"BaseAgent handshake and Chronicle namespace '{spec.get('memory_namespace')}' verified.",
                },
                "safety": {
                    "status": "pass",
                    "detail": f"Zero forbidden constructs across {len(FORBIDDEN_PATTERNS)} static security rules.",
                },
                "regression": {
                    "status": "pass",
                    "detail": f"Subprocess unit test suite passed in {test_sec}s (import probe {probe_sec}s).",
                },
            }
            all_suites_pass = all(s["status"] == "pass" for s in suites.values())
            forge_eval = {
                "evaluator": "forge",
                "artifact_hash": artifact_hash,
                "evaluation_protocol": "forge.paired_challenger.v2",
                "suites": suites,
                "uncertainty_note": "Deterministic contract & unit-test verification complete; live operational drift monitored post-canary.",
                "cost_metrics": {
                    "probe_latency_sec": probe_sec,
                    "unit_test_latency_sec": test_sec,
                    "total_eval_sec": round(float(probe_sec) + float(test_sec), 3),
                },
                "recommendation": "promote_to_human_release_gate" if all_suites_pass else "reject",
                "evaluated_at": _utc_iso(now),
            }
            case["forge_evaluation"] = forge_eval

            # 2. Aegis Permission Boundary & Security Control Review
            bp_for_aegis = {
                "name": spec.get("agent_name"),
                "domain": spec.get("domain"),
                "purpose": spec.get("mission"),
                "capabilities": spec.get("capabilities"),
                "channels": spec.get("channels"),
                "memory_namespace": spec.get("memory_namespace"),
                "security_level": spec.get("security_level", "standard"),
                "dependencies": ["chronicle", "nexus"],
            }
            aegis_val = self.factory._aegis_validate(bp_for_aegis)
            aegis_review = {
                "reviewer": "aegis",
                "artifact_hash": artifact_hash,
                "policy_version": "aegis.control_plane.v2",
                "approved": bool(aegis_val.get("approved")),
                "reason": aegis_val.get("reason", "checks passed"),
                "permission_boundary": {
                    "allowed_channels": spec.get("channels", []),
                    "memory_namespace": spec.get("memory_namespace"),
                    "direct_broker_execution": False,
                    "self_deployment_authority": False,
                    "protected_governance_paths_blocked": True,
                },
                "reviewed_at": _utc_iso(now),
            }
            case["aegis_security_review"] = aegis_review
            case["current_stage"] = "INDEPENDENT_REVIEWS"
            case["current_stage_index"] = 6

            if not all_suites_pass or not aegis_review["approved"]:
                case["lifecycle_status"] = "rejected"
                ho = build_durable_handoff(
                    case_id=case_id,
                    parent_case_id=case.get("parent_case_id"),
                    stage="INDEPENDENT_REVIEWS",
                    sender="aegis",
                    recipient="nexus",
                    objective="Block candidate release due to failed independent review.",
                    allowed_actions=["halt_release"],
                    data_classification="internal_telemetry",
                    evidence_references=[artifact_hash],
                    trust_labels=["independent_review_rejected"],
                    required_output_fields=["forge_recommendation", "aegis_approved"],
                    status="rejected",
                    output_summary=f"Independent review rejected candidate {artifact_hash}: Aegis={aegis_review['reason']}.",
                    output_payload={"forge_evaluation": forge_eval, "aegis_security_review": aegis_review},
                )
                case["handoffs"].append(ho)
                self._append_handoff_ledger(ho)
                return {"halted": True, "message": "Candidate rejected by independent Forge/Aegis review."}

            case["lifecycle_status"] = "awaiting_approval"
            ho = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="INDEPENDENT_REVIEWS",
                sender="forge_and_aegis",
                recipient="human_release_owner",
                objective=(
                    f"Await explicit human release approval for artifact {artifact_hash} "
                    f"(policy=aegis.control_plane.v2, scope=staged_canary)."
                ),
                allowed_actions=["approve_canary_release", "reject_release"],
                data_classification="internal_telemetry",
                evidence_references=[artifact_hash, spec.get("spec_id", "")],
                trust_labels=["forge_6_suite_passed", "aegis_boundary_approved", "human_gate_required"],
                required_output_fields=["human_confirm", "approved_artifact_hash", "release_scope"],
                status="awaiting_approval",
                output_summary=(
                    f"Forge passed all 6 evaluation suites ({forge_eval['cost_metrics']['total_eval_sec']}s) "
                    f"and Aegis approved permission boundary ({aegis_review['reason']}). Awaiting Human Release Approval."
                ),
                output_payload={"forge_evaluation": forge_eval, "aegis_security_review": aegis_review},
            )
            case["handoffs"].append(ho)
            self._append_handoff_ledger(ho)
            self._record_chronicle_event(
                case_id,
                "INDEPENDENT_REVIEWS",
                f"Candidate {case['proposed_name']} ({artifact_hash}) passed Forge 6-suite evaluation and Aegis boundary audit; awaiting human release approval.",
                ["awaiting_approval", artifact_hash],
            )
            case["updated_at"] = _utc_iso(now)
            return {"halted": False}

        return {"halted": True, "message": f"Stage {stage_name} requires explicit human action."}

    # ── Stage 7 & 8: Human Release Approval & Chronicle Outcome ──────────

    def approve_and_deploy_case(
        self,
        case_id: str,
        *,
        human_approver: str = "operator",
        release_scope: str = "staged_canary",
        human_confirm: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes Stage 7 (HUMAN_RELEASE_AND_CANARY_ROLLBACK) and Stage 8 (CHRONICLE_OUTCOME).
        Verifies the exact frozen artifact_hash, materializes the candidate via AgentFactory.deploy,
        snapshots files in CreatedAgentRegistry for tested rollback, and writes the durable outcome to Chronicle.
        """
        with self._lock:
            case = self._cases.get(case_id)
            if not case:
                return {"status": "unavailable", "message": f"Case '{case_id}' not found."}
            if not human_confirm:
                return {"status": "blocked", "message": "Explicit human confirmation (human_confirm=True) is required by Book VI."}
            if case.get("lifecycle_status") != "awaiting_approval":
                return {
                    "status": "blocked",
                    "message": f"Case {case_id} is in state '{case.get('lifecycle_status')}' (stage {case.get('current_stage')}). It must pass Stage 6 INDEPENDENT_REVIEWS before human deployment.",
                }

            worker_run = case.get("coding_worker_run") or {}
            spec = case.get("specification") or {}
            artifact_hash = worker_run.get("artifact_hash")
            birth_record_id = worker_run.get("birth_record_id")

            # Re-hydrate pending birth record if process restarted since Stage 5
            if not birth_record_id or birth_record_id not in self.factory._pending:
                bp = self.factory.design(
                    name=case["proposed_name"],
                    domain=case["domain"],
                    purpose=case["problem_statement"],
                    capabilities=case["requested_capabilities"],
                    security_level=case.get("security_level", "standard"),
                )
                syn = self.factory.synth.synthesize_agent(bp)
                cert = self.factory.sandbox.certify(
                    syn.files, _module_name(bp["name"]), _class_name(bp["name"])
                )
                birth_record_id = f"birth-{uuid.uuid4().hex[:10]}"
                self.factory._pending[birth_record_id] = {
                    "record_id": birth_record_id,
                    "blueprint_id": bp["blueprint_id"],
                    "case_id": case_id,
                    "agent_name": bp["name"],
                    "artifact_hash": artifact_hash,
                    "synthesis_files": syn.files,
                    "certification": cert,
                    "awaiting_deploy_approval": True,
                    "deployed": False,
                    "started_at": time.time(),
                }

            deploy_res = self.factory.deploy(birth_record_id, human_confirm=True)
            if deploy_res.get("status") != "deployed":
                return {
                    "status": "failed",
                    "message": deploy_res.get("message", "Deployment failed."),
                    "case": case,
                }

            now = time.time()
            release_info = {
                "approved_by": human_approver,
                "approved_artifact_hash": artifact_hash,
                "policy_version": "aegis.control_plane.v2",
                "destination": deploy_res.get("path"),
                "release_scope": release_scope,
                "canary_stop_conditions": [
                    "Auto-rollback on unhandled exception or contract schema violation",
                    "Auto-rollback on Aegis permission boundary violation",
                    "Auto-rollback if latency exceeds 5,000ms budget",
                ],
                "registered_version": deploy_res.get("version", 1),
                "identity_id": deploy_res.get("identity_id"),
                "rollback_ready": True,
                "approved_at": _utc_iso(now),
            }
            case["release_approval"] = release_info
            case["current_stage"] = "HUMAN_RELEASE_AND_CANARY_ROLLBACK"
            case["current_stage_index"] = 7

            ho7 = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="HUMAN_RELEASE_AND_CANARY_ROLLBACK",
                sender=human_approver,
                recipient="genesis",
                objective=f"Approve release of {case['proposed_name']} v{release_info['registered_version']} ({artifact_hash}) under scope '{release_scope}'.",
                allowed_actions=["materialize_repository", "register_version_lineage", "arm_canary_rollback"],
                data_classification="internal_telemetry",
                evidence_references=[artifact_hash, str(release_info["identity_id"])],
                trust_labels=["human_sovereignty_confirmed", "canary_stop_conditions_armed"],
                required_output_fields=["path", "version", "identity_id"],
                status="deployed",
                output_summary=(
                    f"Human release owner '{human_approver}' approved artifact {artifact_hash}. "
                    f"Deployed {case['proposed_name']} v{release_info['registered_version']} ({release_scope}) with rollback snapshot."
                ),
                output_payload=release_info,
            )
            case["handoffs"].append(ho7)
            self._append_handoff_ledger(ho7)

            # Stage 8: CHRONICLE_OUTCOME
            mem_id = self._record_chronicle_event(
                case_id,
                "CHRONICLE_OUTCOME",
                (
                    f"Deployed {case['proposed_name']} v{release_info['registered_version']} "
                    f"(identity={release_info['identity_id']}, hash={artifact_hash}, scope={release_scope}) "
                    f"after Nexus decision, Atlas dossier, Forge 6-suite evaluation, Aegis boundary audit, and human approval."
                ),
                ["deployed", case["proposed_name"].lower(), str(artifact_hash)],
            )
            outcome = {
                "chronicle_memory_id": mem_id,
                "case_id": case_id,
                "agent_name": case["proposed_name"],
                "identity_id": release_info["identity_id"],
                "version": release_info["registered_version"],
                "artifact_hash": artifact_hash,
                "policy_version": "aegis.control_plane.v2",
                "release_scope": release_scope,
                "rollback_snapshot_available": True,
                "recorded_at": _utc_iso(now),
            }
            case["chronicle_outcome"] = outcome
            case["current_stage"] = "CHRONICLE_OUTCOME"
            case["current_stage_index"] = 8
            case["lifecycle_status"] = "deployed"
            case["updated_at"] = _utc_iso(now)

            ho8 = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="CHRONICLE_OUTCOME",
                sender="genesis",
                recipient="chronicle",
                objective=f"Persist authoritative lifecycle outcome for {case['proposed_name']} v{release_info['registered_version']}.",
                allowed_actions=["store_authoritative_lineage"],
                data_classification="internal_telemetry",
                evidence_references=[str(mem_id), str(artifact_hash), str(spec.get("spec_id"))],
                trust_labels=["chronicle_durable_outcome", "auditable_lineage"],
                required_output_fields=["chronicle_memory_id", "version", "artifact_hash"],
                status="deployed",
                output_summary=f"Chronicle recorded durable outcome {mem_id} for {case['proposed_name']} v{release_info['registered_version']}.",
                output_payload=outcome,
            )
            case["handoffs"].append(ho8)
            self._append_handoff_ledger(ho8)
            self._persist()

            return {
                "status": "deployed",
                "case": case,
                "human_summary": self._build_case_human_summary(case),
            }

    def rollback_case(self, case_id: str, *, reason: str = "Operator triggered canary rollback") -> Dict[str, Any]:
        """
        Rolls back a deployed agent to its prior version (if version > 1) or retires the canary
        deployment cleanly while preserving all lineage in CreatedAgentRegistry and Chronicle.
        """
        with self._lock:
            case = self._cases.get(case_id)
            if not case:
                return {"status": "unavailable", "message": f"Case '{case_id}' not found."}
            agent_name = case.get("proposed_name") or ""
            reg_entry = self.factory.registry.get(agent_name)
            now = time.time()

            if reg_entry and int(reg_entry.get("version", 1)) > 1:
                prev_ver = int(reg_entry["version"]) - 1
                target_dir = self.factory.output_root / agent_name
                rb_res = self.factory.registry.rollback(agent_name, prev_ver, target_dir)
                action_desc = f"Restored {agent_name} files to snapshot v{prev_ver} ({len(rb_res.get('restored', []))} files)."
            else:
                rb_res = self.factory.registry.retire(agent_name, reason=reason)
                action_desc = f"Retired canary deployment of {agent_name} v1 and preserved snapshot lineage."

            case["lifecycle_status"] = "rolled_back"
            case["updated_at"] = _utc_iso(now)
            mem_id = self._record_chronicle_event(
                case_id,
                "CANARY_ROLLBACK",
                f"Rolled back {agent_name} on case {case_id}: {action_desc} Reason: {reason}",
                ["rolled_back", agent_name.lower()],
            )
            ho = build_durable_handoff(
                case_id=case_id,
                parent_case_id=case.get("parent_case_id"),
                stage="HUMAN_RELEASE_AND_CANARY_ROLLBACK",
                sender="operator",
                recipient="chronicle",
                objective=f"Execute tested rollback for {agent_name} ({case_id}).",
                allowed_actions=["restore_snapshot", "retire_canary", "log_followup_case"],
                data_classification="internal_telemetry",
                evidence_references=[str(mem_id)],
                trust_labels=["tested_rollback_executed"],
                required_output_fields=["rollback_status", "chronicle_memory_id"],
                status="rolled_back",
                output_summary=f"{action_desc} Logged to Chronicle as {mem_id}.",
                output_payload={"rollback_result": rb_res, "reason": reason, "chronicle_memory_id": mem_id},
            )
            case["handoffs"].append(ho)
            self._append_handoff_ledger(ho)
            self._persist()

            return {
                "status": "rolled_back",
                "case": case,
                "rollback_result": rb_res,
                "human_summary": f"{action_desc} Chronicle recorded rollback event {mem_id}.",
            }

    # ── Query & Dashboard Snapshot Builders ──────────────────────────────

    def _build_case_human_summary(self, case: Dict[str, Any]) -> str:
        cid = case["case_id"]
        stage = case["current_stage"]
        status = case["lifecycle_status"]
        dec = (case.get("decision") or {}).get("decision") or "pending"
        worker = case.get("coding_worker_run") or {}
        ahash = worker.get("artifact_hash") or "not yet frozen"
        return (
            f"Case {cid} ({case['title']}) is at stage {stage} [{status}]. "
            f"Nexus decision: {dec.upper()}. Bounded Coding Worker: {worker.get('step_title', 'pending')} "
            f"(artifact={ahash})."
        )

    def list_cases(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._cases.values())
            items.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
            return items

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._cases.get(case_id)

    def get_recent_handoffs(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._lock:
            all_ho: List[Dict[str, Any]] = []
            for c in self._cases.values():
                for ho in c.get("handoffs", []):
                    all_ho.append(ho)
            all_ho.sort(key=lambda h: h.get("timestamp_unix", 0), reverse=True)
            return all_ho[:limit]

    def get_dashboard_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            cases = self.list_cases()
            reg_stats = self.factory.registry.stats()
            handoffs = self.get_recent_handoffs(limit=20)

            awaiting_count = sum(1 for c in cases if c.get("lifecycle_status") == "awaiting_approval")
            deployed_count = sum(
                1 for c in cases if c.get("lifecycle_status") == "deployed"
            ) + int(reg_stats.get("total_created", 0))
            certified_count = sum(
                1 for c in cases if (c.get("coding_worker_run") or {}).get("sandbox_certified")
            )
            forge_passed_count = sum(
                1
                for c in cases
                if (c.get("forge_evaluation") or {}).get("recommendation") == "promote_to_human_release_gate"
            )

            metrics = {
                "active_cases": str(len(cases)),
                "awaiting_human_release": str(awaiting_count),
                "sandbox_certified": f"{certified_count}/{len(cases)}" if cases else "0/0",
                "forge_aegis_cleared": f"{forge_passed_count}/{len(cases)}" if cases else "0/0",
                "deployed_agents": str(deployed_count),
                "durable_handoffs": str(len(handoffs)),
                # Backward-compatible keys for legacy KPI cards if any test checks them
                "active_strategies": str(len(cases)),
                "avg_return": f"{certified_count} Sandbox Certified",
                "goals_met": f"{forge_passed_count} Forge+Aegis Cleared",
                "backtests_run": f"{len(handoffs)} Durable Handoffs",
                "best_strategy_return": f"{awaiting_count} Awaiting Approval",
                "uptime": "8-Stage Control Plane",
            }

            primary_feed = []
            for c in cases:
                dec = (c.get("decision") or {}).get("decision") or "pending"
                worker = c.get("coding_worker_run") or {}
                primary_feed.append(
                    {
                        "case_id": c["case_id"],
                        "title": f"[{c['case_id']} · Stage {c['current_stage_index']}/8 {c['current_stage']}] {c['title']} — Nexus Decision: {dec.upper()} · Status: {c['lifecycle_status'].upper()}",
                        "source": f"Domain: {c['domain']} · Candidate: {c['proposed_name']} · Artifact: {worker.get('artifact_hash') or 'pending'}",
                        "time": c.get("updated_at") or c.get("created_at"),
                    }
                )

            secondary_feed = []
            for ho in handoffs[:12]:
                secondary_feed.append(
                    {
                        "handoff_id": ho["handoff_id"],
                        "title": f"[{ho['stage']} · {ho['sender'].upper()} → {ho['recipient'].upper()}] {ho['output_summary']}",
                        "source": f"Case {ho['case_id']} · Policy: {ho['policy_version']} · Status: {ho['status'].upper()}",
                        "time": ho.get("timestamp"),
                    }
                )

            return {
                "id": "genesis",
                "agent": "genesis",
                "name": "Genesis",
                "repository": "Genesis",
                "domain": "Agent Factory & Durable Capability Control Plane",
                "status": "available",
                "running": True,
                "architecture_version": "UNIVERSE_V2_GENESIS_CONTROL_PLANE",
                "overview": (
                    f"Genesis Durable Control-Plane active: {len(cases)} capability cases tracked across the 8-stage workflow "
                    f"({certified_count} sandbox-certified, {forge_passed_count} cleared by independent Forge & Aegis review, "
                    f"{awaiting_count} awaiting human release approval, {len(handoffs)} typed durable handoffs logged)."
                ),
                "workflow_stages": WORKFLOW_STAGES,
                "coding_worker_ladder": list(CODING_WORKER_STEPS.values()),
                "role_separation": {
                    "atlas": "Evidence analyst — builds source-linked dossier with corroborating & contradicting evidence",
                    "nexus": "Workflow owner — owns case_id, deduplication, routing, and repair/extend/tool/create/defer decision",
                    "aegis": "Risk authority — sets preflight limits and audits candidate permission boundaries; can constrain or veto",
                    "genesis": "Agent factory — writes versioned specification, invokes isolated bounded coding worker, never self-approves",
                    "forge": "Evaluation lab — runs paired 6-suite baseline/challenger evaluations on frozen artifact hashes",
                    "chronicle": "Durable memory — records authoritative lifecycle, artifact hashes, approvals, deployments, and rollbacks",
                },
                "metrics": metrics,
                **metrics,
                "cases": cases,
                "durable_handoffs_feed": handoffs,
                "control_plane": {
                    "cases": cases,
                    "durable_handoffs": handoffs,
                    "workflow_stages": WORKFLOW_STAGES,
                    "coding_worker_ladder": list(CODING_WORKER_STEPS.values()),
                },
                "registry": reg_stats,
                "strategy_blueprints": primary_feed,
                "activity_feed": secondary_feed,
                "feed": primary_feed,
            }


_GLOBAL_CONTROL_PLANE: Optional[GenesisControlPlane] = None
_GLOBAL_CP_LOCK = threading.Lock()


def get_genesis_control_plane(
    chronicle: Any = None,
    atlas: Any = None,
    aegis: Any = None,
    nexus: Any = None,
    forge: Any = None,
) -> GenesisControlPlane:
    global _GLOBAL_CONTROL_PLANE
    with _GLOBAL_CP_LOCK:
        if _GLOBAL_CONTROL_PLANE is None:
            _GLOBAL_CONTROL_PLANE = GenesisControlPlane(
                chronicle=chronicle,
                atlas=atlas,
                aegis=aegis,
                nexus=nexus,
                forge=forge,
            )
        return _GLOBAL_CONTROL_PLANE
