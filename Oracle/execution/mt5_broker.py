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

  FIX-2  Convert risk-manager position units to broker lots, then enforce
         symbol volume constraints before order_send().
         Brokers enforce volume_min / volume_max / volume_step per symbol.
         RiskManager sizes positions in underlying units; MT5 expects lots.
         _clamp_volume() uses symbol_info().trade_contract_size to convert
         units to lots, rounds down to volume_step, and rejects missing or
         invalid metadata rather than treating units as lots.

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
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

try:
    from execution.market_clock import MarketClock  # type: ignore
except ImportError:
    from Oracle.execution.market_clock import MarketClock  # type: ignore

from shared.startup import load_dotenv_early

load_dotenv_early(__file__)

from shared.config import get_config

_cfg = get_config(reload=True)
log = logging.getLogger("oracle.mt5")


def _normalize_order_direction(value: Any) -> Optional[str]:
    direction = str(value or "").strip().lower()
    return direction if direction in ("long", "buy", "short", "sell") else None


def _round_price_to_digits(price: float, digits: int) -> float:
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(price)).quantize(quantum, rounding=ROUND_HALF_UP))


DEFAULT_SYMBOL_MAP = {
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY", "AUDUSD": "AUDUSD",
    "USDCAD": "USDCAD", "USDCHF": "USDCHF", "NZDUSD": "NZDUSD", "EURGBP": "EURGBP",
    "EURJPY": "EURJPY", "GBPJPY": "GBPJPY",
    "XAUUSD": "XAUUSD", "USOIL": "USOIL",
    "BTCUSD": "BTCUSD", "ETHUSD": "ETHUSD", "SOLUSD": "SOLUSD", "XRPUSD": "XRPUSD",
    "BNBUSD": "BNBUSD", "ADAUSD": "ADAUSD", "LTCUSD": "LTCUSD", "DOGEUSD": "DOGEUSD",
    "DOTUSD": "DOTUSD", "LINKUSD": "LINKUSD", "AVAXUSD": "AVAXUSD", "MATICUSD": "MATICUSD",
    "BCHUSD": "BCHUSD", "UNIUSD": "UNIUSD", "XLMUSD": "XLMUSD", "ATOMUSD": "ATOMUSD",
    "NEARUSD": "NEARUSD", "SHIBUSD": "SHIBUSD", "TRXUSD": "TRXUSD", "TONUSD": "TONUSD",
    "SUIUSD": "SUIUSD", "PEPEUSD": "PEPEUSD",
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
    trade_mode: Optional[int] = None
    login: Optional[int] = None
    server: str = ""
    balance: float = 0.0
    equity: float = 0.0
    currency: str = ""
    reason: str = ""
    account_info_available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"connected": self.connected, "account_type": self.account_type,
                "trade_mode": self.trade_mode,
                "login": self.login, "server": self.server, "balance": self.balance,
                "equity": self.equity, "currency": self.currency, "reason": self.reason,
                "account_info_available": self.account_info_available}


def _classify_account_type(mt5: Any, info: Any) -> tuple[str, Optional[int]]:
    raw_trade_mode = getattr(info, "trade_mode", None)
    try:
        trade_mode = int(raw_trade_mode)
    except (TypeError, ValueError, OverflowError):
        return "unknown", None
    if trade_mode != raw_trade_mode:
        return "unknown", None

    account_modes = (
        ("ACCOUNT_TRADE_MODE_DEMO", 0, "demo"),
        ("ACCOUNT_TRADE_MODE_CONTEST", 1, "contest"),
        ("ACCOUNT_TRADE_MODE_REAL", 2, "real"),
    )
    for constant_name, fallback_value, account_type in account_modes:
        mode_value = getattr(mt5, constant_name, fallback_value)
        try:
            if trade_mode == int(mode_value):
                return account_type, trade_mode
        except (TypeError, ValueError, OverflowError):
            continue
    return "unknown", trade_mode


