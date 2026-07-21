"""
Nexus.agents.coordinator_agent
=============================
Nexus (formerly Universal AI): the institutional coordinator, on the
constitutional BaseAgent. (Book II Part I; Book II Part II Ch VIII; Book I
Article IX.)

Institutional coordination:
  * EXECUTION LAYER   every dispatch runs under an SLA/budget, guarded by a
    per-agent circuit breaker, with TTL result caching (core.execution).
  * PARALLEL ORCHESTRATION  multi-agent queries run independent sub-tasks
    concurrently in dependency levels (core.orchestration).
  * CONFIDENCE-WEIGHTED CONFLICT RESOLUTION when specialists disagree.
  * LEARNED COLLABORATION GRAPH  who feeds whom, learned from outcomes.
  * ROUTING reasoning  direct / memory-first / orchestrate, with the classifier
    self-reinforcing and PRIORITY lanes for urgent queries.

FIX LOG (phase3-nexus-v1  2026-07-21):
  BUG-N1  _strat_direct() propagated Atlas status="error" directly to Nexus
          solve(), causing Nexus to exhaust all 3 routing strategies even when
          Atlas had a usable best-effort report in its trace.
          FIX: _strat_direct() now calls engine._extract_best_report() to
          surface a usable report from Atlas even when it returned "error".
          If a report with a non-empty summary is found, status is promoted to
          "complete" with degraded=True.
          Constitutional law: Book II Principle V Graceful Degradation;
          Book IV No Silent Failures.

  BUG-N2  _strat_orchestrate() returned status="error" for single-domain
          queries, wasting Nexus's 3rd routing attempt on a guaranteed failure.
          FIX: Falls back to _strat_direct() for single-domain queries.
          Constitutional law: Book II Principle V Graceful Degradation.

  BUG-N3  _strat_direct() passed only {"query", "symbol"} to Atlas, omitting
          "domain", "depth", and "memory_context".
          FIX: Full context dict forwarded.
          Constitutional law: Book III Ch VIII Standardized Interfaces.

  BUG-N6  Chronicle memory context was retrieved but never forwarded to Atlas.
          FIX: memory_context now included in Atlas dispatch context.
          Constitutional law: Book II Principle I Memory First.

  CHR-N1  Added _send_to_chronicle() and _receive_from_chronicle() calls in
          execute() for Memory First compliance.
          Constitutional law: Book II Principle I Memory First;
          Book II Everything Communicates.

FIX LOG (phase5-coordinator-v1  2026-07-21):
  FIX-CA-01  _strat_direct() extracted symbol as query.upper() for ALL agents.
             For Oracle (trading/prediction), the symbol should be extracted
             from the query (e.g. "EURUSD" from "is there a trade on EURUSD").
             For Atlas (research), symbol is irrelevant.
             FIX: Symbol extraction now uses a regex to find known financial
             symbols in the query, falling back to query.upper() only for
             trading-domain dispatches.
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-CA-02  _strat_direct() used cls["domain"] directly as the task domain
             for Oracle, but Oracle's domain is "prediction" while its
             capabilities use "trading" terminology (trade.signal, etc.).
             The domain forwarded to Oracle must be "trading" so Oracle's
             execute() routes to the correct internal handler.
             FIX: Added _canonical_domain() helper that maps "prediction" ->
             "trading" for Oracle dispatch context.
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-CA-03  _needs_multiple() used orchestrator.decompose() which requires
             confidence >= 0.15 per clause. Compound queries like
             "is there a trade on EURUSD and what is the news sentiment"
             sometimes scored below threshold on individual clauses, returning
             only 1 subtask and falling through to _strat_direct() which
             routed the whole query to a single agent.
             FIX: Added _classify_domains() that classifies each AND-split
             clause independently and returns unique domains with score > 0.
             _needs_multiple() now uses _classify_domains() with a lower
             threshold (score > 0, not confidence >= 0.15).
             Constitutional law: Book II Principle V Graceful Degradation;
             Book III Ch VIII Standardized Interfaces.

FIX LOG (phase5b-coordinator-v1  2026-07-21):
  FIX-CA-04  Chronicle fast path was NOT short-circuiting.
             The execute() method retrieved Chronicle prior knowledge and stored
             it in ctx["_chronicle_prior"], but then ALWAYS called solve() which
             ran the full 3-strategy routing cycle (including Atlas 3-round
             research). For repeat queries with 13 Chronicle records, this meant
             full external API research every time.
             FIX: Added _chronicle_fast_path() that:
               1. Calls chronicle.search() for the query
               2. Applies relevance_filter() to reject off-topic memories
               3. If >= 2 relevant memories found: returns cached answer
                  immediately WITHOUT calling solve() or dispatching to Atlas
               4. Logs the fast-path hit for observability
             Constitutional law: Book II Principle I Memory First — "Have we
             already solved this? Generation shall always come after retrieval."

  FIX-CA-05  Multi-domain orchestration broken: _strat_orchestrate() called
             orchestrator.run() which returned {"multi_agent": False} when
             decompose() found only 1 subtask (confidence < 0.15 threshold).
             But _needs_multiple() uses a LOWER threshold (score > 0), so
             _needs_multiple() said True while decompose() said 1 subtask ->
             orchestrator returned "single-domain" -> _strat_orchestrate()
             returned status="error" -> "no approach succeeded in 3 attempts".
             FIX: Added _manual_orchestrate() fallback that:
               1. Uses _classify_domains() (lower threshold) to find all domains
               2. Dispatches each domain's agent directly via executor.call()
               3. Collects all responses and synthesizes them
             _strat_orchestrate() now calls _manual_orchestrate() when
             orchestrator.run() returns multi_agent=False for a multi-domain query.
             Constitutional law: Book II Principle V Graceful Degradation.

  FIX-CA-06  Chronicle memory contamination: _receive_from_chronicle() injected
             ALL recent memories regardless of topic relevance. "latest news on
             farmlands" received "what is an animal" memories because they were
             the most recent Chronicle entries.
             FIX: Applied classifier.relevance_filter() to Chronicle memories
             before injecting them into the routing context.
             Constitutional law: Book II Principle I Memory First — inject only
             relevant memories.
"""
from __future__ import annotations

