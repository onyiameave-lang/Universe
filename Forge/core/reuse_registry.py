"""
Forge - Reuse Registry (§26.5)
================================
Discovers opportunities to reuse validated datasets, pipelines, model
artifacts, experiment templates, benchmark suites, and prior findings
across agents.

Before reuse, checks:
- Target compatibility
- Data and permission boundaries
- Evaluation comparability
- Domain shift and assumptions
- Version compatibility
- Resource and licensing restrictions
- Whether adaptation or independent revalidation is required

Reuse NEVER silently transfers a model's claims of validity from one
domain to another.
"""

import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("forge.reuse_registry")


# ==============================================================================
# 1. ENUMS
# ==============================================================================

class ArtifactKind(str, Enum):
    """Kind of reusable artifact."""
    DATASET = "dataset"
    PIPELINE = "pipeline"
    MODEL = "model"
    EXPERIMENT_TEMPLATE = "experiment_template"
    BENCHMARK_SUITE = "benchmark_suite"
    EVALUATION_SUITE = "evaluation_suite"
    FEATURE_SET = "feature_set"
    FINDING = "finding"
    CONFIGURATION = "configuration"


class ReuseEligibility(str, Enum):
    """Whether an artifact is eligible for reuse."""
    ELIGIBLE = "eligible"
    REQUIRES_ADAPTATION = "requires_adaptation"
    REQUIRES_REVALIDATION = "requires_revalidation"
    INCOMPATIBLE = "incompatible"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class CompatibilityCheckStatus(str, Enum):
    """Result of a compatibility check."""
    COMPATIBLE = "compatible"
    PARTIALLY_COMPATIBLE = "partially_compatible"
    INCOMPATIBLE = "incompatible"
    UNCHECKED = "unchecked"


# ==============================================================================
# 2. MODELS
# ==============================================================================

class ReusableArtifact(BaseModel):
    """A registered reusable artifact."""

    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: ArtifactKind
    name: str
    description: str = ""

    # Provenance
    source_agent: str
    source_repository: str
    source_experiment_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"

    # Identity and integrity
    content_fingerprint: Optional[str] = None
    artifact_path: Optional[str] = None

    # Domain and compatibility
    domain: str = ""
    target_types: List[str] = Field(default_factory=list)
    compatible_domains: List[str] = Field(default_factory=list)
    data_permissions: List[str] = Field(default_factory=list)

    # Quality evidence
    validation_evidence: Dict[str, Any] = Field(default_factory=dict)
    evaluation_metrics: Dict[str, float] = Field(default_factory=dict)

    # Lifecycle
    eligibility: ReuseEligibility = ReuseEligibility.ELIGIBLE
    expires_at: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CompatibilityCheck(BaseModel):
    """Result of checking whether an artifact can be reused in a new context."""
    model_config = ConfigDict(frozen=True)

    check_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    artifact_id: str
    artifact_fingerprint: str = ""
    requesting_agent: str
    target_domain: str

    # Check results
    status: CompatibilityCheckStatus = CompatibilityCheckStatus.UNCHECKED
    eligibility: ReuseEligibility = ReuseEligibility.UNKNOWN

    # Detailed findings
    target_compatible: bool = False
    permission_granted: bool = False
    evaluation_comparable: bool = False
    domain_shift_detected: bool = False
    version_compatible: bool = False
    licensing_clear: bool = False

    # Recommendations
    adaptation_required: bool = False
    revalidation_required: bool = False
    warnings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)

    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("warnings", "blockers", mode="before")
    @classmethod
    def _ensure_list(cls, value):
        if isinstance(value, tuple):
            return list(value)
        return value


class ReuseRecord(BaseModel):
    """Records an approved reuse of an artifact."""

    reuse_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    artifact_id: str
    check_id: str
    requesting_agent: str
    target_domain: str
    approved: bool = False
    approved_at: Optional[datetime] = None
    conditions: List[str] = Field(default_factory=list)
    authorization_id: Optional[str] = None
    authorized_by: Optional[str] = None
    authorization_claims_digest: Optional[str] = None


