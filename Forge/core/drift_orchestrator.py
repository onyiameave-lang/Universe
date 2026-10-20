"""
Forge V2 - Drift Orchestrator (Phase 6)
========================================
Coordinates authorized investigations in response to drift reports.

The orchestrator receives reports; it does not perform drift detection,
mutate champion state, or bypass the promotion gate.
"""

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from Forge.core.drift import DriftPolicyResolver, DriftReport, DriftSeverity
from Forge.core.promotion import AuthorizationToken


DRIFT_DIR = Path(__file__).resolve().parents[1] / "drift"
INVESTIGATIONS_FILE = DRIFT_DIR / "investigations.json"
INVESTIGATIONS_DB = DRIFT_DIR / "investigations.sqlite3"


class InvestigationType(str, Enum):
    CHALLENGER_MATCH = "challenger_match"
    RETRAIN_INCUMBENT = "retrain_incumbent"
    ABLATION_STUDY = "ablation_study"
    MONITOR_ONLY = "monitor_only"


class DriftInvestigationSuggestion(BaseModel):
    """A recommendation, not an action."""

    model_config = ConfigDict(frozen=True)

    report_id: str
    source_report_digest: str
    model_id: str
    champion_id: Optional[str] = None
    model_domain: Optional[str] = None
    suggested_investigation: InvestigationType
    suggested_hypothesis: Optional[str] = None
    priority: str = "normal"
    justification: str = ""
    cooldown_active: bool = False
    existing_investigation_found: bool = False
    budget_remaining: bool = True


