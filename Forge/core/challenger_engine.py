"""
Forge V2 - ChallengerEngine (Phase 4C)
========================================
Orchestrates the complete challenge pipeline:
  Validate → Execute → Evaluate → Benchmark → Package Result.

Principle:
  Evaluator measures. Benchmark contextualizes. Orchestration coordinates.
  Promotion decides.

The ChallengerEngine:
  - Accepts a ChallengeRequest and a BenchmarkContext.
  - Validates the request against the ChampionRegistry.
  - Validates that the BenchmarkContext matches the requested champion.
  - Executes the experiment via ExperimentRunner.
  - Evaluates via Evaluator.
  - Benchmarks via Benchmark.
  - Packages an immutable ChallengeResult.

The ChallengerEngine does NOT:
  - Promote a champion.
  - Modify ChampionRegistry.
  - Modify ModelRegistry.
  - Modify MethodDiscovery.
  - Decide whether a candidate is worthy of promotion.
  - Alter the experiment contract.
  - Invent metric directions.
  - Perform statistical significance testing.

Expected operational/scientific failures return ChallengeResult(FAILED).
Capacity rejection before a run exists returns the challenge to PENDING and is
raised as a retryable signal; internal programming/invariant failures raise.
"""

import json
import hashlib
import logging
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from Forge.core.experimentation import (
    ExperimentCapacityExceeded,
    ExperimentResult,
    ExperimentStatus,
)
from Forge.core.evaluator import Evaluator, EvaluationReport
from Forge.core.benchmark import Benchmark, BenchmarkContext, BenchmarkReport, BenchmarkStatus
from Forge.core.challenger import (
    ChallengeRequest,
    ChallengeResult,
    ChallengeReconciliationAuthorization,
    ChallengeStatus,
    ChampionRegistry,
)
from Forge.core.storage_paths import migrate_legacy_storage


# ==============================================================================
# 1. PERSISTENCE CONFIGURATION
# ==============================================================================
_FORGE_ROOT = Path(__file__).resolve().parents[1]
CHALLENGER_DIR = _FORGE_ROOT / "memory" / "challenger"
CHALLENGE_RESULTS_FILE = CHALLENGER_DIR / "challenge_results.json"
CHALLENGES_DB = CHALLENGER_DIR / "challenges.sqlite3"

CHALLENGER_DIR.mkdir(parents=True, exist_ok=True)
migrate_legacy_storage(
    _FORGE_ROOT / "challenger", CHALLENGER_DIR,
    ("challenge_results.json", "challenges.sqlite3"),
)
log = logging.getLogger("forge.challenger")


# ==============================================================================
# 2. CHALLENGE FAILURE EXCEPTION
# ==============================================================================
class ChallengeFailure(Exception):
    """
    Raised internally when an expected operational/scientific failure occurs.
    Caught by the engine's run() method and converted to ChallengeResult(FAILED).

    This is NOT raised for programming bugs or invariant violations.
    Those propagate as normal exceptions.
    """
    def __init__(
        self,
        message: str,
        stage: str,
        experiment_result: Optional[ExperimentResult] = None,
        evaluation_report: Optional[EvaluationReport] = None,
        benchmark_report: Optional[BenchmarkReport] = None,
    ):
        super().__init__(message)
        self.stage = stage  # Which pipeline stage failed
        self.experiment_result = experiment_result
        self.evaluation_report = evaluation_report
        self.benchmark_report = benchmark_report


# ==============================================================================
# 3. PERSISTENCE HELPERS
# ==============================================================================
def _load_json(file_path: Path) -> dict:
    if file_path.exists():
        with open(file_path, "r") as f:
            return json.load(f)
    return {}


def _save_json(file_path: Path, data: dict) -> None:
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


class ChallengeInProgress(RuntimeError):
    """The same challenge ID is already claimed by another execution."""


