"""Forge's durable model registry and local model artifact store.

Registry metadata is kept in SQLite so model and experiment records have one
transactional, process-safe source of truth. The former JSON files are imported
once and retained as recovery inputs. Pickled artifacts are only deserialized
after their recorded digest and storage boundary have been verified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import pickle
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional
from Forge.core.knowledge_outbox import KnowledgeOutbox

log = logging.getLogger("forge.model_registry")


class ModelRegistry:
    def __init__(self, storage_dir: Optional[str] = None,
                 legacy_storage_dir: Optional[str] = None):
        forge_root = Path(__file__).resolve().parents[1]
        default_dir = forge_root / "memory" / "models"
        self.storage_dir = Path(storage_dir or default_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.storage_dir / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self._index = self.storage_dir / "registry.json"
        self._exp = self.storage_dir / "experiments.json"
        self._database = self.storage_dir / "registry.sqlite3"
        self._lock = threading.RLock()
        self._models: Dict[str, Dict[str, Any]] = {}
        self._experiments: Dict[str, Dict[str, Any]] = {}
        if legacy_storage_dir is None and storage_dir is None:
            legacy_storage_dir = str(forge_root / "models")
        if legacy_storage_dir:
            self._migrate_legacy_storage(Path(legacy_storage_dir).resolve())
        self._initialize_database()
        self._load()

    def _migrate_legacy_storage(self, legacy_dir: Path) -> None:
        """Copy the former Forge/models registry into Forge/memory/models once."""
        if legacy_dir == self.storage_dir or not legacy_dir.is_dir():
            return
        legacy_db = legacy_dir / "registry.sqlite3"
        if not self._database.exists() and legacy_db.is_file():
            try:
                with closing(sqlite3.connect(str(legacy_db), timeout=30.0)) as source, \
                        closing(sqlite3.connect(str(self._database), timeout=30.0)) as target:
                    source.backup(target)
                    check = target.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0] != "ok":
                        raise RuntimeError("legacy model registry failed SQLite integrity check")
            except Exception as exc:
                self._database.unlink(missing_ok=True)
                raise RuntimeError("Could not safely migrate Forge/models registry") from exc

        for name in ("registry.json", "experiments.json"):
            source = legacy_dir / name
            target = self.storage_dir / name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)

        source_artifacts = legacy_dir / "artifacts"
        if source_artifacts.is_dir() and not any(self.artifacts.iterdir()):
            try:
                shutil.copytree(source_artifacts, self.artifacts, dirs_exist_ok=True)
            except OSError as exc:
                raise RuntimeError("Could not safely migrate Forge model artifacts") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @property
    def database_path(self) -> Path:
        return self._database

    @staticmethod
    def _encode(record: Dict[str, Any]) -> str:
        return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _read_legacy_snapshot(self):
        models: Dict[str, Dict[str, Any]] = {}
        experiments: Dict[str, Dict[str, Any]] = {}
        if self._index.exists():
            try:
                raw = json.loads(self._index.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Cannot safely migrate model registry file {self._index}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise RuntimeError(f"Legacy model registry {self._index} must be a JSON object.")
            if "schema_version" in raw:
                if raw.get("schema_version") != 1:
                    raise RuntimeError(
                        f"Unsupported legacy model registry schema: {raw.get('schema_version')}"
                    )
                models = raw.get("models")
                experiments = raw.get("experiments")
                if not isinstance(models, dict) or not isinstance(experiments, dict):
                    raise RuntimeError("Legacy registry snapshot has invalid record maps.")
            else:
                models = raw

        if not experiments and self._exp.exists():
            try:
                experiments = json.loads(self._exp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Cannot safely migrate experiment registry {self._exp}: {exc}"
                ) from exc
            if not isinstance(experiments, dict):
                raise RuntimeError(f"Legacy experiment registry {self._exp} must be a JSON object.")

        if not all(isinstance(record, dict) for record in models.values()):
            raise RuntimeError("Legacy model registry contains a non-object model record.")
        if not all(isinstance(record, dict) for record in experiments.values()):
            raise RuntimeError("Legacy experiment registry contains a non-object record.")

        for model_id, record in list(models.items()):
            if record.get("model_id") not in (None, model_id):
                raise RuntimeError(
                    f"Legacy model key '{model_id}' does not match its model_id field."
                )
            record.setdefault("model_id", model_id)
            status = record.get("status", "experimental")
            if status not in {"experimental", "candidate", "production"}:
                raise RuntimeError(
                    f"Legacy model '{model_id}' has unsupported lifecycle status '{status}'."
                )

        # The old API allowed production status to be asserted with a boolean
        # supplied by the caller. Preserve that historical fact, but do not
        # import it as trusted production state.
        for model_id, record in list(models.items()):
            if record.get("status") == "production":
                quarantined = deepcopy(record)
                quarantined["status"] = "candidate"
                quarantined["legacy_unverified_status"] = "production"
                models[model_id] = quarantined
        for experiment_id, record in experiments.items():
            if record.get("experiment_id") not in (None, experiment_id):
                raise RuntimeError(
                    f"Legacy experiment key '{experiment_id}' does not match its experiment_id field."
                )
            record.setdefault("experiment_id", experiment_id)
        return models, experiments

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS registry_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS models "
                "(model_id TEXT PRIMARY KEY, domain TEXT NOT NULL, status TEXT NOT NULL, "
                "record_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS models_domain_status_idx "
                "ON models(domain, status)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiments "
                "(experiment_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_outbox ("
                "event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE, "
                "payload_json TEXT NOT NULL, status TEXT NOT NULL "
                "CHECK(status IN ('pending', 'delivered')), "
                "attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "delivered_at TEXT, last_error TEXT)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_outbox_pending "
                "ON knowledge_outbox(status, created_at)"
            )
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Model registry SQLite integrity check failed: {integrity}")
            connection.execute("BEGIN IMMEDIATE")
            schema = connection.execute(
                "SELECT value FROM registry_metadata WHERE key = ?", ("schema_version",)
            ).fetchone()
            if schema is not None and schema["value"] not in {"1", "2"}:
                raise RuntimeError(
                    f"Unsupported model registry database schema version: {schema['value']}"
                )
            if schema is None:
                connection.execute(
                    "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
                    ("schema_version", "2"),
                )
            elif schema["value"] == "1":
                connection.execute(
                    "UPDATE registry_metadata SET value = ? WHERE key = ?",
                    ("2", "schema_version"),
                )
            migrated = connection.execute(
                "SELECT value FROM registry_metadata WHERE key = ?",
                ("legacy_json_migrated",),
            ).fetchone()
            if migrated is None:
                models, experiments = self._read_legacy_snapshot()
                connection.executemany(
                    "INSERT INTO models (model_id, domain, status, record_json) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            model_id,
                            str(record.get("domain", "unknown")),
                            str(record.get("status", "experimental")),
                            self._encode(record),
                        )
                        for model_id, record in models.items()
                    ],
                )
                connection.executemany(
                    "INSERT INTO experiments (experiment_id, record_json) VALUES (?, ?)",
                    [(experiment_id, self._encode(record))
                     for experiment_id, record in experiments.items()],
                )
                connection.execute(
                    "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
                    ("legacy_json_migrated", "1"),
                )
            connection.commit()

    def _load(self) -> None:
        with closing(self._connect()) as connection:
            model_rows = connection.execute(
                "SELECT model_id, record_json FROM models"
            ).fetchall()
            experiment_rows = connection.execute(
                "SELECT experiment_id, record_json FROM experiments"
            ).fetchall()
        self._models = {
            row["model_id"]: json.loads(row["record_json"]) for row in model_rows
        }
        self._experiments = {
            row["experiment_id"]: json.loads(row["record_json"])
            for row in experiment_rows
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as artifact_file:
            for block in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def record_experiment(
        self,
        exp: Dict[str, Any],
        *,
        knowledge_lesson: Optional[Dict[str, str]] = None,
    ) -> str:
        with self._lock, closing(self._connect()) as connection:
            experiment_id = exp.get("experiment_id") or f"exp-{uuid.uuid4().hex[:10]}"
            record = deepcopy(exp)
            record["experiment_id"] = experiment_id
            record.setdefault("recorded_at", time.time())
            payload = self._encode(record)
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT record_json FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if existing_row is not None:
                existing = json.loads(existing_row["record_json"])
                prior_identity = {key: value for key, value in existing.items()
                                  if key != "recorded_at"}
                requested_identity = {key: value for key, value in record.items()
                                      if key != "recorded_at"}
                if prior_identity != requested_identity:
                    connection.rollback()
                    raise ValueError(
                        f"experiment_id '{experiment_id}' is already registered with different content"
                    )
                record = existing
            else:
                connection.execute(
                    "INSERT INTO experiments (experiment_id, record_json) VALUES (?, ?)",
                    (experiment_id, payload),
                )
            if knowledge_lesson is not None:
                if not isinstance(knowledge_lesson, dict):
                    connection.rollback()
                    raise ValueError("knowledge_lesson must be a mapping")
                try:
                    KnowledgeOutbox.enqueue_in_connection(
                        connection,
                        knowledge_lesson["content"],
                        backend=knowledge_lesson["backend"],
                        model_name=knowledge_lesson["model_name"],
                        idempotency_key=f"experiment:{experiment_id}",
                    )
                except (KeyError, TypeError, ValueError):
                    connection.rollback()
                    raise
            connection.commit()
            self._experiments[experiment_id] = record
            return experiment_id

    def register_model(
        self,
        name,
        version,
        domain,
        task_type,
        metrics,
        experiment_id,
        model_obj=None,
        hyperparams=None,
        feature_baseline=None,
        source_run_id: Optional[str] = None,
        data_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Model name must be a non-empty string.")
            if not isinstance(version, str) or not version.strip():
                raise ValueError("Model version must be a non-empty string.")
            if not isinstance(domain, str) or not domain.strip():
                raise ValueError("Model domain must be a non-empty string.")
            if not isinstance(experiment_id, str) or not experiment_id.strip():
                raise ValueError("Model registration requires an experiment_id.")
            if not isinstance(metrics, dict):
                raise ValueError("Model metrics must be a mapping.")
            model_id = f"model-{uuid.uuid4().hex[:10]}"
            artifact_path = None
            artifact_digest = None
            if model_obj is not None:
                artifact = self.artifacts / f"{model_id}.pkl"
                try:
                    with artifact.open("wb") as artifact_file:
                        pickle.dump(model_obj, artifact_file)
                        artifact_file.flush()
                        os.fsync(artifact_file.fileno())
                    artifact_digest = self._sha256_file(artifact)
                    artifact_path = str(artifact)
                except Exception:
                    artifact.unlink(missing_ok=True)
                    raise

            record = {
                "model_id": model_id,
                "name": name,
                "version": version,
                "domain": domain,
                "task_type": task_type,
                "metrics": deepcopy(metrics),
                "experiment_id": experiment_id,
                "hyperparameters": deepcopy(hyperparams or {}),
                "status": "experimental",
                "artifact_path": artifact_path,
                "artifact_sha256": artifact_digest,
                "feature_baseline": deepcopy(feature_baseline or {}),
                "registered_at": time.time(),
                "source_run_id": source_run_id,
                "data_fingerprint": data_fingerprint,
            }
            try:
                payload = self._encode(record)
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO models (model_id, domain, status, record_json) "
                        "VALUES (?, ?, ?, ?)",
                        (model_id, str(domain), "experimental", payload),
                    )
                    connection.commit()
            except Exception:
                if artifact_path:
                    Path(artifact_path).unlink(missing_ok=True)
                raise
            self._models[model_id] = record
            return deepcopy(record)

    def load_model(self, model_id):
        """Load an artifact after registry integrity checks.

        Serving code must use ``load_model_for_deployment`` so the approved
        rollout digest is also checked. This unrestricted method remains for
        offline evaluation and explicitly trusted internal callers.
        """
        return self._load_model(model_id, deployment_digest=None)

    def load_model_for_deployment(self, model_id: str, approved_digest: str):
        """Load only when registry metadata and artifact bytes match approval."""
        if (
            not isinstance(approved_digest, str)
            or len(approved_digest) != 64
            or any(char not in "0123456789abcdef" for char in approved_digest.lower())
        ):
            log.error("missing or malformed approved deployment digest: model_id=%s", model_id)
            return None
        return self._load_model(model_id, deployment_digest=approved_digest)

    def _load_model(self, model_id: str, deployment_digest: Optional[str]):
        record = self.get_model(model_id)
        if not record or not record.get("artifact_path"):
            log.warning("model artifact unavailable: model_id=%s", model_id)
            return None
        registry_digest = record.get("artifact_sha256")
        if deployment_digest is not None and registry_digest != deployment_digest:
            log.error("model artifact digest does not match deployment approval: model_id=%s", model_id)
            return None
        artifact_path = Path(record["artifact_path"])
        try:
            resolved_path = artifact_path.resolve(strict=True)
            if self.artifacts.resolve() not in resolved_path.parents:
                log.error("model artifact path escaped registry boundary: model_id=%s", model_id)
                return None
            # Hash and deserialize the same byte snapshot to avoid a path-swap
            # between integrity verification and pickle loading.
            artifact_bytes = resolved_path.read_bytes()
            actual_digest = hashlib.sha256(artifact_bytes).hexdigest()
            if (
                not registry_digest
                or actual_digest != registry_digest
                or (deployment_digest is not None and actual_digest != deployment_digest)
            ):
                log.error("model artifact digest mismatch: model_id=%s", model_id)
                return None
            return pickle.loads(artifact_bytes)
        except Exception as exc:
            log.exception("model artifact load failed: model_id=%s error=%s", model_id, type(exc).__name__)
            return None

    def verify_artifact(self, model_id: str) -> bool:
        """Check artifact path and digest without deserializing the pickle."""
        record = self.get_model(model_id)
        if not record or not record.get("artifact_path"):
            return False
        try:
            resolved_path = Path(record["artifact_path"]).resolve(strict=True)
            if self.artifacts.resolve() not in resolved_path.parents:
                return False
            expected_digest = record.get("artifact_sha256")
            return bool(
                expected_digest
                and self._sha256_file(resolved_path) == expected_digest
            )
        except (OSError, ValueError):
            return False

    def attach_source_lineage(
        self,
        model_id: str,
        source_experiment_id: str,
        source_run_id: str,
        source_result_id: Optional[str] = None,
        data_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attach V2 experiment provenance to an experimental artifact once.

        Existing V1 experiment identity is preserved in ``experiment_id``;
        these separate source fields point to the governing V2 run and result.
        """
        for label, value in (
            ("source_experiment_id", source_experiment_id),
            ("source_run_id", source_run_id),
            ("source_result_id", source_result_id),
            ("data_fingerprint", data_fingerprint),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{label} must be a non-empty string when provided")
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT record_json, status FROM models WHERE model_id = ?", (model_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Model '{model_id}' not found")
                record = json.loads(row["record_json"])
                if row["status"] != "experimental":
                    raise ValueError("V2 source lineage can only be attached to an experimental model")
                supplied = {
                    "source_experiment_id": source_experiment_id,
                    "source_run_id": source_run_id,
                    "source_result_id": source_result_id,
                    "data_fingerprint": data_fingerprint,
                }
                for key, value in supplied.items():
                    if value is None:
                        continue
                    previous = record.get(key)
                    if previous not in (None, value):
                        raise ValueError(f"Model already has conflicting {key} provenance")
                    record[key] = value
                connection.execute(
                    "UPDATE models SET record_json = ? WHERE model_id = ? AND status = 'experimental'",
                    (self._encode(record), model_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RuntimeError("Model lifecycle changed while attaching source lineage")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._models[model_id] = record
            return deepcopy(record)

    def promote(self, model_id, to_status, benchmark_passed=False) -> Dict[str, Any]:
        """Move an experimental model to candidate status only.

        A caller-supplied ``benchmark_passed`` boolean is not evidence or
        authorization. Production activation belongs to a governed staged
        deployment controller, which is not configured yet.
        """
        if to_status == "production":
            return {
                "status": "error",
                "message": (
                    "direct production promotion is disabled; benchmark_passed "
                    "is not evidence or authorization. Use the governed staged "
                    "deployment controller."
                ),
            }
        if to_status != "candidate":
            return {"status": "error", "message": "only experimental-to-candidate promotion is supported here"}

        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM models WHERE model_id = ?", (model_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return {"status": "error", "message": "model not found"}
            record = json.loads(row["record_json"])
            current_status = record.get("status")
            if current_status == "candidate":
                connection.commit()
                self._models[model_id] = record
                return {"status": "complete", "model": deepcopy(record)}
            if current_status != "experimental":
                connection.rollback()
                return {
                    "status": "error",
                    "message": f"invalid promotion from '{current_status}' to candidate",
                }
            record["status"] = "candidate"
            connection.execute(
                "UPDATE models SET status = ?, record_json = ? WHERE model_id = ?",
                ("candidate", self._encode(record), model_id),
            )
            connection.commit()
            self._models[model_id] = record
            return {"status": "complete", "model": deepcopy(record)}

    def leaderboard(self, domain, metric="accuracy") -> List[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT record_json FROM models WHERE domain = ?", (str(domain),)
            ).fetchall()
        models = [json.loads(row["record_json"]) for row in rows]
        def metric_score(model):
            metrics = model.get("metrics")
            value = metrics.get(metric) if isinstance(metrics, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return float("-inf")
            return value if math.isfinite(value) else float("-inf")

        models.sort(key=metric_score, reverse=True)
        return deepcopy(models[:20])

    def get_model(self, model_id):
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_json FROM models WHERE model_id = ?", (model_id,)
            ).fetchone()
        if row is None:
            self._models.pop(model_id, None)
            return None
        record = json.loads(row["record_json"])
        self._models[model_id] = record
        return deepcopy(record)

    def detect_drift(self, model_id: str, live_features: List[List[float]]) -> Dict[str, Any]:
        """Compute standardized mean shift against the stored training baseline."""
        rec = self.get_model(model_id)
        if not rec:
            return {"status": "error", "message": "model not found"}
        baseline = rec.get("feature_baseline", {})
        if not baseline or not live_features:
            return {"status": "error", "message": "no baseline or no live data"}
        means = baseline.get("means", [])
        stds = baseline.get("stds", [])
        dimensions = len(live_features[0])
        drifts = []
        for index in range(min(dimensions, len(means))):
            live_column = [row[index] for row in live_features]
            live_mean = sum(live_column) / len(live_column)
            baseline_std = stds[index] if index < len(stds) and stds[index] else 1.0
            shift = abs(live_mean - means[index]) / baseline_std
            if not math.isfinite(shift):
                continue
            shift = round(shift, 3)
            if shift > 0.25:
                drifts.append({"feature_index": index, "shift_sigma": shift})
        return {
            "status": "complete",
            "model_id": model_id,
            "drifted_features": drifts,
            "drift_detected": bool(drifts),
            "recommendation": "retrain" if drifts else "stable",
        }

    def stats(self) -> Dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            model_count = connection.execute("SELECT COUNT(*) FROM models").fetchone()[0]
            experiment_count = connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM models GROUP BY status"
            ).fetchall()
        return {
            "database_integrity": integrity == "ok",
            "models": model_count,
            "experiments": experiment_count,
            "by_status": {row["status"]: row["count"] for row in status_rows},
            "ready": integrity == "ok",
        }
