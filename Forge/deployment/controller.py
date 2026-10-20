"""Governed staged model rollout and rollback control plane.

This module records deployment state and supplies deterministic traffic routes.
It does not fabricate approvals or health results: both come from trusted,
injected adapters, and every operation fails closed when they are absent.
Serving infrastructure must consume ``route()`` for these stages to affect
traffic; the controller does not claim a rollout is live by itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Forge.core.benchmark import BenchmarkReport, BenchmarkStatus
from Forge.core.model_registry import ModelRegistry

log = logging.getLogger("forge.deployment")


class DeploymentPhase(str, Enum):
    SHADOW = "shadow"
    CANARY = "canary"
    PRODUCTION = "production"
    ROLLED_BACK = "rolled_back"


class DeploymentStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class DeploymentAuthorization(BaseModel):
    """Signed Aegis assertion; values are claims until the verifier validates them."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action: str
    decision: str
    issuer: str
    audience: str = "forge.deployment"
    authorized_by: str
    nonce: str
    signature: str
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    claims: Dict[str, Any]

    @field_validator("decision")
    @classmethod
    def _decision_must_be_authorized(cls, value):
        if value != "AUTHORIZED":
            raise ValueError("deployment authorization decision must be AUTHORIZED")
        return value

    @field_validator("issuer", "audience", "authorized_by", "nonce", "signature", "action")
    @classmethod
    def _required_text(cls, value):
        if not value.strip():
            raise ValueError("authorization identity, action, nonce, and signature are required")
        return value

    @field_validator("claims", mode="before")
    @classmethod
    def _copy_claims(cls, value):
        if not isinstance(value, dict):
            raise ValueError("authorization claims must be a mapping")
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


class DeploymentAuthorizationRequest(BaseModel):
    """Exact claims an external Aegis signer must approve."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    action: str
    claims: Dict[str, Any]
    expires_at: datetime


class HealthAssessment(BaseModel):
    """Evidence from a trusted deployment health observer."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    deployment_id: str
    model_id: str
    target_phase: DeploymentPhase
    passed: bool = Field(strict=True)
    assessed_at: datetime
    observer: str
    evidence: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("assessment_id", "deployment_id", "model_id", "observer")
    @classmethod
    def _non_empty_identity(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("health assessment identity fields must be non-empty strings")
        return value

    @field_validator("evidence", mode="before")
    @classmethod
    def _copy_evidence(cls, value):
        if not isinstance(value, dict) or not value:
            raise ValueError("health assessment evidence must be a non-empty mapping")
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))

    @field_validator("assessed_at")
    @classmethod
    def _aware_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("health assessment timestamp must be timezone-aware")
        return value


class DeploymentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    deployment_id: str
    domain: str
    model_id: str
    previous_model_id: Optional[str] = None
    previous_deployment_id: Optional[str] = None
    phase: DeploymentPhase
    status: DeploymentStatus
    canary_percent: int
    benchmark_digest: str
    artifact_digest: str
    expected_generation: int
    production_generation: Optional[int] = None
    started_at: datetime
    updated_at: datetime
    last_health_assessment_id: Optional[str] = None
    failure_reason: Optional[str] = None


class TrafficRoute(BaseModel):
    """Route decision returned to the serving layer for one request key."""

    model_config = ConfigDict(frozen=True)

    domain: str
    primary_model_id: Optional[str]
    shadow_model_id: Optional[str] = None
    deployment_id: Optional[str] = None
    phase: Optional[DeploymentPhase] = None
    canary_percent: int = 0


class DeploymentVerifier(Protocol):
    """Trusted Aegis adapter; must verify a signature and consume nonce once."""

    def verify_and_consume(
        self,
        authorization: DeploymentAuthorization,
        required_claims: Dict[str, Any],
    ) -> bool:
        ...


class DeploymentHealthGate(Protocol):
    """Trusted runtime observer; caller-supplied booleans are not accepted."""

    def operational_status(self) -> Dict[str, Any]:
        """Return ``{"ready": True}`` only when its telemetry source is usable."""
        ...

    def assess(
        self,
        deployment: DeploymentRecord,
        target_phase: DeploymentPhase,
    ) -> HealthAssessment:
        ...


class DeploymentError(RuntimeError):
    pass


class DeploymentAuthorizationError(DeploymentError):
    pass


class DeploymentConflict(DeploymentError):
    pass


