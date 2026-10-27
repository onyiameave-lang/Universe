"""
Forge - Request Handler (§26.3 centralized infrastructure)
===========================================================
Validates, queues, dispatches, executes, evaluates, and tracks training
requests through Forge's standard pipeline.  Every request has a traceable
lifecycle and an identifiable result.

This is Forge's internal orchestrator.  Specialist agents interact through
the TrainingRequest/TrainingReport public contract.
"""

import hashlib
import json
import logging
import os
import sqlite3
import socket
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

from Forge.core.training_request import (
    AcceptanceCriteria,
    DataReference,
    RequestCategory,
    RequestLifecycle,
    RequestPriority,
    RequestReconciliationAuthorization,
    RequestStatus,
    RiskCategory,
    TargetType,
    TrainingReport,
    TrainingRequest,
)
from Forge.core.resource_scheduler import ResourceScheduler, SchedulerConfig
from Forge.core.promotion import AuthorizationToken

log = logging.getLogger("forge.request_handler")


# ==============================================================================
# 1. PERSISTENCE
# ==============================================================================

_FORGE_ROOT = Path(__file__).resolve().parents[1]
_REQUESTS_DIR = _FORGE_ROOT / "memory" / "training_requests"
_REQUESTS_DB = _REQUESTS_DIR / "requests.sqlite3"
_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)

_LEGACY_REQUESTS_DB = _FORGE_ROOT / "training_requests" / "requests.sqlite3"
if _LEGACY_REQUESTS_DB.is_file() and not _REQUESTS_DB.exists():
    with closing(sqlite3.connect(str(_LEGACY_REQUESTS_DB))) as source_db, \
            closing(sqlite3.connect(str(_REQUESTS_DB))) as target_db:
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            _REQUESTS_DB.unlink(missing_ok=True)
            raise RuntimeError("Forge request database migration failed integrity check")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


# ==============================================================================
# 2. TARGET-TYPE TO ADAPTER MAPPING
# ==============================================================================

# Maps TargetType to the ExperimentDomain and adapter resolution strategy.
# Adapters are registered on the ExperimentRunner; this mapping tells the
# handler which domain to route to.
_TARGET_DOMAIN_MAP: Dict[TargetType, str] = {
    TargetType.ML_MODEL: "ML_SUPERVISED",
    TargetType.NEURAL_NETWORK: "ML_SUPERVISED",
    TargetType.RL_POLICY: "ML_SUPERVISED",
    TargetType.TRADING_STRATEGY: "ORACLE_STRATEGY",
    TargetType.INDICATOR_CONFIG: "ORACLE_STRATEGY",
    TargetType.LLM_PROMPT: "PROMPT_OPTIMIZATION",
    TargetType.TASK_CONFIG: "CONFIG_OPTIMIZATION",
    TargetType.AGENT_WORKFLOW: "WORKFLOW_OPTIMIZATION",
    TargetType.TOOL_SELECTION_POLICY: "WORKFLOW_OPTIMIZATION",
    TargetType.FEATURE_PIPELINE: "ML_SUPERVISED",
    TargetType.ENSEMBLE_CONFIG: "ML_SUPERVISED",
    TargetType.FINE_TUNE: "ML_SUPERVISED",
    TargetType.DISTILLATION: "ML_SUPERVISED",
    TargetType.EVALUATION_ONLY: "EVALUATION_ONLY",
}


# ==============================================================================
# 3. VALIDATION
# ==============================================================================

class ValidationResult(BaseModel):
    """Result of request validation."""
    valid: bool = True
    errors: List[str] = []
    warnings: List[str] = []


class RequestDailyQuotaExceeded(RuntimeError):
    """The durable shared request ledger has exhausted an agent's UTC-day quota."""


class RequestQueueCapacityExceeded(RuntimeError):
    """The durable shared request ledger has exhausted queue capacity."""


class EquivalentRequestAlreadyActive(RuntimeError):
    """An equivalent immutable request is already active in the shared ledger."""

    def __init__(self, existing_request_id: str):
        self.existing_request_id = existing_request_id
        super().__init__(f"Equivalent request is already active as {existing_request_id}")


def validate_request(request: TrainingRequest) -> ValidationResult:
    """Validate a training request against structural and domain rules.

    This does NOT check authorization -- that is handled by governance
    integration (§26.8).
    """
    errors: List[str] = []
    warnings: List[str] = []

    # Required fields
    if not request.requestor.agent_name:
        errors.append("Requestor agent_name is required")
    if not request.requestor.agent_repository:
        errors.append("Requestor agent_repository is required")
    if not request.objective.description:
        errors.append("Objective description is required")

    # Target type validation
    if request.target.target_type not in _TARGET_DOMAIN_MAP:
        errors.append(f"Unsupported target type: {request.target.target_type}")

    # Data reference checks
    if request.request_category != RequestCategory.EVALUATION_ONLY:
        if request.target.target_type in (
            TargetType.ML_MODEL, TargetType.NEURAL_NETWORK,
            TargetType.FEATURE_PIPELINE, TargetType.ENSEMBLE_CONFIG,
        ):
            if not request.data.dataset_path and not request.data.dataset_id:
                errors.append("ML-type training requires a dataset reference")

    # Budget reasonableness
    if request.budget.max_duration_seconds is not None:
        if request.budget.max_duration_seconds <= 0:
            errors.append("Budget max_duration_seconds must be positive")
        if request.budget.max_duration_seconds > 86400:
            warnings.append("Budget max_duration_seconds exceeds 24 hours")

    if request.budget.max_experiments is not None:
        if request.budget.max_experiments <= 0:
            errors.append("Budget max_experiments must be positive")

    # Risk category / approval alignment
    if request.risk_category in (RiskCategory.ELEVATED, RiskCategory.CRITICAL):
        warnings.append(
            f"{request.risk_category.value.upper()} risk requests require explicit "
            "Aegis authorization before execution"
        )

    # Constraint consistency
    if (request.constraints.required_backends and
            request.constraints.excluded_backends):
        overlap = set(request.constraints.required_backends) & set(
            request.constraints.excluded_backends
        )
        if overlap:
            errors.append(f"Backends cannot be both required and excluded: {overlap}")

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


# ==============================================================================
# 4. REQUEST HANDLER
# ==============================================================================