class MT5Broker:
    def __init__(
        self,
        symbol_map: Optional[Dict[str, str]] = None,
        *,
        paper: Optional[bool] = None,
        allow_live: Optional[bool] = None,
    ):
        self._mt5 = None
        self.status = BrokerStatus()
        self.symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
        if allow_live is None:
            raw_allow = os.getenv("ORACLE_ALLOW_LIVE")
            self.allow_live = (
                raw_allow.strip().lower() in ("1", "true", "yes", "on")
                if raw_allow is not None and raw_allow.strip() != ""
                else _cfg.oracle_allow_live
            )
        else:
            self.allow_live = allow_live
        if paper is None:
            raw_paper = os.getenv("ORACLE_PAPER_TRADING")
            self.paper = (
                raw_paper.strip().lower() not in ("0", "false", "no", "off")
                if raw_paper is not None and raw_paper.strip() != ""
                else _cfg.oracle_paper_trading
            )
        else:
            self.paper = paper
        self._paper_positions = {}  # for paper trading mode: {ticket: position_dict}
        self._next_ticket = 1000000  # paper trading ticket counter
        self._positions_last_refresh_ok: bool = True
        self._symbol_schedule_by_name: Dict[str, Dict[str, Any]] = {}
        self._symbol_constraints: Dict[str, Dict[str, Any]] = {}
        self._constraints_lock = threading.Lock()

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
        self._update_status_from_account_info(info, login=login, server=server)
        acct_type = self.status.account_type
        log.info("MT5 connected: %s account on %s (balance %.2f %s)", acct_type, self.status.server,
                 self.status.balance, self.status.currency)
        return self.status.to_dict()

    def _update_status_from_account_info(self, info: Any, login=None, server=None) -> None:
        account_type, trade_mode = _classify_account_type(self._mt5, info)
        balance = float(getattr(info, "balance", 0.0) or 0.0)
        equity = float(getattr(info, "equity", 0.0) or 0.0)
        info_ok = math.isfinite(balance) and math.isfinite(equity) and (balance > 0 or equity > 0)
        self.status = BrokerStatus(
            connected=True,
            account_type=account_type,
            trade_mode=trade_mode,
            login=getattr(info, "login", login if login is not None else self.status.login),
            server=getattr(info, "server", server or self.status.server),
            balance=balance,
            equity=equity,
            currency=str(getattr(info, "currency", "") or ""),
            reason="ok",
            account_info_available=info_ok,
        )
        if account_type == "unknown":
            log.warning(
                "MT5 account type is unrecognized (trade_mode=%r, server=%s)",
                trade_mode,
                self.status.server,
            )

    def disconnect(self) -> None:
        if self._mt5:
            try:
                self._mt5.shutdown()
            except Exception as exc:
                log.debug("MT5 shutdown call failed (likely already disconnected): %s", exc)

    # ── FIX-A: symbols() & cTrader-parity catalog/metadata/schedules ──────────────
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

    def symbol_catalog(self) -> List[str]:
        """Alias for symbols() matching CTraderBroker.symbol_catalog()."""
        return sorted(self.symbols())

    def symbol_metadata(self, symbol: str) -> Dict[str, Any]:
        """Return broker-reported metadata for a listed MT5 symbol (matching CTraderBroker)."""
        sym = str(symbol or "").strip()
        if not sym:
            raise ValueError("symbol name is required")
        lock = getattr(self, "_constraints_lock", None)
        cache = getattr(self, "_symbol_constraints", None)
        if lock is not None and isinstance(cache, dict):
            with lock:
                if sym.upper() in cache:
                    return dict(cache[sym.upper()])

        mt5 = getattr(self, "_mt5", None)
        symbol_info_fn = getattr(mt5, "symbol_info", None)
        if not callable(symbol_info_fn):
            return {"digits": None, "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}
        try:
            if callable(getattr(mt5, "symbol_select", None)):
                mt5.symbol_select(sym, True)
            info = symbol_info_fn(sym)
        except Exception:
            info = None
        if info is None:
            return {"digits": None, "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}

        digits = getattr(info, "digits", None)
        digits = int(digits) if isinstance(digits, int) and not isinstance(digits, bool) and digits >= 0 else None
        metadata = {
            "symbol": getattr(info, "name", sym),
            "digits": digits,
            "point": float(getattr(info, "point", 0.0) or 0.0),
            "trade_stops_level": int(getattr(info, "trade_stops_level", 0) or 0),
            "trade_contract_size": float(getattr(info, "trade_contract_size", 100000.0) or 100000.0),
            "volume_min": float(getattr(info, "volume_min", 0.01) or 0.01),
            "volume_max": float(getattr(info, "volume_max", 100.0) or 100.0),
            "volume_step": float(getattr(info, "volume_step", 0.01) or 0.01),
            "trade_mode": int(getattr(info, "trade_mode", 4) if getattr(info, "trade_mode", None) is not None else 4),
            "currency_base": str(getattr(info, "currency_base", "") or ""),
            "currency_profit": str(getattr(info, "currency_profit", "") or ""),
            "spread": int(getattr(info, "spread", 0) or 0),
        }
        if lock is not None and isinstance(cache, dict):
            with lock:
                cache[sym.upper()] = metadata
        return dict(metadata)

    def load_market_schedules(self, symbol_names: List[str]) -> None:
        """Load MT5 trading schedules & trade modes for configured symbols (cTrader parity)."""
        if not getattr(self.status, "connected", False) or self._mt5 is None:
            return
        schedules = getattr(self, "_symbol_schedule_by_name", None)
        if not isinstance(schedules, dict):
            self._symbol_schedule_by_name = {}
            schedules = self._symbol_schedule_by_name

        # Standard 24/5 FX/CFD weekly schedule (Sun 22:00 UTC -> Fri 22:00 UTC) and 24/7 Crypto schedule
        fx_cfd_schedule = [{"startSecond": 22 * 3600, "endSecond": 5 * 86400 + 22 * 3600}]
        crypto_schedule = [{"startSecond": 0, "endSecond": 7 * 86400}]

        for name in symbol_names:
            if not name:
                continue
            sym_up = str(name).strip().upper()
            meta = self.symbol_metadata(str(name).strip())
            mt5_mode = int(meta.get("trade_mode", 4))
            # Map MT5 trade_mode (0=Disabled, 1=LongOnly, 2=ShortOnly, 3=CloseOnly, 4=Full)
            # to MarketClock trading_mode (0=Full, 1=Disabled, 3=CloseOnly)
            if mt5_mode == 0:
                clock_mode = 1
            elif mt5_mode == 3:
                clock_mode = 3
            else:
                clock_mode = 0
            is_crypto = any(sym_up.startswith(p) for p in ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LTC", "DOGE"))
            schedules[sym_up] = {
                "schedule": crypto_schedule if is_crypto else fx_cfd_schedule,
                "timezone": "Etc/UTC",
                "trading_mode": clock_mode,
                "holidays": [],
            }
        log.info("MT5: loaded market schedules for %d configured symbols", len(schedules))

    def market_state(self, symbol: str, now=None) -> Dict[str, Any]:
        """Evaluate whether a symbol is currently open for trading (cTrader parity)."""
        sym_up = str(symbol or "").strip().upper()
        schedules = getattr(self, "_symbol_schedule_by_name", None) or {}
        details = schedules.get(sym_up)
        if details is None:
            is_crypto = any(sym_up.startswith(p) for p in ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LTC", "DOGE"))
            default_sched = (
                [{"startSecond": 0, "endSecond": 7 * 86400}]
                if is_crypto
                else [{"startSecond": 22 * 3600, "endSecond": 5 * 86400 + 22 * 3600}]
            )
            return MarketClock.evaluate(
                sym_up, default_sched, "Etc/UTC",
                trading_mode=0, holidays=[], now=now or datetime.now(timezone.utc),
            )
        return MarketClock.evaluate(
            sym_up, details["schedule"], details["timezone"],
            trading_mode=details["trading_mode"], holidays=details["holidays"], now=now,
        )

    def live_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return a live bid/ask/mid quote dict from MT5 for symbol, or None if unavailable."""
        if not getattr(self.status, "connected", False) or self._mt5 is None:
            return None
        mt5 = self._mt5
        bsym = self._map(symbol)
        try:
            if callable(getattr(mt5, "symbol_select", None)):
                mt5.symbol_select(bsym, True)
            tick = mt5.symbol_info_tick(bsym) if callable(getattr(mt5, "symbol_info_tick", None)) else None
            if tick is None:
                return None
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            mid = (bid + ask) / 2.0
            spread_bps = round(((ask - bid) / mid) * 10000.0, 4) if mid > 0 else None
            return {
                "status": "complete",
                "symbol": bsym,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_bps": spread_bps,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            return None

    def _resolve_broker_symbol_name(self, symbol: str) -> str:
        """Resolve an Oracle or broker symbol name against the connected MT5 terminal."""
        smap = getattr(self, "symbol_map", None) or {}
        bsym = smap.get(str(symbol).upper(), str(symbol).strip())
        mt5 = getattr(self, "_mt5", None)
        if mt5 is None:
            return bsym
        try:
            if callable(getattr(mt5, "symbol_select", None)) and mt5.symbol_select(bsym, True):
                return bsym
            prefix = str(symbol).strip().upper()[:6]
            for s in (mt5.symbols_get() or []) if callable(getattr(mt5, "symbols_get", None)) else []:
                sname = getattr(s, "name", "")
                if sname.upper().startswith(prefix):
                    return sname
        except Exception:
            pass
        return bsym

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
    def _clamp_volume(self, symbol: str, raw_units: float) -> Optional[float]:
        """
        Convert underlying units to MT5 lots and round down to the broker's
        volume step. Return None when broker metadata is missing/invalid or
        the requested size is below the minimum; never increase risk by
        treating units as lots or rounding up to the minimum.
        """
        mt5 = self._mt5
        if mt5 is None:
            log.error("_clamp_volume(%s): MT5 unavailable; refusing order", symbol)
            return None
        try:
            info = mt5.symbol_info(symbol)
            if info is None:
                log.error("_clamp_volume(%s): symbol_info returned None; refusing order", symbol)
                return None

            contract_size = getattr(info, "trade_contract_size", None)
            vol_min = getattr(info, "volume_min", None)
            vol_max = getattr(info, "volume_max", None)
            vol_step = getattr(info, "volume_step", None)
            values = (raw_units, contract_size, vol_min, vol_max, vol_step)
            if (not all(isinstance(value, (int, float)) and math.isfinite(value)
                        for value in values)
                    or raw_units <= 0 or contract_size <= 0 or vol_min <= 0
                    or vol_max < vol_min or vol_step <= 0):
                log.error("_clamp_volume(%s): invalid size or symbol contract/volume metadata; "
                          "refusing order", symbol)
                return None

            raw_lots = raw_units / contract_size
            # Round down to ensure rounding never exceeds the risk-sized units.
            steps = math.floor((raw_lots / vol_step) + 1e-12)
            vol = steps * vol_step

            # Respect the broker maximum without ever raising a too-small
            # risk-sized trade to the minimum volume.
            vol = min(vol_max, vol)
            if vol < vol_min:
                log.warning(
                    "_clamp_volume(%s): requested %.4f units (%.8f lots) is below "
                    "minimum %.4f lots; refusing order",
                    symbol, raw_units, raw_lots, vol_min)
                return None

            step_text = f"{vol_step:.10f}".rstrip("0").rstrip(".")
            decimals = len(step_text.partition(".")[2])
            vol = round(vol, decimals)

            log.info(
                "_clamp_volume(%s): %.4f units → %.4f lots "
                "(contract_size=%.4f min=%.4f max=%.4f step=%.4f)",
                symbol, raw_units, vol, contract_size, vol_min, vol_max, vol_step)
            return vol

        except Exception as exc:
            log.error("_clamp_volume(%s): %s; refusing order", symbol, exc)
            return None

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
        direction = _normalize_order_direction(plan.get("direction"))
        if direction is None:
            return {"status": "rejected", "reason": "direction must be buy/long or sell/short"}
        is_buy = direction in ("long", "buy")
        price = float(tick.ask if is_buy else tick.bid)
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        stop = float(plan["stop"])
        target = float(plan["target"])
        if plan.get("rebase_stops_to_market"):
            reference_entry = float(plan.get("entry", 0.0))
            max_entry_deviation = float(plan.get("max_entry_deviation", 0.0))
            if reference_entry <= 0 or max_entry_deviation <= 0:
                return {
                    "status": "rejected",
                    "reason": "invalid market-entry rebase limits",
                    "symbol": symbol,
                }
            if abs(price - reference_entry) > max_entry_deviation:
                return {
                    "status": "rejected",
                    "reason": "current MT5 price moved beyond the signal entry tolerance",
                    "symbol": symbol,
                }
            stop_distance = abs(reference_entry - stop)
            target_distance = abs(target - reference_entry)
            stop = price - stop_distance if is_buy else price + stop_distance
            target = price + target_distance if is_buy else price - target_distance

        # Precision rounding & broker stops_level enforcement when symbol_info is available
        sym_info_fn = getattr(mt5, "symbol_info", None)
        if callable(sym_info_fn):
            try:
                sinfo = sym_info_fn(symbol)
            except Exception:
                sinfo = None
            if sinfo is not None:
                digits = getattr(sinfo, "digits", None)
                point = float(getattr(sinfo, "point", 0.0) or 0.0)
                stops_level = int(getattr(sinfo, "trade_stops_level", 0) or 0)
                if point > 0 and stops_level > 0:
                    min_stop_dist = stops_level * point * 1.05
                    if stop > 0:
                        if is_buy and (price - stop) < min_stop_dist:
                            stop = price - min_stop_dist
                        elif (not is_buy) and (stop - price) < min_stop_dist:
                            stop = price + min_stop_dist
                    if target > 0:
                        if is_buy and (target - price) < min_stop_dist:
                            target = price + min_stop_dist
                        elif (not is_buy) and (price - target) < min_stop_dist:
                            target = price - min_stop_dist
                if isinstance(digits, int) and digits >= 0:
                    price = _round_price_to_digits(price, digits) or price
                    if stop > 0:
                        stop = _round_price_to_digits(stop, digits) or stop
                    if target > 0:
                        target = _round_price_to_digits(target, digits) or target

        # FIX-1: auto-detect filling mode
        filling = self._filling_mode(symbol)

        # FIX-2: clamp/round volume to broker symbol constraints
        raw_units = float(plan["size"])
        volume = self._clamp_volume(symbol, raw_units)
        if volume is None:
            return {
                "status": "rejected",
                "reason": f"could not safely convert requested {raw_units:.4f} "
                          f"{symbol} units to MT5 lots",
                "symbol": symbol,
                "volume_units_raw": raw_units,
            }

        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                  "volume": volume, "type": order_type, "price": price,
                  "sl": stop, "tp": target,
                  "deviation": 20, "magic": 770077, "comment": "OracleAI",
                  "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
        result = mt5.order_send(request)
        if result is None: return {"status": "error", "reason": f"order_send returned None: {mt5.last_error()}"}
        retcode = getattr(result, "retcode", None)
        comment = str(getattr(result, "comment", "") or "")
        ok = retcode == mt5.TRADE_RETCODE_DONE
        reason = "" if ok else (
            f"MT5 order rejected (retcode={retcode}, "
            f"comment={comment or 'no broker comment'})"
        )
        if not ok:
            log.warning(
                "%s symbol=%s volume=%.4f lots price=%.8f sl=%.8f tp=%.8f "
                "filling=%s retcode=%s comment=%s",
                reason, symbol, volume, price, float(plan["stop"]),
                float(plan["target"]), filling, retcode, comment or "<empty>",
            )
        return {"status": "filled" if ok else "rejected", "retcode": retcode,
               "reason": reason,
               "account_type": self.status.account_type, "symbol": symbol,
               "volume": volume, "price": getattr(result, "price", price),
               "order": getattr(result, "order", None), "sl": stop, "tp": target,
               "comment": comment,
               "filling_mode": filling, "volume_raw": raw_units,
               "volume_units_raw": raw_units, "volume_lots": volume}

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
            self._positions_last_refresh_ok = True
            # Paper trading mode: return from internal state
            result = list(self._paper_positions.values())
            if symbol:
                result = [p for p in result if p["symbol"].upper().startswith(symbol.upper()[:6])]
            return result
        
        if not self.status.connected:
            self._positions_last_refresh_ok = False
            return []
        
        try:
            raw_poss = self._mt5.positions_get()
            if raw_poss is None:
                self._positions_last_refresh_ok = False
                poss = []
            else:
                self._positions_last_refresh_ok = True
                poss = list(raw_poss)
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
                    "ticket": int(p.ticket),
                }
                result.append(pos_dict)
            
            if symbol:
                result = [p for p in result if p["symbol"].upper().startswith(symbol.upper()[:6])]
            
            return result
        except Exception as exc:
            self._positions_last_refresh_ok = False
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

            sym_info_fn = getattr(mt5, "symbol_info", None)
            if callable(sym_info_fn):
                try:
                    sinfo = sym_info_fn(pos.symbol)
                    digits = getattr(sinfo, "digits", None) if sinfo is not None else None
                    if isinstance(digits, int) and digits >= 0:
                        if new_sl is not None and float(new_sl) > 0:
                            new_sl = _round_price_to_digits(float(new_sl), digits) or new_sl
                        if new_tp is not None and float(new_tp) > 0:
                            new_tp = _round_price_to_digits(float(new_tp), digits) or new_tp
                except Exception:
                    pass

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
        except Exception as exc:
            log.debug("connection health check failed: %s", exc)

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

    def observed_positions_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Read positions from the connected MT5 terminal, including in paper mode.

        This is intentionally read-only. Signal-only mode needs to observe
        positions the user opened manually even when Oracle's own execution is
        configured for paper trading.
        """
        if not self.status.connected or self._mt5 is None:
            return []
        try:
            self._mt5.symbol_select(symbol, True)
            raw = self._mt5.positions_get(symbol=symbol)
            if not raw:
                prefix = symbol.upper()[:6]
                raw = [p for p in (self._mt5.positions_get() or [])
                       if p.symbol.upper().startswith(prefix)]
            return [{
                "symbol": p.symbol,
                "volume": p.volume,
                "type": "buy" if int(p.type) == 0 else "sell",
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "ticket": int(p.ticket),
                "time": float(getattr(p, "time", 0.0)) or None,
            } for p in (raw or [])]
        except Exception as exc:
            log.warning("observed_positions_by_symbol(%s): %s", symbol, exc)
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
        if self._mt5 is None and self.available:
            self.connect()
        if self._mt5 is not None:
            info = self._mt5.account_info()
            if info is None:
                self.connect()
                info = self._mt5.account_info() if self._mt5 is not None else None
            if info is not None:
                self._update_status_from_account_info(info)
                d = self.status.to_dict()
                d["profit"] = float(getattr(info, "profit", 0.0) or 0.0)
                d["margin"] = float(getattr(info, "margin", 0.0) or 0.0)
                d["margin_free"] = float(getattr(info, "margin_free", d["equity"]) or 0.0)
                return d
            self.status.connected = False
            self.status.account_info_available = False
            self.status.reason = "MT5 account_info unavailable"
        return self.status.to_dict()

    def historical_bars(
        self,
        symbol: str,
        from_timestamp_ms: Optional[int] = None,
        to_timestamp_ms: Optional[int] = None,
        period: str = "D1",
        count: int = 200,
        include_live_tick: bool = True,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """Fetch historical OHLCV bars directly from the connected MT5 terminal.

        Provides an identical return contract to ``CTraderBroker.historical_bars``
        so ``BrokerMarketDataSource`` in ``Oracle/core/market_data.py`` can query
        either broker seamlessly as Priority #1 live market data.
        """
        if self._mt5 is None and self.available:
            self.connect()
        if not self.status.connected or self._mt5 is None:
            return {"status": "error", "reason": "not connected to MT5", "bars": []}

        mt5 = self._mt5
        bsym = self._resolve_broker_symbol_name(symbol)
        try:
            if not mt5.symbol_select(bsym, True):
                return {"status": "error", "reason": f"symbol {bsym} not available at MT5 broker", "bars": []}

            period_key = str(period or "D1").strip().upper()
            tf_map = {
                "M1": getattr(mt5, "TIMEFRAME_M1", 1),
                "M5": getattr(mt5, "TIMEFRAME_M5", 5),
                "M15": getattr(mt5, "TIMEFRAME_M15", 15),
                "M30": getattr(mt5, "TIMEFRAME_M30", 30),
                "H1": getattr(mt5, "TIMEFRAME_H1", 16385),
                "H4": getattr(mt5, "TIMEFRAME_H4", 16388),
                "D1": getattr(mt5, "TIMEFRAME_D1", 16408),
                "W1": getattr(mt5, "TIMEFRAME_W1", 32769),
            }
            if period_key not in tf_map:
                return {"status": "error", "reason": f"unsupported trendbar period {period!r}", "bars": []}
            tf_code = tf_map[period_key]

            bar_count = max(30, int(count or 200))
            if from_timestamp_ms is not None and to_timestamp_ms is not None and period_key == "D1":
                span_days = max(30, int((int(to_timestamp_ms) - int(from_timestamp_ms)) // (86400 * 1000)) + 5)
                bar_count = max(bar_count, min(span_days, 1000))

            rates = None
            copy_pos_fn = getattr(mt5, "copy_rates_from_pos", None)
            if callable(copy_pos_fn):
                rates = copy_pos_fn(bsym, tf_code, 0, bar_count)

            if (rates is None or len(rates) == 0) and from_timestamp_ms is not None and to_timestamp_ms is not None:
                copy_range_fn = getattr(mt5, "copy_rates_range", None)
                if callable(copy_range_fn):
                    dt_from = datetime.fromtimestamp(int(from_timestamp_ms) / 1000.0, tz=timezone.utc)
                    dt_to = datetime.fromtimestamp(int(to_timestamp_ms) / 1000.0, tz=timezone.utc)
                    rates = copy_range_fn(bsym, tf_code, dt_from, dt_to)

            if rates is None or len(rates) == 0:
                return {"status": "error", "reason": f"MT5 returned 0 bars for {bsym} ({period_key})", "bars": []}

            sinfo = None
            sym_info_fn = getattr(mt5, "symbol_info", None)
            if callable(sym_info_fn):
                try:
                    sinfo = sym_info_fn(bsym)
                except Exception:
                    sinfo = None
            digits = getattr(sinfo, "digits", None) if sinfo is not None else None
            round_digits = digits if (isinstance(digits, int) and digits >= 0) else 6

            bars: List[Dict[str, Any]] = []
            for r in rates:
                try:
                    u_sec = int(r["time"])
                    o_val = float(r["open"])
                    h_val = float(r["high"])
                    l_val = float(r["low"])
                    c_val = float(r["close"])
                    try:
                        v_val = float(r["real_volume"])
                        if v_val <= 0:
                            v_val = float(r["tick_volume"])
                    except Exception:
                        v_val = float(r["tick_volume"])
                except Exception:
                    u_sec = int(getattr(r, "time", 0))
                    o_val = float(getattr(r, "open", 0.0))
                    h_val = float(getattr(r, "high", 0.0))
                    l_val = float(getattr(r, "low", 0.0))
                    c_val = float(getattr(r, "close", 0.0))
                    v_val = float(getattr(r, "real_volume", 0.0) or getattr(r, "tick_volume", 0.0) or 0.0)

                if not all(math.isfinite(x) and x > 0 for x in (o_val, h_val, l_val, c_val)):
                    continue
                if h_val < max(o_val, c_val, l_val) or l_val > min(o_val, c_val, h_val):
                    continue
                dt_utc = datetime.fromtimestamp(u_sec, tz=timezone.utc)
                ts_iso = dt_utc.strftime("%Y-%m-%d") if period_key in ("D1", "W1") else dt_utc.isoformat()
                bars.append({
                    "timestamp": ts_iso,
                    "timestamp_ms": u_sec * 1000,
                    "open": round(o_val, round_digits),
                    "high": round(h_val, round_digits),
                    "low": round(l_val, round_digits),
                    "close": round(c_val, round_digits),
                    "volume": max(0.0, float(v_val)),
                })

            if not bars:
                return {"status": "error", "reason": f"all MT5 bars invalid for {bsym}", "bars": []}

            bars.sort(key=lambda item: int(item.get("timestamp_ms", 0)))

            # Anchor forming latest bar to live bid tick if available
            if include_live_tick:
                try:
                    tick = mt5.symbol_info_tick(bsym)
                    bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else 0.0
                    if math.isfinite(bid) and bid > 0:
                        last_bar = bars[-1]
                        last_bar["close"] = round(bid, round_digits)
                        if bid > float(last_bar["high"]):
                            last_bar["high"] = round(bid, round_digits)
                        if bid < float(last_bar["low"]):
                            last_bar["low"] = round(bid, round_digits)
                except Exception:
                    pass

            return {
                "status": "complete",
                "symbol": bsym,
                "source": f"mt5_broker_live:{bsym}",
                "price_basis": "bid",
                "period": period_key,
                "bars": bars,
                "has_more": False,
            }
        except Exception as exc:
            log.warning("historical_bars(%s/%s): %s", symbol, bsym, exc)
            return {"status": "error", "reason": str(exc), "bars": []}

    def fetch_7tf_rates(
        self, symbol: str, broker_symbol: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Pull 7-timeframe OHLCV bars (W1, D1, H4, H1, M30, M15, M5) and live bid/ask
        spread directly from the connected MetaTrader 5 terminal via copy_rates_from_pos.
        Returns None if MT5 is not connected or rates are unavailable.
        """
        if self._mt5 is None and self.available:
            self.connect()
        if not self.status.connected or self._mt5 is None:
            return None

        mt5 = self._mt5
        bsym = broker_symbol or self.symbol_map.get(symbol.upper(), symbol.upper())
        try:
            if not mt5.symbol_select(bsym, True):
                # Try prefix match across broker symbols if exact symbol name differs (e.g. XAUUSDm)
                prefix = symbol.upper()[:6]
                for s in (mt5.symbols_get() or []):
                    sname = getattr(s, "name", "")
                    if sname.upper().startswith(prefix):
                        bsym = sname
                        mt5.symbol_select(bsym, True)
                        break

            tf_specs = {
                "W1": (getattr(mt5, "TIMEFRAME_W1", 32769), 110),
                "D1": (getattr(mt5, "TIMEFRAME_D1", 16408), 365),
                "H4": (getattr(mt5, "TIMEFRAME_H4", 16388), 360),
                "H1": (getattr(mt5, "TIMEFRAME_H1", 16385), 720),
                "M30": (getattr(mt5, "TIMEFRAME_M30", 30), 720),
                "M15": (getattr(mt5, "TIMEFRAME_M15", 15), 960),
                "M5": (getattr(mt5, "TIMEFRAME_M5", 5), 1200),
            }
            raw_series: Dict[str, List[Dict[str, Any]]] = {}
            import datetime as _dt

            for tf_name, (tf_code, bar_count) in tf_specs.items():
                rates = mt5.copy_rates_from_pos(bsym, tf_code, 0, bar_count)
                if rates is None or len(rates) == 0:
                    continue
                bars: List[Dict[str, Any]] = []
                for r in rates:
                    # Support both numpy structured row (r['time']) and object attribute (r.time)
                    try:
                        u_sec = int(r["time"])
                        o = float(r["open"])
                        h = float(r["high"])
                        l = float(r["low"])
                        c = float(r["close"])
                        v = float(r["tick_volume"]) if "tick_volume" in r.dtype.names else float(r["real_volume"])
                    except Exception:
                        u_sec = int(getattr(r, "time", 0))
                        o = float(getattr(r, "open", 0.0))
                        h = float(getattr(r, "high", 0.0))
                        l = float(getattr(r, "low", 0.0))
                        c = float(getattr(r, "close", 0.0))
                        v = float(getattr(r, "tick_volume", getattr(r, "real_volume", 0.0)) or 0.0)
                    if u_sec > 0 and c > 0 and h >= max(o, c) and l <= min(o, c):
                        ts_iso = (
                            _dt.datetime.fromtimestamp(u_sec, tz=_dt.timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                        bars.append(
                            {
                                "ts": ts_iso,
                                "unix_sec": u_sec,
                                "open": o,
                                "high": h,
                                "low": l,
                                "close": c,
                                "volume": v,
                            }
                        )
                raw_series[tf_name] = bars

            if len(raw_series.get("M15", [])) < 40 or len(raw_series.get("H1", [])) < 20:
                return None

            tick = mt5.symbol_info_tick(bsym)
            info = mt5.symbol_info(bsym)
            bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else 0.0
            ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick is not None else 0.0
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else float(raw_series["M15"][-1]["close"])
            spread_price = max(0.0, ask - bid) if (bid > 0 and ask > 0) else 0.0
            if spread_price <= 0 and info is not None:
                pt = float(getattr(info, "point", 0.0) or 0.0)
                sp_pts = float(getattr(info, "spread", 0.0) or 0.0)
                spread_price = pt * sp_pts
            live_spread_bps = round((spread_price / max(mid, 1e-9)) * 10000.0, 4) if spread_price > 0 else None

            return {
                "symbol": symbol.upper(),
                "broker_symbol": bsym,
                "source": f"mt5_broker_live:{bsym}",
                "price_basis": "bid",
                "live_bid": bid,
                "live_ask": ask,
                "live_spread_bps": live_spread_bps,
                "raw_series": raw_series,
            }
        except Exception as exc:
            log.warning("fetch_7tf_rates(%s): %s", symbol, exc)
            return None

    def get_closed_deals(
        self, symbol: Optional[str] = None, lookback_days: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Query MT5 history_deals_get() over the past `lookback_days` and pair entry/exit
        deals by position_id so the runner can reconcile terminal win/loss reports
        against actual closed MT5 broker positions.
        """
        if self._mt5 is None and self.available:
            self.connect()
        if not self.status.connected or self._mt5 is None:
            return []

        mt5 = self._mt5
        try:
            now_ts = time.time()
            from_ts = now_ts - max(1, int(lookback_days)) * 86400.0
            deals = mt5.history_deals_get(from_ts, now_ts + 3600.0)
            if not deals:
                return []

            by_pos: Dict[int, Dict[str, Any]] = {}
            prefix = symbol.upper()[:6] if symbol else None

            for d in deals:
                d_sym = str(getattr(d, "symbol", "") or "")
                if not d_sym:
                    continue
                if prefix and not d_sym.upper().startswith(prefix):
                    continue
                pos_id = int(getattr(d, "position_id", 0) or getattr(d, "order", 0) or getattr(d, "ticket", 0))
                if pos_id <= 0:
                    continue
                entry_flag = int(getattr(d, "entry", -1))
                d_type = int(getattr(d, "type", -1))
                # Only buy (0) and sell (1) market deals
                if d_type not in (0, 1):
                    continue

                rec = by_pos.setdefault(
                    pos_id,
                    {
                        "position_id": pos_id,
                        "broker_symbol": d_sym,
                        "direction": None,
                        "volume_lots": 0.0,
                        "entry_price": 0.0,
                        "exit_price": 0.0,
                        "entry_time_unix": 0,
                        "exit_time_unix": 0,
                        "gross_profit_usd": 0.0,
                        "commission_usd": 0.0,
                        "swap_usd": 0.0,
                        "net_profit_usd": 0.0,
                        "has_exit_deal": False,
                        "exit_reason_code": int(getattr(d, "reason", 0) or 0),
                        "comment": str(getattr(d, "comment", "") or ""),
                    },
                )
                rec["commission_usd"] += float(getattr(d, "commission", 0.0) or 0.0)
                rec["swap_usd"] += float(getattr(d, "swap", 0.0) or 0.0)
                rec["gross_profit_usd"] += float(getattr(d, "profit", 0.0) or 0.0)

                if entry_flag == 0:  # DEAL_ENTRY_IN
                    rec["direction"] = "LONG" if d_type == 0 else "SHORT"
                    rec["volume_lots"] = float(getattr(d, "volume", rec["volume_lots"]) or 0.0)
                    rec["entry_price"] = float(getattr(d, "price", 0.0) or 0.0)
                    rec["entry_time_unix"] = int(getattr(d, "time", 0) or 0)
                elif entry_flag in (1, 2, 3):  # DEAL_ENTRY_OUT / INOUT / OUT_BY
                    rec["has_exit_deal"] = True
                    if rec["direction"] is None:
                        # Opposite of closing deal type
                        rec["direction"] = "LONG" if d_type == 1 else "SHORT"
                    if rec["volume_lots"] <= 0:
                        rec["volume_lots"] = float(getattr(d, "volume", 0.0) or 0.0)
                    rec["exit_price"] = float(getattr(d, "price", 0.0) or 0.0)
                    rec["exit_time_unix"] = int(getattr(d, "time", 0) or 0)
                    rec["exit_reason_code"] = int(getattr(d, "reason", 0) or 0)
                    if getattr(d, "comment", ""):
                        rec["comment"] = str(getattr(d, "comment", ""))

            closed_list: List[Dict[str, Any]] = []
            for pos_id, rec in sorted(by_pos.items(), key=lambda kv: kv[1]["exit_time_unix"]):
                if not rec["has_exit_deal"]:
                    continue
                net_pnl = round(
                    rec["gross_profit_usd"] + rec["commission_usd"] + rec["swap_usd"], 2
                )
                rec["gross_profit_usd"] = round(rec["gross_profit_usd"], 2)
                rec["commission_usd"] = round(rec["commission_usd"], 2)
                rec["swap_usd"] = round(rec["swap_usd"], 2)
                rec["net_profit_usd"] = net_pnl
                comment_low = rec["comment"].lower()
                r_code = rec["exit_reason_code"]
                # MT5 DEAL_REASON_SL == 4, DEAL_REASON_TP == 5
                if r_code == 4 or "[sl" in comment_low:
                    rec["exit_reason"] = "STOP_LOSS"
                elif r_code == 5 or "[tp" in comment_low:
                    rec["exit_reason"] = "TAKE_PROFIT"
                else:
                    rec["exit_reason"] = "CLIENT_OR_EXPERT_CLOSE"
                closed_list.append(rec)
            return closed_list
        except Exception as exc:
            log.warning("get_closed_deals(%s): %s", symbol, exc)
            return []

