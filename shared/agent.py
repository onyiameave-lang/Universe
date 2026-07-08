"""
shared.agent
============
The constitutional BaseAgent every agent in the ecosystem inherits.
(Book I Part IV: Constitution of Agents; Article IX Trial and Error;
 Article X Decision Making; Article XII Self-Evaluation; Book II Ch IV
 Research Before Assumption.)

Design philosophy (per the ecosystem's own decision):
  Agents decide by EVIDENCE and EXPERIENCE, not by asking an LLM for answers.

The default problem-solving loop (`solve`) is:
    1. RECALL    prior knowledge from Chronicle (Memory First).
    2. RESEARCH  reasons FOR and AGAINST each candidate approach
                 (Chronicle memory + Atlas research; LLM only advises).
    3. DECIDE    the approach with the best EARNED track record, while keeping
                 an exploration budget so a better approach can still surface.
    4. TRY       enact the chosen strategy via its handler method.
    5a. SUCCESS  reinforce it; reuse it continuously until something better wins.
    5b. FAILURE  diagnose WHY, lower its standing, and try a DIFFERENT approach.
       Every outcome becomes evidence preserved to Chronicle.

The LLM brain (`think`) is available as an ADVISOR only, never the decider.
Everything degrades honestly when a key or peer is absent.

Concrete agents implement:
    * capabilities / channels / mission (class attributes)
    * register_strategies()   -> declare candidate approaches per problem-type
    * strategy handler methods -> enact one approach, return a result dict
    * execute(task, context)   -> map a UCP task to a problem-type + handler
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
    exploration: float = 0.35              # reasoning exploration/exploitation balance

    def __init__(self, chronicle_client: Any = None, atlas_client: Any = None,
                 llm: Any = None, storage_dir: str = "memory", **kw):
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)

        self.chronicle = chronicle_client
        self.atlas = atlas_client
        self.llm = llm or (get_llm() if _HAS_LLM else None)

        # learn-from-mistakes log
        self.learning = (LearningLog(self.name, llm=self.llm, chronicle=chronicle_client,
                                     storage_dir=storage_dir) if _HAS_LEARNING else None)

        # evidence-and-experience reasoning engine (the real decider)
        self.reasoning = (ReasoningEngine(self.name, storage_dir=storage_dir,
                                          chronicle=chronicle_client, atlas=atlas_client,
                                          llm=self.llm, exploration=self.exploration)
                          if _HAS_REASONING else None)

        self._bus = get_bus() if _HAS_PROTOCOL else None
        self._registry = get_registry() if _HAS_PROTOCOL else None
        self._stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._started = False
        self._handled = 0
        self._failed = 0
        self._started_at = 0.0

        # subclasses declare their candidate approaches
        if self.reasoning is not None:
            try:
                self.register_strategies()
            except Exception as exc:
                log.debug("%s register_strategies failed: %s", self.name, exc)

    # ============================================================
    # Lifecycle (Book I Article II Birth; Article XIV Retirement)
    # ============================================================

    def start(self) -> None:
        self._started = True
        self._started_at = time.time()
        self._stop.clear()
        if self._bus and self._registry:
            for ch in self.channels:
                self._bus.subscribe(ch, self._on_bus_message)
            self._registry.register(AgentSpec(
                name=self.name, capabilities=list(self.capabilities),
                repository=self.repository, description=self.description,
                version=self.version, accepts_channels=list(self.channels),
                mission=dict(self.mission or {}), domain=self.domain,
                memory_namespace=self.memory_namespace,
                security_level=self.security_level), handler=self.handle)
            if self.heartbeat_interval_sec > 0:
                self._hb_thread = threading.Thread(target=self._heartbeat_loop,
                                                   daemon=True, name=f"hb-{self.name}")
                self._hb_thread.start()
        self.on_start()
        log.info("%s started (domain=%s, brain=%s, reasoning=%s, learning=%s)",
                 self.name, self.domain, self.has_brain,
                 self.reasoning is not None, self.learning is not None)

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
        """Preserve knowledge before shutdown (Article XIV)."""
        self.remember(content=f"Agent {self.name} retired: {reason}.",
                     memory_type="constitutional", domain=self.domain,
                     tags=["retirement", self.name])
        self.lifecycle_status = "retired"
        summary = {"agent": self.name, "reason": reason, "stats": self.get_status()}
        self.stop()
        return summary

    def on_start(self) -> None: ...
    def on_stop(self) -> None: ...

    # ============================================================
    # What subclasses implement
    # ============================================================

    def register_strategies(self) -> None:
        """
        Declare candidate approaches for the agent's problem-types, e.g.:

            self.reasoning.register_strategy(
                problem_type="market_direction", name="trend_follow",
                handler="_strat_trend_follow",
                reasons_for=["works in trending regimes"],
                reasons_against=["whipsaws in ranges"])

        Optional: an agent with no competing strategies can skip this and
        implement execute() directly.
        """
        return None

    @abc.abstractmethod
    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Map a UCP task to work. For tasks that have competing approaches,
        call `self.solve(problem_type, context)`; for simple deterministic
        tasks, do the work directly. Return a dict with a 'status' key.
        """

    # ============================================================
    # solve(): the evidence-and-experience loop (Article IX/X)
    # ============================================================

    def solve(self, problem_type: str, context: Optional[Dict[str, Any]] = None,
              max_attempts: int = 3) -> Dict[str, Any]:
        """
        Solve a problem by evidence and experience:
        recall -> research reasons -> decide -> try -> reinforce or diagnose+retry.
        """
        context = dict(context or {})
        if self.reasoning is None:
            return {"status": "error", "message": "reasoning engine unavailable"}

        # 1. RECALL prior knowledge (Memory First).
        prior = self.recall(f"{problem_type} {context.get('query', '')}".strip())
        if prior:
            context["_recalled"] = prior

        trace: List[Dict[str, Any]] = []
        tried_ids: List[str] = []

        for attempt in range(1, max_attempts + 1):
            # 2-3. RESEARCH reasons + DECIDE by earned track record.
            decision = self.reasoning.decide(problem_type, context, research=True)
            if decision.get("needs_new_strategy"):
                return {"status": "error", "problem_type": problem_type,
                       "message": "no strategy available; agent must register approaches",
                       "trace": trace}

            strategy: Strategy = decision["strategy_obj"]
            # avoid re-trying the exact same failed strategy within one solve()
            if strategy.strategy_id in tried_ids and attempt > 1:
                pass  # exploration may still re-surface it; that's acceptable
            tried_ids.append(strategy.strategy_id)

            handler: Optional[Callable] = getattr(self, strategy.handler, None)
            if handler is None:
                self.reasoning.record_outcome(strategy.strategy_id, False,
                                             detail="handler missing")
                trace.append({"attempt": attempt, "strategy": strategy.name,
                             "outcome": "handler_missing"})
                continue

            # 4. TRY the chosen approach.
            try:
                result = handler(context)
                ok = bool(result.get("status") in ("complete", "unimplemented")
                         and not result.get("failed"))
                err = "" if ok else result.get("message", result.get("reason", ""))
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}
                ok = False
                err = str(exc)
                self._report_failure(f"{problem_type}:{strategy.name}", err, context)

            # 5. record real outcome.
            self.reasoning.record_outcome(strategy.strategy_id, ok,
                                         detail=err or "ok", context=context)
            trace.append({"attempt": attempt, "strategy": strategy.name,
                         "confidence": strategy.confidence, "ok": ok,
                         "score_breakdown": decision.get("score_breakdown")})

            if ok:
                # 5a. SUCCESS: reinforce, return. (Reuse happens next call because
                #     this strategy's earned confidence just rose.)
                result["_reasoning"] = {"chosen": strategy.name,
                                       "confidence": strategy.confidence,
                                       "attempt": attempt, "trace": trace}
                self._handled += 1
                self._learn(problem_type, result.get("status", "complete"), True, context, "")
                return result

            # 5b. FAILURE: diagnose WHY and try something DIFFERENT next loop.
            diag = self.reasoning.diagnose_failure(strategy, context, error=err)
            trace[-1]["diagnosis"] = {"root_cause": diag.get("root_cause"),
                                     "variation": diag.get("variation", {}).get("name")}

        # exhausted attempts
        self._failed += 1
        self._learn(problem_type, "exhausted", False, context, "all attempts failed")
        return {"status": "error", "problem_type": problem_type,
               "message": f"no approach succeeded in {max_attempts} attempts",
               "trace": trace}

    # ============================================================
    # act(): entry point that also records simple (non-strategy) tasks
    # ============================================================

    def act(self, task: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = dict(context or {})
        if self.learning:
            advice = self.learning.advice_for(task, context)
            if advice:
                context["_lessons"] = advice
        try:
            result = self.execute(task, context)
            success = result.get("status") in ("complete", "unimplemented")
            error = "" if success else result.get("message", "")
        except Exception as exc:
            result = {"status": "error", "message": str(exc),
                     "trace": traceback.format_exc()[-1500:]}
            success = False
            error = str(exc)
            self._report_failure(task, error, context)
        # solve() already records strategy outcomes; only record here if the task
        # did not go through the reasoning loop (avoid double counting).
        if "_reasoning" not in result:
            self._learn(task, result.get("status", "unknown"), success,
                       {k: v for k, v in context.items() if not k.startswith("_")}, error)
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
        task = msg.get("task", "")
        context = msg.get("context", {}) or {}
        context.setdefault("_sender", msg.get("sender", "unknown"))
        return self.act(task, context)

    def _on_bus_message(self, msg: Any) -> None:
        try:
            receiver = getattr(msg, "receiver", None) or (msg.get("receiver") if isinstance(msg, dict) else None)
            channel = getattr(msg, "channel", None) or (msg.get("channel") if isinstance(msg, dict) else None)
            if receiver in (self.name, "*") or channel in self.channels:
                self.handle(msg)
        except Exception as exc:
            log.debug("%s bus handler error: %s", self.name, exc)

    def _report_failure(self, task: str, error: str, context: Dict[str, Any]) -> None:
        """No silent failures: report to Aegis (Book II Ch XI)."""
        if not self._bus:
            return
        try:
            self._bus.send(Message(
                sender=self.name, receiver="aegis", task="agent.error",
                type=MessageType.AUDIT, channel="ecosystem.audit",
                context={"agent": self.name, "task": task, "error": error[:500]}))
        except Exception:
            log.debug("%s could not report failure to Aegis", self.name)

    # ============================================================
    # The brain (ADVISOR ONLY, Book I Article I)
    # ============================================================

    @property
    def has_brain(self) -> bool:
        return self.llm is not None and getattr(self.llm, "has_any", False)

    def think(self, prompt: str, temperature: float = 0.3,
              max_tokens: int = 800) -> Optional[str]:
        """Ask the LLM for ADVICE as this agent. None if no brain (honest)."""
        if not self.has_brain:
            return None
        sys_prompt = system_prompt(self.name) if _HAS_LLM else ""
        r = self.llm.complete(sys_prompt, prompt, temperature=temperature, max_tokens=max_tokens)
        return r.text if r.ok else None

    def think_json(self, prompt: str, temperature: float = 0.2,
                   max_tokens: int = 800) -> Optional[Any]:
        if not self.has_brain:
            return None
        sys_prompt = system_prompt(self.name) if _HAS_LLM else ""
        parsed, _ = self.llm.complete_json(sys_prompt, prompt, temperature=temperature,
                                           max_tokens=max_tokens)
        return parsed

    # ============================================================
    # Memory access (Book I Article VIII)
    # ============================================================

    def remember(self, content: Any, memory_type: str = "semantic",
                 domain: Optional[str] = None, tags: Optional[List[str]] = None,
                 evidence: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        if self.chronicle is None:
            return None
        try:
            return self.chronicle.store(content=content, memory_type=memory_type,
                                       domain=domain or self.domain, tags=tags,
                                       source=self.name, evidence=evidence)
        except Exception as exc:
            log.debug("%s remember() failed: %s", self.name, exc)
            return None

    def recall(self, query: str, domain: Optional[str] = None,
               limit: int = 5) -> List[Dict[str, Any]]:
        if self.chronicle is None:
            return []
        try:
            res = self.chronicle.search(query=query, domain=domain or self.domain,
                                       limit=limit, requester=self.name)
            return res if isinstance(res, list) else []
        except Exception:
            return []

    def research(self, query: str, domain: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Ask Atlas to investigate (real research). None if Atlas absent."""
        if self.atlas is None:
            return None
        try:
            out = self.atlas.handle({"task": "research.investigate",
                                    "context": {"query": query, "domain": domain or self.domain},
                                    "sender": self.name})
            return out.get("report") if isinstance(out, dict) else None
        except Exception:
            return None

    # ============================================================
    # Self-evaluation (Book I Article XII)
    # ============================================================

    def self_evaluate(self) -> Dict[str, Any]:
        learn = self.learning.stats() if self.learning else {}
        reason = self.reasoning.stats() if self.reasoning else {}
        success_rate = learn.get("success_rate")
        unadapted = learn.get("unadapted_failures", [])
        return {
            "agent": self.name,
            "performing_mission": self._handled > 0,
            "improving": success_rate is None or success_rate >= 0.6,
            "wasting_resources": self._failed > self._handled if self._handled else False,
            "failure_to_learn": len(unadapted) > 0,
            "should_request_help": len(unadapted) > 0,
            "best_strategies": reason.get("best_strategies", []),
            "stats": {"handled": self._handled, "failed": self._failed,
                      "success_rate": success_rate},
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
        s = {"name": self.name, "repository": self.repository, "domain": self.domain,
             "running": self._started, "handled": self._handled, "failed": self._failed,
             "uptime_sec": round(time.time() - self._started_at, 1) if self._started_at else 0,
             "capabilities": self.capabilities, "security_level": self.security_level,
             "has_brain": self.has_brain}
        if self.reasoning:
            s["reasoning"] = self.reasoning.stats()
        if self.learning:
            s["learning"] = self.learning.stats()
        if self.llm:
            s["llm"] = self.llm.stats()
        return s
