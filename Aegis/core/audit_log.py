"""
Aegis.core.audit_log
====================
Persistent, tamper-evident audit log. (Book VI Part II Ch VI Accountability;
Book II Part II Ch XI Failures shall never remain silent; Book III Part III
Principle IV Traceability.)

Hash-chained: each entry stores the hash of the previous entry, so any
tampering with history is detectable via verify_integrity(). This is a real
integrity mechanism, not decoration. Append-only JSONL on disk.

Constitutional fix (2026-07-20):
  Principle 6 — "Nothing Dies Without Leaving Knowledge": audit events are
  now ALSO mirrored to Chronicle so audit history is shared across the
  ecosystem and survives machine boundaries.
  Principle 2 — "Everything Communicates": Aegis audit events are now
  visible to all agents that query Chronicle (domain="audit").
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuditLog:
    GENESIS_HASH = "0" * 64

    def __init__(self, storage_dir: str = "security", chronicle_client=None):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "audit_log.jsonl"
        self._lock = threading.RLock()
        self._entries: List[Dict[str, Any]] = []
        self._last_hash = self.GENESIS_HASH
        # Chronicle client for cross-ecosystem mirroring (Principles 2 & 6)
        self._chronicle = chronicle_client
        self._load()

    def set_chronicle(self, chronicle_client) -> None:
        """Wire a Chronicle client after construction (called by AuditorAgent)."""
        self._chronicle = chronicle_client

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    self._entries.append(entry)
                    self._last_hash = entry.get("entry_hash", self._last_hash)
        except Exception:
            if self._path.exists():
                self._path.rename(self.storage_dir / "audit_log.corrupt.jsonl")
            self._entries = []
            self._last_hash = self.GENESIS_HASH

    def _hash_entry(self, entry: Dict[str, Any]) -> str:
        payload = json.dumps({k: v for k, v in entry.items() if k != "entry_hash"}, sort_keys=True)
        return hashlib.sha256((self._last_hash + payload).encode("utf-8")).hexdigest()

    def _mirror_to_chronicle(self, entry: Dict[str, Any]) -> None:
        """Mirror the audit entry to Chronicle (Principles 2 & 6).

        Only mirrors severity >= 'warning' to avoid flooding Chronicle with
        routine 'info' events.  Failures are silently swallowed so Chronicle
        unavailability never breaks the local audit chain.
        """
        if self._chronicle is None:
            return
        if entry.get("severity", "info") == "info":
            return  # skip routine info events to keep Chronicle signal-rich
        try:
            summary = (
                f"[AEGIS AUDIT] {entry['iso_time']} | "
                f"repo={entry['repository']} agent={entry['agent']} "
                f"action={entry['action']} severity={entry['severity']} | "
                f"{entry.get('detail', '')} | "
                f"violations={entry.get('violations', [])} | "
                f"audit_id={entry['audit_id']}"
            )
            self._chronicle.store(
                content=summary,
                memory_type="episodic",
                domain="audit",
                tags=["aegis", "audit", entry["severity"], entry["repository"]],
                source="aegis",
            )
        except Exception:
            pass  # aegis:allow-silent — Chronicle unavailability must not break audit

    def append(self, repository: str, agent: str, action: str, severity: str = "info",
               detail: str = "", context: Optional[Dict[str, Any]] = None,
               violations: Optional[List[str]] = None) -> Dict[str, Any]:
        with self._lock:
            entry = {"audit_id": f"audit-{uuid.uuid4().hex[:12]}", "timestamp": time.time(),
                    "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "repository": repository, "agent": agent, "action": action,
                    "severity": severity, "detail": detail, "context": context or {},
                    "violations": violations or [], "prev_hash": self._last_hash}
            entry["entry_hash"] = self._hash_entry(entry)
            self._last_hash = entry["entry_hash"]
            self._entries.append(entry)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            # Mirror to Chronicle for cross-ecosystem visibility (Principles 2 & 6)
            self._mirror_to_chronicle(entry)
            return entry

    def verify_integrity(self) -> Dict[str, Any]:
        with self._lock:
            prev = self.GENESIS_HASH
            for i, entry in enumerate(self._entries):
                payload = json.dumps({k: v for k, v in entry.items() if k != "entry_hash"}, sort_keys=True)
                expected = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
                if expected != entry.get("entry_hash"):
                    return {"intact": False, "broken_at": i, "audit_id": entry.get("audit_id")}
                prev = entry["entry_hash"]
            return {"intact": True, "entries": len(self._entries)}

    def query(self, repository: Optional[str] = None, severity: Optional[str] = None,
              agent: Optional[str] = None, since: Optional[float] = None,
              limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            e = self._entries
            if repository:
                e = [x for x in e if x["repository"] == repository]
            if severity:
                e = [x for x in e if x["severity"] == severity]
            if agent:
                e = [x for x in e if x["agent"] == agent]
            if since:
                e = [x for x in e if x["timestamp"] >= since]
            return e[-limit:]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_sev: Dict[str, int] = {}
            by_repo: Dict[str, int] = {}
            for e in self._entries:
                by_sev[e["severity"]] = by_sev.get(e["severity"], 0) + 1
                by_repo[e["repository"]] = by_repo.get(e["repository"], 0) + 1
            return {"total": len(self._entries), "by_severity": by_sev, "by_repository": by_repo,
                    "chronicle_mirroring": self._chronicle is not None}
