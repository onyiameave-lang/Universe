"""
shared.protocol.bus
===================
In-process message bus for UCP. Transport-agnostic. (Book II Part II.)
"""
from __future__ import annotations
import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional
from shared.protocol.message import Message, MessageType

log = logging.getLogger("ecosystem.bus")
Subscriber = Callable[[Message], None]


class MessageBus:
    def __init__(self):
        self._subs: Dict[str, List[Subscriber]] = {}
        self._pending: Dict[str, "queue.Queue[Message]"] = {}
        self._lock = threading.RLock()
        self._history: List[Message] = []

    def subscribe(self, channel: str, handler: Subscriber) -> None:
        with self._lock:
            self._subs.setdefault(channel, []).append(handler)

    def unsubscribe(self, channel: str) -> None:
        with self._lock:
            self._subs.pop(channel, None)

    def send(self, msg: Message) -> None:
        with self._lock:
            self._history.append(msg)
            if len(self._history) > 10000:
                self._history = self._history[-10000:]
            handlers = list(self._subs.get(msg.channel, []))
        for h in handlers:
            try:
                h(msg)
            except Exception as exc:
                log.error("Bus handler failed on %s: %s", msg.channel, exc)
        if msg.type == MessageType.RESPONSE and msg.conversation_id in self._pending:
            self._pending[msg.conversation_id].put(msg)

    def request(self, msg: Message, timeout: float = 2.0) -> Optional[Message]:
        cid = msg.conversation_id or msg.message_id
        msg.conversation_id = cid
        q: "queue.Queue[Message]" = queue.Queue()
        with self._lock:
            self._pending[cid] = q
        self.send(msg)
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            with self._lock:
                self._pending.pop(cid, None)

    def history(self, channel: Optional[str] = None, limit: int = 100) -> List[Message]:
        with self._lock:
            h = self._history if channel is None else [m for m in self._history if m.channel == channel]
            return list(h[-limit:])


_bus: Optional[MessageBus] = None

def get_bus() -> MessageBus:
    global _bus
    if _bus is None:
        _bus = MessageBus()
    return _bus
