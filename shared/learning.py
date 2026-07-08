"""
shared.learning
===============
The learn-from-mistakes engine. (Book I Part IV Article IX: Trial and Error;
Article XII Self-Evaluation; Book II Part III Ch VIII Memory Evolution;
Book VI Part II Ch X Repairing Trust.)

Every agent inherits a LearningLog. When an action fails or underperforms,
the agent records the episode; the engine (optionally using the LLM brain)
reflects on the root cause and derives a lesson and a concrete adjustment.
Lessons are preserved to Chronicle so the WHOLE civilization learns, not just
one agent. Repeated identical failures without adaptation are flagged, exactly
as the Constitution demands ("Repeated mistakes without adaptation constitute
a failure to learn").

This is real: lessons change future behavior via `advice_for()`, which agents
consult before acting so they avoid known pitfalls.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class Episode:
    """A single success/failure experience."""
    def __init__(self, task: str, outcome: str, success: bool,
                 context: Optional[Dict] = None, error: str = ""):
        self.episode_id = f"ep-{uuid.uuid4().hex[:10]}"
        self.task = task
        self.outcome = outcome
        self.success = success
        self.context = context or {}
        self.error = error
        self.timestamp = time.time()
        self.lesson: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"episode_id": self.episode_id, "task": self.task, "outcome": self.outcome,
                "success": self.success, "error": self.error, "context": self.context,
                "timestamp": self.timestamp, "lesson": self.lesson}


class LearningLog:
    """
    Per-agent experiential memory with real reflection and adaptation.
    """

    def __init__(self, agent_name: str, llm=None, chronicle=None,
                 storage_dir: str = "memory"):
        self.agent = agent_name
        self.llm = llm
        self.chronicle = chronicle
        self._lock = threading.RLock()
        self._episodes: List[Episode] = []
        self._lessons: List[Dict[str, Any]] = []
        self._failure_signatures: Dict[str, int] = {}
        self._path = Path(storage_dir) / f"{agent_name}_learning.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._lessons = data.get("lessons", [])
                self._failure_signatures = data.get("failure_signatures", {})
            except Exception:
                pass

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps({
                "agent": self.agent, "lessons": self._lessons,
                "failure_signatures": self._failure_signatures}, indent=2), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent (best-effort persistence)

    # ---- recording experience ----

    def record(self, task: str, outcome: str, success: bool,
               context: Optional[Dict] = None, error: str = "") -> Episode:
        """Record an episode. On failure, reflect and derive a lesson."""
        ep = Episode(task, outcome, success, context, error)
        with self._lock:
            self._episodes.append(ep)
            if not success:
                sig = self._signature(task, error)
                self._failure_signatures[sig] = self._failure_signatures.get(sig, 0) + 1
                ep.lesson = self._reflect(ep, repeat_count=self._failure_signatures[sig])
                if ep.lesson:
                    self._lessons.append(ep.lesson)
                    self._preserve_lesson(ep.lesson)
                self._persist()
        return ep

    def _signature(self, task: str, error: str) -> str:
        return f"{task}::{(error or '')[:80]}"

    def _reflect(self, ep: Episode, repeat_count: int) -> Optional[Dict[str, Any]]:
        """
        Derive a lesson from a failure. Uses the LLM brain if available for a
        real root-cause analysis; otherwise falls back to a structured heuristic.
        """
        repeated = repeat_count >= 3
        # Try the LLM for genuine reflection.
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm.prompts import system_prompt, prompt_reflect_on_failure
                parsed, result = self.llm.complete_json(
                    system=system_prompt(self.agent),
                    prompt=prompt_reflect_on_failure(self.agent, ep.task, ep.error or ep.outcome, ep.context),
                    temperature=0.2)
                if parsed and isinstance(parsed, dict):
                    parsed["source"] = "llm_reflection"
                    parsed["task"] = ep.task
                    parsed["repeated"] = repeated
                    parsed["repeat_count"] = repeat_count
                    return parsed
            except Exception:
                pass
        # Heuristic fallback (still real, still useful).
        return {
            "source": "heuristic",
            "task": ep.task,
            "root_cause": ep.error or ep.outcome or "unknown",
            "lesson": f"Task '{ep.task}' failed. Validate inputs and preconditions before retry.",
            "adjustment": "Add precondition checks and retry with corrected inputs.",
            "confidence": 0.4,
            "repeated": repeated,
            "repeat_count": repeat_count,
        }

    # ---- using what was learned ----

    def advice_for(self, task: str, context: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Return relevant lessons an agent should heed BEFORE performing a task.
        This is how learning changes behavior, not just storage.
        """
        with self._lock:
            relevant = [l for l in self._lessons
                       if l.get("task") == task or self._related(l.get("task", ""), task)]
            # prioritize repeated failures and higher-confidence lessons
            relevant.sort(key=lambda l: (l.get("repeated", False), l.get("confidence", 0)),
                         reverse=True)
            return relevant[:5]

    def _related(self, lesson_task: str, task: str) -> bool:
        if not lesson_task or not task:
            return False
        a = set(lesson_task.lower().split("."))
        b = set(task.lower().split("."))
        return len(a & b) > 0

    def unadapted_failures(self) -> List[Dict[str, Any]]:
        """Failures that keep recurring: a constitutional 'failure to learn'."""
        with self._lock:
            return [{"signature": sig, "count": n}
                   for sig, n in self._failure_signatures.items() if n >= 3]

    def _preserve_lesson(self, lesson: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            self.chronicle.store(
                content=f"Lesson [{self.agent}]: {lesson.get('lesson')} "
                       f"(cause: {lesson.get('root_cause')}; fix: {lesson.get('adjustment')})",
                memory_type="evolutionary", domain="learning",
                tags=["lesson", self.agent], source=self.agent)
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            successes = sum(1 for e in self._episodes if e.success)
            return {
                "agent": self.agent,
                "episodes": len(self._episodes),
                "successes": successes,
                "failures": len(self._episodes) - successes,
                "lessons_learned": len(self._lessons),
                "unadapted_failures": self.unadapted_failures(),
                "success_rate": round(successes / len(self._episodes), 3) if self._episodes else None,
            }
