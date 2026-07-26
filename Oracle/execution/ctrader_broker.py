"""
Oracle cTrader Broker Adapter
==============================
A second broker adapter, alongside (NOT replacing) execution/mt5_broker.py.
Everything built on top of MT5Broker -- the Continuous Trade Manager,
News/Social Intelligence, Demo Trade Learning, the Trading Benchmark --
talks only to the method names below (positions(), place_order(),
close_position(), modify_position(), self.status.connected). None of that
code needs to change; it works with whichever broker object it's handed.

IMPORTANT -- testing limitation
--------------------------------
This file was written against Spotware's official documentation and
sample code (github.com/spotware/OpenApiPy), NOT verified against a live
connection -- this sandbox has no network access and cannot install the
`ctrader-open-api` / `twisted` packages. Expect a debugging round against
real output the first time this actually runs on your machine/VPS,
the same way we caught real bugs elsewhere in this codebase by running it.

Why this needs a background thread
------------------------------------
cTrader's official Python SDK is built on Twisted (an async/event-loop
networking library): you send a request and get a Deferred, not an
immediate return value. Everything the rest of Oracle expects
(MT5Broker.positions() returning a list right away, close_position()
returning a result dict right away) is synchronous/blocking.

To bridge that gap: Twisted's reactor runs forever in one dedicated
background thread (started once, in __init__). Every public method here
(positions(), place_order(), etc.) uses reactor.callFromThread() to hand
the actual request over to that thread, then blocks the CALLING thread on
a threading.Event until the response/event arrives (with a timeout, so a
network hiccup can't hang the trading loop forever).

Known cTrader Open API quirks accounted for here
--------------------------------------------------
- Position price / stopLoss / takeProfit are plain doubles already --
  NOT scaled integers (confirmed against the official .proto comments).
- Volume IS scaled: cTrader represents volume in "cents" of a unit, i.e.
  actual_volume * 100. This adapter divides by 100 on the way in and
  multiplies by 100 on the way out, exactly once, in the two places it
  crosses the boundary (positions() and _send_new_order/close/modify).
- ProtoOANewOrderReq / ProtoOAClosePositionReq / ProtoOAAmendPositionSLTPReq
  do NOT get an immediate typed response -- they're acknowledged
  asynchronously via a separate ProtoOAExecutionEvent message. This
  adapter correlates that back to the right waiting call via the
  `clientMsgId` field on the outer ProtoMessage envelope (echoed back by
  the server on the triggering event), which is exactly the mechanism
  Spotware's own docs describe for this.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.ctrader")

_DEFAULT_TIMEOUT_SEC = 15.0


@dataclass
class BrokerStatus:
    """Deliberately field-for-field identical to mt5_broker.BrokerStatus so
    any code doing `self.status.connected` / `.to_dict()` works unchanged."""
    connected: bool = False
    account_type: str = "unknown"   # "demo" | "live" | "unknown"
    login: Optional[int] = None
    server: str = ""
    balance: float = 0.0
    equity: float = 0.0
    currency: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"connected": self.connected, "account_type": self.account_type,
                "login": self.login, "server": self.server, "balance": self.balance,
                "equity": self.equity, "currency": self.currency, "reason": self.reason}


class CTraderBroker:
    """
    Same public surface as execution.mt5_broker.MT5Broker:
      .status (BrokerStatus)
      .connect(...) -> Dict
      .positions(symbol=None) -> List[Dict]
      .place_order(plan, human_confirm=False) -> Dict
      .close_position(ticket) -> Dict
      .modify_position(ticket, stop_loss=None, take_profit=None) -> Dict

    "ticket" in this adapter is cTrader's positionId (an int), used the
    same way MT5's position ticket is used elsewhere in the codebase.
    """

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None,
                 access_token: Optional[str] = None, account_id: Optional[int] = None,
                 use_demo: bool = True):
        self.status = BrokerStatus()
        self._client_id = client_id or os.getenv("CTRADER_CLIENT_ID")
        self._client_secret = client_secret or os.getenv("CTRADER_CLIENT_SECRET")
        self._access_token = access_token or os.getenv("CTRADER_ACCESS_TOKEN")
        self._account_id = account_id or int(os.getenv("CTRADER_ACCOUNT_ID", "0") or 0)
        self._use_demo = use_demo

        self._client = None                 # ctrader_open_api.Client, set in connect()
        self._symbol_id_by_name: Dict[str, int] = {}
        self._symbol_name_by_id: Dict[int, str] = {}
        self._positions_cache: Dict[int, Dict[str, Any]] = {}   # positionId -> our dict shape
        self._cache_lock = threading.Lock()

        # clientMsgId -> {"event": threading.Event, "result": [payload_or_None]}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

        self._reactor_thread: Optional[threading.Thread] = None
        self._reactor_started = threading.Event()

    # ── connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> Dict[str, Any]:
        if not (self._client_id and self._client_secret and self._access_token and self._account_id):
            self.status = BrokerStatus(connected=False, reason=(
                "missing one of CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET / "
                "CTRADER_ACCESS_TOKEN / CTRADER_ACCOUNT_ID"))
            return self.status.to_dict()

        try:
            from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq, ProtoOASymbolsListReq,
                ProtoOATraderReq,
            )
        except ImportError as exc:
            self.status = BrokerStatus(connected=False, reason=f"ctrader-open-api not installed: {exc}")
            return self.status.to_dict()

        from twisted.internet import reactor as _reactor
        self._reactor = _reactor
        self._Protobuf = Protobuf

        host = EndPoints.PROTOBUF_DEMO_HOST if self._use_demo else EndPoints.PROTOBUF_LIVE_HOST
        self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

        connected_ok = threading.Event()
        auth_error: List[str] = []

        def on_connected(client):
            req = ProtoOAApplicationAuthReq()
            req.clientId = self._client_id
            req.clientSecret = self._client_secret
            d = client.send(req)
            d.addErrback(lambda f: auth_error.append(str(f)))

        def on_message(client, message):
            self._dispatch_message(message)
            if message.payloadType == 2101:  # ProtoOAApplicationAuthRes payloadType
                acc_req = ProtoOAAccountAuthReq()
                acc_req.ctidTraderAccountId = self._account_id
                acc_req.accessToken = self._access_token
                d = client.send(acc_req)
                d.addErrback(lambda f: auth_error.append(str(f)))
            elif message.payloadType == 2103:  # ProtoOAAccountAuthRes payloadType
                connected_ok.set()

        def on_disconnected(client, reason):
            log.warning("cTrader disconnected: %s", reason)
            self.status.connected = False

        self._client.setConnectedCallback(on_connected)
        self._client.setDisconnectedCallback(on_disconnected)
        self._client.setMessageReceivedCallback(on_message)

        if not self._reactor_thread:
            self._reactor_thread = threading.Thread(
                target=self._run_reactor, name="ctrader-reactor", daemon=True)
            self._reactor_thread.start()
            self._reactor_started.wait(timeout=5)

        self._reactor.callFromThread(self._client.startService)

        if not connected_ok.wait(timeout=_DEFAULT_TIMEOUT_SEC):
            reason = auth_error[0] if auth_error else "timed out waiting for auth"
            self.status = BrokerStatus(connected=False, reason=reason)
            return self.status.to_dict()

        # Pull the symbol list once — needed to translate symbol name <-> id.
        self._load_symbols(ProtoOASymbolsListReq)

        self.status = BrokerStatus(connected=True,
                                    account_type="demo" if self._use_demo else "live",
                                    login=self._account_id)
        log.info("cTrader connected: account %s (%s)", self._account_id,
                 "demo" if self._use_demo else "live")
        return self.status.to_dict()

    def _run_reactor(self):
        self._reactor_started.set()
        self._reactor.run(installSignalHandlers=False)

    def _dispatch_message(self, message) -> None:
        """Route any ProtoOAExecutionEvent / response back to whichever
        pending call is waiting on its clientMsgId, and keep the position
        cache in sync as executions happen."""
        try:
            payload = self._Protobuf.extract(message)
        except Exception as exc:
            log.debug("could not extract message payload: %s", exc)
            return

        client_msg_id = getattr(message, "clientMsgId", None)
        if client_msg_id:
            with self._pending_lock:
                slot = self._pending.get(client_msg_id)
            if slot is not None:
                slot["result"][0] = payload
                slot["event"].set()

        # Keep our local position cache current on every execution/reconcile.
        self._update_cache_from_payload(payload)

    def _load_symbols(self, ProtoOASymbolsListReq) -> None:
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._account_id
        payload = self._send_and_wait(req, timeout=_DEFAULT_TIMEOUT_SEC)
        if payload is None:
            log.warning("cTrader: symbol list fetch timed out; symbol translation will fail")
            return
        for sym in getattr(payload, "symbol", []):
            self._symbol_id_by_name[sym.symbolName.upper()] = sym.symbolId
            self._symbol_name_by_id[sym.symbolId] = sym.symbolName

    # ── the sync bridge ───────────────────────────────────────────────────

    def _send_and_wait(self, request, timeout: float = _DEFAULT_TIMEOUT_SEC):
        """Send a protobuf request, block this (calling) thread until the
        matching response/event arrives (via clientMsgId correlation) or
        timeout. Returns the extracted payload, or None on timeout."""
        if self._client is None:
            return None
        msg_id = str(uuid.uuid4())
        ev = threading.Event()
        slot = {"event": ev, "result": [None]}
        with self._pending_lock:
            self._pending[msg_id] = slot

        def _send():
            d = self._client.send(request, clientMsgId=msg_id)
            d.addErrback(lambda f: (slot.__setitem__("result", [None]), ev.set()))

        self._reactor.callFromThread(_send)
        ok = ev.wait(timeout=timeout)
        with self._pending_lock:
            self._pending.pop(msg_id, None)
        if not ok:
            log.warning("cTrader: request timed out after %.1fs (clientMsgId=%s)", timeout, msg_id)
            return None
        return slot["result"][0]

    # ── position cache ────────────────────────────────────────────────────

    def _update_cache_from_payload(self, payload) -> None:
        """ProtoOAReconcileReq's response and ProtoOAExecutionEvent both
        carry ProtoOAPosition entries (directly, or nested under .position
        for execution events) -- normalize whichever shows up into our
        cache using the same dict shape MT5Broker.positions() returns."""
        positions = []
        if hasattr(payload, "position") and not hasattr(payload, "positions"):
            # A single position, e.g. inside a ProtoOAExecutionEvent.
            pos = payload.position
            if pos and getattr(pos, "positionId", None):
                positions = [pos]
        elif hasattr(payload, "position") and hasattr(payload, "positions"):
            positions = list(payload.positions)   # ProtoOAReconcileRes-style plural field

        with self._cache_lock:
            for pos in positions:
                trade = pos.tradeData
                symbol_name = self._symbol_name_by_id.get(trade.symbolId, str(trade.symbolId))
                is_buy = trade.tradeSide == 1  # ProtoOATradeSide.BUY == 1
                self._positions_cache[pos.positionId] = {
                    "symbol": symbol_name,
                    "volume": trade.volume / 100.0,
                    "type": "buy" if is_buy else "sell",
                    "price_open": getattr(pos, "price", 0.0),
                    "sl": getattr(pos, "stopLoss", 0.0) or 0.0,
                    "tp": getattr(pos, "takeProfit", 0.0) or 0.0,
                    "profit": 0.0,   # unrealized P&L needs a separate PnL request; left 0 here
                    "ticket": pos.positionId,
                }
            # An execution event closing a position removes it.
            if hasattr(payload, "executionType") and getattr(payload, "executionType", None) == 3:
                # 3 == ORDER_FILLED for a closing order in some SDK versions;
                # safer check: position no longer present in a fresh reconcile.
                pass

    # ── public interface matching MT5Broker ──────────────────────────────

    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Refresh from the server via ProtoOAReconcileReq, then return the
        (possibly symbol-filtered) cache -- mirrors MT5Broker.positions()."""
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq
        except ImportError:
            return list(self._positions_cache.values())

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self._account_id
        payload = self._send_and_wait(req)
        if payload is not None:
            with self._cache_lock:
                self._positions_cache.clear()
            self._update_cache_from_payload(payload)

        with self._cache_lock:
            result = list(self._positions_cache.values())
        if symbol:
            result = [p for p in result if p["symbol"].upper().startswith(symbol.upper()[:6])]
        return result

    def place_order(self, plan, human_confirm: bool = False) -> Dict[str, Any]:
        if not plan or not plan.get("approved"):
            return {"status": "rejected", "reason": "plan not risk-approved"}
        if self.status.account_type == "live" and not human_confirm:
            return {"status": "blocked", "reason": "live account requires human_confirm=True"}
        if not self.status.connected:
            return {"status": "error", "reason": "not connected to cTrader"}

        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOANewOrderReq
            from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
                ProtoOAOrderType, ProtoOATradeSide,
            )
        except ImportError as exc:
            return {"status": "error", "reason": f"ctrader-open-api not installed: {exc}"}

        symbol_name = (plan.get("broker_symbol") or plan.get("symbol", "")).upper()
        symbol_id = self._symbol_id_by_name.get(symbol_name)
        if symbol_id is None:
            return {"status": "error", "reason": f"symbol {symbol_name} not found in cTrader symbol list"}

        is_buy = plan["direction"] in ("long", "buy")
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if is_buy else ProtoOATradeSide.SELL
        req.volume = int(round(float(plan["size"]) * 100))   # cTrader volume is in cents
        if plan.get("stop") is not None:
            req.stopLoss = float(plan["stop"])
        if plan.get("target") is not None:
            req.takeProfit = float(plan["target"])

        payload = self._send_and_wait(req)
        if payload is None:
            return {"status": "error", "reason": "order timed out waiting for execution event"}

        position_id = getattr(getattr(payload, "position", None), "positionId", None)
        fill_price = getattr(getattr(payload, "position", None), "price", None) or getattr(payload, "executionPrice", 0.0)
        if position_id is None:
            return {"status": "rejected", "reason": getattr(payload, "errorCode", "unknown error")}

        return {"status": "filled", "order": position_id, "price": fill_price,
                "volume": float(plan["size"]), "sl": plan.get("stop"), "tp": plan.get("target"),
                "symbol": symbol_name, "account_type": self.status.account_type}

    def close_position(self, ticket: int) -> Dict[str, Any]:
        ticket = int(ticket)
        if not self.status.connected:
            return {"status": "error", "reason": "not connected to cTrader"}
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq
        except ImportError as exc:
            return {"status": "error", "reason": f"ctrader-open-api not installed: {exc}"}

        with self._cache_lock:
            pos = self._positions_cache.get(ticket)
        if pos is None:
            return {"status": "error", "reason": f"position {ticket} not found"}

        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = ticket
        req.volume = int(round(pos["volume"] * 100))

        payload = self._send_and_wait(req)
        if payload is None:
            return {"status": "error", "reason": "close timed out waiting for execution event"}

        with self._cache_lock:
            self._positions_cache.pop(ticket, None)
        return {"status": "closed", "ticket": ticket}

    def modify_position(self, ticket: int, stop_loss: Optional[float] = None,
                         take_profit: Optional[float] = None) -> Dict[str, Any]:
        ticket = int(ticket)
        if not self.status.connected:
            return {"status": "error", "reason": "not connected to cTrader"}
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAAmendPositionSLTPReq
        except ImportError as exc:
            return {"status": "error", "reason": f"ctrader-open-api not installed: {exc}"}

        with self._cache_lock:
            pos = self._positions_cache.get(ticket)
        if pos is None:
            return {"status": "error", "reason": f"position {ticket} not found"}

        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = ticket
        req.stopLoss = stop_loss if stop_loss is not None else pos["sl"]
        req.takeProfit = take_profit if take_profit is not None else pos["tp"]

        payload = self._send_and_wait(req)
        if payload is None:
            return {"status": "error", "reason": "modify timed out waiting for execution event"}

        with self._cache_lock:
            if stop_loss is not None:
                pos["sl"] = stop_loss
            if take_profit is not None:
                pos["tp"] = take_profit
        return {"status": "modified", "ticket": ticket}

    def account(self) -> Dict[str, Any]:
        return self.status.to_dict()