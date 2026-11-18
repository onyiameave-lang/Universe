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
  - Existing cTrader positions are reconciled and adopted for management,
    including in shadow-entry mode; adopted trades are not attributed to an
    Oracle strategy unless their entry context is known.

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
from execution.chronicle_position_log import ChroniclePositionLog  # type: ignore
from execution.trade_manager import (  # type: ignore
    TradeManager, TradeManagerConfig, Position, MarketSnapshot, Direction, Action,
    SCOUT_ENTRY_CONTEXT_KEY,
)
from execution.position_limits import OpenPositionBudget, account_position_lock  # type: ignore
from execution.shadow_feedback import replay_shadow_candidates  # type: ignore
from intelligence.news_impact import assess_news_impact  # type: ignore
from intelligence.social_risk import assess_social_risk  # type: ignore
from intelligence.trade_learning import TradeLearningEngine  # type: ignore
from core.risk import Position as RiskPosition  # type: ignore

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

# ── Shared evolution + execution watchlist ────────────────────────────────────
CRYPTO_SYMBOLS: List[str] = [
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
    "LTCUSD", "DOGEUSD", "DOTUSD", "LINKUSD", "AVAXUSD", "MATICUSD",
    "BCHUSD", "UNIUSD", "XLMUSD", "ATOMUSD", "NEARUSD", "SHIBUSD",
    "TRXUSD", "TONUSD", "SUIUSD", "PEPEUSD",
]

DEFAULT_SYMBOLS: List[str] = [
    # Major forex
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    # Minor forex
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD",
    # Commodities
    "XAUUSD", "XAGUSD", "USOIL", "UKOIL", "NATGAS",
    # Crypto (22 pairs)
    *CRYPTO_SYMBOLS,
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
    "BRKB":    "BRK.B.US",
    "V":       "V.US",
}

