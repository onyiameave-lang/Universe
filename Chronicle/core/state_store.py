"""
Chronicle State Store
========================
Chronicle's existing VectorStore is deliberately append-only episodic
memory -- "this happened, at this time" -- and that's the right design
for research conclusions, trade journal entries, and the like. But
frequently-UPDATED state (champion confidence counters, term reliability
weights, a hypothesis's current status) is a genuinely different kind of
data: there's one current value per key, not a growing log of events.
Forcing state through an append-only store means either flooding it with
near-duplicate records on every update, or building fragile "find the
latest matching record" search logic. Neither is right.

This gives Chronicle a second, small, honest mechanism: upsert-by-key.
Chronicle remains the single source of truth for BOTH kinds of data --
episodic (VectorStore) and state (this) -- without distorting either
one's natural shape.

Used by ChronicleBackedStore (shared/chronicle_sync.py), which is how
individual agents' trackers (ChampionConfidenceTracker, TermReliabilityTracker,
HypothesisQueue, etc.) get Chronicle as their real source of truth while
keeping a fast local cache.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("chronicle.state_store")


class StateStore:
    def __init__(self, storage_dir: str):
        self.path = Path(storage_dir) / "state_store.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("state_store.json unreadable (%s); starting fresh", exc)
        return {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError as exc:
            log.warning("could not persist state_store.json: %s", exc)

    def set(self, key: str, value: Any, owner: str = "") -> Dict[str, Any]:
        """Upserts value under key -- overwrites whatever was there before,
        by design (this is state, not a log). owner is just metadata
        (which agent/component this belongs to), for readability when
        inspecting the file by hand."""
        with self._lock:
            self._data[key] = {"value": value, "owner": owner, "updated_at": time.time()}
            self._save()
        return {"status": "complete", "key": key}

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            rec = self._data.get(key)
            return rec["value"] if rec is not None else None

    def delete(self, key: str) -> Dict[str, Any]:
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            self._save()
        return {"status": "complete", "deleted": existed}

    def list_keys(self, prefix: str = "") -> list:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]