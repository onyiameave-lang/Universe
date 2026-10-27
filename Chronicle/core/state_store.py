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

Each key has a monotonic storage revision. Remote clients can pass that
revision to state.set as expected_revision so stale writers receive a conflict
instead of overwriting newer state. State mutations use a process-shared lock
and atomic file replacement; deleted keys retain a revision tombstone so an
offline cache cannot silently recreate them. Oracle's learned-strategy keys are
owner-bound to Oracle, and an existing key's owner cannot be changed by a write.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import copy
import hashlib
import errno
from contextlib import contextmanager
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
                raise RuntimeError(f"Chronicle state store is unreadable: {self.path}") from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"Chronicle state store has an invalid schema: {self.path}")
            # Migrate records written before history tracking existed --
            # old shape was {"value":..., "owner":..., "updated_at":...}
            # with no "history" key. Adopt in place rather than losing
            # the one value they already have.
            for key, rec in data.items():
                if not isinstance(key, str) or not key or not isinstance(rec, dict) or "value" not in rec:
                    raise RuntimeError(
                        f"Chronicle state store contains an invalid record for key {key!r}"
                    )
                if "history" not in rec:
                    rec["history"] = []
                rec.setdefault("revision", 0)
                if (not isinstance(rec["revision"], int) or isinstance(rec["revision"], bool)
                        or rec["revision"] < 0):
                    raise RuntimeError(
                        f"Chronicle state store contains an invalid revision for key {key!r}"
                    )
                if not isinstance(rec.get("deleted", False), bool):
                    raise RuntimeError(
                        f"Chronicle state store contains an invalid deletion marker for key {key!r}"
                    )
                if not isinstance(rec["history"], list) or any(
                    not isinstance(entry, dict) or "value" not in entry
                    for entry in rec["history"]
                ):
                    raise RuntimeError(
                        f"Chronicle state store contains invalid history for key {key!r}"
                    )
            return data
        return {}

    @contextmanager
    def _file_lock(self):
        """Serialize read/modify/write transactions across StateStore processes."""
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                            raise
                        time.sleep(0.05)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                handle.seek(0)
                unlock()

    def _save(self) -> bool:
        temporary_path = None
        try:
            if self.path.exists():
                disk_data = json.loads(self.path.read_text())
                if not isinstance(disk_data, dict):
                    raise ValueError("existing state_store.json has an invalid top-level shape")
                for key, disk_rec in disk_data.items():
                    if not isinstance(key, str) or not key or not isinstance(disk_rec, dict) or "value" not in disk_rec:
                        raise ValueError(f"existing state_store.json has an invalid record for key {key!r}")
                    disk_revision = disk_rec.get("revision", 0)
                    if (not isinstance(disk_revision, int) or isinstance(disk_revision, bool)
                            or disk_revision < 0 or not isinstance(disk_rec.get("deleted", False), bool)):
                        raise ValueError(f"existing state_store.json has invalid revision metadata for key {key!r}")
                    disk_history = disk_rec.get("history", [])
                    if not isinstance(disk_history, list) or any(
                        not isinstance(entry, dict) or "value" not in entry
                        for entry in disk_history
                    ):
                        raise ValueError(f"existing state_store.json has invalid history for key {key!r}")
                    current = self._data.get(key)
                    if current is None:
                        self._data[key] = disk_rec
                        continue
                    disk_updated = disk_rec.get("updated_at", 0) if isinstance(disk_rec, dict) else 0
                    current_updated = current.get("updated_at", 0) if isinstance(current, dict) else 0
                    disk_revision = disk_rec.get("revision", 0)
                    current_revision = current.get("revision", 0)
                    if (disk_revision > current_revision
                            or (disk_revision == current_revision
                                and disk_updated > current_updated)):
                        self._data[key] = disk_rec
            encoded = json.dumps(self._data, indent=2, sort_keys=True, allow_nan=False)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            log.warning("could not persist state_store.json: %s", exc)
            return False

    def set(
        self, key: str, value: Any, owner: str = "",
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Upserts the CURRENT value under key (get()/data() still return
        just this, unchanged, for every fast hot-path caller) -- but first
        appends whatever was there before to this key's history trail, so
        nothing is destroyed. Every entry in history is real: (old value,
        when it stopped being current). If expected_revision is supplied, a
        stale writer receives a conflict without changing the key."""
        with self._lock, self._file_lock():
            # Refresh under the process-shared lock so expected_revision is
            # compared with the authoritative disk revision, not stale cache.
            self._data = self._load()
            previous_data = copy.deepcopy(self._data)
            existing = self._data.get(key)
            current_revision = existing.get("revision", 0) if existing else 0
            required_owner = _OWNER_BOUND_KEYS.get(key)
            if required_owner is not None and owner != required_owner:
                return {"status": "forbidden", "key": key,
                        "message": "state key is owned by another agent"}
            if existing and existing.get("owner") and existing["owner"] != owner:
                return {"status": "forbidden", "key": key,
                        "message": "state key owner cannot be changed"}
            if expected_revision is not None:
                if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                        or expected_revision < 0):
                    return {"status": "error", "key": key,
                            "message": "expected_revision must be a non-negative integer"}
                if expected_revision != current_revision:
                    return {"status": "conflict", "key": key,
                            "revision": current_revision,
                            "message": "state revision changed before this write"}
            history: List[Dict[str, Any]] = existing.get("history", []) if existing else []
            if existing is not None:
                history.append({"value": existing["value"], "owner": existing.get("owner", ""),
                                "updated_at": existing.get("updated_at")})
            next_revision = current_revision + 1
            self._data[key] = {
                "value": value,
                "owner": owner,
                "updated_at": time.time(),
                "history": history,
                "revision": next_revision,
            }
            try:
                persisted = self._save()
            except Exception:
                persisted = False
            if not persisted:
                self._data = previous_data
                return {"status": "error", "key": key, "message": "state was not durably saved"}
            stored = self._data.get(key, {})
            if (
                stored.get("revision") != next_revision
                or stored.get("value") != value
                or stored.get("owner", "") != owner
            ):
                self._data = previous_data
                return {
                    "status": "conflict", "key": key,
                    "revision": stored.get("revision", 0),
                    "message": "state changed concurrently before this write committed",
                }
        return {"status": "complete", "key": key, "revision": next_revision}

    def compare_and_set_version(
        self, key: str, expected_version: int, new_value: Any, owner: str = ""
    ) -> bool:
        """Atomically replace one key if its composite state revision matches.

        Chronicle composite values use ``revision`` as a transaction revision,
        incremented for every state change. For older composite values without
        that field, ``version_number`` is used as a compatibility fallback.
        A missing key has revision zero. Both the instance lock and a
        process-shared file lock serialize this read/check/write operation.
        """
        with self._lock, self._file_lock():
            self._data = self._load()
            previous_data = copy.deepcopy(self._data)
            existing = self._data.get(key)
            required_owner = _OWNER_BOUND_KEYS.get(key)
            if required_owner is not None and owner != required_owner:
                return False
            if existing and existing.get("owner") and existing["owner"] != owner:
                return False
            current_value = existing.get("value") if existing else None
            if isinstance(current_value, dict):
                current_version = current_value.get(
                    "revision", current_value.get("version_number", 0)
                )
            else:
                current_version = 0
            if current_version != expected_version:
                return False

            history = list(existing.get("history", [])) if existing else []
            if existing is not None:
                history.append({
                    "value": existing["value"],
                    "owner": existing.get("owner", ""),
                    "updated_at": existing.get("updated_at"),
                })
            self._data[key] = {
                "value": new_value,
                "owner": owner,
                "updated_at": time.time(),
                "history": history,
                "revision": (existing.get("revision", 0) if existing else 0) + 1,
            }
            try:
                persisted = self._save()
            except Exception:
                persisted = False
            if not persisted:
                self._data = previous_data
                return False
            return True

    def get(self, key: str) -> Optional[Any]:
        """Fast path -- just the current value, exactly as before. Every
        existing caller (ChronicleBackedStore included) keeps working
        unchanged; history is purely additive, never in this call's way."""
        with self._lock:
            rec = self._data.get(key)
            return rec["value"] if rec is not None and not rec.get("deleted", False) else None

    def get_with_revision(self, key: str) -> Dict[str, Any]:
        """Return a consistent current value and revision for optimistic writes."""
        with self._lock:
            rec = self._data.get(key)
            if rec is None or rec.get("deleted", False):
                return {
                    "value": None,
                    "found": False,
                    "deleted": bool(rec and rec.get("deleted", False)),
                    "revision": rec.get("revision", 0) if rec else 0,
                }
            return {
                "value": rec["value"],
                "found": rec["value"] is not None,
                "deleted": False,
                "revision": rec.get("revision", 0),
            }

    def get_revision(self, key: str) -> int:
        """Return the monotonic revision for a key; absent keys have revision zero."""
        with self._lock:
            rec = self._data.get(key)
            return rec.get("revision", 0) if rec is not None else 0

    def get_history(self, key: str, include_current: bool = True) -> List[Dict[str, Any]]:
        """The full trajectory for a key, oldest first -- what Forge/Atlas
        actually need for nightly research ("has this been declining"),
        as opposed to get()'s single current snapshot."""
        with self._lock:
            rec = self._data.get(key)
            if rec is None:
                return []
            trail = list(rec.get("history", []))
            if include_current and not rec.get("deleted", False):
                trail.append({"value": rec["value"], "owner": rec.get("owner", ""),
                              "updated_at": rec.get("updated_at")})
            return trail

    def delete(
        self, key: str, expected_revision: Optional[int] = None, owner: str = ""
    ) -> Dict[str, Any]:
        with self._lock, self._file_lock():
            self._data = self._load()
            previous_data = copy.deepcopy(self._data)
            existing = self._data.get(key)
            current_revision = existing.get("revision", 0) if existing else 0
            required_owner = _OWNER_BOUND_KEYS.get(key)
            if required_owner is not None and owner != required_owner:
                return {"status": "forbidden", "deleted": False,
                        "message": "state key is owned by another agent"}
            if existing and existing.get("owner") and existing["owner"] != owner:
                return {"status": "forbidden", "deleted": False,
                        "message": "state key owner cannot be changed"}
            if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                    or expected_revision < 0):
                return {"status": "error", "deleted": False,
                        "message": "expected_revision must be a non-negative integer"}
            if expected_revision != current_revision:
                return {"status": "conflict", "deleted": False,
                        "revision": current_revision,
                        "message": "state revision changed before deletion"}
            existed = existing is not None and not existing.get("deleted", False)
            if not existed:
                return {
                    "status": "complete",
                    "deleted": False,
                    "revision": existing.get("revision", 0) if existing else 0,
                }
            history = list(existing.get("history", []))
            history.append({
                "value": existing["value"],
                "owner": existing.get("owner", ""),
                "updated_at": existing.get("updated_at"),
            })
            next_revision = existing.get("revision", 0) + 1
            self._data[key] = {
                "value": existing["value"],
                "owner": existing.get("owner", ""),
                "updated_at": time.time(),
                "history": history,
                "revision": next_revision,
                "deleted": True,
            }
            try:
                persisted = self._save()
            except Exception:
                persisted = False
            if not persisted:
                self._data = previous_data
                return {"status": "error", "deleted": False,
                        "message": "state deletion was not durably saved"}
        return {"status": "complete", "deleted": existed, "revision": next_revision}

    def export_snapshot(self, prefix: str) -> Dict[str, Any]:
        """Export a namespace with revisions and history for external durability.

        Callers should choose a narrow owner namespace. The Oracle Forge
        Actions workflow uses only ``forge.oracle.`` so it never exports
        unrelated agents' Chronicle state.
        """
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("a non-empty Chronicle snapshot prefix is required")
        with self._lock, self._file_lock():
            self._data = self._load()
            records = {
                key: copy.deepcopy(record)
                for key, record in self._data.items()
                if key.startswith(prefix)
            }
        records_json = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
        snapshot = {
            "schema": "chronicle.state_snapshot.v1",
            "prefix": prefix,
            "records": records,
            "record_count": len(records),
            "records_sha256": hashlib.sha256(records_json.encode("utf-8")).hexdigest(),
        }
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 100 * 1024 * 1024:
            raise ValueError("Chronicle state snapshot exceeds the 100 MiB limit")
        return snapshot

    def restore_snapshot(self, snapshot: Dict[str, Any], prefix: str) -> Dict[str, Any]:
        """Merge a validated namespace snapshot without rolling back newer state."""
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
                or snapshot.get("records_sha256") != hashlib.sha256(
                    encoded.encode("utf-8")).hexdigest()):
            return {"status": "error", "message": "Chronicle state snapshot count or fingerprint is invalid"}
        if len(encoded.encode("utf-8")) > 100 * 1024 * 1024:
            return {"status": "error", "message": "Chronicle state snapshot exceeds the 100 MiB limit"}

        for key, record in records.items():
            if not isinstance(key, str) or not key.startswith(prefix) or not key:
                return {"status": "error", "message": "Chronicle state snapshot contains a key outside its namespace"}
            if (not isinstance(record, dict) or "value" not in record
                    or not isinstance(record.get("owner", ""), str)
                    or not isinstance(record.get("revision"), int)
                    or isinstance(record.get("revision"), bool) or record["revision"] < 0
                    or not isinstance(record.get("history", []), list)
                    or not isinstance(record.get("deleted", False), bool)):
                return {"status": "error", "message": f"Chronicle snapshot record {key!r} is malformed"}
            if any(not isinstance(item, dict) or "value" not in item
                   for item in record.get("history", [])):
                return {"status": "error", "message": f"Chronicle snapshot history for {key!r} is malformed"}
            required_owner = _OWNER_BOUND_KEYS.get(key)
            if required_owner is not None and record.get("owner", "") != required_owner:
                return {"status": "error", "message": f"Chronicle snapshot owner for {key!r} is invalid"}

        with self._lock, self._file_lock():
            self._data = self._load()
            previous_data = copy.deepcopy(self._data)
            restored = 0
            skipped_newer = 0
            for key, record in records.items():
                existing = self._data.get(key)
                if existing is not None:
                    if (existing.get("owner") and record.get("owner")
                            and existing["owner"] != record["owner"]):
                        self._data = previous_data
                        return {"status": "error", "message": f"Chronicle snapshot owner conflicts for {key!r}"}
                    current_revision = int(existing.get("revision", 0))
                    incoming_revision = record["revision"]
                    if current_revision > incoming_revision:
                        skipped_newer += 1
                        continue
                    if current_revision == incoming_revision:
                        if existing != record:
                            self._data = previous_data
                            return {"status": "conflict", "message": f"Chronicle snapshot revision conflicts for {key!r}"}
                        continue
                self._data[key] = copy.deepcopy(record)
                restored += 1
            try:
                persisted = self._save()
            except Exception:
                persisted = False
            if not persisted:
                self._data = previous_data
                return {"status": "error", "message": "Chronicle state snapshot could not be durably restored"}
        return {"status": "complete", "restored": restored,
                "skipped_newer": skipped_newer, "record_count": len(records)}

    def list_keys(self, prefix: str = "") -> list:
        with self._lock:
            return [
                key for key, record in self._data.items()
                if key.startswith(prefix) and not record.get("deleted", False)
            ]
