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
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("shared.learning")


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
        self._reflection_slot = threading.BoundedSemaphore(1)
        self._chronicle_slot = threading.BoundedSemaphore(1)
        self._episodes: List[Episode]          = []
        self._lessons:  List[Dict[str, Any]]   = []
        self._failure_signatures: Dict[str, int] = {}
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
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("could not load learning log for %s from %s: %s",
                            self.agent, self._path, exc)

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps({
                "agent":              self.agent,
                "lessons":            self._lessons,
                "failure_signatures": self._failure_signatures,
            }, indent=2), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not persist learning log for %s to %s: %s",
                        self.agent, self._path, exc)

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
            if lesson:
                self._lessons.append(lesson)
            self._persist()

        if lesson:
            if self._chronicle_slot.acquire(blocking=False):
                try:
                    self._preserve_lesson(lesson)
                finally:
                    self._chronicle_slot.release()
            else:
                log.warning("%s lesson saved locally but Chronicle preservation skipped; "
                            "another lesson write is still running", self.agent)
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

    def _preserve_lesson(self, lesson: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(
                content=(
                    f"Lesson [{self.agent}]: {lesson.get('lesson')} "
                    f"(cause: {lesson.get('root_cause')}; "
                    f"fix: {lesson.get('adjustment')})"
                ),
                pillar="evolutionary", domain="learning",
                tags=["lesson", self.agent], source_repository=self.agent,
            )
        except Exception as exc:
            log.warning("could not preserve learning lesson for %s in Chronicle: %s",
                        self.agent, exc)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            successes = sum(1 for e in self._episodes if e.success)
            return {
                "agent":             self.agent,
                "episodes":          len(self._episodes),
                "successes":         successes,
                "failures":          len(self._episodes) - successes,
                "lessons_learned":   len(self._lessons),
                "unadapted_failures": self.unadapted_failures(),
                "success_rate":      (
                    round(successes / len(self._episodes), 3)
                    if self._episodes else None
                ),
            }
