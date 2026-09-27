"""
shared.learning  (Universe-oracle deep-fix v5)
==============================================
Learn-from-mistakes engine.

Changes in this version:
  * _reflect() calls llm.complete_json() with essential=False — reflection is
    advisory, never trade-critical.  In essential_only mode the LLM call is
    skipped instantly (zero HTTP) and the heuristic fallback is used instead.
  * No other logic changes — all existing behaviour preserved.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("shared.learning")
_SYNC_RETRY_BASE_SEC = 5.0
_SYNC_RETRY_MAX_SEC = 300.0


class Episode:
    """A single success/failure experience."""

    def __init__(self, task: str, outcome: str, success: bool,
                 context: Optional[Dict] = None, error: str = ""):
        self.episode_id = f"ep-{uuid.uuid4().hex[:10]}"
        self.task       = task
        self.outcome    = outcome
        self.success    = success
        self.context    = context or {}
        self.error      = error
        self.timestamp  = time.time()
        self.lesson: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id, "task": self.task,
            "outcome": self.outcome, "success": self.success,
            "error": self.error, "context": self.context,
            "timestamp": self.timestamp, "lesson": self.lesson,
        }


class LearningLog:
    """Per-agent experiential memory with real reflection and adaptation."""

    def __init__(self, agent_name: str, llm=None, chronicle=None,
                 storage_dir: str = "memory"):
        self.agent     = agent_name
        self.llm       = llm
        self.chronicle = chronicle
        self._lock     = threading.RLock()
        self._sync_condition = threading.Condition(self._lock)
        self._reflection_slot = threading.BoundedSemaphore(1)
        self._episodes: List[Episode]          = []
        self._lessons:  List[Dict[str, Any]]   = []
        self._failure_signatures: Dict[str, int] = {}
        self._pending_chronicle: List[Dict[str, Any]] = []
        self._sync_stop = threading.Event()
        self._sync_thread: Optional[threading.Thread] = None
        self._chronicle_unavailable_warned = False
        self._path = Path(storage_dir) / f"{agent_name}_learning.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._lessons             = data.get("lessons", [])
                self._failure_signatures  = data.get("failure_signatures", {})
                pending = data.get("pending_chronicle", [])
                if isinstance(pending, list):
                    self._pending_chronicle = [
                        item for item in pending
                        if isinstance(item, dict)
                        and isinstance(item.get("memory_id"), str)
                        and isinstance(item.get("lesson"), dict)
                    ]
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("could not load learning log for %s from %s: %s",
                            self.agent, self._path, exc)

    def _persist(self) -> bool:
        try:
            with self._lock:
                data = json.dumps({
                    "agent":              self.agent,
                    "lessons":            self._lessons,
                    "failure_signatures": self._failure_signatures,
                    "pending_chronicle":  self._pending_chronicle,
                }, indent=2)
                tmp_path = self._path.with_suffix(".tmp")
                with tmp_path.open("w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_path, self._path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not persist learning log for %s to %s: %s",
                        self.agent, self._path, exc)
            return False

    def start_sync_worker(self) -> None:
        """Start one durable outbox worker for this agent's lessons."""
        with self._sync_condition:
            if self.chronicle is None or not self._pending_chronicle or (
                    self._sync_thread and self._sync_thread.is_alive()):
                return
            self._sync_stop.clear()
            self._sync_thread = threading.Thread(
                target=self._sync_loop,
                name=f"learning-sync-{self.agent}",
                daemon=True,
            )
            self._sync_thread.start()
            self._sync_condition.notify_all()

    def set_chronicle(self, chronicle: Any) -> None:
        """Attach or replace Chronicle and resume any durable pending writes."""
        with self._sync_condition:
            self.chronicle = chronicle
            if chronicle is not None:
                self._chronicle_unavailable_warned = False
            self._sync_condition.notify_all()
        if chronicle is not None:
            self.start_sync_worker()

    def stop_sync_worker(self, timeout: float = 2.0) -> None:
        self._sync_stop.set()
        with self._sync_condition:
            self._sync_condition.notify_all()
        worker = self._sync_thread
        if worker is not None:
            worker.join(timeout=timeout)
            if worker.is_alive():
                log.warning(
                    "%s Chronicle sync worker is still blocked; pending lessons remain on disk",
                    self.agent,
                )

    def _sync_loop(self) -> None:
        while not self._sync_stop.is_set():
            with self._sync_condition:
                if self.chronicle is None:
                    self._sync_condition.wait()
                    continue
                now = time.time()
                due = next(
                    (item for item in self._pending_chronicle
                     if item.get("next_attempt_at", 0) <= now),
                    None,
                )
                if due is None:
                    wait_for = min(
                        (max(0.1, item.get("next_attempt_at", 0) - now)
                         for item in self._pending_chronicle),
                        default=None,
                    )
                    self._sync_condition.wait(timeout=wait_for)
                    continue
                entry = dict(due)

            self._sync_one(entry)

    def _sync_one(self, entry: Dict[str, Any]) -> None:
        if not self._persist():
            self._retry_sync(entry, "local outbox could not be persisted")
            return

        try:
            stored = self._preserve_lesson(
                entry["lesson"], memory_id=entry["memory_id"])
        except Exception as exc:
            stored = False
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = "Chronicle did not confirm storage"

        with self._sync_condition:
            current = next(
                (item for item in self._pending_chronicle
                 if item.get("memory_id") == entry["memory_id"]),
                None,
            )
            if current is None:
                return
            if stored:
                self._pending_chronicle.remove(current)
                if not self._persist():
                    current["next_attempt_at"] = time.time() + 5.0
                    current["last_error"] = "could not persist Chronicle acknowledgement"
                    self._pending_chronicle.append(current)
                    self._persist()
                    log.warning(
                        "%s Chronicle stored lesson %s but local acknowledgement failed; "
                        "the stable ID will make retry safe",
                        self.agent, entry["memory_id"],
                    )
                else:
                    log.info("%s synced lesson %s to Chronicle",
                             self.agent, entry["memory_id"])
                self._sync_condition.notify_all()
                return

        self._retry_sync(entry, error)

    def _retry_sync(self, entry: Dict[str, Any], error: str) -> None:
        with self._sync_condition:
            current = next(
                (item for item in self._pending_chronicle
                 if item.get("memory_id") == entry.get("memory_id")),
                None,
            )
            if current is None:
                return
            attempts = int(current.get("attempts", 0)) + 1
            delay = min(
                _SYNC_RETRY_BASE_SEC * (2 ** min(attempts - 1, 6)),
                _SYNC_RETRY_MAX_SEC,
            )
            current.update({
                "attempts": attempts,
                "last_error": error[:300],
                "next_attempt_at": time.time() + delay,
            })
            self._persist()
            self._sync_condition.notify_all()
        log.warning(
            "%s could not sync lesson %s to Chronicle (attempt %d; retry in %.0fs): %s",
            self.agent, entry.get("memory_id"), attempts, delay, error,
        )

    # ------------------------------------------------------------------
    def record(self, task: str, outcome: str, success: bool,
               context: Optional[Dict] = None, error: str = "") -> Episode:
        """Record locally under lock; keep LLM and Chronicle calls outside it."""
        ep = Episode(task, outcome, success, context, error)
        with self._lock:
            self._episodes.append(ep)
            if success:
                return ep
            sig = self._signature(task, error)
            repeat_count = self._failure_signatures.get(sig, 0) + 1
            self._failure_signatures[sig] = repeat_count

        if self._reflection_slot.acquire(blocking=False):
            try:
                lesson = self._reflect(ep, repeat_count=repeat_count)
            finally:
                self._reflection_slot.release()
        else:
            lesson = self._heuristic_reflection(ep, repeat_count)
            # The local heuristic remains available immediately. Skip only
            # optional LLM enrichment while another reflection is in flight.
            log.warning("%s learning reflection skipped LLM enrichment; another reflection is running",
                        self.agent)

        with self._lock:
            ep.lesson = lesson
            outbox_persisted = True
            if lesson:
                self._lessons.append(lesson)
                self._pending_chronicle.append({
                    "memory_id": f"learning-{self.agent}-{uuid.uuid4().hex}",
                    "lesson": lesson,
                    "created_at": time.time(),
                    "attempts": 0,
                    "next_attempt_at": 0.0,
                    "last_error": "",
                })
            outbox_persisted = self._persist()
            pending_count = len(self._pending_chronicle)

        if lesson and not outbox_persisted:
            log.error(
                "%s lesson/outbox could not be durably saved; Chronicle sync is not guaranteed",
                self.agent,
            )
        if lesson and self.chronicle is None:
            if not self._chronicle_unavailable_warned:
                log.warning(
                    "%s lesson queued locally for Chronicle; Chronicle is unavailable "
                    "(pending=%d)", self.agent, pending_count,
                )
                self._chronicle_unavailable_warned = True
        elif lesson:
            self.start_sync_worker()
            with self._sync_condition:
                self._sync_condition.notify_all()
        return ep

    def _signature(self, task: str, error: str) -> str:
        return f"{task}::{(error or '')[:80]}"

    def _reflect(self, ep: Episode, repeat_count: int) -> Optional[Dict[str, Any]]:
        """
        Derive a lesson from a failure.

        Uses the LLM brain if available, with essential=False so it is skipped
        instantly in essential_only mode.  Falls back to a structured heuristic.
        """
        repeated = repeat_count >= 3

        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm.prompts import system_prompt, prompt_reflect_on_failure  # type: ignore
                parsed, result = self.llm.complete_json(
                    system=system_prompt(self.agent),
                    prompt=prompt_reflect_on_failure(
                        self.agent, ep.task, ep.error or ep.outcome, ep.context
                    ),
                    temperature=0.2,
                    essential=False,   # ← advisory: skipped in essential_only mode
                )
                if parsed and isinstance(parsed, dict):
                    parsed["source"]       = "llm_reflection"
                    parsed["task"]         = ep.task
                    parsed["repeated"]     = repeated
                    parsed["repeat_count"] = repeat_count
                    return parsed
            except Exception as exc:
                log.warning("LLM reflection failed for %s; using heuristic lesson: %s",
                            self.agent, exc)

        return self._heuristic_reflection(ep, repeat_count)

    def _heuristic_reflection(self, ep: Episode, repeat_count: int) -> Dict[str, Any]:
        """Build a local lesson without external calls."""
        repeated = repeat_count >= 3
        return {
            "source":       "heuristic",
            "task":         ep.task,
            "root_cause":   ep.error or ep.outcome or "unknown",
            "lesson":       (
                f"Task '{ep.task}' failed. "
                "Validate inputs and preconditions before retry."
            ),
            "adjustment":   "Add precondition checks and retry with corrected inputs.",
            "confidence":   0.4,
            "repeated":     repeated,
            "repeat_count": repeat_count,
        }

    # ------------------------------------------------------------------
    def advice_for(self, task: str,
                   context: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Return relevant lessons an agent should heed BEFORE performing a task."""
        with self._lock:
            relevant = [
                l for l in self._lessons
                if l.get("task") == task or self._related(l.get("task", ""), task)
            ]
            relevant.sort(
                key=lambda l: (l.get("repeated", False), l.get("confidence", 0)),
                reverse=True,
            )
            return relevant[:5]

    def _related(self, lesson_task: str, task: str) -> bool:
        if not lesson_task or not task:
            return False
        a = set(lesson_task.lower().split("."))
        b = set(task.lower().split("."))
        return bool(a & b)

    def unadapted_failures(self) -> List[Dict[str, Any]]:
        """Failures that keep recurring: a constitutional 'failure to learn'."""
        with self._lock:
            return [
                {"signature": sig, "count": n}
                for sig, n in self._failure_signatures.items()
                if n >= 3
            ]

    def _preserve_lesson(self, lesson: Dict[str, Any], memory_id: str) -> bool:
        if self.chronicle is None:
            return False
        try:
            result = self.chronicle.store_memory(
                content=(
                    f"Lesson [{self.agent}]: {lesson.get('lesson')} "
                    f"(cause: {lesson.get('root_cause')}; "
                    f"fix: {lesson.get('adjustment')})"
                ),
                pillar="evolutionary", domain="learning",
                tags=["lesson", self.agent], source_repository=self.agent,
                memory_id=memory_id, autolink=False,
            )
            return isinstance(result, dict) and result.get("status") == "complete"
        except Exception as exc:
            log.warning("could not preserve learning lesson for %s in Chronicle: %s",
                        self.agent, exc)
            return False

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            successes = sum(1 for e in self._episodes if e.success)
            return {
                "agent":             self.agent,
                "episodes":          len(self._episodes),
                "successes":         successes,
                "failures":          len(self._episodes) - successes,
                "lessons_learned":   len(self._lessons),
                "pending_chronicle_syncs": len(self._pending_chronicle),
                "unadapted_failures": self.unadapted_failures(),
                "success_rate":      (
                    round(successes / len(self._episodes), 3)
                    if self._episodes else None
                ),
            }