# ── Fallback aliases for fuzzy matching ───────────────────────────────────────
_FALLBACK_ALIASES: Dict[str, List[str]] = {
    "USOIL":   ["USOIL.cash", "WTI.cash", "WTI", "XTIUSD"],
    "UKOIL":   ["UKOIL.cash", "BRENT.cash", "XBRUSD"],
    "NATGAS":  ["NATGAS.cash", "NGAS.cash", "XNGUSD"],
    "BTCUSD":   ["BTCUSD", "BTC/USD", "BTCUSDT", "BTC-USD"],
    "ETHUSD":   ["ETHUSD", "ETH/USD", "ETHUSDT", "ETH-USD"],
    "SOLUSD":   ["SOLUSD", "SOL/USD", "SOLUSDT", "SOL-USD"],
    "XRPUSD":   ["XRPUSD", "XRP/USD", "XRPUSDT", "XRP-USD"],
    "BNBUSD":   ["BNBUSD", "BNB/USD", "BNBUSDT", "BNB-USD"],
    "ADAUSD":   ["ADAUSD", "ADA/USD", "ADAUSDT", "ADA-USD"],
    "LTCUSD":   ["LTCUSD", "LTC/USD", "LTCUSDT", "LTC-USD"],
    "DOGEUSD":  ["DOGEUSD", "DOGE/USD", "DOGEUSDT", "DOGE-USD"],
    "DOTUSD":   ["DOTUSD", "DOT/USD", "DOTUSDT", "DOT-USD"],
    "LINKUSD":  ["LINKUSD", "LINK/USD", "LINKUSDT", "LINK-USD"],
    "AVAXUSD":  ["AVAXUSD", "AVAX/USD", "AVAXUSDT", "AVAX-USD"],
    "MATICUSD": ["MATICUSD", "MATIC/USD", "MATICUSDT", "POLUSD", "POL/USD"],
    "BCHUSD":   ["BCHUSD", "BCH/USD", "BCHUSDT", "BCH-USD"],
    "UNIUSD":   ["UNIUSD", "UNI/USD", "UNIUSDT", "UNI-USD"],
    "XLMUSD":   ["XLMUSD", "XLM/USD", "XLMUSDT", "XLM-USD"],
    "ATOMUSD":  ["ATOMUSD", "ATOM/USD", "ATOMUSDT", "ATOM-USD"],
    "NEARUSD":  ["NEARUSD", "NEAR/USD", "NEARUSDT", "NEAR-USD"],
    "SHIBUSD":  ["SHIBUSD", "SHIB/USD", "SHIBUSDT", "1000SHIBUSD"],
    "TRXUSD":   ["TRXUSD", "TRX/USD", "TRXUSDT", "TRX-USD"],
    "TONUSD":   ["TONUSD", "TON/USD", "TONUSDT", "TON-USD"],
    "SUIUSD":   ["SUIUSD", "SUI/USD", "SUIUSDT", "SUI-USD"],
    "PEPEUSD":  ["PEPEUSD", "PEPE/USD", "PEPEUSDT", "1000PEPEUSD"],
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
        self.max_trades = max_trades if max_trades is not None else _env_int(
            "ORACLE_MAX_TRADES_PER_SESSION", 10)
        self.confirm_live = confirm_live
        # Shadow mode blocks new orders; existing broker positions are still
        # reconciled and managed by Oracle's position manager.
        self.shadow_mode = os.getenv("ORACLE_CTRADER_SHADOW_MODE", "true").strip().lower() not in {
            "0", "false", "no", "off",
        }
        self.broker = CTraderBroker()
        self._trades_this_session = 0
        self._start_equity: Optional[float] = None
        self._open_context: Dict[str, Dict] = {}
        self._trading_disabled_symbols = set()
        self._shadow_market_snapshots: Dict[str, Dict[str, Any]] = {}

        self._symbol_timeout = float(
            os.getenv("ORACLE_SYMBOL_TIMEOUT_SEC", str(_DEFAULT_SYMBOL_TIMEOUT)))

        _ks_ttl = float(os.getenv("KILL_SWITCH_EQUITY_CACHE_SEC", "60"))
        self._ks_equity_ttl = _ks_ttl
        self._ks_equity_cache: Optional[float] = None
        self._ks_equity_ts: float = 0.0
        self._ks_check_unavailable = False
        self._ks_fired = False

        self._sym_mapper = SymbolMapper()

        self._trade_manager = TradeManager(TradeManagerConfig())
        self._trade_learning = TradeLearningEngine()
        # Key by cTrader position id, not symbol: accounts can have multiple
        # broker positions for one symbol and each must be managed separately.
        self._managed_positions: Dict[str, Position] = {}
        self._manage_interval = float(
            os.getenv("ORACLE_MANAGE_INTERVAL_SEC", str(_DEFAULT_MANAGE_INTERVAL)))
        self._manage_lock = threading.Lock()
        self._manage_stop_event = threading.Event()
        self._manage_thread: Optional[threading.Thread] = None

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
        self.oracle.start()
        if hasattr(self.oracle, "data") and hasattr(self.oracle.data, "attach_broker"):
            self.oracle.data.attach_broker(self.broker, self._sym_mapper)
        self._trade_learning.confidence = self.oracle._champion_confidence

        self._pos_log = ChroniclePositionLog(
            chronicle_agent=self.chronicle, trader_id=_TRADER_ID)
        log.info("CTraderDemoTrader: TRADER_ID=%r", _TRADER_ID)

    def connect(self):
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
                log.info("Connected: %s account, equity %.2f %s",
                         status["account_type"], self._start_equity,
                         status.get("currency", ""))
            else:
                self._start_equity = None
                log.warning(
                    "Connected to cTrader but initial account equity is unavailable; "
                    "new entries will wait for a valid account refresh")
            self._build_symbol_map()
            mapped_symbols = [self._sym_mapper.translate(symbol) for symbol in self.symbols]
            try:
                self.broker.load_market_schedules([name for name in mapped_symbols if name])
            except Exception as exc:
                log.warning("Could not load broker market schedules; new entries will fail closed: %s", exc)
        else:
            log.warning("cTrader not connected: %s", status.get("reason"))
            self._build_symbol_map(broker_symbols=[])
        if hasattr(self.oracle, "data") and hasattr(self.oracle.data, "attach_broker"):
            self.oracle.data.attach_broker(self.broker, self._sym_mapper)
        return status

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

    @staticmethod
    def _position_key(broker_position):
        position_id = (broker_position or {}).get("ticket") or (broker_position or {}).get("id")
        if position_id is not None:
            return str(position_id)
        symbol = str((broker_position or {}).get("symbol") or "UNKNOWN").upper()
        side = str((broker_position or {}).get("type") or "unknown").lower()
        entry = (broker_position or {}).get("price_open") or (broker_position or {}).get("price") or 0
        return f"{symbol}:{side}:{entry}"

    def _oracle_symbol_for_broker(self, broker_symbol):
        broker_key = str(broker_symbol or "").upper()
        for canonical, mapped in self._sym_mapper._map.items():
            if str(mapped).upper() == broker_key:
                return canonical
        known_symbols = set(self.symbols) | set(_CTRADER_BROKER_MAP) | set(_FALLBACK_ALIASES)
        broker_original = {broker_key: str(broker_symbol)}
        for canonical in known_symbols:
            resolved = self._sym_mapper._resolve_one(
                canonical, {broker_key}, broker_original
            )
            if resolved:
                return canonical
        return broker_key

    def _register_position(self, symbol, direction, entry_price, stop, target, confidence, size=0.0,
                            news_level="none", news_reason="", social_level="none", social_reason="",
                            position_id=None, adopted_external=False):
        position_key = str(position_id) if position_id is not None else str(symbol)
        with self._manage_lock:
            if position_key in self._managed_positions:
                return self._managed_positions[position_key]
        try:
            sig = self.oracle.act("trade.signal", {"symbol": symbol, "_sender": "ctrader_demo"})
            entry_regime = (sig or {}).get("regime") or "ranging"
            entry_atr = (sig or {}).get("atr")
        except Exception as exc:
            log.warning("[%s] could not fetch entry regime: %s", symbol, exc)
            entry_regime = "ranging"
            entry_atr = None
        entry_streams = dict(self._open_context.get(symbol, {}) or {})
        scout_entry = entry_streams.pop(SCOUT_ENTRY_CONTEXT_KEY, {})
        scout_entry = scout_entry if isinstance(scout_entry, dict) else {}
        technical_stream = entry_streams.get("technical", {})
        strategy_id = scout_entry.get("strategy_id") or (
            technical_stream.get("genome") if isinstance(technical_stream, dict) else None
        )
        if adopted_external:
            # We can manage an existing broker position, but must not claim its
            # entry as an Oracle strategy decision or train that genome on it.
            strategy_id = None
            entry_streams["position_origin"] = "adopted_from_cTrader"
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
            if position_key in self._managed_positions:
                return self._managed_positions[position_key]
            managed_position = Position(
                symbol=symbol, direction=dir_norm, entry_price=entry_price,
                initial_stop=stop, initial_target=target, entry_confidence=confidence,
                entry_regime=entry_regime, entry_streams=entry_streams, entry_atr=entry_atr,
                entry_term_evidence=entry_term_evidence, strategy_id=strategy_id,
                timeframe_policy_id=timeframe_policy_id,
                entry_scout_dimension_scores=dict(scout_entry.get("dimension_scores") or {}),
                entry_scout_session=scout_entry.get("session"),
                execution_environment=str(getattr(self.broker.status, "account_type", "unknown")))
            # Trade Journal (roadmap Phase 3 groundwork): reasons for
            # entering this trade, wired to Chronicle, linked to its
            # eventual win/loss outcome via the returned journal_id.
            managed_position.journal_id = self.oracle.trade_journal.log_entry(
                symbol, direction, confidence, entry_regime,
                news_level, news_reason, social_level, social_reason,
                entry_streams, size)
            self._managed_positions[position_key] = managed_position
        risk_direction = "long" if dir_norm == Direction.BUY else "short"
        self.oracle.risk.portfolio.positions.append(
            RiskPosition(symbol, risk_direction, size, entry_price, stop, target))
        log.info("[%s] registered with CTM: dir=%s entry=%.5f stop=%.5f target=%.5f conf=%.3f",
                 symbol, dir_norm.value, entry_price, stop, target, confidence)
        return managed_position

    def _remove_risk_position_for(self, managed_position):
        """Remove one portfolio entry, preserving other positions in that symbol."""
        for index, tracked in enumerate(self.oracle.risk.portfolio.positions):
            if (
                tracked.symbol == managed_position.symbol
                and tracked.direction == (
                    "long" if managed_position.direction == Direction.BUY else "short"
                )
                and math.isclose(float(tracked.entry), float(managed_position.entry_price),
                                 rel_tol=0.0, abs_tol=1e-9)
            ):
                self.oracle.risk.portfolio.positions.pop(index)
                return

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
        # Adopt every open cTrader position, including positions opened
        # manually or by another process and symbols outside this scan preset.
        with self._manage_lock:
            known_position_ids = set(self._managed_positions)
        for broker_position in open_positions:
            key = self._position_key(broker_position)
            if key in known_position_ids:
                continue
            broker_sym = str(broker_position.get("symbol") or "").upper()
            symbol = self._oracle_symbol_for_broker(broker_sym)
            entry_price = float(broker_position.get("price_open") or broker_position.get("price") or 0.0)
            if not broker_sym or entry_price <= 0:
                log.error("Skipping cTrader position adoption with invalid identity/entry: %s", key)
                continue
            direction = "long" if str(broker_position.get("type") or "").lower() in {"buy", "long"} else "short"
            stop = float(broker_position.get("sl") or 0.0)
            target = float(broker_position.get("tp") or 0.0)
            # Existing code's fallback geometry is retained for positions
            # that have no broker-side SL/TP; cTrader remains authoritative.
            stop = stop or entry_price * (0.995 if direction == "long" else 1.005)
            target = target or entry_price * (1.01 if direction == "long" else 0.99)
            try:
                self._register_position(
                    symbol, direction, entry_price=entry_price, stop=stop, target=target,
                    confidence=0.5, size=broker_position.get("volume", 0.0),
                    position_id=broker_position.get("ticket") or broker_position.get("id"),
                    adopted_external=True,
                )
                known_position_ids.add(key)
                log.info("Adopted cTrader position #%s (%s) for continuous management", key, broker_sym)
            except Exception as exc:
                log.warning("Could not adopt cTrader position #%s (%s): %s", key, broker_sym, exc)
        with self._manage_lock:
            managed_items = list(self._managed_positions.items())
        log.info("CTM reconciliation: broker_open=%d managed=%d shadow_entries=%s",
                 len(open_positions), len(managed_items), self.shadow_mode)
        live_by_key = {self._position_key(row): row for row in open_positions}
        for position_key, pos in managed_items:
            symbol = pos.symbol
            with self._manage_lock:
                pos = self._managed_positions.get(position_key)
            if pos is None:
                continue
            broker_sym = self._sym_mapper.translate(symbol) or symbol
            live_pos = live_by_key.get(position_key)
            if live_pos is None and position_key == str(symbol):
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
                self._remove_risk_position_for(pos)
                with self._manage_lock:
                    self._managed_positions.pop(position_key, None)
                continue
            try:
                market_state = self.broker.market_state(broker_sym)
            except Exception as exc:
                log.warning("[%s] CTM could not read broker market state: %s", symbol, exc)
                continue
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
                        self._remove_risk_position_for(pos)
                        with self._manage_lock:
                            self._managed_positions.pop(position_key, None)
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
              f"shadow_entries={self.shadow_mode} | existing-position management=enabled")
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
                replay_shadow_candidates(
                    self.oracle,
                    trade_manager=self._trade_manager,
                    current_snapshots=self._shadow_market_snapshots,
                )
                print(f"\nCycle {cycle_label} done — scanned {summary['scanned']} symbols | "
                      f"{summary['trades']} trade(s) | {summary['holds']} hold | "
                      f"{summary['rejects']} reject | {summary['errors']} error | "
                      f"{summary['timeouts']} timeout | {summary['unmapped']} unmapped | "
                        f"{summary['shadowed']} shadowed | "
                        f"{summary['managed']} managed | {summary['market_closed']} not open")
                if summary["kill_switch"]:
                    print("KILL SWITCH: session loss limit hit. Flattening + stopping.")
                    print(self._close_all_positions())
                    break
                if cycles is None or cycle < cycles:
                    print(f"Sleeping {self.interval}s until next cycle...")
                    time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            self._learn_from_closed()
            self.shutdown()

    def _tick(self):
        self._shadow_market_snapshots = {}
        summary = dict(scanned=0, trades=0, holds=0, rejects=0,
                   errors=0, timeouts=0, unmapped=0, managed=0,
                   market_closed=0, shadowed=0, kill_switch=False)
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
        if self.broker.status.connected:
            try:
                open_positions = self.broker.positions()
            except Exception as exc:
                log.warning("Could not refresh open positions for scan: %s", exc)
                summary["errors"] += 1
                return summary
            if not getattr(self.broker, "_positions_last_refresh_ok", True):
                log.warning("cTrader position reconciliation failed; skipping new-entry scan")
                summary["errors"] += 1
                return summary
        position_budget = OpenPositionBudget(self.max_trades, len(open_positions))

        for symbol in self.symbols:
            if self._kill_switch_check():
                summary["kill_switch"] = True
                return summary
            if self._ks_check_unavailable:
                summary["errors"] += 1
                log.warning("Stopping scan because cTrader account equity is unavailable")
                return summary
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
                position_key = self._position_key(existing_pos)
                if position_key not in self._managed_positions:
                    entry_price = existing_pos.get("price_open") or existing_pos.get("price") or 0.0
                    direction = "long" if str(existing_pos.get("type", "")).lower() == "buy" else "short"
                    sl = existing_pos.get("sl") or entry_price * (0.995 if direction == "long" else 1.005)
                    tp = existing_pos.get("tp") or entry_price * (1.01 if direction == "long" else 0.99)
                    self._register_position(symbol, direction, entry_price=entry_price,
                                             stop=sl, target=tp, confidence=0.5,
                                             size=existing_pos.get("volume", 0.0),
                                             position_id=existing_pos.get("ticket") or existing_pos.get("id"),
                                             adopted_external=True)
                continue
            if broker_sym.upper() in self._trading_disabled_symbols:
                log.info("[%s->%s] new entry skipped — cTrader previously reported "
                         "TRADING_DISABLED for this instrument",
                         symbol, broker_sym)
                print(f"[{symbol}->{broker_sym}] SKIP trading disabled by cTrader")
                summary["market_closed"] += 1
                continue
            if self._pos_log.has_open_position(symbol, broker_sym):
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
            if isinstance(sig, dict):
                signal_snapshot = sig.get("signal") or {}
                if isinstance(signal_snapshot, dict) and sig.get("last") is not None:
                    self._shadow_market_snapshots[symbol] = {
                        "price": sig.get("last"),
                        "confidence": sig.get("effective_confidence", signal_snapshot.get("confidence", 0.5)),
                        "regime": sig.get("regime") or signal_snapshot.get("regime") or "ranging",
                        "atr": sig.get("atr"),
                        "news_impact": sig.get("news_impact") or "none",
                        "social_risk": sig.get("social_risk") or "none",
                        "observed_at_unix": time.time(),
                    }
            if status != "complete":
                message = (sig or {}).get("message", "unknown")
                risk = (sig or {}).get("risk") or {}
                reasons = risk.get("reasons")
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
            entry_context = dict(sig.get("_streams", {}) or {})
            entry_context[SCOUT_ENTRY_CONTEXT_KEY] = dict(sig.get("scout_evaluation") or {})
            self._open_context[symbol] = entry_context
            self._open_context[symbol]["market_context"] = {
                key: market_state.get(key) for key in
                ("state", "sessions", "overlap", "schedule_timezone", "updated_at_utc")
            }
            conf_str = f"conf={s.get('confidence', 0):.3f}"
            broker_plan = dict(plan)
            broker_plan["broker_symbol"] = broker_sym
            if self.shadow_mode:
                summary["shadowed"] += 1
                print(f"[{symbol}->{broker_sym}] SHADOW direction={plan['direction']} "
                      f"conf={s.get('confidence', 0):.3f} size={plan.get('size', 0)} "
                      f"stop={plan.get('stop')} target={plan.get('target')}; no order sent")
                self._record_shadow_proposal(symbol, broker_sym, sig, market_state)
                continue
            if self.broker.status.connected:
                with account_position_lock():
                    try:
                        latest_positions = self.broker.positions()
                        if not getattr(self.broker, "_positions_last_refresh_ok", True):
                            raise RuntimeError("broker position snapshot is not authoritative")
                        position_budget.reconcile(len(latest_positions))
                    except Exception as exc:
                        log.warning("[%s] refusing order; could not reconcile account positions: %s", symbol, exc)
                        summary["errors"] += 1
                        continue
                    if position_budget.full:
                        reason = (f"account-wide max open positions reached "
                                  f"({position_budget.open_count}/{self.max_trades})")
                        log.info("[%s] order skipped — %s; tracking as shadow candidate", symbol, reason)
                        self._record_shadow_proposal(
                            symbol, broker_sym, sig, market_state,
                            rejection_reason=reason,
                        )
                        summary["rejects"] += 1
                        continue
                    if not position_budget.reserve():
                        summary["rejects"] += 1
                        continue
                    result = self.broker.place_order(broker_plan, human_confirm=self.confirm_live)
                res_status = result.get("status", "unknown")
                res_reason = result.get("reason", "")
                print(f"[{symbol}->{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                      f"  -> {res_status}  {res_reason}")
                if res_status == "filled":
                    self._trades_this_session += 1
                    summary["trades"] += 1
                    ticket = result.get("order") or result.get("ticket") or 0
                    self._pos_log.log_opened(symbol, broker_sym, ticket,
                        direction=plan["direction"],
                        volume=result.get("volume", plan.get("size", 0)),
                        price=result.get("price", 0.0),
                        sl=plan.get("stop", 0.0), tp=plan.get("target", 0.0))
                    open_positions.append({
                        "symbol": broker_sym,
                        "ticket": ticket,
                        "type": "buy" if plan["direction"] in ("long", "buy") else "sell",
                        "price_open": result.get("price") or plan.get("entry", 0.0),
                        "volume": result.get("volume", plan.get("size", 0.0)),
                        "sl": plan.get("stop", 0.0),
                        "tp": plan.get("target", 0.0),
                    })
                    self._register_position(symbol, plan["direction"],
                        entry_price=result.get("price") or plan.get("entry", 0.0),
                        stop=plan.get("stop", 0.0), target=plan.get("target", 0.0),
                        confidence=float(sig.get("effective_confidence", s.get("confidence", 0.0))),
                        size=result.get("volume", plan.get("size", 0.0)),
                        news_level=sig.get("news_impact", "none"),
                        news_reason=sig.get("news_reason", ""),
                        social_level=sig.get("social_risk", "none"),
                        social_reason=sig.get("social_reason", ""))
                    self.oracle.risk.portfolio.record_trade_opened(symbol)
                else:
                    if str(res_status).lower() in {"rejected", "blocked"}:
                        position_budget.release_rejected_order()
                    summary["rejects"] += 1
                    if _is_trading_disabled_rejection(result):
                        self._trading_disabled_symbols.add(broker_sym.upper())
                        log.warning(
                            "[%s->%s] cTrader disabled new trading; suppressing further "
                            "entry attempts for this instrument until trader restart",
                            symbol, broker_sym,
                        )
            else:
                print(f"[{symbol}->{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                      f"  (cTrader not connected)")
                summary["trades"] += 1
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
        equity = self._ks_equity_cache
        if self._start_equity is None:
            return False
        loss = (self._start_equity - equity) / self._start_equity
        if loss >= self.session_max_loss_pct:
            self._ks_fired = True
        return self._ks_fired

    def _learn_from_closed(self):
        if self.shadow_mode:
            return
        for canon_sym, streams in self._open_context.items():
            broker_sym = self._sym_mapper.translate(canon_sym) or canon_sym
            poss = [p for p in self.broker.positions()
                    if p["symbol"].upper().startswith(broker_sym[:6].upper())]
            if not poss:
                continue
            realized = 1 if sum(p["profit"] for p in poss) >= 0 else -1
            self.oracle.act("fusion.learn", {"symbol": canon_sym, "streams": streams,
                                             "realized_direction": realized,
                                             "trade_won": realized > 0,
                                             "_sender": "ctrader_demo"})

    def _record_shadow_proposal(
        self, symbol, broker_symbol, response, market_state, *, rejection_reason="shadow_mode"
    ):
        """Append a non-executable Oracle decision to Chronicle's research area."""
        signal = response.get("signal") or {}
        plan = response.get("plan") or {}
        streams = response.get("_streams") or {}
        technical = streams.get("technical") if isinstance(streams, dict) else {}
        technical = technical if isinstance(technical, dict) else {}
        record = {
            "schema": "oracle.ctrader.shadow_proposal.v1",
            "observed_at_unix": time.time(),
            "symbol": symbol,
            "broker_symbol": broker_symbol,
            "strategy_id": technical.get("genome"),
            "regime": response.get("regime") or technical.get("regime"),
            "direction": plan.get("direction"),
            "confidence": signal.get("confidence"),
            "entry": plan.get("entry"),
            "size": plan.get("size"),
            "stop": plan.get("stop"),
            "target": plan.get("target"),
            "market_state": {
                key: market_state.get(key)
                for key in ("state", "sessions", "overlap", "updated_at_utc")
            },
            "execution_mode": "shadow_no_order_sent",
            "rejection_reason": str(rejection_reason),
        }
        evaluation = response.get("scout_evaluation") or {}
        ledger = getattr(self.oracle, "decision_ledger", None)
        direction = str(plan.get("direction") or "hold")
        strategy_id = evaluation.get("strategy_id") or technical.get("genome")
        already_pending = False
        try:
            entry, stop, target = (
                float(plan.get(level) or 0.0) for level in ("entry", "stop", "target")
            )
            valid_shadow_levels = all(value > 0 for value in (entry, stop, target))
            if direction in ("long", "buy"):
                valid_shadow_levels = valid_shadow_levels and stop < entry < target
            elif direction in ("short", "sell"):
                valid_shadow_levels = valid_shadow_levels and target < entry < stop
            else:
                valid_shadow_levels = False
        except (TypeError, ValueError, OverflowError):
            valid_shadow_levels = False
        if ledger is not None and valid_shadow_levels:
            try:
                is_pending = getattr(ledger, "has_pending_shadow_candidate", None)
                already_pending = bool(is_pending(
                    symbol=symbol, direction=direction, strategy_id=strategy_id
                )) if callable(is_pending) else False
                if already_pending:
                    record_overlap = getattr(ledger, "record_shadow_candidate_overlap", None)
                    if callable(record_overlap):
                        record_overlap(
                            symbol=symbol, direction=direction, strategy_id=strategy_id,
                            candidate_context={
                                "entry": entry, "stop": stop, "target": target,
                                "reason": str(rejection_reason),
                                "confidence": response.get("effective_confidence") or signal.get("confidence"),
                            },
                        )
                if not already_pending:
                    ledger.record_trade_not_entered(
                        symbol=symbol,
                        direction=direction,
                        rejection_stage="max_positions_shadow" if rejection_reason != "shadow_mode" else "trader_shadow_mode",
                        primary_reason=str(rejection_reason),
                        reasons=[str(rejection_reason)],
                        scout_score=float(response.get("scout_score") or response.get("effective_confidence") or signal.get("confidence") or 0.0),
                        entry_confidence=float(response.get("effective_confidence") or signal.get("confidence") or 0.0),
                        learned_conviction_floor=float(response.get("learned_conviction_floor") or 0.5),
                        regime=str(evaluation.get("regime") or response.get("regime") or technical.get("regime") or "ranging"),
                        session=str(evaluation.get("session") or "unknown"),
                        strategy_id=str(strategy_id) if strategy_id else None,
                        hypothetical_entry_price=float(plan.get("entry") or 0.0),
                        hypothetical_stop=float(plan.get("stop") or 0.0),
                        hypothetical_target=float(plan.get("target") or 0.0),
                        atr=float(response.get("atr") or 0.0),
                        entry_streams=streams,
                        shadow_observation=True,
                        dimension_scores=dict(evaluation.get("dimension_scores") or {}),
                        learned_dimension_weights=dict(evaluation.get("learned_dimension_weights") or {}),
                    )
            except Exception as exc:
                log.warning("[%s] could not add shadow candidate to Oracle ledger: %s", symbol, type(exc).__name__)
        if already_pending:
            return
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
    ap.add_argument("--preset", choices=["all", "live", "crypto"], default="all")
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
    elif args.preset == "crypto":
        symbols = CRYPTO_SYMBOLS
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
