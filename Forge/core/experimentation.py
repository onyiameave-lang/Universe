"""
Forge V2 - Experimentation Core (Phase 3A)
=========================================
Defines the unified Experiment Contract, Run, and Result models.
Implements the ExperimentRunner orchestrator and the Domain Adapter interface.
Preserves V1 by wrapping it rather than rewriting it.

Phase 2 Refinements:
- Separated ExperimentData, ExperimentEnvironment, ExperimentEvaluation.
- dataset_path required for ML experiments (dataset_id deferred).
- configuration_fingerprint vs environment_fingerprint explicitly separated.
- evaluation.metrics defines intent, not results.

Phase 3A Additions:
- MetricDirection enum and MetricSpec model.
- Timezone-aware UTC timestamps.
"""

import argparse
import getpass
import hashlib
import importlib.metadata
import inspect
import json
import logging
import os
import platform
import sqlite3
import sys
import tempfile
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from Forge.core.metrics import finite_metric_value
from Forge.core.storage_paths import migrate_legacy_storage

log = logging.getLogger(__name__)

# ==============================================================================
# 1. PERSISTENCE CONFIGURATION
# ==============================================================================
_FORGE_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = _FORGE_ROOT / "memory" / "experiments"
EXPERIMENTS_FILE = EXPERIMENTS_DIR / "experiments.json"
RESULTS_FILE = EXPERIMENTS_DIR / "results.json"
EXPERIMENTS_DB = EXPERIMENTS_DIR / "experiments.sqlite3"

EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
migrate_legacy_storage(
    _FORGE_ROOT / "experiments", EXPERIMENTS_DIR,
    ("experiments.json", "results.json", "experiments.sqlite3"),
)


# ==============================================================================
# 2. ENUMS (Strict Typing)
# ==============================================================================
class ExperimentStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class ExperimentDomain(str, Enum):
    ML_SUPERVISED = "ML_SUPERVISED"
    ORACLE_STRATEGY = "ORACLE_STRATEGY"
    CHRONICLE_MEMORY = "CHRONICLE_MEMORY"
    PROMPT_OPTIMIZATION = "PROMPT_OPTIMIZATION"
    WORKFLOW_OPTIMIZATION = "WORKFLOW_OPTIMIZATION"
    CONFIG_OPTIMIZATION = "CONFIG_OPTIMIZATION"
    EVALUATION_ONLY = "EVALUATION_ONLY"


class MetricDirection(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


# ==============================================================================
# 3. PYDANTIC MODELS (The Scientific Method)
# ==============================================================================
class ExperimentData(BaseModel):
    """
    Data identity and provenance.
    dataset_id/dataset_path tells Forge WHAT to load.
    data_fingerprint tells Forge WHAT EXACT DATA was used.
    """
    dataset_id: Optional[str] = None
    dataset_path: Optional[str] = None
    target_column: Optional[str] = None
    data_fingerprint: Optional[str] = None


class ExperimentEnvironment(BaseModel):
    """
    Configuration fingerprint: Hash of relevant experiment/training configuration.
    Environment fingerprint: Hash/identifier representing the execution environment.
    """
    configuration_fingerprint: Optional[str] = None
    environment_fingerprint: Optional[str] = None
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)


class MetricSpec(BaseModel):
    """Declares how a single metric should be evaluated."""
    direction: MetricDirection
    minimum_effect: float = Field(default=0.0, ge=0.0)

    @field_validator("minimum_effect", mode="before")
    @classmethod
    def _finite_minimum_effect(cls, value):
        normalized = finite_metric_value(value)
        if normalized is None or normalized < 0:
            raise ValueError("minimum_effect must be a finite, non-negative number")
        return normalized


