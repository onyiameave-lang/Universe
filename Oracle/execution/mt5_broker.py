"""
Oracle.execution.mt5_broker
==========================
Real MetaTrader 5 broker adapter. (Book VI Part I: capital sovereignty stays
with the human; Book III Principle VI: Security by Design.)

v2 changes vs v1:
  FIX-A  Added symbols() method: calls mt5.symbols_get() and returns a list of
         symbol name strings so SymbolMapper in live_trader.py can auto-discover
         the broker's full symbol catalogue on startup.
         Returns [] gracefully when MT5 is not connected.

  FIX-B  place_order() now checks plan["broker_symbol"] first before falling
         back to self._map(plan["symbol"]).  This lets live_trader.py's
         SymbolMapper be the primary mapper (fuzzy matching + env overrides)
         while keeping the hardcoded DEFAULT_SYMBOL_MAP as a last-resort
         fallback for callers that don't pre-translate.

         Priority in place_order():
           1. plan["broker_symbol"]  — set by live_trader.py SymbolMapper
           2. self._map(plan["symbol"])  — DEFAULT_SYMBOL_MAP / identity

v3 changes vs v2:
  FIX-1  Auto-detect filling mode from symbol_info().filling_mode bitmask.
         MT5 encodes supported filling modes as a bitmask:
           bit 0 (value 1) → ORDER_FILLING_FOK supported
           bit 1 (value 2) → ORDER_FILLING_IOC supported
           0               → ORDER_FILLING_RETURN (market execution)
         Previously hardcoded ORDER_FILLING_IOC caused rejections on symbols
         that only support FOK (e.g. USDJPY filling_mode=1, WTI filling_mode=1).
         Now _filling_mode(symbol) queries symbol_info() and picks the right
         constant.  Falls back to FOK if symbol_info() is unavailable.

  FIX-2  Clamp and round volume to symbol constraints before order_send().
         Brokers enforce volume_min / volume_max / volume_step per symbol.
         WTI has volume_min=1.0, volume_step=1.0 — sending 0.01 lots was
         silently rejected.  New _clamp_volume(symbol, raw_vol) queries
         symbol_info() and returns a valid volume, logging any adjustment.
         Falls back to raw_vol if symbol_info() is unavailable.

v4 changes vs v3:
  FIX-3  Added close_position(ticket) and modify_position(ticket, sl, tp) methods.
         These are called by live_trader.py and mt5_demo_trader.py to manage
         existing positions (close on signal reversal, modify SL/TP on re-analysis).
         
         close_position(ticket):
           - Queries positions_get() to find the position by ticket
           - Sends opposite TRADE_ACTION_DEAL order to close it
           - Returns {"status": "closed", "ticket": ticket} on success
           - Returns {"status": "error", "reason": "..."} on failure
           - In paper trading mode, removes position from internal state
         
         modify_position(ticket, stop_loss=None, take_profit=None):
           - Queries positions_get() to find the position by ticket
           - Sends TRADE_ACTION_SLTP order to update SL/TP
           - Returns {"status": "modified", "ticket": ticket} on success
           - Returns {"status": "error", "reason": "..."} on failure
           - In paper trading mode, updates position in internal state
         
         positions(symbol=None):
           - Returns list of open positions (all symbols if symbol=None)
           - Each position is a dict with: symbol, volume, type, price_open,
             sl, tp, profit, ticket
           - Used by live_trader.py's _get_open_position() to check for
             existing positions before opening new ones

v5 changes vs v4 (FIX-DEDUP):
  FIX-5  get_positions_by_symbol(symbol) — dedicated helper that queries MT5
         positions_get(symbol=symbol) directly, bypassing the internal paper
         tracking dict.  Called by both traders at the START of every symbol
         evaluation to get the REAL MT5 state regardless of which script opened
         the position.  If a position is found that isn't in the trader's
         internal tracking dict, the trader adopts it (adds it to its own
         tracking so it gets managed, not duplicated).

  FIX-6  positions() now ALWAYS queries MT5 directly in live mode (was already
         doing this, but now also accepts symbol= kwarg for filtered queries).
         Paper mode still uses internal dict.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.config import get_config
_cfg = get_config()
log = logging.getLogger("oracle.mt5")

DEFAULT_SYMBOL_MAP = {
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY", "AUDUSD": "AUDUSD",
    "USDCAD": "USDCAD", "USDCHF": "USDCHF", "NZDUSD": "NZDUSD", "EURGBP": "EURGBP",
    "EURJPY": "EURJPY", "GBPJPY": "GBPJPY",
    "XAUUSD": "XAUUSD", "USOIL": "XTIUSD",
    "BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD", "SOLUSD": "SOLUSD", "XRPUSD": "XRPUSD",
    "BNBUSD": "BNBUSD", "ADAUSD": "ADAUSD",
    "SPX": "US500", "NASDAQ": "USTEC", "DJI": "US30", "RUT": "US2000", "VIX": "VIX",
    "FTSE": "UK100", "DAX": "DE40", "CAC40": "FRA40", "NIKKEI": "JPN225",
    "HSI": "HK50", "SENSEX": "IN50", "ASX200": "AUS200",
    "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA", "GOOGL": "GOOGL", "AMZN": "AMZN",
    "META": "META", "TSLA": "TSLA", "BRKB": "BRKB", "LLY": "LLY", "V": "V", "JPM": "JPM",
}

@dataclass
class BrokerStatus:
    connected: bool = False
    account_type: str = "unknown"
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

class MT5Broker:
    def __init__(self, symbol_map: Optional[Dict[str, str]] = None):
        self._mt5 = None
        self.status = BrokerStatus()
        self.symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
        self.allow_live = _cfg.oracle_allow_live
        self.paper = _cfg.oracle_paper_trading
        self._paper_positions = {}  # for paper trading mode: {ticket: position_dict}
        self._next_ticket = 1000000  # paper trading ticket counter

    @property
    def available(self) -> bool:
        try:
            import MetaTrader5
            return True
        except Exception:
            return False

    def connect(self, login=None, password=None, server=None) -> Dict[str, Any]:
        if not self.available:
            self.status = BrokerStatus(connected=False, reason="MetaTrader5 package not installed")
            return self.status.to_dict()
        import MetaTrader5 as mt5
        self._mt5 = mt5
        if not login and _cfg.mt5_login:
            try: login = int(_cfg.mt5_login)
            except ValueError:
                self.status = BrokerStatus(connected=False, reason=f"Invalid MT5_LOGIN: '{_cfg.mt5_login}'")
                return self.status.to_dict()
        password = password or _cfg.mt5_password or None
        server = server or _cfg.mt5_server or None
        ok = mt5.initialize(login=login, password=password, server=server) if login else mt5.initialize()
        if not ok:
            self.status = BrokerStatus(connected=False, reason=f"MT5 initialize failed: {mt5.last_error()}")
            return self.status.to_dict()
        info = mt5.account_info()
        if info is None:
            self.status = BrokerStatus(connected=False, reason="connected but no account_info")
            return self.status.to_dict()
        acct_type = {0: "demo", 1: "contest", 2: "real"}.get(getattr(info, "trade_mode", 0), "unknown")
        self.status = BrokerStatus(connected=True, account_type=acct_type,
                                 login=getattr(info, "login", login), server=getattr(info, "server", server or ""),
                                 balance=getattr(info, "balance", 0.0), equity=getattr(info, "equity", 0.0),
                                 currency=getattr(info, "currency", ""), reason="ok")
        log.info("MT5 connected: %s account on %s (balance %.2f %s)", acct_type, self.status.server,
                 self.status.balance, self.status.currency)
        return self.status.to_dict()

    def disconnect(self) -> None:
        if self._mt5:
            try: self._mt5.shutdown()
            except Exception: pass

    # ── FIX-A: symbols() ──────────────────────────────────────────────────────────────────────────────────────────
    def symbols(self) -> List[str]:
        """
        Return a list of all symbol name strings available at the connected MT5 broker.

        Called by live_trader.py's SymbolMapper.build() on startup so it can
        auto-discover the broker's actual symbol names (e.g. "XAUUSDm", "XTIUSD",
        "BTCUSDm") and build a fuzzy canonical→broker map.

        Returns [] gracefully when:
          - MT5 package is not installed
          - MT5 is not connected (self._mt5 is None)
          - mt5.symbols_get() returns None or raises
        """
        if self._mt5 is None:
            log.debug("symbols(): MT5 not connected — returning empty list")
            return []
        try:
            raw = self._mt5.symbols_get()
            if not raw:
                log.warning("symbols(): mt5.symbols_get() returned None or empty")
                return []
            names = [s.name for s in raw if hasattr(s, "name") and s.name]
            log.info("symbols(): found %d broker symbols", len(names))
            return names
        except Exception as exc:
            log.warning("symbols(): mt5.symbols_get() raised %s — returning empty list", exc)
            return []

    # ── FIX-1: filling mode auto-detection ────────────────────────────────────────────────────────────────────────
    def _filling_mode(self, symbol: str):
        """
        Return the correct MT5 ORDER_FILLING_* constant for *symbol*.

        MT5 symbol_info().filling_mode is a bitmask:
          bit 0 (& 1) → FOK  (Fill or Kill)   supported
          bit 1 (& 2) → IOC  (Immediate or Cancel) supported
          0           → RETURN (market execution, no partial fills)

        Priority: FOK > IOC > RETURN.
        Falls back to FOK if symbol_info() is unavailable (safe default for
        most brokers; the order will be rejected with a clear retcode if wrong,
        rather than silently sending the wrong mode).
        """
        mt5 = self._mt5
        if mt5 is None:
            return None   # caller handles None → skip order
        try:
            info = mt5.symbol_info(symbol)
            if info is None:
                log.warning("_filling_mode(%s): symbol_info returned None — defaulting to FOK", symbol)
                return mt5.ORDER_FILLING_FOK
            fm = getattr(info, "filling_mode", 0)
            if fm & 1:
                return mt5.ORDER_FILLING_FOK
            if fm & 2:
                return mt5.ORDER_FILLING_IOC
            # filling_mode == 0 → market execution (RETURN)
            return mt5.ORDER_FILLING_RETURN
        except Exception as exc:
            log.warning("_filling_mode(%s): %s — defaulting to FOK", symbol, exc)
            return mt5.ORDER_FILLING_FOK

    # ── FIX-2: volume clamping / rounding ──────────────────────────────────────────────────────────────────────────
    def _clamp_volume(self, symbol: str, raw_vol: float) -> float:
        """
        Clamp *raw_vol* to [volume_min, volume_max] and round to volume_step
        as reported by symbol_info() for *symbol*.

        WTI example: volume_min=1.0, volume_step=1.0 → 0.01 → 1.0
        EURUSD example: volume_min=0.01, volume_step=0.01 → 0.01 → 0.01 (unchanged)

        Returns raw_vol unchanged if symbol_info() is unavailable.
        """
        mt5 = self._mt5
        if mt5 is None:
            return raw_vol
        try:
            info = mt5.symbol_info(symbol)
            if info is None:
                log.warning("_clamp_volume(%s): symbol_info returned None — using raw vol %.4f",
                            symbol, raw_vol)
                return raw_vol

            vol_min  = getattr(info, "volume_min",  0.01)
            vol_max  = getattr(info, "volume_max",  500.0)
            vol_step = getattr(info, "volume_step", 0.01)

            # round to nearest step
            if vol_step > 0:
                steps = round(raw_vol / vol_step)
                vol = steps * vol_step
            else:
                vol = raw_vol

            # clamp
            vol = max(vol_min, min(vol_max, vol))

            # round to avoid floating-point noise (e.g. 0.9999999 → 1.0)
            decimals = max(0, -int(math.floor(math.log10(vol_step)))) if vol_step < 1 else 0
            vol = round(vol, decimals)

            if abs(vol - raw_vol) > 1e-9:
                log.info("_clamp_volume(%s): adjusted %.4f → %.4f "
                         "(min=%.4f max=%.4f step=%.4f)",
                         symbol, raw_vol, vol, vol_min, vol_max, vol_step)
            return vol

        except Exception as exc:
            log.warning("_clamp_volume(%s): %s — using raw vol %.4f", symbol, exc, raw_vol)
            return raw_vol

    # ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _map(self, symbol: str) -> str:
        return self.symbol_map.get(symbol.upper(), symbol.upper())

    def _live_allowed(self) -> Optional[str]:
        if not self.status.connected: return "not connected to MT5"
        if self.paper: return "paper trading is ON"
        if self.status.account_type == "real" and not self.allow_live:
            return "REAL-money account detected and ORACLE_ALLOW_LIVE is not true"
        return None

    # ── FIX-B + FIX-1 + FIX-2: place_order() ──────────────────────────────────────────────────────────────────────
    def place_order(self, plan, human_confirm=False) -> Dict[str, Any]:
        """
        Submit a market order to MT5.

        Symbol resolution priority (FIX-B):
          1. plan["broker_symbol"]  — pre-translated by live_trader.py SymbolMapper
                                      (fuzzy match + env overrides + fallback table)
          2. self._map(plan["symbol"])  — DEFAULT_SYMBOL_MAP / identity fallback
                                          (for callers that don't pre-translate)

        Filling mode (FIX-1):
          Auto-detected from symbol_info().filling_mode bitmask.
          FOK if bit0 set, IOC if bit1 set, RETURN if 0.

        Volume (FIX-2):
          Clamped to [volume_min, volume_max] and rounded to volume_step
          from symbol_info() before order_send().
        """
        if not plan or not plan.get("approved"):
            return {"status": "rejected", "reason": "plan not risk-approved"}
        blocked = self._live_allowed()
        if blocked: return {"status": "blocked", "reason": blocked, "account": self.status.to_dict()}
        if self.status.account_type == "real" and not human_confirm:
            return {"status": "blocked", "reason": "real account requires human_confirm=True"}
        mt5 = self._mt5

        # FIX-B: prefer pre-translated broker_symbol; fall back to _map()
        if plan.get("broker_symbol"):
            symbol = plan["broker_symbol"]
            log.debug("place_order: using pre-translated broker_symbol=%r", symbol)
        else:
            symbol = self._map(plan["symbol"])
            log.debug("place_order: translated via _map: %r -> %r", plan["symbol"], symbol)

        if not mt5.symbol_select(symbol, True):
            return {"status": "error", "reason": f"symbol {symbol} not available at broker"}
        tick = mt5.symbol_info_tick(symbol)
        if tick is None: return {"status": "error", "reason": f"no tick for {symbol}"}
        is_buy = plan["direction"] in ("long", "buy")
        price = tick.ask if is_buy else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

        # FIX-1: auto-detect filling mode
        filling = self._filling_mode(symbol)

        # FIX-2: clamp/round volume to broker symbol constraints
        raw_vol = float(plan["size"])
        volume  = self._clamp_volume(symbol, raw_vol)

        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                  "volume": volume, "type": order_type, "price": price,
                  "sl": float(plan["stop"]), "tp": float(plan["target"]),
                  "deviation": 20, "magic": 770077, "comment": "OracleAI",
                  "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
        result = mt5.order_send(request)
        if result is None: return {"status": "error", "reason": f"order_send returned None: {mt5.last_error()}"}
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return {"status": "filled" if ok else "rejected", "retcode": result.retcode,
               "account_type": self.status.account_type, "symbol": symbol,
               "volume": volume, "price": getattr(result, "price", price),
               "order": getattr(result, "order", None), "sl": plan["stop"], "tp": plan["target"],
               "comment": getattr(result, "comment", ""),
               "filling_mode": filling, "volume_raw": raw_vol}

    # ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return list of open positions.
        
        If symbol is None, returns all open positions.
        If symbol is provided, returns only positions for that symbol.
        
        Each position dict contains:
          - symbol: str
          - volume: float
          - type: "buy" or "sell"
          - price_open: float
          - sl: float (stop loss)
          - tp: float (take profit)
          - profit: float (current P&L)
          - ticket: int (position ticket number)
        
        In paper trading mode, returns positions from internal _paper_positions dict.
        In live mode, queries MT5 via positions_get().
        """
        if self.paper:
            # Paper trading mode: return from internal state
            result = list(self._paper_positions.values())
            if symbol:
                result = [p for p in result if p["symbol"].upper().startswith(symbol.upper()[:6])]
            return result
        
        if not self.status.connected:
            return []
        
        try:
            poss = self._mt5.positions_get() or []
            result = []
            for p in poss:
                pos_dict = {
                    "symbol": p.symbol,
                    "volume": p.volume,
                    "type": "buy" if p.type == 0 else "sell",
                    "price_open": p.price_open,
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": p.profit,
                    "ticket": p.ticket
                }
                result.append(pos_dict)
            
            if symbol:
                result = [p for p in result if p["symbol"].upper().startswith(symbol.upper()[:6])]
            
            return result
        except Exception as exc:
            log.warning("positions(): %s", exc)
            return []

    # ── FIX-3: close_position() ────────────────────────────────────────────────────────────────────
    def close_position(self, ticket: int) -> Dict[str, Any]:
        """
        Close an open position by ticket number.

        FIX-4 changes vs FIX-3:
          - ticket is always coerced to plain Python int (handles numpy int64 from MT5)
          - _ensure_connected() is called before any MT5 query
          - 3-tier position lookup:
              Tier 1: positions_get(ticket=int(ticket))
              Tier 2: positions_get() all → manual search by int comparison
              Tier 3: positions_get(symbol=...) if symbol hint available
          - DEBUG log of raw positions_get() response so mismatches are visible

        In paper trading mode:
          - Removes position from internal _paper_positions dict
        """
        # Always work with a plain Python int — MT5 returns numpy int64 tickets
        ticket = int(ticket)

        if self.paper:
            if ticket in self._paper_positions:
                del self._paper_positions[ticket]
                log.info("close_position(%d): removed from paper trading state", ticket)
                return {"status": "closed", "ticket": ticket}
            else:
                log.warning("close_position(%d): position not found in paper trading state", ticket)
                return {"status": "error", "reason": f"position {ticket} not found"}

        # Reconnect if needed
        self._ensure_connected()

        if not self.status.connected or self._mt5 is None:
            return {"status": "error", "reason": "not connected to MT5"}

        mt5 = self._mt5
        try:
            pos = self._find_position_by_ticket(mt5, ticket)

            if pos is None:
                log.warning("close_position(%d): position not found after 3-tier lookup", ticket)
                return {"status": "error", "reason": f"position {ticket} not found"}

            # Get current tick for close price
            if not mt5.symbol_select(pos.symbol, True):
                return {"status": "error", "reason": f"symbol {pos.symbol} not available"}

            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                return {"status": "error", "reason": f"no tick for {pos.symbol}"}

            # Determine close order type (opposite of position type)
            is_buy = int(pos.type) == 0  # 0 = buy, 1 = sell
            close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if is_buy else tick.ask

            # Auto-detect filling mode
            filling = self._filling_mode(pos.symbol)

            # Send close order
            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     ticket,
                "price":        close_price,
                "deviation":    20,
                "magic":        770077,
                "comment":      "OracleAI-close",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }

            result = mt5.order_send(request)
            if result is None:
                return {"status": "error", "reason": f"order_send returned None: {mt5.last_error()}"}

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("close_position(%d): closed successfully", ticket)
                return {"status": "closed", "ticket": ticket}
            else:
                log.warning("close_position(%d): order rejected with retcode %d", ticket, result.retcode)
                return {"status": "error", "reason": f"order rejected: retcode {result.retcode}"}

        except Exception as exc:
            log.error("close_position(%d): %s", ticket, exc)
            return {"status": "error", "reason": str(exc)}

    # ── FIX-3: modify_position() ───────────────────────────────────────────────────────────────────
    def modify_position(self, ticket: int, stop_loss: Optional[float] = None,
                       take_profit: Optional[float] = None) -> Dict[str, Any]:
        """
        Modify SL/TP of an open position.

        FIX-4 changes vs FIX-3:
          - ticket coerced to plain Python int
          - _ensure_connected() before any MT5 query
          - 3-tier position lookup (same as close_position)
          - DEBUG log of raw positions_get() response
        """
        ticket = int(ticket)

        if self.paper:
            if ticket in self._paper_positions:
                pos = self._paper_positions[ticket]
                if stop_loss is not None:
                    pos["sl"] = stop_loss
                if take_profit is not None:
                    pos["tp"] = take_profit
                log.info("modify_position(%d): updated in paper trading state (sl=%.5f tp=%.5f)",
                         ticket, pos.get("sl", 0), pos.get("tp", 0))
                return {"status": "modified", "ticket": ticket}
            else:
                log.warning("modify_position(%d): position not found in paper trading state", ticket)
                return {"status": "error", "reason": f"position {ticket} not found"}

        # Reconnect if needed
        self._ensure_connected()

        if not self.status.connected or self._mt5 is None:
            return {"status": "error", "reason": "not connected to MT5"}

        mt5 = self._mt5
        try:
            pos = self._find_position_by_ticket(mt5, ticket)

            if pos is None:
                log.warning("modify_position(%d): position not found after 3-tier lookup", ticket)
                return {"status": "error", "reason": f"position {ticket} not found"}

            # Use current SL/TP if not provided
            new_sl = stop_loss  if stop_loss  is not None else pos.sl
            new_tp = take_profit if take_profit is not None else pos.tp

            # Send modify order
            request = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   pos.symbol,
                "position": ticket,
                "sl":       new_sl,
                "tp":       new_tp,
                "magic":    770077,
                "comment":  "OracleAI-modify",
            }

            result = mt5.order_send(request)
            if result is None:
                return {"status": "error", "reason": f"order_send returned None: {mt5.last_error()}"}

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("modify_position(%d): modified successfully (sl=%.5f tp=%.5f)",
                         ticket, new_sl, new_tp)
                return {"status": "modified", "ticket": ticket}
            else:
                log.warning("modify_position(%d): order rejected with retcode %d", ticket, result.retcode)
                return {"status": "error", "reason": f"order rejected: retcode {result.retcode}"}

        except Exception as exc:
            log.error("modify_position(%d): %s", ticket, exc)
            return {"status": "error", "reason": str(exc)}

    # ── FIX-4: _find_position_by_ticket() — 3-tier lookup ─────────────────────────────────────────
    def _find_position_by_ticket(self, mt5, ticket: int):
        """
        Robustly find an open MT5 position by ticket number.

        Tier 1: positions_get(ticket=ticket)  — fastest, but may fail on some MT5 builds
        Tier 2: positions_get() all positions → manual int() comparison (handles numpy int64)
        Tier 3: (fallback) same as Tier 2 but after a fresh symbol_select scan

        Always coerces ticket to plain Python int before comparison.
        Logs DEBUG output of raw MT5 response so mismatches are visible.
        """
        ticket = int(ticket)

        # ── Tier 1: direct ticket filter ──────────────────────────────────────────
        try:
            tier1 = mt5.positions_get(ticket=ticket)
            log.debug("_find_position_by_ticket(%d) tier1 raw: %r", ticket, tier1)
            if tier1:
                for p in tier1:
                    if int(p.ticket) == ticket:
                        log.debug("_find_position_by_ticket(%d): found via tier1", ticket)
                        return p
        except Exception as e:
            log.debug("_find_position_by_ticket(%d) tier1 exception: %s", ticket, e)

        # ── Tier 2: fetch all positions, search manually ───────────────────────────
        try:
            all_pos = mt5.positions_get() or []
            log.debug("_find_position_by_ticket(%d) tier2 all_pos count=%d tickets=%s",
                      ticket, len(all_pos), [int(p.ticket) for p in all_pos])
            for p in all_pos:
                if int(p.ticket) == ticket:
                    log.debug("_find_position_by_ticket(%d): found via tier2 manual scan", ticket)
                    return p
        except Exception as e:
            log.debug("_find_position_by_ticket(%d) tier2 exception: %s", ticket, e)

        # ── Tier 3: re-initialize symbol list then retry all positions ─────────────
        try:
            log.debug("_find_position_by_ticket(%d): tier3 — refreshing symbols then retrying", ticket)
            syms = mt5.symbols_get() or []
            for s in syms:
                mt5.symbol_select(s.name, True)
            all_pos2 = mt5.positions_get() or []
            log.debug("_find_position_by_ticket(%d) tier3 all_pos2 count=%d tickets=%s",
                      ticket, len(all_pos2), [int(p.ticket) for p in all_pos2])
            for p in all_pos2:
                if int(p.ticket) == ticket:
                    log.debug("_find_position_by_ticket(%d): found via tier3", ticket)
                    return p
        except Exception as e:
            log.debug("_find_position_by_ticket(%d) tier3 exception: %s", ticket, e)

        log.warning("_find_position_by_ticket(%d): not found in any tier", ticket)
        return None

    # ── FIX-4: _ensure_connected() ────────────────────────────────────────────────────────────────
    def _ensure_connected(self) -> bool:
        """
        Check MT5 connection health and attempt reconnect if needed.
        Returns True if connected (or reconnected), False if still disconnected.
        """
        if self.paper:
            return True

        if self._mt5 is None:
            return False

        # Quick health check: account_info() returns None if connection dropped
        try:
            info = self._mt5.account_info()
            if info is not None:
                return True
        except Exception:
            pass

        # Connection dropped — attempt reconnect
        log.warning("_ensure_connected(): MT5 connection lost, attempting reconnect…")
        result = self.connect()
        if self.status.connected:
            log.info("_ensure_connected(): reconnected successfully")
            return True
        else:
            log.error("_ensure_connected(): reconnect failed: %s", result.get("reason", "unknown"))
            return False

    # ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

    # ── FIX-5: get_positions_by_symbol() ─────────────────────────────────────
    def get_positions_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """
        Query MT5 directly for all open positions on *symbol*.

        Unlike positions(symbol=...) which uses the internal paper dict in paper
        mode, this method ALWAYS queries MT5 directly in live mode.  This is the
        correct method to call before deciding whether to open a new position —
        it reflects the REAL broker state regardless of which script opened the
        position.

        In paper trading mode, falls back to the internal _paper_positions dict
        (since there is no real MT5 to query).

        Returns a list of position dicts (same format as positions()).
        Returns [] if not connected, symbol not found, or no positions.
        """
        if self.paper:
            # Paper mode: use internal dict filtered by symbol prefix
            prefix = symbol.upper()[:6]
            return [p for p in self._paper_positions.values()
                    if p.get("symbol", "").upper().startswith(prefix)]

        if not self.status.connected or self._mt5 is None:
            log.debug("get_positions_by_symbol(%s): not connected", symbol)
            return []

        mt5 = self._mt5
        try:
            # Ensure symbol is selected (visible) before querying
            mt5.symbol_select(symbol, True)

            # Try direct symbol filter first (fastest)
            raw = mt5.positions_get(symbol=symbol)
            log.debug("get_positions_by_symbol(%s): positions_get(symbol=) returned %r",
                      symbol, raw)

            if raw is None:
                # Fallback: fetch all and filter manually (handles broker symbol
                # variants like BTCUSDm vs BTCUSD)
                all_pos = mt5.positions_get() or []
                prefix = symbol.upper()[:6]
                raw = [p for p in all_pos
                       if p.symbol.upper().startswith(prefix)]
                log.debug("get_positions_by_symbol(%s): fallback all-positions filter "
                          "found %d matches", symbol, len(raw))

            result = []
            for p in (raw or []):
                result.append({
                    "symbol":      p.symbol,
                    "volume":      p.volume,
                    "type":        "buy" if int(p.type) == 0 else "sell",
                    "price_open":  p.price_open,
                    "sl":          p.sl,
                    "tp":          p.tp,
                    "profit":      p.profit,
                    "ticket":      int(p.ticket),   # always plain Python int
                })
            return result

        except Exception as exc:
            log.warning("get_positions_by_symbol(%s): %s", symbol, exc)
            return []

    # ── FIX-5: adopt_position() ───────────────────────────────────────────────
    def adopt_position(self, pos_dict: Dict[str, Any]) -> None:
        """
        Register an externally-opened position in the paper trading state so
        it gets managed (not duplicated) by this trader instance.

        Only meaningful in paper trading mode.  In live mode this is a no-op
        because positions() always queries MT5 directly.
        """
        if not self.paper:
            return
        ticket = int(pos_dict.get("ticket", 0))
        if ticket and ticket not in self._paper_positions:
            self._paper_positions[ticket] = dict(pos_dict)
            log.info("adopt_position(%d): adopted externally-opened position for %s",
                     ticket, pos_dict.get("symbol", "?"))

    # ── end FIX-5 ─────────────────────────────────────────────────────────────

    def close_all(self) -> Dict[str, Any]:
        if not self.status.connected: return {"status": "error", "reason": "not connected"}
        mt5 = self._mt5
        closed, errors = [], []
        for p in (mt5.positions_get() or []):
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None: errors.append(p.symbol); continue
            is_buy = p.type == 0
            # FIX-1: also auto-detect filling mode for close orders
            filling = self._filling_mode(p.symbol)
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                  "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                  "position": p.ticket, "price": tick.bid if is_buy else tick.ask,
                  "deviation": 20, "magic": 770077, "comment": "OracleAI-close",
                  "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE: closed.append(p.symbol)
            else: errors.append(p.symbol)
        return {"status": "complete", "closed": closed, "errors": errors}

    def account(self) -> Dict[str, Any]:
        if self.status.connected and self._mt5:
            info = self._mt5.account_info()
            if info:
                self.status.balance = getattr(info, "balance", self.status.balance)
                self.status.equity = getattr(info, "equity", self.status.equity)
        return self.status.to_dict()