"""
shared.reasoning
================
The reasoning engine: how agents decide by EVIDENCE and EXPERIENCE, not by
asking an LLM for the answer. (Book I Part IV Article IX: Trial and Error;
Article X: Decision Making; Book II Ch IV: Research Before Assumption.)

Core idea
---------
An agent facing a situation does NOT immediately "generate an answer." It:

  1. RECALLS whether this problem-type was solved before (prior strategies).
  2. RESEARCHES reasons FOR and AGAINST each candidate approach. Reasons come
     from memory (Chronicle), research (Atlas), and OPTIONALLY the LLM, which
     only helps articulate/weigh reasons. The LLM never gets the final vote.
  3. DECIDES by evidence-weighted track record. Confidence is EARNED via the
     Wilson lower bound on a strategy's success rate, so a strategy must
     repeatedly succeed to be trusted (no fabricated confidence).
  4. TRIES the chosen strategy and measures the real outcome.
  5. On SUCCESS: reinforces it; reuses it; keeps a small exploration budget so
     a BETTER strategy can still be discovered ("until a better way comes up").
  6. On FAILURE: diagnoses WHY, lowers the strategy's standing, and tries a
     DIFFERENT approach. The failure itself becomes evidence for next time.

Selection uses a UCB-style rule: exploit strategies with proven track records,
but give under-tried strategies an exploration bonus. This is genuine
trial-and-error with retention, not a coin flip and not an LLM oracle.

Strategies and their track records persist to disk, so learning compounds
across restarts and is preserved to Chronicle for the whole civilization.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("shared.reasoning")


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """
    Lower bound of the Wilson score interval for a success proportion.
    A strategy with 1/1 success scores lower than one with 50/50, because
    trust must be EARNED through repeated evidence. This is the mathematical
    embodiment of "confidence shall never be fabricated" (Book II UCP Ch VI).
    """
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


@dataclass
class Strategy:
    """One approach to a class of problems, with an earned track record."""
    strategy_id: str
    problem_type: str
    name: str
    description: str = ""
    handler: str = ""                      # name of the agent method that enacts it
    params: Dict[str, Any] = field(default_factory=dict)
    reasons_for: List[str] = field(default_factory=list)
    reasons_against: List[str] = field(default_factory=list)
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    last_outcome: str = ""
    last_used: float = 0.0
    status: str = "candidate"              # candidate | active | deprecated
    created_at: float = field(default_factory=time.time)
    lineage: Optional[str] = None          # parent strategy this was varied from
    researched: bool = False               # True once research_reasons() has been
                                            # attempted, regardless of whether Atlas/
                                            # LLM calls succeeded -- prevents decide()
                                            # from retrying a failing/rate-limited
                                            # external call on every single attempt

    @property
    def confidence(self) -> float:
        """Earned confidence: Wilson lower bound on the success rate."""
        return round(wilson_lower_bound(self.successes, self.attempts), 4)

    @property
    def net_reason_weight(self) -> float:
        """Reasons FOR minus reasons AGAINST, normalized to roughly [-1, 1]."""
        total = len(self.reasons_for) + len(self.reasons_against)
        if total == 0:
            return 0.0
        return (len(self.reasons_for) - len(self.reasons_against)) / total

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["confidence"] = self.confidence
        return d


class ReasoningEngine:
    """
    Evidence-and-experience decision engine shared by every agent.

    The agent registers candidate strategies for a problem-type (each a named
    handler method). The engine researches reasons, selects by UCB over earned
    confidence, and records outcomes. It exposes:

        decide(problem_type, context)      -> chosen Strategy (+ trace)
        record_outcome(strategy_id, ok)    -> updates track record
        diagnose_failure(strategy, ctx)    -> a reason + a proposed variation
    """

    def __init__(self, agent_name: str, storage_dir: str = "memory",
                 chronicle=None, atlas=None, llm=None,
                 exploration: float = 0.35):
        self.agent = agent_name
        self.chronicle = chronicle
        self.atlas = atlas
        self.llm = llm
        self.exploration = exploration       # UCB exploration weight
        self._lock = threading.RLock()
        self._strategies: Dict[str, Strategy] = {}
        self._by_problem: Dict[str, List[str]] = {}
        self._path = Path(storage_dir) / f"{agent_name}_strategies.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data.get("strategies", []):
                s = Strategy(**{k: v for k, v in item.items() if k != "confidence"})
                self._strategies[s.strategy_id] = s
                self._by_problem.setdefault(s.problem_type, []).append(s.strategy_id)
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps(
                {"agent": self.agent,
                 "strategies": [s.to_dict() for s in self._strategies.values()]},
                indent=2), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent (best-effort)

    # ---- registering approaches ----

    def register_strategy(self, problem_type: str, name: str, handler: str,
                         description: str = "", params: Optional[Dict] = None,
                         reasons_for: Optional[List[str]] = None,
                         reasons_against: Optional[List[str]] = None,
                         lineage: Optional[str] = None) -> Strategy:
        """Register a candidate approach for a problem-type.

        Idempotent: if a strategy with this exact name already exists for
        this problem_type (e.g. reloaded from persisted storage on a prior
        boot), reuse it instead of creating a duplicate. Every fresh process
        launch used to call this for the same canonical strategies, silently
        piling up duplicates (52 found in production after ~13 boots) which
        broke exclude_ids-based rotation and caused decide() to repeatedly
        re-run expensive Atlas/LLM research on "new" duplicates that were
        functionally the same strategy.
        """
        with self._lock:
            for sid in self._by_problem.get(problem_type, []):
                existing = self._strategies.get(sid)
                if existing is not None and existing.name == name and existing.status != "deprecated":
                    return existing

            sid = f"strat-{uuid.uuid4().hex[:10]}"
            s = Strategy(strategy_id=sid, problem_type=problem_type, name=name,
                        description=description, handler=handler, params=params or {},
                        reasons_for=reasons_for or [], reasons_against=reasons_against or [],
                        lineage=lineage)
            self._strategies[sid] = s
            self._by_problem.setdefault(problem_type, []).append(sid)
            self._persist()
            return s

    def candidates(self, problem_type: str) -> List[Strategy]:
        with self._lock:
            return [self._strategies[sid] for sid in self._by_problem.get(problem_type, [])
                   if self._strategies[sid].status != "deprecated"]

    # ---- step 2: research reasons for AND against ----

    def research_reasons(self, strategy: Strategy, context: Dict[str, Any]) -> Strategy:
        """
        Gather reasons FOR and AGAINST a strategy from memory, Atlas, and the
        LLM (optional). This is 'find reasons and contradicting reasons'.
        """
        # From memory: has this strategy or similar succeeded/failed before?
        if self.chronicle is not None:
            try:
                mem = self.chronicle.search(query=f"{strategy.problem_type} {strategy.name}",
                                           domain="strategy", limit=3, requester=self.agent)
                for m in mem:
                    summary = m.get("summary", "") if isinstance(m, dict) else str(m)
                    if any(w in summary.lower() for w in ("worked", "success", "profit", "correct")):
                        strategy.reasons_for.append(f"memory: {summary[:120]}")
                    elif any(w in summary.lower() for w in ("failed", "loss", "wrong", "error")):
                        strategy.reasons_against.append(f"memory: {summary[:120]}")
            except Exception:
                pass

        # From Atlas: real research on the approach (contradicting evidence too).
        if self.atlas is not None:
            try:
                out = self.atlas.handle({"task": "research.investigate",
                                        "context": {"query": f"{strategy.name} for {strategy.problem_type}",
                                                   "domain": strategy.problem_type},
                                        "sender": self.agent})
                report = out.get("report", {})
                conf = report.get("confidence", 0)
                if conf >= 0.5:
                    strategy.reasons_for.append(
                        f"research supports (conf {conf}): {report.get('summary','')[:120]}")
                elif report.get("summary"):
                    strategy.reasons_against.append(
                        f"research is weak (conf {conf})")
            except Exception:
                pass

        # From LLM (optional advisor only): ask for one contradicting reason.
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                r = self.llm.complete(
                    system_prompt(self.agent),
                    f"Approach '{strategy.name}' for problem '{strategy.problem_type}'. "
                    f"Give ONE strong reason it might FAIL. One sentence.",
                    temperature=0.4, max_tokens=80)
                if r.ok and r.text.strip():
                    strategy.reasons_against.append(f"caution: {r.text.strip()[:120]}")
            except Exception:
                pass

        strategy.researched = True
        with self._lock:
            self._persist()
        return strategy

    # ---- step 3: decide (exploit + explore) ----

    def decide(self, problem_type: str, context: Optional[Dict] = None,
               research: bool = True, exclude_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Choose a strategy for the problem using earned confidence + exploration.
        Returns {strategy, score, trace} or a signal that a NEW approach is needed.
        exclude_ids: strategy_ids already tried this solve() call -- skipped
        unless excluding them would leave no candidates at all.
        """
        context = context or {}
        cands = self.candidates(problem_type)
        if exclude_ids:
            remaining = [s for s in cands if s.strategy_id not in exclude_ids]
            if remaining:  # only exclude if something else is actually available
                cands = remaining
        if not cands:
            return {"strategy": None, "needs_new_strategy": True,
                   "reason": f"no candidate strategies for '{problem_type}'"}

        # Optionally research reasons for the top few unresearched candidates.
        if research:
            for s in cands:
                if not s.researched:
                    self.research_reasons(s, context)

        total_attempts = sum(s.attempts for s in cands)
        scored: List[Tuple[Strategy, float, Dict]] = []
        for s in cands:
            conf = s.confidence
            # UCB exploration bonus: under-tried strategies get a boost, so a
            # better approach can surface even while a decent one is in use.
            bonus = self.exploration * math.sqrt(
                math.log(total_attempts + 2) / (s.attempts + 1))
            reason_bias = 0.15 * s.net_reason_weight   # evidence nudges, not decides
            score = conf + bonus + reason_bias
            scored.append((s, score, {"confidence": conf, "explore_bonus": round(bonus, 3),
                                      "reason_bias": round(reason_bias, 3)}))

        scored.sort(key=lambda x: x[1], reverse=True)
        chosen, score, breakdown = scored[0]
        return {
            "strategy": chosen.to_dict(),
            "strategy_obj": chosen,
            "score": round(score, 4),
            "score_breakdown": breakdown,
            "alternatives": [{"name": s.name, "confidence": s.confidence,
                            "score": round(sc, 4)} for s, sc, _ in scored[1:4]],
            "needs_new_strategy": False,
        }

    # ---- step 5/6: record outcome, reinforce or diagnose ----

    def record_outcome(self, strategy_id: str, success: bool,
                       detail: str = "", context: Optional[Dict] = None) -> Dict[str, Any]:
        """Update a strategy's track record. Reinforce on success; flag on failure."""
        with self._lock:
            s = self._strategies.get(strategy_id)
            if not s:
                return {"error": "unknown strategy"}
            s.attempts += 1
            s.last_used = time.time()
            s.last_outcome = "success" if success else "failure"
            if success:
                s.successes += 1
                # Promote to active once it has proven itself.
                if s.status == "candidate" and s.confidence >= 0.5:
                    s.status = "active"
            else:
                s.failures += 1
                # Deprecate a strategy that keeps failing after fair trials.
                if s.attempts >= 5 and s.confidence < 0.2:
                    s.status = "deprecated"
            self._persist()

        # Preserve the outcome as civilization memory.
        self._preserve_outcome(s, success, detail)
        return {"strategy_id": strategy_id, "confidence": s.confidence,
               "status": s.status, "attempts": s.attempts}

    def diagnose_failure(self, strategy: Strategy, context: Dict[str, Any],
                        error: str = "") -> Dict[str, Any]:
        """
        Ask WHY a strategy failed and propose a DIFFERENT approach to try.
        Uses the LLM as an advisor if present; otherwise falls back to letting
        decide()'s exclude_ids naturally move on to another registered
        strategy next attempt, rather than manufacturing a same-handler clone
        that isn't actually a different approach.
        Returns {root_cause, variation} where variation may be None when no
        genuinely different approach was proposed.
        """
        root_cause = error or strategy.last_outcome or "unknown"
        variation_desc = None
        proposed_different_handler = False

        # NOTE: intentionally disabled. This call no longer changes which
        # strategy gets chosen (see the fix above that stops cloning the
        # same handler under a new name) -- its only remaining effect was a
        # cosmetic root_cause annotation. But it's called once per FAILED
        # attempt, and a threat-response loop with many violations can
        # trigger dozens of these in one command, which under any real-world
        # rate limit turns a sub-second operation into a multi-minute one
        # (observed: ~6 minutes for `threat X 10` against a rate-limited
        # Gemini key). Not worth the latency/cost for a cosmetic label.
        if False and self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                parsed = None
                sys_p = system_prompt(self.agent)
                r = self.llm.complete_json(
                    sys_p,
                    f"Strategy '{strategy.name}' for '{strategy.problem_type}' failed. "
                    f"Error: {error}. Context: {json.dumps(context)[:400]}.\n"
                    f"Return JSON: {{\"root_cause\": str, \"different_approach\": str}}",
                    temperature=0.4, max_tokens=200)
                parsed = r[0]
                if parsed and isinstance(parsed, dict):
                    root_cause = parsed.get("root_cause", root_cause)
                    variation_desc = parsed.get("different_approach")
                    proposed_different_handler = bool(variation_desc)
            except Exception as exc:
                log.warning("LLM advisor call failed during diagnose_failure: %s", exc)

        # Record the lesson either way -- this strategy now has a known
        # failure mode, which lowers its future score via reason_bias.
        strategy.reasons_against.append(f"failed: {root_cause[:100]}")

        variation = None
        if proposed_different_handler:
            # NOTE: we deliberately do NOT clone strategy.handler here.
            # There's no mechanism for the LLM's text description to map to
            # an actually different handler function, so registering
            # "{name} (revised)" with the same handler produces a strategy
            # that is functionally identical to the one that just failed --
            # it will fail for the same reason, crowd out the genuinely
            # different registered strategies (quarantine/self_heal/escalate)
            # via the exploration bonus, and never let decide()'s exclude_ids
            # do its job. We record the diagnosis for learning purposes only
            # and let exclude_ids naturally move on to a real alternative.
            pass

        with self._lock:
            self._persist()

        return {"root_cause": root_cause,
               "variation": variation.to_dict() if variation else None,
               "message": ("diagnosed failure; registered a different approach to try next"
                          if variation else
                          "diagnosed failure; no new approach proposed, will try another registered strategy next")}

    def _preserve_outcome(self, s: Strategy, success: bool, detail: str) -> None:
        if self.chronicle is None:
            return
        try:
            verdict = "worked" if success else "failed"
            self.chronicle.store(
                content=f"Strategy '{s.name}' for {s.problem_type} {verdict}. "
                       f"Confidence now {s.confidence}. {detail}",
                memory_type="evolutionary", domain="strategy",
                tags=["strategy", s.problem_type, verdict], source=self.agent)
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_status: Dict[str, int] = {}
            for s in self._strategies.values():
                by_status[s.status] = by_status.get(s.status, 0) + 1
            active = [s for s in self._strategies.values() if s.status == "active"]
            return {
                "agent": self.agent,
                "total_strategies": len(self._strategies),
                "by_status": by_status,
                "problem_types": list(self._by_problem.keys()),
                "best_strategies": sorted(
                    [{"name": s.name, "problem": s.problem_type,
                      "confidence": s.confidence, "attempts": s.attempts}
                     for s in active], key=lambda x: x["confidence"], reverse=True)[:5],
            }