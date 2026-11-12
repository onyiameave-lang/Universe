"""
Chronicle.core.dedup_ledger
===========================
Native Python 3.10 Non-Duplicating Memory & Lesson Ledger for Chronicle.

Enforces the repository law:
  - When a lesson, mistake, or market event recurs, Chronicle NEVER creates a
    duplicate row in memory.
  - Instead, it matches the deterministic `concept_key`, increments
    `occurrence_count`, and appends `"Happened again on YYYY-MM-DD HH:MM UTC: ..."`
    to the existing record's `occurrences` history.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def format_utc_stamp(unix_sec: Optional[float] = None) -> str:
    ts = unix_sec if unix_sec is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def normalize_concept_signature(domain: str, text: str, explicit_key: Optional[str] = None) -> str:
    if explicit_key and explicit_key.strip():
        return re.sub(r"\s+", "_", explicit_key.strip().lower())
    cleaned = re.sub(r"\b20\d{2}-\d{2}-\d{2}[t\s]\d{2}:\d{2}(:\d{2}(\.\d+)?z?)?\b", "", text.lower())
    cleaned = re.sub(r"\b(exp|cand|hyp|ep|mem)-[a-z0-9_-]+\b", "", cleaned)
    cleaned = re.sub(r"[-+]?\d+\.\d+", "#", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    digest = hashlib.sha256(f"{domain.lower()}::{cleaned}".encode("utf-8")).hexdigest()[:20]
    return f"concept::{domain.lower()}::{digest}"


class ChronicleDeduplicatingLedger:
    """Consolidates recurring lessons and episodes in Chronicle without duplication."""

    def __init__(self, workspace_root: Optional[Path] = None) -> None:
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self.store_path = (
            self.workspace_root / "Chronicle" / "memory" / "store" / "memories.json"
        )

    def _atomic_write(self, memories: List[Dict[str, Any]]) -> None:
        import os

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(memories, indent=2)
        tmp_path = self.store_path.with_name(f"{self.store_path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
        try:
            tmp_path.write_text(serialized, encoding="utf-8")
            os.replace(tmp_path, self.store_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            self.store_path.write_text(serialized, encoding="utf-8")

    def load_store(self) -> List[Dict[str, Any]]:
        if not self.store_path.exists():
            return []
        try:
            raw = self.store_path.read_text(encoding="utf-8").lstrip()
            if not raw:
                return []
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data, _ = json.JSONDecoder().raw_decode(raw)
                if isinstance(data, list):
                    self._atomic_write(data)
            return data if isinstance(data, list) else data.get("memories", [])
        except Exception:
            return []

    def record_lesson(
        self,
        domain: str,
        content: str,
        concept_key: Optional[str] = None,
        pillar: str = "procedural",
        tags: Optional[List[str]] = None,
        source_repository: str = "forge",
    ) -> Dict[str, Any]:
        memories: List[Dict[str, Any]] = self.load_store()
        now_unix = time.time()
        stamp = format_utc_stamp(now_unix)
        sig = normalize_concept_signature(domain, content, concept_key)

        for mem in memories:
            existing_sig = mem.get("concept_key") or normalize_concept_signature(
                mem.get("domain", ""), mem.get("content", "")
            )
            if existing_sig == sig:
                mem["concept_key"] = sig
                mem["occurrence_count"] = int(mem.get("occurrence_count", 1)) + 1
                mem["last_occurred_unix"] = now_unix
                mem["updated_at_unix"] = now_unix
                mem["confidence"] = round(min(0.99, float(mem.get("confidence", 0.85)) + 0.02), 4)
                occurrences = mem.setdefault("occurrences", [])
                occurrences.append({
                    "date_iso": datetime.fromtimestamp(now_unix, tz=timezone.utc).isoformat(),
                    "timestamp_unix": now_unix,
                    "note": f"Happened again on {stamp}: {content}",
                    "source_repository": source_repository,
                })
                self._atomic_write(memories)
                return {"deduplicated": True, "record": mem}

        new_record = {
            "memory_id": f"mem-{hashlib.sha256(f'{sig}:{now_unix}'.encode()).hexdigest()[:12]}",
            "concept_key": sig,
            "content": content,
            "pillar": pillar,
            "domain": domain,
            "tags": tags or [],
            "confidence": 0.88,
            "source_repository": source_repository,
            "occurrence_count": 1,
            "occurrences": [
                {
                    "date_iso": datetime.fromtimestamp(now_unix, tz=timezone.utc).isoformat(),
                    "timestamp_unix": now_unix,
                    "note": f"First recorded on {stamp}: {content}",
                    "source_repository": source_repository,
                }
            ],
            "created_at_unix": now_unix,
            "last_occurred_unix": now_unix,
            "updated_at_unix": now_unix,
        }
        memories.append(new_record)
        self._atomic_write(memories)
        return {"deduplicated": False, "record": new_record}
