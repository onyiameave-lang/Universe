"""
shared.protocol.events
======================
Ecosystem event streaming. Memory-worthy events forward to Chronicle.
(Book II Part II Ch IX-XI.)
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class EventCategory(str, Enum):
    AGENT_LIFECYCLE = "agent.lifecycle"; MEMORY_UPDATE = "memory.update"
    RESEARCH_COMPLETE = "research.complete"; TRAINING_COMPLETE = "training.complete"
    TRADE_SIGNAL = "trade.signal"; NEWS_ALERT = "news.alert"; SOCIAL_TREND = "social.trend"
    SECURITY_ALERT = "security.alert"; OPTIMIZATION_COMPLETE = "optimization.complete"
    ARCHITECTURE_CHANGE = "architecture.change"; CONSTITUTION_UPDATE = "constitution.update"
    HEALTH_REPORT = "health.report"; CAPABILITY_GAP = "capability.gap"
    COLLABORATION_START = "collaboration.start"; COLLABORATION_END = "collaboration.end"


class EventPriority(int, Enum):
    EMERGENCY = 1; CRITICAL = 2; HIGH = 3; NORMAL = 4; BACKGROUND = 5


@dataclass
class EcosystemEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    category: str = ""
    source_repository: str = ""
    source_agent: str = ""
    timestamp: float = field(default_factory=time.time)
    priority: EventPriority = EventPriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    affects_repositories: List[str] = field(default_factory=list)
    memory_worthy: bool = False
    conversation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"event_id": self.event_id, "category": self.category,
                "source_repository": self.source_repository, "source_agent": self.source_agent,
                "timestamp": self.timestamp,
                "priority": self.priority.value if isinstance(self.priority, EventPriority) else self.priority,
                "payload": self.payload, "affects_repositories": self.affects_repositories,
                "memory_worthy": self.memory_worthy}


EventHandler = Callable[[EcosystemEvent], None]


class EventStream:
    def __init__(self):
        self._subs: Dict[str, List[EventHandler]] = {}
        self._global: List[EventHandler] = []
        self._history: List[EcosystemEvent] = []
        self._chronicle: Optional[EventHandler] = None

    def subscribe(self, category: str, handler: EventHandler) -> None:
        self._subs.setdefault(category, []).append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        self._global.append(handler)

    def set_chronicle_handler(self, handler: EventHandler) -> None:
        self._chronicle = handler

    def emit(self, event: EcosystemEvent) -> None:
        self._history.append(event)
        if len(self._history) > 10000:
            self._history = self._history[-10000:]
        for h in self._subs.get(event.category, []) + self._global:
            try:
                h(event)
            except Exception:
                pass  # aegis:allow-silent (handler failures don't stop propagation)
        if event.memory_worthy and self._chronicle:
            try:
                self._chronicle(event)
            except Exception:
                pass  # aegis:allow-silent

    def get_history(self, category=None, source=None, since=None, limit=100) -> List[EcosystemEvent]:
        e = self._history
        if category: e = [x for x in e if x.category == category]
        if source: e = [x for x in e if x.source_repository == source]
        if since: e = [x for x in e if x.timestamp >= since]
        return e[-limit:]


_stream: Optional[EventStream] = None

def get_event_stream() -> EventStream:
    global _stream
    if _stream is None:
        _stream = EventStream()
    return _stream