class ChallengeLedger:
    """Transactional store for challenge requests and their complete evidence."""

    def __init__(self, database_path: Path = CHALLENGES_DB, results_file: Path = CHALLENGE_RESULTS_FILE):
        self.database_path = Path(database_path)
        self.results_file = Path(results_file)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _canonical(model) -> str:
        return json.dumps(
            model.model_dump(mode="json"), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS challenge_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS challenges (
                    challenge_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    evaluation_report_id TEXT,
                    evaluation_json TEXT,
                    benchmark_report_id TEXT,
                    benchmark_json TEXT
                );
                CREATE INDEX IF NOT EXISTS challenges_status_idx ON challenges(status);
            """)
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Challenge ledger integrity check failed: {integrity}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute(
                    "SELECT value FROM challenge_metadata WHERE key = 'schema_version'"
                ).fetchone()
                if version is not None and version[0] != "1":
                    raise RuntimeError(f"Unsupported challenge ledger schema: {version[0]}")
                connection.execute(
                    "INSERT OR IGNORE INTO challenge_metadata(key, value) VALUES('schema_version', '1')"
                )
                migrated = connection.execute(
                    "SELECT value FROM challenge_metadata WHERE key = 'legacy_json_migrated'"
                ).fetchone()
                if migrated is None:
                    legacy = _load_json(self.results_file)
                    for key, raw in legacy.items():
                        result = ChallengeResult.model_validate(raw)
                        if key != result.challenge_id:
                            raise RuntimeError(
                                f"Legacy challenge key does not match {result.challenge_id}"
                            )
                        request = {
                            "challenge_id": result.challenge_id,
                            "champion_id": result.champion_id,
                            "candidate_id": result.candidate_id,
                            "experiment_id": result.experiment_id,
                        }
                        connection.execute(
                            "INSERT INTO challenges(challenge_id, request_json, context_json, status, result_json) "
                            "VALUES (?, ?, '{}', ?, ?)",
                            (result.challenge_id, json.dumps(request, sort_keys=True),
                             result.status.value, self._canonical(result)),
                        )
                    connection.execute(
                        "INSERT INTO challenge_metadata(key, value) VALUES('legacy_json_migrated', '1')"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def claim(self, request: ChallengeRequest, context: BenchmarkContext) -> Optional[ChallengeResult]:
        request_json = self._canonical(request)
        context_json = self._canonical(context)
        initial = ChallengeResult(
            challenge_id=request.challenge_id,
            champion_id=request.champion_id,
            candidate_id=request.candidate_id,
            experiment_id=request.experiment.experiment_id,
            run_id="",
            result_id="",
            status=ChallengeStatus.RUNNING,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT request_json, context_json, status, result_json FROM challenges WHERE challenge_id = ?",
                    (request.challenge_id,),
                ).fetchone()
                if row is not None:
                    if row["request_json"] != request_json or row["context_json"] != context_json:
                        raise ValueError("Challenge ID already exists with different request or benchmark context")
                    if row["status"] == ChallengeStatus.RUNNING.value:
                        raise ChallengeInProgress(f"Challenge '{request.challenge_id}' is already running")
                    if row["status"] == ChallengeStatus.PENDING.value:
                        connection.execute(
                            "UPDATE challenges SET status = ?, result_json = ?, created_at = CURRENT_TIMESTAMP, "
                            "evaluation_report_id = NULL, evaluation_json = NULL, "
                            "benchmark_report_id = NULL, benchmark_json = NULL "
                            "WHERE challenge_id = ? AND status = ?",
                            (ChallengeStatus.RUNNING.value, self._canonical(initial),
                             request.challenge_id, ChallengeStatus.PENDING.value),
                        )
                        if connection.execute("SELECT changes()").fetchone()[0] != 1:
                            raise RuntimeError("Pending challenge could not be claimed")
                        connection.commit()
                        return None
                    connection.commit()
                    return ChallengeResult.model_validate_json(row["result_json"])
                connection.execute(
                    "INSERT INTO challenges(challenge_id, request_json, context_json, status, result_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (request.challenge_id, request_json, context_json,
                     ChallengeStatus.RUNNING.value, self._canonical(initial)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return None

    def release_for_capacity_retry(
        self, request: ChallengeRequest, context: BenchmarkContext
    ) -> None:
        """Return a pre-run challenge to PENDING after bounded capacity rejection."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT request_json, context_json, result_json, status FROM challenges "
                    "WHERE challenge_id = ?",
                    (request.challenge_id,),
                ).fetchone()
                if row is None or row["status"] != ChallengeStatus.RUNNING.value:
                    raise RuntimeError("Challenge is not claimed and running")
                if (
                    row["request_json"] != self._canonical(request)
                    or row["context_json"] != self._canonical(context)
                ):
                    raise ValueError("Challenge inputs changed before capacity retry")
                claimed = ChallengeResult.model_validate_json(row["result_json"])
                if claimed.run_id or claimed.result_id:
                    raise RuntimeError("Challenge with run evidence cannot be retried as pre-run")
                pending = claimed.model_copy(update={
                    "status": ChallengeStatus.PENDING,
                    "error_message": "Execution capacity was unavailable; safe to retry.",
                    "timestamp": datetime.now(timezone.utc),
                })
                connection.execute(
                    "UPDATE challenges SET status = ?, result_json = ? "
                    "WHERE challenge_id = ? AND status = ?",
                    (ChallengeStatus.PENDING.value, self._canonical(pending),
                     request.challenge_id, ChallengeStatus.RUNNING.value),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RuntimeError("Challenge state changed during capacity retry release")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.export_snapshot()

    def complete(
        self,
        result: ChallengeResult,
        request: ChallengeRequest,
        context: BenchmarkContext,
        evaluation_report: Optional[EvaluationReport],
        benchmark_report: Optional[BenchmarkReport],
    ) -> ChallengeResult:
        if (
            result.challenge_id != request.challenge_id
            or result.champion_id != request.champion_id
            or result.candidate_id != request.candidate_id
            or result.experiment_id != request.experiment.experiment_id
        ):
            raise ValueError("Challenge result provenance does not match its request")
        if result.status not in (
            ChallengeStatus.COMPLETED,
            ChallengeStatus.FAILED,
            ChallengeStatus.ABORTED,
        ):
            raise ValueError("A persisted challenge result must have a terminal status")
        if result.status == ChallengeStatus.COMPLETED and (
            evaluation_report is None or benchmark_report is None
        ):
            raise ValueError("A completed challenge must retain evaluation and benchmark evidence")
        if benchmark_report is not None and evaluation_report is None:
            raise ValueError("A benchmark report cannot be persisted without its evaluation report")
        if (result.evaluation_report_id is not None) != (evaluation_report is not None):
            raise ValueError("Challenge evaluation report reference does not match persisted evidence")
        if (result.benchmark_report_id is not None) != (benchmark_report is not None):
            raise ValueError("Challenge benchmark report reference does not match persisted evidence")
        for report in (evaluation_report, benchmark_report):
            if report is not None and (
                report.experiment_id != result.experiment_id
                or report.run_id != result.run_id
                or report.result_id != result.result_id
            ):
                raise ValueError("Challenge report provenance does not match its result")
        if benchmark_report is not None and benchmark_report.reference_id != result.champion_id:
            raise ValueError("Benchmark reference does not match the challenged champion")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, request_json, context_json, result_json "
                    "FROM challenges WHERE challenge_id = ?",
                    (result.challenge_id,),
                ).fetchone()
                if row is None or row["status"] != ChallengeStatus.RUNNING.value:
                    raise RuntimeError("Challenge claim is missing or no longer running")
                claimed = ChallengeResult.model_validate_json(row["result_json"])
                if (
                    row["request_json"] != self._canonical(request)
                    or row["context_json"] != self._canonical(context)
                    or claimed.status != ChallengeStatus.RUNNING
                    or claimed.challenge_id != request.challenge_id
                    or claimed.champion_id != request.champion_id
                    or claimed.candidate_id != request.candidate_id
                    or claimed.experiment_id != request.experiment.experiment_id
                    or claimed.run_id != ""
                    or claimed.result_id != ""
                ):
                    raise ValueError("Challenge claim identity or inputs changed before completion")
                update = connection.execute(
                    "UPDATE challenges SET status = ?, result_json = ?, evaluation_report_id = ?, "
                    "evaluation_json = ?, benchmark_report_id = ?, benchmark_json = ? "
                    "WHERE challenge_id = ?",
                    (result.status.value, self._canonical(result), result.evaluation_report_id,
                     self._canonical(evaluation_report) if evaluation_report else None,
                     result.benchmark_report_id,
                     self._canonical(benchmark_report) if benchmark_report else None,
                     result.challenge_id),
                )
                if update.rowcount != 1:
                    raise RuntimeError("Challenge state changed during result persistence")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.export_snapshot()
        return result

    def get_reconciliation_inputs(self, challenge_id: str):
        """Return persisted request/context only while a challenge is RUNNING."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_json, context_json, status, result_json "
                "FROM challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Challenge {challenge_id} was not found")
        if row["status"] != ChallengeStatus.RUNNING.value:
            raise ValueError(f"Challenge {challenge_id} is not RUNNING")
        return (
            ChallengeRequest.model_validate_json(row["request_json"]),
            BenchmarkContext.model_validate_json(row["context_json"]),
            ChallengeResult.model_validate_json(row["result_json"]),
        )

    def reconcile_abandoned_pre_run(
        self,
        challenge_id: str,
        request: ChallengeRequest,
        context: BenchmarkContext,
        result: ChallengeResult,
    ) -> ChallengeResult:
        """Atomically close a challenge abandoned before experiment execution."""
        if (
            result.challenge_id != challenge_id
            or result.champion_id != request.champion_id
            or result.candidate_id != request.candidate_id
            or result.experiment_id != request.experiment.experiment_id
            or result.status != ChallengeStatus.ABORTED
            or result.run_id
            or result.result_id
            or result.evaluation_report_id is not None
            or result.benchmark_report_id is not None
            or result.reconciliation_evidence.get("action")
            != "reconcile_abandoned_challenge_before_run"
            or result.reconciliation_evidence.get("worker_terminated_attested") is not True
        ):
            raise ValueError("Aborted challenge reconciliation result is incomplete or inconsistent")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT request_json, context_json, status FROM challenges "
                    "WHERE challenge_id = ?",
                    (challenge_id,),
                ).fetchone()
                if row is None or row["status"] != ChallengeStatus.RUNNING.value:
                    raise RuntimeError("Challenge claim is missing or no longer running")
                if (
                    row["request_json"] != self._canonical(request)
                    or row["context_json"] != self._canonical(context)
                ):
                    raise ValueError("Challenge inputs changed after the durable claim")
                update = connection.execute(
                    "UPDATE challenges SET status = ?, result_json = ?, "
                    "evaluation_report_id = NULL, evaluation_json = NULL, "
                    "benchmark_report_id = NULL, benchmark_json = NULL "
                    "WHERE challenge_id = ? AND status = ?",
                    (ChallengeStatus.ABORTED.value, self._canonical(result), challenge_id,
                     ChallengeStatus.RUNNING.value),
                )
                if update.rowcount != 1:
                    raise RuntimeError("Challenge state changed during reconciliation")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.export_snapshot()
        return result

    def get_result(self, challenge_id: str) -> Optional[ChallengeResult]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT result_json FROM challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()
        return ChallengeResult.model_validate_json(row[0]) if row else None

    def get_evaluation_report(self, report_id: str) -> Optional[EvaluationReport]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT evaluation_json FROM challenges WHERE evaluation_report_id = ?",
                (report_id,),
            ).fetchone()
        return EvaluationReport.model_validate_json(row[0]) if row and row[0] else None

    def get_benchmark_report(self, report_id: str) -> Optional[BenchmarkReport]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT benchmark_json FROM challenges WHERE benchmark_report_id = ?",
                (report_id,),
            ).fetchone()
        return BenchmarkReport.model_validate_json(row[0]) if row and row[0] else None

    def list_results(self):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT result_json FROM challenges ORDER BY created_at, challenge_id"
            ).fetchall()
        return [ChallengeResult.model_validate_json(row[0]) for row in rows]

    def stats(self):
        with closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            grouped = connection.execute(
                "SELECT status, COUNT(*) AS count FROM challenges GROUP BY status"
            ).fetchall()
            running = connection.execute(
                "SELECT challenge_id, created_at, result_json FROM challenges "
                "WHERE status = ? ORDER BY created_at",
                (ChallengeStatus.RUNNING.value,),
            ).fetchall()
            pending = connection.execute(
                "SELECT challenge_id, created_at, result_json FROM challenges "
                "WHERE status = ? ORDER BY created_at",
                (ChallengeStatus.PENDING.value,),
            ).fetchall()
        now = datetime.now(timezone.utc)
        running_challenges = []
        pending_challenges = []
        for row in running:
            result = ChallengeResult.model_validate_json(row["result_json"])
            started_at = datetime.fromisoformat(row["created_at"])
            if started_at.tzinfo is None or started_at.utcoffset() is None:
                # SQLite CURRENT_TIMESTAMP stores UTC without an explicit zone.
                started_at = started_at.replace(tzinfo=timezone.utc)
            running_challenges.append({
                "challenge_id": row["challenge_id"],
                "experiment_id": result.experiment_id,
                "started_at": started_at.isoformat(),
                "elapsed_seconds": max(0, int((now - started_at).total_seconds())),
            })
        for row in pending:
            result = ChallengeResult.model_validate_json(row["result_json"])
            pending_challenges.append({
                "challenge_id": row["challenge_id"],
                "experiment_id": result.experiment_id,
                "pending_since": result.timestamp.isoformat(),
            })
        return {
            "database_integrity": integrity == "ok",
            "challenges_by_status": {row["status"]: row["count"] for row in grouped},
            "running_challenges": running_challenges,
            "pending_challenges": pending_challenges,
            "ready": integrity == "ok",
        }

    def export_snapshot(self):
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                data = {
                    row["challenge_id"]: json.loads(row["result_json"])
                    for row in connection.execute(
                        "SELECT challenge_id, result_json FROM challenges WHERE status != ?",
                        (ChallengeStatus.RUNNING.value,),
                    )
                }
                _save_json(self.results_file, data)
                connection.commit()
            except Exception:
                connection.rollback()
                log.exception("Unable to export challenge compatibility snapshot")


# ==============================================================================
# 4. CHALLENGER ENGINE
# ==============================================================================
class ChallengerEngine:
    """
    Orchestrates the complete challenge pipeline.

    Public API:
        result = engine.run(request=challenge_request, benchmark_context=context)

    Internal stages (private):
        _validate_request()
        _execute_experiment()
        _evaluate_result()
        _benchmark_result()
        _build_challenge_result()

    The engine does NOT own persistence of champion state, model state,
    or scientific policy. It transactionally persists challenge requests,
    results, and the reports required to audit a later promotion decision.
    """

    def __init__(
        self,
        registry: ChampionRegistry,
        experiment_runner,
        evaluator: Evaluator,
        benchmark: Benchmark,
        reconciliation_verifier=None,
    ):
        """
        Args:
            registry: ChampionRegistry for validating challenge scope.
            experiment_runner: ExperimentRunner for executing experiments.
            evaluator: Evaluator for measuring baseline vs treatment.
            benchmark: Benchmark for comparing treatment vs reference.
        """
        self._registry = registry
        self._runner = experiment_runner
        self._evaluator = evaluator
        self._benchmark = benchmark
        self._reconciliation_verifier = reconciliation_verifier
        self._ledger = ChallengeLedger(CHALLENGES_DB, CHALLENGE_RESULTS_FILE)

    @property
    def reconciliation_authorization_configured(self) -> bool:
        return self._reconciliation_verifier is not None

    def challenge_reconciliation_claims(
        self,
        challenge_id: str,
        authorized_by: str,
        reason: str,
        worker_terminated: bool,
    ) -> dict:
        """Build Aegis claims for an orphaned challenge that never began a run."""
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by must be a non-empty operator identity")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("reason must contain 1 through 1000 non-whitespace characters")
        if worker_terminated is not True:
            raise ValueError("worker_terminated must be explicitly attested")
        request, context, _ = self._ledger.get_reconciliation_inputs(challenge_id)
        if request.challenge_id != challenge_id:
            raise ValueError("Persisted challenge identity does not match its key")
        runs = self._runner.list_runs(request.experiment.experiment_id)
        if runs:
            raise RuntimeError(
                "The challenge's experiment has a persisted run; preserve or reconcile "
                "that run before handling the challenge record"
            )
        request_json = self._ledger._canonical(request)
        context_json = self._ledger._canonical(context)
        return {
            "action": "reconcile_abandoned_challenge_before_run",
            "audience": "forge.operations",
            "challenge_id": challenge_id,
            "experiment_id": request.experiment.experiment_id,
            "request_digest": hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
            "context_digest": hashlib.sha256(context_json.encode("utf-8")).hexdigest(),
            "authorized_by": authorized_by.strip(),
            "reason": reason.strip(),
            "worker_terminated": True,
        }

    def challenge_resume_claims(
        self,
        challenge_id: str,
        run_id: str,
        authorized_by: str,
        reason: str,
        worker_terminated: bool,
    ) -> dict:
        """Bind authorization to a completed durable experiment for resumption."""
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by must be a non-empty operator identity")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("reason must contain 1 through 1000 non-whitespace characters")
        if worker_terminated is not True:
            raise ValueError("worker_terminated must be explicitly attested")
        request, context, _ = self._ledger.get_reconciliation_inputs(challenge_id)
        persisted_contract = self._runner.get_experiment(request.experiment.experiment_id)
        if (
            persisted_contract is None
            or self._ledger._canonical(persisted_contract)
            != self._ledger._canonical(request.experiment)
        ):
            raise ValueError("Persisted experiment contract does not match challenge inputs")
        runs = self._runner.list_runs(request.experiment.experiment_id)
        run = next((item for item in runs if item.run_id == run_id), None)
        if run is None:
            raise ValueError("The selected run is not part of the challenge experiment")
        if run.status not in (
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.ABORTED,
        ):
            raise RuntimeError("The selected experiment run is not in a terminal state")
        experiment_result = self._runner.get_result_for_run(run_id)
        if experiment_result is None:
            raise ValueError("The selected terminal run has no durable result")
        if (
            experiment_result.run_id != run.run_id
            or experiment_result.experiment_id != request.experiment.experiment_id
            or experiment_result.status != run.status
        ):
            raise ValueError("The persisted run and result lineage do not match")
        run_json = json.dumps(
            run.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        result_json = json.dumps(
            experiment_result.model_dump(mode="json"), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
        return {
            "action": "resume_abandoned_challenge_from_experiment_result",
            "audience": "forge.operations",
            "challenge_id": challenge_id,
            "experiment_id": request.experiment.experiment_id,
            "request_digest": hashlib.sha256(
                self._ledger._canonical(request).encode("utf-8")
            ).hexdigest(),
            "context_digest": hashlib.sha256(
                self._ledger._canonical(context).encode("utf-8")
            ).hexdigest(),
            "run_id": run.run_id,
            "result_id": experiment_result.result_id,
            "run_digest": hashlib.sha256(run_json.encode("utf-8")).hexdigest(),
            "result_digest": hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
            "authorized_by": authorized_by.strip(),
            "reason": reason.strip(),
            "worker_terminated": True,
        }

    def resume_abandoned_challenge(
        self,
        challenge_id: str,
        run_id: str,
        authorization: ChallengeReconciliationAuthorization,
    ) -> ChallengeResult:
        """Resume evaluation from persisted experiment evidence, without rerunning it."""
        verifier = self._reconciliation_verifier
        if verifier is None:
            raise PermissionError("No trusted forge.operations verifier is configured")
        if (
            authorization.decision != "AUTHORIZED"
            or authorization.audience != "forge.operations"
            or authorization.challenge_id != challenge_id
        ):
            raise PermissionError("Authorization is not valid for this challenge resumption")
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
        required_claims = self.challenge_resume_claims(
            challenge_id=challenge_id,
            run_id=run_id,
            authorized_by=authorization.authorized_by,
            reason=authorization.claims.get("reason"),
            worker_terminated=authorization.claims.get("worker_terminated"),
        )
        if authorization.claims != required_claims:
            raise PermissionError("Authorization does not bind the selected persisted evidence")
        if not verifier.verify_and_consume(authorization, required_claims):
            raise PermissionError("Trusted Aegis verifier rejected challenge resumption")
        if self.challenge_resume_claims(
            challenge_id=challenge_id,
            run_id=run_id,
            authorized_by=authorization.authorized_by,
            reason=required_claims["reason"],
            worker_terminated=True,
        ) != required_claims:
            raise RuntimeError("Challenge evidence changed during authorization verification")

        request, context, _ = self._ledger.get_reconciliation_inputs(challenge_id)
        experiment_result = self._runner.get_result_for_run(run_id)
        if experiment_result is None:
            raise RuntimeError("Persisted experiment result disappeared before challenge resumption")
        evaluation_report = None
        benchmark_report = None
        if experiment_result.status == ExperimentStatus.COMPLETED:
            try:
                evaluation_report = self._evaluate_result(request, experiment_result)
                benchmark_report = self._benchmark_result(
                    evaluation_report, experiment_result, context
                )
                challenge_result = self._build_challenge_result(
                    request, experiment_result, evaluation_report, benchmark_report
                )
            except ChallengeFailure as exc:
                evaluation_report = exc.evaluation_report or evaluation_report
                benchmark_report = exc.benchmark_report or benchmark_report
                challenge_result = ChallengeResult(
                    challenge_id=challenge_id,
                    champion_id=request.champion_id,
                    candidate_id=request.candidate_id,
                    experiment_id=request.experiment.experiment_id,
                    run_id=experiment_result.run_id,
                    result_id=experiment_result.result_id,
                    status=ChallengeStatus.FAILED,
                    error_message=f"[{exc.stage}] {exc}",
                )
        else:
            challenge_result = ChallengeResult(
                challenge_id=challenge_id,
                champion_id=request.champion_id,
                candidate_id=request.candidate_id,
                experiment_id=request.experiment.experiment_id,
                run_id=experiment_result.run_id,
                result_id=experiment_result.result_id,
                status=(
                    ChallengeStatus.ABORTED
                    if experiment_result.status == ExperimentStatus.ABORTED
                    else ChallengeStatus.FAILED
                ),
                error_message=(
                    f"Persisted experiment run ended with {experiment_result.status.value}: "
                    f"{experiment_result.error_message or 'no error details'}"
                ),
            )

        completed_at = datetime.now(timezone.utc)
        challenge_result = challenge_result.model_copy(update={
            "evaluation_report_id": str(uuid.uuid4()) if evaluation_report is not None else None,
            "benchmark_report_id": str(uuid.uuid4()) if benchmark_report is not None else None,
            "reconciliation_evidence": {
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
                "signature_digest": hashlib.sha256(
                    authorization.signature.encode("utf-8")
                ).hexdigest(),
                "reconciled_at": completed_at.isoformat(),
            },
            "timestamp": completed_at,
        })
        return self._ledger.complete(
            challenge_result, request, context, evaluation_report, benchmark_report
        )

    def reconcile_abandoned_challenge(
        self,
        challenge_id: str,
        authorization: ChallengeReconciliationAuthorization,
    ) -> ChallengeResult:
        """Record an authorized abort only when no experiment run was started."""
        verifier = self._reconciliation_verifier
        if verifier is None:
            raise PermissionError("No trusted forge.operations verifier is configured")
        if (
            authorization.decision != "AUTHORIZED"
            or authorization.audience != "forge.operations"
            or authorization.challenge_id != challenge_id
        ):
            raise PermissionError("Authorization is not valid for this challenge reconciliation")
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
        required_claims = self.challenge_reconciliation_claims(
            challenge_id=challenge_id,
            authorized_by=authorization.authorized_by,
            reason=authorization.claims.get("reason"),
            worker_terminated=authorization.claims.get("worker_terminated"),
        )
        if authorization.claims != required_claims:
            raise PermissionError("Authorization claims do not match persisted challenge inputs")
        if not verifier.verify_and_consume(authorization, required_claims):
            raise PermissionError("Trusted Aegis verifier rejected challenge reconciliation")

        current_claims = self.challenge_reconciliation_claims(
            challenge_id=challenge_id,
            authorized_by=authorization.authorized_by,
            reason=required_claims["reason"],
            worker_terminated=True,
        )
        if current_claims != required_claims:
            raise RuntimeError("Challenge or experiment state changed during authorization verification")

        request, context, _ = self._ledger.get_reconciliation_inputs(challenge_id)
        completed_at = datetime.now(timezone.utc)
        claims_digest = hashlib.sha256(
            json.dumps(required_claims, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
        result = ChallengeResult(
            challenge_id=challenge_id,
            champion_id=request.champion_id,
            candidate_id=request.candidate_id,
            experiment_id=request.experiment.experiment_id,
            run_id="",
            result_id="",
            status=ChallengeStatus.ABORTED,
            error_message="Challenge was abandoned before experiment execution and reconciled by an authorized operator.",
            reconciliation_evidence={
                "action": required_claims["action"],
                "operator": authorization.authorized_by,
                "reason": required_claims["reason"],
                "worker_terminated_attested": True,
                "authorization_id": authorization.authorization_id,
                "issuer": authorization.issuer,
                "nonce": authorization.nonce,
                "claims_digest": claims_digest,
                "signature_digest": hashlib.sha256(
                    authorization.signature.encode("utf-8")
                ).hexdigest(),
                "reconciled_at": completed_at.isoformat(),
            },
            timestamp=completed_at,
        )
        return self._ledger.reconcile_abandoned_pre_run(
            challenge_id, request, context, result
        )

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def run(
        self,
        request: ChallengeRequest,
        benchmark_context: BenchmarkContext,
    ) -> ChallengeResult:
        """
        Executes the complete challenge pipeline.

        Args:
            request: The challenge declaration (what is competing, which incumbent).
            benchmark_context: The frozen reference snapshot to compare against.

        Returns:
            An immutable ChallengeResult.

        Expected failures (experiment crash, missing metrics, context mismatch)
        return ChallengeResult(status=FAILED).

        Programming bugs and invariant violations raise exceptions.
        """
        request = request.model_copy(deep=True)
        benchmark_context = benchmark_context.model_copy(deep=True)
        preparation_error = None
        try:
            # Reject invalid scope before accessing or hashing caller-selected data.
            self._validate_request(request, benchmark_context)
        except ChallengeFailure as exc:
            preparation_error = f"[{exc.stage}] {str(exc)}"
        prepare_contract = getattr(self._runner, "prepare_contract", None)
        if preparation_error is None and callable(prepare_contract):
            try:
                prepared_contract = prepare_contract(request.experiment)
                request = request.model_copy(update={"experiment": prepared_contract})
            except Exception as exc:
                preparation_error = f"Experiment contract preparation failed ({type(exc).__name__})"
        elif preparation_error is None:
            preparation_error = "Experiment runner does not support reproducibility preparation"
        cached = self._ledger.claim(request, benchmark_context)
        if cached is not None:
            return cached

        if preparation_error is not None:
            challenge_result = ChallengeResult(
                challenge_id=request.challenge_id,
                champion_id=request.champion_id,
                candidate_id=request.candidate_id,
                experiment_id=request.experiment.experiment_id,
                status=ChallengeStatus.FAILED,
                error_message=preparation_error,
            )
            self._persist_result(
                challenge_result, request, benchmark_context, None, None
            )
            return challenge_result

        experiment_result = None
        evaluation_report = None
        benchmark_report = None
        try:
            # Stage 1: Validate request and context
            self._validate_request(request, benchmark_context)

            # Stage 2: Execute the experiment
            experiment_result = self._execute_experiment(request)

            # Stage 3: Evaluate baseline vs treatment
            evaluation_report = self._evaluate_result(request, experiment_result)

            # Stage 4: Benchmark treatment vs reference
            benchmark_report = self._benchmark_result(
                evaluation_report, experiment_result, benchmark_context
            )

            # Stage 5: Package the ChallengeResult
            challenge_result = self._build_challenge_result(
                request, experiment_result, evaluation_report, benchmark_report
            )

        except ExperimentCapacityExceeded:
            persisted_runs = self._runner.list_runs(request.experiment.experiment_id)
            if persisted_runs:
                raise RuntimeError(
                    "Capacity rejection followed a persisted experiment run; "
                    "challenge was left RUNNING for operator review."
                )
            self._ledger.release_for_capacity_retry(request, benchmark_context)
            raise
        except ChallengeFailure as cf:
            # Expected operational/scientific failure
            experiment_result = experiment_result or cf.experiment_result
            evaluation_report = evaluation_report or cf.evaluation_report
            benchmark_report = benchmark_report or cf.benchmark_report
            challenge_result = ChallengeResult(
                challenge_id=request.challenge_id,
                champion_id=request.champion_id,
                candidate_id=request.candidate_id,
                experiment_id=request.experiment.experiment_id,
                run_id=experiment_result.run_id if experiment_result is not None else "",
                result_id=experiment_result.result_id if experiment_result is not None else "",
                status=ChallengeStatus.FAILED,
                error_message=f"[{cf.stage}] {str(cf)}",
            )

        challenge_result = challenge_result.model_copy(update={
            "evaluation_report_id": str(uuid.uuid4()) if evaluation_report is not None else None,
            "benchmark_report_id": str(uuid.uuid4()) if benchmark_report is not None else None,
        })
        # Result and any reports needed for promotion are one durable record.
        self._persist_result(
            challenge_result, request, benchmark_context, evaluation_report, benchmark_report
        )

        return challenge_result

    # =========================================================================
    # PRIVATE STAGE METHODS
    # =========================================================================

    def _validate_request(
        self,
        request: ChallengeRequest,
        benchmark_context: BenchmarkContext,
    ) -> None:
        """
        Stage 1: Validate the challenge request and benchmark context.

        Validation rules:
          1. champion_id must exist in the registry.
          2. request.domain must match champion.domain.
          3. request.context_key must match champion.context_key.
          4. benchmark_context.reference_id must match request.champion_id.

        Mismatches are REJECTED, never silently corrected.

        Raises:
            ChallengeFailure: If validation fails (expected operational failure).
        """
        # Validate against ChampionRegistry
        validation_error = self._registry.validate_challenge(request)
        if validation_error is not None:
            raise ChallengeFailure(validation_error, stage="validation")

        champion = self._registry.get_champion(request.champion_id)
        if champion is None or champion.current_tenure is None:
            raise ChallengeFailure(
                "A challenge requires an established incumbent tenure.",
                stage="validation",
            )

        # Validate that the BenchmarkContext matches the requested champion
        if benchmark_context.reference_id != request.champion_id:
            raise ChallengeFailure(
                f"BenchmarkContext.reference_id ('{benchmark_context.reference_id}') "
                f"does not match ChallengeRequest.champion_id ('{request.champion_id}'). "
                f"The benchmark context must correspond to the champion being challenged.",
                stage="validation",
            )

        incumbent_evidence = champion.current_tenure.evidence_reference
        authoritative_metrics = (
            incumbent_evidence.get("benchmark_metrics")
            or incumbent_evidence.get("founding_metrics")
        )
        if not isinstance(authoritative_metrics, dict) or not authoritative_metrics:
            raise ChallengeFailure(
                f"Champion '{request.champion_id}' has no persisted metric snapshot.",
                stage="validation",
            )
        if benchmark_context.reference_type != "v2_champion":
            raise ChallengeFailure(
                "BenchmarkContext must identify its reference as a V2 champion.",
                stage="validation",
            )
        if benchmark_context.reference_metrics != authoritative_metrics:
            raise ChallengeFailure(
                "BenchmarkContext metrics differ from the persisted incumbent snapshot.",
                stage="validation",
            )
        authoritative_bootstrap = incumbent_evidence.get("metric_bootstrap")
        authoritative_bootstrap_evidence = incumbent_evidence.get("bootstrap_evidence")
        if (
            not isinstance(authoritative_bootstrap, dict)
            or not authoritative_bootstrap
            or benchmark_context.reference_bootstrap != authoritative_bootstrap
            or not isinstance(authoritative_bootstrap_evidence, dict)
            or benchmark_context.bootstrap_evidence != authoritative_bootstrap_evidence
        ):
            raise ChallengeFailure(
                "Champion lacks authoritative bootstrap evidence matching this benchmark context.",
                stage="validation",
            )
        expected_provenance = {
            "champion_id": champion.champion_id,
            "candidate_id": champion.current_tenure.candidate_id,
            "tenure_id": champion.current_tenure.tenure_id,
        }
        if any(
            benchmark_context.provenance.get(key) != value
            for key, value in expected_provenance.items()
        ):
            raise ChallengeFailure(
                "BenchmarkContext provenance does not match the persisted incumbent tenure.",
                stage="validation",
            )

        # Validate that the reference has metrics to compare against
        if not benchmark_context.reference_metrics:
            raise ChallengeFailure(
                f"BenchmarkContext for champion '{request.champion_id}' has no reference_metrics. "
                f"Cannot benchmark against an empty reference.",
                stage="validation",
            )

    def _execute_experiment(self, request: ChallengeRequest) -> ExperimentResult:
        """
        Stage 2: Execute the experiment contained in the ChallengeRequest.

        Submits the ExperimentContract to the ExperimentRunner and executes it.

        Returns:
            The ExperimentResult from execution.

        Raises:
            ChallengeFailure: If the experiment fails (expected operational failure).
        """
        contract = request.experiment

        try:
            # Submit the experiment
            experiment_id = self._runner.submit_experiment(contract)
        except ValueError as e:
            raise ChallengeFailure(
                f"Experiment submission failed: {str(e)}",
                stage="experiment_execution",
            )

        try:
            # Execute the experiment
            result = self._runner.execute_experiment(experiment_id)
        except ExperimentCapacityExceeded:
            raise
        except Exception as e:
            raise ChallengeFailure(
                f"Experiment execution failed: {str(e)}",
                stage="experiment_execution",
            )

        # Check if the experiment itself reported failure
        if result.status == ExperimentStatus.FAILED:
            raise ChallengeFailure(
                f"Experiment completed with FAILED status: {result.error_message or 'Unknown error'}",
                stage="experiment_execution",
                experiment_result=result,
            )

        return result

    def _evaluate_result(
        self,
        request: ChallengeRequest,
        experiment_result: ExperimentResult,
    ) -> EvaluationReport:
        """
        Stage 3: Evaluate baseline vs treatment using the Evaluator.

        Returns:
            An immutable EvaluationReport.

        Raises:
            ChallengeFailure: If evaluation fails (expected operational failure).
        """
        try:
            evaluation = self._evaluator.evaluate(request.experiment, experiment_result)
        except ValueError as e:
            raise ChallengeFailure(
                f"Evaluation failed: {str(e)}",
                stage="evaluation",
                experiment_result=experiment_result,
            )

        return evaluation

    def _benchmark_result(
        self,
        evaluation_report: EvaluationReport,
        experiment_result: ExperimentResult,
        benchmark_context: BenchmarkContext,
    ) -> BenchmarkReport:
        """
        Stage 4: Benchmark treatment vs reference using the Benchmark.

        Returns:
            An immutable BenchmarkReport.

        Raises:
            ChallengeFailure: If benchmarking fails unexpectedly.
        """
        bench_report = self._benchmark.evaluate(
            evaluation_report, experiment_result, benchmark_context
        )

        # Check if the benchmark reported a non-success status
        if bench_report.status == BenchmarkStatus.FAILED:
            raise ChallengeFailure(
                f"Benchmark execution failed: {bench_report.error_message or 'Unknown error'}",
                stage="benchmark",
                experiment_result=experiment_result,
                evaluation_report=evaluation_report,
                benchmark_report=bench_report,
            )

        if bench_report.status == BenchmarkStatus.UNAVAILABLE:
            raise ChallengeFailure(
                f"Benchmark reference unavailable: {bench_report.error_message or 'Unknown reason'}",
                stage="benchmark",
                experiment_result=experiment_result,
                evaluation_report=evaluation_report,
                benchmark_report=bench_report,
            )

        return bench_report

    def _build_challenge_result(
        self,
        request: ChallengeRequest,
        experiment_result: ExperimentResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
    ) -> ChallengeResult:
        """
        Stage 5: Package all outputs into an immutable ChallengeResult.

        The ChallengeResult is a lightweight factual record.
        Authoritative metric values remain in the BenchmarkReport.
        """
        return ChallengeResult(
            challenge_id=request.challenge_id,
            champion_id=request.champion_id,
            candidate_id=request.candidate_id,
            experiment_id=experiment_result.experiment_id,
            run_id=experiment_result.run_id,
            result_id=experiment_result.result_id,
            # IDs are assigned by run() and point to the reports retained by
            # ChallengeLedger in the same transaction as this result.
            evaluation_report_id=None,
            benchmark_report_id=None,
            metrics_better_than_champion=benchmark_report.metrics_better,
            metrics_worse_than_champion=benchmark_report.metrics_worse,
            metrics_unchanged=benchmark_report.metrics_unchanged,
            metrics_incomparable=benchmark_report.metrics_incomparable,
            status=ChallengeStatus.COMPLETED,
            error_message=None,
        )

    # =========================================================================
    # PERSISTENCE (ChallengeResults only — for auditability)
    # =========================================================================

    def _persist_result(
        self,
        result: ChallengeResult,
        request: ChallengeRequest,
        context: BenchmarkContext,
        evaluation_report: Optional[EvaluationReport],
        benchmark_report: Optional[BenchmarkReport],
    ) -> None:
        self._ledger.complete(result, request, context, evaluation_report, benchmark_report)

    def get_result(self, challenge_id: str) -> Optional[ChallengeResult]:
        """Retrieves a previously persisted ChallengeResult."""
        return self._ledger.get_result(challenge_id)

    def get_evaluation_report(self, report_id: str) -> Optional[EvaluationReport]:
        """Load the immutable evaluation evidence referenced by a challenge."""
        return self._ledger.get_evaluation_report(report_id)

    def get_benchmark_report(self, report_id: str) -> Optional[BenchmarkReport]:
        """Load the immutable benchmark evidence referenced by a challenge."""
        return self._ledger.get_benchmark_report(report_id)

    def list_results(self) -> list:
        """Lists persisted challenges, including any left RUNNING after a crash."""
        return self._ledger.list_results()

    def operational_status(self) -> dict:
        """Expose challenge persistence and in-progress work for operations."""
        return self._ledger.stats()
