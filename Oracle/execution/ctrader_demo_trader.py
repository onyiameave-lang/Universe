"""
Oracle.execution.ctrader_demo_trader
=====================================
Demo / paper trading loop for cTrader — identical signal pipeline to
mt5_demo_trader.py but using CTraderBroker instead of MT5Broker.

This is the cTrader equivalent of mt5_demo_trader.py. The full Oracle AI
pipeline (signal, risk gate, execution, monitoring, learning) is identical
— only the broker adapter changes from MT5Broker to CTraderBroker.

Pipeline (identical to mt5_demo_trader):
  1. SIGNAL     Oracle produces an evidence-fused, evolved-strategy signal.
  2. RISK GATE  RiskManager sizes + gates the trade.
  3. EXECUTE    CTraderBroker submits via Spotware Open API.
  4. MONITOR    Continuous Trade Manager + kill switch.
  5. LEARN      Realized outcomes feed Oracle's adaptive fusion.

Key differences from mt5_demo_trader.py:
  - Uses CTraderBroker (Spotware Open API / ctrader-open-api) instead of MT5Broker.
  - cTrader symbol naming: DE40 (not GER40), US100 (not NAS100), etc.
  - No MT5 paper mode — always connects to a real cTrader account.
    **Use a DEMO cTrader account for safe testing!**
  - positions(symbol=...) instead of get_positions_by_symbol().
  - close_all() implemented by iterating positions.
  - No adopt_position() — not needed since always live.

Environment variables:
  CTRADER_CLIENT_ID        — from connect.spotware.com
  CTRADER_CLIENT_SECRET    — from connect.spotware.com
  CTRADER_ACCESS_TOKEN     — obtained via ctrader_get_token.py
  CTRADER_ACCOUNT_ID       — ctidTraderAccountId (numeric)
  CTRADER_USE_DEMO=true    — set "false" for live account
  BROKER_SYMBOL_MAP        — optional: "EURUSD:EURUSD.i,USOIL:WTI.cash"
  ORACLE_SYMBOL_TIMEOUT_SEC  — per-symbol timeout (default 60)
  ORACLE_MANAGE_INTERVAL_SEC — Trade Manager poll interval (default 15)

Run (from Universe-oracle-v1/ ecosystem root):
  python -m Oracle.execution.ctrader_demo_trader
  python -m Oracle.execution.ctrader_demo_trader --symbols EURUSD GBPUSD XAUUSD
  python -m Oracle.execution.ctrader_demo_trader --preset live
  python -m Oracle.execution.ctrader_demo_trader --evolve-first
  python -m Oracle.execution.ctrader_demo_trader --cycles 5 --interval 60

See ctrader_get_token.py to obtain your access token before first run.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
import time
import threading
import concurrent.futures as _cf
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make sure the project .env is loaded before any broker credentials are read.
try:
    from shared.startup import load_dotenv_early  # type: ignore
    load_dotenv_early(__file__)
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[1]   # Oracle/
_ECO_ROOT  = _REPO_ROOT.parent                     # Universe/ (ecosystem root)
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution.ctrader_broker import CTraderBroker  # type: ignore
from execution.account_risk_state import AccountRiskState  # type: ignore
from execution.chronicle_position_log import ChroniclePositionLog, adverse_slippage_price  # type: ignore
from execution.trade_manager import (  # type: ignore
    TradeManager, TradeManagerConfig, Position, MarketSnapshot, Direction, Action,
)
from execution.telegram_notifier import (  # type: ignore
    TelegramNotifier, signal_only_enabled,
    format_entry_alert, format_execution_alert, format_position_alert,
)
try:
    from intelligence.news_impact import assess_news_impact  # type: ignore
    from intelligence.social_risk import assess_social_risk  # type: ignore
    from intelligence.trade_learning import TradeLearningEngine  # type: ignore
except ModuleNotFoundError as exc:
    # Other agents also expose a top-level ``intelligence`` package. When one
    # was imported first, resolve Oracle's modules through the unambiguous
    # package path instead of failing trader startup.
    if exc.name not in {"intelligence", "intelligence.news_impact", "intelligence.social_risk", "intelligence.trade_learning"}:
        raise
    from Oracle.intelligence.news_impact import assess_news_impact
    from Oracle.intelligence.social_risk import assess_social_risk
    from Oracle.intelligence.trade_learning import TradeLearningEngine
from Oracle.core.risk import Position as RiskPosition

log = logging.getLogger("oracle.ctrader_demo")

_symbol_slots = None
_symbol_futures = {}
_symbol_executor_lock = threading.RLock()
_shadow_log_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid integer for %s=%r; using %d", name, raw, default)
        return default

# ── Constants ─────────────────────────────────────────────────────────────────
_DEFAULT_MANAGE_INTERVAL = 15   # seconds
_TRADER_ID: str = os.getenv("TRADER_ID", "ctrader_demo")
_DEFAULT_SYMBOL_TIMEOUT = 60
_DASHBOARD_SNAPSHOT_PATH = Path(os.getenv(
    "ORACLE_CTRADER_DASHBOARD_SNAPSHOT_PATH",
    str(_ECO_ROOT / "Oracle" / "memory" / "ctrader_dashboard_snapshot.json"),
)).expanduser()

# ── Shared evolution + execution watchlist ────────────────────────────────────
DEFAULT_SYMBOLS: List[str] = [
    # Major forex
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    # Minor forex
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD",
    # Commodities
    "XAUUSD", "XAGUSD", "USOIL", "UKOIL", "NATGAS",
    # Crypto
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
    # Indices
    "US30", "US500", "NAS100", "GER40", "UK100", "JPN225", "AUS200",
    "SPX", "NASDAQ", "DJI", "RUT", "VIX", "FTSE", "DAX", "CAC40",
    "NIKKEI", "HSI", "SENSEX", "ASX200",
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRKB",
    "LLY", "V", "JPM",
]

_LIVE_PRESET: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD",
    "NZDUSD", "EURGBP",
    "VIX", "MSFT", "USDCAD",
]

MAX_POSITIONS_PER_SYMBOL: int = 1

# ── cTrader-specific broker symbol overrides ──────────────────────────────────
_CTRADER_BROKER_MAP: Dict[str, str] = {
    "USOIL":   "WTOIL-PERP",
    "UKOIL":   "BRENTOIL-PERP",
    "NATGAS":  "NATGAS.cash",
    "NAS100":  "US100",
    "GER40":   "DE40",
    "JPN225":  "JP225",
    "SPX":     "US500",
    "NASDAQ":  "US100",
    "DJI":     "US30",
    "FTSE":    "UK100",
    "DAX":     "DE40",
    "NIKKEI":  "JP225",
    "HSI":     "HK50",
    "SENSEX":  "IN50",
    "ASX200":  "ASX.AU",
    "BRKB":    "BRK.B.US",
    "V":       "V.US",
}

# ── Fallback aliases for fuzzy matching ───────────────────────────────────────
_FALLBACK_ALIASES: Dict[str, List[str]] = {
    "USOIL":   ["USOIL.cash", "WTI.cash", "WTI", "XTIUSD"],
    "UKOIL":   ["UKOIL.cash", "BRENT.cash", "XBRUSD"],
    "NATGAS":  ["NATGAS.cash", "NGAS.cash", "XNGUSD"],
    "BTCUSD":  ["BTCUSD", "BTC/USD"],
    "ETHUSD":  ["ETHUSD", "ETH/USD"],
    "SOLUSD":  ["SOLUSD", "SOL/USD"],
    "NAS100":  ["US100", "NAS100", "USTEC"],
    "GER40":   ["DE40", "GER40", "DAX40"],
    "UK100":   ["UK100", "FTSE100"],
    "JPN225":  ["JP225", "JPN225", "NIKKEI"],
    "AUS200":  ["AUS200", "ASX200"],
    "US30":    ["US30", "DJ30"],
    "US500":   ["US500", "SPX500"],
    "SPX":     ["US500", "SPX500", "SPX"],
    "NASDAQ":  ["US100", "NAS100", "USTEC", "NASDAQ"],
    "DJI":     ["US30", "DJ30", "DJI"],
    "RUT":     ["RUS2000", "US2000", "RUT"],
    "VIX":     ["VIX", "VIXY"],
    "FTSE":    ["UK100", "FTSE100", "FTSE"],
    "DAX":     ["DE40", "GER40", "DAX40", "DAX"],
    "CAC40":   ["FR40", "CAC40"],
    "NIKKEI":  ["JP225", "JPN225", "NIKKEI"],
    "HSI":     ["HK50", "HSI"],
    "SENSEX":  ["IN50", "SENSEX"],
    "ASX200":  ["AU200", "ASX200"],
    "EURUSD":  ["EURUSD", "EUR/USD"],
    "GBPUSD":  ["GBPUSD", "GBP/USD"],
    "USDJPY":  ["USDJPY", "USD/JPY"],
    "AUDUSD":  ["AUDUSD", "AUD/USD"],
    "USDCAD":  ["USDCAD", "USD/CAD"],
    "USDCHF":  ["USDCHF", "USD/CHF"],
    "NZDUSD":  ["NZDUSD", "NZD/USD"],
    "EURGBP":  ["EURGBP", "EUR/GBP"],
    "EURJPY":  ["EURJPY", "EUR/JPY"],
    "GBPJPY":  ["GBPJPY", "GBP/JPY"],
    "AUDJPY":  ["AUDJPY", "AUD/JPY"],
    "CADJPY":  ["CADJPY", "CAD/JPY"],
    "CHFJPY":  ["CHFJPY", "CHF/JPY"],
    "EURAUD":  ["EURAUD", "EUR/AUD"],
    "EURCAD":  ["EURCAD", "EUR/CAD"],
    "EURCHF":  ["EURCHF", "EUR/CHF"],
    "GBPAUD":  ["GBPAUD", "GBP/AUD"],
    "GBPCAD":  ["GBPCAD", "GBP/CAD"],
    "GBPCHF":  ["GBPCHF", "GBP/CHF"],
    "AUDCAD":  ["AUDCAD", "AUD/CAD"],
    "AUDCHF":  ["AUDCHF", "AUD/CHF"],
    "AUDNZD":  ["AUDNZD", "AUD/NZD"],
    "AAPL":    ["AAPL"],
    "MSFT":    ["MSFT"],
    "NVDA":    ["NVDA"],
    "GOOGL":   ["GOOGL", "GOOG"],
    "AMZN":    ["AMZN"],
    "META":    ["META", "FB"],
    "TSLA":    ["TSLA"],
    "BRKB":    ["BRKB", "BRK.B"],
    "LLY":     ["LLY"],
    "V":       ["V"],
    "JPM":     ["JPM"],
}

# ── Module conflict management ────────────────────────────────────────────────
CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry"
]


def _unload_conflicting_modules():
    modules_to_delete = []
    for mod_name in CONFLICTING_MODULES:
        for m in list(sys.modules.keys()):
            if m == mod_name or m.startswith(mod_name + '.'):
                modules_to_delete.append(m)
    for m in modules_to_delete:
        if m in sys.modules:
            del sys.modules[m]


def _load(folder, rel, cls, **kw):
    path_added = False
    try:
        root = _ECO_ROOT / folder
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        import importlib.util
        path = root / rel
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # type: ignore
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except Exception as exc:
        log.warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def _call_with_timeout(fn, timeout_sec: float, *, work_key=None,
                      reason_out=None) -> Tuple[Any, bool]:
    """Run a bounded Oracle proposal on a daemon thread with a caller timeout.

    Python cannot stop a thread blocked in Oracle/driver code. Keep its
    capacity reserved, reject additional work when all slots are occupied,
    and optionally prevent overlapping calls for the same work key. Daemon
    workers ensure an abandoned, read-only proposal cannot hold process
    shutdown open. Results arriving after timeout are discarded by the caller.
    """
    global _symbol_slots
    with _symbol_executor_lock:
        if _symbol_slots is None:
            max_workers = max(1, int(os.getenv("ORACLE_SYMBOL_MAX_WORKERS", "4")))
            _symbol_slots = threading.BoundedSemaphore(max_workers)
        if work_key is not None:
            previous = _symbol_futures.get(work_key)
            if previous is not None and not previous.done():
                if reason_out is not None:
                    reason_out.append("previous_worker_active")
                log.warning("Oracle proposal skipped for %s: previous timed-out worker is still running",
                            work_key[-1] if isinstance(work_key, tuple) else work_key)
                return None, True
            if previous is not None:
                _symbol_futures.pop(work_key, None)
        if not _symbol_slots.acquire(blocking=False):
            if reason_out is not None:
                reason_out.append("worker_capacity_full")
            log.warning("Oracle proposal skipped for %s: all symbol worker slots are occupied",
                        work_key[-1] if isinstance(work_key, tuple) else work_key)
            return None, True
        fut = _cf.Future()
        if work_key is not None:
            _symbol_futures[work_key] = fut

        def _release_slot(completed):
            with _symbol_executor_lock:
                if work_key is not None and _symbol_futures.get(work_key) is completed:
                    _symbol_futures.pop(work_key, None)
                _symbol_slots.release()

        fut.add_done_callback(_release_slot)

        def _run():
            if not fut.set_running_or_notify_cancel():
                return
            try:
                result = fn()
            except BaseException as exc:
                fut.set_exception(exc)
            else:
                fut.set_result(result)

        worker = threading.Thread(
            target=_run,
            name="oracle-symbol-proposal",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            if work_key is not None and _symbol_futures.get(work_key) is fut:
                _symbol_futures.pop(work_key, None)
            _symbol_slots.release()
            raise
    try:
        result = fut.result(timeout=timeout_sec)
    except _cf.TimeoutError:
        if reason_out is not None:
            reason_out.append("deadline_exceeded")
        log.warning("Oracle proposal exceeded %.1fs for %s; daemon worker remains active",
                    timeout_sec, work_key[-1] if isinstance(work_key, tuple) else work_key)
        return None, True
    except Exception:
        raise
    return result, False


def _duplicate_broker_symbol_mappings(symbols, mapper) -> Dict[str, str]:
    """Map duplicate canonical symbols to the first canonical using that instrument."""
    first_by_broker: Dict[str, str] = {}
    duplicates: Dict[str, str] = {}
    for symbol in symbols:
        broker_symbol = mapper.translate(symbol)
        if broker_symbol is None:
            continue
        broker_key = broker_symbol.upper()
        first = first_by_broker.get(broker_key)
        if first is None:
            first_by_broker[broker_key] = symbol
        else:
            duplicates[symbol] = first
    return duplicates


class SymbolMapper:
    def __init__(self, broker_overrides=None):
        self._map: Dict[str, str] = {}
        self._broker_symbols: List[str] = []
        self._env_overrides = self._parse_env_overrides()
        self._broker_overrides = {
            k.upper(): v for k, v in (broker_overrides or _CTRADER_BROKER_MAP).items()
        }

    @staticmethod
    def _parse_env_overrides():
        raw = os.getenv("BROKER_SYMBOL_MAP", "").strip()
        result = {}
        if not raw:
            return result
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            k, v = pair.split(":", 1)
            k, v = k.strip().upper(), v.strip()
            if k and v:
                result[k] = v
        return result

    def build(self, broker_symbols, oracle_symbols):
        self._broker_symbols = [s.upper() for s in broker_symbols]
        broker_set = set(self._broker_symbols)
        broker_orig = {s.upper(): s for s in broker_symbols}
        self._map.clear()
        for canon in oracle_symbols:
            canon_up = canon.upper()
            resolved = self._resolve_one(canon_up, broker_set, broker_orig)
            if resolved:
                self._map[canon_up] = resolved

    def _resolve_one(self, canon, broker_set, broker_orig):
        if canon in self._env_overrides:
            candidate = self._env_overrides[canon].upper()
            if candidate in broker_set:
                return broker_orig[candidate]
        if canon in broker_set:
            return broker_orig[canon]
        if canon in self._broker_overrides:
            candidate = self._broker_overrides[canon].upper()
            if candidate in broker_set:
                return broker_orig[candidate]
        for alias in _FALLBACK_ALIASES.get(canon, []):
            if alias.upper() in broker_set:
                return broker_orig[alias.upper()]
        for bsym in broker_set:
            if len(bsym) >= 4 and bsym.startswith(canon) and len(bsym) <= len(canon) + 4:
                return broker_orig[bsym]
        for bsym in broker_set:
            if len(bsym) >= 5 and canon.startswith(bsym):
                return broker_orig[bsym]
        for bsym in broker_set:
            if len(bsym) >= 4 and canon in bsym and len(bsym) <= len(canon) + 6:
                return broker_orig[bsym]
        return None

    def translate(self, oracle_symbol):
        return self._map.get(oracle_symbol.upper())

    def is_mapped(self, oracle_symbol):
        return oracle_symbol.upper() in self._map

    def log_map(self, oracle_symbols):
        print("\n── cTrader Broker Symbol Map ───────────────────────────")
        for canon in oracle_symbols:
            broker = self._map.get(canon.upper())
            if broker:
                src = "env" if canon.upper() in self._env_overrides else \
                      "cfg" if canon.upper() in self._broker_overrides else "auto"
                same = " (same)" if broker.upper() == canon.upper() else ""
                print(f"  {canon:12s} -> {broker}{same}  [{src}]")
            else:
                print(f"  {canon:12s} -> *** UNMAPPED ***")
        print("────────────────────────────────────────────────────────\n")


def _is_trading_disabled_rejection(result: Dict[str, Any]) -> bool:
    if result.get("status") != "rejected":
        return False
    reason = str(result.get("reason", "")).strip()
    error_code = reason.split(":", 1)[0].strip().upper()
    return error_code == "TRADING_DISABLED"


class CTraderDemoTrader:
    """Demo trader for cTrader. Same pipeline as DemoTrader, CTraderBroker adapter."""

    def __init__(self, symbols, interval_sec=300, session_max_loss_pct=0.05,
                 max_trades=None, confirm_live=False):
        self.symbols = [s.upper() for s in symbols]
        self.interval = interval_sec
        self.session_max_loss_pct = session_max_loss_pct
        self.max_trades = max_trades
        self.confirm_live = confirm_live
        self.signal_only = signal_only_enabled("ORACLE_CTRADER_SIGNAL_ONLY")
        self.telegram = TelegramNotifier()
        if self.signal_only and not self.telegram.enabled:
            log.warning("Signal-only mode is ON, but Telegram credentials are missing; "
                        "approved cTrader signals will be printed locally only")
        # Shadow mode supplements live cTrader execution after its configured
        # live-position capacity is reached.
        self.shadow_mode = os.getenv("ORACLE_CTRADER_SHADOW_MODE", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self.broker = CTraderBroker()
        self._trades_this_session = 0
        self._start_equity: Optional[float] = None
        self._risk_day_key = time.strftime("%Y-%m-%d", time.gmtime())
        self._risk_day_start_equity: Optional[float] = None
        self._account_risk_state: Optional[AccountRiskState] = None
        self._open_context: Dict[str, Dict] = {}
        self._trading_disabled_symbols = set()

        self._symbol_timeout = float(
            os.getenv("ORACLE_SYMBOL_TIMEOUT_SEC", str(_DEFAULT_SYMBOL_TIMEOUT)))

        _ks_ttl = float(os.getenv("KILL_SWITCH_EQUITY_CACHE_SEC", "60"))
        self._ks_equity_ttl = _ks_ttl
        self._ks_equity_cache: Optional[float] = None
        self._ks_equity_ts: float = 0.0
        self._ks_check_unavailable = False
        self._ks_fired = False
        self._dashboard_positions: List[Dict[str, Any]] = []
        self._dashboard_positions_updated_at_unix: Optional[float] = None
        self._dashboard_closed_trades: List[Dict[str, Any]] = []
        self._dashboard_history_status = "pending"
        self._dashboard_history_updated_at_unix: Optional[float] = None
        self._dashboard_history_lookback_days = 90
        self._dashboard_history_lock = threading.Lock()
        self._dashboard_snapshot_lock = threading.Lock()
        self._dashboard_history_last_started = 0.0

        self._sym_mapper = SymbolMapper()

        self._trade_manager = TradeManager(TradeManagerConfig())
        self._trade_learning = TradeLearningEngine()
        self._managed_positions: Dict[str, Position] = {}
        self._manage_interval = float(
            os.getenv("ORACLE_MANAGE_INTERVAL_SEC", str(_DEFAULT_MANAGE_INTERVAL)))
        self._manage_lock = threading.Lock()
        self._manage_stop_event = threading.Event()
        self._manage_thread: Optional[threading.Thread] = None
        self._position_state_lock = threading.Lock()
        self._broker_position_signature: Optional[Tuple[Tuple[str, str], ...]] = None
        self._capacity_release_event = threading.Event()

        _unload_conflicting_modules()
        self.chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                               storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
        _unload_conflicting_modules()
        self.sentinel = _load("Sentinel", "agents/sentinel_agent.py", "SentinelAgent",
                              chronicle_client=self.chronicle)
        _unload_conflicting_modules()
        self.pulse = _load("Pulse", "agents/pulse_agent.py", "PulseAgent",
                           chronicle_client=self.chronicle)
        _unload_conflicting_modules()
        self.atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
        _unload_conflicting_modules()
        from agents.oracle_agent import OracleAgent  # type: ignore
        self.oracle = OracleAgent(
            chronicle_client=self.chronicle, sentinel_client=self.sentinel,
            pulse_client=self.pulse, atlas_client=self.atlas)
        # The active RL risk manager is the authority for concurrent positions.
        # Keep any explicit CLI override for compatibility; otherwise mirror
        # RL_MAX_POSITIONS as loaded by Oracle's risk configuration.
        if self.max_trades is None:
            self.max_trades = max(0, int(self.oracle.risk.max_positions))
        self.oracle.start()
        self._trade_learning.confidence = self.oracle._champion_confidence

        self._pos_log = ChroniclePositionLog(
            chronicle_agent=self.chronicle, trader_id=_TRADER_ID)
        log.info("CTraderDemoTrader: TRADER_ID=%r", _TRADER_ID)

    def connect(self):
        # Oracle's research and fill-observation paths use the same account.
        # Share this authenticated client instead of constructing a second
        # CTraderBroker, which would attempt to start Twisted's global reactor
        # in another thread and duplicate application/account authorization.
        self.oracle._forge_ctrader_broker = self.broker
        status = self.broker.connect()
        if status.get("connected"):
            starting_equity = status.get("equity") or status.get("balance")
            if (status.get("account_info_available") is True
                    and isinstance(starting_equity, (int, float))
                    and math.isfinite(starting_equity)
                    and starting_equity > 0):
                self._start_equity = float(starting_equity)
                self._ks_equity_cache = self._start_equity
                self._ks_equity_ts = time.time()
                try:
                    self._account_risk_state = AccountRiskState({
                        "broker": "ctrader", "login": status.get("login"),
                        "server": status.get("server"), "account_type": status.get("account_type"),
                        "currency": status.get("currency"),
                    })
                    risk_state = self._account_risk_state.load_or_initialize(self._start_equity)
                    self._risk_day_key = risk_state["utc_day"]
                    self._risk_day_start_equity = risk_state["day_start_equity"]
                    self._start_equity = risk_state["day_start_equity"]
                    self._ks_fired = risk_state["kill_switch_latched"]
                except Exception as exc:
                    self._ks_check_unavailable = True
                    self._start_equity = None
                    log.error("Cannot restore cTrader account risk guard state; entries are blocked: %s",
                              type(exc).__name__)
                log.info("Connected: %s account, equity %.2f %s",
                         status["account_type"], self._start_equity,
                         status.get("currency", ""))
            else:
                self._start_equity = None
                log.warning(
                    "Connected to cTrader but initial account equity is unavailable; "
                    "new entries will wait for a valid account refresh")
            self._persist_dashboard_snapshot()
            self._build_symbol_map()
            mapped_symbols = [self._sym_mapper.translate(symbol) for symbol in self.symbols]
            try:
                self.broker.load_market_schedules([name for name in mapped_symbols if name])
            except Exception as exc:
                log.warning("Could not load broker market schedules; new entries will fail closed: %s", exc)
        else:
            log.warning("cTrader not connected: %s", status.get("reason"))
            self._build_symbol_map(broker_symbols=[])
        return status

    def _persist_dashboard_snapshot(self, positions=None) -> None:
        """Persist only account facts confirmed by this authenticated cTrader session."""
        status = self.broker.status.to_dict()
        balance = status.get("balance")
        equity = status.get("equity")
        if (status.get("account_info_available") is not True
                or not isinstance(balance, (int, float))
                or not math.isfinite(balance) or balance <= 0
                or not isinstance(equity, (int, float))
                or not math.isfinite(equity) or equity <= 0):
            return

        if positions is not None:
            normalized_positions = []
            for position in positions:
                if not isinstance(position, dict):
                    continue
                normalized_positions.append({
                    "ticket": position.get("ticket"),
                    "symbol": str(position.get("symbol") or "").upper(),
                    "direction": str(position.get("type") or "").lower(),
                    "volume": position.get("volume"),
                    "entry_price": position.get("price_open", position.get("price")),
                    "stop_loss": position.get("sl"),
                    "take_profit": position.get("tp"),
                })
            self._dashboard_positions = normalized_positions
            self._dashboard_positions_updated_at_unix = time.time()

        now = time.time()
        snapshot = {
            "schema": "oracle.ctrader_dashboard_snapshot.v1",
            "source": "authenticated_ctrader_open_api",
            "account_type": status.get("account_type"),
            "account_info_available": True,
            "connected": bool(status.get("connected")),
            "balance": float(balance),
            "equity": float(equity),
            "currency": str(status.get("currency") or "").upper(),
            "account_updated_at_unix": float(self._ks_equity_ts or now),
            "positions": self._dashboard_positions,
            "positions_updated_at_unix": self._dashboard_positions_updated_at_unix,
            "history_status": self._dashboard_history_status,
            "history_lookback_days": self._dashboard_history_lookback_days,
            "history_updated_at_unix": self._dashboard_history_updated_at_unix,
            "closed_trades": self._dashboard_closed_trades,
            "snapshot_written_at_unix": now,
        }
        try:
            _DASHBOARD_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = _DASHBOARD_SNAPSHOT_PATH.with_suffix(
                _DASHBOARD_SNAPSHOT_PATH.suffix + f".{os.getpid()}.tmp")
            with self._dashboard_snapshot_lock:
                tmp_path.write_text(json.dumps(snapshot, allow_nan=False), encoding="utf-8")
                os.replace(tmp_path, _DASHBOARD_SNAPSHOT_PATH)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not persist verified cTrader dashboard snapshot: %s",
                        type(exc).__name__)

    def _schedule_dashboard_history_refresh(self) -> None:
        """Fetch verified close-deal outcomes off the trading/management threads."""
        configured_interval = max(120, _env_int("ORACLE_CTRADER_DASHBOARD_HISTORY_REFRESH_SEC", 900))
        interval = configured_interval if self._dashboard_history_status == "complete" else min(configured_interval, 120)
        now = time.time()
        if now - self._dashboard_history_last_started < interval:
            return
        if not self._dashboard_history_lock.acquire(blocking=False):
            return
        self._dashboard_history_last_started = now
        threading.Thread(target=self._refresh_dashboard_history, daemon=True,
                         name="ctrader-dashboard-history").start()

    def _refresh_dashboard_history(self) -> None:
        try:
            days = max(1, min(3650, _env_int("ORACLE_CTRADER_DASHBOARD_HISTORY_DAYS", 90)))
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - days * 24 * 60 * 60 * 1000
            result = self.broker.historical_deals(start_ms, now_ms, max_rows=10000, timeout=30.0)
            if not isinstance(result, dict) or result.get("status") != "complete":
                self._dashboard_history_status = "incomplete"
                self._dashboard_history_updated_at_unix = time.time()
                log.warning("cTrader dashboard deal history is unavailable or incomplete")
                self._persist_dashboard_snapshot()
                return

            current_positions = self.broker.positions()
            if not getattr(self.broker, "_positions_last_refresh_ok", True):
                self._dashboard_history_status = "incomplete"
                self._dashboard_history_updated_at_unix = time.time()
                log.warning("cTrader dashboard history omitted: current position reconciliation failed")
                self._persist_dashboard_snapshot()
                return
            open_ids = {int(p["ticket"]) for p in current_positions if p.get("ticket") is not None}
            grouped: Dict[int, Dict[str, Any]] = {}
            for deal in result.get("deals", []):
                if not isinstance(deal, dict) or deal.get("deal_status") not in (2, 3):
                    continue
                close = deal.get("close_detail")
                position_id = int(deal.get("position_id") or 0)
                if not isinstance(close, dict) or not position_id:
                    continue
                digits = close.get("money_digits")
                if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 12:
                    continue
                scale = 10 ** digits
                record = grouped.setdefault(position_id, {
                    "position_id": position_id,
                    "symbol": str(deal.get("symbol") or "").upper(),
                    "gross_profit": 0.0,
                    "swap": 0.0,
                    "commission": 0.0,
                    "conversion_fee": 0.0,
                    "closed_at_unix": 0.0,
                    "closed_deal_count": 0,
                })
                record["gross_profit"] += float(close.get("gross_profit_raw") or 0) / scale
                record["swap"] += float(close.get("swap_raw") or 0) / scale
                record["commission"] += float(close.get("commission_raw") or 0) / scale
                record["conversion_fee"] += float(close.get("pnl_conversion_fee_raw") or 0) / scale
                record["closed_deal_count"] += 1
                record["closed_at_unix"] = max(
                    record["closed_at_unix"],
                    float(deal.get("execution_timestamp_ms") or 0) / 1000.0,
                )

            closed_trades = []
            for position_id, record in grouped.items():
                if position_id in open_ids:
                    continue
                net = (record["gross_profit"] + record["swap"]
                       + record["commission"] + record["conversion_fee"])
                closed_trades.append({
                    **record,
                    "net_pnl": round(net, 8),
                    "outcome": "win" if net > 0 else "loss" if net < 0 else "breakeven",
                    "currency": str(self.broker.status.currency or "").upper(),
                })
            closed_trades.sort(key=lambda item: (item["closed_at_unix"], item["position_id"]))
            self._dashboard_closed_trades = closed_trades[-1000:]
            self._dashboard_history_lookback_days = days
            self._dashboard_history_status = "complete"
            self._dashboard_history_updated_at_unix = time.time()
            self._persist_dashboard_snapshot(positions=current_positions)
            log.info("cTrader dashboard history refreshed: %d completed positions in last %d days",
                     len(self._dashboard_closed_trades), days)
        except Exception as exc:
            self._dashboard_history_status = "error"
            self._dashboard_history_updated_at_unix = time.time()
            log.warning("cTrader dashboard history refresh failed: %s", type(exc).__name__)
            self._persist_dashboard_snapshot()
        finally:
            self._dashboard_history_lock.release()

    def _get_broker_symbols(self):
        try:
            if hasattr(self.broker, '_symbol_id_by_name') and self.broker._symbol_id_by_name:
                return list(self.broker._symbol_id_by_name.keys())
            return []
        except Exception as exc:
            log.warning("Could not fetch cTrader symbol list: %s", exc)
            return []

    def _build_symbol_map(self, broker_symbols=None):
        if broker_symbols is None:
            broker_symbols = self._get_broker_symbols()
        if not broker_symbols:
            log.warning("Broker returned 0 symbols — using overrides + canonical names")
            broker_symbols = list(self.symbols) + list(_CTRADER_BROKER_MAP.values())
        self._sym_mapper.build(broker_symbols, self.symbols)
        self._sym_mapper.log_map(self.symbols)

    def _get_open_position(self, broker_sym, positions=None):
        try:
            if positions is None:
                positions = self.broker.positions(symbol=broker_sym)
            else:
                prefix = broker_sym.upper()[:6]
                positions = [position for position in positions
                             if position.get("symbol", "").upper().startswith(prefix)]
        except Exception as exc:
            log.warning("Could not fetch positions for %s: %s", broker_sym, exc)
            return None
        if not positions:
            return None
        return positions[0]

    def _observe_broker_positions(self, positions) -> bool:
        """Flag a capacity release when a fresh broker snapshot loses positions."""
        signature = tuple(sorted(
            (str(position.get("ticket") or position.get("id") or ""),
             str(position.get("symbol") or "").upper())
            for position in positions if isinstance(position, dict)
        ))
        with self._position_state_lock:
            previous = self._broker_position_signature
            self._broker_position_signature = signature
            released = previous is not None and len(signature) < len(previous)
        if released:
            self._capacity_release_event.set()
        return released

    def _sync_risk_positions_from_broker(self, positions) -> None:
        """Make cTrader's confirmed positions authoritative for the risk book.

        Keep realized PnL, cooldowns, and daily trade counts intact; only replace
        the in-memory open-position list so closed positions stop consuming slots.
        """
        observed = []
        for position in positions:
            if not isinstance(position, dict):
                continue
            broker_symbol = str(position.get("symbol") or "").upper()
            if not broker_symbol:
                continue
            symbol = broker_symbol
            for candidate in self.symbols:
                mapped = self._sym_mapper.translate(candidate)
                if mapped and (broker_symbol == mapped.upper()
                               or broker_symbol.startswith(mapped.upper()[:6])):
                    symbol = candidate
                    break
            direction = str(position.get("type") or "").lower()
            if direction in {"buy", "long", "1"}:
                direction = "long"
            elif direction in {"sell", "short", "2"}:
                direction = "short"
            else:
                log.warning("Ignoring cTrader position with unknown direction: %s", broker_symbol)
                continue
            try:
                entry = float(position.get("price_open") or position.get("price") or 0.0)
                size = float(position.get("volume") or 0.0)
                stop = float(position.get("sl") or 0.0)
                target = float(position.get("tp") or 0.0)
            except (TypeError, ValueError):
                log.warning("Ignoring malformed cTrader position for %s", broker_symbol)
                continue
            if not math.isfinite(entry) or not math.isfinite(size) or entry <= 0 or size <= 0:
                log.warning("Ignoring cTrader position with invalid size/entry for %s", broker_symbol)
                continue
            observed.append(RiskPosition(symbol, direction, size, entry, stop, target))

        # Risk evaluation and the CTM both touch this account's open book.
        with self._manage_lock:
            self.oracle.risk.portfolio.positions = observed

    def _register_position(self, symbol, direction, entry_price, stop, target, confidence, size=0.0,
                            news_level="none", news_reason="", social_level="none", social_reason=""):
        try:
            sig = self.oracle.act("trade.signal", {"symbol": symbol, "_sender": "ctrader_demo"})
            entry_regime = (sig or {}).get("regime") or "ranging"
            entry_atr = (sig or {}).get("atr")
        except Exception as exc:
            log.warning("[%s] could not fetch entry regime: %s", symbol, exc)
            entry_regime = "ranging"
            entry_atr = None
        entry_streams = dict(self._open_context.get(symbol, {}) or {})
        technical_stream = entry_streams.get("technical", {})
        strategy_id = technical_stream.get("genome") if isinstance(technical_stream, dict) else None
        timeframe_policy_id = technical_stream.get("timeframe_policy_id") if isinstance(technical_stream, dict) else None
        entry_term_evidence = {"bullish": [], "bearish": []}
        try:
            news = self.sentinel.act("news.credibility", {"topics": [symbol], "_sender": "ctrader_demo"}) \
                if self.sentinel else None
            for a in (news or {}).get("articles", []):
                mt = a.get("matched_terms") or {}
                entry_term_evidence["bullish"].extend(mt.get("bullish", []))
                entry_term_evidence["bearish"].extend(mt.get("bearish", []))
        except Exception:
            pass
        dir_norm = Direction.BUY if direction in ("long", "buy") else Direction.SELL
        with self._manage_lock:
            self._managed_positions[symbol] = Position(
                symbol=symbol, direction=dir_norm, entry_price=entry_price,
                initial_stop=stop, initial_target=target, entry_confidence=confidence,
                entry_regime=entry_regime, entry_streams=entry_streams, entry_atr=entry_atr,
                entry_term_evidence=entry_term_evidence, strategy_id=strategy_id,
                timeframe_policy_id=timeframe_policy_id,
                execution_environment=str(getattr(self.broker.status, "account_type", "unknown")))
            # Trade Journal (roadmap Phase 3 groundwork): reasons for
            # entering this trade, wired to Chronicle, linked to its
            # eventual win/loss outcome via the returned journal_id.
            self._managed_positions[symbol].journal_id = self.oracle.trade_journal.log_entry(
                symbol, direction, confidence, entry_regime,
                news_level, news_reason, social_level, social_reason,
                entry_streams, size)
        risk_direction = "long" if dir_norm == Direction.BUY else "short"
        self.oracle.risk.portfolio.remove_by_symbol(symbol)
        self.oracle.risk.portfolio.positions.append(
            RiskPosition(symbol, risk_direction, size, entry_price, stop, target))
        log.info("[%s] registered with CTM: dir=%s entry=%.5f stop=%.5f target=%.5f conf=%.3f",
                 symbol, dir_norm.value, entry_price, stop, target, confidence)

    def _poll_managed_positions(self):
        if not self.broker.status.connected:
            return
        try:
            open_positions = self.broker.positions()
        except Exception as exc:
            log.warning("Could not refresh open positions for CTM: %s", exc)
            return
        if not getattr(self.broker, "_positions_last_refresh_ok", True):
            log.warning("cTrader position reconciliation failed; skipping CTM pass to avoid false closes")
            return
        self._observe_broker_positions(open_positions)
        self._sync_risk_positions_from_broker(open_positions)
        self._persist_dashboard_snapshot(positions=open_positions)
        self._schedule_dashboard_history_refresh()
        with self._manage_lock:
            symbols = list(self._managed_positions.keys())
        for symbol in symbols:
            with self._manage_lock:
                pos = self._managed_positions.get(symbol)
            if pos is None:
                continue
            broker_sym = self._sym_mapper.translate(symbol) or symbol
            live_pos = self._get_open_position(broker_sym, open_positions)
            if live_pos is None:
                log.info("[%s] no longer open — deregistering from CTM", symbol)
                try:
                    outcome = self._trade_learning.record_close_once(
                        f"{symbol}:{pos.entry_time}:broker-close",
                        self.oracle, pos, exit_price=pos.last_price,
                        exit_confidence=pos.last_confidence, exit_regime=pos.last_regime,
                        exit_reason="closed at broker (SL/TP hit or manual)")
                    if outcome is not None:
                        if outcome.won:
                            self.oracle.risk.portfolio.record_win(symbol)
                        else:
                            self.oracle.risk.portfolio.record_loss(symbol)
                except Exception as exc:
                    log.warning("[%s] Trade Learning failed on close: %s", symbol, exc)
                self.oracle.risk.portfolio.remove_by_symbol(symbol)
                with self._manage_lock:
                    self._managed_positions.pop(symbol, None)
                continue
            market_state = self.broker.market_state(broker_sym)
            if market_state["state"] in {"CLOSED", "DAILY_BREAK", "HOLIDAY", "PRE_OPEN"}:
                log.debug("[%s] position remains broker-protected; market state=%s",
                          symbol, market_state["state"])
                continue
            try:
                sig = self.oracle.act("trade.signal", {"symbol": symbol, "_sender": "ctrader_demo"})
            except Exception:
                continue
            if (sig or {}).get("status") != "complete":
                continue
            price = sig.get("last")
            confidence = (sig.get("signal") or {}).get("confidence", 0.0)
            regime = sig.get("regime") or pos.entry_regime
            current_atr = sig.get("atr")
            if price is None:
                continue
            news_assessment = assess_news_impact(self.sentinel, symbol)
            social_assessment = assess_social_risk(self.pulse, symbol)
            snap = MarketSnapshot(price=float(price), confidence=float(confidence),
                                   regime=regime, news_impact=news_assessment.level,
                                   social_risk=social_assessment.level, atr=current_atr)
            decision = self._trade_manager.evaluate(pos, snap)
            pos_id = live_pos.get("ticket") or live_pos.get("id")
            if self.signal_only:
                prior_stop = pos.current_stop
                if decision.action == Action.TIGHTEN_STOP:
                    pos.current_stop = prior_stop
                    stop_key = f"{float(decision.new_stop or 0.0):.8f}"
                    self.telegram.send(
                        format_position_alert(symbol, pos_id, "tighten_stop", decision,
                                               pos.initial_stop, pos.initial_target),
                        f"position:{pos_id}:tighten:{stop_key}",
                    )
                elif decision.action == Action.CLOSE:
                    self.telegram.send(
                        format_position_alert(symbol, pos_id, "close", decision,
                                               pos.initial_stop, pos.initial_target),
                        f"position:{pos_id}:close",
                    )
                # Signal-only management stays advisory; never change or close
                # a broker position from the cTrader management loop.
                continue
            if decision.action == Action.HOLD:
                if decision.reason != pos.last_journaled_hold_reason:
                    self.oracle.trade_journal.log_hold(
                        symbol, decision.reason, decision.current_confidence, decision.current_regime)
                    pos.last_journaled_hold_reason = decision.reason
                continue
            if decision.action == Action.TIGHTEN_STOP:
                log.info("[%s] CTM tighten #%s -> %.5f: %s",
                         symbol, pos_id, decision.new_stop, decision.reason)
                try:
                    mod = self.broker.modify_position(pos_id, stop_loss=decision.new_stop)
                    if mod.get("status") == "modified":
                        print(f"[{symbol}->{broker_sym}] CTM TIGHTEN #{pos_id} "
                              f"stop->{decision.new_stop:.5f}  {decision.reason}")
                        self._pos_log.log_modified(symbol, broker_sym, pos_id,
                                                    sl=decision.new_stop or 0.0, tp=pos.initial_target)
                    else:
                        log.warning("[%s] CTM: stop modification not confirmed #%s: %s",
                                    symbol, pos_id, mod.get("reason", "unknown broker response"))
                        print(f"[{symbol}->{broker_sym}] CTM TIGHTEN FAILED #{pos_id}: "
                              f"{mod.get('reason', 'broker did not confirm the new stop')}")
                except Exception as exc:
                    log.warning("[%s] CTM: modify failed #%s: %s", symbol, pos_id, exc)
                    print(f"[{symbol}->{broker_sym}] CTM TIGHTEN FAILED #{pos_id}: {exc}")
            elif decision.action == Action.CLOSE:
                log.info("[%s] CTM close #%s: %s", symbol, pos_id, decision.reason)
                print(f"[{symbol}->{broker_sym}] CTM CLOSE #{pos_id}  {decision.reason}")
                try:
                    close_result = self.broker.close_position(pos_id)
                    if close_result.get("status") == "closed":
                        self._pos_log.log_closed(symbol, broker_sym, pos_id, reason=decision.reason)
                        try:
                            outcome = self._trade_learning.record_close_once(
                                f"{symbol}:{pos_id}:manager-close",
                                self.oracle, pos, exit_price=snap.price,
                                exit_confidence=snap.confidence, exit_regime=snap.regime,
                                exit_reason=decision.reason)
                            if outcome is not None:
                                if outcome.won:
                                    self.oracle.risk.portfolio.record_win(symbol)
                                else:
                                    self.oracle.risk.portfolio.record_loss(symbol)
                        except Exception:
                            pass
                        self.oracle.risk.portfolio.remove_by_symbol(symbol)
                        with self._manage_lock:
                            self._managed_positions.pop(symbol, None)
                except Exception as exc:
                    log.warning("[%s] CTM: close failed #%s: %s", symbol, pos_id, exc)

    def _manage_loop(self):
        log.info("CTM thread started (interval=%.0fs)", self._manage_interval)
        while not self._manage_stop_event.is_set():
            try:
                self._poll_managed_positions()
            except Exception as exc:
                log.warning("CTM poll error: %s", exc)
            self._manage_stop_event.wait(self._manage_interval)
        log.info("CTM thread stopped")

    def start_trade_manager(self):
        if self._manage_thread is not None and self._manage_thread.is_alive():
            return
        self._manage_stop_event.clear()
        self._manage_thread = threading.Thread(target=self._manage_loop, name="ctm-poll", daemon=True)
        self._manage_thread.start()

    def stop_trade_manager(self):
        self._manage_stop_event.set()
        if self._manage_thread is not None:
            self._manage_thread.join(timeout=5)

    def _close_all_positions(self):
        if self.signal_only:
            return {"status": "signal_only", "message":
                    "Oracle is in signal-only mode; manage positions manually."}
        results = []
        try:
            for pos in self.broker.positions():
                ticket = pos.get("ticket")
                if ticket is not None:
                    try:
                        results.append(self.broker.close_position(ticket))
                    except Exception as exc:
                        results.append({"status": "error", "ticket": ticket, "reason": str(exc)})
            return {"status": "done", "closed": len(results), "results": results}
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}

    def run(self, cycles=None):
        status = self.connect()
        if not status.get("connected"):
            print("Cannot start trading:", status.get("reason"))
            print("Oracle will still compute signals; execution disabled until cTrader connects.")
        print(f"\ncTrader demo trader started. Symbols={self.symbols} interval={self.interval}s")
        print(f"Account: {status.get('account_type')} | login={status.get('login')} | "
              f"currency={status.get('currency', '?')}")
        print(f"Symbol timeout: {self._symbol_timeout}s | max open positions: {self.max_trades} | "
              f"signal_only={self.signal_only} | overflow shadow={self.shadow_mode}")
        print(f"CTM: polling every {self._manage_interval:.0f}s")
        print("Press Ctrl+C to stop.\n")
        self.start_trade_manager()
        cycle = 0
        try:
            while cycles is None or cycle < cycles:
                cycle += 1
                cycle_label = f"{cycle}/{cycles}" if cycles else str(cycle)
                print(f"\n{'─'*56}")
                print(f"  Cycle {cycle_label} — scanning {len(self.symbols)} symbols")
                print(f"{'─'*56}")
                summary = self._tick()
                print(f"\nCycle {cycle_label} done — scanned {summary['scanned']} symbols | "
                      f"{summary['trades']} trade(s) | {summary['holds']} hold | "
                      f"{summary['signals']} signal(s) | "
                      f"{summary['rejects']} reject | {summary['errors']} error | "
                      f"{summary['timeouts']} timeout | {summary['unmapped']} unmapped | "
                        f"{summary['shadowed']} shadowed | "
                        f"{summary['managed']} managed | {summary['market_closed']} not open")
                if summary["kill_switch"]:
                    if self.signal_only:
                        print("KILL SWITCH: session loss limit hit. Stopping signals; "
                              "signal-only mode will not close broker positions.")
                    else:
                        print("KILL SWITCH: session loss limit hit. Flattening + stopping.")
                        print(self._close_all_positions())
                    break
                if (summary.get("capacity_released")
                        and summary.get("open_positions_after_scan", 0) < self.max_trades):
                    print("A cTrader slot opened during the scan; immediately rescanning all symbols for fresh setups.")
                    continue
                if cycles is None or cycle < cycles:
                    print(f"Waiting up to {self.interval}s for the next cycle or a cTrader slot to open...")
                    self._capacity_release_event.wait(self.interval)
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            self._learn_from_closed()
            self.shutdown()

    def _tick(self):
        summary = dict(scanned=0, trades=0, signals=0, holds=0, rejects=0,
                   errors=0, timeouts=0, unmapped=0, managed=0,
                   market_closed=0, shadowed=0, kill_switch=False,
                   capacity_released=False)
        duplicate_mappings = _duplicate_broker_symbol_mappings(
            self.symbols, self._sym_mapper)
        if self._kill_switch_check():
            summary["kill_switch"] = True
            return summary
        if self._ks_check_unavailable:
            summary["errors"] += 1
            log.warning("Skipping new entries because cTrader account equity is unavailable")
            return summary
        open_positions = []
        real_position_count = 0
        active_shadow_symbols = set()
        if self.broker.status.connected:
            try:
                open_positions = self.broker.positions()
                real_position_count = len(open_positions)
            except Exception as exc:
                log.warning("Could not refresh open positions for scan: %s", exc)
                summary["errors"] += 1
                return summary
            if not getattr(self.broker, "_positions_last_refresh_ok", True):
                log.warning("cTrader position reconciliation failed; skipping new-entry scan")
                summary["errors"] += 1
                return summary
            self._observe_broker_positions(open_positions)
            self._sync_risk_positions_from_broker(open_positions)
            # This cycle starts at the beginning of a fresh full-symbol scan.
            self._capacity_release_event.clear()
            self._persist_dashboard_snapshot(positions=open_positions)
            self._schedule_dashboard_history_refresh()
        try:
            pending_shadow = self.oracle.decision_ledger.unentered_trades_pending_replay(
                max_items=5000, shadow_only=True)
            active_shadow_symbols = {
                str(item.get("symbol") or "").upper()
                for item in pending_shadow if item.get("symbol")
            }
        except Exception as exc:
            log.warning("Could not load active cTrader shadow observations: %s", type(exc).__name__)

        candidate_queue = []
        for symbol in self.symbols:
            if self._kill_switch_check():
                summary["kill_switch"] = True
                return summary
            if self._ks_check_unavailable:
                summary["errors"] += 1
                log.warning("Stopping scan because cTrader account equity is unavailable")
                return summary
            # A close can happen while a long symbol scan is in progress. Refresh
            # at the next symbol boundary rather than reusing the count captured
            # at cycle start for the rest of the scan.
            if self._capacity_release_event.is_set() and self.broker.status.connected:
                try:
                    refreshed_positions = self.broker.positions()
                except Exception as exc:
                    log.warning("Could not refresh positions after a close: %s", exc)
                    summary["errors"] += 1
                    return summary
                if not getattr(self.broker, "_positions_last_refresh_ok", True):
                    log.warning("Position refresh after close was not confirmed; stopping new-entry scan")
                    summary["errors"] += 1
                    return summary
                previous_count = real_position_count
                open_positions = refreshed_positions
                real_position_count = len(open_positions)
                self._observe_broker_positions(open_positions)
                self._sync_risk_positions_from_broker(open_positions)
                self._capacity_release_event.clear()
                if real_position_count < previous_count:
                    summary["capacity_released"] = True
                    log.info("cTrader slot released during scan: %d/%d open positions",
                             real_position_count, self.max_trades)
                self._persist_dashboard_snapshot(positions=open_positions)
            broker_sym = self._sym_mapper.translate(symbol)
            if broker_sym is None:
                print(f"[{symbol}] UNMAPPED — add BROKER_SYMBOL_MAP={symbol}:<name> to .env")
                summary["unmapped"] += 1
                continue
            duplicate_of = duplicate_mappings.get(symbol)
            if duplicate_of is not None:
                log.warning(
                    "[%s->%s] duplicate broker mapping; already scanning %s for this instrument",
                    symbol, broker_sym, duplicate_of,
                )
                print(f"[{symbol}->{broker_sym}] SKIP duplicate broker mapping "
                      f"(covered by {duplicate_of})")
                summary["holds"] += 1
                continue
            summary["scanned"] += 1
            existing_pos = self._get_open_position(broker_sym, open_positions)
            if existing_pos is not None:
                summary["managed"] += 1
                summary["holds"] += 1
                if symbol not in self._managed_positions:
                    entry_price = existing_pos.get("price_open") or existing_pos.get("price") or 0.0
                    direction = "long" if str(existing_pos.get("type", "")).lower() == "buy" else "short"
                    sl = existing_pos.get("sl") or entry_price * (0.995 if direction == "long" else 1.005)
                    tp = existing_pos.get("tp") or entry_price * (1.01 if direction == "long" else 0.99)
                    self._register_position(symbol, direction, entry_price=entry_price,
                                             stop=sl, target=tp, confidence=0.5,
                                             size=existing_pos.get("volume", 0.0))
                continue
            if broker_sym.upper() in self._trading_disabled_symbols:
                log.info("[%s->%s] new entry skipped — cTrader previously reported "
                         "TRADING_DISABLED for this instrument",
                         symbol, broker_sym)
                print(f"[{symbol}->{broker_sym}] SKIP trading disabled by cTrader")
                summary["market_closed"] += 1
                continue
            capacity_full = real_position_count >= self.max_trades
            if capacity_full and not self.shadow_mode and not self.signal_only:
                log.info("[%s] new entry skipped; configured cTrader position limit reached", symbol)
                summary["holds"] += 1
                continue
            if capacity_full and symbol in active_shadow_symbols:
                log.info("[%s] duplicate shadow setup skipped; an unresolved shadow position already exists", symbol)
                summary["holds"] += 1
                continue
            # The broker snapshot above is the authoritative open-position
            # state. Use Chronicle only as a short race-window guard; an old
            # unmatched event must not permanently block a fresh setup after
            # cTrader has confirmed that the position is gone.
            if self._pos_log.has_open_position(symbol, broker_sym, max_age_sec=60):
                print(f"[{symbol}->{broker_sym}] DEDUP: Chronicle shows open position")
                summary["holds"] += 1
                continue
            market_state = self.broker.market_state(broker_sym)
            if not market_state["can_open"]:
                log.info("[%s] new entry skipped — market state=%s (%s)",
                         symbol, market_state["state"], market_state["reason"])
                summary["market_closed"] += 1
                continue
            print(f"[{symbol}->{broker_sym}] evaluating entry...")
            proposal_started = time.monotonic()
            timeout_reason = []
            try:
                sig, timed_out = _call_with_timeout(
                    lambda sym=symbol: self.oracle.act(
                        "trade.propose", {"symbol": sym, "_sender": "ctrader_demo",
                                          "market_state": market_state}),
                    self._symbol_timeout,
                    work_key=(id(self), symbol), reason_out=timeout_reason)
            except Exception as exc:
                print(f"[{symbol}] ERROR  {exc}")
                summary["errors"] += 1
                continue
            if timed_out:
                elapsed = time.monotonic() - proposal_started
                reason = timeout_reason[0] if timeout_reason else "unknown"
                if reason == "deadline_exceeded":
                    print(f"[{symbol}] TIMEOUT after {elapsed:.1f}s (limit {self._symbol_timeout:.0f}s)")
                    summary["timeouts"] += 1
                else:
                    print(f"[{symbol}] SKIP after {elapsed:.1f}s (proposal {reason})")
                    summary["errors"] += 1
                continue
            status = (sig or {}).get("status")
            capacity_limited_signal = False
            if status != "complete":
                message = (sig or {}).get("message", "unknown")
                risk = (sig or {}).get("risk") or {}
                reasons = risk.get("reasons")
                shadow_candidate = (sig or {}).get("shadow_candidate")
                capacity_only_rejection = bool(reasons) and all(
                    str(reason).startswith("max positions (") for reason in reasons
                )
                if ((self.signal_only or (capacity_full and self.shadow_mode))
                        and capacity_only_rejection
                        and isinstance(shadow_candidate, dict)):
                    # The risk decision remains rejected for real execution.
                    # Reuse its non-executable candidate for shadow observation
                    # or signal-only notification, never as an order approval.
                    sig = shadow_candidate
                    status = "complete"
                    capacity_limited_signal = True
                else:
                    if "hold" in message.lower():
                        print(f"[{symbol}] HOLD   ({message})")
                        summary["holds"] += 1
                    elif "risk gate" in message.lower() or reasons:
                        conf_str = ""
                        try:
                            conf_str = f"conf={sig['signal']['confidence']:.3f}  "
                        except Exception:
                            pass
                        print(f"[{symbol}] REJECT {conf_str}reasons={reasons}")
                        summary["rejects"] += 1
                    else:
                        print(f"[{symbol}] ERROR  {message}")
                        summary["errors"] += 1
                    continue
            plan = sig["plan"]
            s = sig["signal"]
            scout_enabled = bool(sig.get(
                "scout_enabled", getattr(self.oracle, "scout_enabled", True)
            ))
            effective_confidence = float(
                sig.get("effective_confidence", s.get("confidence", 0.0)) or 0.0
            )
            selection_score = float(
                sig.get("scout_score", effective_confidence) or 0.0
            )
            score_label = "scout" if scout_enabled else "confidence"
            conf_str = (
                f"{score_label}={selection_score:.3f} "
                f"effective_conf={effective_confidence:.3f} "
                f"raw_conf={float(s.get('confidence', 0.0) or 0.0):.3f}"
            )
            if capacity_full and self.shadow_mode:
                summary["shadowed"] += 1
                shadow_reason = f"cTrader position capacity full ({real_position_count}/{self.max_trades})"
                print(f"[{symbol}->{broker_sym}] SHADOW ({shadow_reason}) direction={plan['direction']} "
                      f"{conf_str} size={plan.get('size', 0)} "
                      f"stop={plan.get('stop')} target={plan.get('target')}; no cTrader order sent")
                self._record_shadow_proposal(
                    symbol, broker_sym, sig, market_state,
                    reason_code="max_open_positions",
                    reason=shadow_reason,
                )
                active_shadow_symbols.add(symbol)
                continue
            candidate_queue.append({
                "symbol": symbol,
                "broker_symbol": broker_sym,
                "signal": sig,
                "market_state": market_state,
                "plan": plan,
                "signal_data": s,
                "scout_enabled": scout_enabled,
                "selection_score": selection_score,
                "effective_confidence": effective_confidence,
                "conf_str": conf_str,
                "capacity_limited_signal": capacity_limited_signal,
            })

        # Decide across the complete scan before consuming any broker slot.
        # This prevents watchlist order from deciding which setup wins.
        candidate_queue.sort(
            key=lambda row: (
                row["selection_score"] if row["scout_enabled"] else row["effective_confidence"],
                row["effective_confidence"],
            ),
            reverse=True,
        )
        if self.signal_only:
            for row in candidate_queue:
                symbol, broker_sym = row["symbol"], row["broker_symbol"]
                plan, sig = row["plan"], row["signal"]
                alert = format_entry_alert(symbol, sig)
                if row["capacity_limited_signal"]:
                    alert = alert.replace(
                        "ORACLE APPROVED SIGNAL",
                        "ORACLE SIGNAL — BROKER CAPACITY BLOCKED",
                        1,
                    )
                    alert += "\nRisk Manager blocked execution because the position limit is full."
                delivered = self.telegram.send(alert, f"entry:{symbol}:{plan.get('direction', '')}")
                summary["signals"] += 1
                delivery = "sent" if delivered else (
                    "not sent" if self.telegram.enabled else "not configured"
                )
                log.info("[%s->%s] SIGNAL ONLY direction=%s %s Telegram=%s",
                         symbol, broker_sym, plan.get("direction"), row["conf_str"], delivery)
                if not self.telegram.enabled:
                    print(alert)
        elif candidate_queue and self.broker.status.connected:
            try:
                # Refresh once after scoring, so externally opened/closed
                # positions during the scan cannot make the slot count stale.
                refreshed_positions = self.broker.positions()
                if not getattr(self.broker, "_positions_last_refresh_ok", True):
                    raise RuntimeError("broker position reconciliation was not confirmed")
                open_positions = refreshed_positions
                real_position_count = len(open_positions)
                self._observe_broker_positions(open_positions)
                self._sync_risk_positions_from_broker(open_positions)
                self._persist_dashboard_snapshot(positions=open_positions)
            except Exception as exc:
                log.warning("Cannot rank-to-execution safely without a fresh position snapshot: %s", exc)
                summary["errors"] += 1
                candidate_queue = []

            available_slots = max(0, self.max_trades - real_position_count)
            execution_outcome_uncertain = False
            for row in candidate_queue:
                symbol, broker_sym = row["symbol"], row["broker_symbol"]
                sig, plan = row["signal"], row["plan"]
                if execution_outcome_uncertain:
                    summary["holds"] += 1
                    continue
                if available_slots <= 0:
                    if self.shadow_mode:
                        summary["shadowed"] += 1
                        reason = f"cTrader position capacity full ({real_position_count}/{self.max_trades}); out-ranked after full scan"
                        self._record_shadow_proposal(
                            symbol, broker_sym, sig, row["market_state"],
                            reason_code="max_open_positions", reason=reason,
                        )
                        active_shadow_symbols.add(symbol)
                    else:
                        summary["holds"] += 1
                    continue
                if self._get_open_position(broker_sym, open_positions) is not None:
                    summary["holds"] += 1
                    continue
                broker_plan = dict(plan)
                broker_plan["broker_symbol"] = broker_sym
                result = self.broker.place_order(broker_plan, human_confirm=self.confirm_live)
                res_status = result.get("status", "unknown")
                res_reason = result.get("reason", "")
                print(f"[{symbol}->{broker_sym}] TRADE direction={plan['direction']} {row['conf_str']}"
                      f" -> {res_status} {res_reason}")
                if res_status != "filled":
                    if res_status == "unknown":
                        execution_outcome_uncertain = True
                        summary["errors"] += 1
                        log.error("[%s] cTrader order outcome uncertain; stopping new order submissions for this scan",
                                  symbol)
                        try:
                            refreshed = self.broker.positions()
                            if getattr(self.broker, "_positions_last_refresh_ok", True):
                                open_positions = refreshed
                                real_position_count = len(refreshed)
                                self._observe_broker_positions(refreshed)
                                self._sync_risk_positions_from_broker(refreshed)
                        except Exception as exc:
                            log.error("Position reconciliation after uncertain cTrader order failed: %s",
                                      type(exc).__name__)
                    else:
                        summary["rejects"] += 1
                    if _is_trading_disabled_rejection(result):
                        self._trading_disabled_symbols.add(broker_sym.upper())
                    continue
                available_slots -= 1
                real_position_count += 1
                self._trades_this_session += 1
                summary["trades"] += 1
                execution_proposal = {**sig, "plan": {**plan, "broker_symbol": broker_sym}}
                self.telegram.send(
                    format_execution_alert(symbol, execution_proposal, result, "cTrader"),
                    f"filled:ctrader:{result.get('order') or result.get('ticket') or symbol + ':' + str(plan.get('direction', ''))}",
                )
                self._open_context[symbol] = dict(sig.get("_streams", {}) or {})
                self._open_context[symbol]["market_context"] = {
                    key: row["market_state"].get(key) for key in
                    ("state", "sessions", "overlap", "schedule_timezone", "updated_at_utc")
                }
                ticket = result.get("order") or result.get("ticket") or 0
                fill_volume = result.get("volume_units", result.get("volume", plan.get("size", 0)))
                self._pos_log.log_opened(
                    symbol, broker_sym, ticket, direction=plan["direction"],
                    volume=fill_volume, price=result.get("price", 0.0),
                    sl=plan.get("stop", 0.0), tp=plan.get("target", 0.0),
                    execution={
                        "broker": "ctrader",
                        "decision_entry_price": plan.get("entry"),
                        "fill_price": result.get("price"),
                        "adverse_slippage_price": adverse_slippage_price(
                            plan["direction"], plan.get("entry"), result.get("price")),
                        "requested_volume": result.get("requested_volume_units", plan.get("size")),
                        "filled_volume": fill_volume,
                        "volume_protocol_cents": result.get("volume_protocol_cents"),
                        "risk_budget_account_currency": plan.get("risk_amount"),
                    },
                )
                open_positions.append({
                    "symbol": broker_sym, "ticket": ticket,
                    "type": "buy" if plan["direction"] in ("long", "buy") else "sell",
                    "price_open": result.get("price") or plan.get("entry", 0.0),
                    "volume": fill_volume, "sl": plan.get("stop", 0.0),
                    "tp": plan.get("target", 0.0),
                })
                self._register_position(
                    symbol, plan["direction"],
                    entry_price=result.get("price") or plan.get("entry", 0.0),
                    stop=plan.get("stop", 0.0), target=plan.get("target", 0.0),
                    confidence=row["signal_data"].get("confidence", 0.0),
                    size=fill_volume, news_level=sig.get("news_impact", "none"),
                    news_reason=sig.get("news_reason", ""),
                    social_level=sig.get("social_risk", "none"),
                    social_reason=sig.get("social_reason", ""),
                )
                self.oracle.risk.portfolio.record_trade_opened(symbol)
        elif candidate_queue:
            for row in candidate_queue:
                print(f"[{row['symbol']}->{row['broker_symbol']}] SIGNAL READY; cTrader is disconnected, no order sent")
                summary["errors"] += 1
        summary["open_positions_after_scan"] = real_position_count
        # Replay only these broker-capacity shadow observations. Settled
        # outcomes update Scout's shadow evidence; they never alter live
        # conviction floors or open cTrader positions.
        if self.shadow_mode:
            try:
                has_pending_shadow = bool(self.oracle.decision_ledger.unentered_trades_pending_replay(
                    max_items=1, shadow_only=True))
                if has_pending_shadow:
                    replay_limit = _env_int("ORACLE_CTRADER_SHADOW_REPLAY_BATCH", 5)
                    replay = self.oracle.act("cio.counterfactual_replay", {
                        "max_records": max(1, replay_limit),
                        "shadow_only": True,
                        "_sender": "ctrader_demo_shadow_replay",
                    })
                    details = replay.get("data") if isinstance(replay, dict) else None
                    log.info("cTrader shadow replay cycle: %s", details or (replay or {}).get("status"))
            except Exception as exc:
                log.warning("cTrader shadow replay pass unavailable: %s", type(exc).__name__)
        if self._kill_switch_check():
            summary["kill_switch"] = True
        elif self._ks_check_unavailable:
            summary["errors"] += 1
        return summary

    def _kill_switch_check(self):
        if self._ks_fired:
            return True
        self._ks_check_unavailable = False
        if not self.broker.status.connected:
            self._ks_check_unavailable = True
            return False
        _now = time.time()
        if (self._start_equity is None or self._ks_equity_cache is None
                or (_now - self._ks_equity_ts) >= self._ks_equity_ttl):
            acct = self.broker.account()
            equity = acct.get("equity")
            if (acct.get("account_info_available") is not True
                    or not isinstance(equity, (int, float))
                    or not math.isfinite(equity)
                    or equity <= 0):
                self._ks_check_unavailable = True
                log.warning(
                    "Kill-switch check deferred: cTrader returned no valid account equity; "
                    "retaining last known value")
                return False
            if self._start_equity is None:
                self._start_equity = float(equity)
                log.info("Starting session equity baseline set to %.2f", self._start_equity)
            self._ks_equity_cache = float(equity)
            self._ks_equity_ts = _now
            self._persist_dashboard_snapshot()
        equity = self._ks_equity_cache
        if self._start_equity is None:
            return False
        today = time.strftime("%Y-%m-%d", time.gmtime(_now))
        if today != self._risk_day_key or self._risk_day_start_equity is None:
            try:
                state = self._account_risk_state.load_or_initialize(float(equity), now=_now)
                self._risk_day_key = state["utc_day"]
                self._risk_day_start_equity = state["day_start_equity"]
                self._start_equity = state["day_start_equity"]
                self._ks_fired = state["kill_switch_latched"]
            except Exception as exc:
                self._ks_check_unavailable = True
                log.error("Cannot roll cTrader account risk state to a new UTC day: %s",
                          type(exc).__name__)
                return False
        self.oracle.risk.sync_broker_account(
            equity=float(equity),
            daily_pnl=float(equity) - float(self._risk_day_start_equity),
        )
        loss = (self._start_equity - equity) / self._start_equity
        if loss >= self.session_max_loss_pct:
            self._ks_fired = True
        try:
            self._ks_fired = self._account_risk_state.save({
                "utc_day": self._risk_day_key,
                "day_start_equity": self._risk_day_start_equity,
                "kill_switch_latched": self._ks_fired,
            })
        except Exception as exc:
            self._ks_check_unavailable = True
            log.error("Cannot persist cTrader account risk state; entries are blocked: %s",
                      type(exc).__name__)
            return False
        return self._ks_fired

    def _learn_from_closed(self):
        for canon_sym, streams in self._open_context.items():
            broker_sym = self._sym_mapper.translate(canon_sym) or canon_sym
            poss = [p for p in self.broker.positions()
                    if p["symbol"].upper().startswith(broker_sym[:6].upper())]
            if not poss:
                continue
            realized = 1 if sum(p["profit"] for p in poss) >= 0 else -1
            self.oracle.act("fusion.learn", {"symbol": canon_sym, "streams": streams,
                                             "realized_direction": realized,
                                             "_sender": "ctrader_demo"})

    def _record_shadow_proposal(self, symbol, broker_symbol, response, market_state,
                                reason_code="max_open_positions", reason="cTrader capacity full"):
        """Persist an overflow setup for cTrader-bar replay and Scout learning."""
        signal = response.get("signal") or {}
        plan = response.get("plan") or {}
        streams = response.get("_streams") or {}
        technical = streams.get("technical") if isinstance(streams, dict) else {}
        technical = technical if isinstance(technical, dict) else {}
        entry = float(plan.get("entry") or signal.get("last") or signal.get("price") or 0.0)
        stop = float(plan.get("stop") or 0.0)
        target = float(plan.get("target") or 0.0)
        try:
            self.oracle.decision_ledger.record_trade_not_entered(
                symbol=symbol,
                direction=str(plan.get("direction") or "hold"),
                hypothetical_entry_price=entry,
                hypothetical_stop=stop,
                hypothetical_target=target,
                scout_score=float(
                    response.get("scout_score")
                    if response.get("scout_score") is not None
                    else response.get("effective_confidence", signal.get("confidence", 0.0)) or 0.0
                ),
                effective_confidence=float(
                    response.get("effective_confidence")
                    if response.get("effective_confidence") is not None
                    else signal.get("confidence", 0.0) or 0.0
                ),
                entry_confidence=float(signal.get("confidence") or 0.0),
                learned_conviction_floor=float(response.get("learned_conviction_floor") or 0.5),
                regime=str(response.get("regime") or technical.get("regime") or "ranging"),
                session=str(market_state.get("session") or "unknown"),
                strategy_id=str(technical.get("genome") or technical.get("strategy_id") or "") or None,
                stage_rejected="position_limit_shadow",
                rejection_stage="position_limit_shadow",
                reason_code=reason_code,
                primary_reason=reason,
                reasons=[reason],
                streams=streams,
                atr=float(response.get("atr") or 0.0),
                shadow_observation=True,
                execution_mode="ctrader_capacity_shadow",
                broker_symbol=broker_symbol,
            )
        except Exception as exc:
            log.warning("[%s] failed to record shadow setup for Scout replay: %s",
                        symbol, type(exc).__name__)
        record = {
            "schema": "oracle.ctrader.shadow_proposal.v1",
            "observed_at_unix": time.time(),
            "symbol": symbol,
            "broker_symbol": broker_symbol,
            "strategy_id": technical.get("genome"),
            "regime": response.get("regime") or technical.get("regime"),
            "direction": plan.get("direction"),
            "confidence": signal.get("confidence"),
            "effective_confidence": response.get("effective_confidence"),
            "scout_score": response.get("scout_score"),
            "scout_enabled": response.get("scout_enabled", getattr(self.oracle, "scout_enabled", True)),
            "learned_conviction_floor": response.get("learned_conviction_floor"),
            "entry": plan.get("entry"),
            "size": plan.get("size"),
            "stop": plan.get("stop"),
            "target": plan.get("target"),
            "market_state": {
                key: market_state.get(key)
                for key in ("state", "sessions", "overlap", "updated_at_utc")
            },
            "execution_mode": "shadow_no_order_sent",
            "shadow_reason": reason,
            "decision_ledger_replay_enabled": True,
        }
        path = _ECO_ROOT / "Chronicle" / "memory" / "research" / "oracle_shadow" / "proposals.jsonl"
        try:
            legacy_path = _ECO_ROOT / "Chronicle" / "research" / "oracle_shadow" / "proposals.jsonl"
            if legacy_path.is_file() and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_path, path)
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False)
            with _shadow_log_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as output:
                    output.write(encoded + "\n")
        except (OSError, TypeError, ValueError) as exc:
            log.warning("[%s] could not persist shadow proposal: %s", symbol, type(exc).__name__)

    def kill(self):
        return self._close_all_positions()

    def shutdown(self):
        self.stop_trade_manager()
        try:
            self.oracle.stop()
        except Exception as exc:
            log.warning("failed to stop oracle: %s", exc)
        for peer in (self.pulse, self.sentinel, self.atlas, self.chronicle):
            if peer:
                try:
                    peer.stop()
                except Exception:
                    pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="Oracle demo trader on cTrader (63 symbols)")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--preset", choices=["all", "live"], default="all")
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    ap.add_argument("--max-trades", type=int, default=None)
    ap.add_argument("--session-max-loss", type=float, default=0.05)
    ap.add_argument("--confirm-live", action="store_true")
    ap.add_argument("--evolve-first", action="store_true")
    args = ap.parse_args()
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif args.preset == "live":
        symbols = _LIVE_PRESET
    else:
        symbols = DEFAULT_SYMBOLS
    trader = CTraderDemoTrader(
        symbols=symbols, interval_sec=args.interval,
        session_max_loss_pct=args.session_max_loss,
        max_trades=args.max_trades, confirm_live=args.confirm_live)
    if args.evolve_first:
        for sym in trader.symbols:
            print(f"Evolving strategy for {sym}...")
            out = trader.oracle.act("strategy.evolve",
                                    {"symbol": sym, "generations": 6, "_sender": "ctrader_demo"})
            print(f"  {sym}: promoted={out.get('promoted_new_champion')} "
                  f"oos_return={(out.get('out_of_sample') or {}).get('total_return')}")
    print("=" * 64)
    print(f"  ORACLE DEMO TRADER (cTrader — {len(symbols)} symbols)")
    print("  Same pipeline as mt5_demo_trader: champions + agent correlation.")
    print("  Uses cTrader Open API via CTraderBroker adapter.")
    print("  ALWAYS use a DEMO account for initial testing!")
    print("=" * 64)
    try:
        trader.run(cycles=args.cycles)
    except Exception:
        raise
    print("cTrader demo trader shutdown complete.")


if __name__ == "__main__":
    main()