class ReuseAuthorization(BaseModel):
    """Signed one-time approval for one exact artifact reuse decision."""
    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    decision: str
    issuer: str
    audience: str = "forge.reuse"
    authorized_by: str
    nonce: str
    signature: str
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    claims: Dict[str, Any]

    @field_validator("decision")
    @classmethod
    def _authorized_only(cls, value):
        if value != "AUTHORIZED":
            raise ValueError("reuse authorization decision must be AUTHORIZED")
        return value

    @field_validator("issuer", "audience", "authorized_by", "nonce", "signature")
    @classmethod
    def _required_text(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reuse authorization identity, nonce, and signature are required")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reuse authorization timestamps must be timezone-aware")
        return value

    @field_validator("claims", mode="before")
    @classmethod
    def _copy_claims(cls, value):
        if not isinstance(value, dict):
            raise ValueError("reuse authorization claims must be a mapping")
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


class ReuseAuthorizationVerifier(Protocol):
    def verify_and_consume(
        self, authorization: ReuseAuthorization, required_claims: Dict[str, Any]
    ) -> bool:
        ...


class ReuseCompatibilityPolicy(Protocol):
    """Operator-owned checks for runtime and licensing compatibility."""

    def operational_status(self) -> Dict[str, Any]:
        ...

    def version_compatible(self, artifact: ReusableArtifact, target_domain: str) -> bool:
        ...

    def licensing_clear(
        self, artifact: ReusableArtifact, requesting_agent: str, target_domain: str
    ) -> bool:
        ...


# ==============================================================================
# 3. REUSE REGISTRY
# ==============================================================================

_REUSE_DIR = Path(__file__).resolve().parents[1] / "reuse"
_REUSE_DB = _REUSE_DIR / "reuse_registry.sqlite3"
_REUSE_DIR.mkdir(parents=True, exist_ok=True)


