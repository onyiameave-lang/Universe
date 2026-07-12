"""
Oracle.execution.mt5_broker
==========================
Real MetaTrader 5 broker adapter. (Book VI Part I: capital sovereignty stays
with the human; Book III Principle VI: Security by Design.)

This connects Oracle to a LIVE (or demo) MT5 account so it can place real orders.
Because this touches real money, every safety rail from the ecosystem stays on:

  * PAPER BY DEFAULT     ORACLE_PAPER_TRADING must be explicitly set to false.
  * DEMO DETECTION       the adapter reports whether the account is DEMO or REAL
                         and refuses REAL-money orders unless ORACLE_ALLOW_LIVE=true.
  * RISK GATE UPSTREAM   only risk-approved plans (from RiskManager) are accepted;
                         this adapter never sizes or decides, it only executes.
  * BROKER-SIDE STOPS    every market order is submitted WITH its stop-loss and
                         take-profit attached, so protection exists at the broker
                         even if Oracle disconnects.
  * KILL SWITCH          close_all() flattens every position immediately.
  * HONEST DEGRADATION   if MetaTrader5 isn't installed or login fails, it says so
                         and does nothing; it never simulates a fill as if real.

Credentials come from the environment (.env): MT5_LOGIN, MT5_PASSWORD, MT5_SERVER.
Never hardcode credentials (Engineering Rule: no hardcoded secrets).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.config import get_config
_cfg = get_config()
log = logging.getLogger("oracle.mt5")

# symbol mapping: ecosystem notation -> common MT5 broker symbols (override via env)
DEFAULT_SYMBOL_MAP = {
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY", "XAUUSD": "XAUUSD",
    "BTCUSD": "BTCUSD", "SPX": "US500", "NASDAQ": "USTEC", "USOIL": "XTIUSD",
}


@dataclass
class BrokerStatus:
    connected: bool = False
    account_type: str = "unknown"   # demo | real | unknown
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
    """Thin, safety-gated wrapper over the MetaTrader5 Python API."""

    def __init__(self, symbol_map: Optional[Dict[str, str]] = None):
        self._mt5 = None
        self.status = BrokerStatus()
        self.symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
        self.allow_live = _cfg.oracle_allow_live
        self.paper = _cfg.oracle_paper_trading

    @property
    def available(self) -> bool:
        try:
            import MetaTrader5  # noqa
            return True
        except Exception:
            return False

    # ---- connection ----

    def connect(self, login: Optional[int] = None, password: Optional[str] = None,
                server: Optional[str] = None) -> Dict[str, Any]:
        """
        Connect to MT5 using env creds (or explicit args). Reports demo vs real.
        Does NOT enable real-money trading by itself (see place_order gate).
        """
        if not self.available:
            self.status = BrokerStatus(connected=False, reason="MetaTrader5 package not installed "
                                     "(pip install MetaTrader5; Windows/Wine only)")
            return self.status.to_dict()
        import MetaTrader5 as mt5
        self._mt5 = mt5

        login = login or (int(_cfg.mt5_login) if _cfg.mt5_login else None)
        password = password or _cfg.mt5_password or None
        server = server or _cfg.mt5_server or None

        ok = mt5.initialize(login=login, password=password, server=server) if login else mt5.initialize()
        if not ok:
            err = mt5.last_error()
            self.status = BrokerStatus(connected=False, reason=f"MT5 initialize failed: {err}")
            return self.status.to_dict()

        info = mt5.account_info()
        if info is None:
            self.status = BrokerStatus(connected=False, reason="connected but no account_info "
                                     "(check login/password/server)")
            return self.status.to_dict()

        # trade_mode: 0 = demo, 1 = contest, 2 = real (per MT5 constants)
        acct_type = {0: "demo", 1: "contest", 2: "real"}.get(getattr(info, "trade_mode", 0), "unknown")
        self.status = BrokerStatus(connected=True, account_type=acct_type,
                                 login=getattr(info, "login", login), server=getattr(info, "server", server or ""),
                                 balance=getattr(info, "balance", 0.0), equity=getattr(info, "equity", 0.0),
                                 currency=getattr(info, "currency", ""), reason="ok")
        log.info("MT5 connected: %s account on %s (balance %.2f %s)",
                 acct_type, self.status.server, self.status.balance, self.status.currency)
        return self.status.to_dict()

    def disconnect(self) -> None:
        if self._mt5:
            try:
                self._mt5.shutdown()
            except Exception:
                pass  # aegis:allow-silent

    # ---- the execution gate ----

    def _map(self, symbol: str) -> str:
        return self.symbol_map.get(symbol.upper(), symbol.upper())

    def _live_allowed(self) -> Optional[str]:
        """Return a refusal reason if live trading is not permitted, else None."""
        if not self.status.connected:
            return "not connected to MT5"
        if self.paper:
            return "paper trading is ON (set ORACLE_PAPER_TRADING=false to trade live)"
        if self.status.account_type == "real" and not self.allow_live:
            return ("REAL-money account detected and ORACLE_ALLOW_LIVE is not true; "
                   "refusing to trade real capital without explicit opt-in")
        return None

    def place_order(self, plan: Dict[str, Any], human_confirm: bool = False) -> Dict[str, Any]:
        """
        Execute a RISK-APPROVED plan on MT5. The plan must come from RiskManager
        (approved=True) and carry symbol/direction/size/stop/target.
        Requires human_confirm=True for a real account (capital sovereignty).
        """
        if not plan or not plan.get("approved"):
            return {"status": "rejected", "reason": "plan not risk-approved"}

        blocked = self._live_allowed()
        if blocked:
            return {"status": "blocked", "reason": blocked, "account": self.status.to_dict()}
        if self.status.account_type == "real" and not human_confirm:
            return {"status": "blocked",
                   "reason": "real account requires human_confirm=True for each order (Book VI)"}

        mt5 = self._mt5
        symbol = self._map(plan["symbol"])
        # ensure symbol is selected in Market Watch
        if not mt5.symbol_select(symbol, True):
            return {"status": "error", "reason": f"symbol {symbol} not available at broker"}
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"status": "error", "reason": f"no tick for {symbol}"}

        is_buy = plan["direction"] in ("long", "buy")
        price = tick.ask if is_buy else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": float(plan["size"]), "type": order_type, "price": price,
            "sl": float(plan["stop"]), "tp": float(plan["target"]),
            "deviation": 20, "magic": 770077, "comment": "OracleAI",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return {"status": "error", "reason": f"order_send returned None: {mt5.last_error()}"}
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return {"status": "filled" if ok else "rejected", "retcode": result.retcode,
               "account_type": self.status.account_type, "symbol": symbol,
               "volume": float(plan["size"]), "price": getattr(result, "price", price),
               "order": getattr(result, "order", None),
               "sl": plan["stop"], "tp": plan["target"],
               "comment": getattr(result, "comment", "")}

    # ---- monitoring + kill switch ----

    def positions(self) -> List[Dict[str, Any]]:
        if not self.status.connected:
            return []
        try:
            poss = self._mt5.positions_get() or []
            return [{"symbol": p.symbol, "volume": p.volume, "type": "buy" if p.type == 0 else "sell",
                    "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit,
                    "ticket": p.ticket} for p in poss]
        except Exception:
            return []

    def close_all(self) -> Dict[str, Any]:
        """Kill switch: flatten every open position immediately."""
        if not self.status.connected:
            return {"status": "error", "reason": "not connected"}
        mt5 = self._mt5
        closed, errors = [], []
        for p in (mt5.positions_get() or []):
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                errors.append(p.symbol); continue
            is_buy = p.type == 0
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                  "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                  "position": p.ticket, "price": tick.bid if is_buy else tick.ask,
                  "deviation": 20, "magic": 770077, "comment": "OracleAI-close",
                  "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                closed.append(p.symbol)
            else:
                errors.append(p.symbol)
        return {"status": "complete", "closed": closed, "errors": errors}

    def account(self) -> Dict[str, Any]:
        if self.status.connected and self._mt5:
            info = self._mt5.account_info()
            if info:
                self.status.balance = getattr(info, "balance", self.status.balance)
                self.status.equity = getattr(info, "equity", self.status.equity)
        return self.status.to_dict()
