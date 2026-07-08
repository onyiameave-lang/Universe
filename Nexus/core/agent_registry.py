"""
Nexus.core.agent_registry
========================
Live registry of the ecosystem's active agents and their health.
(Book I Part IV Article XI Standardized Interfaces; Book III Part III Agent
Discovery; Book III Part II Ch XIII Repository Health.)

Holds real references to running agent objects, tracks health from real call
latency and failure rates, and answers "who can do X?" from actual declared
capabilities. This is the connective tissue Nexus uses to route work.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional


class RegisteredAgent:
    def __init__(self, name: str, handle: Any):
        self.name = name
        self.handle = handle
        self.repository = getattr(handle, "repository", name)
        self.domain = getattr(handle, "domain", "")
        self.capabilities = list(getattr(handle, "capabilities", []))
        self.registered_at = time.time()
        self.last_seen = time.time()
        self.calls = 0
        self.failures = 0
        self.total_latency = 0.0

    @property
    def healthy(self) -> bool:
        recent = (time.time() - self.last_seen) < 120
        rate_ok = (self.failures / self.calls) < 0.5 if self.calls else True
        return recent and rate_ok

    @property
    def avg_latency_ms(self) -> float:
        return round((self.total_latency / self.calls) * 1000, 2) if self.calls else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "repository": self.repository, "domain": self.domain,
                "capabilities": self.capabilities, "healthy": self.healthy,
                "calls": self.calls, "failures": self.failures,
                "avg_latency_ms": self.avg_latency_ms,
                "seconds_since_seen": round(time.time() - self.last_seen, 1)}


class AgentRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._agents: Dict[str, RegisteredAgent] = {}

    def register(self, name: str, handle: Any) -> RegisteredAgent:
        with self._lock:
            ra = RegisteredAgent(name, handle)
            self._agents[name] = ra
            return ra

    def unregister(self, name: str) -> None:
        with self._lock:
            self._agents.pop(name, None)

    def get(self, name: str) -> Optional[RegisteredAgent]:
        with self._lock:
            return self._agents.get(name)

    def has_domain(self, domain: str) -> bool:
        with self._lock:
            return any(a.domain == domain for a in self._agents.values())

    def find_by_domain(self, domain: str) -> Optional[RegisteredAgent]:
        with self._lock:
            healthy = [a for a in self._agents.values() if a.domain == domain and a.healthy]
            if healthy:
                return healthy[0]
            any_match = [a for a in self._agents.values() if a.domain == domain]
            return any_match[0] if any_match else None

    def find_by_capability(self, capability: str) -> List[RegisteredAgent]:
        with self._lock:
            return [a for a in self._agents.values() if capability in a.capabilities]

    def record_call(self, name: str, latency: float, success: bool) -> None:
        with self._lock:
            a = self._agents.get(name)
            if a:
                a.calls += 1
                a.total_latency += latency
                a.last_seen = time.time()
                if not success:
                    a.failures += 1

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {n: a.to_dict() for n, a in self._agents.items()}

    def health_summary(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._agents)
            healthy = sum(1 for a in self._agents.values() if a.healthy)
            return {"total_agents": total, "healthy": healthy, "unhealthy": total - healthy,
                   "domains_covered": sorted({a.domain for a in self._agents.values() if a.domain}),
                   "agents": self.all()}