class ReuseRegistry:
    """
    Cross-agent artifact reuse registry.

    Maintains a catalog of reusable artifacts, performs compatibility checks,
    and records approved reuse decisions with provenance.

    Uses Chronicle's public client interface (when available) to persist
    institutional knowledge.
    """

    SCHEMA_VERSION = "1"

    def __init__(
        self,
        database_path: Optional[Path] = None,
        chronicle_client: Any = None,
        compatibility_policy: Optional[ReuseCompatibilityPolicy] = None,
        authorization_verifier: Optional[ReuseAuthorizationVerifier] = None,
    ):
        self._db_path = database_path or _REUSE_DB
        self._chronicle = chronicle_client
        self._compatibility_policy = compatibility_policy
        self._authorization_verifier = authorization_verifier
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_db()

    def _initialize_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    eligibility TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compatibility_checks (
                    check_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                    requesting_agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    check_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reuse_records (
                    reuse_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                    check_id TEXT NOT NULL REFERENCES compatibility_checks(check_id),
                    requesting_agent TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    approved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
                CREATE INDEX IF NOT EXISTS idx_artifacts_domain ON artifacts(domain);
                CREATE INDEX IF NOT EXISTS idx_artifacts_agent ON artifacts(source_agent);
                CREATE INDEX IF NOT EXISTS idx_checks_artifact ON compatibility_checks(artifact_id);
                CREATE INDEX IF NOT EXISTS idx_reuse_artifact ON reuse_records(artifact_id);
            """)
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
                (self.SCHEMA_VERSION,),
            )
            schema = conn.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if schema is None or schema["value"] != self.SCHEMA_VERSION:
                raise RuntimeError(
                    "Unsupported Forge reuse registry schema: "
                    f"{schema['value'] if schema else 'missing'}"
                )
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Forge reuse registry integrity check failed: {integrity}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def operational_status(self) -> Dict[str, Any]:
        """Report durable health and whether governed reuse can be approved."""
        with closing(self._connect()) as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            counts = {
                "artifacts": conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
                "compatibility_checks": conn.execute(
                    "SELECT COUNT(*) FROM compatibility_checks"
                ).fetchone()[0],
                "approved_reuses": conn.execute(
                    "SELECT COUNT(*) FROM reuse_records WHERE approved = 1"
                ).fetchone()[0],
            }
        policy_status = {"ready": False, "error": "compatibility_policy_not_configured"}
        if self._compatibility_policy is not None:
            try:
                callback = getattr(self._compatibility_policy, "operational_status", None)
                candidate = callback() if callable(callback) else None
                if isinstance(candidate, dict):
                    policy_status = candidate
                else:
                    policy_status = {"ready": False, "error": "invalid_policy_status"}
            except Exception as exc:
                policy_status = {"ready": False, "error": type(exc).__name__}
        return {
            "ready": integrity == "ok",
            "database_integrity": integrity,
            "compatibility_policy_configured": self._compatibility_policy is not None,
            "compatibility_policy": policy_status,
            "authorization_verifier_configured": self._authorization_verifier is not None,
            "governed_reuse_ready": (
                integrity == "ok"
                and self._compatibility_policy is not None
                and self._authorization_verifier is not None
                and policy_status.get("ready") is True
            ),
            **counts,
        }

    @staticmethod
    def _encode(model: BaseModel) -> str:
        return json.dumps(
            model.model_dump(mode="json"),
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_artifact(self, artifact: ReusableArtifact) -> ReusableArtifact:
        """Register an artifact as available for cross-agent reuse."""
        encoded = self._encode(artifact)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO artifacts
                       (artifact_id, kind, name, source_agent, domain,
                        eligibility, artifact_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact.artifact_id, artifact.kind.value,
                        artifact.name, artifact.source_agent,
                        artifact.domain, artifact.eligibility.value,
                        encoded, artifact.created_at.isoformat(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        # Sync to Chronicle if available
        self._sync_to_chronicle(artifact)

        log.info(
            "Registered reusable artifact '%s' (%s) from %s",
            artifact.name, artifact.kind.value, artifact.source_agent,
        )
        return artifact

    def check_compatibility(
        self,
        artifact_id: str,
        requesting_agent: str,
        target_domain: str,
        target_types: Optional[List[str]] = None,
        required_permissions: Optional[List[str]] = None,
    ) -> CompatibilityCheck:
        """Check whether an artifact can be reused in a new context.

        Performs the §26.5 checks:
        - Target compatibility
        - Data and permission boundaries
        - Evaluation comparability
        - Domain shift and assumptions
        - Version compatibility
        - Resource and licensing restrictions
        - Whether adaptation or revalidation is required
        """
        artifact = self._load_artifact(artifact_id)
        if artifact is None:
            return CompatibilityCheck(
                artifact_id=artifact_id,
                requesting_agent=requesting_agent,
                target_domain=target_domain,
                status=CompatibilityCheckStatus.INCOMPATIBLE,
                eligibility=ReuseEligibility.INCOMPATIBLE,
                blockers=["Artifact not found in registry"],
            )

        warnings: List[str] = []
        blockers: List[str] = []
        unknowns: List[str] = []

        # 1. Target compatibility
        target_compatible = False
        if not target_types or not artifact.target_types:
            unknowns.append("Target compatibility cannot be established without declared target types")
        else:
            target_compatible = any(t in artifact.target_types for t in target_types)
            if not target_compatible:
                blockers.append(
                    f"Artifact supports {artifact.target_types}, requested {target_types}"
                )

        # 2. Permission boundaries
        permission_granted = required_permissions is not None
        if required_permissions is None:
            unknowns.append("Required data permissions were not specified for this reuse check")
        else:
            missing = set(required_permissions) - set(artifact.data_permissions)
            if missing:
                permission_granted = False
                blockers.append(f"Missing data permissions: {missing}")

        # 3. Evaluation comparability
        evaluation_comparable = bool(artifact.evaluation_metrics)
        if not evaluation_comparable:
            warnings.append("Artifact has no evaluation metrics for comparison")

        # 4. Domain shift detection
        domain_shift_detected = False
        if artifact.domain and target_domain:
            if artifact.domain != target_domain:
                if target_domain not in artifact.compatible_domains:
                    domain_shift_detected = True
                    warnings.append(
                        f"Domain shift: artifact from '{artifact.domain}', "
                        f"target domain '{target_domain}'"
                    )

        # 5. Runtime and license claims require an operator-owned policy. An
        # absent checker is unknown, never implicit approval.
        version_compatible = False
        licensing_clear = False
        policy = self._compatibility_policy
        if policy is None:
            unknowns.extend([
                "Runtime version compatibility policy is not configured",
                "License compatibility policy is not configured",
            ])
        else:
            try:
                version_compatible = policy.version_compatible(artifact, target_domain) is True
                licensing_clear = policy.licensing_clear(
                    artifact, requesting_agent, target_domain
                ) is True
            except Exception as exc:
                unknowns.append(
                    f"Compatibility policy failed ({type(exc).__name__}); reuse is blocked"
                )
            if not version_compatible and not unknowns:
                blockers.append("Runtime version compatibility was not established")
            if not licensing_clear and not unknowns:
                blockers.append("License compatibility was not established")

        # 7. Eligibility check
        if artifact.eligibility == ReuseEligibility.EXPIRED:
            blockers.append("Artifact has expired")
        if artifact.expires_at and artifact.expires_at <= datetime.now(timezone.utc):
            blockers.append("Artifact expiration date has passed")

        # Determine overall eligibility
        adaptation_required = domain_shift_detected
        revalidation_required = domain_shift_detected or not evaluation_comparable

        if blockers:
            eligibility = ReuseEligibility.INCOMPATIBLE
            status = CompatibilityCheckStatus.INCOMPATIBLE
        elif unknowns:
            eligibility = ReuseEligibility.UNKNOWN
            status = CompatibilityCheckStatus.UNCHECKED
        elif adaptation_required:
            eligibility = ReuseEligibility.REQUIRES_ADAPTATION
            status = CompatibilityCheckStatus.PARTIALLY_COMPATIBLE
        elif revalidation_required:
            eligibility = ReuseEligibility.REQUIRES_REVALIDATION
            status = CompatibilityCheckStatus.PARTIALLY_COMPATIBLE
        else:
            eligibility = ReuseEligibility.ELIGIBLE
            status = CompatibilityCheckStatus.COMPATIBLE

        check = CompatibilityCheck(
            artifact_id=artifact_id,
            artifact_fingerprint=hashlib.sha256(self._encode(artifact).encode("utf-8")).hexdigest(),
            requesting_agent=requesting_agent,
            target_domain=target_domain,
            status=status,
            eligibility=eligibility,
            target_compatible=target_compatible,
            permission_granted=permission_granted,
            evaluation_comparable=evaluation_comparable,
            domain_shift_detected=domain_shift_detected,
            version_compatible=version_compatible,
            licensing_clear=licensing_clear,
            adaptation_required=adaptation_required,
            revalidation_required=revalidation_required,
            warnings=warnings + unknowns,
            blockers=blockers,
        )

        # Persist the check
        self._persist_check(check)
        return check

    def reuse_authorization_claims(
        self, check_id: str, authorized_by: str, conditions: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Build exact signed claims for a pending compatible reuse decision."""
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by is required")
        check = self._load_check(check_id)
        if check is None:
            raise KeyError("compatibility check was not found")
        checked_at = check.checked_at
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise RuntimeError("compatibility check timestamp is not timezone-aware")
        if datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc) > timedelta(minutes=5):
            raise RuntimeError("compatibility check is older than the five-minute approval window")
        conditions = list(conditions or [])
        if any(not isinstance(item, str) or not item.strip() for item in conditions):
            raise ValueError("reuse conditions must be non-empty strings")
        if check.eligibility not in {
            ReuseEligibility.ELIGIBLE,
            ReuseEligibility.REQUIRES_ADAPTATION,
            ReuseEligibility.REQUIRES_REVALIDATION,
        }:
            raise PermissionError("reuse compatibility has not been established")
        if check.eligibility in {
            ReuseEligibility.REQUIRES_ADAPTATION,
            ReuseEligibility.REQUIRES_REVALIDATION,
        } and not conditions:
            raise ValueError("conditional reuse requires explicit conditions")
        artifact = self._load_artifact(check.artifact_id)
        if artifact is None or hashlib.sha256(
            self._encode(artifact).encode("utf-8")
        ).hexdigest() != check.artifact_fingerprint:
            raise RuntimeError("artifact changed after the compatibility check")
        if (artifact.eligibility == ReuseEligibility.EXPIRED
                or (artifact.expires_at is not None
                    and artifact.expires_at <= datetime.now(timezone.utc))):
            raise RuntimeError("artifact expired after the compatibility check")
        return {
            "action": "approve_artifact_reuse",
            "audience": "forge.reuse",
            "check_id": check.check_id,
            "artifact_id": check.artifact_id,
            "artifact_fingerprint": check.artifact_fingerprint,
            "requesting_agent": check.requesting_agent,
            "target_domain": check.target_domain,
            "eligibility": check.eligibility.value,
            "conditions_digest": hashlib.sha256(
                json.dumps(conditions, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
            ).hexdigest(),
            "authorized_by": authorized_by.strip(),
        }

    def reuse_authorization_request(
        self, check_id: str, authorized_by: str, conditions: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return {
            "action": "approve_artifact_reuse",
            "audience": "forge.reuse",
            "required_claims": self.reuse_authorization_claims(
                check_id, authorized_by, conditions
            ),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    def approve_reuse(
        self, check_id: str, authorization: ReuseAuthorization,
        conditions: Optional[List[str]] = None,
    ) -> ReuseRecord:
        """Record reuse only after exact Aegis authorization is verified and consumed."""
        if self._authorization_verifier is None:
            raise PermissionError("artifact reuse authorization is not configured")
        conditions = list(conditions or [])
        claims = self.reuse_authorization_claims(
            check_id, authorization.authorized_by, conditions
        )
        if authorization.audience != "forge.reuse" or authorization.claims != claims:
            raise PermissionError("Aegis claims do not match this exact reuse decision")
        if not self._authorization_verifier.verify_and_consume(authorization, claims):
            raise PermissionError("Aegis rejected or already consumed the reuse authorization")

        now = datetime.now(timezone.utc)
        claims_digest = hashlib.sha256(
            json.dumps(claims, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
        record = ReuseRecord(
            artifact_id=claims["artifact_id"],
            check_id=check_id,
            requesting_agent=claims["requesting_agent"],
            target_domain=claims["target_domain"],
            approved=True,
            approved_at=now,
            conditions=conditions,
            authorization_id=authorization.authorization_id,
            authorized_by=authorization.authorized_by,
            authorization_claims_digest=claims_digest,
        )
        self._persist_approved_reuse(record, claims["artifact_fingerprint"])
        return record

    def _persist_approved_reuse(self, record: ReuseRecord, expected_fingerprint: str) -> None:
        encoded = self._encode(record)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                check_row = conn.execute(
                    "SELECT check_json FROM compatibility_checks WHERE check_id = ?",
                    (record.check_id,),
                ).fetchone()
                artifact_row = conn.execute(
                    "SELECT artifact_json FROM artifacts WHERE artifact_id = ?",
                    (record.artifact_id,),
                ).fetchone()
                if check_row is None or artifact_row is None:
                    raise RuntimeError("reuse evidence disappeared before approval commit")
                check = CompatibilityCheck.model_validate_json(check_row["check_json"])
                artifact = ReusableArtifact.model_validate_json(artifact_row["artifact_json"])
                current_fingerprint = hashlib.sha256(
                    self._encode(artifact).encode("utf-8")
                ).hexdigest()
                if (check.artifact_fingerprint != expected_fingerprint
                        or current_fingerprint != expected_fingerprint):
                    raise RuntimeError("artifact changed before reuse approval commit")
                checked_at = check.checked_at
                if (checked_at.tzinfo is None or checked_at.utcoffset() is None
                        or datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)
                        > timedelta(minutes=5)):
                    raise RuntimeError("compatibility check expired before approval commit")
                if (artifact.eligibility == ReuseEligibility.EXPIRED
                        or (artifact.expires_at is not None
                            and artifact.expires_at <= datetime.now(timezone.utc))):
                    raise RuntimeError("artifact expired before approval commit")
                existing = conn.execute(
                    "SELECT 1 FROM reuse_records WHERE check_id = ? AND approved = 1 LIMIT 1",
                    (record.check_id,),
                ).fetchone()
                if existing is not None:
                    raise RuntimeError("reuse check has already been approved")
                conn.execute(
                    """INSERT INTO reuse_records
                       (reuse_id, artifact_id, check_id, requesting_agent,
                        approved, record_json, approved_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (record.reuse_id, record.artifact_id, record.check_id,
                     record.requesting_agent, encoded, record.approved_at.isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def search_artifacts(
        self,
        kind: Optional[ArtifactKind] = None,
        domain: Optional[str] = None,
        source_agent: Optional[str] = None,
        eligible_only: bool = True,
    ) -> List[ReusableArtifact]:
        """Search for reusable artifacts matching criteria."""
        with closing(self._connect()) as conn:
            query = "SELECT artifact_json FROM artifacts"
            conditions = []
            params = []
            if kind:
                conditions.append("kind = ?")
                params.append(kind.value)
            if domain:
                conditions.append("domain = ?")
                params.append(domain)
            if source_agent:
                conditions.append("source_agent = ?")
                params.append(source_agent)
            if eligible_only:
                conditions.append("eligibility = ?")
                params.append(ReuseEligibility.ELIGIBLE.value)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at DESC"

            return [
                ReusableArtifact.model_validate_json(row["artifact_json"])
                for row in conn.execute(query, params)
            ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_artifact(self, artifact_id: str) -> Optional[ReusableArtifact]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT artifact_json FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if row:
                return ReusableArtifact.model_validate_json(row["artifact_json"])
        return None

    def _load_check(self, check_id: str) -> Optional[CompatibilityCheck]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT check_json FROM compatibility_checks WHERE check_id = ?",
                (check_id,),
            ).fetchone()
            if row:
                return CompatibilityCheck.model_validate_json(row["check_json"])
        return None

    def _persist_check(self, check: CompatibilityCheck) -> None:
        encoded = self._encode(check)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO compatibility_checks
                       (check_id, artifact_id, requesting_agent, status,
                        check_json, checked_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        check.check_id, check.artifact_id,
                        check.requesting_agent, check.status.value,
                        encoded, check.checked_at.isoformat(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _persist_reuse_record(self, record: ReuseRecord) -> None:
        encoded = self._encode(record)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO reuse_records
                       (reuse_id, artifact_id, check_id, requesting_agent,
                        approved, record_json, approved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.reuse_id, record.artifact_id,
                        record.check_id, record.requesting_agent,
                        int(record.approved), encoded,
                        record.approved_at.isoformat() if record.approved_at else None,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _sync_to_chronicle(self, artifact: ReusableArtifact) -> None:
        """Sync artifact to Chronicle for cross-agent discovery (§26.5)."""
        if self._chronicle is None:
            return
        try:
            if hasattr(self._chronicle, "act"):
                self._chronicle.act("memory.store", {
                    "content": f"Reusable artifact registered: {artifact.name} "
                              f"({artifact.kind.value}) from {artifact.source_agent}",
                    "tags": ["forge", "reuse", artifact.kind.value, artifact.source_agent],
                    "metadata": {
                        "artifact_id": artifact.artifact_id,
                        "kind": artifact.kind.value,
                        "domain": artifact.domain,
                        "source_agent": artifact.source_agent,
                    },
                    "_sender": "forge",
                })
        except Exception as exc:
            log.warning("Failed to sync artifact to Chronicle: %s", exc)