class ExperimentEvaluation(BaseModel):
    """
    Defines WHAT we intend to measure and HOW to interpret it.
    The canonical representation is metric_specs.
    """
    metric_specs: Dict[str, MetricSpec] = Field(default_factory=dict)
    sample_requirement: Optional[int] = None
    minimum_samples: int = Field(default=30, ge=30)
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    bootstrap_replicates: int = Field(default=1000, ge=200, le=20000)
    bootstrap_block_length: Optional[int] = Field(default=None, ge=2)
    multiple_comparison_family_size: int = Field(default=1, ge=1, le=1000)
    comparison_family_id: Optional[str] = None
    comparison_family_members: List[str] = Field(default_factory=list)
    stopping_criteria: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("sample_requirement", mode="before")
    @classmethod
    def _validate_sample_requirement(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("sample_requirement must be a positive integer when supplied")
        return value

    @field_validator("bootstrap_block_length", mode="before")
    @classmethod
    def _validate_bootstrap_block_length(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ValueError("bootstrap_block_length must be an integer >= 2 when supplied")
        return value

    @model_validator(mode="after")
    def _validate_comparison_family(self):
        family_id = self.comparison_family_id
        members = self.comparison_family_members
        if family_id is not None and (
            not isinstance(family_id, str) or not family_id.strip()
        ):
            raise ValueError("comparison_family_id must be non-empty when supplied")
        if any(
            not isinstance(member, str) or not member.strip() or member != member.strip()
            for member in members
        ):
            raise ValueError("comparison family members must be non-empty candidate IDs")
        if len(members) != len(set(members)):
            raise ValueError("comparison family members must be unique")
        if members and family_id is None:
            raise ValueError("comparison family members require a comparison_family_id")
        if family_id is not None and len(members) != self.multiple_comparison_family_size:
            raise ValueError(
                "comparison family member count must match multiple_comparison_family_size"
            )
        return self

    @property
    def metrics(self) -> List[str]:
        """Read-only backward-compatible view of declared metric names."""
        return list(self.metric_specs.keys())


class ExperimentContract(BaseModel):
    """
    The immutable definition of an experiment.
    Created by the requesting agent (Oracle, Chronicle, etc.).
    """
    experiment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    hypothesis_id: Optional[str] = None
    domain: ExperimentDomain

    # Comparison: Baseline vs. Treatment
    baseline_config: Dict[str, Any]
    treatment_config: Dict[str, Any]

    # Variables & Ablation
    variables: Dict[str, Any] = Field(default_factory=dict)
    ablation_targets: List[str] = Field(default_factory=list)
    parent_experiment_id: Optional[str] = None

    # Structured sub-models
    data: ExperimentData
    environment: ExperimentEnvironment
    evaluation: ExperimentEvaluation

    # Provenance
    created_by: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExperimentRun(BaseModel):
    """
    A specific execution instance of an ExperimentContract.
    Allows the same experiment definition to be run multiple times.
    """
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    experiment_id: str
    forge_version: str = "2.0.0"
    environment_fingerprint: Optional[str] = None
    environment_snapshot: Dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    status: ExperimentStatus = ExperimentStatus.PENDING
    error_message: Optional[str] = None


class ExperimentResult(BaseModel):
    """
    The outcome of an ExperimentRun. First-class persistent object.
    Contains execution evidence, NOT evaluation conclusions.
    """
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    experiment_id: str
    status: ExperimentStatus
    metrics: Dict[str, Any] = Field(default_factory=dict)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunReconciliationAuthorization(BaseModel):
    """Aegis-signed authority to close one verified abandoned run.

    The claims must include a digest of the exact persisted RUNNING record and
    the operator's assertion that its worker process has terminated. This
    artifact cannot stop a worker; it only authorizes an audited ledger
    transition after external verification.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    experiment_id: str
    authorized_by: str
    decision: str = "AUTHORIZED"
    audience: str = "forge.operations"
    issuer: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    claims: Dict[str, Any]


# ==============================================================================
# 4. DOMAIN ADAPTER INTERFACE
# ==============================================================================
class ExperimentAdapter(Protocol):
    """
    Domain-specific adapters handle the actual execution logic.
    The ExperimentRunner remains generic and delegates to these adapters.
    Adapters collect evidence; they do NOT evaluate or compare results.
    """
    domain: ExperimentDomain

    def execute(self, contract: ExperimentContract, run: ExperimentRun) -> ExperimentResult:
        """Execute the experiment and return the result."""
        ...


class ExperimentCapacityExceeded(RuntimeError):
    """Raised before creating a run when shared execution capacity is full."""


# ==============================================================================
# 5. PERSISTENCE HELPERS
# ==============================================================================
def _load_json(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot safely read legacy experiment file {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Legacy experiment file {file_path} must contain a JSON object.")
    return data


def _save_json(file_path: Path, data: Dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=file_path.parent,
            prefix=f".{file_path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, file_path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def fingerprint_dataset_file(dataset_path: str | Path) -> tuple[str, str]:
    """Return a canonical absolute path and SHA-256 for an exact dataset file."""
    path = Path(dataset_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Dataset path is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as dataset_file:
        for chunk in iter(lambda: dataset_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return str(path), digest.hexdigest()


def _configuration_fingerprint(contract: ExperimentContract) -> str:
    contract_json = contract.model_dump(mode="json")
    payload = {
        "domain": contract_json["domain"],
        "baseline_config": contract_json["baseline_config"],
        "treatment_config": contract_json["treatment_config"],
        "variables": contract_json["variables"],
        "ablation_targets": contract_json["ablation_targets"],
        "data": {
            "dataset_id": contract_json["data"]["dataset_id"],
            "target_column": contract_json["data"]["target_column"],
            "data_fingerprint": contract_json["data"]["data_fingerprint"],
        },
        "random_seed": contract_json["environment"]["random_seed"],
        "expected_environment_fingerprint": contract_json["environment"]["environment_fingerprint"],
        "evaluation": contract_json["evaluation"],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_contract(contract: ExperimentContract) -> ExperimentContract:
    """Freeze data identity and configuration provenance before persistence."""
    data = contract.data.model_copy(deep=True)
    if contract.domain == ExperimentDomain.ML_SUPERVISED and not data.dataset_path:
        raise ValueError("ML experiments require a dataset_path so Forge can fingerprint exact input data")
    if data.dataset_path:
        canonical_path, actual_fingerprint = fingerprint_dataset_file(data.dataset_path)
        if data.data_fingerprint and data.data_fingerprint != actual_fingerprint:
            raise ValueError("Supplied dataset fingerprint does not match the dataset file")
        data.dataset_path = canonical_path
        data.data_fingerprint = actual_fingerprint
    prepared = contract.model_copy(deep=True, update={"data": data})
    expected_configuration_fingerprint = _configuration_fingerprint(prepared)
    configured_fingerprint = prepared.environment.configuration_fingerprint
    if configured_fingerprint and configured_fingerprint != expected_configuration_fingerprint:
        raise ValueError("Supplied configuration fingerprint does not match the experiment contract")
    environment = prepared.environment.model_copy(deep=True, update={
        "configuration_fingerprint": expected_configuration_fingerprint
    })
    prepared = prepared.model_copy(update={"environment": environment})
    return prepared


class ExperimentLedger:
    """SQLite source of truth for experiment contracts, runs, and results.

    The JSON files remain compatibility snapshots for older diagnostics. They
    are never read after the one-time migration and are not authoritative.
    """

    SCHEMA_VERSION = "3"

    def __init__(self, database_path: Path, experiments_file: Path, results_file: Path):
        self.database_path = Path(database_path)
        self.experiments_file = Path(experiments_file)
        self.results_file = Path(results_file)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _encode(model: BaseModel) -> str:
        return json.dumps(
            model.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    changed_at TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    previous_limit INTEGER NOT NULL,
                    new_limit INTEGER NOT NULL,
                    active_runs INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    contract_digest TEXT NOT NULL,
                    contract_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    run_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_experiment_started
                    ON runs(experiment_id, started_at);
                CREATE TABLE IF NOT EXISTS results (
                    result_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_results_experiment
                    ON results(experiment_id);
                CREATE TABLE IF NOT EXISTS evaluation_reports (
                    report_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                    result_id TEXT NOT NULL UNIQUE REFERENCES results(result_id),
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_holdout_families (
                    family_id TEXT PRIMARY KEY,
                    holdout_key TEXT NOT NULL UNIQUE,
                    dataset_fingerprint TEXT NOT NULL,
                    partition_fingerprint TEXT NOT NULL,
                    members_json TEXT NOT NULL,
                    evaluated_members_json TEXT NOT NULL,
                    first_experiment_id TEXT NOT NULL,
                    first_run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_holdout_uses (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    family_id TEXT,
                    holdout_key TEXT,
                    candidate_id TEXT,
                    disposition TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_holdout_uses_disposition
                    ON evaluation_holdout_uses(disposition, recorded_at);
            """)
            check = connection.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"Experiment ledger integrity check failed: {check}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if version is not None and version[0] not in ("1", "2", self.SCHEMA_VERSION):
                    raise RuntimeError(
                        f"Unsupported experiment ledger schema version: {version[0]}"
                    )
                if version is None:
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                        (self.SCHEMA_VERSION,),
                    )
                elif version[0] != self.SCHEMA_VERSION:
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                        (self.SCHEMA_VERSION,),
                    )
                migrated = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'legacy_json_migrated'"
                ).fetchone()
                if migrated is None:
                    self._migrate_legacy_json(connection)
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES('legacy_json_migrated', '1')"
                    )
                if version is None or version[0] in ("1", "2"):
                    legacy_completed = connection.execute(
                        "SELECT run_id, result_json FROM results WHERE status = ?",
                        (ExperimentStatus.COMPLETED.value,),
                    ).fetchall()
                    for row in legacy_completed:
                        result = ExperimentResult.model_validate_json(row["result_json"])
                        connection.execute(
                            "INSERT OR IGNORE INTO evaluation_holdout_uses "
                            "(run_id, family_id, holdout_key, candidate_id, disposition, recorded_at) "
                            "VALUES (?, NULL, NULL, NULL, 'legacy_untracked', ?)",
                            (row["run_id"], result.timestamp.isoformat()),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def configure_capacity(self, maximum: int) -> int:
        """Pin and return one process-shared execution limit in the ledger."""
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 256:
            raise ValueError("maximum must be an integer from 1 through 256")
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'max_total_concurrent_runs'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES('max_total_concurrent_runs', ?)",
                        (str(maximum),),
                    )
                    configured = maximum
                elif row["value"] != str(maximum):
                    raise RuntimeError(
                        "Experiment ledger execution limit is already configured as "
                        f"{row['value']}; all Forge processes sharing this ledger must use "
                        f"the same max_total_concurrent_runs value ({maximum} requested)."
                    )
                else:
                    configured = int(row["value"])
                connection.commit()
                return configured
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def reconfigure_capacity(
        database_path: str | Path,
        maximum: int,
        reason: str,
        operator: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Change shared capacity under the run-claim lock and append an audit event."""
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 256:
            raise ValueError("maximum must be an integer from 1 through 256")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a non-empty reason is required for capacity changes")
        operator = operator or getpass.getuser()
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator identity must be non-empty")
        path = Path(database_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Experiment ledger does not exist: {path}")

        with closing(ExperimentLedger._connect(path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"Experiment ledger integrity check failed: {integrity}")
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if version is None or version[0] != ExperimentLedger.SCHEMA_VERSION:
                    raise RuntimeError("Experiment ledger schema is missing or unsupported")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS capacity_events ("
                    "event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "changed_at TEXT NOT NULL, operator TEXT NOT NULL, reason TEXT NOT NULL, "
                    "previous_limit INTEGER NOT NULL, new_limit INTEGER NOT NULL, "
                    "active_runs INTEGER NOT NULL)"
                )
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'max_total_concurrent_runs'"
                ).fetchone()
                if row is None:
                    raise RuntimeError("Experiment ledger has no configured shared capacity")
                previous = int(row["value"])
                active = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE status = ?",
                    (ExperimentStatus.RUNNING.value,),
                ).fetchone()[0]
                if active > maximum:
                    raise ExperimentCapacityExceeded(
                        f"Cannot lower shared capacity to {maximum}; {active} durable runs are RUNNING."
                    )
                if previous == maximum:
                    connection.commit()
                    return {
                        "changed": False,
                        "previous_limit": previous,
                        "new_limit": maximum,
                        "active_runs": active,
                    }
                changed_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'max_total_concurrent_runs'",
                    (str(maximum),),
                )
                connection.execute(
                    "INSERT INTO capacity_events "
                    "(changed_at, operator, reason, previous_limit, new_limit, active_runs) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (changed_at, operator.strip(), reason.strip(), previous, maximum, active),
                )
                connection.commit()
                return {
                    "changed": True,
                    "changed_at": changed_at,
                    "operator": operator.strip(),
                    "reason": reason.strip(),
                    "previous_limit": previous,
                    "new_limit": maximum,
                    "active_runs": active,
                }
            except Exception:
                connection.rollback()
                raise

    def _migrate_legacy_json(self, connection: sqlite3.Connection) -> None:
        legacy_experiments = _load_json(self.experiments_file)
        legacy_results = _load_json(self.results_file)
        for key, raw in legacy_experiments.items():
            contract = ExperimentContract.model_validate(raw)
            if key != contract.experiment_id:
                raise RuntimeError(f"Legacy experiment key does not match {contract.experiment_id}")
            encoded = self._encode(contract)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO experiments(experiment_id, contract_digest, contract_json) VALUES(?, ?, ?)",
                (contract.experiment_id, digest, encoded),
            )

        for key, raw in legacy_results.items():
            result = ExperimentResult.model_validate(raw)
            if key != result.result_id:
                raise RuntimeError(f"Legacy result key does not match {result.result_id}")
            if connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id = ?", (result.experiment_id,)
            ).fetchone() is None:
                raise RuntimeError(
                    f"Legacy result {result.result_id} references a missing experiment"
                )
            completed = result.timestamp
            run = ExperimentRun(
                run_id=result.run_id,
                experiment_id=result.experiment_id,
                started_at=completed,
                completed_at=completed,
                status=result.status,
                error_message=result.error_message,
            )
            run_json = self._encode(run)
            result_json = self._encode(result)
            connection.execute(
                "INSERT INTO runs(run_id, experiment_id, status, started_at, run_json) VALUES(?, ?, ?, ?, ?)",
                (run.run_id, run.experiment_id, run.status.value, run.started_at.isoformat(), run_json),
            )
            connection.execute(
                "INSERT INTO results(result_id, run_id, experiment_id, status, result_json) VALUES(?, ?, ?, ?, ?)",
                (result.result_id, result.run_id, result.experiment_id, result.status.value, result_json),
            )

    def load_all(self):
        with closing(self._connect(self.database_path)) as connection:
            experiments = {
                row["experiment_id"]: ExperimentContract.model_validate_json(row["contract_json"])
                for row in connection.execute("SELECT experiment_id, contract_json FROM experiments")
            }
            runs = {
                row["run_id"]: ExperimentRun.model_validate_json(row["run_json"])
                for row in connection.execute("SELECT run_id, run_json FROM runs")
            }
            results = {
                row["result_id"]: ExperimentResult.model_validate_json(row["result_json"])
                for row in connection.execute("SELECT result_id, result_json FROM results")
            }
            from Forge.core.evaluator import EvaluationReport
            evaluations = {
                row["report_id"]: EvaluationReport.model_validate_json(row["report_json"])
                for row in connection.execute("SELECT report_id, report_json FROM evaluation_reports")
            }
        return experiments, runs, results, evaluations

    def record_evaluation(self, report):
        """Persist one immutable evaluation report for a completed run."""
        from Forge.core.evaluator import EvaluationReport
        if not isinstance(report, EvaluationReport):
            raise TypeError("report must be an EvaluationReport")
        encoded = self._encode(report)
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    "SELECT experiment_id, status FROM results WHERE result_id = ? AND run_id = ?",
                    (report.result_id, report.run_id),
                ).fetchone()
                if (
                    result is None
                    or result["experiment_id"] != report.experiment_id
                    or result["status"] != ExperimentStatus.COMPLETED.value
                ):
                    raise ValueError("Evaluation report provenance does not match a completed persisted result")
                existing = connection.execute(
                    "SELECT report_json FROM evaluation_reports WHERE run_id = ?",
                    (report.run_id,),
                ).fetchone()
                if existing is not None:
                    stored = EvaluationReport.model_validate_json(existing["report_json"])
                    if stored.result_id != report.result_id:
                        raise RuntimeError("Run already has an evaluation for different result evidence")
                    connection.commit()
                    return stored
                connection.execute(
                    "INSERT INTO evaluation_reports "
                    "(report_id, experiment_id, run_id, result_id, report_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (report.result_id, report.experiment_id, report.run_id,
                     report.result_id, encoded, report.timestamp.isoformat()),
                )
                connection.commit()
                return report.model_copy(deep=True)
            except Exception:
                connection.rollback()
                raise

    def get_evaluation_for_run(self, run_id: str):
        from Forge.core.evaluator import EvaluationReport
        with closing(self._connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT report_json FROM evaluation_reports WHERE run_id = ?", (run_id,)
            ).fetchone()
        return EvaluationReport.model_validate_json(row["report_json"]) if row else None

    def submit(self, contract: ExperimentContract) -> ExperimentContract:
        encoded = self._encode(contract)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT contract_digest, contract_json FROM experiments WHERE experiment_id = ?",
                    (contract.experiment_id,),
                ).fetchone()
                if row is not None:
                    if row["contract_digest"] != digest or row["contract_json"] != encoded:
                        raise ValueError(
                            f"Experiment ID {contract.experiment_id} already exists with different content"
                        )
                    connection.commit()
                    return ExperimentContract.model_validate_json(row["contract_json"])
                connection.execute(
                    "INSERT INTO experiments(experiment_id, contract_digest, contract_json) VALUES(?, ?, ?)",
                    (contract.experiment_id, digest, encoded),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return contract.model_copy(deep=True)

    def begin_run(
        self,
        experiment_id: str,
        environment_fingerprint: Optional[str] = None,
        environment_snapshot: Optional[Dict[str, Any]] = None,
    ) -> ExperimentRun:
        run = ExperimentRun(
            experiment_id=experiment_id,
            status=ExperimentStatus.RUNNING,
            environment_fingerprint=environment_fingerprint,
            environment_snapshot=environment_snapshot or {},
        )
        encoded = self._encode(run)
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM experiments WHERE experiment_id = ?", (experiment_id,)
                ).fetchone() is None:
                    raise ValueError(f"Experiment {experiment_id} not found.")
                capacity_row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'max_total_concurrent_runs'"
                ).fetchone()
                if capacity_row is None:
                    raise RuntimeError("Experiment ledger shared execution capacity is not configured")
                shared_limit = int(capacity_row["value"])
                active = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE status = ?",
                    (ExperimentStatus.RUNNING.value,),
                ).fetchone()[0]
                if active >= shared_limit:
                    raise ExperimentCapacityExceeded(
                        "Forge's shared experiment execution limit is full "
                        f"({active}/{shared_limit} durable runs are RUNNING); "
                        "retry after a run finishes or is safely reconciled."
                    )
                connection.execute(
                    "INSERT INTO runs(run_id, experiment_id, status, started_at, run_json) VALUES(?, ?, ?, ?, ?)",
                    (run.run_id, run.experiment_id, run.status.value, run.started_at.isoformat(), encoded),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return run

    def complete_run(self, run: ExperimentRun, result: ExperimentResult):
        run_json = self._encode(run)
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, experiment_id, run_json FROM runs WHERE run_id = ?",
                    (run.run_id,),
                ).fetchone()
                if row is None or row["status"] != ExperimentStatus.RUNNING.value:
                    raise RuntimeError(f"Run {run.run_id} is missing or is no longer running")
                persisted_run = ExperimentRun.model_validate_json(row["run_json"])
                if persisted_run.status != ExperimentStatus.RUNNING:
                    raise RuntimeError("Run JSON and ledger status disagree")
                if row["experiment_id"] != run.experiment_id or result.run_id != run.run_id or result.experiment_id != run.experiment_id:
                    raise ValueError("Run and result provenance do not match")
                immutable_fields = (
                    "run_id", "experiment_id", "forge_version", "environment_fingerprint",
                    "environment_snapshot", "started_at",
                )
                if any(
                    getattr(persisted_run, field) != getattr(run, field)
                    for field in immutable_fields
                ):
                    raise ValueError("Run identity or execution provenance changed before completion")
                if run.status not in (ExperimentStatus.COMPLETED, ExperimentStatus.FAILED, ExperimentStatus.ABORTED):
                    raise ValueError("A run can only be completed with a terminal status")
                if result.status != run.status:
                    raise ValueError("Run and result statuses must match")
                if run.completed_at is None:
                    raise ValueError("A terminal run must record completed_at")
                timestamps = (persisted_run.started_at, run.completed_at, result.timestamp)
                if any(ts.tzinfo is None or ts.utcoffset() is None for ts in timestamps):
                    raise ValueError("Run and result timestamps must be timezone-aware")
                if run.completed_at < persisted_run.started_at:
                    raise ValueError("Run completion cannot precede its start")
                if result.timestamp > run.completed_at:
                    raise ValueError("Result timestamp cannot follow run completion")
                if result.error_message != run.error_message:
                    raise ValueError("Run and result error details must match")
                if result.status == ExperimentStatus.COMPLETED:
                    contract_row = connection.execute(
                        "SELECT contract_json FROM experiments WHERE experiment_id = ?",
                        (run.experiment_id,),
                    ).fetchone()
                    if contract_row is None:
                        raise RuntimeError("Experiment contract disappeared before result commit")
                    contract = ExperimentContract.model_validate_json(
                        contract_row["contract_json"]
                    )
                    result = self._record_holdout_use(connection, contract, run, result)
                result_json = self._encode(result)
                connection.execute(
                    "UPDATE runs SET status = ?, run_json = ? WHERE run_id = ?",
                    (run.status.value, run_json, run.run_id),
                )
                connection.execute(
                    "INSERT INTO results(result_id, run_id, experiment_id, status, result_json) VALUES(?, ?, ?, ?, ?)",
                    (result.result_id, result.run_id, result.experiment_id, result.status.value, result_json),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return run.model_copy(deep=True), result.model_copy(deep=True)

    def _record_holdout_use(
        self,
        connection: sqlite3.Connection,
        contract: ExperimentContract,
        run: ExperimentRun,
        result: ExperimentResult,
    ) -> ExperimentResult:
        """Bind a completed evaluation to a single-use holdout family."""
        evidence = dict(result.model_dump(mode="json").get("evidence") or {})
        partition = evidence.get("evaluation_partition")
        dataset_fingerprint = (
            evidence.get("dataset_fingerprint") or contract.data.data_fingerprint
        )
        family = contract.evaluation
        family_id = family.comparison_family_id or contract.experiment_id
        candidate_id = (
            contract.variables.get("candidate_id")
            or contract.variables.get("candidate_method")
        )
        members = list(family.comparison_family_members)
        if candidate_id is None and family.multiple_comparison_family_size == 1:
            candidate_id = members[0] if members else contract.experiment_id
        if not members and family.multiple_comparison_family_size == 1:
            members = [candidate_id]
        disposition = "untracked"
        holdout_key = None
        partition_fingerprint = None
        holdout_fingerprint = (
            partition.get("holdout_fingerprint")
            or partition.get("evaluation_suffix_fingerprint")
            if isinstance(partition, dict) else None
        )
        holdout_fingerprint_valid = (
            isinstance(holdout_fingerprint, str)
            and len(holdout_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in holdout_fingerprint.lower())
        )
        dataset_valid = (
            isinstance(dataset_fingerprint, str)
            and len(dataset_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in dataset_fingerprint.lower())
        )
        if isinstance(partition, dict) and partition:
            partition_fingerprint = hashlib.sha256(
                json.dumps(
                    partition,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        legacy_holdout_key = None
        legacy_partition_fingerprint = None
        if dataset_valid and partition_fingerprint is not None:
            legacy_partition = dict(partition) if isinstance(partition, dict) else {}
            legacy_partition.pop("holdout_fingerprint", None)
            legacy_partition_fingerprint = hashlib.sha256(
                json.dumps(
                    legacy_partition,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            legacy_holdout_key = hashlib.sha256(
                f"{dataset_fingerprint.lower()}:{legacy_partition_fingerprint}".encode("ascii")
            ).hexdigest()
        if dataset_valid and partition_fingerprint is not None and holdout_fingerprint_valid:
            holdout_key = hashlib.sha256(
                f"evaluation-holdout-v1:{holdout_fingerprint.lower()}".encode("ascii")
            ).hexdigest()
            candidate_binding_valid = (
                candidate_id in members
                if family.multiple_comparison_family_size > 1
                else candidate_id is None or candidate_id in members
            )
            family_scope_valid = (
                len(members) == family.multiple_comparison_family_size
                and len(set(members)) == len(members)
                and all(isinstance(member, str) and member.strip() for member in members)
                and isinstance(family_id, str)
                and bool(family_id.strip())
                and candidate_binding_valid
            )
            if not family_scope_valid:
                disposition = "invalid_family_scope"
            else:
                family_row = connection.execute(
                    "SELECT * FROM evaluation_holdout_families "
                    "WHERE family_id = ? OR holdout_key IN (?, ?)",
                    (family_id, holdout_key, legacy_holdout_key),
                ).fetchall()
                members_json = self._encode_json(members)
                if (
                    len(family_row) == 1
                    and family_row[0]["family_id"] == family_id
                    and family_row[0]["holdout_key"] == legacy_holdout_key
                ):
                    # Continue an existing v3 family using its original key format.
                    holdout_key = legacy_holdout_key
                matching = (
                    len(family_row) == 1
                    and family_row[0]["family_id"] == family_id
                    and family_row[0]["holdout_key"] in {holdout_key, legacy_holdout_key}
                    and family_row[0]["members_json"] == members_json
                )
                if not family_row:
                    now = datetime.now(timezone.utc).isoformat()
                    connection.execute(
                        "INSERT INTO evaluation_holdout_families "
                        "(family_id, holdout_key, dataset_fingerprint, partition_fingerprint, "
                        "members_json, evaluated_members_json, first_experiment_id, first_run_id, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (family_id, holdout_key, dataset_fingerprint.lower(),
                         partition_fingerprint, members_json,
                        self._encode_json([candidate_id]),
                         contract.experiment_id, run.run_id, now, now),
                    )
                    disposition = "unique_family"
                elif matching:
                    evaluated = json.loads(family_row[0]["evaluated_members_json"])
                    member_key = candidate_id
                    if member_key in evaluated:
                        disposition = "repeated_candidate"
                    else:
                        evaluated.append(member_key)
                        evaluated.sort()
                        connection.execute(
                            "UPDATE evaluation_holdout_families SET evaluated_members_json = ?, "
                            "updated_at = ? WHERE family_id = ?",
                            (self._encode_json(evaluated),
                             datetime.now(timezone.utc).isoformat(), family_id),
                        )
                        disposition = "unique_family"
                elif any(row["holdout_key"] == holdout_key for row in family_row):
                    disposition = "reused_holdout"
                elif any(row["holdout_key"] == legacy_holdout_key for row in family_row):
                    disposition = "reused_holdout"
                else:
                    disposition = "family_binding_mismatch"

        connection.execute(
            "INSERT INTO evaluation_holdout_uses "
            "(run_id, family_id, holdout_key, candidate_id, disposition, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run.run_id, family_id, holdout_key, candidate_id, disposition,
             datetime.now(timezone.utc).isoformat()),
        )
        evidence["evaluation_holdout"] = {
            "family_id": family_id,
            "candidate_id": candidate_id,
            "members": members,
            "dataset_fingerprint": dataset_fingerprint.lower()
            if dataset_valid else None,
            "partition_fingerprint": partition_fingerprint,
            "legacy_partition_fingerprint": legacy_partition_fingerprint
            if legacy_partition_fingerprint is not None else None,
            "holdout_fingerprint": holdout_fingerprint.lower()
            if holdout_fingerprint_valid else None,
            "key_version": (
                "legacy_dataset_partition_v3"
                if holdout_key is not None and holdout_key == legacy_holdout_key
                else "content_v1" if holdout_key is not None else None
            ),
            "disposition": disposition,
        }
        return result.model_copy(update={"evidence": evidence})

    @staticmethod
    def _encode_json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def get_experiment(self, experiment_id: str) -> Optional[ExperimentContract]:
        with closing(self._connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT contract_json FROM experiments WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        return ExperimentContract.model_validate_json(row[0]) if row else None

    def get_run(self, run_id: str) -> Optional[ExperimentRun]:
        with closing(self._connect(self.database_path)) as connection:
            row = connection.execute("SELECT run_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return ExperimentRun.model_validate_json(row[0]) if row else None

    def list_runs(self, experiment_id: str) -> List[ExperimentRun]:
        with closing(self._connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT run_json FROM runs WHERE experiment_id = ? ORDER BY started_at, run_id",
                (experiment_id,),
            ).fetchall()
        return [ExperimentRun.model_validate_json(row[0]) for row in rows]

    def get_result(self, result_id: str) -> Optional[ExperimentResult]:
        with closing(self._connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT result_json FROM results WHERE result_id = ?", (result_id,)
            ).fetchone()
        return ExperimentResult.model_validate_json(row[0]) if row else None

    def get_result_for_run(self, run_id: str) -> Optional[ExperimentResult]:
        with closing(self._connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT result_json FROM results WHERE run_id = ?", (run_id,)
            ).fetchone()
        return ExperimentResult.model_validate_json(row[0]) if row else None

    def stats(self) -> Dict[str, Any]:
        with closing(self._connect(self.database_path)) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            capacity_limit = self._capacity_limit(connection)
            capacity_policy_ready = (
                isinstance(capacity_limit, int)
                and not isinstance(capacity_limit, bool)
                and 1 <= capacity_limit <= 256
            )
            running_rows = connection.execute(
                "SELECT run_json FROM runs WHERE status = ? ORDER BY started_at, run_id",
                (ExperimentStatus.RUNNING.value,),
            ).fetchall()
            running_runs = []
            now = datetime.now(timezone.utc)
            for row in running_rows:
                run = ExperimentRun.model_validate_json(row["run_json"])
                started_at = run.started_at
                if started_at.tzinfo is None or started_at.utcoffset() is None:
                    raise ValueError(f"Run {run.run_id} has a timezone-naive start timestamp")
                running_runs.append({
                    "run_id": run.run_id,
                    "experiment_id": run.experiment_id,
                    "started_at": started_at.isoformat(),
                    "elapsed_seconds": max(0, int((now - started_at).total_seconds())),
                })
            capacity_changes = connection.execute(
                "SELECT changed_at, operator, reason, previous_limit, new_limit, active_runs "
                "FROM capacity_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            capacity_change_count = connection.execute(
                "SELECT COUNT(*) FROM capacity_events"
            ).fetchone()[0]
            holdout_families = connection.execute(
                "SELECT COUNT(*) FROM evaluation_holdout_families"
            ).fetchone()[0]
            holdout_use_rows = connection.execute(
                "SELECT disposition, COUNT(*) AS count FROM evaluation_holdout_uses "
                "GROUP BY disposition"
            ).fetchall()
            experiment_count = connection.execute(
                "SELECT COUNT(*) FROM experiments"
            ).fetchone()[0]
            run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            result_count = connection.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            evaluation_count = connection.execute(
                "SELECT COUNT(*) FROM evaluation_reports"
            ).fetchone()[0]
        consistency = self.verify_state_consistency()
        return {
            "database_integrity": integrity == "ok",
            "capacity_policy_ready": capacity_policy_ready,
            "experiments": experiment_count,
            "runs": run_count,
            "results": result_count,
            "evaluation_reports": evaluation_count,
            "holdout_families": holdout_families,
            "holdout_uses_by_disposition": {
                row["disposition"]: row["count"] for row in holdout_use_rows
            },
            "running": len(running_runs),
            "configured_max_concurrent_runs": capacity_limit,
            "capacity_change_count": capacity_change_count,
            "last_capacity_change": dict(capacity_changes) if capacity_changes else None,
            # RUNNING records may belong to live workers or to a process
            # that exited unexpectedly. Report identifiers and age so
            # operators can distinguish and investigate them; do not infer
            # that an old timestamp means the underlying work was cancelled.
            "running_runs": running_runs,
            "state_consistency": consistency,
            "ready": integrity == "ok" and capacity_policy_ready
            and consistency.get("intact") is True,
        }

    def verify_state_consistency(self) -> Dict[str, Any]:
        """Check holdout family reservations against completed result evidence."""
        try:
            with closing(self._connect(self.database_path)) as connection:
                families = connection.execute(
                    "SELECT family_id, holdout_key, dataset_fingerprint, "
                    "partition_fingerprint, members_json, evaluated_members_json, "
                    "first_run_id, created_at, updated_at FROM evaluation_holdout_families "
                    "ORDER BY family_id"
                ).fetchall()
                uses = connection.execute(
                    "SELECT u.run_id, u.family_id, u.holdout_key, u.candidate_id, "
                    "u.disposition, r.status, r.result_json "
                    "FROM evaluation_holdout_uses u "
                    "LEFT JOIN results r USING(run_id) ORDER BY u.run_id"
                ).fetchall()
                completed_count = connection.execute(
                    "SELECT COUNT(*) FROM results WHERE status = ?",
                    (ExperimentStatus.COMPLETED.value,),
                ).fetchone()[0]
            family_by_id = {row["family_id"]: row for row in families}
            family_use_members: Dict[str, set[str]] = {}
            for row in families:
                try:
                    members = json.loads(row["members_json"])
                    evaluated = json.loads(row["evaluated_members_json"])
                    created = datetime.fromisoformat(row["created_at"])
                    updated = datetime.fromisoformat(row["updated_at"])
                except Exception:
                    return {
                        "intact": False,
                        "issue": "holdout_family_invalid",
                        "family_id": row["family_id"],
                    }
                first_result_row = connection.execute(
                    "SELECT result_json FROM results WHERE run_id = ?",
                    (row["first_run_id"],),
                ).fetchone()
                first_holdout_fingerprint = None
                first_key_version = None
                if first_result_row is not None:
                    try:
                        first_result = ExperimentResult.model_validate_json(
                            first_result_row["result_json"]
                        )
                        first_holdout = first_result.evidence.get("evaluation_holdout")
                        if isinstance(first_holdout, dict):
                            first_holdout_fingerprint = first_holdout.get("holdout_fingerprint")
                            first_key_version = first_holdout.get("key_version")
                    except Exception:
                        first_holdout_fingerprint = None
                if (
                    first_key_version == "content_v1"
                    and isinstance(first_holdout_fingerprint, str)
                    and len(first_holdout_fingerprint) == 64
                    and all(char in "0123456789abcdef" for char in first_holdout_fingerprint.lower())
                ):
                    expected_key = hashlib.sha256(
                        f"evaluation-holdout-v1:{first_holdout_fingerprint.lower()}".encode(
                            "ascii"
                        )
                    ).hexdigest()
                else:
                    expected_key = hashlib.sha256(
                        f"{row['dataset_fingerprint']}:{row['partition_fingerprint']}".encode(
                            "ascii"
                        )
                    ).hexdigest()
                if (
                    row["holdout_key"] != expected_key
                    or len(row["dataset_fingerprint"]) != 64
                    or len(row["partition_fingerprint"]) != 64
                    or not isinstance(members, list)
                    or not members
                    or any(not isinstance(item, str) or not item.strip() for item in members)
                    or len(members) != len(set(members))
                    or not isinstance(evaluated, list)
                    or len(evaluated) != len(set(evaluated))
                    or any(item not in members for item in evaluated)
                    or created.tzinfo is None or created.utcoffset() is None
                    or updated.tzinfo is None or updated.utcoffset() is None
                ):
                    return {
                        "intact": False,
                        "issue": "holdout_family_binding_mismatch",
                        "family_id": row["family_id"],
                    }
                family_use_members[row["family_id"]] = set()

            allowed_dispositions = {
                "unique_family", "repeated_candidate", "reused_holdout",
                "family_binding_mismatch", "invalid_family_scope", "untracked",
                "legacy_untracked",
            }
            for row in uses:
                if row["status"] != ExperimentStatus.COMPLETED.value:
                    return {
                        "intact": False,
                        "issue": "holdout_use_without_completed_result",
                        "run_id": row["run_id"],
                    }
                if row["disposition"] not in allowed_dispositions:
                    return {
                        "intact": False,
                        "issue": "holdout_disposition_invalid",
                        "run_id": row["run_id"],
                    }
                if row["disposition"] == "legacy_untracked":
                    continue
                try:
                    result = ExperimentResult.model_validate_json(row["result_json"])
                    holdout = result.evidence.get("evaluation_holdout")
                except Exception:
                    holdout = None
                if (
                    not isinstance(holdout, dict)
                    or holdout.get("disposition") != row["disposition"]
                    or holdout.get("family_id") != row["family_id"]
                    or holdout.get("candidate_id") != row["candidate_id"]
                ):
                    return {
                        "intact": False,
                        "issue": "holdout_use_result_mismatch",
                        "run_id": row["run_id"],
                    }
                if row["holdout_key"] is not None:
                    dataset_fingerprint = holdout.get("dataset_fingerprint")
                    partition_fingerprint = holdout.get("partition_fingerprint")
                    legacy_partition_fingerprint = holdout.get(
                        "legacy_partition_fingerprint"
                    ) or partition_fingerprint
                    evidence_holdout_fingerprint = holdout.get("holdout_fingerprint")
                    if holdout.get("key_version") == "content_v1":
                        if not isinstance(evidence_holdout_fingerprint, str) or (
                            len(evidence_holdout_fingerprint) != 64
                            or not all(char in "0123456789abcdef" for char in evidence_holdout_fingerprint.lower())
                        ):
                            expected_use_key = None
                        else:
                            expected_use_key = hashlib.sha256(
                                f"evaluation-holdout-v1:{evidence_holdout_fingerprint.lower()}".encode(
                                    "ascii"
                                )
                            ).hexdigest()
                    elif holdout.get("key_version") in {
                        None, "legacy_dataset_partition_v3"
                    } and isinstance(dataset_fingerprint, str) and isinstance(
                        legacy_partition_fingerprint, str
                    ):
                        expected_use_key = hashlib.sha256(
                            f"{dataset_fingerprint.lower()}:{legacy_partition_fingerprint}".encode(
                                "ascii"
                            )
                        ).hexdigest()
                    else:
                        expected_use_key = None
                    if expected_use_key != row["holdout_key"]:
                        return {
                            "intact": False,
                            "issue": "holdout_use_key_mismatch",
                            "run_id": row["run_id"],
                        }
                if row["disposition"] in {"unique_family", "repeated_candidate"}:
                    family = family_by_id.get(row["family_id"])
                    if (
                        family is None
                        or family["holdout_key"] != row["holdout_key"]
                        or row["candidate_id"] not in json.loads(family["members_json"])
                        or row["candidate_id"] not in json.loads(family["evaluated_members_json"])
                        or holdout.get("members") != json.loads(family["members_json"])
                    ):
                        return {
                            "intact": False,
                            "issue": "holdout_use_family_mismatch",
                            "run_id": row["run_id"],
                        }
                    family_use_members[row["family_id"]].add(row["candidate_id"])

            for family_id, row in family_by_id.items():
                if family_use_members[family_id] != set(
                    json.loads(row["evaluated_members_json"])
                ):
                    return {
                        "intact": False,
                        "issue": "holdout_family_member_history_mismatch",
                        "family_id": family_id,
                    }

            if completed_count != len(uses):
                return {
                    "intact": False,
                    "issue": "completed_result_holdout_use_count_mismatch",
                    "completed_results": completed_count,
                    "holdout_uses": len(uses),
                }
            return {
                "intact": True,
                "holdout_families": len(families),
                "completed_results_tracked": completed_count,
            }
        except Exception as exc:
            log.exception("experiment evaluation ledger consistency check failed")
            return {"intact": False, "issue": type(exc).__name__}

    @staticmethod
    def _capacity_limit(connection: sqlite3.Connection) -> Optional[int]:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'max_total_concurrent_runs'"
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def export_compatibility_snapshots(self) -> None:
        with closing(self._connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                experiments = {
                    row["experiment_id"]: json.loads(row["contract_json"])
                    for row in connection.execute("SELECT experiment_id, contract_json FROM experiments")
                }
                results = {
                    row["result_id"]: json.loads(row["result_json"])
                    for row in connection.execute("SELECT result_id, result_json FROM results")
                }
                # Keep competing writers out until both exports reflect the
                # same committed database snapshot.
                _save_json(self.experiments_file, experiments)
                _save_json(self.results_file, results)
                connection.commit()
            except Exception:
                connection.rollback()
                log.exception("Unable to export experiment compatibility snapshots")


# ==============================================================================
# 6. ORCHESTRATOR (ExperimentRunner)
# ==============================================================================
class ExperimentRunner:
    """
    The generic orchestrator. Accepts contracts, creates runs,
    delegates to domain adapters, and persists results.
    """
    def __init__(
        self,
        reconciliation_verifier=None,
        max_concurrent_runs: int = 2,
        max_total_concurrent_runs: int = 2,
    ):
        if (
            isinstance(max_concurrent_runs, bool)
            or not isinstance(max_concurrent_runs, int)
            or not 1 <= max_concurrent_runs <= 64
        ):
            raise ValueError("max_concurrent_runs must be an integer from 1 through 64")
        if (
            isinstance(max_total_concurrent_runs, bool)
            or not isinstance(max_total_concurrent_runs, int)
            or not 1 <= max_total_concurrent_runs <= 256
        ):
            raise ValueError("max_total_concurrent_runs must be an integer from 1 through 256")
        self.adapters: Dict[ExperimentDomain, ExperimentAdapter] = {}
        self.experiments: Dict[str, ExperimentContract] = {}
        self.runs: Dict[str, ExperimentRun] = {}
        self.results: Dict[str, ExperimentResult] = {}
        self.evaluation_reports: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._max_concurrent_runs = max_concurrent_runs
        self._run_capacity = threading.BoundedSemaphore(max_concurrent_runs)
        self._active_run_count = 0
        self._capacity_rejections = 0
        self._reconciliation_verifier = reconciliation_verifier
        self._ledger = ExperimentLedger(
            database_path=EXPERIMENTS_FILE.with_suffix(".sqlite3"),
            experiments_file=EXPERIMENTS_FILE,
            results_file=RESULTS_FILE,
        )
        self._ledger.configure_capacity(max_total_concurrent_runs)
        self._load_state()

    def _load_state(self):
        (self.experiments, self.runs, self.results,
         self.evaluation_reports) = self._ledger.load_all()

    def set_reconciliation_verifier(self, verifier) -> None:
        """Inject the trusted operator verifier used only for run reconciliation."""
        if verifier is None:
            raise ValueError("A trusted reconciliation verifier is required")
        with self._lock:
            if (
                self._reconciliation_verifier is not None
                and self._reconciliation_verifier is not verifier
            ):
                raise RuntimeError("A reconciliation verifier is already configured")
            self._reconciliation_verifier = verifier

    @property
    def reconciliation_authorization_configured(self) -> bool:
        return self._reconciliation_verifier is not None

    def register_adapter(self, adapter: ExperimentAdapter):
        """Register a domain-specific adapter (e.g., MLExperimentAdapter)."""
        with self._lock:
            if adapter.domain in self.adapters:
                raise ValueError(f"Adapter for domain {adapter.domain} already registered.")
            self.adapters[adapter.domain] = adapter

    def submit_experiment(self, contract: ExperimentContract) -> str:
        """
        Accepts a contract, validates it, and prepares it for execution.
        Returns the experiment_id.
        """
        with self._lock:
            if contract.domain not in self.adapters:
                raise ValueError(f"No adapter registered for domain: {contract.domain}")
        contract = self.prepare_contract(contract)
        stored = self._ledger.submit(contract)
        with self._lock:
            self.experiments[stored.experiment_id] = stored.model_copy(deep=True)
        self._ledger.export_compatibility_snapshots()
        return stored.experiment_id

    @staticmethod
    def prepare_contract(contract: ExperimentContract) -> ExperimentContract:
        """Return the immutable-by-ledger contract with exact input/config identity."""
        return _prepare_contract(contract)

    def execute_experiment(self, experiment_id: str) -> ExperimentResult:
        """Run only when a bounded local worker slot is immediately available."""
        if not self._run_capacity.acquire(blocking=False):
            with self._lock:
                self._capacity_rejections += 1
            raise ExperimentCapacityExceeded(
                f"Forge is at its local execution limit ({self._max_concurrent_runs}); "
                "retry after an active run finishes."
            )
        with self._lock:
            self._active_run_count += 1
        try:
            try:
                return self._execute_experiment_with_capacity(experiment_id)
            except ExperimentCapacityExceeded:
                with self._lock:
                    self._capacity_rejections += 1
                raise
        finally:
            with self._lock:
                self._active_run_count -= 1
            self._run_capacity.release()

    def _execute_experiment_with_capacity(self, experiment_id: str) -> ExperimentResult:
        """
        Creates a Run, delegates to the appropriate adapter, and persists the Result.
        """
        contract = self._ledger.get_experiment(experiment_id)
        if contract is None:
            raise ValueError(f"Experiment {experiment_id} not found.")
        with self._lock:
            adapter = self.adapters.get(contract.domain)
        if adapter is None:
            raise ValueError(f"No adapter registered for domain: {contract.domain}")

        environment_snapshot = _runtime_environment_snapshot(adapter)
        environment_fingerprint = hashlib.sha256(
            json.dumps(
                environment_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        run = self._ledger.begin_run(
            experiment_id,
            environment_fingerprint=environment_fingerprint,
            environment_snapshot=environment_snapshot,
        )
        with self._lock:
            self.runs[run.run_id] = run.model_copy(deep=True)

        try:
            expected_environment = contract.environment.environment_fingerprint
            if expected_environment and expected_environment != run.environment_fingerprint:
                raise ValueError("Runtime environment does not match the contract's pinned environment fingerprint")
            if not environment_snapshot["adapter_provenance"]["source_fingerprint_available"]:
                raise ValueError(
                    "Experiment adapter source is not fingerprintable; provide a source file "
                    "or a 64-character SHA-256 code_fingerprint"
                )
            self._verify_dataset_identity(contract)
            result = adapter.execute(contract.model_copy(deep=True), run.model_copy(deep=True))
            self._verify_dataset_identity(contract)
            if _adapter_implementation_provenance(adapter) != environment_snapshot["adapter_provenance"]:
                raise ValueError("Experiment adapter implementation changed during the run")
            if not isinstance(result, ExperimentResult):
                raise TypeError("ExperimentAdapter.execute must return ExperimentResult")
            if result.run_id != run.run_id or result.experiment_id != experiment_id:
                raise ValueError("Adapter result provenance does not match its run and contract")
            if result.status not in (
                ExperimentStatus.COMPLETED,
                ExperimentStatus.FAILED,
                ExperimentStatus.ABORTED,
            ):
                raise ValueError(f"Adapter returned non-terminal status '{result.status.value}'")
            result = result.model_copy(deep=True)
            self._ledger._encode(result)
        except Exception as exc:
            log.error(
                "experiment adapter failed: experiment_id=%s run_id=%s error_type=%s",
                experiment_id,
                run.run_id,
                type(exc).__name__,
            )
            result = ExperimentResult(
                run_id=run.run_id,
                experiment_id=experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=f"Adapter execution failed ({type(exc).__name__})",
            )

        final_run = run.model_copy(update={
            "status": result.status,
            "completed_at": datetime.now(timezone.utc),
            "error_message": result.error_message,
        })
        stored_run, stored_result = self._ledger.complete_run(final_run, result)
        with self._lock:
            self.runs[stored_run.run_id] = stored_run.model_copy(deep=True)
            self.results[stored_result.result_id] = stored_result.model_copy(deep=True)
        self._ledger.export_compatibility_snapshots()
        return stored_result.model_copy(deep=True)

    @staticmethod
    def _verify_dataset_identity(contract: ExperimentContract) -> None:
        if contract.data.dataset_path is None:
            return
        canonical_path, fingerprint = fingerprint_dataset_file(contract.data.dataset_path)
        if (
            canonical_path != contract.data.dataset_path
            or not contract.data.data_fingerprint
            or fingerprint != contract.data.data_fingerprint
        ):
            raise ValueError("Experiment dataset changed after its contract was pinned")

    def get_experiment(self, experiment_id: str) -> Optional[ExperimentContract]:
        return self._ledger.get_experiment(experiment_id)

    def get_run(self, run_id: str) -> Optional[ExperimentRun]:
        return self._ledger.get_run(run_id)

    def list_runs(self, experiment_id: str) -> List[ExperimentRun]:
        return self._ledger.list_runs(experiment_id)

    def get_result(self, result_id: str) -> Optional[ExperimentResult]:
        return self._ledger.get_result(result_id)

    def get_result_for_run(self, run_id: str) -> Optional[ExperimentResult]:
        return self._ledger.get_result_for_run(run_id)

    def record_evaluation(self, report):
        from Forge.core.evaluator import Evaluator
        contract = self._ledger.get_experiment(report.experiment_id)
        result = self._ledger.get_result(report.result_id)
        if contract is None or result is None:
            raise ValueError("Evaluation report must reference a persisted contract and result")
        expected = Evaluator().evaluate(contract, result)
        submitted_payload = report.model_dump(mode="json", exclude={"timestamp"})
        expected_payload = expected.model_dump(mode="json", exclude={"timestamp"})
        if submitted_payload != expected_payload:
            raise ValueError("Evaluation report differs from Forge's deterministic evaluation")
        stored = self._ledger.record_evaluation(expected)
        with self._lock:
            self.evaluation_reports[stored.result_id] = stored.model_copy(deep=True)
        return stored.model_copy(deep=True)

    def get_evaluation_for_run(self, run_id: str):
        report = self._ledger.get_evaluation_for_run(run_id)
        if report is not None:
            with self._lock:
                self.evaluation_reports[report.result_id] = report.model_copy(deep=True)
        return report

    def run_reconciliation_claims(
        self,
        run_id: str,
        authorized_by: str,
        reason: str,
        worker_terminated: bool,
    ) -> Dict[str, Any]:
        """Build exact Aegis claims for an operator-verified abandoned run."""
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by must be a non-empty operator identity")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("reason must contain 1 through 1000 non-whitespace characters")
        if worker_terminated is not True:
            raise ValueError(
                "worker_terminated must be explicitly attested before reconciliation"
            )
        run = self._ledger.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} was not found")
        if run.status != ExperimentStatus.RUNNING:
            raise ValueError(f"Run {run_id} is not RUNNING and cannot be reconciled")
        run_digest = hashlib.sha256(self._ledger._encode(run).encode("utf-8")).hexdigest()
        return {
            "action": "reconcile_abandoned_experiment_run",
            "audience": "forge.operations",
            "run_id": run.run_id,
            "experiment_id": run.experiment_id,
            "run_digest": run_digest,
            "authorized_by": authorized_by.strip(),
            "reason": reason.strip(),
            "worker_terminated": True,
        }

    def reconcile_abandoned_run(
        self, run_id: str, authorization: RunReconciliationAuthorization
    ) -> ExperimentResult:
        """Close one run only after trusted, exact-claim Aegis authorization.

        This records an operator-authorized ABORTED state. It does not kill or
        cancel adapter code; callers must establish that the worker process has
        terminated before obtaining authorization.
        """
        verifier = self._reconciliation_verifier
        if verifier is None:
            raise PermissionError("No trusted forge.operations verifier is configured")
        if authorization.decision != "AUTHORIZED" or authorization.audience != "forge.operations":
            raise PermissionError("Authorization is not valid for Forge run reconciliation")
        if authorization.run_id != run_id:
            raise PermissionError("Authorization run ID does not match the requested run")
        if (
            authorization.issued_at.tzinfo is None
            or authorization.issued_at.utcoffset() is None
            or authorization.expires_at.tzinfo is None
            or authorization.expires_at.utcoffset() is None
        ):
            raise PermissionError("Authorization timestamps must be timezone-aware")
        now = datetime.now(timezone.utc)
        if authorization.issued_at > now or authorization.expires_at <= now:
            raise PermissionError("Authorization is not currently valid")
        reason = authorization.claims.get("reason")
        required_claims = self.run_reconciliation_claims(
            run_id=run_id,
            authorized_by=authorization.authorized_by,
            reason=reason,
            worker_terminated=authorization.claims.get("worker_terminated"),
        )
        if authorization.experiment_id != required_claims["experiment_id"]:
            raise PermissionError("Authorization experiment ID does not match the persisted run")
        if authorization.claims != required_claims:
            raise PermissionError("Authorization claims do not match the persisted RUNNING record")
        if not verifier.verify_and_consume(authorization, required_claims):
            raise PermissionError("Trusted Aegis verifier rejected the reconciliation authorization")

        current_claims = self.run_reconciliation_claims(
            run_id=run_id,
            authorized_by=authorization.authorized_by,
            reason=required_claims["reason"],
            worker_terminated=True,
        )
        if current_claims != required_claims:
            raise RuntimeError("Run changed while reconciliation authorization was being verified")

        run = self._ledger.get_run(run_id)
        if run is None or run.status != ExperimentStatus.RUNNING:
            raise RuntimeError("Run changed state before reconciliation could be committed")
        completed_at = datetime.now(timezone.utc)
        audit = {
            "action": required_claims["action"],
            "operator": authorization.authorized_by,
            "reason": required_claims["reason"],
            "worker_terminated_attested": True,
            "authorization_id": authorization.authorization_id,
            "issuer": authorization.issuer,
            "nonce": authorization.nonce,
            "claims_digest": hashlib.sha256(
                json.dumps(required_claims, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
            ).hexdigest(),
            "signature_digest": hashlib.sha256(authorization.signature.encode("utf-8")).hexdigest(),
            "reconciled_at": completed_at.isoformat(),
        }
        result = ExperimentResult(
            run_id=run.run_id,
            experiment_id=run.experiment_id,
            status=ExperimentStatus.ABORTED,
            evidence={"reconciliation": audit},
            error_message="Run was marked ABORTED through authorized operator reconciliation.",
            timestamp=completed_at,
        )
        terminal_run = run.model_copy(update={
            "status": ExperimentStatus.ABORTED,
            "completed_at": completed_at,
            "error_message": result.error_message,
        })
        stored_run, stored_result = self._ledger.complete_run(terminal_run, result)
        with self._lock:
            self.runs[stored_run.run_id] = stored_run.model_copy(deep=True)
            self.results[stored_result.result_id] = stored_result.model_copy(deep=True)
        self._ledger.export_compatibility_snapshots()
        return stored_result.model_copy(deep=True)

    def stats(self) -> Dict[str, Any]:
        stats = self._ledger.stats()
        with self._lock:
            stats["registered_domains"] = sorted(domain.value for domain in self.adapters)
            stats["execution_capacity"] = {
                "active": self._active_run_count,
                "maximum": self._max_concurrent_runs,
                "active_local": self._active_run_count,
                "active_shared": stats["running"],
                "maximum_local": self._max_concurrent_runs,
                "maximum_shared": stats["configured_max_concurrent_runs"],
                "rejections": self._capacity_rejections,
                "scope": "shared_experiment_ledger",
            }
        return stats


def _adapter_implementation_provenance(adapter) -> Dict[str, Any]:
    adapter_source_digest = None
    try:
        adapter_source_file = inspect.getsourcefile(type(adapter))
    except (OSError, TypeError):
        adapter_source_file = None
    if adapter_source_file:
        adapter_path = Path(adapter_source_file)
        if adapter_path.is_file():
            adapter_source_digest = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    explicit_fingerprint = getattr(adapter, "code_fingerprint", None)
    if (
        adapter_source_digest is None
        and isinstance(explicit_fingerprint, str)
        and len(explicit_fingerprint) == 64
        and all(char in "0123456789abcdefABCDEF" for char in explicit_fingerprint)
    ):
        adapter_source_digest = explicit_fingerprint.lower()
    dependency_fingerprints = []
    dependency_files = getattr(adapter, "provenance_files", ())
    if not isinstance(dependency_files, (list, tuple)):
        dependency_files = ()
    dependencies_available = True
    for dependency in dependency_files:
        try:
            dependency_path = Path(dependency).expanduser().resolve(strict=True)
            if not dependency_path.is_file():
                raise ValueError("declared adapter dependency is not a regular file")
            dependency_digest = hashlib.sha256(dependency_path.read_bytes()).hexdigest()
            dependency_fingerprints.append({
                "path": str(dependency_path),
                "sha256": dependency_digest,
            })
        except (OSError, TypeError, ValueError):
            dependencies_available = False
            dependency_fingerprints.append({"path": str(dependency), "sha256": None})
    domain = getattr(adapter, "domain", "unknown")
    return {
        "domain": getattr(domain, "value", str(domain)),
        "module": type(adapter).__module__,
        "class": type(adapter).__qualname__,
        "version": getattr(adapter, "version", None),
        "source_fingerprint": adapter_source_digest,
        "dependency_fingerprints": dependency_fingerprints,
        "source_fingerprint_available": (
            adapter_source_digest is not None and dependencies_available
        ),
    }


def _runtime_environment_snapshot(adapter=None) -> Dict[str, Any]:
    """Capture versions that materially affect Forge's ML execution."""
    package_names = (
        "numpy", "pandas", "scipy", "scikit-learn", "torch", "xgboost",
        "lightgbm", "stable-baselines3", "gymnasium", "gym", "pydantic", "joblib",
    )
    packages: Dict[str, Optional[str]] = {}
    for package in package_names:
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    repo_root = Path(__file__).resolve().parents[2]
    source_files = (
        "Forge/core/experimentation.py",
        "Forge/core/adapters/ml_adapter.py",
        "Forge/core/backends.py",
        "Forge/core/extra_backends.py",
        "Forge/core/dataset.py",
        "Forge/core/metrics.py",
        "Forge/core/evaluator.py",
        "Forge/core/benchmark.py",
        "Forge/core/challenger.py",
        "Forge/core/challenger_engine.py",
        "Forge/core/promotion.py",
        "Forge/core/model_registry.py",
        "Forge/agents/training_agent.py",
        "Forge/intelligence/optimizer.py",
    )
    source_digest = hashlib.sha256()
    for relative_path in source_files:
        path = repo_root / relative_path
        source_digest.update(relative_path.encode("utf-8"))
        source_digest.update(b"\0")
        source_digest.update(path.read_bytes())
        source_digest.update(b"\0")
    adapter_provenance = _adapter_implementation_provenance(adapter) if adapter is not None else None
    return {
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
        "forge_code_fingerprint": source_digest.hexdigest(),
        "adapter_provenance": adapter_provenance,
    }


def _capacity_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain Forge's shared experiment capacity")
    subcommands = parser.add_subparsers(dest="command", required=True)
    set_parser = subcommands.add_parser("set-capacity", help="change the shared run limit")
    set_parser.add_argument("maximum", type=int)
    set_parser.add_argument("--reason", required=True)
    set_parser.add_argument("--database", type=Path, default=EXPERIMENTS_DB)
    args = parser.parse_args(argv)
    try:
        result = ExperimentLedger.reconfigure_capacity(
            args.database, args.maximum, args.reason
        )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_capacity_cli())
