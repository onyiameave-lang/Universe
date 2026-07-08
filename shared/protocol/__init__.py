"""
shared.protocol
===============
Universal Communication Protocol (UCP): the nervous system of the ecosystem.
(Book II Part II; Book III Part III.)
"""
from __future__ import annotations
from shared.protocol.message import AgentSpec, Message, MessageType, heartbeat
from shared.protocol.bus import MessageBus, get_bus
from shared.protocol.registry import AgentRegistry, get_registry
from shared.protocol.events import (EcosystemEvent, EventCategory, EventPriority,
                                    EventStream, get_event_stream)

__all__ = ["AgentSpec", "Message", "MessageType", "heartbeat", "MessageBus", "get_bus",
           "AgentRegistry", "get_registry", "EcosystemEvent", "EventCategory",
           "EventPriority", "EventStream", "get_event_stream", "UCPAddress", "resolve_address"]


class UCPAddress:
    def __init__(self, address: str):
        self._raw = address
        parts = address.split("://", 1)
        self.repository = parts[0] if parts else ""
        rem = parts[1] if len(parts) > 1 else ""
        pp = rem.split("/", 1)
        self.service = pp[0] if pp else ""
        self.path = pp[1] if len(pp) > 1 else ""

    def __str__(self): return self._raw
    def __eq__(self, o): return str(o) == self._raw
    def __hash__(self): return hash(self._raw)

    @property
    def is_broadcast(self): return self.repository == "*"
    @property
    def is_agent(self): return "agents/" in self._raw


ADDRESSES = {n: UCPAddress(f"{n}://{d}") for n, d in {
    "chronicle": "memory", "oracle": "prediction", "atlas": "research",
    "sentinel": "news", "pulse": "social", "forge": "training",
    "genesis": "creation", "nexus": "coordination", "aegis": "audit"}.items()}


def resolve_address(name: str) -> UCPAddress:
    if "://" in name:
        return UCPAddress(name)
    return ADDRESSES.get(name.lower(), UCPAddress(f"{name}://unknown"))
