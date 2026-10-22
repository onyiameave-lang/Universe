"""Durable, idempotent delivery queue for Forge-to-Chronicle memories."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict


_DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "memory" / "knowledge_outbox.sqlite3"


class KnowledgeOutbox:
    """Persist memory events locally until Chronicle confirms durable storage.

    Delivery uses a stable memory ID. If Forge stops after Chronicle stores the
    record but before this ledger marks it delivered, retrying overwrites the
    same Chronicle record instead of creating a duplicate.
    """

    def __init__(self, database_path: str | Path = _DEFAULT_DATABASE):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_outbox (
                    event_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'delivered')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT,
                    claim_token TEXT,
                    claim_until TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_outbox_pending
                    ON knowledge_outbox(status, created_at);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(knowledge_outbox)"
                ).fetchall()
            }
            if "claim_token" not in columns:
                connection.execute(
                    "ALTER TABLE knowledge_outbox ADD COLUMN claim_token TEXT"
                )
            if "claim_until" not in columns:
                connection.execute(
                    "ALTER TABLE knowledge_outbox ADD COLUMN claim_until TEXT"
                )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=30, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def enqueue(
        self,
        content: str,
        *,
        backend: str,
        model_name: str,
        idempotency_key: str | None = None,
    ) -> str:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            event_id = self.enqueue_in_connection(
                connection,
                content,
                backend=backend,
                model_name=model_name,
                idempotency_key=idempotency_key,
            )
            connection.commit()
        return event_id

    @staticmethod
    def enqueue_in_connection(
        connection: sqlite3.Connection,
        content: str,
        *,
        backend: str,
        model_name: str,
        idempotency_key: str | None = None,
    ) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("knowledge outbox content must be a non-empty string")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("knowledge outbox backend must be a non-empty string")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("knowledge outbox model name must be a non-empty string")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise ValueError("knowledge outbox idempotency key must be a non-empty string")

        key = idempotency_key or uuid.uuid4().hex
        event_id = hashlib.sha256(f"forge-chronicle-outbox:{key}".encode("utf-8")).hexdigest()[:32]
        memory_id = "forge-training-" + hashlib.sha256(event_id.encode("ascii")).hexdigest()[:32]
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "content": content,
            "pillar": "evolutionary",
            "domain": "training",
            "tags": ["forge", backend, f"event:{event_id}"],
            "source_repository": "forge",
            "source_agent": "forge",
            "memory_id": memory_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        existing = connection.execute(
            "SELECT payload_json FROM knowledge_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != encoded:
                raise ValueError("outbox idempotency key already refers to different lesson content")
            return event_id
        connection.execute(
            "INSERT INTO knowledge_outbox "
            "(event_id, memory_id, payload_json, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
            (event_id, memory_id, encoded, now, now),
        )
        return event_id

    def drain(self, chronicle: Any, *, limit: int = 25) -> Dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("outbox drain limit must be an integer from 1 through 500")
        if chronicle is None or not callable(getattr(chronicle, "store_memory", None)):
            return {"attempted": 0, "delivered": 0, "failed": 0, "error": "chronicle_unavailable"}

        claim_token = uuid.uuid4().hex
        attempted = delivered = failed = 0
        while attempted < limit:
            row = self._claim_next(claim_token)
            if row is None:
                break
            attempted += 1
            try:
                payload = json.loads(row["payload_json"])
                response = chronicle.store_memory(**payload)
                if (
                    not isinstance(response, dict)
                    or response.get("status") != "complete"
                    or response.get("memory_id") != row["memory_id"]
                ):
                    raise RuntimeError("Chronicle did not confirm the expected memory ID")
            except Exception as exc:
                self._record_failure(row["event_id"], claim_token, type(exc).__name__)
                failed += 1
                continue
            if self._record_delivery(row["event_id"], claim_token):
                delivered += 1
            else:
                failed += 1

        return {
            "attempted": attempted,
            "delivered": delivered,
            "failed": failed,
            "remaining": self.pending_count(),
        }

    def _claim_next(self, claim_token: str):
        claimed_at = datetime.now(timezone.utc)
        claim_until = (claimed_at + timedelta(minutes=5)).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_id, memory_id, payload_json FROM knowledge_outbox "
                "WHERE status = 'pending' AND "
                "(claim_token IS NULL OR claim_until IS NULL OR claim_until <= ?) "
                "ORDER BY created_at, event_id LIMIT 1",
                (claimed_at.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                "UPDATE knowledge_outbox SET claim_token = ?, claim_until = ?, "
                "updated_at = ? WHERE event_id = ? AND status = 'pending' AND "
                "(claim_token IS NULL OR claim_until IS NULL OR claim_until <= ?)",
                (claim_token, claim_until, claimed_at.isoformat(), row["event_id"],
                 claimed_at.isoformat()),
            )
            connection.commit()
            return row if updated.rowcount == 1 else None

    def _record_failure(self, event_id: str, claim_token: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE knowledge_outbox SET attempts = attempts + 1, updated_at = ?, "
                "last_error = ?, claim_token = NULL, claim_until = NULL "
                "WHERE event_id = ? AND status = 'pending' AND claim_token = ?",
                (now, error[:160], event_id, claim_token),
            )

    def _record_delivery(self, event_id: str, claim_token: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            updated = connection.execute(
                "UPDATE knowledge_outbox SET status = 'delivered', attempts = attempts + 1, "
                "updated_at = ?, delivered_at = ?, last_error = NULL, "
                "claim_token = NULL, claim_until = NULL "
                "WHERE event_id = ? AND status = 'pending' AND claim_token = ?",
                (now, now, event_id, claim_token),
            )
            return updated.rowcount == 1

    def pending_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM knowledge_outbox WHERE status = 'pending'"
            ).fetchone()[0])

    def stats(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM knowledge_outbox GROUP BY status"
                ).fetchall()
            }
            oldest = connection.execute(
                "SELECT created_at FROM knowledge_outbox WHERE status = 'pending' "
                "ORDER BY created_at, event_id LIMIT 1"
            ).fetchone()
            last_error = connection.execute(
                "SELECT last_error FROM knowledge_outbox WHERE status = 'pending' "
                "AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            active_claims = connection.execute(
                "SELECT COUNT(*) FROM knowledge_outbox WHERE status = 'pending' "
                "AND claim_token IS NOT NULL AND claim_until > ?",
                (now,),
            ).fetchone()[0]
            expired_claims = connection.execute(
                "SELECT COUNT(*) FROM knowledge_outbox WHERE status = 'pending' "
                "AND claim_token IS NOT NULL AND (claim_until IS NULL OR claim_until <= ?)",
                (now,),
            ).fetchone()[0]
        consistency = self.verify_state_consistency()
        return {
            "ready": integrity == "ok" and consistency.get("intact") is True,
            "database_integrity": integrity == "ok",
            "state_consistency": consistency,
            "pending": counts.get("pending", 0),
            "delivered": counts.get("delivered", 0),
            "active_delivery_claims": active_claims,
            "expired_delivery_claims": expired_claims,
            "oldest_pending_at": oldest["created_at"] if oldest else None,
            "last_delivery_error": last_error["last_error"] if last_error else None,
        }

    def verify_state_consistency(self) -> Dict[str, Any]:
        """Check durable payload bindings, state fields, and delivery leases."""
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT event_id, memory_id, payload_json, status, attempts, "
                    "created_at, updated_at, delivered_at, claim_token, claim_until "
                    "FROM knowledge_outbox ORDER BY event_id"
                ).fetchall()
            for row in rows:
                event_id = row["event_id"]
                try:
                    payload = json.loads(row["payload_json"])
                    datetime.fromisoformat(row["created_at"])
                    datetime.fromisoformat(row["updated_at"])
                    if row["delivered_at"] is not None:
                        datetime.fromisoformat(row["delivered_at"])
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                except Exception:
                    return {"intact": False, "issue": "outbox_record_invalid", "event_id": event_id}
                expected_memory_id = "forge-training-" + hashlib.sha256(
                    event_id.encode("ascii")
                ).hexdigest()[:32]
                lease_valid = (row["claim_token"] is None) == (row["claim_until"] is None)
                if row["claim_until"] is not None:
                    try:
                        lease_valid = lease_valid and datetime.fromisoformat(
                            row["claim_until"]
                        ).tzinfo is not None
                    except (TypeError, ValueError):
                        lease_valid = False
                if (
                    payload.get("memory_id") != row["memory_id"]
                    or row["memory_id"] != expected_memory_id
                    or row["status"] not in {"pending", "delivered"}
                    or not isinstance(row["attempts"], int)
                    or isinstance(row["attempts"], bool)
                    or row["attempts"] < 0
                    or not lease_valid
                    or (row["status"] == "pending" and row["delivered_at"] is not None)
                    or (row["status"] == "delivered" and row["delivered_at"] is None)
                    or (row["status"] == "delivered" and row["claim_token"] is not None)
                ):
                    return {
                        "intact": False,
                        "issue": "outbox_binding_or_lifecycle_mismatch",
                        "event_id": event_id,
                    }
            return {"intact": True, "events": len(rows)}
        except Exception as exc:
            return {"intact": False, "issue": type(exc).__name__}
