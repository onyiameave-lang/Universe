"""
Oracle.execution.chronicle_position_log
========================================
Shared helper that lets live_trader.py and mt5_demo_trader.py use Chronicle
as a cross-script source of truth for open positions.

Why this exists
---------------
When both scripts run simultaneously they each maintain their own in-memory
position tracking dict (_open_context).  If live_trader opens USDJPY, the
demo_trader doesn't know about it and may open another USDJPY.

This module provides two things:
  1. ChroniclePositionLog — writes position events (opened/closed/modified)
     to Chronicle so the other script can see them.
  2. query_open_positions(symbol) — reads Chronicle's recent events for a
     symbol and returns any positions that were opened but not yet closed.

Chronicle integration
---------------------
Chronicle is the ecosystem's shared memory agent.  We communicate with it
via its .act() method (if the agent object is available) OR via the REST API
at http://localhost:8000/agents/chronicle/chat (if running inside the
ecosystem).

The event format stored in Chronicle:
  {
    "event":     "position_opened" | "position_closed" | "position_modified",
    "symbol":    "USDJPY",          # Oracle canonical name
    "broker_sym":"USDJPY",          # broker symbol name
    "ticket":    12345,             # MT5 ticket number
    "direction": "buy" | "sell",
    "volume":    0.01,
    "price":     149.50,
    "sl":        149.00,
    "tp":        150.00,
    "trader_id": "live_trader_1",   # TRADER_ID env var
    "ts":        1721462400.0,      # Unix timestamp
  }

Usage
-----
    from execution.chronicle_position_log import ChroniclePositionLog

    # In trader __init__:
    self._pos_log = ChroniclePositionLog(
        chronicle_agent=self.chronicle,   # may be None
        trader_id=os.getenv("TRADER_ID", "live_trader"),
    )

    # Before opening a new position:
    if self._pos_log.has_open_position(symbol, broker_sym):
        # Another script already has this open — skip
        ...

    # After opening:
    self._pos_log.log_opened(symbol, broker_sym, ticket, direction, volume, price, sl, tp)

    # After closing:
    self._pos_log.log_closed(symbol, broker_sym, ticket)

    # After modifying:
    self._pos_log.log_modified(symbol, broker_sym, ticket, sl, tp)
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.pos_log")

# How far back (seconds) to look in Chronicle for position events.
# 24 hours covers any reasonable trading session.
_LOOKBACK_SEC: float = float(os.getenv("CHRONICLE_LOOKBACK_SEC", str(24 * 3600)))

# Chronicle REST API base URL (used when chronicle_agent is None)
_CHRONICLE_API_URL: str = os.getenv(
    "CHRONICLE_API_URL",
    "http://localhost:8000/agents/chronicle/chat"
)

# TRADER_ID tags every event so you can see which script created it
_DEFAULT_TRADER_ID: str = os.getenv("TRADER_ID", "trader")


class ChroniclePositionLog:
    """
    Writes and reads position events via Chronicle.

    Thread-safe: all methods are stateless (no shared mutable state beyond
    the chronicle_agent reference which is read-only after __init__).
    """

    def __init__(self,
                 chronicle_agent=None,
                 trader_id: str = _DEFAULT_TRADER_ID):
        """
        chronicle_agent: a loaded ChronicleAgent instance (may be None).
                         If None, falls back to REST API calls.
        trader_id:       string tag added to every event (e.g. "live_trader_1").
        """
        self._chronicle = chronicle_agent
        self._trader_id = trader_id or _DEFAULT_TRADER_ID
        log.info("ChroniclePositionLog: trader_id=%r  chronicle_agent=%s  api=%s",
                 self._trader_id,
                 "available" if chronicle_agent else "None (REST fallback)",
                 _CHRONICLE_API_URL)

    # ── write events ──────────────────────────────────────────────────────────

    def log_opened(self, symbol: str, broker_sym: str, ticket: int,
                   direction: str, volume: float,
                   price: float = 0.0, sl: float = 0.0, tp: float = 0.0) -> None:
        """Record that a position was opened."""
        self._write({
            "event":      "position_opened",
            "symbol":     symbol,
            "broker_sym": broker_sym,
            "ticket":     int(ticket),
            "direction":  direction,
            "volume":     volume,
            "price":      price,
            "sl":         sl,
            "tp":         tp,
        })

    def log_closed(self, symbol: str, broker_sym: str, ticket: int,
                   reason: str = "") -> None:
        """Record that a position was closed."""
        self._write({
            "event":      "position_closed",
            "symbol":     symbol,
            "broker_sym": broker_sym,
            "ticket":     int(ticket),
            "reason":     reason,
        })

    def log_modified(self, symbol: str, broker_sym: str, ticket: int,
                     sl: float = 0.0, tp: float = 0.0) -> None:
        """Record that a position's SL/TP was modified."""
        self._write({
            "event":      "position_modified",
            "symbol":     symbol,
            "broker_sym": broker_sym,
            "ticket":     int(ticket),
            "sl":         sl,
            "tp":         tp,
        })

    # ── read / query ──────────────────────────────────────────────────────────

    def has_open_position(self, symbol: str, broker_sym: str = "") -> bool:
        """
        Return True if Chronicle has a recent 'position_opened' event for
        *symbol* that has NOT been followed by a 'position_closed' event.

        This is the cross-script duplicate-prevention check.  Call this BEFORE
        opening a new position.

        Returns False (safe default) if Chronicle is unavailable or returns
        an unexpected response — we never block a trade due to a Chronicle
        query failure.
        """
        try:
            events = self._query_recent_events(symbol)
            if not events:
                return False

            # Walk events newest-first; if we see a 'closed' before an 'opened'
            # for the same ticket, the position is closed.
            open_tickets: set = set()
            closed_tickets: set = set()

            for ev in sorted(events, key=lambda e: e.get("ts", 0)):
                evt  = ev.get("event", "")
                tick = ev.get("ticket")
                if evt == "position_opened" and tick is not None:
                    open_tickets.add(int(tick))
                elif evt == "position_closed" and tick is not None:
                    closed_tickets.add(int(tick))

            still_open = open_tickets - closed_tickets
            if still_open:
                log.info("has_open_position(%s): Chronicle shows %d open ticket(s): %s",
                         symbol, len(still_open), still_open)
                return True
            return False

        except Exception as exc:
            log.warning("has_open_position(%s): Chronicle query failed (%s) — "
                        "assuming no open position (safe default)", symbol, exc)
            return False

    def get_open_tickets(self, symbol: str) -> List[int]:
        """
        Return a list of ticket numbers that Chronicle believes are still open
        for *symbol*.  Returns [] on any error.
        """
        try:
            events = self._query_recent_events(symbol)
            open_tickets: set = set()
            closed_tickets: set = set()
            for ev in events:
                evt  = ev.get("event", "")
                tick = ev.get("ticket")
                if evt == "position_opened" and tick is not None:
                    open_tickets.add(int(tick))
                elif evt == "position_closed" and tick is not None:
                    closed_tickets.add(int(tick))
            return list(open_tickets - closed_tickets)
        except Exception as exc:
            log.warning("get_open_tickets(%s): %s", symbol, exc)
            return []

    # ── internal helpers ──────────────────────────────────────────────────────

    def _write(self, payload: Dict[str, Any]) -> None:
        """
        Write a position event to Chronicle.

        Adds trader_id and timestamp, then sends via:
          1. chronicle_agent.act("memory.store", ...) if agent is available
          2. REST POST to /agents/chronicle/chat as fallback
          3. Logs a warning and continues if both fail (never blocks trading)
        """
        payload = dict(payload)
        payload["trader_id"] = self._trader_id
        payload["ts"]        = time.time()

        # Build a human-readable summary for Chronicle's memory store
        summary = (
            f"[POSITION EVENT] {payload['event'].upper()} "
            f"symbol={payload.get('symbol','?')} "
            f"ticket={payload.get('ticket','?')} "
            f"trader={self._trader_id} "
            f"ts={payload['ts']:.0f}"
        )
        if payload.get("direction"):
            summary += f" direction={payload['direction']}"
        if payload.get("reason"):
            summary += f" reason={payload['reason']}"

        # Attempt 1: direct agent call
        if self._chronicle is not None:
            try:
                self._chronicle.act("memory.store", {
                    "key":     f"position_event:{payload.get('symbol','?')}:{payload['ts']:.0f}",
                    "value":   json.dumps(payload),
                    "summary": summary,
                    "_sender": self._trader_id,
                })
                log.debug("_write: stored via chronicle_agent: %s", summary)
                return
            except Exception as exc:
                log.debug("_write: chronicle_agent.act failed (%s) — trying REST", exc)

        # Attempt 2: REST API
        try:
            body = json.dumps({
                "message": f"STORE_POSITION_EVENT {json.dumps(payload)}",
                "_sender": self._trader_id,
            }).encode()
            req = urllib.request.Request(
                _CHRONICLE_API_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            log.debug("_write: stored via REST: %s", summary)
            return
        except Exception as exc:
            log.debug("_write: REST fallback also failed (%s) — event not stored", exc)

        # Both failed — log at WARNING but never raise (never block trading)
        log.warning("_write: could not store position event to Chronicle: %s", summary)

    def _query_recent_events(self, symbol: str) -> List[Dict[str, Any]]:
        """
        Query Chronicle for recent position events for *symbol*.

        Returns a list of event dicts.  Returns [] on any error.

        Tries:
          1. chronicle_agent.act("memory.query", ...) if agent is available
          2. REST POST to /agents/chronicle/chat as fallback
        """
        cutoff = time.time() - _LOOKBACK_SEC
        query  = f"position_event {symbol} trader"

        # Attempt 1: direct agent call
        if self._chronicle is not None:
            try:
                result = self._chronicle.act("memory.query", {
                    "query":   query,
                    "limit":   50,
                    "_sender": self._trader_id,
                })
                return self._parse_events(result, symbol, cutoff)
            except Exception as exc:
                log.debug("_query_recent_events(%s): chronicle_agent.act failed (%s) "
                          "— trying REST", symbol, exc)

        # Attempt 2: REST API
        try:
            body = json.dumps({
                "message": f"QUERY_POSITION_EVENTS symbol={symbol} limit=50",
                "_sender": self._trader_id,
            }).encode()
            req = urllib.request.Request(
                _CHRONICLE_API_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read().decode())
            return self._parse_events(raw, symbol, cutoff)
        except Exception as exc:
            log.debug("_query_recent_events(%s): REST fallback failed (%s)", symbol, exc)

        return []

    @staticmethod
    def _parse_events(raw: Any, symbol: str,
                      cutoff: float) -> List[Dict[str, Any]]:
        """
        Extract position event dicts from a Chronicle response.

        Chronicle may return:
          - a list of memory entries (each with a "value" key containing JSON)
          - a dict with a "memories" or "results" key
          - a plain string (the agent's text reply)

        We try to extract JSON blobs from whatever we get.
        """
        events: List[Dict[str, Any]] = []
        symbol_up = symbol.upper()

        def _try_parse(text: str) -> None:
            """Try to parse a JSON blob from text and add to events if valid."""
            text = text.strip()
            if not text.startswith("{"):
                return
            try:
                ev = json.loads(text)
                if not isinstance(ev, dict):
                    return
                # Must be a position event for this symbol
                if ev.get("event", "").startswith("position_") and \
                        ev.get("symbol", "").upper() == symbol_up:
                    ts = ev.get("ts", 0)
                    if ts >= cutoff:
                        events.append(ev)
            except (json.JSONDecodeError, ValueError):
                pass

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _try_parse(item.get("value", ""))
                    _try_parse(item.get("content", ""))
                elif isinstance(item, str):
                    _try_parse(item)
        elif isinstance(raw, dict):
            for key in ("memories", "results", "items", "data"):
                sub = raw.get(key)
                if isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, dict):
                            _try_parse(item.get("value", ""))
                        elif isinstance(item, str):
                            _try_parse(item)
            # Also try the top-level "text" or "response" key
            _try_parse(raw.get("text", ""))
            _try_parse(raw.get("response", ""))
        elif isinstance(raw, str):
            # Chronicle returned a plain text reply — scan for JSON blobs
            for line in raw.splitlines():
                _try_parse(line)

        return events
