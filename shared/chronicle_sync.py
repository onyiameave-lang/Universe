"""
Chronicle-Backed Store
=========================
The actual "Chronicle is the source of truth, each agent keeps a fast
local cache" mechanism — matching the git-style pull/push model discussed:
an agent refreshes its cache from Chronicle when it can reach it; if
Chronicle is briefly unreachable, it falls back to whatever's in the
local cache rather than failing outright. Chronicle never loses anything;
an agent's local file is disposable and rebuildable, never a second
source of truth.

Used by any tracker that holds frequently-updated STATE (not an episodic
log) -- ChampionConfidenceTracker, TermReliabilityTracker, HypothesisQueue,
etc. Backed by Chronicle's state.set/state.get tasks (core/state_store.py),
not the episodic memory.store, since state has one current value per key,
not a growing log of events.

Design notes:
  - PULL happens once, at construction (agent startup) -- not on every
    single read, which would reintroduce the hot-path latency problem
    found earlier this session (the 60s-timeout symbol-skipping bug).
    Reads after construction use the in-memory dict, same as before.
  - PUSH happens on every save() call, best-effort -- if Chronicle is
    unreachable, the local cache still gets written (so nothing is lost
    locally), and the push is silently skipped rather than raising.
  - If BOTH Chronicle and the local cache are empty/unreachable (e.g. a
    genuinely fresh install), starts from an empty dict, same as today.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("shared.chronicle_sync")


class ChronicleBackedStore:
    def __init__(self, chronicle_client, store_key: str, local_path: Path, owner: str = ""):
        self.chronicle = chronicle_client
        self.store_key = store_key
        self.local_path = Path(local_path)
        self.owner = owner
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = self._pull_or_load()

    def data(self) -> Dict[str, Any]:
        """The current in-memory state -- callers read/mutate this directly,
        then call save(self.data()) (or save() with no args to persist the
        current dict as-is) when they're done."""
        return self._data

    def save(self, data: Optional[Dict[str, Any]] = None) -> None:
        if data is not None:
            self._data = data
        self._write_local_cache(self._data)
        self._push_to_chronicle(self._data)

    # -- internal --------------------------------------------------------

    def _pull_or_load(self) -> Dict[str, Any]:
        if self.chronicle is not None:
            try:
                pulled = self._chronicle_get()
                if pulled is not None:
                    self._write_local_cache(pulled)   # keep local cache in sync with what we just pulled
                    return pulled
            except Exception as exc:
                log.warning("[%s] could not pull from Chronicle, falling back to local cache: %s",
                           self.store_key, exc)
        return self._read_local_cache()

    def _chronicle_get(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self.chronicle, "act"):
            return None
        result = self.chronicle.act("state.get", {"key": self.store_key, "_sender": self.owner})
        if result and result.get("found"):
            return result.get("value")
        return None

    def _push_to_chronicle(self, data: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            if hasattr(self.chronicle, "act"):
                self.chronicle.act("state.set", {"key": self.store_key, "value": data, "_sender": self.owner})
        except Exception as exc:
            # Local cache is already correct at this point -- a failed push
            # means Chronicle is temporarily unreachable, not data loss.
            log.warning("[%s] could not push to Chronicle (local cache still saved): %s",
                       self.store_key, exc)

    def _write_local_cache(self, data: Dict[str, Any]) -> None:
        try:
            self.local_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except OSError as exc:
            log.warning("[%s] could not write local cache: %s", self.store_key, exc)

    def _read_local_cache(self) -> Dict[str, Any]:
        if self.local_path.exists():
            try:
                return json.loads(self.local_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("[%s] local cache unreadable (%s); starting fresh", self.store_key, exc)
        return {}