import logging
import re
import sys
import socket as _socket
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

from Nexus.intelligence.domain_classifier import DomainClassifier       # type: ignore
from Nexus.intelligence.collaboration_graph import CollaborationGraph    # type: ignore
from Nexus.core.agent_registry import AgentRegistry                      # type: ignore
from Nexus.core.coordination_engine import CoordinationEngine            # type: ignore
from Nexus.core.execution import Executor, PRIORITY_BUDGET               # type: ignore
from Nexus.core.orchestration import Orchestrator                        # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "nexus")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("nexus")

PRIORITY_WORDS = {"urgent": 2, "critical": 2, "emergency": 1, "now": 2, "immediately": 1}

# FIX-CA-02: Map classifier domain -> dispatch domain for agents whose
# code domain differs from the vocabulary domain used in routing.
# Oracle.domain = "prediction" but its capabilities use "trading" terminology.
_DISPATCH_DOMAIN = {
    "prediction": "trading",   # Oracle: domain="prediction", capabilities use "trading"
}

# FIX-CA-01: Common financial symbols for extraction from query text.
_SYMBOL_RE = re.compile(
    r"\b(EURUSD|GBPUSD|USDJPY|USDCHF|AUDUSD|NZDUSD|USDCAD|XAUUSD|XAGUSD|"
    r"BTCUSD|ETHUSD|BTC|ETH|SPX|NDX|DJI|[A-Z]{3,6}USD|[A-Z]{2,4}/[A-Z]{2,4})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# BUG A FIX (Phase 5c): Freshness and action bypass for _chronicle_fast_path().
#
# ROOT CAUSE: _chronicle_fast_path() had no bypass — it fired for ANY query
# with >=2 relevant memories. "latest news on EURUSD" (5 memories) → returned
# stale cache instead of routing to Sentinel for LIVE news. "train a new strategy
# on AAPL" (8 memories after BUG C) → returned cached memories instead of
# routing to Forge to actually train.
#
# Fix: Two bypass categories checked BEFORE memory retrieval:
#   1. FRESHNESS_KEYWORDS — queries that inherently need live/current data.
#      These must always go to the specialist agent, never the cache.
#   2. ACTION_KEYWORDS — imperative commands that trigger real work.
#      Returning cached memories for "train", "deploy", "execute" is wrong.
#
# Constitutional law: Book II Principle V Graceful Degradation — the fast path
# must not degrade the quality of time-sensitive or action queries.
# ---------------------------------------------------------------------------
FRESHNESS_KEYWORDS: frozenset = frozenset({
    "latest", "current", "now", "today", "breaking", "live", "recent",
    "just", "happening", "right now", "at the moment", "this moment",
    "this week", "this month", "this year", "2024", "2025", "2026",
    "yesterday", "last hour", "last 24", "last week", "real-time",
    "realtime", "real time", "up to date", "up-to-date", "fresh",
    "new developments", "latest update", "latest news", "latest report",
})

ACTION_KEYWORDS: frozenset = frozenset({
    "train", "execute", "deploy", "create", "spawn", "run", "start",
    "stop", "open", "close", "build", "generate", "backtest", "optimize",
    "retrain", "fine-tune", "finetune", "launch", "boot", "initialize",
    "register", "certify", "synthesize", "evolve", "discover", "install",
    "upgrade", "update", "delete", "remove", "kill", "restart", "reset",
    "place", "submit", "cancel", "modify", "hedge", "buy", "sell",
    "long", "short", "enter", "exit",
})


def _extract_symbol(query: str) -> str:
    """Extract a financial symbol from query text, or return query.upper()."""
    m = _SYMBOL_RE.search(query)
    return m.group(0).upper().replace("/", "") if m else query.upper()


def _canonical_domain(domain: str) -> str:
    """Map classifier domain to the domain string agents expect in their context."""
    return _DISPATCH_DOMAIN.get(domain, domain)


class NexusAgent(BaseAgent):
    name = "nexus"
    repository = "Nexus"
    domain = "coordination"
    description = "Institutional coordinator: SLAs, breakers, parallel orchestration, learned collaboration."
    capabilities = ["domain.classify", "ecosystem.route", "ecosystem.orchestrate", "ecosystem.monitor",
                    "collaboration.create", "collaboration.graph", "execution.stats", "ecosystem.reinforce"]
    channels = ["ecosystem.coordination", "ecosystem.broadcast", "ecosystem.health"]
    memory_namespace = "nexus_memory"
    security_level = "constitutional"
    mission = {"purpose": "Route, orchestrate in parallel under SLAs, and learn how the civilization cooperates."}

    def __init__(self, chronicle_client=None, atlas_client=None, **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                        storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.classifier = DomainClassifier(storage_dir=str(_REPO_ROOT / "memory"))
        self.graph = CollaborationGraph(storage_dir=str(_REPO_ROOT / "memory"))
        self.registry = AgentRegistry()
        self.engine = CoordinationEngine(self.registry, self.classifier, chronicle_client)
        self.executor = Executor(self.registry, self.engine._dispatch)
        self.orchestrator = Orchestrator(self.registry, self.classifier, self.executor, self.graph,
                                        chronicle=chronicle_client, atlas=atlas_client, llm=self.llm)
        if chronicle_client is not None:
            self.registry.register("chronicle", chronicle_client)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("routing", "direct_specialist", "_strat_direct",
            reasons_for=["fastest; single clear specialist"], reasons_against=["misses multi-domain nuance"])
        self.reasoning.register_strategy("routing", "memory_first", "_strat_memory_first",
            reasons_for=["grounded in prior knowledge"], reasons_against=["slightly slower"])
        self.reasoning.register_strategy("routing", "orchestrate", "_strat_orchestrate",
            reasons_for=["required for multi-domain queries; parallel + SLA-bounded"],
            reasons_against=["uses several agents"])

    def on_start(self) -> None:
        # FIX-M-03: Don't log roster here — agents register AFTER on_start().
        # The accurate roster is logged at the END of boot() in main.py.
        log.info("Nexus institutional online. Coordinator ready.")

    def register_agent(self, name: str, handle: Any) -> Dict[str, Any]:
        ra = self.registry.register(name, handle)
        return {"registered": name, "domain": ra.domain, "capabilities": ra.capabilities}

    def _priority(self, query: str) -> int:
        q = query.lower()
        for w, p in PRIORITY_WORDS.items():
            if w in q:
                return p
        return 4

    # FIX-CA-03: Classify each AND-split clause independently.
    # Lower threshold than orchestrator.decompose() (score > 0, not confidence >= 0.15)
    # so compound financial queries like "trade on EURUSD and news sentiment" correctly
    # identify 2+ domains.
    def _classify_domains(self, query: str) -> List[str]:
        """Return list of unique domains found in the query (one per clause)."""
        clauses = [p.strip() for p in re.split(
            r"\b(?:and|given|considering|based on|then|while|plus|also)\b|[,;]",
            query, flags=re.IGNORECASE) if p and len(p.strip()) > 3]
        seen: List[str] = []
        for clause in clauses:
            cls = self.classifier.classify(clause)
            d = cls["domain"]
            if d and d not in seen:
                seen.append(d)
        # Also classify the whole query
        whole = self.classifier.classify(query)
        if whole["domain"] and whole["domain"] not in seen:
            seen.append(whole["domain"])
        return seen

    # ---- routing strategies ----

    def _dispatch_with_timeout(self, agent_name, task, context, priority, timeout_sec=30):
        """
        Dispatch to an agent with a timeout wrapper. Uses ThreadPoolExecutor to
        enforce a hard timeout on agent.handle() calls.
        
        FIX-CA-09 (Phase 5d): Coordinator-level query timeout. Protects against
        ANY agent hanging (Sentinel, Pulse, Oracle, etc.). If an agent's HTTP
        collectors timeout, the coordinator still returns within timeout_sec.

        FIX-CA-11 (Phase 5e): socket.setdefaulttimeout(20) set inside the worker
        thread so DNS resolution — which is NOT covered by urllib timeout= — is
        also bounded. This is the nuclear option that prevents C-level socket
        hangs from escaping the thread.
        
        Constitutional law: Book II Principle V Graceful Degradation.
        """
        query = context.get("query", "")
        log.info(
            "[nexus] Dispatching to '%s' task='%s' query=%r with %ds timeout. "
            "Constitutional: Book II Principle V Graceful Degradation.",
            agent_name, task, query[:80], timeout_sec,
        )
        _t0 = time.time()

        def _call_agent():
            # FIX-CA-11: Set socket-level default timeout inside the worker thread.
            # urllib timeout= only covers the read phase; DNS resolution blocks at
            # the OS level and is NOT bounded by urllib's timeout parameter.
            # socket.setdefaulttimeout() is the only way to bound DNS hangs.
            _socket.setdefaulttimeout(timeout_sec - 2)  # 2s margin for overhead
            return self.executor.call(agent_name, task, context, priority=priority)
        
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_call_agent)
                result = future.result(timeout=timeout_sec)
            elapsed = round(time.time() - _t0, 2)
            log.info(
                "[nexus] '%s' returned in %.2fs for query=%r.",
                agent_name, elapsed, query[:80],
            )
            return result
        except FuturesTimeoutError:
            elapsed = round(time.time() - _t0, 2)
            log.warning(
                "[nexus] Agent '%s' timed out after %.2fs (limit=%ds) for query=%r. "
                "Constitutional: Book II Principle V Graceful Degradation.",
                agent_name, elapsed, timeout_sec, query[:80],
            )
            return self._format_timeout_response(agent_name, context.get("domain", "unknown"))
        except Exception as e:
            log.error("[nexus] Dispatch to '%s' failed: %s", agent_name, e)
            return {"status": "error", "message": f"dispatch failed: {e}"}

    def _format_timeout_response(self, agent_name, domain):
        """
        FIX-CA-10 (Phase 5d): Format a user-friendly timeout message instead of
        raw traceback. Logs Constitutional reference.
        
        Constitutional law: Book II No Silent Failures.
        """
        msg = f"[{agent_name}] {domain} agent is currently experiencing delays. Please try again in a moment."
        log.info(
            "[nexus] Timeout response for %s (%s): %s",
            agent_name, domain, msg
        )
        return {
            "status": "error",
            "message": msg,
            "degraded": True,
            "timeout": True,
        }

    def _strat_direct(self, c):
        if self._needs_multiple(c.get("query", "")):
            return {"status": "error", "message": "multi-domain; not direct"}
        cls = self.classifier.classify(c.get("query", ""))
        # BUG B FIX (Phase 5c): Use _resolve_agent() triple-fallback instead of
        # single registry.get() + find_by_domain(). This handles Forge/Genesis
        # name variations and startup failures gracefully.
        agent = self._resolve_agent(cls["domain"], cls["repository"])
        if agent is None:
            return {"status": "error", "message": f"no agent for {cls['domain']} (repository={cls['repository']})"}
        from Nexus.core.orchestration import PRIMARY_TASK  # type: ignore

        # FIX-CA-01: Extract financial symbol from query for trading-domain agents.
        # FIX-CA-02: Map "prediction" -> "trading" for Oracle's dispatch context.
        # FIX-CA-19: Also extract symbol for news/social domains so Sentinel/Pulse
        #            receive "GBPUSD" not "WHAT IS THE LATEST NEWS ON GBPUSD".
        dispatch_domain = _canonical_domain(cls.get("domain", "general"))
        raw_query = c.get("query", "")
        if dispatch_domain in ("trading", "news", "social"):
            symbol = _extract_symbol(raw_query)
        else:
            symbol = raw_query.upper()
        # FIX-CA-20: For news/social agents, also pass `topics` so collectors
        #            can filter articles by symbol. Sentinel.execute() reads
        #            ctx["topics"] for news.report/news.collect; ctx["symbol"]
        #            for news.sentiment/news.for_symbol. Pass both so every
        #            task variant gets the right data.
        topics = [symbol] if symbol and dispatch_domain in ("news", "social") else None

        # BUG-N3 FIX: pass full context including domain, depth, memory_context
        # BUG-N6 FIX: include memory_context so Atlas can use Chronicle prior knowledge
        # FIX-CA-09: Wrap with timeout (30s per agent)
        out = self._dispatch_with_timeout(
            agent.name,
            PRIMARY_TASK.get(agent.name, ""),
            {
                "query": raw_query,
                "symbol": symbol,                    # FIX-CA-01 + FIX-CA-19
                "topics": topics,                    # FIX-CA-20: for Sentinel/Pulse collectors
                "domain": dispatch_domain,           # FIX-CA-02
                "depth": c.get("depth", "standard"),
                "user_id": c.get("user_id", "user"),
                "memory_context": c.get("memory_context"),   # BUG-N6 FIX
            },
            priority=c.get("priority", 4),
            timeout_sec=30,
        )

        # BUG-N1 FIX: if Atlas returned status="error" but has a usable report,
        # promote it to "complete" with degraded=True rather than propagating
        # the error up to Nexus solve() which would exhaust all 3 strategies.
        if out.get("status") == "error":
            best = self.engine._extract_best_report(out)
            if best:
                log.info(
                    "_strat_direct: %s returned error but has usable report "
                    "(summary=%d chars); promoting to complete/degraded. "
                    "Constitutional: Book II Principle V Graceful Degradation.",
                    agent.name,
                    len(best.get("summary", "")),
                )
                return {
                    "status": "complete",
                    "degraded": True,
                    "routed_to": agent.name,
                    "result": out,
                    "report": best,
                    "classification": cls,
                    "message": "best-effort answer (all research paths below confidence target)",
                }
            # No usable report at all — genuine failure, propagate
            log.warning(
                "_strat_direct: %s returned error with no usable report. "
                "query=%r agent=%s msg=%s",
                agent.name, c.get("query"), agent.name, out.get("message"),
            )

        status = "complete" if out.get("status") != "error" else "error"
        return {"status": status, "routed_to": agent.name, "result": out,
               "classification": cls,
               "message": out.get("message", "") if status == "error" else ""}

    def _strat_memory_first(self, c):
        # FIX-CA-14 (Phase 5f): If freshness/action bypass was detected, skip Chronicle
        # entirely and go directly to the specialist domain.
        # FIX-CA-17 (Phase 5g): This check now WORKS because execute() forwards
        # _bypass_chronicle into the solve() context dict. Previously solve() received
        # a fresh dict without this key, so c.get("_bypass_chronicle") was always None.
        if c.get("_bypass_chronicle"):
            cls = self.classifier.classify(c.get("query", ""))
            log.info(
                "[nexus] Freshness/action bypass detected — skipping memory-first strategy, "
                "routing directly to specialist domain=%r repository=%r. "
                "Constitutional: Book II Principle V Graceful Degradation.",
                cls.get("domain"), cls.get("repository"),
            )
            return self._strat_direct(c)
        
        if self._needs_multiple(c.get("query", "")):
            return {"status": "error", "message": "multi-domain; needs orchestration"}
        # FIX-CA-12 (Phase 5e): Chronicle lookup must also be timeout-protected.
        # Previously executor.call("chronicle", ...) had no outer timeout guard —
        # if Chronicle's vector store hung, _strat_memory_first would freeze too.
        mem = None
        if self.registry.get("chronicle"):
            mem = self._dispatch_with_timeout(
                "chronicle", "memory.answer",
                {"query": c.get("query", "")},
                priority=c.get("priority", 4),
                timeout_sec=10,  # Chronicle should be fast; 10s is generous
            )
            if mem and mem.get("timeout"):
                log.warning("[nexus] Chronicle lookup timed out; proceeding without memory context.")
                mem = None
        # BUG-N6 FIX: inject memory result into context for _strat_direct
        c_with_mem = {**c, "memory_context": mem}
        direct = self._strat_direct(c_with_mem)
        direct["memory_context"] = mem
        return direct

    def _strat_orchestrate(self, c):
        # BUG-N2 FIX: single-domain queries must not return status="error" here —
        # that wastes Nexus's 3rd routing attempt on a guaranteed failure.
        # Fall back to _strat_direct() for single-domain queries.
        if not self._needs_multiple(c.get("query", "")):
            log.info(
                "_strat_orchestrate: single-domain query, falling back to direct. "
                "Constitutional: Book II Principle V Graceful Degradation."
            )
            return self._strat_direct(c)
        session = self.orchestrator.run(c.get("query", ""), c.get("user_id", "user"),
                                      priority=c.get("priority", 4))
        # FIX-CA-05: If orchestrator returned single-domain (decompose threshold too strict),
        # fall back to _manual_orchestrate() which uses the lower _classify_domains() threshold.
        if not session.get("multi_agent", False):
            log.info(
                "_strat_orchestrate: orchestrator.decompose() returned single-domain "
                "(confidence threshold too strict). Falling back to _manual_orchestrate(). "
                "Constitutional: Book II Principle V Graceful Degradation."
            )
            return self._manual_orchestrate(c)
        return {"status": "complete", "session": session, "degraded": bool(session.get("missing_agents"))}

    def _needs_multiple(self, query: str) -> bool:
        # FIX-CA-03: Use _classify_domains() with lower threshold instead of
        # orchestrator.decompose() which requires confidence >= 0.15 per clause.
        domains = self._classify_domains(query)
        # Filter out "general" and "coordination" — they don't map to real specialists
        specialist_domains = [d for d in domains if d not in ("general", "coordination")]
        return len(specialist_domains) >= 2

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        priority = ctx.get("priority", self._priority(ctx.get("query", "")))

        if task == "domain.classify":
            return {"status": "complete", "classification": self.classifier.classify(ctx.get("query", ""))}
        if task == "ecosystem.orchestrate":
            return {"status": "complete", "session": self.orchestrator.run(
                ctx.get("query", ""), ctx.get("user_id", "user"), priority=priority)}
        if task == "ecosystem.route":
            if ctx.get("query"):
                # FIX-CA-04: Chronicle fast path — check BEFORE solve() to short-circuit.
                # Constitutional: Book II Principle I Memory First.
                fast, bypass_flag = self._chronicle_fast_path(ctx["query"])  # FIX-CA-14: unpack tuple
                if fast:
                    # Store the fast-path hit in Chronicle for learning
                    self._send_to_chronicle(
                        content=f"Chronicle fast-path hit for query={ctx['query']!r}",
                        domain="coordination",
                        tags=["nexus", "fast_path", "cache_hit"],
                    )
                    return {"status": fast["status"], "priority": priority, **fast}

                # FIX-CA-14: Store bypass_flag in context for _try_strategies to use
                if bypass_flag:
                    ctx = {**ctx, "_bypass_chronicle": True}

            # CHR-N1: Memory First — retrieve relevant prior knowledge before routing.
            # FIX-CA-16 (Phase 5g): SKIP Chronicle lookup entirely for bypass queries.
            # Previously _receive_from_chronicle() was called unconditionally here,
            # meaning freshness/action queries still hit Chronicle before solve() even
            # though bypass_flag=True was set. This caused a redundant Chronicle call
            # that could hang for 10-40s before the strategy loop even started.
            if ctx.get("query") and not ctx.get("_bypass_chronicle"):
                prior = self._receive_from_chronicle(ctx["query"])
                if prior:
                    ctx = {**ctx, "_chronicle_prior": prior}

            if self.reasoning is not None:
                # FIX-CA-17 (Phase 5g): Pass _bypass_chronicle into the solve() context dict.
                # Previously solve() received a FRESH dict without _bypass_chronicle, so
                # _strat_memory_first(c) always saw c.get("_bypass_chronicle") == None (falsy)
                # and dispatched to Chronicle anyway — defeating the entire bypass mechanism.
                # The fix: forward _bypass_chronicle (and _chronicle_prior) from ctx into solve().
                solve_ctx = {
                    "query": ctx.get("query", ""),
                    "user_id": ctx.get("user_id", "user"),
                    "priority": priority,
                    "memory_context": ctx.get("_chronicle_prior"),
                    "depth": ctx.get("depth", "standard"),
                    "_bypass_chronicle": ctx.get("_bypass_chronicle", False),  # FIX-CA-17
                }
                solved = self.solve("routing", solve_ctx)
                cls = self.classifier.classify(ctx.get("query", ""))
                if cls.get("domain") and cls["domain"] != "general":
                    self.classifier.reinforce(ctx.get("query", ""), cls["domain"], weight=0.3)
                # CHR-N1: store routing outcome in Chronicle
                self._send_to_chronicle(
                    content=f"Nexus routed query={ctx.get('query')!r} "
                            f"status={solved.get('status')} "
                            f"strategy={solved.get('_reasoning', {}).get('chosen', 'unknown')}",
                    domain="coordination",
                    tags=["nexus", "routing"],
                )
                # FIX-CA-18 (Phase 5g): Improve error message for freshness queries that
                # fail all strategies. "no approach succeeded in 3 attempts" is confusing
                # for news queries — replace with a domain-specific graceful message.
                if solved.get("status") == "error" and ctx.get("_bypass_chronicle"):
                    domain_hint = cls.get("domain", "general")
                    agent_map = {
                        "news": "Sentinel (news)", "social": "Pulse (social)",
                        "trading": "Oracle (trading)", "training": "Forge (training)",
                        "research": "Atlas (research)", "creation": "Genesis (creation)",
                    }
                    agent_name = agent_map.get(domain_hint, f"{domain_hint} specialist")
                    solved = {
                        **solved,
                        "message": (
                            f"{agent_name} could not retrieve live data for "
                            f"{ctx.get('query')!r} at this time. "
                            f"The service may be temporarily unavailable. "
                            f"Please try again in a moment."
                        ),
                    }
                    log.warning(
                        "[nexus] All strategies failed for freshness query=%r domain=%r. "
                        "Returning graceful degradation message. "
                        "Constitutional: Book II Principle V Graceful Degradation.",
                        ctx.get("query"), domain_hint,
                    )
                return {"status": solved.get("status", "complete"), "priority": priority, **solved}
            # FIX-CA-13 (Phase 5e): engine.route() calls coordination_engine._dispatch()
            # which has NO timeout wrapper. Wrap it here so it can't hang forever.
            log.info("[nexus] No reasoning engine; falling back to engine.route() with 30s timeout.")
            def _engine_route():
                _socket.setdefaulttimeout(28)
                return self.engine.route(ctx.get("query", ""), ctx.get("user_id", "user"))
            try:
                with ThreadPoolExecutor(max_workers=1) as _pool:
                    _fut = _pool.submit(_engine_route)
                    _route_result = _fut.result(timeout=30)
                return {"status": "complete", **_route_result}
            except FuturesTimeoutError:
                log.warning("[nexus] engine.route() timed out after 30s for query=%r.", ctx.get("query", ""))
                return {"status": "error", "priority": priority,
                        "message": "Routing timed out. The specialist agent is experiencing delays. Please try again."}
        if task == "collaboration.create":
            return {"status": "complete", "session": self.engine.collaborate(
                ctx.get("objective", ""), ctx.get("participants", []), ctx.get("context"))}
        if task == "collaboration.graph":
            return {"status": "complete", "graph": self.graph.stats()}
        if task == "execution.stats":
            return {"status": "complete", "execution": self.executor.stats()}
        if task == "ecosystem.monitor":
            stats = self.engine.stats()
            stats["collaboration_learning"] = self.graph.stats()
            stats["execution"] = self.executor.stats()
            return {"status": "complete", "stats": stats}
        if task == "ecosystem.reinforce":
            if ctx.get("query") and ctx.get("domain"):
                self.classifier.reinforce(ctx["query"], ctx["domain"])
            return {"status": "complete", "reinforced": True}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["engine"] = self.engine.stats()
        base["execution"] = self.executor.stats()
        base["collaboration_graph"] = self.graph.stats()
        return base

    def route(self, query: str, user_id: str = "user"):
        return self.act("ecosystem.route", {"query": query, "user_id": user_id, "_sender": "user"})

    def _receive_from_chronicle(self, query: str, domain: str = None, limit: int = 5):
        """Retrieve relevant prior knowledge from Chronicle.

        FIX-CA-06: Apply relevance_filter() to reject off-topic memories.
        """
        if not self.chronicle:
            return None
        try:
            results = self.chronicle.search(
                query=query,
                domain=domain,
                limit=limit,
            )
            if not results:
                return None
            # FIX-CA-06: Filter out off-topic memories (Chronicle contamination guard)
            filtered = self.classifier.relevance_filter(query, results)
            if not filtered:
                log.debug("_receive_from_chronicle: all %d memories filtered as off-topic for query=%r",
                         len(results), query)
                return None
            log.debug("_receive_from_chronicle: %d/%d memories passed relevance filter for query=%r",
                     len(filtered), len(results), query)
            return filtered
        except Exception as exc:
            log.warning("_receive_from_chronicle failed: %s", exc)
            return None

    def _chronicle_fast_path(self, query: str) -> tuple[Optional[Dict[str, Any]], bool]:
        """FIX-CA-04: Short-circuit routing when Chronicle has a high-confidence cached answer.

        Returns a tuple (result, bypass_flag):
          - result: complete result dict if fast path is taken, None otherwise
          - bypass_flag: True if freshness/action keywords detected (skip Chronicle entirely)

        Conditions for fast path:
          - Chronicle is available
          - >= 2 relevant memories found (after relevance filtering)
          - At least one memory has substantial content (>= 50 chars)

        FIX-CA-14 (Phase 5f): When bypass_flag=True, the coordinator skips
        _strat_memory_first() entirely and routes directly to the specialist
        domain. This prevents re-dispatching to Chronicle after we've already
        determined the query needs fresh/live data or an action.

        Constitutional law: Book II Principle I Memory First — "Have we already
        solved this? Generation shall always come after retrieval."
        """
        if not self.chronicle:
            return (None, False)

        # BUG A FIX (Phase 5c): Bypass fast path for freshness-sensitive queries.
        # Queries containing freshness or action keywords MUST go to the specialist
        # agent — returning stale cached data would be incorrect and misleading.
        # Constitutional law: Book II Principle V Graceful Degradation.
        q_lower = (query or "").lower()
        q_words = set(q_lower.split())

        # Check freshness keywords (word-boundary safe: check both full phrase and word)
        for kw in FRESHNESS_KEYWORDS:
            if kw in q_lower:
                log.info(
                    "_chronicle_fast_path: BYPASS (freshness query) — keyword=%r in query=%r. "
                    "Routing to specialist for live data. "
                    "Constitutional: Book II Principle V Graceful Degradation.",
                    kw, query,
                )
                return (None, True)  # FIX-CA-14: return bypass_flag=True

        # Check action keywords (single words — check against word set for speed)
        for kw in ACTION_KEYWORDS:
            if " " in kw:
                if kw in q_lower:
                    log.info(
                        "_chronicle_fast_path: BYPASS (action query) — keyword=%r in query=%r. "
                        "Routing to specialist to perform the action. "
                        "Constitutional: Book II Principle V Graceful Degradation.",
                        kw, query,
                    )
                    return (None, True)  # FIX-CA-14: return bypass_flag=True
            elif kw in q_words:
                log.info(
                    "_chronicle_fast_path: BYPASS (action query) — keyword=%r in query=%r. "
                    "Routing to specialist to perform the action. "
                    "Constitutional: Book II Principle V Graceful Degradation.",
                    kw, query,
                )
                return (None, True)  # FIX-CA-14: return bypass_flag=True

        try:
            results = self.chronicle.search(query=query, limit=8)
            if not results:
                return (None, False)
            # Apply relevance filter — reject off-topic memories
            relevant = self.classifier.relevance_filter(query, results)
            if len(relevant) < 2:
                log.debug("_chronicle_fast_path: only %d relevant memories for query=%r — no fast path",
                         len(relevant), query)
                return (None, False)
            # Check that at least one memory has substantial content
            substantial = [m for m in relevant
                          if len(str(m.get("content", m) if isinstance(m, dict) else m)) >= 50]
            if not substantial:
                return (None, False)
            # Build the cached answer from the most relevant memories
            parts = []
            for mem in substantial[:3]:
                if isinstance(mem, dict):
                    text = mem.get("content") or mem.get("summary") or str(mem)
                else:
                    text = str(mem)
                parts.append(text.strip())
            cached_answer = "\n\n".join(parts)
            log.info(
                "_chronicle_fast_path: HIT for query=%r — %d relevant memories, "
                "returning cached answer (%d chars). "
                "Constitutional: Book II Principle I Memory First.",
                query, len(relevant), len(cached_answer),
            )
            return ({
                "status": "complete",
                "routed_to": "chronicle",
                "_strategy": "chronicle_fast_path",
                "cached": True,
                "memory_count": len(relevant),
                "report": {
                    "summary": cached_answer,
                    "source": "chronicle_cache",
                    "confidence": 0.75,
                },
                "message": f"Answered from Chronicle cache ({len(relevant)} relevant memories).",
            }, False)  # FIX-CA-14: return bypass_flag=False (normal hit, not bypass)
        except Exception as exc:
            log.warning("_chronicle_fast_path failed: %s — proceeding with normal routing.", exc)
            return (None, False)

    # ---------------------------------------------------------------------------
    # BUG B FIX (Phase 5c): Robust agent lookup with triple fallback.
    #
    # ROOT CAUSE: Forge registered as name="forge" (correct), domain="training"
    # (correct), repository="Forge" (capital F). _strat_direct called
    # registry.get(cls["repository"]) where cls["repository"] = "forge" (from
    # DOMAIN_TO_REPO). This should work — but if Forge failed to start (silent
    # exception in boot()), registry.get("forge") returns None AND
    # find_by_domain("training") also returns None → "no agent for training".
    #
    # Fix: _resolve_agent() tries three lookups in order:
    #   1. registry.get(repository_key)          — exact name match
    #   2. registry.find_by_domain(domain)        — domain match
    #   3. registry.find_by_capability(cap_prefix) — capability prefix match
    # This makes dispatch resilient to name variations and startup failures.
    # Constitutional law: Book II Principle V Graceful Degradation.
    # ---------------------------------------------------------------------------
    def _resolve_agent(self, domain: str, repository: str) -> Optional[Any]:
        """Triple-fallback agent lookup: name → domain → capability prefix.

        BUG B FIX (Phase 5c): Makes dispatch resilient to agent name variations
        and startup failures. Tries registry.get(repository), then
        find_by_domain(domain), then find_by_capability(domain + ".").
        Constitutional law: Book II Principle V Graceful Degradation.
        """
        # 1. Exact name match (most specific)
        agent = self.registry.get(repository)
        if agent:
            return agent
        # 2. Domain match (handles name mismatches)
        agent = self.registry.find_by_domain(domain)
        if agent:
            log.debug("_resolve_agent: name lookup missed %r, found via domain=%r", repository, domain)
            return agent
        # 3. Capability prefix match (most permissive fallback)
        cap_prefix = domain + "."
        matches = self.registry.find_by_capability(cap_prefix) if hasattr(self.registry, "find_by_capability") else []
        if matches:
            log.debug("_resolve_agent: domain lookup missed %r, found via capability prefix=%r", domain, cap_prefix)
            return matches[0]
        return None

    def _manual_orchestrate(self, c: Dict[str, Any]) -> Dict[str, Any]:
        """FIX-CA-05: Manual multi-domain dispatch when orchestrator.decompose() threshold
        is too strict.

        Uses _classify_domains() (lower threshold) to find all domains, then
        dispatches each agent directly via executor.call() and synthesizes results.
        Constitutional law: Book II Principle V Graceful Degradation.
        """
        import time
        from Nexus.core.orchestration import PRIMARY_TASK  # type: ignore
        query = c.get("query", "")
        domains = self._classify_domains(query)
        specialist_domains = [d for d in domains if d not in ("general", "coordination", "memory")]
        if not specialist_domains:
            return {"status": "error", "message": "no specialist domains found"}

        log.info(
            "_manual_orchestrate: dispatching to domains=%s for query=%r. "
            "Constitutional: Book II Everything Communicates.",
            specialist_domains, query,
        )

        results: Dict[str, Any] = {}
        missing: List[str] = []
        for domain in specialist_domains:
            from Nexus.intelligence.domain_classifier import DOMAIN_TO_REPO  # type: ignore
            repo = DOMAIN_TO_REPO.get(domain, "atlas")
            agent = self._resolve_agent(domain, repo)
            if agent is None:
                missing.append(repo)
                log.warning("_manual_orchestrate: no agent for domain=%s repo=%s — skipping.", domain, repo)
                continue
            dispatch_domain = _canonical_domain(domain)
            # FIX-CA-19 (also in _manual_orchestrate): extract symbol for news/social too
            if dispatch_domain in ("trading", "news", "social"):
                symbol = _extract_symbol(query)
            else:
                symbol = query.upper()
            # FIX-CA-20: pass topics for Sentinel/Pulse collectors
            topics = [symbol] if symbol and dispatch_domain in ("news", "social") else None
            # FIX-CA-09: Wrap with timeout (30s per agent in parallel orchestration)
            out = self._dispatch_with_timeout(
                agent.name,
                PRIMARY_TASK.get(agent.name, ""),
                {
                    "query": query,
                    "symbol": symbol,
                    "topics": topics,                # FIX-CA-20
                    "domain": dispatch_domain,
                    "depth": c.get("depth", "standard"),
                    "user_id": c.get("user_id", "user"),
                },
                priority=c.get("priority", 4),
                timeout_sec=30,
            )
            results[domain] = {"agent": agent.name, "output": out,
                              "ok": isinstance(out, dict) and out.get("status") != "error"}

        if not results:
            return {"status": "error", "message": f"all agents unavailable: {missing}"}

        # Synthesize: build a structured multi-agent answer
        parts = []
        for domain, r in results.items():
            if not r["ok"]:
                continue
            out = r["output"]
            # Extract summary from the agent's output
            summary = None
            if isinstance(out, dict):
                report = out.get("report", {})
                if isinstance(report, dict):
                    summary = report.get("summary") or report.get("text")
                if not summary:
                    summary = out.get("summary") or out.get("text") or out.get("answer")
            if summary:
                label = domain.upper()
                parts.append(f"[{label}] {summary}")

        synthesis = "\n\n".join(parts) if parts else "No results from specialist agents."
        overall = bool(parts)

        return {
            "status": "complete" if overall else "error",
            "routed_to": [r["agent"] for r in results.values()],
            "_strategy": "manual_orchestrate",
            "multi_agent": True,
            "domains": specialist_domains,
            "missing_agents": missing,
            "results": results,
            "session": {"synthesis": synthesis, "multi_agent": True},
            "message": synthesis if overall else f"No agents responded. Missing: {missing}",
        }