class DriftOrchestrator:
    """Create suggestions and register investigations only with authorization."""

    def __init__(
        self,
        challenger_engine=None,
        discovery_translator=None,
        champion_registry=None,
        hypothesis_queue=None,
        policy_resolver: Optional[DriftPolicyResolver] = None,
        authorization_verifier=None,
    ):
        self._challenger_engine = challenger_engine
        self._discovery_translator = discovery_translator
        self._champion_registry = champion_registry
        self._hypothesis_queue = hypothesis_queue
        self._policy_resolver = policy_resolver
        self._authorization_verifier = authorization_verifier
        self._lock = threading.RLock()
        self._database_path = INVESTIGATIONS_DB
        self._initialize_database()
        self._ledger = self._load_ledger()

    def process_report(self, report: DriftReport) -> DriftInvestigationSuggestion:
        if report.overall_severity in (
            DriftSeverity.NONE,
            DriftSeverity.INSUFFICIENT_DATA,
        ):
            suggestion = DriftInvestigationSuggestion(
                report_id=report.report_id,
                source_report_digest=_canonical_digest(report.model_dump(mode="json")),
                model_id=report.model_id,
                champion_id=report.champion_id,
                model_domain=report.model_domain,
                suggested_investigation=InvestigationType.MONITOR_ONLY,
                justification=(
                    f"Severity is {report.overall_severity.value}; no investigation is suggested."
                ),
            )
            self._record_report_suggestion(report, suggestion)
            return suggestion

        with self._lock:
            self._ledger = self._load_ledger()
            model_key = report.champion_id or report.model_id
            entry = self._ledger.get(model_key, _empty_entry())
            investigations = entry.get("active_investigations", [])
            active_count = sum(inv.get("status") == "active" for inv in investigations)
            duplicate = any(
                inv.get("report_id") == report.report_id
                for inv in investigations
            )
            cooldown_seconds, max_active = self._policy_limits(
                report.champion_id, report.model_domain
            )
            cooldown_active = self._cooldown_is_active(
                entry.get("last_investigation_at"), cooldown_seconds
            )
            budget_remaining = active_count < max_active

        drifted_count = len(report.drifted_feature_indices)
        hypothesis = (
            f"Observed distribution shift in {drifted_count} feature(s) may affect "
            f"model behavior for {report.model_id}; evaluate with validated outcome data."
        )
        if duplicate:
            investigation = InvestigationType.MONITOR_ONLY
            justification = "An investigation for this drift report is already recorded."
        elif cooldown_active:
            investigation = InvestigationType.MONITOR_ONLY
            justification = "The policy cooldown is active; reassess with a new drift report after the cooldown."
        elif not budget_remaining:
            investigation = InvestigationType.MONITOR_ONLY
            justification = "The active investigation limit has been reached for this model or champion."
        else:
            investigation = InvestigationType.CHALLENGER_MATCH
            justification = (
                f"{report.overall_severity.value.capitalize()} drift detected. "
                "A challenger comparison is recommended; drift alone does not establish "
                "performance degradation."
            )

        suggestion = DriftInvestigationSuggestion(
            report_id=report.report_id,
            source_report_digest=_canonical_digest(report.model_dump(mode="json")),
            model_id=report.model_id,
            champion_id=report.champion_id,
            model_domain=report.model_domain,
            suggested_investigation=investigation,
            suggested_hypothesis=hypothesis if not duplicate else None,
            priority="high" if report.overall_severity == DriftSeverity.ACTIONABLE else "normal",
            justification=justification,
            cooldown_active=cooldown_active,
            existing_investigation_found=active_count > 0,
            budget_remaining=budget_remaining,
        )
        self._record_report_suggestion(report, suggestion)
        return suggestion

    def _record_report_suggestion(
        self, report: DriftReport, suggestion: DriftInvestigationSuggestion
    ) -> None:
        """Persist the exact report and derived suggestion before authorization."""
        model_key = report.champion_id or report.model_id
        report_json = _canonical_json(report.model_dump(mode="json"))
        suggestion_json = _canonical_json(suggestion.model_dump(mode="json"))
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT report_json, suggestion_json FROM processed_reports WHERE report_id = ?",
                    (report.report_id,),
                ).fetchone()
                if existing is not None:
                    if existing["report_json"] != report_json or existing["suggestion_json"] != suggestion_json:
                        raise ValueError(
                            "Drift report ID was reused with different report or suggestion content"
                        )
                else:
                    connection.execute(
                        "INSERT INTO processed_reports "
                        "(report_id, model_key, report_json, suggestion_json, recorded_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (report.report_id, model_key, report_json, suggestion_json,
                         datetime.now(timezone.utc).isoformat()),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO model_state (model_key, last_investigation_at) "
                        "VALUES (?, NULL)",
                        (model_key,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._ledger = self._load_ledger()

    @staticmethod
    def authorization_claims(
        suggestion: DriftInvestigationSuggestion, authorized_by: str
    ) -> Dict[str, Any]:
        """Exact Aegis claims for one persisted drift investigation suggestion."""
        suggestion_payload = suggestion.model_dump(mode="json")
        canonical = json.dumps(
            suggestion_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return {
            "action": "drift_investigation",
            "report_id": suggestion.report_id,
            "model_id": suggestion.model_id,
            "champion_id": suggestion.champion_id,
            "model_domain": suggestion.model_domain,
            "investigation_type": suggestion.suggested_investigation.value,
            "suggestion_digest": hashlib.sha256(canonical).hexdigest(),
            "source_report_digest": suggestion.source_report_digest,
            "risk_category": "drift_investigation",
            "authorized_by": authorized_by,
        }

    def get_persisted_suggestion(
        self, report_id: str
    ) -> Optional[DriftInvestigationSuggestion]:
        """Return only the suggestion previously derived from a stored report."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT suggestion_json FROM processed_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return (
            DriftInvestigationSuggestion.model_validate_json(row["suggestion_json"])
            if row else None
        )

    def operational_status(self) -> Dict[str, Any]:
        """Expose persisted report and investigation counts for operator review."""
        with closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            models = connection.execute("SELECT COUNT(*) FROM model_state").fetchone()[0]
            reports = connection.execute("SELECT COUNT(*) FROM processed_reports").fetchone()[0]
            investigations = connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0]
            active = connection.execute(
                "SELECT COUNT(*) FROM investigations WHERE status = 'active'"
            ).fetchone()[0]
        return {
            "database_integrity": integrity == "ok",
            "models_tracked": models,
            "processed_reports": reports,
            "investigations": investigations,
            "active_investigations": active,
            "authorization_configured": self._authorization_verifier is not None,
            "ledger_path": str(self._database_path),
            "ready": integrity == "ok",
        }

    def execute_investigation(
        self,
        suggestion: DriftInvestigationSuggestion,
        authorization: AuthorizationToken,
    ) -> Optional[str]:
        """Register an authorized investigation after an immediate guard recheck.

        This records and optionally queues an investigation; it does not submit
        an experiment because the report contains no dataset or benchmark context.
        """
        if suggestion.suggested_investigation == InvestigationType.MONITOR_ONLY:
            return None
        if authorization.decision != "AUTHORIZED":
            raise PermissionError("Aegis authorization did not approve the investigation")
        if authorization.challenge_id != suggestion.report_id:
            raise PermissionError("Authorization is not bound to this drift report")
        if authorization.audience != "forge.promotion":
            raise PermissionError("Authorization audience must be forge.promotion")
        if authorization.risk_category != "drift_investigation":
            raise PermissionError("Authorization risk category does not match drift investigation")
        if not authorization.authorized_by.strip():
            raise PermissionError("Authorization identity is required")
        if not authorization.issuer or not authorization.signature or not authorization.nonce:
            raise PermissionError("Authorization must include a trusted issuer, signature, and nonce")
        if authorization.expires_at is None:
            raise PermissionError("Authorization expiry is required")
        _require_aware(authorization.issued_at, "authorization issued_at")
        _require_aware(authorization.expires_at, "authorization expires_at")
        now = datetime.now(timezone.utc)
        if authorization.issued_at > now:
            raise PermissionError("Authorization was issued in the future")
        if authorization.expires_at <= now or authorization.expires_at <= authorization.issued_at:
            raise PermissionError("Authorization is expired or has an invalid validity interval")

        model_key = suggestion.champion_id or suggestion.model_id
        suggestion_json = _canonical_json(suggestion.model_dump(mode="json"))
        with closing(self._connect()) as connection:
            persisted = connection.execute(
                "SELECT model_key, report_json, suggestion_json FROM processed_reports "
                "WHERE report_id = ?",
                (suggestion.report_id,),
            ).fetchone()
        if persisted is None or persisted["model_key"] != model_key:
            raise PermissionError("Drift report must be processed and persisted before execution")
        if persisted["suggestion_json"] != suggestion_json:
            raise PermissionError("Suggestion does not match the persisted drift assessment")
        if _canonical_digest(json.loads(persisted["report_json"])) != suggestion.source_report_digest:
            raise PermissionError("Persisted drift report does not match the authorized assessment")

        required_claims = self.authorization_claims(suggestion, authorization.authorized_by)
        if authorization.claims != required_claims:
            raise PermissionError("Authorization claims do not bind approval to this exact suggestion")
        verifier = self._authorization_verifier
        if verifier is None:
            raise PermissionError("No trusted Aegis verifier is configured for drift investigations")
        if not verifier.verify_and_consume(authorization, required_claims):
            raise PermissionError("Trusted Aegis verifier rejected the drift authorization")

        now = datetime.now(timezone.utc)
        investigation_id = str(uuid.uuid4())
        record = {
            "investigation_id": investigation_id,
            "report_id": suggestion.report_id,
            "model_id": suggestion.model_id,
            "type": suggestion.suggested_investigation.value,
            "status": "active",
            "authorization_id": authorization.authorization_id,
            "authorization_issuer": authorization.issuer,
            "authorization_nonce": authorization.nonce,
            "authorization_claims_digest": _canonical_digest(required_claims),
            "authorized_by": authorization.authorized_by,
            "started_at": now.isoformat(),
        }
        cooldown_seconds, max_active = self._policy_limits(
            suggestion.champion_id, suggestion.model_domain
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                persisted = connection.execute(
                    "SELECT model_key, report_json, suggestion_json FROM processed_reports "
                    "WHERE report_id = ?",
                    (suggestion.report_id,),
                ).fetchone()
                if (
                    persisted is None
                    or persisted["model_key"] != model_key
                    or persisted["suggestion_json"] != suggestion_json
                    or _canonical_digest(json.loads(persisted["report_json"]))
                    != suggestion.source_report_digest
                ):
                    raise PermissionError("Persisted drift assessment changed during authorization")
                if connection.execute(
                    "SELECT 1 FROM investigations WHERE report_id = ?",
                    (suggestion.report_id,),
                ).fetchone():
                    connection.commit()
                    return None
                state = connection.execute(
                    "SELECT last_investigation_at FROM model_state WHERE model_key = ?",
                    (model_key,),
                ).fetchone()
                last_investigation = state["last_investigation_at"] if state else None
                if self._cooldown_is_active(last_investigation, cooldown_seconds):
                    raise RuntimeError("Investigation cooldown became active before commit")
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM investigations "
                    "WHERE model_key = ? AND status = 'active'",
                    (model_key,),
                ).fetchone()[0]
                if active_count >= max_active:
                    raise RuntimeError("Active investigation limit was reached before commit")
                connection.execute(
                    "INSERT INTO investigations "
                    "(investigation_id, model_key, report_id, status, record_json) "
                    "VALUES (?, ?, ?, 'active', ?)",
                    (investigation_id, model_key, suggestion.report_id,
                     _canonical_json(record)),
                )
                connection.execute(
                    "INSERT INTO model_state (model_key, last_investigation_at) "
                    "VALUES (?, ?) ON CONFLICT(model_key) DO UPDATE SET "
                    "last_investigation_at = excluded.last_investigation_at",
                    (model_key, now.isoformat()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._ledger = self._load_ledger()

        if self._hypothesis_queue and suggestion.suggested_hypothesis:
            self._hypothesis_queue.add(
                statement=suggestion.suggested_hypothesis,
                proposed_by="DriftOrchestrator",
            )
        return investigation_id

    def complete_investigation(self, model_key: str, investigation_id: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT record_json, status FROM investigations "
                    "WHERE investigation_id = ? AND model_key = ?",
                    (investigation_id, model_key),
                ).fetchone()
                if row is None or row["status"] != "active":
                    connection.commit()
                    return
                record = json.loads(row["record_json"])
                record["status"] = "completed"
                record["completed_at"] = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "UPDATE investigations SET status = 'completed', record_json = ? "
                    "WHERE investigation_id = ? AND status = 'active'",
                    (_canonical_json(record), investigation_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._ledger = self._load_ledger()

    def _policy_limits(
        self, champion_id: Optional[str], domain: Optional[str]
    ) -> tuple[int, int]:
        if self._policy_resolver is None:
            return 86400, 3
        policy = self._policy_resolver.resolve(champion_id, domain)
        return policy.cooldown_seconds, policy.max_active_investigations

    @staticmethod
    def _cooldown_is_active(last_timestamp: Optional[str], cooldown_seconds: int) -> bool:
        if last_timestamp is None:
            return False
        try:
            last_dt = datetime.fromisoformat(last_timestamp)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Investigation ledger has an invalid last_investigation_at") from exc
        _require_aware(last_dt, "ledger last_investigation_at")
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed < cooldown_seconds

    @staticmethod
    def _load_legacy_ledger() -> Dict[str, Dict[str, Any]]:
        if not INVESTIGATIONS_FILE.exists():
            return {}
        try:
            data = json.loads(INVESTIGATIONS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read investigation ledger: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Investigation ledger must contain a JSON object")
        for model_key, entry in data.items():
            if (
                not isinstance(model_key, str)
                or not isinstance(entry, dict)
                or not isinstance(entry.get("active_investigations", []), list)
            ):
                raise RuntimeError("Investigation ledger contains an invalid entry")
        return data

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS drift_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_state (
                    model_key TEXT PRIMARY KEY,
                    last_investigation_at TEXT
                );
                CREATE TABLE IF NOT EXISTS processed_reports (
                    report_id TEXT PRIMARY KEY,
                    model_key TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    suggestion_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_processed_reports_model
                    ON processed_reports(model_key, recorded_at);
                CREATE TABLE IF NOT EXISTS investigations (
                    investigation_id TEXT PRIMARY KEY,
                    model_key TEXT NOT NULL,
                    report_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_investigations_active
                    ON investigations(model_key, status);
            """)
            connection.execute("BEGIN IMMEDIATE")
            try:
                schema = connection.execute(
                    "SELECT value FROM drift_metadata WHERE key = 'schema_version'"
                ).fetchone()
                if schema is not None and schema["value"] != "1":
                    raise RuntimeError(
                        f"Unsupported drift ledger schema version: {schema['value']}"
                    )
                if schema is None:
                    connection.execute(
                        "INSERT INTO drift_metadata(key, value) VALUES('schema_version', '1')"
                    )
                migrated = connection.execute(
                    "SELECT value FROM drift_metadata WHERE key = 'legacy_json_migrated'"
                ).fetchone()
                if migrated is None:
                    legacy = self._load_legacy_ledger()
                    for model_key, entry in legacy.items():
                        connection.execute(
                            "INSERT OR IGNORE INTO model_state(model_key, last_investigation_at) "
                            "VALUES (?, ?)",
                            (model_key, entry.get("last_investigation_at")),
                        )
                        for raw in entry.get("active_investigations", []):
                            investigation_id = raw.get("investigation_id")
                            report_id = raw.get("report_id")
                            if not isinstance(investigation_id, str) or not isinstance(report_id, str):
                                raise RuntimeError("Legacy investigation record lacks stable IDs")
                            status = raw.get("status", "active")
                            connection.execute(
                                "INSERT OR IGNORE INTO investigations "
                                "(investigation_id, model_key, report_id, status, record_json) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (investigation_id, model_key, report_id, status,
                                 _canonical_json(raw)),
                            )
                    connection.execute(
                        "INSERT INTO drift_metadata(key, value) VALUES('legacy_json_migrated', '1')"
                    )
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"Drift ledger integrity check failed: {integrity}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _load_ledger(self) -> Dict[str, Dict[str, Any]]:
        with closing(self._connect()) as connection:
            states = connection.execute(
                "SELECT model_key, last_investigation_at FROM model_state"
            ).fetchall()
            report_rows = connection.execute(
                "SELECT report_id, model_key, report_json, suggestion_json, recorded_at "
                "FROM processed_reports"
            ).fetchall()
            investigation_rows = connection.execute(
                "SELECT model_key, record_json FROM investigations ORDER BY rowid"
            ).fetchall()
        ledger = {
            row["model_key"]: {
                "last_investigation_at": row["last_investigation_at"],
                "active_investigations": [],
                "processed_reports": {},
            }
            for row in states
        }
        for row in report_rows:
            ledger.setdefault(row["model_key"], _empty_entry()).setdefault(
                "processed_reports", {}
            )[row["report_id"]] = {
                "report": json.loads(row["report_json"]),
                "suggestion": json.loads(row["suggestion_json"]),
                "recorded_at": row["recorded_at"],
            }
        for row in investigation_rows:
            record = json.loads(row["record_json"])
            ledger.setdefault(row["model_key"], _empty_entry())["active_investigations"].append(record)
        return ledger


def _empty_entry() -> Dict[str, Any]:
    return {
        "active_investigations": [],
        "last_investigation_at": None,
        "processed_reports": {},
    }


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_digest(value: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_aware(value: datetime, label: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
