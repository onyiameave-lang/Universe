"""
shared.protocol.registry
========================
Agent discovery and routing registry. (Book III Part III; Book I Article XI.)
"""
from __future__ import annotations
import threading
from typing import Any, Callable, Dict, List, Optional
from shared.protocol.message import AgentSpec, Message


class AgentRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._specs: Dict[str, AgentSpec] = {}
        self._handlers: Dict[str, Callable[[Message], Any]] = {}

    def register(self, spec: AgentSpec, handler: Optional[Callable] = None) -> None:
        with self._lock:
            self._specs[spec.name] = spec
            if handler:
                self._handlers[spec.name] = handler

    def unregister(self, name: str) -> None:
        with self._lock:
            self._specs.pop(name, None)
            self._handlers.pop(name, None)

    def get(self, name: str) -> Optional[AgentSpec]:
        with self._lock:
            return self._specs.get(name)

    def find_by_capability(self, capability: str) -> List[AgentSpec]:
        with self._lock:
            return [s for s in self._specs.values() if capability in s.capabilities]

    def find_by_domain(self, domain: str) -> List[AgentSpec]:
        with self._lock:
            return [s for s in self._specs.values() if s.domain == domain]

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {n: {"repository": s.repository, "domain": s.domain,
                       "capabilities": s.capabilities} for n, s in self._specs.items()}


_registry: Optional[AgentRegistry] = None

def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
