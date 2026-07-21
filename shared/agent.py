"""
shared.agent  (Universe-oracle deep-fix v5)
===========================================
The constitutional BaseAgent every agent in the ecosystem inherits.

Changes in this version:
  * think() and think_json() now pass essential=False to llm.complete() /
    llm.complete_json().  The LLM brain is an ADVISOR only — it is never
    trade-critical.  In essential_only mode these calls return None instantly
    with zero HTTP traffic.
  * No other logic changes — all existing behaviour preserved.
"""
from __future__ import annotations

import abc
import logging
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("ecosystem.agent")

# ---- optional layers, degrade honestly if unavailable ----
try:
    from shared.protocol import (AgentSpec, Message, MessageType, heartbeat,
                                  get_bus, get_registry)
    _HAS_PROTOCOL = True
except Exception:
    _HAS_PROTOCOL = False

try:
    from shared.llm import get_llm, system_prompt
    _HAS_LLM = True
except Exception:
    _HAS_LLM = False

try:
    from shared.learning import LearningLog
    _HAS_LEARNING = True
except Exception:
    _HAS_LEARNING = False

try:
    from shared.reasoning import ReasoningEngine, Strategy
    _HAS_REASONING = True
except Exception:
    _HAS_REASONING = False


class BaseAgent(abc.ABC):
    """Autonomous, evidence-reasoning, learning constitutional agent."""

    # ---- constitutional identity (override in subclasses) ----
    name: str = "base"
    repository: str = ""
    domain: str = ""
    description: str = ""
    version: str = "1.0.0"
    capabilities: List[str] = []
    channels: List[str] = []
    mission: Dict[str, Any] = {}
    memory_namespace: str = ""
    security_level: str = "standard"
    lifecycle_status: str = "active"
    heartbeat_interval_sec: float = 15.0
    exploration: float = 0.35

    def __init__(self, chronicle_client: Any = None, atlas_client: Any = None,
                 llm: Any = None, storage_dir: str = "memory", **kw):
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)

        self.chronicle = chronicle_client
        self.atlas     = atlas_client
        self.llm       = llm or (get_llm() if _HAS_LLM else None)

        self.learning = (
            LearningLog(self.name, llm=self.llm, chronicle=chronicle_client,
                        storage_dir=storage_dir)
            if _HAS_LEARNING else None
        )
        self.reasoning = (
            ReasoningEngine(self.name, storage_dir=storage_dir,
                            chronicle=chronicle_client, atlas=atlas_client,
                            llm=self.llm, exploration=self.exploration)
            if _HAS_REASONING else None
        )

        self._bus      = get_bus()      if _HAS_PROTOCOL else None
        self._registry = get_registry() if _HAS_PROTOCOL else None
        self._stop     = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._started    = False
        self._handled    = 0
        self._failed     = 0
        self._started_at = 0.0

        if self.reasoning is not None:
            try:
                self.register_strategies()
            except Exception as exc:
                # FIX-04: Upgraded from DEBUG to ERROR level (No Silent Failures)
                log.error("%s: Failed to register strategies: %s\n%s", 
                          self.name, exc, traceback.format_exc())

    # ============================================================
    # Lifecycle
    # ============================================================

    def start(self) -> None:
        self._started    = True
        self._started_at = time.time()
        self._stop.clear()
        if self._bus and self._registry:
            for ch in self.channels:
                self._bus.subscribe(ch, self._on_bus_message)
            self._registry.register(
                AgentSpec(
                    name=self.name, capabilities=list(self.capabilities),
                    repository=self.repository, description=self.description,
                    version=self.version, accepts_channels=list(self.channels),
                    mission=dict(self.mission or {}), domain=self.domain,
                    memory_namespace=self.memory_namespace,
                    security_level=self.security_level,
                ),
                handler=self.handle,
            )
            if self.heartbeat_interval_sec > 0:
                self._hb_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True,
                    name=f"hb-{self.name}",
                )
                self._hb_thread.start()
        self.on_start()
        log.info(
            "%s started (domain=%s, brain=%s, reasoning=%s, learning=%s)",
            self.name, self.domain, self.has_brain,
            self.reasoning is not None, self.learning is not None,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=2.0)
        if self._bus and self._registry:
            for ch in self.channels:
                self._bus.unsubscribe(ch)
            self._registry.unregister(self.name)
        self.on_stop()
        self._started = False
        log.info("%s stopped", self.name)

    def retire(self, reason: str = "") -> Dict[str, Any]:
        self.remember(
            content=f"Agent {self.name} retired: {reason}.",
            memory_type="constitutional", domain=self.domain,
            tags=["retirement", self.name],
        )
        self.lifecycle_status = "retired"
        summary = {"agent": self.name, "reason": reason, "stats": self.get_status()}
        self.stop()
        return summary

    def on_start(self) -> None: ...
    def on_stop(self)  -> None: ...

    # ============================================================
    # What subclasses implement
    # ============================================================

    def register_strategies(self) -> None:
        return None

    @abc.abstractmethod
    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]: ...

    # ============================================================
    # solve(): evidence-and-experience loop
    # ============================================================

    def solve(self, problem_type: str, context: Optional[Dict[str, Any]] = None,
              max_attempts: int = 3) -> Dict[str, Any]:
        context = dict(context or {})
        if self.reasoning is None:
            return {"status": "error", "message": "reasoning engine unavailable"}

        prior = self.recall(f"{problem_type} {context.get('query', '')}".strip())
        if prior:
            context["_recalled"] = prior

        trace:     List[Dict[str, Any]] = []
        tried_ids: List[str]            = []

        for attempt in range(1, max_attempts + 1):
            decision = self.reasoning.decide(
                problem_type, context, research=True, exclude_ids=tried_ids,
            )
            if decision.get("needs_new_strategy"):
                return {
                    "status": "error", "problem_type": problem_type,
                    "message": "no strategy available; agent must register approaches",
                    "trace": trace,
                }

            strategy: Strategy = decision["strategy_obj"]
            tried_ids.append(strategy.strategy_id)

            handler: Optional[Callable] = getattr(self, strategy.handler, None)
            if handler is None:
                self.reasoning.record_outcome(
                    strategy.strategy_id, False, detail="handler missing",
                )
                trace.append({"attempt": attempt, "strategy": strategy.name,
                               "outcome": "handler_missing"})
                continue

            try:
                result = handler(context)
                ok  = bool(result.get("status") in ("complete", "unimplemented")
                           and not result.get("failed"))
                err = "" if ok else result.get("message", result.get("reason", ""))
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}
                ok  = False
                err = str(exc)
                self._report_failure(f"{problem_type}:{strategy.name}", err, context)

            self.reasoning.record_outcome(
                strategy.strategy_id, ok, detail=err or "ok", context=context,
            )
            trace.append({
                "attempt": attempt, "strategy": strategy.name,
                "confidence": strategy.confidence, "ok": ok,
                "score_breakdown": decision.get("score_breakdown"),
            })

            if ok:
                result["_reasoning"] = {
                    "chosen": strategy.name, "confidence": strategy.confidence,
                    "attempt": attempt, "trace": trace,
                }
                self._handled += 1
                self._learn(problem_type, result.get("status", "complete"), True, context, "")
                return result

            # FIX-CA-32 (Phase 5j): Early exit on timeout/error
            # If a strategy returned a timeout error (e.g. "timed out after 130s"),
            # don't bother trying remaining strategies — they'll hit the same timeout
            # or the dispatch cache and return the same error. Exit immediately.
            if "timed out" in err.lower() or "timeout" in err.lower():
                log.warning(
                    "[nexus] Strategy '%s' timed out for %s=%r. Exiting early instead of "
                    "trying remaining strategies. (Book II Principle V Graceful Degradation)",
                    strategy.name, problem_type, context.get("query", "")[:80]
                )
                self._failed += 1
                self._learn(problem_type, "timeout", False, context, "strategy timed out")
                return {
                    "status": "error", "problem_type": problem_type,
                    "message": err or "Strategy timed out. Please try again.",
                    "trace": trace,
                }

            diag      = self.reasoning.diagnose_failure(strategy, context, error=err)
            variation = diag.get("variation") or {}
            trace[-1]["diagnosis"] = {
                "root_cause": diag.get("root_cause"),
                "variation":  variation.get("name"),
            }

        self._failed += 1
        self._learn(problem_type, "exhausted", False, context, "all attempts failed")
        return {
            "status": "error", "problem_type": problem_type,
            "message": f"no approach succeeded in {max_attempts} attempts",
            "trace": trace,
        }

    # ============================================================
    # act()
    # ============================================================

    def act(self, task: str,
            context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = dict(context or {})
        if self.learning:
            advice = self.learning.advice_for(task, context)
            if advice:
                context["_lessons"] = advice
        try:
            result  = self.execute(task, context)
            success = result.get("status") in ("complete", "unimplemented")
            error   = "" if success else result.get("message", "")
        except Exception as exc:
            result  = {"status": "error", "message": str(exc),
                       "trace": traceback.format_exc()[-1500:]}
            success = False
            error   = str(exc)
            self._report_failure(task, error, context)

        if "_reasoning" not in result:
            self._learn(
                task, result.get("status", "unknown"), success,
                {k: v for k, v in context.items() if not k.startswith("_")}, error,
            )
            if success:
                self._handled += 1
            elif result.get("status") == "error":
                self._failed += 1
        return result

    def _learn(self, task: str, outcome: str, success: bool,
               context: Dict[str, Any], error: str) -> None:
        if self.learning:
            self.learning.record(task=task, outcome=outcome, success=success,
                                 context=context, error=error)

    # ============================================================
    # UCP message handling
    # ============================================================

    def handle(self, msg: Any) -> Dict[str, Any]:
        if not isinstance(msg, dict):
            msg = getattr(msg, "__dict__", {}) or {}
        task    = msg.get("task", "")
        context = msg.get("context", {}) or {}
        context.setdefault("_sender", msg.get("sender", "unknown"))
        return self.act(task, context)

    def _on_bus_message(self, msg: Any) -> None:
        try:
            receiver = (getattr(msg, "receiver", None)
                        or (msg.get("receiver") if isinstance(msg, dict) else None))
            channel  = (getattr(msg, "channel", None)
                        or (msg.get("channel") if isinstance(msg, dict) else None))
            task     = (getattr(msg, "task", None)
                        or (msg.get("task") if isinstance(msg, dict) else None))
            if task == "heartbeat":
                return
            if receiver in (self.name, "*") or channel in self.channels:
                self.handle(msg)
        except Exception as exc:
            log.debug("%s bus handler error: %s", self.name, exc)

    def _report_failure(self, task: str, error: str,
                        context: Dict[str, Any]) -> None:
        if not self._bus:
            return
        try:
            self._bus.send(Message(
                sender=self.name, receiver="aegis", task="agent.error",
                type=MessageType.AUDIT, channel="ecosystem.audit",
                context={"agent": self.name, "task": task, "error": error[:500]},
            ))
        except Exception:
            log.debug("%s could not report failure to Aegis", self.name)

    # ============================================================
    # The brain — ADVISOR ONLY (essential=False on all calls)
    # ============================================================

    @property
    def has_brain(self) -> bool:
        return self.llm is not None and getattr(self.llm, "has_any", False)

    def think(self, prompt: str, temperature: float = 0.3,
              max_tokens: int = 800) -> Optional[str]:
        """
        Ask the LLM for ADVICE as this agent.  Returns None if no brain or
        if the call is skipped (essential_only mode).
        """
        if not self.has_brain:
            return None
        sys_p = system_prompt(self.name) if _HAS_LLM else ""
        r = self.llm.complete(
            sys_p, prompt,
            temperature=temperature, max_tokens=max_tokens,
            essential=False,   # ← advisory: skipped in essential_only mode
        )
        return r.text if r.ok else None

    def think_json(self, prompt: str, temperature: float = 0.2,
                   max_tokens: int = 800) -> Optional[Any]:
        """
        Ask the LLM for a JSON response.  Returns None if no brain or skipped.
        """
        if not self.has_brain:
            return None
        sys_p = system_prompt(self.name) if _HAS_LLM else ""
        parsed, _ = self.llm.complete_json(
            sys_p, prompt,
            temperature=temperature, max_tokens=max_tokens,
            essential=False,   # ← advisory: skipped in essential_only mode
        )
        return parsed

    # ============================================================
    # Memory access (Chronicle integration)
    # ============================================================
    # FIX-03: Explicit Chronicle send/receive hooks (Memory First principle)
    # FIX-04: Upgraded error logging from DEBUG to ERROR level (No Silent Failures)

    def _send_to_chronicle(self, content: Any, domain: str = "general",
                           tags: Optional[List[str]] = None,
                           memory_type: str = "semantic",
                           summary: str = "", **kw) -> bool:
        """Store an event/result in Chronicle (Memory First principle, Book 2).

        Signature matches all callers:
          research_agent.py:  _send_to_chronicle(content=..., memory_type=..., domain=..., tags=...)
          coordinator_agent.py: _send_to_chronicle(content=..., domain=..., tags=...)

        Calls chronicle.store_memory() — the real ChronicleAgent public API.
        FIX-CHR-01: was calling chronicle.record() which does not exist.
        FIX-CHR-02: was expecting (event_type, data) but callers pass content= kwargs.
        """
        if not self.chronicle:
            log.warning("%s: Chronicle unavailable, event not persisted (domain=%s)",
                        self.name, domain)
            return False
        try:
            self.chronicle.store_memory(
                content=content,
                pillar=memory_type,
                domain=domain,
                summary=summary or (str(content)[:160] if content else ""),
                source_repository=getattr(self, "repository", self.name),
                source_agent=self.name,
                tags=tags or [],
            )
            return True
        except Exception as exc:
            log.error("%s: Failed to send to Chronicle (domain=%s): %s\n%s",
                      self.name, domain, exc, traceback.format_exc())
            return False

    def _receive_from_chronicle(self, query: str, domain: Optional[str] = None,
                                limit: int = 5) -> Optional[List[Dict]]:
        """Retrieve memories from Chronicle (Memory First principle, Book 2).

        Signature matches all callers:
          coordinator_agent.py: _receive_from_chronicle(ctx["query"])   — positional string
          research_agent.py:    _receive_from_chronicle(query=..., domain=...)

        Calls chronicle.search() — the real ChronicleAgent public API.
        FIX-CHR-03: was calling chronicle.query() which does not exist.
        FIX-CHR-04: was expecting a dict but callers pass a plain string.
        """
        if not self.chronicle:
            log.warning("%s: Chronicle unavailable, cannot retrieve knowledge", self.name)
            return None
        try:
            return self.chronicle.search(
                query=query,
                domain=domain or getattr(self, "domain", "general"),
                limit=limit,
            )
        except Exception as exc:
            log.error("%s: Failed to retrieve from Chronicle (query=%r): %s\n%s",
                      self.name, query, exc, traceback.format_exc())
            return None

    def remember(self, content: Any, memory_type: str = "semantic",
                 domain: Optional[str] = None, tags: Optional[List[str]] = None,
                 evidence: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        if self.chronicle is None:
            log.warning("%s: Chronicle unavailable, cannot store memory", self.name)
            return None
        try:
            # FIX-P4-01: chronicle.store() resolves to ChronicleAgent.self.store
            # (a VectorStore instance attribute), not the store() method.
            # Use store_memory() — the unambiguous public API.
            # Constitutional law: Book II Principle I Memory First.
            result = self.chronicle.store_memory(
                content=content,
                pillar=memory_type,
                domain=domain or self.domain,
                summary=str(content)[:160] if content else "",
                source_repository=getattr(self, "repository", self.name),
                source_agent=self.name,
                tags=tags or [],
                evidence=evidence or [],
            )
            # FIX-04: Log success at INFO level for observability
            if result:
                log.debug("%s: Stored memory (type=%s, domain=%s)", 
                          self.name, memory_type, domain or self.domain)
            return result
        except Exception as exc:
            # FIX-04: Upgraded from DEBUG to ERROR level (No Silent Failures)
            log.error("%s: Failed to store memory: %s\n%s", 
                      self.name, exc, traceback.format_exc())
            return None

    def recall(self, query: str, domain: Optional[str] = None,
               limit: int = 5) -> List[Dict[str, Any]]:
        if self.chronicle is None:
            log.warning("%s: Chronicle unavailable, cannot recall memory", self.name)
            return []
        try:
            res = self.chronicle.search(
                query=query, domain=domain or self.domain,
                limit=limit, requester=self.name,
            )
            return res if isinstance(res, list) else []
        except Exception as exc:
            # FIX-04: Upgraded from silent to ERROR level
            log.error("%s: Failed to recall memory (query=%s): %s\n%s", 
                      self.name, query, exc, traceback.format_exc())
            return []

    def research(self, query: str,
                 domain: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if self.atlas is None:
            log.warning("%s: Atlas unavailable, cannot research", self.name)
            return None
        try:
            out = self.atlas.handle({
                "task": "research.investigate",
                "context": {"query": query, "domain": domain or self.domain},
                "sender": self.name,
            })
            return out.get("report") if isinstance(out, dict) else None
        except Exception as exc:
            # FIX-04: Upgraded from silent to ERROR level
            log.error("%s: Failed to research (query=%s): %s\n%s", 
                      self.name, query, exc, traceback.format_exc())
            return None

    # ============================================================
    # Self-evaluation
    # ============================================================

    def self_evaluate(self) -> Dict[str, Any]:
        learn  = self.learning.stats()  if self.learning  else {}
        reason = self.reasoning.stats() if self.reasoning else {}
        success_rate = learn.get("success_rate")
        unadapted    = learn.get("unadapted_failures", [])
        return {
            "agent":              self.name,
            "performing_mission": self._handled > 0,
            "improving":          success_rate is None or success_rate >= 0.6,
            "wasting_resources":  self._failed > self._handled if self._handled else False,
            "failure_to_learn":   len(unadapted) > 0,
            "should_request_help": len(unadapted) > 0,
            "best_strategies":    reason.get("best_strategies", []),
            "stats": {
                "handled": self._handled, "failed": self._failed,
                "success_rate": success_rate,
            },
        }

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_sec):
            try:
                self._bus.send(heartbeat(self.name, context=self.get_status()))
            except Exception:
                log.debug("%s heartbeat failed", self.name)

    # ============================================================
    # Status
    # ============================================================

    def get_status(self) -> Dict[str, Any]:
        s = {
            "name": self.name, "repository": self.repository, "domain": self.domain,
            "running": self._started, "handled": self._handled, "failed": self._failed,
            "uptime_sec": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "capabilities": self.capabilities, "security_level": self.security_level,
            "has_brain": self.has_brain,
        }
        if self.reasoning: s["reasoning"] = self.reasoning.stats()
        if self.learning:  s["learning"]  = self.learning.stats()
        if self.llm:       s["llm"]       = self.llm.stats()
        return s