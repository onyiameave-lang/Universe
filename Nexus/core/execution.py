"""
Nexus.core.execution
====================
Institutional execution layer: SLAs/budgets, circuit breakers, result caching,
and parallel dispatch. (Book III Ch VIII Scalability; Book II Part II Ch V
Priority Levels; institutional orchestration: bounded latency, cost, and
fault isolation.)

Real production-hardening for the coordinator:

  * BUDGETS / SLAs   every dispatch runs under a deadline; late results are
                     returned as timed-out rather than blocking the pipeline.
  * PRIORITY LANES   emergency/critical/normal/background priorities (Book II
                     Part II Ch V) influence timeout generosity.
  * CIRCUIT BREAKERS per-agent breakers trip after repeated failures, so Nexus
                     stops routing to a sick agent and fails over, then
                     half-opens to test recovery. (Classic 3-state breaker.)
  * RESULT CACHE     TTL cache memoizes recent (agent, task, key) results, so
                     identical sub-queries don't re-run the whole pipeline.
  * PARALLEL DISPATCH independent sub-tasks run concurrently via a thread pool.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


# Priority -> latency budget (seconds). Book II Part II Ch V.
PRIORITY_BUDGET = {1: 8.0, 2: 6.0, 3: 4.0, 4: 3.0, 5: 2.0}   # emergency..background


class CircuitBreaker:
    """Per-agent 3-state breaker: closed -> open -> half-open -> closed."""
    def __init__(self, fail_threshold: int = 4, reset_after: float = 30.0):
        self.fail_threshold = fail_threshold
        self.reset_after = reset_after
        self.failures = 0
        self.state = "closed"
        self.opened_at = 0.0

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.opened_at >= self.reset_after:
                self.state = "half-open"   # allow a single trial
                return True
            return False
        return True  # half-open: allow the trial call

    def record(self, success: bool) -> None:
        if success:
            self.failures = 0
            self.state = "closed"
        else:
            self.failures += 1
            if self.state == "half-open" or self.failures >= self.fail_threshold:
                self.state = "open"
                self.opened_at = time.time()

    def status(self) -> Dict[str, Any]:
        return {"state": self.state, "failures": self.failures}


class ResultCache:
    """TTL cache for (agent, task, key) -> result."""
    def __init__(self, ttl_sec: float = 60.0, max_entries: int = 500):
        self.ttl = ttl_sec
        self.max = max_entries
        self._lock = threading.RLock()
        self._store: Dict[str, Tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, agent: str, task: str, context: Dict[str, Any]) -> str:
        material = json.dumps({"a": agent, "t": task,
                              "q": context.get("query") or context.get("symbol") or context.get("clause")},
                             sort_keys=True)
        return hashlib.md5(material.encode()).hexdigest()

    def get(self, agent, task, context) -> Optional[Any]:
        k = self._key(agent, task, context)
        with self._lock:
            item = self._store.get(k)
            if item and (time.time() - item[0]) < self.ttl:
                self.hits += 1
                return item[1]
            if item:
                self._store.pop(k, None)
            self.misses += 1
            return None

    def put(self, agent, task, context, result) -> None:
        k = self._key(agent, task, context)
        with self._lock:
            if len(self._store) >= self.max:
                # evict oldest
                oldest = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(oldest, None)
            self._store[k] = (time.time(), result)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {"entries": len(self._store), "hits": self.hits, "misses": self.misses,
               "hit_rate": round(self.hits / total, 3) if total else 0.0}


class Executor:
    """Runs dispatches with budgets, breakers, caching, and parallelism."""

    def __init__(self, registry, dispatch_fn: Callable[[str, str, Dict], Dict],
                 cache_ttl: float = 60.0):
        self.registry = registry
        self._dispatch = dispatch_fn
        self.cache = ResultCache(ttl_sec=cache_ttl)
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def _breaker(self, agent: str) -> CircuitBreaker:
        with self._lock:
            if agent not in self._breakers:
                self._breakers[agent] = CircuitBreaker()
            return self._breakers[agent]

    def call(self, agent_name: str, task: str, context: Dict[str, Any],
             priority: int = 4, use_cache: bool = True) -> Dict[str, Any]:
        """One guarded dispatch: cache -> breaker -> budgeted execution."""
        # 1. cache
        if use_cache:
            cached = self.cache.get(agent_name, task, context)
            if cached is not None:
                return {**cached, "_cached": True}

        # 2. circuit breaker
        breaker = self._breaker(agent_name)
        if not breaker.allow():
            return {"status": "error", "message": f"circuit open for {agent_name}",
                   "_breaker": breaker.status()}

        # 3. budgeted execution
        budget = PRIORITY_BUDGET.get(priority, 3.0)
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(self._dispatch, agent_name, task, context)
            try:
                result = fut.result(timeout=budget)
                success = isinstance(result, dict) and result.get("status") != "error"
            except concurrent.futures.TimeoutError:
                result = {"status": "error", "message": f"SLA breach: {agent_name} exceeded {budget}s"}
                success = False
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}
                success = False
        latency = time.time() - start
        breaker.record(success)
        self.registry.record_call(agent_name, latency, success)
        result["_latency_ms"] = round(latency * 1000, 1)
        if success and use_cache:
            self.cache.put(agent_name, task, context, result)
        return result

    def call_parallel(self, jobs: List[Tuple[str, str, Dict[str, Any]]],
                     priority: int = 4) -> Dict[str, Dict[str, Any]]:
        """Run independent (agent, task, context) jobs concurrently."""
        results: Dict[str, Dict[str, Any]] = {}
        if not jobs:
            return results
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), 6)) as pool:
            future_map = {pool.submit(self.call, a, t, c, priority): a for a, t, c in jobs}
            for fut in concurrent.futures.as_completed(future_map):
                agent = future_map[fut]
                try:
                    results[agent] = fut.result()
                except Exception as exc:
                    results[agent] = {"status": "error", "message": str(exc)}
        return results

    def breaker_states(self) -> Dict[str, Any]:
        with self._lock:
            return {a: b.status() for a, b in self._breakers.items()}

    def stats(self) -> Dict[str, Any]:
        return {"cache": self.cache.stats(), "breakers": self.breaker_states()}
