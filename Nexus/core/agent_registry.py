"""
Nexus.core.agent_registry
========================
Live registry of the ecosystem's active agents and their health.
(Book I Part IV Article XI Standardized Interfaces; Book III Part III Agent
Discovery; Book III Part II Ch XIII Repository Health.)

Holds real references to running agent objects, tracks health from real call
latency and failure rates, and answers "who can do X?" from actual declared
capabilities. This is the connective tissue Nexus uses to route work.

FIX LOG (phase5-orchestration-v1  2026-07-21):
  BUG-P5-01  find_by_domain() matched ONLY on the agent's self-declared
             `domain` string. But agents and the classifier disagree on
             domain names: Oracle self-declares domain="prediction" while the
             classifier's canonical routing domain is "trading". Result:
             find_by_domain("trading") returned None even though Oracle was
             live and registered, silently breaking trading routing.
             FIX: The registry now maintains an EXTENSIBLE domain->repo map
             (DOMAIN_TO_REPO, shared with the classifier) plus per-agent
             registered domain aliases. find_by_domain() resolves a canonical
             domain to the owning repository through this map, then falls back
             to self-declared domain and finally capability match. New agents
             register their own domain aliases via register_domain() with NO
             code change to Nexus (future-proof).
             Constitutional law: Book II Principle II Everything Communicates
             (standardized interfaces); Book III Part III Agent Discovery.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

# Canonical routing-domain -> repository name. Kept in sync with the
# DomainClassifier's DOMAIN_TO_REPO. This is the single source of truth for
# "which repository owns a routing domain". New domains added here (or via
# register_domain() at runtime) are immediately routable with no Nexus change.
DOMAIN_TO_REPO: Dict[str, str] = {
    "trading": "oracle",
    "prediction": "oracle",
    "news": "sentinel",
    "social": "pulse",
    "intelligence": "pulse",
    "research": "atlas",
    "general": "atlas",
    "training": "forge",
    "memory": "chronicle",
    "governance": "aegis",
    "security": "aegis",
    "risk": "aegis",
    "creation": "genesis",
    "coordination": "nexus",
}


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
        # Extensible canonical-domain -> repository map. Seeded from the shared
        # DOMAIN_TO_REPO; agents may extend it at registration time.
        self._domain_to_repo: Dict[str, str] = dict(DOMAIN_TO_REPO)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, handle: Any) -> RegisteredAgent:
        """
        Register a live agent AND auto-register its domain aliases so
        find_by_domain() resolves both the canonical routing domain and the
        agent's self-declared domain to this repository.
        Constitutional law: Book II Principle II Everything Communicates.
        """
        with self._lock:
            ra = RegisteredAgent(name, handle)
            self._agents[name] = ra
            # Self-declared domain -> this repo (e.g. Oracle "prediction" -> oracle)
            if ra.domain:
                self._domain_to_repo.setdefault(ra.domain, name)
            # Any extra domains the agent advertises (future-proofing)
            for extra in getattr(handle, "domains", []) or []:
                self._domain_to_repo.setdefault(extra, name)
            return ra

    def register_domain(self, domain: str, repository: str) -> None:
        """Register/extend a canonical-domain -> repository mapping at runtime.
        New agents can claim a domain with NO change to Nexus code."""
        with self._lock:
            self._domain_to_repo[domain] = repository

    def unregister(self, name: str) -> None:
        with self._lock:
            self._agents.pop(name, None)

    def get(self, name: str) -> Optional[RegisteredAgent]:
        with self._lock:
            return self._agents.get(name)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def has_domain(self, domain: str) -> bool:
        return self.find_by_domain(domain) is not None

    def find_by_domain(self, domain: str) -> Optional[RegisteredAgent]:
        """
        BUG-P5-01 FIX: Resolve a canonical routing domain to a live agent
        through THREE layers, preferring healthy agents:
          1. Canonical domain -> repository map (handles the classifier vs.
             agent domain-name mismatch, e.g. "trading" -> oracle).
          2. Agent self-declared domain string (legacy behaviour).
          3. Capability match on "<domain>.*" (last-resort discovery).
        Constitutional law: Book II Principle II Everything Communicates;
        Book III Part III Agent Discovery.
        """
        if not domain:
            return None
        with self._lock:
            # Layer 1: canonical domain -> repository name
            repo = self._domain_to_repo.get(domain)
            if repo and repo in self._agents:
                return self._agents[repo]
            # Layer 2: agent self-declared domain (prefer healthy)
            matches = [a for a in self._agents.values() if a.domain == domain]
            healthy = [a for a in matches if a.healthy]
            if healthy:
                return healthy[0]
            if matches:
                return matches[0]
            # Layer 3: capability prefix match ("trading.*", "news.*", ...)
            cap_matches = [a for a in self._agents.values()
                           if any(c.startswith(domain + ".") for c in a.capabilities)]
            if cap_matches:
                healthy_caps = [a for a in cap_matches if a.healthy]
                return (healthy_caps or cap_matches)[0]
            return None

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

    def domain_map(self) -> Dict[str, str]:
        """Expose the live canonical-domain -> repository map (diagnostics)."""
        with self._lock:
            return dict(self._domain_to_repo)

    def health_summary(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._agents)
            healthy = sum(1 for a in self._agents.values() if a.healthy)
            return {"total_agents": total, "healthy": healthy, "unhealthy": total - healthy,
                   "domains_covered": sorted({a.domain for a in self._agents.values() if a.domain}),
                   "domain_map": dict(self._domain_to_repo),
                   "agents": self.all()}
