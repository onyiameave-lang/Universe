"""
Aegis.core.audit_log
====================
Persistent, tamper-evident audit log. (Book VI Part II Ch VI Accountability;
Book II Part II Ch XI Failures shall never remain silent; Book III Part III
Principle IV Traceability.)

Hash-chained: each entry stores the hash of the previous entry, so any
tampering with history is detectable via verify_integrity(). This is a real
integrity mechanism, not decoration. Append-only JSONL on disk.
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

    def __init__(self, storage_dir: str = "security"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "audit_log.jsonl"
        self._lock = threading.RLock()
        self._entries: List[Dict[str, Any]] = []
        self._last_hash = self.GENESIS_HASH
        self._load()

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
            return {"total": len(self._entries), "by_severity": by_sev, "by_repository": by_repo}