class DeploymentController:
    """SQLite-backed rollout state machine with health-gated stage changes."""

    def __init__(
        self,
        registry: ModelRegistry,
        authorization_verifier: Optional[DeploymentVerifier] = None,
        health_gate: Optional[DeploymentHealthGate] = None,
        database_path: Optional[str | Path] = None,
        canary_percent: int = 5,
    ):
        if not 1 <= canary_percent <= 99:
            raise ValueError("canary_percent must be between 1 and 99")
        self._registry = registry
        self._authorization_verifier = authorization_verifier
        self._health_gate = health_gate
        self._database_path = (
            Path(database_path).resolve()
            if database_path is not None
            else Path(__file__).resolve().with_name("deployments.sqlite3")
        )
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._canary_percent = canary_percent
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deployment_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deployments "
                "(deployment_id TEXT PRIMARY KEY, domain TEXT NOT NULL, model_id TEXT NOT NULL, "
                "phase TEXT NOT NULL, status TEXT NOT NULL, record_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_rollout_per_domain "
                "ON deployments(domain) WHERE status = 'in_progress'"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS active_models "
                "(domain TEXT PRIMARY KEY, model_id TEXT, deployment_id TEXT, generation INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deployment_events "
                "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, "
                "deployment_id TEXT NOT NULL, event_json TEXT NOT NULL, "
                "previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_transitions "
                "(request_id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, "
                "from_phase TEXT NOT NULL, to_phase TEXT NOT NULL, claims_json TEXT NOT NULL, "
                "health_json TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_pending_transition_per_rollout "
                "ON pending_transitions(deployment_id) WHERE status = 'pending'"
            )
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Deployment database integrity check failed: {integrity}")
            connection.execute("BEGIN IMMEDIATE")
            schema = connection.execute(
                "SELECT value FROM deployment_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if schema is not None and schema["value"] != "1":
                raise RuntimeError(
                    f"Unsupported deployment database schema version: {schema['value']}"
                )
            if schema is None:
                connection.execute(
                    "INSERT INTO deployment_metadata (key, value) VALUES ('schema_version', '1')"
                )
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Deployment database integrity check failed: {integrity}")
            connection.commit()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def _digest(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _record_from_json(raw: str) -> DeploymentRecord:
        return DeploymentRecord.model_validate_json(raw)

    def _read_active(self, connection: sqlite3.Connection, domain: str):
        row = connection.execute(
            "SELECT model_id, deployment_id, generation FROM active_models WHERE domain = ?",
            (domain,),
        ).fetchone()
        if row is None:
            return {"model_id": None, "deployment_id": None, "generation": 0}
        return dict(row)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        deployment_id: str,
        event_type: str,
        details: Dict[str, Any],
        timestamp: datetime,
    ) -> Dict[str, Any]:
        last = connection.execute(
            "SELECT event_hash FROM deployment_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = last["event_hash"] if last else "0" * 64
        event = {
            "event_id": str(uuid.uuid4()),
            "deployment_id": deployment_id,
            "event_type": event_type,
            "timestamp": timestamp.isoformat(),
            "details": details,
        }
        event_hash = hashlib.sha256(
            (previous_hash + self._canonical_json(event)).encode("utf-8")
        ).hexdigest()
        connection.execute(
            "INSERT INTO deployment_events "
            "(event_id, deployment_id, event_json, previous_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (event["event_id"], deployment_id, self._canonical_json(event), previous_hash, event_hash),
        )
        connection.execute(
            "INSERT INTO deployment_metadata (key, value) VALUES ('audit_head_hash', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (event_hash,),
        )
        return event

    def _verify_authorization(
        self,
        authorization: Optional[DeploymentAuthorization],
        required_claims: Dict[str, Any],
    ) -> None:
        if authorization is None:
            raise DeploymentAuthorizationError("A signed Aegis authorization is required.")
        now = datetime.now(timezone.utc)
        if (
            authorization.issued_at.tzinfo is None
            or authorization.issued_at.utcoffset() is None
            or authorization.expires_at.tzinfo is None
            or authorization.expires_at.utcoffset() is None
        ):
            raise DeploymentAuthorizationError("Authorization timestamps must be timezone-aware.")
        if authorization.issued_at > now or authorization.expires_at <= now:
            raise DeploymentAuthorizationError("Authorization is not currently valid.")
        approval_deadline = required_claims.get("approval_expires_by")
        if approval_deadline is not None:
            try:
                deadline = datetime.fromisoformat(approval_deadline)
            except (TypeError, ValueError) as exc:
                raise DeploymentAuthorizationError("Approval request expiry is invalid.") from exc
            if authorization.expires_at > deadline:
                raise DeploymentAuthorizationError(
                    "Authorization expiry exceeds the signed request's validity window."
                )
        if authorization.action != required_claims.get("action"):
            raise DeploymentAuthorizationError("Authorization action does not match the request.")
        if authorization.claims != required_claims:
            raise DeploymentAuthorizationError("Authorization claims do not exactly match the request.")
        verifier = self._authorization_verifier
        if verifier is None:
            raise DeploymentAuthorizationError(
                "No trusted deployment authorization verifier is configured."
            )

    def _consume_authorization(
        self,
        authorization: Optional[DeploymentAuthorization],
        required_claims: Dict[str, Any],
    ) -> bool:
        """Cryptographically verify and consume a token at the mutation boundary."""
        verifier = self._authorization_verifier
        if authorization is None or verifier is None:
            raise DeploymentAuthorizationError("Trusted deployment authorization is unavailable.")
        try:
            accepted = verifier.verify_and_consume(authorization, required_claims)
        except Exception as exc:
            raise DeploymentAuthorizationError(
                f"Authorization verification failed: {type(exc).__name__}."
            ) from exc
        if not accepted:
            raise DeploymentAuthorizationError("Trusted verifier rejected the authorization.")
        return True

    def _record_failed_authorized_attempt(
        self,
        deployment_id: str,
        event_type: str,
        authorization: Optional[DeploymentAuthorization],
        authorization_consumed: bool,
        error: Exception,
    ) -> None:
        """Keep failed control-plane attempts in the hash-chained audit ledger."""
        try:
            now = datetime.now(timezone.utc)
            with self._lock, closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._append_event(
                    connection,
                    deployment_id,
                    event_type,
                    {
                        "authorization_id": (
                            authorization.authorization_id if authorization is not None else None
                        ),
                        "authorization_consumed": authorization_consumed,
                        "failure_type": type(error).__name__,
                    },
                    now,
                )
                connection.commit()
        except Exception:
            log.exception("failed to persist deployment control attempt: id=%s type=%s",
                          deployment_id, event_type)

    @staticmethod
    def _authorization_event(authorization: DeploymentAuthorization) -> Dict[str, str]:
        return {
            "authorization_id": authorization.authorization_id,
            "issuer": authorization.issuer,
            "authorized_by": authorization.authorized_by,
            "signature_digest": hashlib.sha256(
                authorization.signature.encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _approval_deadline(value: Optional[str] = None) -> datetime:
        now = datetime.now(timezone.utc)
        if value is None:
            return now + timedelta(minutes=5)
        try:
            deadline = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise DeploymentAuthorizationError("Approval request expiry is invalid.") from exc
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise DeploymentAuthorizationError("Approval request expiry must be timezone-aware.")
        if deadline <= now or deadline > now + timedelta(minutes=5):
            raise DeploymentAuthorizationError("Approval request is expired or exceeds the five-minute limit.")
        return deadline

    def _load_record(self, connection: sqlite3.Connection, deployment_id: str):
        row = connection.execute(
            "SELECT record_json FROM deployments WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchone()
        return self._record_from_json(row["record_json"]) if row else None

    @staticmethod
    def _validate_benchmark(model: Dict[str, Any], report: BenchmarkReport) -> str:
        if report.status != BenchmarkStatus.SUCCESS or not report.reference_available:
            raise DeploymentError("A successful benchmark with an available reference is required.")
        if not report.comparisons:
            raise DeploymentError("Benchmark contains no metric comparisons.")
        if report.metrics_incomparable or report.metrics_worse:
            raise DeploymentError("Benchmark contains incomparable or degraded metrics.")
        source_experiment_id = model.get("source_experiment_id") or model.get("experiment_id")
        if report.experiment_id != source_experiment_id:
            raise DeploymentError("Benchmark experiment does not match the registered model.")
        if model.get("source_run_id") and report.run_id != model["source_run_id"]:
            raise DeploymentError("Benchmark run does not match the model's source run.")
        if model.get("source_result_id") and report.result_id != model["source_result_id"]:
            raise DeploymentError("Benchmark result does not match the model's source result.")
        return DeploymentController._digest(report.model_dump(mode="json"))

    def prepare_rollout_authorization(
        self,
        model_id: str,
        benchmark_report: BenchmarkReport,
        deployment_id: Optional[str] = None,
        approval_expires_by: Optional[str] = None,
    ) -> DeploymentAuthorizationRequest:
        """Return exact signed claims for Aegis before changing deployment state."""
        model = self._registry.get_model(model_id)
        if model is None:
            raise DeploymentError(f"Model '{model_id}' is not registered.")
        if model.get("status") != "candidate":
            raise DeploymentError("Only candidate models may enter staged deployment.")
        if not model.get("artifact_sha256") or not self._registry.verify_artifact(model_id):
            raise DeploymentError("Model has no integrity-verified deployable artifact.")
        benchmark_digest = self._validate_benchmark(model, benchmark_report)
        domain = model["domain"]
        deployment_id = deployment_id or str(uuid.uuid4())
        with closing(self._connect()) as connection:
            current = self._read_active(connection, domain)
        deadline = self._approval_deadline(approval_expires_by)
        claims = {
            "action": "start_rollout",
            "deployment_id": deployment_id,
            "domain": domain,
            "model_id": model_id,
            "artifact_digest": model["artifact_sha256"],
            "benchmark_digest": benchmark_digest,
            "previous_model_id": current["model_id"],
            "previous_deployment_id": current["deployment_id"],
            "expected_generation": current["generation"],
            "canary_percent": self._canary_percent,
            "approval_expires_by": deadline.isoformat(),
        }
        return DeploymentAuthorizationRequest(
            request_id=deployment_id,
            action="start_rollout",
            claims=claims,
            expires_at=deadline,
        )

    def start_rollout(
        self,
        model_id: str,
        benchmark_report: BenchmarkReport,
        authorization: Optional[DeploymentAuthorization],
        deployment_id: Optional[str] = None,
    ) -> DeploymentRecord:
        if deployment_id is None and authorization is not None:
            deployment_id = authorization.claims.get("deployment_id")
        approval_expires_by = (
            authorization.claims.get("approval_expires_by")
            if authorization is not None else None
        )
        prepared = self.prepare_rollout_authorization(
            model_id=model_id,
            benchmark_report=benchmark_report,
            deployment_id=deployment_id,
            approval_expires_by=approval_expires_by,
        )
        claims = prepared.claims
        try:
            self._verify_authorization(authorization, claims)
        except Exception as exc:
            self._record_failed_authorized_attempt(
                claims["deployment_id"], "rollout_start_authorization_rejected",
                authorization, False, exc,
            )
            raise
        model = self._registry.get_model(model_id)
        assert model is not None
        benchmark_digest = claims["benchmark_digest"]
        domain = model["domain"]
        deployment_id = claims["deployment_id"]
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            current = self._read_active(connection, domain)
        if (
            current["generation"] != claims["expected_generation"]
            or current["model_id"] != claims["previous_model_id"]
            or current["deployment_id"] != claims["previous_deployment_id"]
        ):
            raise DeploymentConflict("Active production state changed after rollout approval was prepared.")
        authorization_consumed = False
        record = DeploymentRecord(
            deployment_id=deployment_id,
            domain=domain,
            model_id=model_id,
            previous_model_id=current["model_id"],
            previous_deployment_id=current["deployment_id"],
            phase=DeploymentPhase.SHADOW,
            status=DeploymentStatus.IN_PROGRESS,
            canary_percent=self._canary_percent,
            benchmark_digest=benchmark_digest,
            artifact_digest=model["artifact_sha256"],
            expected_generation=current["generation"],
            started_at=now,
            updated_at=now,
        )
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                active_now = self._read_active(connection, domain)
                if active_now != current:
                    raise DeploymentConflict("Active model changed while rollout was authorized.")
                existing_rollout = connection.execute(
                    "SELECT deployment_id FROM deployments WHERE domain = ? AND status = ? LIMIT 1",
                    (domain, DeploymentStatus.IN_PROGRESS.value),
                ).fetchone()
                if existing_rollout is not None:
                    raise DeploymentConflict(
                        "A rollout is already in progress for this domain."
                    )
                authorization_consumed = self._consume_authorization(authorization, claims)
                connection.execute(
                    "INSERT INTO deployments "
                    "(deployment_id, domain, model_id, phase, status, record_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (deployment_id, domain, model_id, record.phase.value,
                     record.status.value, record.model_dump_json()),
                )
                self._append_event(
                    connection, deployment_id, "rollout_started",
                    {"domain": domain, "model_id": model_id,
                     "benchmark_digest": benchmark_digest,
                     **self._authorization_event(authorization)}, now,
                )
                connection.commit()
                log.info(
                    "deployment rollout started: id=%s domain=%s model=%s benchmark=%s",
                    deployment_id, domain, model_id, benchmark_digest,
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                self._record_failed_authorized_attempt(
                    deployment_id, "rollout_start_failed", authorization,
                    authorization_consumed, exc,
                )
                raise DeploymentConflict(
                    "An in-progress rollout already exists for this domain."
                ) from exc
            except Exception as exc:
                connection.rollback()
                self._record_failed_authorized_attempt(
                    deployment_id, "rollout_start_failed", authorization,
                    authorization_consumed, exc,
                )
                raise
        return record

    def prepare_advance_authorization(
        self,
        deployment_id: str,
    ) -> DeploymentAuthorizationRequest:
        """Run the trusted health gate and persist the exact transition to sign."""
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            record = self._load_record(connection, deployment_id)
            if record is None:
                raise DeploymentError(f"Deployment '{deployment_id}' was not found.")
            pending = connection.execute(
                "SELECT request_id, claims_json, expires_at FROM pending_transitions "
                "WHERE deployment_id = ? AND status = 'pending'",
                (deployment_id,),
            ).fetchone()
        if record.status != DeploymentStatus.IN_PROGRESS:
            raise DeploymentConflict("Only an in-progress rollout can advance.")
        if pending is not None and datetime.fromisoformat(pending["expires_at"]) > now:
            return DeploymentAuthorizationRequest(
                request_id=pending["request_id"],
                action="advance_rollout",
                claims=json.loads(pending["claims_json"]),
                expires_at=datetime.fromisoformat(pending["expires_at"]),
            )
        if record.phase == DeploymentPhase.SHADOW:
            target_phase = DeploymentPhase.CANARY
        elif record.phase == DeploymentPhase.CANARY:
            target_phase = DeploymentPhase.PRODUCTION
        else:
            raise DeploymentConflict(f"No forward transition is valid from '{record.phase.value}'.")
        if self._health_gate is None:
            raise DeploymentError("No trusted deployment health gate is configured.")
        supplied_assessment = self._health_gate.assess(record, target_phase)
        if not isinstance(supplied_assessment, HealthAssessment):
            raise DeploymentError("Deployment health gate must return a HealthAssessment instance.")
        # Pydantic's frozen setting is shallow. Revalidate a copied payload so
        # the observer cannot mutate evidence after it has been digest-bound.
        assessment = HealthAssessment.model_validate(
            supplied_assessment.model_dump(mode="json")
        )
        if (
            not assessment.assessment_id
            or not assessment.observer
            or assessment.deployment_id != record.deployment_id
            or assessment.model_id != record.model_id
            or assessment.target_phase != target_phase
        ):
            raise DeploymentError("Health assessment is not bound to this deployment transition.")
        now = datetime.now(timezone.utc)
        if assessment.assessed_at > now or now - assessment.assessed_at > timedelta(minutes=5):
            raise DeploymentError("Health assessment is future-dated or older than five minutes.")
        health_digest = self._digest(assessment.model_dump(mode="json"))
        if not assessment.passed:
            self._record_health_failure(record, assessment, health_digest)
            raise DeploymentError("Health gate rejected the rollout; deployment marked failed.")
        with closing(self._connect()) as connection:
            active = self._read_active(connection, record.domain)
        if (
            active["generation"] != record.expected_generation
            or active["model_id"] != record.previous_model_id
            or active["deployment_id"] != record.previous_deployment_id
        ):
            raise DeploymentConflict(
                "Active production state changed since this rollout began; "
                "rollback or reconcile the in-progress rollout before advancing."
            )
        prepared_at = datetime.now(timezone.utc)
        expires_at = prepared_at + timedelta(minutes=5)
        request_id = str(uuid.uuid4())
        claims = {
            "action": "advance_rollout",
            "deployment_id": record.deployment_id,
            "domain": record.domain,
            "model_id": record.model_id,
            "from_phase": record.phase.value,
            "to_phase": target_phase.value,
            "expected_generation": active["generation"],
            "previous_model_id": active["model_id"],
            "previous_deployment_id": active["deployment_id"],
            "benchmark_digest": record.benchmark_digest,
            "health_digest": health_digest,
            "approval_expires_by": expires_at.isoformat(),
        }
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._load_record(connection, deployment_id)
                active_now = self._read_active(connection, record.domain)
                if current is None or current.status != DeploymentStatus.IN_PROGRESS:
                    raise DeploymentConflict("Rollout changed during health assessment.")
                if current.phase != record.phase or active_now != active:
                    raise DeploymentConflict("Rollout stage or active generation changed during health assessment.")
                if pending is not None:
                    connection.execute(
                        "UPDATE pending_transitions SET status = 'expired' "
                        "WHERE request_id = ? AND status = 'pending'",
                        (pending["request_id"],),
                    )
                connection.execute(
                    "INSERT INTO pending_transitions "
                    "(request_id, deployment_id, from_phase, to_phase, claims_json, "
                    "health_json, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (request_id, deployment_id, record.phase.value, target_phase.value,
                     self._canonical_json(claims), assessment.model_dump_json(),
                     expires_at.isoformat()),
                )
                self._append_event(
                    connection, deployment_id, "transition_authorization_prepared",
                    {"request_id": request_id, "to_phase": target_phase.value,
                     "health_assessment_id": assessment.assessment_id,
                     "health_digest": health_digest}, prepared_at,
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DeploymentConflict("Another transition authorization is pending.") from exc
            except Exception:
                connection.rollback()
                raise
        return DeploymentAuthorizationRequest(
            request_id=request_id,
            action="advance_rollout",
            claims=claims,
            expires_at=expires_at,
        )

    def advance(
        self,
        deployment_id: str,
        request_id: str,
        authorization: Optional[DeploymentAuthorization],
    ) -> DeploymentRecord:
        with closing(self._connect()) as connection:
            pending = connection.execute(
                "SELECT * FROM pending_transitions WHERE request_id = ? "
                "AND deployment_id = ? AND status = 'pending'",
                (request_id, deployment_id),
            ).fetchone()
            record = self._load_record(connection, deployment_id)
        if record is None or pending is None:
            raise DeploymentConflict("No pending authorization request exists for this transition.")
        expires_at = datetime.fromisoformat(pending["expires_at"])
        if expires_at <= datetime.now(timezone.utc):
            self._expire_pending_transition(
                deployment_id, request_id, "transition_authorization_expired",
                {"reason": "authorization_window_expired"},
            )
            raise DeploymentAuthorizationError("The prepared transition authorization has expired.")
        claims = json.loads(pending["claims_json"])
        assessment = HealthAssessment.model_validate_json(pending["health_json"])
        if (
            pending["from_phase"] != claims.get("from_phase")
            or pending["to_phase"] != claims.get("to_phase")
            or claims.get("deployment_id") != deployment_id
            or assessment.deployment_id != deployment_id
            or assessment.model_id != claims.get("model_id")
            or assessment.target_phase.value != claims.get("to_phase")
        ):
            raise DeploymentError("Persisted transition fields do not match the signed claims.")
        health_digest = self._digest(assessment.model_dump(mode="json"))
        if health_digest != claims.get("health_digest"):
            raise DeploymentError("Persisted health evidence does not match the signed request.")
        now = datetime.now(timezone.utc)
        if not self._health_assessment_is_fresh(assessment, now):
            self._expire_pending_transition(
                deployment_id, request_id, "transition_health_evidence_expired",
                {"assessment_id": assessment.assessment_id, "health_digest": health_digest},
            )
            raise DeploymentAuthorizationError(
                "The health assessment is older than five minutes; prepare a new transition request."
            )
        with closing(self._connect()) as connection:
            active_before_authorization = self._read_active(connection, record.domain)
        if (
            record.status != DeploymentStatus.IN_PROGRESS
            or record.deployment_id != deployment_id
            or record.domain != claims.get("domain")
            or record.model_id != claims.get("model_id")
            or record.phase.value != claims.get("from_phase")
            or record.benchmark_digest != claims.get("benchmark_digest")
            or record.expected_generation != claims.get("expected_generation")
            or active_before_authorization["generation"] != claims.get("expected_generation")
            or active_before_authorization["model_id"] != claims.get("previous_model_id")
            or active_before_authorization["deployment_id"] != claims.get("previous_deployment_id")
        ):
            raise DeploymentConflict(
                "Rollout or active production state changed; prepare a fresh transition request."
            )
        try:
            self._verify_authorization(authorization, claims)
        except Exception as exc:
            self._record_failed_authorized_attempt(
                deployment_id, "rollout_transition_authorization_rejected",
                authorization, False, exc,
            )
            raise
        now = datetime.now(timezone.utc)
        if not self._health_assessment_is_fresh(assessment, now):
            self._expire_pending_transition(
                deployment_id, request_id, "transition_health_evidence_expired",
                {"assessment_id": assessment.assessment_id, "health_digest": health_digest},
            )
            raise DeploymentAuthorizationError(
                "The health assessment expired during authorization verification; prepare a new request."
            )
        target_phase = DeploymentPhase(pending["to_phase"])
        authorization_consumed = False
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc)
                if not self._health_assessment_is_fresh(assessment, now):
                    connection.execute(
                        "UPDATE pending_transitions SET status = 'expired' "
                        "WHERE request_id = ? AND deployment_id = ? AND status = 'pending'",
                        (request_id, deployment_id),
                    )
                    self._append_event(
                        connection, deployment_id, "transition_health_evidence_expired",
                        {"request_id": request_id,
                         "assessment_id": assessment.assessment_id,
                         "health_digest": health_digest}, now,
                    )
                    connection.commit()
                    raise DeploymentAuthorizationError(
                        "The health assessment expired before transition commit; prepare a new request."
                    )
                pending_now = connection.execute(
                    "SELECT * FROM pending_transitions WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                current = self._load_record(connection, deployment_id)
                active_now = self._read_active(connection, record.domain)
                if (
                    pending_now is None or pending_now["status"] != "pending"
                    or pending_now["deployment_id"] != deployment_id
                    or pending_now["from_phase"] != pending["from_phase"]
                    or pending_now["to_phase"] != pending["to_phase"]
                    or pending_now["claims_json"] != pending["claims_json"]
                    or pending_now["health_json"] != pending["health_json"]
                    or pending_now["expires_at"] != pending["expires_at"]
                    or current is None or current.status != DeploymentStatus.IN_PROGRESS
                    or current.phase.value != claims["from_phase"]
                    or current.deployment_id != deployment_id
                    or current.domain != claims.get("domain")
                    or current.model_id != claims.get("model_id")
                    or current.benchmark_digest != claims.get("benchmark_digest")
                    or current.expected_generation != claims.get("expected_generation")
                ):
                    raise DeploymentConflict("Rollout or pending authorization changed before commit.")
                if datetime.fromisoformat(pending_now["expires_at"]) <= now:
                    connection.execute(
                        "UPDATE pending_transitions SET status = 'expired' "
                        "WHERE request_id = ? AND status = 'pending'", (request_id,)
                    )
                    self._append_event(
                        connection, deployment_id, "transition_authorization_expired",
                        {"request_id": request_id,
                         "reason": "authorization_window_expired_during_commit"}, now,
                    )
                    connection.commit()
                    raise DeploymentAuthorizationError(
                        "The transition authorization expired before commit."
                    )
                if (
                    active_now["generation"] != claims["expected_generation"]
                    or active_now["model_id"] != claims["previous_model_id"]
                    or active_now["deployment_id"] != claims["previous_deployment_id"]
                ):
                    raise DeploymentConflict("Active production state changed before transition commit.")
                authorization_consumed = self._consume_authorization(authorization, claims)
                production_generation = current.production_generation
                status = DeploymentStatus.IN_PROGRESS
                if target_phase == DeploymentPhase.PRODUCTION:
                    production_generation = active_now["generation"] + 1
                    status = DeploymentStatus.COMPLETED
                    connection.execute(
                        "INSERT INTO active_models (domain, model_id, deployment_id, generation) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(domain) DO UPDATE SET "
                        "model_id = excluded.model_id, deployment_id = excluded.deployment_id, "
                        "generation = excluded.generation",
                        (record.domain, record.model_id, record.deployment_id, production_generation),
                    )
                updated = current.model_copy(update={
                    "phase": target_phase,
                    "status": status,
                    "production_generation": production_generation,
                    "updated_at": now,
                    "last_health_assessment_id": assessment.assessment_id,
                })
                connection.execute(
                    "UPDATE deployments SET phase = ?, status = ?, record_json = ? "
                    "WHERE deployment_id = ?",
                    (updated.phase.value, updated.status.value, updated.model_dump_json(), deployment_id),
                )
                connection.execute(
                    "UPDATE pending_transitions SET status = 'consumed' WHERE request_id = ?",
                    (request_id,),
                )
                self._append_event(
                    connection, deployment_id, "rollout_advanced",
                    {"from_phase": current.phase.value, "to_phase": target_phase.value,
                     "health_assessment_id": assessment.assessment_id,
                     "health_digest": health_digest,
                     **self._authorization_event(authorization),
                     "active_generation": production_generation or active_now["generation"]},
                    now,
                )
                connection.commit()
                log.info(
                    "deployment transition authorized: id=%s phase=%s->%s health=%s",
                    deployment_id, record.phase.value, target_phase.value,
                    assessment.assessment_id,
                )
                return updated
            except Exception as exc:
                connection.rollback()
                self._record_failed_authorized_attempt(
                    deployment_id, "rollout_transition_failed", authorization,
                    authorization_consumed, exc,
                )
                raise

    @staticmethod
    def _health_assessment_is_fresh(
        assessment: HealthAssessment, now: datetime
    ) -> bool:
        assessed_at = assessment.assessed_at.astimezone(timezone.utc)
        current = now.astimezone(timezone.utc)
        return assessed_at <= current and current - assessed_at <= timedelta(minutes=5)

    def _expire_pending_transition(
        self,
        deployment_id: str,
        request_id: str,
        event_type: str,
        details: Dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                update = connection.execute(
                    "UPDATE pending_transitions SET status = 'expired' "
                    "WHERE request_id = ? AND deployment_id = ? AND status = 'pending'",
                    (request_id, deployment_id),
                )
                if update.rowcount:
                    self._append_event(
                        connection, deployment_id, event_type,
                        {"request_id": request_id, **details}, now,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _record_health_failure(
        self,
        record: DeploymentRecord,
        assessment: HealthAssessment,
        health_digest: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_record(connection, record.deployment_id)
            if current is None or current.status != DeploymentStatus.IN_PROGRESS or current.phase != record.phase:
                connection.rollback()
                raise DeploymentConflict("Rollout changed before health failure could be recorded.")
            updated = current.model_copy(update={
                "status": DeploymentStatus.FAILED,
                "updated_at": now,
                "last_health_assessment_id": assessment.assessment_id,
                "failure_reason": "health_gate_rejected",
            })
            connection.execute(
                "UPDATE deployments SET status = ?, record_json = ? WHERE deployment_id = ?",
                (updated.status.value, updated.model_dump_json(), record.deployment_id),
            )
            self._append_event(
                connection, record.deployment_id, "health_gate_rejected",
                {"assessment_id": assessment.assessment_id, "health_digest": health_digest}, now,
            )
            connection.commit()
        log.warning(
            "deployment health gate rejected rollout: id=%s phase=%s assessment=%s",
            record.deployment_id, record.phase.value, assessment.assessment_id,
        )

    def prepare_rollback_authorization(
        self,
        deployment_id: str,
        reason: str,
        approval_expires_by: Optional[str] = None,
    ) -> DeploymentAuthorizationRequest:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rollback requires a non-empty reason")
        with closing(self._connect()) as connection:
            record = self._load_record(connection, deployment_id)
            if record is None:
                raise DeploymentError(f"Deployment '{deployment_id}' was not found.")
            active = self._read_active(connection, record.domain)
        if record.status not in (DeploymentStatus.IN_PROGRESS, DeploymentStatus.COMPLETED):
            raise DeploymentConflict("Only an active or completed rollout can be rolled back.")
        is_live_production = active["deployment_id"] == deployment_id
        if record.status == DeploymentStatus.COMPLETED and not is_live_production:
            raise DeploymentConflict(
                "This completed rollout is no longer active; refusing to roll back over a newer deployment."
            )
        if is_live_production and active["generation"] != record.production_generation:
            raise DeploymentConflict("A newer deployment is active; refusing stale rollback.")
        expires_at = self._approval_deadline(approval_expires_by)
        claims = {
            "action": "rollback_deployment",
            "deployment_id": deployment_id,
            "domain": record.domain,
            "model_id": record.model_id,
            "deployment_status": record.status.value,
            "deployment_phase": record.phase.value,
            "reason": reason.strip(),
            "expected_generation": active["generation"],
            "expected_active_model_id": active["model_id"],
            "expected_active_deployment_id": active["deployment_id"],
            "restore_model_id": record.previous_model_id if is_live_production else active["model_id"],
            "benchmark_digest": record.benchmark_digest,
            "approval_expires_by": expires_at.isoformat(),
        }
        return DeploymentAuthorizationRequest(
            request_id=deployment_id,
            action="rollback_deployment",
            claims=claims,
            expires_at=expires_at,
        )

    def rollback(
        self,
        deployment_id: str,
        reason: str,
        authorization: Optional[DeploymentAuthorization],
    ) -> DeploymentRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rollback requires a non-empty reason")
        with closing(self._connect()) as connection:
            record = self._load_record(connection, deployment_id)
            if record is None:
                raise DeploymentError(f"Deployment '{deployment_id}' was not found.")
        if record.status == DeploymentStatus.ROLLED_BACK:
            return record
        approval_expires_by = (
            authorization.claims.get("approval_expires_by")
            if authorization is not None else None
        )
        prepared = self.prepare_rollback_authorization(
            deployment_id, reason, approval_expires_by=approval_expires_by
        )
        with closing(self._connect()) as connection:
            current_before_authorization = self._load_record(connection, deployment_id)
            active = self._read_active(connection, record.domain)
        if (
            current_before_authorization != record
            or record.status.value != prepared.claims.get("deployment_status")
            or record.phase.value != prepared.claims.get("deployment_phase")
            or active["generation"] != prepared.claims.get("expected_generation")
            or active["model_id"] != prepared.claims.get("expected_active_model_id")
            or active["deployment_id"] != prepared.claims.get("expected_active_deployment_id")
        ):
            raise DeploymentConflict(
                "Rollout or active production state changed; prepare a fresh rollback request."
            )
        try:
            self._verify_authorization(authorization, prepared.claims)
        except Exception as exc:
            self._record_failed_authorized_attempt(
                deployment_id, "rollout_rollback_authorization_rejected",
                authorization, False, exc,
            )
            raise
        is_live_production = active["deployment_id"] == deployment_id
        now = datetime.now(timezone.utc)
        authorization_consumed = False
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._load_record(connection, deployment_id)
                active_now = self._read_active(connection, record.domain)
                if current is None or current.status not in (
                    DeploymentStatus.IN_PROGRESS, DeploymentStatus.COMPLETED
                ):
                    raise DeploymentConflict("Rollout changed before rollback committed.")
                if (
                    current.status.value != prepared.claims.get("deployment_status")
                    or current.phase.value != prepared.claims.get("deployment_phase")
                    or current.model_id != prepared.claims.get("model_id")
                ):
                    raise DeploymentConflict("Rollout stage changed before rollback committed.")
                if active_now != active:
                    raise DeploymentConflict("Active generation changed before rollback committed.")
                authorization_consumed = self._consume_authorization(
                    authorization, prepared.claims
                )
                production_generation = current.production_generation
                if is_live_production:
                    production_generation = active_now["generation"] + 1
                    connection.execute(
                        "INSERT INTO active_models (domain, model_id, deployment_id, generation) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(domain) DO UPDATE SET "
                        "model_id = excluded.model_id, deployment_id = excluded.deployment_id, "
                        "generation = excluded.generation",
                        (record.domain, record.previous_model_id,
                         record.previous_deployment_id, production_generation),
                    )
                updated = current.model_copy(update={
                    "phase": DeploymentPhase.ROLLED_BACK,
                    "status": DeploymentStatus.ROLLED_BACK,
                    "production_generation": production_generation,
                    "updated_at": now,
                    "failure_reason": reason.strip(),
                })
                connection.execute(
                    "UPDATE deployments SET phase = ?, status = ?, record_json = ? "
                    "WHERE deployment_id = ?",
                    (updated.phase.value, updated.status.value,
                     updated.model_dump_json(), deployment_id),
                )
                self._append_event(
                    connection, deployment_id, "deployment_rolled_back",
                    {"reason": reason.strip(), "was_live_production": is_live_production,
                     "restored_model_id": record.previous_model_id if is_live_production else active["model_id"],
                     **self._authorization_event(authorization)}, now,
                )
                connection.commit()
                log.warning(
                    "deployment rolled back: id=%s domain=%s live=%s reason=%s",
                    deployment_id, record.domain, is_live_production, reason.strip(),
                )
                return updated
            except Exception as exc:
                connection.rollback()
                self._record_failed_authorized_attempt(
                    deployment_id, "rollout_rollback_failed", authorization,
                    authorization_consumed, exc,
                )
                raise

    def route(self, domain: str, request_key: str) -> TrafficRoute:
        """Return deterministic primary/shadow routing for a serving adapter."""
        if not request_key:
            raise ValueError("request_key is required for deterministic canary assignment")
        with closing(self._connect()) as connection:
            active = self._read_active(connection, domain)
            rows = connection.execute(
                "SELECT record_json FROM deployments WHERE domain = ? AND status = ? "
                "ORDER BY started_at DESC",
                (domain, DeploymentStatus.IN_PROGRESS.value),
            ).fetchall()
        if len(rows) > 1:
            raise DeploymentConflict(
                "Multiple in-progress rollouts exist for this domain; routing is paused."
            )
        if not rows:
            return TrafficRoute(domain=domain, primary_model_id=active["model_id"])
        rollout = self._record_from_json(rows[0]["record_json"])
        if rollout.phase == DeploymentPhase.SHADOW:
            return TrafficRoute(
                domain=domain,
                primary_model_id=active["model_id"],
                shadow_model_id=rollout.model_id,
                deployment_id=rollout.deployment_id,
                phase=rollout.phase,
            )
        if rollout.phase == DeploymentPhase.CANARY:
            bucket = int(hashlib.sha256(
                f"{rollout.deployment_id}:{request_key}".encode("utf-8")
            ).hexdigest()[:8], 16) % 100
            primary = (
                rollout.model_id
                if bucket < rollout.canary_percent or active["model_id"] is None
                else active["model_id"]
            )
            return TrafficRoute(
                domain=domain,
                primary_model_id=primary,
                deployment_id=rollout.deployment_id,
                phase=rollout.phase,
                canary_percent=rollout.canary_percent,
            )
        return TrafficRoute(domain=domain, primary_model_id=active["model_id"])

    def get_deployment(self, deployment_id: str) -> Optional[DeploymentRecord]:
        with closing(self._connect()) as connection:
            return self._load_record(connection, deployment_id)

    def active_model(self, domain: str) -> Optional[str]:
        with closing(self._connect()) as connection:
            return self._read_active(connection, domain)["model_id"]

    def approved_artifact_digest(self, domain: str, model_id: str) -> Optional[str]:
        """Return the digest bound to the deployment that currently authorizes this model."""
        with closing(self._connect()) as connection:
            active = self._read_active(connection, domain)
            if active["model_id"] == model_id and active["deployment_id"]:
                record = self._load_record(connection, active["deployment_id"])
                if (
                    record is not None
                    and record.domain == domain
                    and record.model_id == model_id
                    and record.status == DeploymentStatus.COMPLETED
                    and record.phase == DeploymentPhase.PRODUCTION
                ):
                    return record.artifact_digest

            rows = connection.execute(
                "SELECT record_json FROM deployments WHERE domain = ? AND model_id = ? "
                "AND status = ? ORDER BY started_at DESC",
                (domain, model_id, DeploymentStatus.IN_PROGRESS.value),
            ).fetchall()
            if len(rows) > 1:
                return None
            if not rows:
                return None
            record = self._record_from_json(rows[0]["record_json"])
            if (
                record.domain != domain
                or record.model_id != model_id
                or record.phase not in (DeploymentPhase.SHADOW, DeploymentPhase.CANARY)
            ):
                return None
            return record.artifact_digest

    def stats(self) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            deployment_rows = connection.execute(
                "SELECT status, phase, COUNT(*) AS count FROM deployments "
                "GROUP BY status, phase"
            ).fetchall()
            active_domains = connection.execute(
                "SELECT COUNT(*) FROM active_models WHERE model_id IS NOT NULL"
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM deployment_events"
            ).fetchone()[0]
        grouped: Dict[str, Dict[str, int]] = {}
        for row in deployment_rows:
            grouped.setdefault(row["status"], {})[row["phase"]] = row["count"]
        return {
            "deployments_by_status_and_phase": grouped,
            "active_domains": active_domains,
            "audit_events": event_count,
            "audit_integrity": self.verify_audit_chain(),
            "state_consistency": self.verify_state_consistency(),
        }

    def operational_status(self) -> Dict[str, Any]:
        try:
            with closing(self._connect()) as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            audit = self.verify_audit_chain()
            state = self.verify_state_consistency()
            authorization_configured = self._authorization_verifier is not None
            health_gate_configured = self._health_gate is not None
            health_gate_ready = False
            health_gate_error = None
            if self._health_gate is not None:
                status_method = getattr(self._health_gate, "operational_status", None)
                if not callable(status_method):
                    health_gate_error = "health_status_method_missing"
                else:
                    try:
                        observer_status = status_method()
                        health_gate_ready = (
                            isinstance(observer_status, dict)
                            and observer_status.get("ready") is True
                        )
                        if not health_gate_ready:
                            health_gate_error = "observer_not_ready"
                    except Exception as exc:
                        health_gate_error = type(exc).__name__
            ready = (
                integrity == "ok"
                and audit.get("intact") is True
                and state.get("intact") is True
                and authorization_configured
                and health_gate_configured
                and health_gate_ready
            )
            result = {
                "ready": ready,
                "database_integrity": integrity == "ok",
                "audit_integrity": audit.get("intact") is True,
                "state_consistency": state.get("intact") is True,
                "authorization_verifier_configured": authorization_configured,
                "health_gate_configured": health_gate_configured,
                "health_gate_ready": health_gate_ready,
            }
            if health_gate_error:
                result["health_gate_error"] = health_gate_error
            return result
        except Exception as exc:
            log.exception("deployment controller operational status check failed")
            return {
                "ready": False,
                "database_integrity": False,
                "audit_integrity": False,
                "state_consistency": False,
                "authorization_verifier_configured": self._authorization_verifier is not None,
                "health_gate_configured": self._health_gate is not None,
                "health_gate_ready": False,
                "error": type(exc).__name__,
            }

    def verify_audit_chain(self) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT sequence, event_id, deployment_id, event_json, previous_hash, event_hash "
                "FROM deployment_events ORDER BY sequence"
            ).fetchall()
        previous_hash = "0" * 64
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return {"intact": False, "broken_at_sequence": row["sequence"]}
            try:
                event = json.loads(row["event_json"])
                if (
                    not isinstance(event, dict)
                    or event.get("event_id") != row["event_id"]
                    or event.get("deployment_id") != row["deployment_id"]
                ):
                    return {"intact": False, "broken_at_sequence": row["sequence"]}
                event_hash = hashlib.sha256(
                    (previous_hash + self._canonical_json(event)).encode("utf-8")
                ).hexdigest()
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"intact": False, "broken_at_sequence": row["sequence"]}
            if event_hash != row["event_hash"]:
                return {"intact": False, "broken_at_sequence": row["sequence"]}
            previous_hash = event_hash
        with closing(self._connect()) as connection:
            stored_head = connection.execute(
                "SELECT value FROM deployment_metadata WHERE key = 'audit_head_hash'"
            ).fetchone()
        if rows and stored_head is None:
            return {"intact": False, "broken_at_sequence": "audit_head_missing"}
        if stored_head is not None and stored_head["value"] != previous_hash:
            return {"intact": False, "broken_at_sequence": "audit_head"}
        return {"intact": True, "events": len(rows), "head_hash": previous_hash}

    def verify_state_consistency(self) -> Dict[str, Any]:
        """Cross-check deployment JSON records, indexed columns, and active pointers."""
        try:
            with closing(self._connect()) as connection:
                deployment_rows = connection.execute(
                    "SELECT deployment_id, domain, model_id, phase, status, record_json "
                    "FROM deployments ORDER BY deployment_id"
                ).fetchall()
                active_rows = connection.execute(
                    "SELECT domain, model_id, deployment_id, generation "
                    "FROM active_models ORDER BY domain"
                ).fetchall()
                pending_rows = connection.execute(
                    "SELECT request_id, deployment_id, from_phase, to_phase, "
                    "claims_json, health_json, expires_at, status "
                    "FROM pending_transitions WHERE status = 'pending' "
                    "ORDER BY request_id"
                ).fetchall()

            records: Dict[str, DeploymentRecord] = {}
            for row in deployment_rows:
                try:
                    record = self._record_from_json(row["record_json"])
                except Exception:
                    return {
                        "intact": False,
                        "issue": "deployment_record_invalid",
                        "deployment_id": row["deployment_id"],
                    }
                if (
                    record.deployment_id != row["deployment_id"]
                    or record.domain != row["domain"]
                    or record.model_id != row["model_id"]
                    or record.phase.value != row["phase"]
                    or record.status.value != row["status"]
                ):
                    return {
                        "intact": False,
                        "issue": "deployment_record_index_mismatch",
                        "deployment_id": row["deployment_id"],
                    }
                if (
                    record.status == DeploymentStatus.IN_PROGRESS
                    and record.phase not in (DeploymentPhase.SHADOW, DeploymentPhase.CANARY)
                ) or (
                    record.status == DeploymentStatus.COMPLETED
                    and (
                        record.phase != DeploymentPhase.PRODUCTION
                        or record.production_generation is None
                    )
                ) or (
                    record.status == DeploymentStatus.ROLLED_BACK
                    and record.phase != DeploymentPhase.ROLLED_BACK
                ) or (
                    record.status == DeploymentStatus.FAILED
                    and record.phase not in (DeploymentPhase.SHADOW, DeploymentPhase.CANARY)
                ):
                    return {
                        "intact": False,
                        "issue": "deployment_lifecycle_invariant_failed",
                        "deployment_id": row["deployment_id"],
                    }
                records[record.deployment_id] = record

            for row in active_rows:
                domain = row["domain"]
                model_id = row["model_id"]
                deployment_id = row["deployment_id"]
                generation = row["generation"]
                if (
                    not isinstance(generation, int)
                    or isinstance(generation, bool)
                    or generation < 1
                    or (model_id is None) != (deployment_id is None)
                ):
                    return {
                        "intact": False,
                        "issue": "active_model_pointer_invalid",
                        "domain": domain,
                    }
                if model_id is None:
                    continue
                record = records.get(deployment_id)
                if (
                    record is None
                    or record.domain != domain
                    or record.model_id != model_id
                    or record.status != DeploymentStatus.COMPLETED
                    or record.phase != DeploymentPhase.PRODUCTION
                    or (
                        record.production_generation is not None
                        and deployment_id == record.deployment_id
                        and generation != record.production_generation
                    )
                ):
                    return {
                        "intact": False,
                        "issue": "active_model_deployment_mismatch",
                        "domain": domain,
                        "deployment_id": deployment_id,
                    }

            for row in pending_rows:
                deployment = records.get(row["deployment_id"])
                try:
                    claims = json.loads(row["claims_json"])
                    assessment = HealthAssessment.model_validate_json(row["health_json"])
                    expires_at = datetime.fromisoformat(row["expires_at"])
                except Exception:
                    return {
                        "intact": False,
                        "issue": "pending_transition_evidence_invalid",
                        "request_id": row["request_id"],
                    }
                next_phase = {
                    DeploymentPhase.SHADOW: DeploymentPhase.CANARY,
                    DeploymentPhase.CANARY: DeploymentPhase.PRODUCTION,
                }.get(deployment.phase) if deployment is not None else None
                expected_health_digest = self._digest(assessment.model_dump(mode="json"))
                if (
                    deployment is None
                    or deployment.status != DeploymentStatus.IN_PROGRESS
                    or row["from_phase"] != deployment.phase.value
                    or next_phase is None
                    or row["to_phase"] != next_phase.value
                    or claims.get("action") != "advance_rollout"
                    or claims.get("deployment_id") != deployment.deployment_id
                    or claims.get("domain") != deployment.domain
                    or claims.get("model_id") != deployment.model_id
                    or claims.get("from_phase") != row["from_phase"]
                    or claims.get("to_phase") != row["to_phase"]
                    or claims.get("benchmark_digest") != deployment.benchmark_digest
                    or claims.get("expected_generation") != deployment.expected_generation
                    or claims.get("previous_model_id") != deployment.previous_model_id
                    or claims.get("previous_deployment_id") != deployment.previous_deployment_id
                    or claims.get("health_digest") != expected_health_digest
                    or assessment.deployment_id != deployment.deployment_id
                    or assessment.model_id != deployment.model_id
                    or assessment.target_phase != next_phase
                    or assessment.passed is not True
                    or expires_at.tzinfo is None
                    or expires_at.utcoffset() is None
                    or expires_at <= assessment.assessed_at
                ):
                    return {
                        "intact": False,
                        "issue": "pending_transition_binding_mismatch",
                        "request_id": row["request_id"],
                    }

            return {
                "intact": True,
                "deployments": len(deployment_rows),
                "active_domains": len(active_rows),
                "pending_transitions": len(pending_rows),
            }
        except Exception as exc:
            log.exception("deployment state consistency check failed")
            return {"intact": False, "issue": type(exc).__name__}

    def events(self, deployment_id: Optional[str] = None, limit: int = 100):
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            if deployment_id is None:
                rows = connection.execute(
                    "SELECT event_json, event_hash FROM deployment_events "
                    "ORDER BY sequence DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT event_json, event_hash FROM deployment_events "
                    "WHERE deployment_id = ? ORDER BY sequence DESC LIMIT ?",
                    (deployment_id, limit),
                ).fetchall()
        return [
            {**json.loads(row["event_json"]), "event_hash": row["event_hash"]}
            for row in reversed(rows)
        ]
