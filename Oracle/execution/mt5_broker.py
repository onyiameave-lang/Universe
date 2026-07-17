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

    # ── FIX-A: symbols() ──────────────────────────────────────────────────────
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

    # ── FIX-1: filling mode auto-detection ───────────────────────────────────
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

    # ── FIX-2: volume clamping / rounding ────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────

    def _map(self, symbol: str) -> str:
        return self.symbol_map.get(symbol.upper(), symbol.upper())

    def _live_allowed(self) -> Optional[str]:
        if not self.status.connected: return "not connected to MT5"
        if self.paper: return "paper trading is ON"
        if self.status.account_type == "real" and not self.allow_live:
            return "REAL-money account detected and ORACLE_ALLOW_LIVE is not true"
        return None

    # ── FIX-B + FIX-1 + FIX-2: place_order() ────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────

    def positions(self) -> List[Dict[str, Any]]:
        if not self.status.connected: return []
        try:
            poss = self._mt5.positions_get() or []
            return [{"symbol": p.symbol, "volume": p.volume, "type": "buy" if p.type == 0 else "sell",
                    "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit,
                    "ticket": p.ticket} for p in poss]
        except Exception: return []

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