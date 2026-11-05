"""SQLite-backed durable Chronicle state with one-time JSON migration.

The former JSON store rewrote the complete state file for every mutation. This
backend keeps the same StateStore API while persisting only the affected record.
The original JSON file is preserved as a dated legacy backup on migration.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("chronicle.state_store")

_OWNER_BOUND_KEYS = {
    "champion_library": "oracle",
    "champion_history": "oracle",
    "evolved_strategies": "oracle",
    "fusion_weights": "oracle",
    "champion_confidence": "oracle",
}

# State is a current projection, not an append-only event log. Immutable Oracle
# decision/heartbeat/cost-profile records live under their own versioned keys;
# keeping a full copy of every projection revision duplicates those events and
# causes quadratic storage growth. Other state retains only a short diagnostic
# trail by default. Add exact keys/prefixes here only when a separate durable
# event record does not already preserve the underlying transition.
_DEFAULT_HISTORY_LIMIT = 20
_STATE_HISTORY_LIMITS = {
    "forge.oracle.decision_summary.": 0,
    "forge.oracle.trader_heartbeat.latest.": 0,
    "forge.oracle.cost_profile.latest.": 0,
    "forge.oracle.strategy_catalog.current": 0,
    "atlas.external_query_feedback.v1": 0,
    "forge.oracle.no_trade_job.": 5,
    "forge.oracle.symbol_scan.": 5,
    "forge.oracle.heartbeat_gap_case.": 5,
    "forge.oracle.operational_no_trade_case.": 5,
    "forge.oracle.no_trade_case.": 5,
}


def history_limit_for_key(key: str) -> int:
    """Maximum full snapshots retained for a mutable state key."""
    for prefix, limit in sorted(_STATE_HISTORY_LIMITS.items(),
                                key=lambda item: len(item[0]), reverse=True):
        if key == prefix or key.startswith(prefix):
            return limit
    return _DEFAULT_HISTORY_LIMIT


def _bounded_history(key: str, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    limit = history_limit_for_key(key)
    return history[-limit:] if limit else []


class StateStore:
    """Durable per-key current state with bounded diagnostic revision history."""

    def __init__(self, storage_dir: str):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.storage_dir / "state_store.json"  # compatibility/manifest path
        self.database_path = self.storage_dir / "state_store.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self, path: Optional[Path] = None) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(path or self.database_path), timeout=60.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS state_records ("
            "key TEXT PRIMARY KEY NOT NULL, record_json TEXT NOT NULL)"
        )

    @staticmethod
    def _validate_record(key: Any, record: Any) -> Dict[str, Any]:
        if not isinstance(key, str) or not key or not isinstance(record, dict) or "value" not in record:
            raise ValueError(f"Chronicle state store contains an invalid record for key {key!r}")
        record = dict(record)
        record.setdefault("history", [])
        record.setdefault("revision", 0)
        if (not isinstance(record["revision"], int) or isinstance(record["revision"], bool)
                or record["revision"] < 0):
            raise ValueError(f"Chronicle state store contains an invalid revision for key {key!r}")
        if not isinstance(record.get("deleted", False), bool):
            raise ValueError(f"Chronicle state store contains an invalid deletion marker for key {key!r}")
        if not isinstance(record["history"], list) or any(
            not isinstance(entry, dict) or "value" not in entry for entry in record["history"]
        ):
            raise ValueError(f"Chronicle state store contains invalid history for key {key!r}")
        return record

    def _read_legacy_json(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Chronicle state store is unreadable: {self.path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Chronicle state store has an invalid schema: {self.path}")
        return {key: self._validate_record(key, record) for key, record in data.items()}

    def _initialize(self) -> None:
        if self.database_path.exists():
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                self._create_schema(connection)
            return

        legacy_data = self._read_legacy_json()
        temporary_database = self.database_path.with_name(
            self.database_path.name + ".migrating"
        )
        for suffix in ("", "-wal", "-shm"):
            Path(str(temporary_database) + suffix).unlink(missing_ok=True)

        try:
            connection = self._connect(temporary_database)
            try:
                self._create_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT INTO state_records(key, record_json) VALUES (?, ?)",
                    ((key, json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                                      allow_nan=False))
                     for key, record in legacy_data.items()),
                )
                connection.commit()
                check = connection.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("Chronicle SQLite state migration failed integrity check")
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

            os.replace(temporary_database, self.database_path)
            if self.path.exists():
                backup = self.path.with_name("state_store.json.legacy")
                if backup.exists():
                    backup = self.path.with_name(
                        f"state_store.json.legacy.{time.strftime('%Y%m%dT%H%M%S')}"
                    )
                os.replace(self.path, backup)
                log.warning("Preserved pre-migration Chronicle state at %s", backup)
            self._write_manifest()
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
            log.info("Chronicle SQLite state online (%d migrated records)", len(legacy_data))
        except Exception:
            for suffix in ("", "-wal", "-shm"):
                Path(str(temporary_database) + suffix).unlink(missing_ok=True)
            raise

    def _write_manifest(self) -> None:
        manifest = {
            "schema": "chronicle.state_store.sqlite.v1",
            "database": self.database_path.name,
            "description": "Authoritative Chronicle state is stored in the SQLite database.",
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.storage_dir,
                prefix=".state_store_manifest.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(manifest, handle, sort_keys=True, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _encode(record: Dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _decode(value: Optional[str]) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        record = json.loads(value)
        return StateStore._validate_record("<database>", record)

    def set(
        self, key: str, value: Any, owner: str = "",
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT record_json FROM state_records WHERE key = ?", (key,)
                ).fetchone()
                existing = self._decode(row[0]) if row else None
                current_revision = existing.get("revision", 0) if existing else 0
                required_owner = _OWNER_BOUND_KEYS.get(key)
                if required_owner is not None and owner != required_owner:
                    connection.rollback()
                    return {"status": "forbidden", "key": key,
                            "message": "state key is owned by another agent"}
                if existing and existing.get("owner") and existing["owner"] != owner:
                    connection.rollback()
                    return {"status": "forbidden", "key": key,
                            "message": "state key owner cannot be changed"}
                if expected_revision is not None and (
                    not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                    or expected_revision < 0
                ):
                    connection.rollback()
                    return {"status": "error", "key": key,
                            "message": "expected_revision must be a non-negative integer"}
                if expected_revision is not None and expected_revision != current_revision:
                    connection.rollback()
                    return {"status": "conflict", "key": key,
                            "revision": current_revision,
                            "message": "state revision changed before this write"}

                history = list(existing.get("history", [])) if existing else []
                if existing is not None:
                    history.append({"value": existing["value"],
                                    "owner": existing.get("owner", ""),
                                    "updated_at": existing.get("updated_at")})
                history = _bounded_history(key, history)
                next_revision = current_revision + 1
                record = {"value": value, "owner": owner, "updated_at": time.time(),
                          "history": history, "revision": next_revision}
                connection.execute(
                    "INSERT INTO state_records(key, record_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET record_json=excluded.record_json",
                    (key, self._encode(record)),
                )
                connection.commit()
                return {"status": "complete", "key": key, "revision": next_revision}
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                connection.rollback()
                log.warning("could not persist Chronicle state key %s: %s", key, type(exc).__name__)
                return {"status": "error", "key": key, "message": "state was not durably saved"}
            finally:
                connection.close()

    def compare_and_set_version(
        self, key: str, expected_version: int, new_value: Any, owner: str = ""
    ) -> bool:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT record_json FROM state_records WHERE key = ?", (key,)
                ).fetchone()
                existing = self._decode(row[0]) if row else None
                required_owner = _OWNER_BOUND_KEYS.get(key)
                if required_owner is not None and owner != required_owner:
                    connection.rollback()
                    return False
                if existing and existing.get("owner") and existing["owner"] != owner:
                    connection.rollback()
                    return False
                current_value = existing.get("value") if existing else None
                current_version = (current_value.get("revision", current_value.get("version_number", 0))
                                   if isinstance(current_value, dict) else 0)
                if current_version != expected_version:
                    connection.rollback()
                    return False
                history = list(existing.get("history", [])) if existing else []
                if existing is not None:
                    history.append({"value": existing["value"],
                                    "owner": existing.get("owner", ""),
                                    "updated_at": existing.get("updated_at")})
                history = _bounded_history(key, history)
                record = {"value": new_value, "owner": owner, "updated_at": time.time(),
                          "history": history,
                          "revision": (existing.get("revision", 0) if existing else 0) + 1}
                connection.execute(
                    "INSERT INTO state_records(key, record_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET record_json=excluded.record_json",
                    (key, self._encode(record)),
                )
                connection.commit()
                return True
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                connection.rollback()
                log.warning("could not compare-and-set Chronicle key %s: %s", key, type(exc).__name__)
                return False
            finally:
                connection.close()

    def get(self, key: str) -> Optional[Any]:
        record = self.get_with_revision(key)
        return record["value"] if record["found"] else None

    def get_with_revision(self, key: str) -> Dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_json FROM state_records WHERE key = ?", (key,)
            ).fetchone()
        record = self._decode(row[0]) if row else None
        if record is None or record.get("deleted", False):
            return {"value": None, "found": False,
                    "deleted": bool(record and record.get("deleted", False)),
                    "revision": record.get("revision", 0) if record else 0}
        return {"value": record["value"], "found": record["value"] is not None,
                "deleted": False, "revision": record.get("revision", 0)}

    def get_revision(self, key: str) -> int:
        return int(self.get_with_revision(key)["revision"])

    def get_history(self, key: str, include_current: bool = True) -> List[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_json FROM state_records WHERE key = ?", (key,)
            ).fetchone()
        record = self._decode(row[0]) if row else None
        if record is None:
            return []
        trail = list(record.get("history", []))
        if include_current and not record.get("deleted", False):
            trail.append({"value": record["value"], "owner": record.get("owner", ""),
                          "updated_at": record.get("updated_at")})
        return trail

    def delete(
        self, key: str, expected_revision: Optional[int] = None, owner: str = ""
    ) -> Dict[str, Any]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT record_json FROM state_records WHERE key = ?", (key,)
                ).fetchone()
                existing = self._decode(row[0]) if row else None
                current_revision = existing.get("revision", 0) if existing else 0
                required_owner = _OWNER_BOUND_KEYS.get(key)
                if required_owner is not None and owner != required_owner:
                    connection.rollback()
                    return {"status": "forbidden", "deleted": False,
                            "message": "state key is owned by another agent"}
                if existing and existing.get("owner") and existing["owner"] != owner:
                    connection.rollback()
                    return {"status": "forbidden", "deleted": False,
                            "message": "state key owner cannot be changed"}
                if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                        or expected_revision < 0):
                    connection.rollback()
                    return {"status": "error", "deleted": False,
                            "message": "expected_revision must be a non-negative integer"}
                if expected_revision != current_revision:
                    connection.rollback()
                    return {"status": "conflict", "deleted": False,
                            "revision": current_revision,
                            "message": "state revision changed before deletion"}
                existed = existing is not None and not existing.get("deleted", False)
                if not existed:
                    connection.rollback()
                    return {"status": "complete", "deleted": False,
                            "revision": current_revision}
                history = list(existing.get("history", []))
                history.append({"value": existing["value"],
                                "owner": existing.get("owner", ""),
                                "updated_at": existing.get("updated_at")})
                history = _bounded_history(key, history)
                next_revision = current_revision + 1
                tombstone = {"value": existing["value"], "owner": existing.get("owner", ""),
                             "updated_at": time.time(), "history": history,
                             "revision": next_revision, "deleted": True}
                connection.execute(
                    "UPDATE state_records SET record_json = ? WHERE key = ?",
                    (self._encode(tombstone), key),
                )
                connection.commit()
                return {"status": "complete", "deleted": True, "revision": next_revision}
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                connection.rollback()
                log.warning("could not delete Chronicle state key %s: %s", key, type(exc).__name__)
                return {"status": "error", "deleted": False,
                        "message": "state deletion was not durably saved"}
            finally:
                connection.close()

    def export_snapshot(self, prefix: str) -> Dict[str, Any]:
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("a non-empty Chronicle snapshot prefix is required")
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT key, record_json FROM state_records WHERE substr(key, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
        records = {row["key"]: self._decode(row["record_json"]) for row in rows}
        records_json = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
        snapshot = {"schema": "chronicle.state_snapshot.v1", "prefix": prefix,
                    "records": records, "record_count": len(records),
                    "records_sha256": hashlib.sha256(records_json.encode("utf-8")).hexdigest()}
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 100 * 1024 * 1024:
            raise ValueError("Chronicle state snapshot exceeds the 100 MiB limit")
        return snapshot

    def restore_snapshot(self, snapshot: Dict[str, Any], prefix: str) -> Dict[str, Any]:
        if (not isinstance(prefix, str) or not prefix.strip()
                or not isinstance(snapshot, dict)
                or snapshot.get("schema") != "chronicle.state_snapshot.v1"
                or snapshot.get("prefix") != prefix
                or not isinstance(snapshot.get("records"), dict)):
            return {"status": "error", "message": "Chronicle state snapshot schema or namespace is invalid"}
        records = snapshot["records"]
        try:
            encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Chronicle state snapshot contains invalid JSON values"}
        if (snapshot.get("record_count") != len(records)
                or snapshot.get("records_sha256") != hashlib.sha256(encoded.encode("utf-8")).hexdigest()):
            return {"status": "error", "message": "Chronicle state snapshot count or fingerprint is invalid"}
        if len(encoded.encode("utf-8")) > 100 * 1024 * 1024:
            return {"status": "error", "message": "Chronicle state snapshot exceeds the 100 MiB limit"}
        normalized: Dict[str, Dict[str, Any]] = {}
        for key, record in records.items():
            try:
                value = self._validate_record(key, record)
            except ValueError:
                return {"status": "error", "message": f"Chronicle snapshot record {key!r} is malformed"}
            value["history"] = _bounded_history(key, value["history"])
            if not key.startswith(prefix) or not isinstance(value.get("owner", ""), str):
                return {"status": "error", "message": "Chronicle state snapshot contains an invalid namespace or owner"}
            required_owner = _OWNER_BOUND_KEYS.get(key)
            if required_owner is not None and value.get("owner", "") != required_owner:
                return {"status": "error", "message": f"Chronicle snapshot owner for {key!r} is invalid"}
            normalized[key] = value

        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                writes: Dict[str, Dict[str, Any]] = {}
                skipped_newer = 0
                for key, incoming in normalized.items():
                    row = connection.execute(
                        "SELECT record_json FROM state_records WHERE key = ?", (key,)
                    ).fetchone()
                    existing = self._decode(row[0]) if row else None
                    if existing is not None:
                        if (existing.get("owner") and incoming.get("owner")
                                and existing["owner"] != incoming["owner"]):
                            connection.rollback()
                            return {"status": "error", "message": f"Chronicle snapshot owner conflicts for {key!r}"}
                        current_revision = int(existing.get("revision", 0))
                        incoming_revision = incoming["revision"]
                        if current_revision > incoming_revision:
                            skipped_newer += 1
                            continue
                        if current_revision == incoming_revision:
                            if existing != incoming:
                                connection.rollback()
                                return {"status": "conflict", "message": f"Chronicle snapshot revision conflicts for {key!r}"}
                            continue
                    writes[key] = incoming
                connection.executemany(
                    "INSERT INTO state_records(key, record_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET record_json=excluded.record_json",
                    ((key, self._encode(record)) for key, record in writes.items()),
                )
                connection.commit()
                return {"status": "complete", "restored": len(writes),
                        "skipped_newer": skipped_newer, "record_count": len(records)}
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                connection.rollback()
                log.warning("could not restore Chronicle state snapshot: %s", type(exc).__name__)
                return {"status": "error", "message": "Chronicle state snapshot could not be durably restored"}
            finally:
                connection.close()

    def list_keys(self, prefix: str = "") -> list:
        with self._lock, closing(self._connect()) as connection:
            if prefix:
                rows = connection.execute(
                    "SELECT key, record_json FROM state_records WHERE substr(key, 1, ?) = ?",
                    (len(prefix), prefix),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT key, record_json FROM state_records"
                ).fetchall()
        return [row["key"] for row in rows
                if not self._decode(row["record_json"]).get("deleted", False)]
