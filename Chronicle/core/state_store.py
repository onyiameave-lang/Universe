"""
Chronicle State Store
========================
Chronicle's existing VectorStore is deliberately append-only episodic
memory -- "this happened, at this time" -- and that's the right design
for research conclusions, trade journal entries, and the like. But
frequently-UPDATED state (champion confidence counters, term reliability
weights, a hypothesis's current status) is a genuinely different kind of
data: there's one CURRENT value per key, not a growing log of events.
Forcing state through an append-only store means either flooding it with
near-duplicate records on every update, or building fragile "find the
latest matching record" search logic. Neither is right.

This gives Chronicle a second, small, honest mechanism: upsert-by-key,
WITH full history retained alongside it. get()/data() stay fast (O(1),
just the current value) for every hot-path caller that only cares "what
is this right now" -- unchanged from before. But every set() now appends
the previous value to a history trail instead of destroying it, so Forge
and Atlas can ask "has this been declining for two weeks" during nightly
research, not just "what is it right now" (a real gap found by testing
this against the actual nightly-research goal -- pure overwrite meant
history was gone by the time anyone went looking for it).

Chronicle remains the single source of truth for BOTH kinds of data --
episodic (VectorStore) and state (this) -- without distorting either
one's natural shape.

Used by ChronicleBackedStore (shared/chronicle_sync.py), which is how
individual agents' trackers (ChampionConfidenceTracker, TermReliabilityTracker,
HypothesisQueue, etc.) get Chronicle as their real source of truth while
keeping a fast local cache. ChronicleBackedStore only ever reads/writes
the CURRENT value (via get()/set()) -- it doesn't need to know history
exists at all; the history trail is purely additive.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
                data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("state_store.json unreadable (%s); starting fresh", exc)
                return {}
            # Migrate records written before history tracking existed --
            # old shape was {"value":..., "owner":..., "updated_at":...}
            # with no "history" key. Adopt in place rather than losing
            # the one value they already have.
            for key, rec in data.items():
                if isinstance(rec, dict) and "history" not in rec:
                    rec["history"] = []
            return data
        return {}

    def _save(self) -> None:
        try:
            if self.path.exists():
                try:
                    disk_data = json.loads(self.path.read_text())
                    if isinstance(disk_data, dict):
                        for key, disk_rec in disk_data.items():
                            current = self._data.get(key)
                            if current is None:
                                self._data[key] = disk_rec
                                continue
                            disk_updated = disk_rec.get("updated_at", 0) if isinstance(disk_rec, dict) else 0
                            current_updated = current.get("updated_at", 0) if isinstance(current, dict) else 0
                            if disk_updated > current_updated:
                                self._data[key] = disk_rec
                except Exception:
                    log.warning("could not merge existing state_store.json before persist",
                                exc_info=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
            try:
                os.replace(str(tmp), str(self.path))
            except (OSError, PermissionError):
                shutil.move(str(tmp), str(self.path))
        except OSError as exc:
            log.warning("could not persist state_store.json: %s", exc)

    def set(self, key: str, value: Any, owner: str = "") -> Dict[str, Any]:
        """Upserts the CURRENT value under key (get()/data() still return
        just this, unchanged, for every fast hot-path caller) -- but first
        appends whatever was there before to this key's history trail, so
        nothing is destroyed. Every entry in history is real: (old value,
        when it stopped being current)."""
        with self._lock:
            existing = self._data.get(key)
            history: List[Dict[str, Any]] = existing.get("history", []) if existing else []
            if existing is not None:
                history.append({"value": existing["value"], "owner": existing.get("owner", ""),
                                "updated_at": existing.get("updated_at")})
            self._data[key] = {"value": value, "owner": owner, "updated_at": time.time(),
                               "history": history}
            self._save()
        return {"status": "complete", "key": key}

    def get(self, key: str) -> Optional[Any]:
        """Fast path -- just the current value, exactly as before. Every
        existing caller (ChronicleBackedStore included) keeps working
        unchanged; history is purely additive, never in this call's way."""
        with self._lock:
            rec = self._data.get(key)
            return rec["value"] if rec is not None else None

    def get_history(self, key: str, include_current: bool = True) -> List[Dict[str, Any]]:
        """The full trajectory for a key, oldest first -- what Forge/Atlas
        actually need for nightly research ("has this been declining"),
        as opposed to get()'s single current snapshot."""
        with self._lock:
            rec = self._data.get(key)
            if rec is None:
                return []
            trail = list(rec.get("history", []))
            if include_current:
                trail.append({"value": rec["value"], "owner": rec.get("owner", ""),
                              "updated_at": rec.get("updated_at")})
            return trail

    def delete(self, key: str) -> Dict[str, Any]:
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            self._save()
        return {"status": "complete", "deleted": existed}

    def list_keys(self, prefix: str = "") -> list:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]