class RequestHandler:
    """
    Central handler for the training request lifecycle.

    Responsibilities:
    - Validates incoming requests (structural + domain rules).
    - Queues requests via ResourceScheduler.
    - Dispatches to the appropriate domain adapter.
    - Tracks lifecycle state in a durable SQLite ledger.
    - Returns structured TrainingReports.

    Does NOT:
    - Bypass Aegis or mandatory approval paths (§26.8).
    - Directly mutate production state.
    - Retry failed requests automatically (§26.6 -- failures must not
      trigger duplicate jobs).
    """

    SCHEMA_VERSION = "1"

    def __init__(
        self,
        scheduler: Optional[ResourceScheduler] = None,
        scheduler_config: Optional[SchedulerConfig] = None,
        database_path: Optional[Path] = None,
        authorization_verifier=None,
        reconciliation_verifier=None,
    ):
        self._scheduler = scheduler or ResourceScheduler(scheduler_config)
        self._db_path = database_path or _REQUESTS_DB
        self._lock = threading.Lock()
        self._lifecycles: Dict[str, RequestLifecycle] = {}
        self._requests: Dict[str, TrainingRequest] = {}
        self._reports: Dict[str, TrainingReport] = {}
        self._authorization_verifier = authorization_verifier
        self._reconciliation_verifier = reconciliation_verifier
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"

        # Adapter dispatch callbacks (registered by ForgeAgent)
        self._dispatch_callbacks: Dict[str, Callable] = {}

        self._initialize_db()

    # ------------------------------------------------------------------
    # Schema setup
    # ------------------------------------------------------------------

    def _initialize_db(self) -> None:
        """Initialize the request ledger."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(self._db_path)) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS training_requests (
                    request_id TEXT PRIMARY KEY,
                    content_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    target_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_lifecycles (
                    request_id TEXT PRIMARY KEY REFERENCES training_requests(request_id),
                    lifecycle_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS training_reports (
                    report_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL REFERENCES training_requests(request_id),
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_requests_agent
                    ON training_requests(agent_name);
                CREATE INDEX IF NOT EXISTS idx_requests_status
                    ON training_requests(status);
                CREATE INDEX IF NOT EXISTS idx_reports_request
                    ON training_reports(request_id);
            """)
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
                (self.SCHEMA_VERSION,),
            )
            stored_policy = conn.execute(
                "SELECT value FROM metadata WHERE key = 'scheduler_policy_fingerprint'"
            ).fetchone()
            if stored_policy is None:
                conn.execute(
                    "INSERT INTO metadata(key, value) VALUES('scheduler_policy_fingerprint', ?)",
                    (self._scheduler.policy_fingerprint,),
                )
            elif stored_policy["value"] != self._scheduler.policy_fingerprint:
                raise RuntimeError(
                    "Training request ledger is already bound to a different scheduler policy; "
                    "all Forge workers sharing this database must use identical quotas and limits"
                )
        self._restore_queued_requests()

    def _restore_queued_requests(self) -> None:
        """Rebuild process-local scheduler queues from the durable request ledger."""
        with closing(_connect(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT r.request_id, r.request_json, l.lifecycle_json "
                "FROM training_requests r JOIN request_lifecycles l USING(request_id) "
                "WHERE r.status = ? ORDER BY r.submitted_at, r.request_id",
                (RequestStatus.QUEUED.value,),
            ).fetchall()
        for row in rows:
            request = TrainingRequest.model_validate_json(row["request_json"])
            lifecycle = RequestLifecycle.model_validate_json(row["lifecycle_json"])
            self._requests[request.request_id] = request
            self._lifecycles[request.request_id] = lifecycle
            restored, restore_reason = self._scheduler.restore_queued(request)
            if not restored:
                if restore_reason == "equivalent_request_already_active":
                    message = "Equivalent queued request was restored earlier; duplicate rejected"
                elif restore_reason == "request_id_already_active":
                    message = "Request ID was already restored; duplicate queue entry rejected"
                elif restore_reason == "deadline_expired":
                    message = "Queued request deadline expired before recovery"
                else:
                    message = "Queued request could not be restored due to an unknown scheduler conflict"
                lifecycle.transition(
                    RequestStatus.REJECTED,
                    message,
                )
                report = TrainingReport(
                    request_id=request.request_id,
                    success=False,
                    error_message=message,
                    failure_analysis={"queue_restore_reason": restore_reason},
                )
                self._finalize_request(
                    lifecycle, report, expected_status=RequestStatus.QUEUED
                )
                log.warning(
                    "Queued request rejected during recovery: request_id=%s reason=%s",
                    request.request_id,
                    restore_reason,
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def authorization_configured(self) -> bool:
        return self._authorization_verifier is not None

    @property
    def reconciliation_authorization_configured(self) -> bool:
        return self._reconciliation_verifier is not None

    def submit_request(self, request: TrainingRequest) -> Dict[str, Any]:
        """Submit a training request. Returns submission result with queue info.

        This is the single entry point for all training requests from any
        agent.  The request is validated, persisted, and queued.
        """
        # Request timestamps are server-assigned. A client cannot backdate a
        # request to jump FIFO ordering or evade the durable UTC-day quota.
        request = request.model_copy(update={"submitted_at": datetime.now(timezone.utc)})

        # 1. Validate
        validation = validate_request(request)
        if not validation.valid:
            return {
                "status": "rejected",
                "request_id": request.request_id,
                "errors": validation.errors,
                "warnings": validation.warnings,
            }

        existing_request = self._load_request(request.request_id)
        if existing_request is not None:
            if existing_request.content_digest() != request.content_digest():
                return {
                    "status": "rejected",
                    "request_id": request.request_id,
                    "reason": "request_id is already bound to different immutable content",
                }
            existing_lifecycle = self._load_lifecycle(request.request_id)
            return {
                "status": existing_lifecycle.status.value.lower()
                if existing_lifecycle else "error",
                "request_id": request.request_id,
                "message": "This request_id was already submitted; no duplicate work was queued.",
            }

        if request.target.target_type.value not in self._dispatch_callbacks:
            return {
                "status": "rejected",
                "request_id": request.request_id,
                "reason": (
                    f"No executable dispatch adapter is registered for target "
                    f"'{request.target.target_type.value}'."
                ),
                "outstanding_requirements": [
                    "Register a domain experiment adapter and request dispatch callback"
                ],
            }

        if request.risk_category in (RiskCategory.ELEVATED, RiskCategory.CRITICAL):
            lifecycle = RequestLifecycle(request_id=request.request_id)
            lifecycle.transition(
                RequestStatus.PENDING_AUTHORIZATION,
                "Awaiting request-bound Aegis authorization",
            )
            try:
                self._persist_request(request, lifecycle)
            except EquivalentRequestAlreadyActive as exc:
                return {
                    "status": "already_active",
                    "request_id": request.request_id,
                    "duplicate_of": exc.existing_request_id,
                    "reason": str(exc),
                }
            except RequestDailyQuotaExceeded as exc:
                return {"status": "rejected", "request_id": request.request_id, "reason": str(exc)}
            self._requests[request.request_id] = request
            self._lifecycles[request.request_id] = lifecycle
            return {
                "status": "pending_authorization",
                "request_id": request.request_id,
                "authorization_configured": self._authorization_verifier is not None,
                "next_task": "training.request.authorization_request",
            }

        # 2. Check governance (§26.8)
        governance_result = self._check_governance(request)
        if governance_result is not None:
            return governance_result

        # Persist queue intent before touching the in-memory scheduler. If the
        # process exits between these operations, startup restores the request.
        lifecycle = RequestLifecycle(request_id=request.request_id)
        lifecycle.transition(RequestStatus.QUEUED, "Accepted and queued")
        try:
            self._persist_request(request, lifecycle)
        except EquivalentRequestAlreadyActive as exc:
            return {
                "status": "already_active",
                "request_id": request.request_id,
                "duplicate_of": exc.existing_request_id,
                "reason": str(exc),
            }
        except RequestDailyQuotaExceeded as exc:
            return {"status": "rejected", "request_id": request.request_id, "reason": str(exc)}
        except RequestQueueCapacityExceeded as exc:
            return {"status": "rejected", "request_id": request.request_id, "reason": str(exc)}
        self._requests[request.request_id] = request
        self._lifecycles[request.request_id] = lifecycle

        # 3. Queue via scheduler (§26.6)
        decision = self._scheduler.submit(request)
        if not decision.allowed:
            lifecycle.transition(RequestStatus.REJECTED, decision.reason)
            report = TrainingReport(
                request_id=request.request_id,
                success=False,
                error_message=decision.reason,
                failure_analysis={"queue_rejection": decision.reason},
            )
            self._finalize_request(
                lifecycle, report, expected_status=RequestStatus.QUEUED
            )
            return {
                "status": "rejected",
                "request_id": request.request_id,
                "reason": decision.reason,
                "warnings": validation.warnings,
            }

        log.info(
            "Request %s from '%s' accepted (queue position: %s)",
            request.request_id,
            request.requestor.agent_name,
            decision.queue_position,
        )

        return {
            "status": "queued",
            "request_id": request.request_id,
            "queue_position": decision.queue_position,
            "estimated_wait_seconds": decision.estimated_wait_seconds,
            "warnings": validation.warnings,
        }

    def authorization_claims(self, request: TrainingRequest, authorized_by: str) -> Dict[str, Any]:
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by must be a non-empty identity")
        return {
            "action": "authorize_training_request",
            "audience": "forge.training",
            "request_id": request.request_id,
            "request_digest": request.content_digest(),
            "risk_category": request.risk_category.value,
            "target_type": request.target.target_type.value,
            "authorized_by": authorized_by.strip(),
        }

    def authorization_request(self, request_id: str, authorized_by: str) -> Dict[str, Any]:
        request = self._load_request(request_id)
        lifecycle = self._load_lifecycle(request_id)
        if request is None or lifecycle is None:
            raise ValueError(f"Training request {request_id} was not found")
        if lifecycle.status != RequestStatus.PENDING_AUTHORIZATION:
            raise ValueError("Training request is not pending authorization")
        claims = self.authorization_claims(request, authorized_by)
        return {
            "request_id": request_id,
            "authorized_by": authorized_by.strip(),
            "audience": "forge.training",
            "required_claims": claims,
            "required_fields": [
                "authorization_id", "challenge_id", "authorized_by", "decision",
                "issued_at", "expires_at", "issuer", "audience", "signature",
                "nonce", "claims",
            ],
        }

    def authorize_request(self, request_id: str, authorization: AuthorizationToken) -> Dict[str, Any]:
        verifier = self._authorization_verifier
        if verifier is None:
            raise PermissionError("No trusted forge.training Aegis verifier is configured")
        request = self._load_request(request_id)
        lifecycle = self._load_lifecycle(request_id)
        if request is None or lifecycle is None:
            raise ValueError(f"Training request {request_id} was not found")
        if lifecycle.status != RequestStatus.PENDING_AUTHORIZATION:
            raise ValueError("Training request is not pending authorization")
        if authorization.challenge_id != request_id:
            raise PermissionError("Authorization request ID does not match")
        authorized_by = authorization.authorized_by
        claims = self.authorization_claims(request, authorized_by)
        if (
            authorization.decision != "AUTHORIZED"
            or authorization.audience != "forge.training"
            or authorization.claims != claims
            or authorization.expires_at is None
        ):
            raise PermissionError("Authorization does not bind to this exact training request")
        if not verifier.verify_and_consume(authorization, claims):
            raise PermissionError("Trusted Aegis verifier rejected the training authorization")

        claims_digest = hashlib.sha256(json.dumps(
            claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        lifecycle.authorization_id = authorization.authorization_id
        lifecycle.authorized_by = authorized_by
        lifecycle.authorization_claims_digest = claims_digest
        lifecycle.authorization_issuer = authorization.issuer
        lifecycle.authorization_nonce = authorization.nonce
        lifecycle.authorization_signature_digest = hashlib.sha256(
            (authorization.signature or "").encode("utf-8")
        ).hexdigest()
        lifecycle.transition(RequestStatus.QUEUED, "Authorized by trusted Aegis and queued")

        queue_rejection = None
        rejection_report = None
        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            queue_statuses = (RequestStatus.QUEUED.value, RequestStatus.SCHEDULED.value)
            placeholders = ", ".join("?" for _ in queue_statuses)
            queued_global = conn.execute(
                "SELECT COUNT(*) FROM training_requests "
                f"WHERE status IN ({placeholders})",
                queue_statuses,
            ).fetchone()[0]
            queued_agent = conn.execute(
                "SELECT COUNT(*) FROM training_requests "
                f"WHERE agent_name = ? AND status IN ({placeholders})",
                (request.requestor.agent_name, *queue_statuses),
            ).fetchone()[0]
            if queued_global >= self._scheduler.global_max_queued:
                queue_rejection = (
                    "Global durable training queue is full "
                    f"({self._scheduler.global_max_queued} queued)"
                )
            elif queued_agent >= self._scheduler.max_queued_for(request.requestor.agent_name):
                queue_rejection = (
                    f"Agent '{request.requestor.agent_name}' durable queue is full "
                    f"({self._scheduler.max_queued_for(request.requestor.agent_name)} queued)"
                )
            if queue_rejection:
                lifecycle.transition(RequestStatus.REJECTED, queue_rejection)
                rejection_report = TrainingReport(
                    request_id=request_id,
                    success=False,
                    error_message=queue_rejection,
                    failure_analysis={
                        "queue_rejection": queue_rejection,
                        "authorization_consumed": True,
                    },
                )
                target_status = RequestStatus.REJECTED
            else:
                target_status = RequestStatus.QUEUED
            updated = conn.execute(
                "UPDATE training_requests SET status = ? "
                "WHERE request_id = ? AND status = ?",
                (target_status.value, request_id,
                 RequestStatus.PENDING_AUTHORIZATION.value),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise RuntimeError(
                    "Training request was authorized or changed by another process"
                )
            conn.execute(
                "UPDATE request_lifecycles SET lifecycle_json = ? WHERE request_id = ?",
                (self._encode(lifecycle), request_id),
            )
            if rejection_report is not None:
                conn.execute(
                    "INSERT INTO training_reports "
                    "(report_id, request_id, report_json, created_at) VALUES (?, ?, ?, ?)",
                    (
                        rejection_report.report_id,
                        request_id,
                        self._encode(rejection_report),
                        rejection_report.timestamp.isoformat(),
                    ),
                )
            conn.commit()
        if rejection_report is not None:
            self._lifecycles[request_id] = lifecycle
            self._reports[request_id] = rejection_report
            return {
                "status": "rejected",
                "request_id": request_id,
                "reason": queue_rejection,
                "authorization_consumed": True,
            }

        # Persist the authorization and queue state before the scheduler
        # mutation. A process restart can safely restore this queued request.
        decision = self._scheduler.submit(request, count_daily=False)
        if not decision.allowed:
            lifecycle.transition(RequestStatus.REJECTED, decision.reason)
            report = TrainingReport(
                request_id=request_id,
                success=False,
                error_message=decision.reason,
                failure_analysis={"queue_rejection": decision.reason},
            )
            self._finalize_request(
                lifecycle, report, expected_status=RequestStatus.QUEUED
            )
            return {"status": "rejected", "request_id": request_id, "reason": decision.reason,
                    "authorization_consumed": True}

        self._requests[request_id] = request
        self._lifecycles[request_id] = lifecycle
        return {"status": "queued", "request_id": request_id,
                "queue_position": decision.queue_position,
                "estimated_wait_seconds": decision.estimated_wait_seconds,
                "authorization_id": authorization.authorization_id}

    def _claim_queued_request(self, request_id: str) -> tuple[str, Optional[RequestLifecycle]]:
        """Atomically claim one queued request across processes sharing the ledger."""
        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, agent_name FROM training_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != RequestStatus.QUEUED.value:
                conn.rollback()
                return "stale", None

            running_global = conn.execute(
                "SELECT COUNT(*) FROM training_requests WHERE status = ?",
                (RequestStatus.RUNNING.value,),
            ).fetchone()[0]
            running_agent = conn.execute(
                "SELECT COUNT(*) FROM training_requests WHERE status = ? AND agent_name = ?",
                (RequestStatus.RUNNING.value, row["agent_name"]),
            ).fetchone()[0]
            if (
                running_global >= self._scheduler.global_max_concurrent
                or running_agent >= self._scheduler.max_concurrent_for(row["agent_name"])
            ):
                conn.rollback()
                return "capacity", None

            lifecycle_row = conn.execute(
                "SELECT lifecycle_json FROM request_lifecycles WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if lifecycle_row is None:
                conn.rollback()
                return "stale", None
            lifecycle = RequestLifecycle.model_validate_json(lifecycle_row["lifecycle_json"])
            lifecycle.transition(RequestStatus.RUNNING, "Claimed for execution")
            lifecycle.worker_id = self._worker_id
            updated = conn.execute(
                "UPDATE training_requests SET status = ? WHERE request_id = ? AND status = ?",
                (RequestStatus.RUNNING.value, request_id, RequestStatus.QUEUED.value),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return "stale", None
            conn.execute(
                "UPDATE request_lifecycles SET lifecycle_json = ? WHERE request_id = ?",
                (self._encode(lifecycle), request_id),
            )
            conn.commit()
            return "claimed", lifecycle

    def execute_next(self) -> Optional[Dict[str, Any]]:
        """Dequeue and execute the next eligible request.

        Returns the TrainingReport dict, or None if nothing is ready.
        Called by Forge's main loop or a dedicated worker.
        """
        entry = self._scheduler.dequeue_next()
        if entry is None:
            return None

        request = self._requests.get(entry.request_id)
        if request is None:
            request = self._load_request(entry.request_id)
            if request is None:
                self._scheduler.fail(entry.request_id)
                return {"status": "error", "message": f"Request {entry.request_id} not found"}

        try:
            claim_status, lifecycle = self._claim_queued_request(entry.request_id)
        except Exception:
            self._scheduler.return_to_queue(entry)
            raise
        if claim_status == "capacity":
            self._scheduler.return_to_queue(entry)
            return None
        if claim_status != "claimed" or lifecycle is None:
            self._scheduler.fail(entry.request_id)
            return None
        self._lifecycles[entry.request_id] = lifecycle

        # Recheck at dispatch as well as submission. This also protects
        # requests that were queued by an older process before governance was
        # made fail-closed.
        governance_result = self._check_governance(request, lifecycle)
        if governance_result is not None:
            reason = governance_result["reason"]
            lifecycle.transition(RequestStatus.REJECTED, reason)
            report = TrainingReport(
                request_id=request.request_id,
                success=False,
                error_message=reason,
                outstanding_requirements=governance_result.get("outstanding_requirements", []),
            )
            self._finalize_request(lifecycle, report)
            self._scheduler.fail(entry.request_id)
            return report.model_dump(mode="json")

        start_time = time.monotonic()
        timeout_limit = self._scheduler.timeout_for(request)

        try:
            report = self._execute_request(request, lifecycle)
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            timed_out = elapsed > timeout_limit
            failure_message = (
                f"Execution exceeded its {timeout_limit:.1f}s deadline before failing: {exc}"
                if timed_out else str(exc)
            )
            lifecycle.transition(
                RequestStatus.TIMED_OUT if timed_out else RequestStatus.FAILED,
                failure_message if timed_out else f"Execution error: {exc}",
            )
            report = TrainingReport(
                request_id=request.request_id,
                success=False,
                error_message=failure_message,
                duration_seconds=elapsed,
                resource_consumption={
                    "elapsed_seconds": elapsed,
                    "timeout_seconds": timeout_limit,
                },
                failure_analysis={"execution_error": str(exc), "timed_out": timed_out},
            )
            self._finalize_request(lifecycle, report)
            self._scheduler.fail(entry.request_id, compute_units=elapsed)
            return report.model_dump(mode="json")

        elapsed = time.monotonic() - start_time
        report = report.model_copy(update={
            "duration_seconds": elapsed,
            "resource_consumption": {
                **report.resource_consumption,
                "elapsed_seconds": elapsed,
                "timeout_seconds": timeout_limit,
            },
        })

        timed_out = elapsed > timeout_limit
        if timed_out:
            timeout_message = (
                f"Execution completed after its {timeout_limit:.1f}s deadline. "
                "The late result is retained for diagnosis and cannot be promoted."
            )
            report = report.model_copy(update={
                "success": False,
                "promotion_eligible": False,
                "error_message": timeout_message,
                "outstanding_requirements": list(dict.fromkeys([
                    *report.outstanding_requirements,
                    "Repeat the work within its declared execution deadline before promotion review",
                ])),
                "failure_analysis": {
                    **report.failure_analysis,
                    "timed_out": True,
                    "timeout_seconds": timeout_limit,
                    "late_result_retained": True,
                },
            })
            lifecycle.transition(RequestStatus.TIMED_OUT, timeout_message)
            self._finalize_request(lifecycle, report)
            self._scheduler.fail(entry.request_id, compute_units=elapsed)
        elif report.success:
            lifecycle.transition(RequestStatus.COMPLETED, "Execution complete")
            self._finalize_request(lifecycle, report)
            self._scheduler.complete(entry.request_id, compute_units=elapsed)
        else:
            lifecycle.transition(
                RequestStatus.FAILED,
                report.error_message or "Adapter returned an unsuccessful training report",
            )
            self._finalize_request(lifecycle, report)
            self._scheduler.fail(entry.request_id, compute_units=elapsed)

        log.info(
            "Request %s completed in %.1fs (success: %s)",
            request.request_id, elapsed, report.success,
        )

        return report.model_dump(mode="json")

    def get_request_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Return the current lifecycle status of a request."""
        lifecycle = self._load_lifecycle(request_id)
        if lifecycle is None:
            return None
        self._lifecycles[request_id] = lifecycle
        return {
            "request_id": request_id,
            "status": lifecycle.status.value,
            "submitted_at": lifecycle.submitted_at.isoformat(),
            "started_at": lifecycle.started_at.isoformat() if lifecycle.started_at else None,
            "completed_at": lifecycle.completed_at.isoformat() if lifecycle.completed_at else None,
            "experiment_id": lifecycle.experiment_id,
            "worker_id": lifecycle.worker_id,
            "error_message": lifecycle.error_message,
            "authorization": ({
                "authorization_id": lifecycle.authorization_id,
                "authorized_by": lifecycle.authorized_by,
                "issuer": lifecycle.authorization_issuer,
                "claims_digest": lifecycle.authorization_claims_digest,
                "signature_digest": lifecycle.authorization_signature_digest,
            } if lifecycle.authorization_id else None),
            "status_history": lifecycle.status_history,
        }

    def operational_status(self) -> Dict[str, Any]:
        """Expose durable queue counts and workers that may need reconciliation."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with closing(_connect(self._db_path)) as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM training_requests GROUP BY status"
            ).fetchall()
            running_rows = conn.execute(
                "SELECT r.request_id, r.request_json, l.lifecycle_json "
                "FROM training_requests r JOIN request_lifecycles l USING(request_id) "
                "WHERE r.status = ? ORDER BY r.submitted_at, r.request_id",
                (RequestStatus.RUNNING.value,),
            ).fetchall()
            daily_rows = conn.execute(
                "SELECT agent_name, COUNT(*) AS count FROM training_requests "
                "WHERE submitted_at >= ? GROUP BY agent_name",
                (day_start.isoformat(),),
            ).fetchall()
            queued_rows = conn.execute(
                "SELECT agent_name, COUNT(*) AS count FROM training_requests "
                "WHERE status IN (?, ?) GROUP BY agent_name",
                (RequestStatus.QUEUED.value, RequestStatus.SCHEDULED.value),
            ).fetchall()
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        consistency = self.verify_state_consistency()
        running = []
        for row in running_rows:
            request = TrainingRequest.model_validate_json(row["request_json"])
            lifecycle = RequestLifecycle.model_validate_json(row["lifecycle_json"])
            started = lifecycle.started_at
            elapsed = max(0.0, (now - started).total_seconds()) if started else None
            timeout = self._scheduler.timeout_for(request)
            running.append({
                "request_id": request.request_id,
                "agent_name": request.requestor.agent_name,
                "worker_id": lifecycle.worker_id,
                "started_at": started.isoformat() if started else None,
                "elapsed_seconds": elapsed,
                "timeout_seconds": timeout,
                "exceeded_timeout": bool(elapsed is not None and elapsed > timeout),
            })
        return {
            "ready": integrity == "ok" and consistency.get("intact") is True,
            "database_integrity": integrity,
            "state_consistency": consistency,
            "counts_by_status": {row["status"]: row["count"] for row in status_rows},
            "queued_requests": sum(
                row["count"] for row in status_rows
                if row["status"] == RequestStatus.QUEUED.value
            ),
            "queue_capacity": {
                "global_queued": sum(row["count"] for row in queued_rows),
                "global_limit": self._scheduler.global_max_queued,
                "per_agent": {
                    row["agent_name"]: {
                        "queued": row["count"],
                        "limit": self._scheduler.max_queued_for(row["agent_name"]),
                    }
                    for row in queued_rows
                },
            },
            "pending_authorization": sum(
                row["count"] for row in status_rows
                if row["status"] == RequestStatus.PENDING_AUTHORIZATION.value
            ),
            "daily_request_counts_utc": {
                row["agent_name"]: {
                    "count": row["count"],
                    "limit": self._scheduler.max_daily_for(row["agent_name"]),
                }
                for row in daily_rows
            },
            "scheduler_policy_fingerprint": self._scheduler.policy_fingerprint,
            "running_requests": running,
            "authorization_configured": self.authorization_configured,
            "worker_id": self._worker_id,
        }

    def verify_state_consistency(self) -> Dict[str, Any]:
        """Validate request identity, lifecycle state, and terminal reports."""
        try:
            with closing(_connect(self._db_path)) as conn:
                rows = conn.execute(
                    "SELECT r.request_id, r.content_digest, r.request_json, r.status, "
                    "l.request_id AS lifecycle_request_id, l.lifecycle_json, "
                    "COUNT(t.report_id) AS report_count, MIN(t.report_json) AS report_json "
                    "FROM training_requests r "
                    "LEFT JOIN request_lifecycles l USING(request_id) "
                    "LEFT JOIN training_reports t USING(request_id) "
                    "GROUP BY r.request_id ORDER BY r.request_id"
                ).fetchall()

            terminal_count = 0
            for row in rows:
                request_id = row["request_id"]
                try:
                    request = TrainingRequest.model_validate_json(row["request_json"])
                    lifecycle = RequestLifecycle.model_validate_json(row["lifecycle_json"])
                    status = RequestStatus(row["status"])
                except Exception:
                    return {
                        "intact": False,
                        "issue": "request_or_lifecycle_invalid",
                        "request_id": request_id,
                    }
                if (
                    request.request_id != request_id
                    or request.content_digest() != row["content_digest"]
                    or row["lifecycle_request_id"] != request_id
                    or lifecycle.request_id != request_id
                    or lifecycle.status != status
                    or (
                        lifecycle.status_history
                        and lifecycle.status_history[-1].get("to_status") != status.value
                    )
                ):
                    return {
                        "intact": False,
                        "issue": "request_lifecycle_binding_mismatch",
                        "request_id": request_id,
                    }

                reports = row["report_count"]
                if status.is_terminal:
                    terminal_count += 1
                    if reports != 1:
                        return {
                            "intact": False,
                            "issue": "terminal_report_count_mismatch",
                            "request_id": request_id,
                        }
                    try:
                        report = TrainingReport.model_validate_json(row["report_json"])
                    except Exception:
                        return {
                            "intact": False,
                            "issue": "terminal_report_invalid",
                            "request_id": request_id,
                        }
                    if report.request_id != request_id:
                        return {
                            "intact": False,
                            "issue": "terminal_report_binding_mismatch",
                            "request_id": request_id,
                        }
                elif reports != 0:
                    return {
                        "intact": False,
                        "issue": "nonterminal_request_has_report",
                        "request_id": request_id,
                    }

            return {
                "intact": True,
                "requests": len(rows),
                "terminal_requests": terminal_count,
            }
        except Exception as exc:
            log.exception("training request ledger consistency check failed")
            return {"intact": False, "issue": type(exc).__name__}

    def request_reconciliation_claims(
        self, request_id: str, authorized_by: str, reason: str, worker_terminated: bool
    ) -> Dict[str, Any]:
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by must be a non-empty operator identity")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("reason must contain 1 through 1000 non-whitespace characters")
        if worker_terminated is not True:
            raise ValueError("worker_terminated must be explicitly attested")
        request = self._load_request(request_id)
        lifecycle = self._load_lifecycle(request_id)
        if request is None or lifecycle is None:
            raise ValueError(f"Training request {request_id} was not found")
        if lifecycle.status != RequestStatus.RUNNING:
            raise ValueError("Only a persisted RUNNING request can be reconciled")
        lifecycle_digest = hashlib.sha256(self._encode(lifecycle).encode("utf-8")).hexdigest()
        return {
            "action": "reconcile_abandoned_training_request",
            "audience": "forge.operations",
            "request_id": request_id,
            "request_digest": request.content_digest(),
            "lifecycle_digest": lifecycle_digest,
            "worker_id": lifecycle.worker_id,
            "authorized_by": authorized_by.strip(),
            "reason": reason.strip(),
            "worker_terminated": True,
        }

    def reconcile_abandoned_request(
        self, request_id: str, authorization: RequestReconciliationAuthorization
    ) -> Dict[str, Any]:
        verifier = self._reconciliation_verifier
        if verifier is None:
            raise PermissionError("No trusted forge.operations verifier is configured")
        if authorization.request_id != request_id:
            raise PermissionError("Authorization request ID does not match")
        reason = authorization.claims.get("reason")
        required_claims = self.request_reconciliation_claims(
            request_id, authorization.authorized_by, reason,
            authorization.claims.get("worker_terminated"),
        )
        if (
            authorization.decision != "AUTHORIZED"
            or authorization.audience != "forge.operations"
            or authorization.claims != required_claims
        ):
            raise PermissionError("Authorization claims do not match the current abandoned request")
        if not verifier.verify_and_consume(authorization, required_claims):
            raise PermissionError("Trusted Aegis verifier rejected the reconciliation authorization")

        # Recheck all claims after nonce consumption. The database update is
        # conditional on RUNNING so only one operator can close this record.
        current_claims = self.request_reconciliation_claims(
            request_id, authorization.authorized_by, reason, True
        )
        if current_claims != required_claims:
            raise RuntimeError("Request changed while reconciliation authorization was verified")
        request = self._load_request(request_id)
        lifecycle = self._load_lifecycle(request_id)
        if request is None or lifecycle is None:
            raise RuntimeError("Request disappeared before reconciliation commit")
        completed_at = datetime.now(timezone.utc)
        audit = {
            "action": required_claims["action"],
            "operator": authorization.authorized_by,
            "reason": required_claims["reason"],
            "worker_terminated_attested": True,
            "worker_id": required_claims["worker_id"],
            "authorization_id": authorization.authorization_id,
            "issuer": authorization.issuer,
            "nonce": authorization.nonce,
            "claims_digest": hashlib.sha256(json.dumps(
                required_claims, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")).hexdigest(),
            "signature_digest": hashlib.sha256(authorization.signature.encode("utf-8")).hexdigest(),
            "reconciled_at": completed_at.isoformat(),
        }
        lifecycle.transition(
            RequestStatus.ABORTED,
            f"Operator-authorized reconciliation {authorization.authorization_id}",
        )
        report = TrainingReport(
            request_id=request_id,
            success=False,
            error_message="Request marked ABORTED after operator verified its worker had terminated.",
            failure_analysis={"reconciliation": audit},
            known_limitations=["Reconciliation records state; it does not terminate worker code."],
        )
        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, lifecycle_json FROM training_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != RequestStatus.RUNNING.value:
                conn.rollback()
                raise RuntimeError("Request is no longer RUNNING")
            persisted_lifecycle = RequestLifecycle.model_validate_json(row["lifecycle_json"])
            persisted_digest = hashlib.sha256(self._encode(persisted_lifecycle).encode("utf-8")).hexdigest()
            if persisted_digest != required_claims["lifecycle_digest"]:
                conn.rollback()
                raise RuntimeError("Persisted request lifecycle changed before reconciliation commit")
            request_updated = conn.execute(
                "UPDATE training_requests SET status = ? WHERE request_id = ? AND status = ?",
                (RequestStatus.ABORTED.value, request_id, RequestStatus.RUNNING.value),
            )
            if request_updated.rowcount != 1:
                conn.rollback()
                raise RuntimeError("Request changed before reconciliation committed")
            lifecycle_updated = conn.execute(
                "UPDATE request_lifecycles SET lifecycle_json = ? WHERE request_id = ?",
                (self._encode(lifecycle), request_id),
            )
            if lifecycle_updated.rowcount != 1:
                conn.rollback()
                raise RuntimeError("Request lifecycle disappeared before reconciliation committed")
            conn.execute(
                "INSERT INTO training_reports (report_id, request_id, report_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (report.report_id, report.request_id, self._encode(report), completed_at.isoformat()),
            )
            conn.commit()
        self._lifecycles[request_id] = lifecycle
        self._reports[request_id] = report
        self._scheduler.release_terminated(request_id)
        return {"status": "aborted", "request_id": request_id,
                "report": report.model_dump(mode="json"), "audit": audit}

    def get_report(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Return the training report for a completed request."""
        report = self._load_report(request_id)
        if report is None:
            return None
        self._reports[request_id] = report
        return report.model_dump(mode="json")

    def cancel_request(self, request_id: str) -> Dict[str, Any]:
        """Cancel queued work; running adapter code cannot be safely killed."""
        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM training_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"status": "not_found", "request_id": request_id}
            if row["status"] == RequestStatus.RUNNING.value:
                lifecycle_row = conn.execute(
                    "SELECT lifecycle_json FROM request_lifecycles WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                conn.rollback()
                lifecycle = (
                    RequestLifecycle.model_validate_json(lifecycle_row["lifecycle_json"])
                    if lifecycle_row else None
                )
                return {
                    "status": "running",
                    "request_id": request_id,
                    "message": "Running work cannot be forcibly cancelled; verify worker termination before authorized reconciliation.",
                    "worker_id": lifecycle.worker_id if lifecycle else None,
                }
            if row["status"] != RequestStatus.QUEUED.value:
                conn.rollback()
                return {"status": row["status"].lower(), "request_id": request_id}
            lifecycle_row = conn.execute(
                "SELECT lifecycle_json FROM request_lifecycles WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if lifecycle_row is None:
                conn.rollback()
                return {"status": "error", "request_id": request_id,
                        "message": "request lifecycle record is missing"}
            lifecycle = RequestLifecycle.model_validate_json(lifecycle_row["lifecycle_json"])
            lifecycle.transition(RequestStatus.CANCELLED, "Cancelled by requestor")
            report = TrainingReport(
                request_id=request_id,
                success=False,
                error_message="Request was cancelled before execution began.",
                failure_analysis={"cancelled_before_execution": True},
            )
            updated = conn.execute(
                "UPDATE training_requests SET status = ? WHERE request_id = ? AND status = ?",
                (RequestStatus.CANCELLED.value, request_id, RequestStatus.QUEUED.value),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return {"status": "running", "request_id": request_id}
            lifecycle_updated = conn.execute(
                "UPDATE request_lifecycles SET lifecycle_json = ? WHERE request_id = ?",
                (self._encode(lifecycle), request_id),
            )
            if lifecycle_updated.rowcount != 1:
                conn.rollback()
                return {
                    "status": "error",
                    "request_id": request_id,
                    "message": "request lifecycle changed before cancellation committed",
                }
            conn.execute(
                "INSERT INTO training_reports "
                "(report_id, request_id, report_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    report.report_id,
                    report.request_id,
                    self._encode(report),
                    report.timestamp.isoformat(),
                ),
            )
            conn.commit()
        self._lifecycles[request_id] = lifecycle
        self._reports[request_id] = report
        self._scheduler.cancel(request_id)
        return {"status": "cancelled", "request_id": request_id}

    def list_requests(
        self, agent_name: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List training requests, optionally filtered by agent or status."""
        results = []
        with closing(_connect(self._db_path)) as conn:
            query = "SELECT request_id, status, agent_name, target_type, submitted_at FROM training_requests"
            conditions = []
            params = []
            if agent_name:
                conditions.append("agent_name = ?")
                params.append(agent_name)
            if status:
                conditions.append("status = ?")
                params.append(status)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY submitted_at DESC"
            for row in conn.execute(query, params):
                results.append({
                    "request_id": row["request_id"],
                    "status": row["status"],
                    "agent_name": row["agent_name"],
                    "target_type": row["target_type"],
                    "submitted_at": row["submitted_at"],
                })
        return results

    def register_dispatch_callback(
        self, target_type: TargetType, callback: Callable
    ) -> None:
        """Register a domain-specific execution callback for a target type.

        The callback receives (TrainingRequest, RequestLifecycle) and returns
        a TrainingReport.
        """
        self._dispatch_callbacks[target_type.value] = callback
        log.info("Registered dispatch callback for target type: %s", target_type.value)

    def scheduler_snapshot(self) -> Dict[str, Any]:
        """Return the scheduler's observable state."""
        return self._scheduler.snapshot().model_dump(mode="json")

    # ------------------------------------------------------------------
    # Internal execution
    # ------------------------------------------------------------------

    def _execute_request(
        self, request: TrainingRequest, lifecycle: RequestLifecycle
    ) -> TrainingReport:
        """Dispatch a request to the appropriate adapter and collect results."""
        target_type = request.target.target_type.value

        # Check for a registered dispatch callback
        callback = self._dispatch_callbacks.get(target_type)
        if callback is not None:
            return callback(request, lifecycle)

        # If no specific callback, attempt generic experiment execution
        domain = _TARGET_DOMAIN_MAP.get(request.target.target_type)
        if domain is None:
            return TrainingReport(
                request_id=request.request_id,
                success=False,
                error_message=f"No adapter registered for target type: {target_type}",
                known_limitations=[f"Target type '{target_type}' requires an explicit adapter"],
            )

        # For target types without a registered callback, return a structured
        # report indicating the adapter needs to be implemented
        return TrainingReport(
            request_id=request.request_id,
            success=False,
            error_message=(
                f"Domain '{domain}' adapter exists but no dispatch callback is registered "
                f"for target type '{target_type}'. Register a callback via "
                f"register_dispatch_callback()."
            ),
            outstanding_requirements=[
                f"Register dispatch callback for {target_type}",
            ],
        )

    def _check_governance(
        self, request: TrainingRequest, lifecycle: Optional[RequestLifecycle] = None
    ) -> Optional[Dict[str, Any]]:
        """Check governance requirements (§26.8).

        Elevated and critical requests can execute only after their persisted
        lifecycle records a verified, request-bound authorization.
        """
        if request.risk_category in (RiskCategory.ELEVATED, RiskCategory.CRITICAL):
            if lifecycle and lifecycle.authorization_id and lifecycle.authorization_claims_digest:
                return None
            log.warning("Request %s blocked: %s authorization is missing",
                        request.request_id, request.risk_category.value.upper())
            return {
                "status": "rejected",
                "request_id": request.request_id,
                "reason": (
                    f"{request.risk_category.value.upper()} risk request has no "
                    "verified Aegis authorization in its persisted lifecycle."
                ),
                "outstanding_requirements": [
                    "Obtain a signed authorization bound to the exact request digest"
                ],
            }
        return None

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode(model: BaseModel) -> str:
        return json.dumps(
            model.model_dump(mode="json"),
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )

    def _persist_request(
        self, request: TrainingRequest, lifecycle: RequestLifecycle
    ) -> None:
        """Durably persist a request and its lifecycle."""
        encoded = self._encode(request)
        digest = request.content_digest()
        lifecycle_json = self._encode(lifecycle)

        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                active_statuses = (
                    RequestStatus.PENDING_AUTHORIZATION.value,
                    RequestStatus.QUEUED.value,
                    RequestStatus.SCHEDULED.value,
                    RequestStatus.RUNNING.value,
                    RequestStatus.EVALUATING.value,
                )
                placeholders = ", ".join("?" for _ in active_statuses)
                duplicate = conn.execute(
                    "SELECT request_id FROM training_requests "
                    f"WHERE content_digest = ? AND status IN ({placeholders}) "
                    "ORDER BY submitted_at, request_id LIMIT 1",
                    (digest, *active_statuses),
                ).fetchone()
                if duplicate is not None:
                    raise EquivalentRequestAlreadyActive(duplicate["request_id"])

                now = datetime.now(timezone.utc)
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                next_day = day_start + timedelta(days=1)
                count = conn.execute(
                    "SELECT COUNT(*) FROM training_requests "
                    "WHERE agent_name = ? AND submitted_at >= ? AND submitted_at < ?",
                    (
                        request.requestor.agent_name,
                        day_start.isoformat(),
                        next_day.isoformat(),
                    ),
                ).fetchone()[0]
                daily_limit = self._scheduler.max_daily_for(request.requestor.agent_name)
                if count >= daily_limit:
                    raise RequestDailyQuotaExceeded(
                        f"Agent '{request.requestor.agent_name}' exceeded durable UTC-day request "
                        f"limit ({daily_limit})"
                    )
                if lifecycle.status in (RequestStatus.QUEUED, RequestStatus.SCHEDULED):
                    queued_statuses = (
                        RequestStatus.QUEUED.value,
                        RequestStatus.SCHEDULED.value,
                    )
                    queued_placeholders = ", ".join("?" for _ in queued_statuses)
                    queued_global = conn.execute(
                        "SELECT COUNT(*) FROM training_requests "
                        f"WHERE status IN ({queued_placeholders})",
                        queued_statuses,
                    ).fetchone()[0]
                    queued_agent = conn.execute(
                        "SELECT COUNT(*) FROM training_requests "
                        f"WHERE agent_name = ? AND status IN ({queued_placeholders})",
                        (request.requestor.agent_name, *queued_statuses),
                    ).fetchone()[0]
                    global_limit = self._scheduler.global_max_queued
                    agent_limit = self._scheduler.max_queued_for(request.requestor.agent_name)
                    if queued_global >= global_limit:
                        raise RequestQueueCapacityExceeded(
                            f"Global durable training queue is full ({global_limit} queued)"
                        )
                    if queued_agent >= agent_limit:
                        raise RequestQueueCapacityExceeded(
                            f"Agent '{request.requestor.agent_name}' durable queue is full "
                            f"({agent_limit} queued)"
                        )
                conn.execute(
                    """INSERT INTO training_requests
                       (request_id, content_digest, request_json, status,
                        submitted_at, agent_name, target_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        request.request_id, digest, encoded,
                        lifecycle.status.value,
                        now.isoformat(),
                        request.requestor.agent_name,
                        request.target.target_type.value,
                    ),
                )
                conn.execute(
                    """INSERT INTO request_lifecycles
                       (request_id, lifecycle_json) VALUES (?, ?)""",
                    (request.request_id, lifecycle_json),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _finalize_request(
        self,
        lifecycle: RequestLifecycle,
        report: TrainingReport,
        *,
        expected_status: RequestStatus = RequestStatus.RUNNING,
    ) -> None:
        """Atomically persist terminal request state and its matching report."""
        if not lifecycle.status.is_terminal:
            raise ValueError("Only terminal request lifecycles can be finalized")
        if lifecycle.request_id != report.request_id:
            raise ValueError("Lifecycle and report request IDs do not match")
        lifecycle_json = self._encode(lifecycle)
        report_json = self._encode(report)
        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    "UPDATE training_requests SET status = ? "
                    "WHERE request_id = ? AND status = ?",
                    (
                        lifecycle.status.value,
                        lifecycle.request_id,
                        expected_status.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        f"Request is no longer {expected_status.value}; terminal result was not committed"
                    )
                lifecycle_updated = conn.execute(
                    "UPDATE request_lifecycles SET lifecycle_json = ? WHERE request_id = ?",
                    (lifecycle_json, lifecycle.request_id),
                )
                if lifecycle_updated.rowcount != 1:
                    raise RuntimeError("Request lifecycle record is missing during finalization")
                conn.execute(
                    "INSERT OR REPLACE INTO training_reports "
                    "(report_id, request_id, report_json, created_at) VALUES (?, ?, ?, ?)",
                    (
                        report.report_id,
                        report.request_id,
                        report_json,
                        report.timestamp.isoformat(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._lifecycles[lifecycle.request_id] = lifecycle
        self._reports[report.request_id] = report

    def _load_request(self, request_id: str) -> Optional[TrainingRequest]:
        """Load a request from the ledger."""
        with closing(_connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT request_json FROM training_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                return TrainingRequest.model_validate_json(row["request_json"])
        return None

    def _load_lifecycle(self, request_id: str) -> Optional[RequestLifecycle]:
        """Load a lifecycle from the ledger."""
        with closing(_connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT lifecycle_json FROM request_lifecycles WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                return RequestLifecycle.model_validate_json(row["lifecycle_json"])
        return None

    def _load_report(self, request_id: str) -> Optional[TrainingReport]:
        """Load a report from the ledger."""
        with closing(_connect(self._db_path)) as conn:
            row = conn.execute(
                "SELECT report_json FROM training_reports WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                return TrainingReport.model_validate_json(row["report_json"])
        return None
