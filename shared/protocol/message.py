"""
shared.protocol.message
=======================
UCP message structure and types. (Book II Part II Ch III; Book III Part III.)
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageType(str, Enum):
    REQUEST = "request"; RESPONSE = "response"; EVENT = "event"
    BROADCAST = "broadcast"; HEARTBEAT = "heartbeat"; AUDIT = "audit"; ERROR = "error"


@dataclass
class AgentSpec:
    name: str
    capabilities: List[str] = field(default_factory=list)
    repository: str = ""
    description: str = ""
    version: str = "1.0.0"
    accepts_channels: List[str] = field(default_factory=list)
    mission: Dict[str, Any] = field(default_factory=dict)
    domain: str = ""
    memory_namespace: str = ""
    security_level: str = "standard"
    lifecycle_status: str = "active"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    sender: str = ""
    receiver: str = ""
    task: str = ""
    type: MessageType = MessageType.REQUEST
    context: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    priority: int = 4
    confidence: Optional[float] = None
    evidence: List[str] = field(default_factory=list)
    memory_references: List[str] = field(default_factory=list)
    source_repo: str = ""
    channel: str = ""
    deadline: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def reply(self, sender: str, **kw) -> "Message":
        return Message(sender=sender, receiver=self.sender, task=self.task,
                      type=MessageType.RESPONSE,
                      conversation_id=self.conversation_id or self.message_id, **kw)

    def to_dict(self) -> Dict[str, Any]:
        return {"message_id": self.message_id, "conversation_id": self.conversation_id,
                "sender": self.sender, "receiver": self.receiver, "task": self.task,
                "type": self.type.value if isinstance(self.type, MessageType) else self.type,
                "context": self.context, "timestamp": self.timestamp, "priority": self.priority,
                "confidence": self.confidence, "channel": self.channel}


def heartbeat(sender: str, context: Optional[Dict[str, Any]] = None) -> Message:
    return Message(sender=sender, receiver="*", task="heartbeat",
                  type=MessageType.HEARTBEAT, context=context or {}, channel="ecosystem.health")
