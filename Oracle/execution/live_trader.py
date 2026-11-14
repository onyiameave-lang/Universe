"""
Oracle.execution.live_trader
==========================
The live trading loop: connects Oracle's evolved brain to a MT5 account and
trades on a schedule, with every constitutional safety rail enforced.
(Book VI capital sovereignty; Book I Article X; Book III Principle VI.)

This is the runnable script you asked for: log in your MT5 account and let
Oracle trade. It does the full loop per symbol, on an interval:

  1. SIGNAL     Oracle produces an evidence-fused, evolved-strategy signal.
  2. RISK GATE  RiskManager sizes + gates the trade (nothing bypasses it).
  3. EXECUTE    MT5Broker submits a market order WITH broker-side stop + target.
  4. MONITOR    positions + account equity are polled; a session drawdown limit
                acts as a kill switch (close_all + stop).
  5. LEARN      realized outcomes feed Oracle's adaptive fusion.

Defaults are deliberately conservative and SAFE:
  * paper trading unless ORACLE_PAPER_TRADING=false
  * real accounts require ORACLE_ALLOW_LIVE=true AND per-order human confirm
  * a session max-loss kill switch and a max-trades-per-session cap

Run:
    python -m Oracle.execution.live_trader           # from ecosystem root
    # or:  cd Oracle && python execution/live_trader.py

Changes vs v2:
  FIX-1  Default symbol list expanded to 8 major pairs + commodities.
         --symbols CLI arg still overrides.
  FIX-2  _tick() wraps every oracle.act() call in a per-symbol timeout
         (ORACLE_SYMBOL_TIMEOUT_SEC, default 60 s) using a daemon thread so
         a hanging yfinance / Sentinel / Pulse call can never freeze the loop.
  FIX-3  _tick() now logs a distinct outcome tag for every symbol:
           [SYMBOL] TRADE  direction=long  conf=0.72  -> filled
           [SYMBOL] HOLD   (signal is hold)
           [SYMBOL] REJECT conf=0.48 < floor=0.50  reasons=[...]
           [SYMBOL] ERROR  <message>
           [SYMBOL] TIMEOUT after 60 s — skipping
  FIX-4  run() cycle summary line printed after every full scan:
           Cycle 1/10 done — scanned 8 symbols, 1 trade, 5 hold, 2 reject
  FIX-5  max_trades cap checked BEFORE oracle.act() so we skip the expensive
         signal call once the session cap is reached.
  FIX-6  _kill_switch_check() double-fire guard: once it fires it latches True
         so the caller's post-tick check always sees it even if equity recovers
         between the two calls.
  FIX-7  Graceful shutdown: oracle.stop() + peer.stop() moved to finally block
         in run() (was only in shutdown()); shutdown() still callable externally.

v3 additions (broker symbol mapping):
  FIX-8  SymbolMapper: on connect(), queries mt5.symbols_get() and builds an
         auto-map from Oracle canonical names → broker symbol names using:
           a) env-var overrides  BROKER_SYMBOL_MAP=USOIL:XTIUSD,BTCUSD:BTCUSDm
           b) hardcoded fallback table for common broker aliases
           c) fuzzy prefix/suffix matching (XAUUSDm → XAUUSD, etc.)
         Logs the full resolved map on startup.
  FIX-9  _tick() translates every Oracle symbol to its broker symbol before
         calling broker.place_order().  Symbols that cannot be mapped are
         skipped with a clear [SYMBOL] UNMAPPED warning.
  FIX-10 _learn_from_closed() uses broker symbol for position lookup but
         reports back to Oracle with the canonical symbol name.

v4 additions (broker_symbol key / mt5_broker.py v2 integration):
  FIX-11 _tick() now sets plan["broker_symbol"] = broker_sym as a SEPARATE
         key instead of overwriting plan["symbol"].  This means:
           - plan["symbol"]        stays as the Oracle canonical name
             (used by AdaptiveFusion.learn, _open_context, logging)
           - plan["broker_symbol"] carries the translated broker name
             (used by MT5Broker.place_order() — checked first before _map())
         Eliminates the risk of the canonical name being lost if place_order()
         or any downstream code reads plan["symbol"] expecting the Oracle name.
  FIX-12 Requires mt5_broker.py v2 which adds:
           - MT5Broker.symbols() → List[str]  (called by _build_symbol_map)
           - MT5Broker.place_order() checks plan["broker_symbol"] before _map()

v5 additions (broker symbol quality filter):
  FIX-3  SymbolMapper._resolve_one() now enforces a minimum match quality for
         fuzzy steps 4/5/6.  A broker symbol must:
           a) be at least 4 characters long (rejects stock tickers like BTC,
              ETH, OIL that are substrings of Oracle canonical names), AND
           b) satisfy FULL containment: either the broker symbol starts with
              the full canonical name (e.g. XAUUSDm starts with XAUUSD) OR
              the canonical name starts with the full broker symbol.
         This prevents MetaQuotes-Demo's stock tickers "BTC" and "ETHU" from
         being matched to BTCUSD and ETHUSD.  Those symbols are now correctly
         marked UNMAPPED instead of silently trading the wrong instrument.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ECO_ROOT = _REPO_ROOT.parent
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution.mt5_broker import MT5Broker  # type: ignore
from execution.chronicle_position_log import ChroniclePositionLog  # type: ignore  # FIX-DEDUP
from execution.trade_manager import (  # type: ignore  # Continuous Trade Manager
    TradeManager, TradeManagerConfig, Position, MarketSnapshot, Direction, Action,
)
from execution.telegram_notifier import (  # type: ignore
    TelegramNotifier, RecentSignalCache, signal_only_enabled,
    format_entry_alert, format_execution_alert, format_position_alert,
    format_native_close_alert,
)
from intelligence.news_impact import assess_news_impact  # type: ignore  # News Intelligence
from intelligence.social_risk import assess_social_risk  # type: ignore  # Social Risk (Pulse)
from intelligence.trade_learning import TradeLearningEngine  # type: ignore  # Demo Trade Learning
from core.risk import Position as RiskPosition  # type: ignore  # Portfolio Awareness sync (roadmap Phase 2 item 9)

log = logging.getLogger("oracle.live")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid integer for %s=%r; using %d", name, raw, default)
        return default

# ── Continuous Trade Manager poll interval ───────────────────────────────────
# Independent of self.interval (the full symbol-scan cycle, default 300s).
# Open positions get checked this often regardless of how slow the full scan is.
_DEFAULT_MANAGE_INTERVAL = 15   # seconds; override with ORACLE_MANAGE_INTERVAL_SEC

# ── TRADER_ID: tags every Chronicle event so cross-script conflicts are visible ──
# Set TRADER_ID=live_trader_1 in .env (or leave as default "live_trader")
_TRADER_ID: str = os.getenv("TRADER_ID", "live_trader")

# ── FIX-1: expanded default watchlist ────────────────────────────────────────
CRYPTO_SYMBOLS: List[str] = [
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
    "LTCUSD", "DOGEUSD", "DOTUSD", "LINKUSD", "AVAXUSD", "MATICUSD",
    "BCHUSD", "UNIUSD", "XLMUSD", "ATOMUSD", "NEARUSD", "SHIBUSD",
    "TRXUSD", "TONUSD", "SUIUSD", "PEPEUSD",
]

DEFAULT_SYMBOLS: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",   # major forex pairs
    "XAUUSD", "USOIL",                          # commodities
    *CRYPTO_SYMBOLS,                            # crypto (22 pairs)
]

# ── ONE position per symbol cap ───────────────────────────────────────────────
MAX_POSITIONS_PER_SYMBOL: int = 1   # never stack more than this many positions per symbol

# ── FIX-2: per-symbol oracle call timeout ────────────────────────────────────
_DEFAULT_SYMBOL_TIMEOUT = 60   # seconds; override with ORACLE_SYMBOL_TIMEOUT_SEC

CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry"
]

def _unload_conflicting_modules():
    """Forcibly unload modules that cause namespace collisions between repositories."""
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


_MAX_INFLIGHT_TIMEOUT_CALLS = max(
    1,
    int(os.getenv("ORACLE_MAX_INFLIGHT_SYMBOL_EVALS", "1") or 1),
)
_TIMEOUT_CALL_SLOTS = threading.BoundedSemaphore(_MAX_INFLIGHT_TIMEOUT_CALLS)
_ACTIVE_TIMEOUT_KEYS_LOCK = threading.Lock()
_ACTIVE_TIMEOUT_KEYS: set[Any] = set()


def _call_with_timeout(
    fn,
    timeout_sec: float,
    *,
    work_key: Any = None,
    reason_out: Optional[List[str]] = None,
) -> Tuple[Any, bool]:
    """
    Call fn() in a daemon thread with bounded single-flight concurrency.
    Returns (result, timed_out).
    """
    if work_key is not None:
        with _ACTIVE_TIMEOUT_KEYS_LOCK:
            if work_key in _ACTIVE_TIMEOUT_KEYS:
                if reason_out is not None:
                    reason_out[:] = ["previous evaluation still running"]
                return None, True
            _ACTIVE_TIMEOUT_KEYS.add(work_key)

    if not _TIMEOUT_CALL_SLOTS.acquire(timeout=0.05):
        if work_key is not None:
            with _ACTIVE_TIMEOUT_KEYS_LOCK:
                _ACTIVE_TIMEOUT_KEYS.discard(work_key)
        if reason_out is not None:
            reason_out[:] = ["previous symbol evaluation still running"]
        return None, True

    result_box: List[Any] = [None]
    exc_box:    List[Optional[BaseException]] = [None]

    def _run():
        try:
            result_box[0] = fn()
        except Exception as e:
            exc_box[0] = e
        finally:
            if work_key is not None:
                with _ACTIVE_TIMEOUT_KEYS_LOCK:
                    _ACTIVE_TIMEOUT_KEYS.discard(work_key)
            _TIMEOUT_CALL_SLOTS.release()

    try:
        t = threading.Thread(target=_run, daemon=True)
        t.start()
    except Exception:
        if work_key is not None:
            with _ACTIVE_TIMEOUT_KEYS_LOCK:
                _ACTIVE_TIMEOUT_KEYS.discard(work_key)
        _TIMEOUT_CALL_SLOTS.release()
        raise

    t.join(timeout_sec)
    if t.is_alive():
        if reason_out is not None:
            reason_out[:] = [f"after {timeout_sec:.0f}s"]
        return None, True          # timed out
    if exc_box[0] is not None:
        raise exc_box[0]           # re-raise so caller can log it
    return result_box[0], False


def _duplicate_broker_symbol_mappings(
    symbols: List[str],
    mapper: "SymbolMapper",
) -> Dict[str, str]:
    first_owner: Dict[str, str] = {}
    duplicates: Dict[str, str] = {}
    for raw_symbol in symbols:
        symbol = raw_symbol.upper()
        broker_sym = mapper.translate(symbol)
        if not broker_sym:
            continue
        key = broker_sym.strip().upper()
        owner = first_owner.get(key)
        if owner is None:
            first_owner[key] = symbol
        elif owner != symbol:
            duplicates[symbol] = owner
    return duplicates


def _is_trading_disabled_rejection(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    retcode = result.get("retcode")
    reason = str(result.get("reason") or "").upper()
    comment = str(result.get("comment") or "").upper()
    if retcode == 10017:
        return True
    return (
        "TRADING_DISABLED" in reason
        or "TRADE_DISABLED" in reason
        or "TRADING IS DISABLED" in reason
        or "TRADING DISABLED" in comment
        or "TRADE DISABLED" in comment
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  FIX-8: SymbolMapper — Oracle canonical name → broker symbol name
# ═══════════════════════════════════════════════════════════════════════════════

# Hardcoded fallback aliases for symbols that brokers commonly rename.
# Keys are Oracle canonical names (uppercase).
# Values are ordered lists of broker names to try (first match wins).
_FALLBACK_ALIASES: Dict[str, List[str]] = {
    # Commodities
    "USOIL":   ["XTIUSD", "WTI", "WTIOIL", "CRUDEOIL", "OILCash", "USOIL.cash",
                 "USOIL.", "OIL", "USCRUDE"],
    "UKOIL":   ["XBRUSD", "BRENT", "BRENTOIL", "UKOIL.", "OILCash.uk"],
    "XAUUSD":  ["XAUUSDm", "XAUUSD.", "XAUUSDx", "XAUUSD+", "GOLD", "GOLDm",
                 "XAUUSD_i", "XAU/USD"],
    "XAGUSD":  ["XAGUSDm", "XAGUSD.", "SILVER", "SILVERm"],
    "XPTUSD":  ["XPTUSDm", "PLATINUM"],
    "NATGAS":  ["XNGUSD", "NATGAS.", "NG", "NGas"],
    # Crypto
    "BTCUSD":   ["BTCUSDm", "BTCUSD.", "BTC/USD", "BTCUSDT", "BTCUSDx", "BTCUSD#", "BTCUSD.i", "BTCUSD.a", "BTCUSD.pro"],
    "ETHUSD":   ["ETHUSDm", "ETHUSD.", "ETH/USD", "ETHUSDT", "ETHUSDx", "ETHUSD#", "ETHUSD.i", "ETHUSD.a", "ETHUSD.pro"],
    "SOLUSD":   ["SOLUSDm", "SOLUSD.", "SOL/USD", "SOLUSDT", "SOLUSDx", "SOLUSD#", "SOLUSD.i", "SOLUSD.a"],
    "XRPUSD":   ["XRPUSDm", "XRPUSD.", "XRP/USD", "XRPUSDT", "XRPUSDx", "XRPUSD#", "XRPUSD.i", "XRPUSD.a"],
    "BNBUSD":   ["BNBUSDm", "BNBUSD.", "BNB/USD", "BNBUSDT", "BNBUSDx", "BNBUSD#", "BNBUSD.i", "BNBUSD.a"],
    "ADAUSD":   ["ADAUSDm", "ADAUSD.", "ADA/USD", "ADAUSDT", "ADAUSDx", "ADAUSD#", "ADAUSD.i", "ADAUSD.a"],
    "LTCUSD":   ["LTCUSDm", "LTCUSD.", "LTC/USD", "LTCUSDT", "LTCUSDx", "LTCUSD#", "LTCUSD.i", "LTCUSD.a"],
    "DOGEUSD":  ["DOGEUSDm", "DOGEUSD.", "DOGE/USD", "DOGEUSDT", "DOGEUSDx", "DOGEUSD#", "DOGEUSD.i", "DOGEUSD.a"],
    "DOTUSD":   ["DOTUSDm", "DOTUSD.", "DOT/USD", "DOTUSDT", "DOTUSDx", "DOTUSD#", "DOTUSD.i", "DOTUSD.a"],
    "LINKUSD":  ["LINKUSDm", "LINKUSD.", "LINK/USD", "LINKUSDT", "LINKUSDx", "LINKUSD#", "LINKUSD.i", "LINKUSD.a"],
    "AVAXUSD":  ["AVAXUSDm", "AVAXUSD.", "AVAX/USD", "AVAXUSDT", "AVAXUSDx", "AVAXUSD#", "AVAXUSD.i", "AVAXUSD.a"],
    "MATICUSD": ["MATICUSDm", "MATICUSD.", "MATIC/USD", "MATICUSDT", "POLUSD", "POLUSDm", "POLUSDT", "MATICUSDx"],
    "BCHUSD":   ["BCHUSDm", "BCHUSD.", "BCH/USD", "BCHUSDT", "BCHUSDx", "BCHUSD#", "BCHUSD.i", "BCHUSD.a"],
    "UNIUSD":   ["UNIUSDm", "UNIUSD.", "UNI/USD", "UNIUSDT", "UNIUSDx", "UNIUSD#", "UNIUSD.i", "UNIUSD.a"],
    "XLMUSD":   ["XLMUSDm", "XLMUSD.", "XLM/USD", "XLMUSDT", "XLMUSDx", "XLMUSD#", "XLMUSD.i", "XLMUSD.a"],
    "ATOMUSD":  ["ATOMUSDm", "ATOMUSD.", "ATOM/USD", "ATOMUSDT", "ATOMUSDx", "ATOMUSD#", "ATOMUSD.i", "ATOMUSD.a"],
    "NEARUSD":  ["NEARUSDm", "NEARUSD.", "NEAR/USD", "NEARUSDT", "NEARUSDx", "NEARUSD#", "NEARUSD.i", "NEARUSD.a"],
    "SHIBUSD":  ["SHIBUSDm", "SHIBUSD.", "SHIB/USD", "SHIBUSDT", "1000SHIBUSD", "SHIBUSDx", "SHIBUSD#"],
    "TRXUSD":   ["TRXUSDm", "TRXUSD.", "TRX/USD", "TRXUSDT", "TRXUSDx", "TRXUSD#", "TRXUSD.i", "TRXUSD.a"],
    "TONUSD":   ["TONUSDm", "TONUSD.", "TON/USD", "TONUSDT", "TONUSDx", "TONUSD#", "TONUSD.i", "TONUSD.a"],
    "SUIUSD":   ["SUIUSDm", "SUIUSD.", "SUI/USD", "SUIUSDT", "SUIUSDx", "SUIUSD#", "SUIUSD.i", "SUIUSD.a"],
    "PEPEUSD":  ["PEPEUSDm", "PEPEUSD.", "PEPE/USD", "PEPEUSDT", "1000PEPEUSD", "PEPEUSDx", "PEPEUSD#"],
    # Forex — brokers sometimes add suffix letters
    "EURUSD":  ["EURUSDm", "EURUSD.", "EUR/USD"],
    "GBPUSD":  ["GBPUSDm", "GBPUSD.", "GBP/USD"],
    "USDJPY":  ["USDJPYm", "USDJPY.", "USD/JPY"],
    "AUDUSD":  ["AUDUSDm", "AUDUSD.", "AUD/USD"],
    "USDCAD":  ["USDCADm", "USDCAD.", "USD/CAD"],
    "USDCHF":  ["USDCHFm", "USDCHF.", "USD/CHF"],
    "NZDUSD":  ["NZDUSDm", "NZDUSD.", "NZD/USD"],
    "EURGBP":  ["EURGBPm", "EURGBP.", "EUR/GBP"],
    "EURJPY":  ["EURJPYm", "EURJPY.", "EUR/JPY"],
    "GBPJPY":  ["GBPJPYm", "GBPJPY.", "GBP/JPY"],
    # Indices
    "US30":    ["US30.", "DJ30", "DJIA", "WallSt30", "US30Cash"],
    "US500":   ["US500.", "SPX500", "SP500", "US500Cash"],
    "NAS100":  ["NAS100.", "NASDAQ", "NDX100", "NAS100Cash"],
    "GER40":   ["GER40.", "DAX40", "GER30", "DAX"],
    "UK100":   ["UK100.", "FTSE100", "FTSE"],
    "JPN225":  ["JPN225.", "NIKKEI", "JP225"],
}


class SymbolMapper:
    """
    Resolves Oracle canonical symbol names to the broker's actual symbol names.

    Resolution order (first match wins):
      1. Env-var overrides  BROKER_SYMBOL_MAP=USOIL:XTIUSD,BTCUSD:BTCUSDm
      2. Hardcoded fallback aliases (_FALLBACK_ALIASES)
      3. Fuzzy prefix/suffix matching against the live broker symbol list
      4. Exact match (canonical name IS the broker name)

    Call build(broker_symbols) once after MT5 connects to populate the map.
    Then use translate(oracle_symbol) → broker_symbol | None.
    """

    def __init__(self):
        self._map: Dict[str, str] = {}          # canonical → broker
        self._broker_symbols: List[str] = []    # all symbols the broker knows
        self._env_overrides: Dict[str, str] = self._parse_env_overrides()

    # ── env-var parsing ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_env_overrides() -> Dict[str, str]:
        """
        Parse BROKER_SYMBOL_MAP=USOIL:XTIUSD,BTCUSD:BTCUSDm
        Returns {canonical_upper: broker_name}
        """
        raw = os.getenv("BROKER_SYMBOL_MAP", "").strip()
        result: Dict[str, str] = {}
        if not raw:
            return result
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" not in pair:
                log.warning("BROKER_SYMBOL_MAP: ignoring malformed entry %r (expected KEY:VALUE)", pair)
                continue
            k, v = pair.split(":", 1)
            k, v = k.strip().upper(), v.strip()
            if k and v:
                result[k] = v
        return result

    # ── build the map ─────────────────────────────────────────────────────────

    def build(self, broker_symbols: List[str], oracle_symbols: List[str]) -> None:
        """
        Build the canonical → broker map for every Oracle symbol.
        broker_symbols: list of symbol names returned by mt5.symbols_get() (or broker.symbols()).
        oracle_symbols: the canonical names Oracle wants to trade.
        """
        self._broker_symbols = [s.upper() for s in broker_symbols]
        broker_set = set(self._broker_symbols)
        # keep original-case lookup: upper → original
        broker_orig: Dict[str, str] = {s.upper(): s for s in broker_symbols}

        self._map.clear()
        for canon in oracle_symbols:
            canon_up = canon.upper()
            resolved = self._resolve_one(canon_up, broker_set, broker_orig)
            if resolved:
                self._map[canon_up] = resolved

    def _resolve_one(self, canon: str, broker_set: set,
                     broker_orig: Dict[str, str]) -> Optional[str]:
        """Try all resolution strategies for one canonical symbol.

        FIX-3: fuzzy steps 4/5/6 now require:
          - broker symbol length >= 4  (rejects 3-char stock tickers like BTC)
          - FULL containment: broker.startswith(canon) OR canon.startswith(broker)
            (rejects partial overlaps like ETHUSD→ETHU or BTCUSD→BTC)
        """

        # 1. Env-var override
        if canon in self._env_overrides:
            candidate = self._env_overrides[canon].upper()
            if candidate in broker_set:
                return broker_orig[candidate]
            log.warning("BROKER_SYMBOL_MAP override %s→%s not found at broker; "
                        "falling through to auto-map", canon, self._env_overrides[canon])

        # 2. Exact match (canonical IS the broker name)
        if canon in broker_set:
            return broker_orig[canon]

        # 3. Hardcoded fallback aliases (explicit list — no quality filter needed)
        for alias in _FALLBACK_ALIASES.get(canon, []):
            if alias.upper() in broker_set:
                return broker_orig[alias.upper()]

        # ── Quality gate for fuzzy steps 4/5/6 ──────────────────────────────
        # Two separate helpers for the two containment directions:
        #
        # _quality_prefix(bsym): broker symbol is an EXTENSION of the canonical
        #   name (e.g. XAUUSDm extends XAUUSD).  Requires len >= 4.
        #
        # _quality_root(bsym): broker symbol is a ROOT that the canonical name
        #   starts with.  Requires len >= 5 to prevent 4-char partial prefixes
        #   like "ETHU" from matching "ETHUSD" (ETHUSD starts with ETHU, but
        #   ETHU is a stock ticker fragment, not a meaningful root).
        def _quality_prefix(bsym: str) -> bool:
            """bsym is an extension of canon (bsym starts with canon)."""
            return len(bsym) >= 4 and bsym.startswith(canon)

        def _quality_root(bsym: str) -> bool:
            """bsym is a root that canon starts with (canon starts with bsym)."""
            return len(bsym) >= 5 and canon.startswith(bsym)

        # 4. Fuzzy: broker symbol starts with the canonical name AND is at most
        #    4 chars longer (e.g. XAUUSDm, EURUSDm, USDJPYm)
        for bsym in broker_set:
            if _quality_prefix(bsym) and len(bsym) <= len(canon) + 4:
                return broker_orig[bsym]

        # 5. Fuzzy: canonical starts with broker symbol — broker symbol is a
        #    meaningful root (>= 5 chars).  Rejects ETHU (4 chars) → ETHUSD.
        for bsym in broker_set:
            if _quality_root(bsym):
                return broker_orig[bsym]

        # 6. Fuzzy: broker symbol contains the canonical name (e.g. XAUUSD.GOLD)
        for bsym in broker_set:
            if len(bsym) >= 4 and canon in bsym and len(bsym) <= len(canon) + 6:
                return broker_orig[bsym]

        return None   # unmapped

    # ── public API ────────────────────────────────────────────────────────────

    def translate(self, oracle_symbol: str) -> Optional[str]:
        """Return the broker symbol for an Oracle canonical name, or None if unmapped."""
        return self._map.get(oracle_symbol.upper())

    def is_mapped(self, oracle_symbol: str) -> bool:
        return oracle_symbol.upper() in self._map

    def log_map(self, oracle_symbols: List[str]) -> None:
        """Print the resolved symbol map to stdout on startup."""
        print("\n── Broker Symbol Map ──────────────────────────────────────────")
        for canon in oracle_symbols:
            broker = self._map.get(canon.upper())
            if broker:
                src = "env" if canon.upper() in self._env_overrides else "auto"
                same = " (same)" if broker.upper() == canon.upper() else ""
                print(f"  {canon:12s} → {broker}{same}  [{src}]")
            else:
                print(f"  {canon:12s} → *** UNMAPPED — will be skipped ***")
        print("───────────────────────────────────────────────────────────────\n")

    @property
    def unmapped(self) -> List[str]:
        return [s for s in self._map if self._map[s] is None]


# ═══════════════════════════════════════════════════════════════════════════════
#  LiveTrader
# ═══════════════════════════════════════════════════════════════════════════════

class LiveTrader:
    def __init__(self, symbols: List[str], interval_sec: int = 300,
                 session_max_loss_pct: float = 0.05, max_trades: Optional[int] = None,
                 confirm_live: bool = False):
        self.symbols   = [s.upper() for s in symbols]
        self.interval  = interval_sec
        self.session_max_loss_pct = session_max_loss_pct
        self.max_trades   = max_trades if max_trades is not None else _env_int(
            "ORACLE_MAX_TRADES_PER_SESSION", 10)
        self.confirm_live = bool(
            confirm_live
            or os.getenv("ORACLE_CONFIRM_LIVE", "").strip().lower() in ("1", "true", "yes", "on")
            or os.getenv("ORACLE_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes", "on")
        )
        self.signal_only = signal_only_enabled()
        self.telegram = TelegramNotifier()
        self._recent_signals = RecentSignalCache()
        if self.signal_only and not self.telegram.enabled:
            log.warning("Signal-only mode is ON, but Telegram credentials are missing; "
                        "approved signals will be printed locally only")
        elif not self.signal_only and self.telegram.enabled:
            log.info("ORACLE_SIGNAL_ONLY=false with Telegram configured: Oracle will execute MT5 trades AND send Telegram notifications")
        self.broker = MT5Broker()
        if not self.signal_only and os.getenv("ORACLE_PAPER_TRADING", "").strip().lower() in ("0", "false", "no", "off"):
            self.broker.paper = False
        self._trades_this_session = 0
        self._start_equity: Optional[float] = None
        self._open_context: Dict[str, Dict] = {}   # canonical symbol -> streams at entry

        # FIX-2: per-symbol timeout
        self._symbol_timeout: float = float(
            os.getenv("ORACLE_SYMBOL_TIMEOUT_SEC", str(_DEFAULT_SYMBOL_TIMEOUT))
        )

        # ---- kill-switch equity cache ----
        _ks_ttl = float(os.getenv("KILL_SWITCH_EQUITY_CACHE_SEC", "60"))
        self._ks_equity_ttl:   float = _ks_ttl
        self._ks_equity_cache: Optional[float] = None
        self._ks_equity_ts:    float = 0.0
        # FIX-6: latch so double-check in run() always sees the fired state
        self._ks_fired: bool = False
        self._ks_check_unavailable: bool = False
        self._trading_disabled_symbols: set[str] = set()

        # FIX-8: symbol mapper (populated in connect())
        self._sym_mapper = SymbolMapper()

        # ── Continuous Trade Manager ─────────────────────────────────────
        # A background thread polls every open, registered position on its
        # own fast interval (independent of self.interval, the full-scan
        # cadence), and applies trailing-stop / confidence-drop /
        # regime-change logic from execution.trade_manager.
        self._trade_manager = TradeManager(TradeManagerConfig())
        self._trade_learning = TradeLearningEngine()  # Demo Trade Learning (roadmap Phase 1 item 3)
        self._managed_positions: Dict[str, Position] = {}   # canonical symbol -> Position
        self._manage_interval: float = float(
            os.getenv("ORACLE_MANAGE_INTERVAL_SEC", str(_DEFAULT_MANAGE_INTERVAL))
        )
        self._manage_lock = threading.Lock()
        self._manage_stop_event = threading.Event()
        self._manage_thread: Optional[threading.Thread] = None

        # boot Oracle + its evidence peers
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
        self.oracle = OracleAgent(chronicle_client=self.chronicle, sentinel_client=self.sentinel,
                                 pulse_client=self.pulse, atlas_client=self.atlas)
        self.oracle.start()
        if hasattr(self.oracle, "data") and hasattr(self.oracle.data, "attach_broker"):
            self.oracle.data.attach_broker(self.broker, self._sym_mapper)
        # Use the ONE Chronicle-wired champion confidence tracker OracleAgent
        # already built, instead of TradeLearningEngine's own separate,
        # non-Chronicle-wired duplicate constructed above — a real bug found
        # this session: two independent trackers silently diverging.
        self._trade_learning.confidence = self.oracle._champion_confidence

        # FIX-DEDUP: Chronicle position log — cross-script duplicate prevention
        self._pos_log = ChroniclePositionLog(
            chronicle_agent=self.chronicle,
            trader_id=_TRADER_ID,
        )
        log.info("LiveTrader: TRADER_ID=%r", _TRADER_ID)

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self) -> Dict[str, Any]:
        status = self.broker.connect()
        if status.get("connected"):
            self._start_equity = status.get("equity") or status.get("balance")
            acct_type = str(status.get("account_type") or "unknown").lower()
            log.info("Connected: %s account, equity %.2f %s", status["account_type"],
                     self._start_equity or 0, status.get("currency", ""))
            if not self.signal_only:
                if acct_type in ("demo", "contest") and self.broker.paper:
                    log.info(
                        "ORACLE_SIGNAL_ONLY=false on MT5 %s account — disabling paper-mode block so MT5 orders execute",
                        acct_type,
                    )
                    self.broker.paper = False
                elif acct_type == "real" and (self.broker.paper or not self.broker.allow_live):
                    log.warning(
                        "Connected to REAL MT5 account with ORACLE_SIGNAL_ONLY=false, "
                        "but execution is gated (paper=%s, allow_live=%s). "
                        "Set ORACLE_PAPER_TRADING=false and ORACLE_ALLOW_LIVE=true in .env to place real-money orders.",
                        self.broker.paper,
                        self.broker.allow_live,
                    )

            # FIX-8: build symbol map now that MT5 is live
            self._build_symbol_map()
            try:
                mapped_syms = [
                    self._sym_mapper.translate(s) or s for s in self.symbols
                ]
                load_sched = getattr(self.broker, "load_market_schedules", None)
                if callable(load_sched):
                    load_sched(mapped_syms)
            except Exception as exc:
                log.debug("Could not pre-load MT5 market schedules: %s", exc)
        else:
            log.warning("MT5 not connected: %s", status.get("reason"))
            # Still build map with empty broker list so unmapped warnings show
            self._build_symbol_map(broker_symbols=[])
        if hasattr(self.oracle, "data") and hasattr(self.oracle.data, "attach_broker"):
            self.oracle.data.attach_broker(self.broker, self._sym_mapper)
        return status

    def _build_symbol_map(self, broker_symbols: Optional[List[str]] = None) -> None:
        """
        Query MT5 for all available symbols and build the canonical→broker map.
        Falls back gracefully if MT5 is not connected.
        """
        if broker_symbols is None:
            # Try to get the live symbol list from the broker
            try:
                raw = self.broker.symbols()   # MT5Broker.symbols() → list[str]
                broker_symbols = raw if isinstance(raw, list) else []
            except Exception as exc:
                log.warning("Could not fetch broker symbol list: %s — using empty list", exc)
                broker_symbols = []

        if not broker_symbols:
            log.warning("Broker returned 0 symbols — all Oracle symbols will be tried as-is "
                        "(exact match only). Add BROKER_SYMBOL_MAP overrides if needed.")
            # Treat every Oracle symbol as its own broker name (best-effort)
            broker_symbols = list(self.symbols)

        self._sym_mapper.build(broker_symbols, self.symbols)
        self._sym_mapper.log_map(self.symbols)

    # ── position helpers ──────────────────────────────────────────────────────

    def _get_open_position(self, broker_sym: str) -> Optional[Dict]:
        """
        Return the first open position for *broker_sym*, or None.

        FIX-DEDUP: Now uses broker.get_positions_by_symbol() which ALWAYS
        queries MT5 directly — this catches positions opened by the OTHER
        script (mt5_demo_trader) that aren't in our internal tracking dict.

        If a position is found that isn't in our _open_context, we adopt it
        (add it to broker's paper state if in paper mode) so it gets managed
        rather than duplicated.
        """
        try:
            # FIX-DEDUP: query MT5 directly, not just internal paper state
            positions = (self.broker.observed_positions_by_symbol(broker_sym)
                         if self.signal_only else
                         self.broker.get_positions_by_symbol(broker_sym))
        except Exception as exc:
            log.warning("Could not fetch positions for %s: %s", broker_sym, exc)
            # Fallback to old method
            try:
                positions = self.broker.positions()
                prefix = broker_sym[:6].upper()
                positions = [p for p in positions
                             if p.get("symbol", "").upper().startswith(prefix)]
            except Exception:
                return None

        if not positions:
            return None

        pos = positions[0]

        # FIX-DEDUP: adopt orphaned positions (opened by the other script)
        ticket = pos.get("ticket")
        if ticket is not None:
            self.broker.adopt_position(pos)   # no-op in live mode; updates paper dict

        return pos

    # ── Continuous Trade Manager ─────────────────────────────────────────────

    def _register_position(self, symbol: str, direction: str, entry_price: float,
                            stop: float, target: float, confidence: float,
                            size: float = 0.0, news_level: str = "none", news_reason: str = "",
                            social_level: str = "none", social_reason: str = "") -> None:
        """
        Call this once, right after a fill, so the background poll thread
        knows this position exists and what Oracle believed at entry.

        Also syncs into self.oracle.risk.portfolio.positions — this was
        previously never populated by the live trading path at all (only
        a separate, unused trade.execute path did this), which silently
        disabled the risk gate's max_positions/per-symbol-dedup checks and
        made Portfolio Awareness (currency concentration) impossible.
        """
        try:
            sig = self.oracle.act("trade.signal", {"symbol": symbol, "_sender": "live_trader"})
            entry_regime = (sig or {}).get("regime") or "ranging"
            entry_atr = (sig or {}).get("atr")
        except Exception as exc:
            log.warning("[%s] could not fetch entry regime, defaulting to 'ranging': %s",
                        symbol, exc)
            entry_regime = "ranging"
            entry_atr = None

        # Entry streams (per-source signal breakdown) were already captured
        # in _tick()'s fill branch for a different purpose; reuse them here
        # so the Demo Trade Learning engine can feed fusion.learn() properly
        # when this position eventually closes. Empty for backfilled/orphaned
        # positions (we never saw their original entry).
        entry_streams = dict(self._open_context.get(symbol, {}) or {})

        # Capture which Sentinel news terms were live at entry (one call,
        # not repeated every poll) so Demo Trade Learning can later report
        # each term's real-world accuracy back to Sentinel's term
        # reliability tracker (Tier 0 self-improvement). Best-effort —
        # empty dict if Sentinel is unavailable or returns nothing.
        entry_term_evidence = {"bullish": [], "bearish": []}
        try:
            news = self.sentinel.act("news.credibility", {"topics": [symbol], "_sender": "live_trader"}) \
                if self.sentinel else None
            for a in (news or {}).get("articles", []):
                mt = a.get("matched_terms") or {}
                entry_term_evidence["bullish"].extend(mt.get("bullish", []))
                entry_term_evidence["bearish"].extend(mt.get("bearish", []))
        except Exception as exc:
            log.warning("[%s] could not capture entry term evidence: %s", symbol, exc)

        dir_norm = Direction.BUY if direction in ("long", "buy") else Direction.SELL
        with self._manage_lock:
            self._managed_positions[symbol] = Position(
                symbol=symbol, direction=dir_norm, entry_price=entry_price,
                initial_stop=stop, initial_target=target,
                entry_confidence=confidence, entry_regime=entry_regime,
                entry_streams=entry_streams, entry_atr=entry_atr,
                entry_term_evidence=entry_term_evidence,
            )
            self._managed_positions[symbol].journal_id = self.oracle.trade_journal.log_entry(
                symbol, direction, confidence, entry_regime,
                news_level, news_reason, social_level, social_reason,
                entry_streams, size)

        risk_direction = "long" if dir_norm == Direction.BUY else "short"
        self.oracle.risk.portfolio.remove_by_symbol(symbol)   # replace, don't duplicate, on re-registration
        self.oracle.risk.portfolio.positions.append(
            RiskPosition(symbol, risk_direction, size, entry_price, stop, target))

        log.info("[%s] registered with Continuous Trade Manager: dir=%s entry=%.5f "
                 "stop=%.5f target=%.5f conf=%.3f regime=%s",
                 symbol, dir_norm.value, entry_price, stop, target, confidence, entry_regime)

    def _poll_managed_positions(self) -> None:
        """One pass: check every registered position against a fresh market
        snapshot and act on the Continuous Trade Manager's decision."""
        with self._manage_lock:
            symbols = list(self._managed_positions.keys())

        for symbol in symbols:
            with self._manage_lock:
                pos = self._managed_positions.get(symbol)
            if pos is None:
                continue

            broker_sym = self._sym_mapper.translate(symbol) or symbol
            live_pos = self._get_open_position(broker_sym)
            if not getattr(self.broker, "_positions_last_refresh_ok", True):
                log.warning(
                    "[%s] broker position refresh failed — keeping CTM state until MT5 responds",
                    symbol,
                )
                return
            if live_pos is None:
                # Position closed elsewhere (SL/TP hit at the broker, manual
                # close, another script) — stop tracking it. This is the
                # MOST common close path in real trading, so it's also where
                # Demo Trade Learning matters most. We don't know the exact
                # close price/time, so use the last snapshot this position
                # saw (best-effort — a few poll intervals of imprecision,
                # fine for a learning signal, not for exact accounting).
                log.info("[%s] no longer open at broker — deregistering from "
                         "Continuous Trade Manager", symbol)
                won_flag: Optional[bool] = None
                try:
                    outcome = self._trade_learning.record_close_once(
                        f"{symbol}:{pos.entry_time}:broker-close",
                        self.oracle, pos, exit_price=pos.last_price,
                        exit_confidence=pos.last_confidence, exit_regime=pos.last_regime,
                        exit_reason="closed at broker (SL/TP hit or manual close)")
                    if outcome is not None:
                        won_flag = bool(outcome.won)
                        if outcome.won:
                            self.oracle.risk.portfolio.record_win(symbol)
                        else:
                            self.oracle.risk.portfolio.record_loss(symbol)
                except Exception as exc:
                    log.warning("[%s] Demo Trade Learning failed on native close: %s", symbol, exc)
                if self.telegram.enabled:
                    self.telegram.send(
                        format_native_close_alert(
                            symbol, broker_sym, "broker", pos.last_price,
                            "closed at broker (SL/TP hit or manual close)", won=won_flag,
                        ),
                        f"position:{symbol}:{pos.entry_time}:native-close",
                    )
                self.oracle.risk.portfolio.remove_by_symbol(symbol)
                with self._manage_lock:
                    self._managed_positions.pop(symbol, None)
                continue

            if self.broker.status.connected and callable(getattr(self.broker, "market_state", None)):
                try:
                    market_state = self.broker.market_state(broker_sym)
                    if market_state.get("state") in {"CLOSED", "DAILY_BREAK", "HOLIDAY", "PRE_OPEN"}:
                        log.debug(
                            "[%s→%s] CTM skipped while market is %s (%s)",
                            symbol, broker_sym, market_state.get("state"), market_state.get("reason", ""),
                        )
                        continue
                except Exception as exc:
                    log.debug("[%s] market_state check failed in CTM: %s", symbol, exc)

            try:
                sig = self.oracle.act("trade.signal", {"symbol": symbol, "_sender": "live_trader"})
            except Exception as exc:
                log.warning("[%s] Continuous Trade Manager: could not fetch snapshot: %s",
                            symbol, exc)
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
            if news_assessment.level != "none":
                log.info("[%s] news impact: %s — %s", symbol, news_assessment.level,
                         news_assessment.reason)
            social_assessment = assess_social_risk(self.pulse, symbol)
            if social_assessment.level != "none":
                log.info("[%s] social risk: %s — %s", symbol, social_assessment.level,
                         social_assessment.reason)
            snap = MarketSnapshot(price=float(price), confidence=float(confidence), regime=regime,
                                   news_impact=news_assessment.level,
                                   social_risk=social_assessment.level,
                                   atr=current_atr)
            observed_stop = float(live_pos.get("sl") or 0.0)
            if observed_stop > 0 and abs(observed_stop - pos.initial_stop) > 1e-10:
                # Pick up a stop change the user applied manually in MT5.
                pos.current_stop = observed_stop
            prior_stop = pos.current_stop
            decision = self._trade_manager.evaluate(pos, snap)

            pos_id = live_pos.get("ticket") or live_pos.get("id")
            if self.signal_only:
                # The decision engine advances its stop internally when it
                # recommends a ratchet. Keep the actual baseline until the
                # user applies that recommendation manually.
                if decision.action == Action.TIGHTEN_STOP:
                    pos.current_stop = prior_stop
                if decision.action == Action.TIGHTEN_STOP:
                    try:
                        digits = int(self.broker._mt5.symbol_info(broker_sym).digits)
                        precision = max(0, digits - 1)
                    except Exception:
                        precision = 3
                    stop_key = format(float(decision.new_stop or 0.0), f".{precision}f")
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
                # In signal-only mode the manager is advisory. Do not call any
                # broker method that could modify or close this position.
                continue

            if decision.action == Action.HOLD:
                log.debug("[%s] CTM hold: %s", symbol, decision.reason)
                if decision.reason != pos.last_journaled_hold_reason:
                    self.oracle.trade_journal.log_hold(
                        symbol, decision.reason, decision.current_confidence, decision.current_regime)
                    pos.last_journaled_hold_reason = decision.reason
                continue

            if decision.action == Action.TIGHTEN_STOP:
                log.info("[%s] CTM tighten stop #%s -> %.5f: %s",
                         symbol, pos_id, decision.new_stop, decision.reason)
                print(f"[{symbol}→{broker_sym}] CTM TIGHTEN #{pos_id} "
                      f"stop->{decision.new_stop:.5f}  {decision.reason}")
                try:
                    mod = self.broker.modify_position(pos_id, stop_loss=decision.new_stop)
                    mod_status = str(mod.get("status", "unknown"))
                    if mod_status == "modified":
                        self._pos_log.log_modified(symbol, broker_sym, pos_id,
                                                    sl=decision.new_stop or 0.0,
                                                    tp=pos.initial_target)
                    if self.telegram.enabled:
                        stop_key = f"{float(decision.new_stop or 0.0):.5f}"
                        self.telegram.send(
                            format_position_alert(
                                symbol, pos_id, "tighten_stop", decision,
                                pos.initial_stop, pos.initial_target,
                                executed=True, execution_status=mod_status,
                            ),
                            f"position:{pos_id}:exec-tighten:{stop_key}",
                        )
                except Exception as exc:
                    log.warning("[%s] CTM: failed to modify stop for #%s: %s",
                                symbol, pos_id, exc)

            elif decision.action == Action.CLOSE:
                log.info("[%s] CTM close #%s: %s", symbol, pos_id, decision.reason)
                print(f"[{symbol}→{broker_sym}] CTM CLOSE #{pos_id}  {decision.reason}")
                try:
                    close_result = self.broker.close_position(pos_id)
                    close_status = str(close_result.get("status", "unknown"))
                    if close_status == "closed":
                        self._pos_log.log_closed(symbol, broker_sym, pos_id,
                                                  reason=decision.reason)
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
                        except Exception as exc:
                            log.warning("[%s] Demo Trade Learning failed on CTM close: %s",
                                        symbol, exc)
                        self.oracle.risk.portfolio.remove_by_symbol(symbol)
                        with self._manage_lock:
                            self._managed_positions.pop(symbol, None)
                    if self.telegram.enabled:
                        self.telegram.send(
                            format_position_alert(
                                symbol, pos_id, "close", decision,
                                pos.initial_stop, pos.initial_target,
                                executed=True, execution_status=close_status,
                            ),
                            f"position:{pos_id}:exec-close",
                        )
                except Exception as exc:
                    log.warning("[%s] CTM: failed to close #%s: %s", symbol, pos_id, exc)

    def _manage_loop(self) -> None:
        log.info("Continuous Trade Manager thread started (interval=%.0fs)",
                 self._manage_interval)
        while not self._manage_stop_event.is_set():
            try:
                self._poll_managed_positions()
            except Exception as exc:
                log.warning("Continuous Trade Manager poll error: %s", exc)
            self._manage_stop_event.wait(self._manage_interval)
        log.info("Continuous Trade Manager thread stopped")

    def start_trade_manager(self) -> None:
        if self._manage_thread is not None and self._manage_thread.is_alive():
            return
        self._manage_stop_event.clear()
        self._manage_thread = threading.Thread(
            target=self._manage_loop, name="ctm-poll", daemon=True)
        self._manage_thread.start()

    def stop_trade_manager(self) -> None:
        self._manage_stop_event.set()
        if self._manage_thread is not None:
            self._manage_thread.join(timeout=5)

    # ── the loop ──────────────────────────────────────────────────────────────

    def run(self, cycles: Optional[int] = None) -> None:
        status = self.connect()
        if not status.get("connected"):
            print("Cannot start live trading:", status.get("reason"))
            print("Oracle will still compute signals; execution is disabled until MT5 connects.")
        print(f"\nLive trader started. Symbols={self.symbols} interval={self.interval}s")
        print(f"Account: {status.get('account_type')} | paper={self.broker.paper} "
              f"| allow_live={self.broker.allow_live} | signal_only={self.signal_only}")
        print(f"Symbol timeout: {self._symbol_timeout}s | max open positions: {self.max_trades}")
        print(f"Continuous Trade Manager: polling every {self._manage_interval:.0f}s "
              f"(independent of the {self.interval}s scan cycle)")
        print("Press Ctrl+C to stop (positions are NOT auto-closed on stop; use kill switch).\n")

        self.start_trade_manager()

        cycle = 0
        try:
            while cycles is None or cycle < cycles:
                cycle += 1
                cycle_label = f"{cycle}/{cycles}" if cycles else str(cycle)
                print(f"\n{'─'*56}")
                print(f"  Cycle {cycle_label} — scanning {len(self.symbols)} symbols")
                print(f"{'─'*56}")

                # FIX-2/3: _tick() now returns a summary dict, never hangs
                summary = self._tick()

                # FIX-4: cycle summary line
                print(
                    f"\nCycle {cycle_label} done — scanned {summary['scanned']} symbols | "
                    f"{summary['signals']} signal(s) | {summary['trades']} trade(s) | {summary['holds']} hold | "
                    f"{summary['rejects']} reject | {summary['errors']} error | "
                    f"{summary['timeouts']} timeout | {summary['unmapped']} unmapped | "
                    f"{summary['managed']} managed"
                )

                # FIX-6: kill-switch check uses latched state
                if summary["kill_switch"] or self._kill_switch_check():
                    if self.signal_only:
                        print("KILL SWITCH: session loss limit hit. Stopping signal scans; "
                              "Oracle will not close positions in signal-only mode.")
                        self.telegram.send(
                            "Oracle risk kill switch reached the session loss limit. "
                            "Signal scans are stopping. Review your open positions and "
                            "manage them manually; Oracle has not closed them.",
                            "risk:session-kill-switch",
                        )
                    else:
                        print("KILL SWITCH: session loss limit hit. Flattening + stopping.")
                        close_all_res = self.broker.close_all()
                        print(close_all_res)
                        if self.telegram.enabled:
                            self.telegram.send(
                                "Oracle risk kill switch reached the session loss limit. "
                                "All open MT5 positions have been flattened and scans are stopping.",
                                "risk:session-kill-switch-exec",
                            )
                    break

                if cycles is None or cycle < cycles:
                    print(f"Sleeping {self.interval}s until next cycle…")
                    time.sleep(self.interval)

        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            self._learn_from_closed()
            self.shutdown()

    # ── per-cycle tick ────────────────────────────────────────────────────────

    def _tick(self) -> Dict[str, int]:
        """
        Scan every symbol once.  Never hangs — each oracle.act() call is wrapped
        in a daemon-thread timeout.

        FIX-POS: Before opening any new position, check if one already exists
        for that symbol.  If yes → re-analyse and manage (hold/modify/close).
        If no → proceed with normal entry logic.
        MAX_POSITIONS_PER_SYMBOL = 1 is enforced.

        FIX-DEDUP: Before opening, also query Chronicle to check if the OTHER
        script (mt5_demo_trader) has already opened a position for this symbol.
        After a successful fill, log the opened event to Chronicle.

        Returns a summary dict:
          scanned / trades / holds / rejects / errors / timeouts / unmapped /
          managed / kill_switch
        """
        summary = dict(scanned=0, trades=0, signals=0, holds=0, rejects=0,
                       errors=0, timeouts=0, unmapped=0, managed=0,
                       kill_switch=False)
        approved_candidates: List[Tuple[str, str, Dict[str, Any]]] = []
        duplicate_mappings = _duplicate_broker_symbol_mappings(self.symbols, self._sym_mapper)

        for symbol in self.symbols:
            # kill-switch check before each symbol (uses cached equity)
            if self._kill_switch_check():
                summary["kill_switch"] = True
                return summary

            # FIX-5: skip expensive signal call once session cap is reached
            # FIX-9: resolve broker symbol before doing anything
            broker_sym = self._sym_mapper.translate(symbol)
            if broker_sym is None:
                log.warning("[%s] UNMAPPED — no matching broker symbol found; skipping", symbol)
                print(f"[{symbol}] UNMAPPED  (no broker symbol found — add "
                      f"BROKER_SYMBOL_MAP={symbol}:<broker_name> to .env to fix)")
                summary["unmapped"] += 1
                continue

            duplicate_owner = duplicate_mappings.get(symbol)
            if duplicate_owner is not None:
                log.warning(
                    "[%s→%s] DUPLICATE MAP — broker symbol already assigned to %s; skipping",
                    symbol, broker_sym, duplicate_owner,
                )
                print(
                    f"[{symbol}→{broker_sym}] DUPLICATE MAP  already assigned to {duplicate_owner} "
                    f"(override with BROKER_SYMBOL_MAP={symbol}:<broker_name>)"
                )
                summary["unmapped"] += 1
                continue

            summary["scanned"] += 1

            # ── FIX-POS: check for existing position FIRST ────────────────────
            # The Continuous Trade Manager (background thread, polls every
            # self._manage_interval seconds) owns monitoring/trailing/closing
            # for any position it has registered. The slow full-symbol scan
            # just needs to avoid opening a second position on top of it.
            existing_pos = self._get_open_position(broker_sym)
            if not getattr(self.broker, "_positions_last_refresh_ok", True):
                log.warning(
                    "[%s→%s] SKIP (position query failed) — avoiding new entry while MT5 position state is unknown",
                    symbol, broker_sym,
                )
                print(f"[{symbol}→{broker_sym}] ERROR  position query failed; skipping new entry")
                summary["errors"] += 1
                continue
            if existing_pos is not None:
                summary["managed"] += 1
                summary["holds"] += 1
                if symbol not in self._managed_positions:
                    # Orphaned position (adopted from a prior run, or opened
                    # by another script) — the CTM doesn't know about it yet.
                    # Best-effort backfill: we don't know the *original* entry
                    # confidence/regime, so use price_open as entry price and
                    # a conservative default stop/target derived from it.
                    log.info("[%s] found pre-existing position not tracked by "
                             "Continuous Trade Manager — backfilling registration",
                             symbol)
                    entry_price = existing_pos.get("price_open") or existing_pos.get("price") or 0.0
                    direction = "long" if str(existing_pos.get("type", "")).lower() == "buy" else "short"
                    recent = self._recent_signals.match(symbol, direction, existing_pos.get("time"))
                    if recent:
                        self._open_context[symbol] = recent.get("streams") or {}
                        sl = float(recent["plan"].get("stop") or 0.0)
                        tp = float(recent["plan"].get("target") or 0.0)
                        confidence = float(recent.get("effective_confidence", 0.5))
                    else:
                        sl = float(existing_pos.get("sl") or 0.0)
                        tp = float(existing_pos.get("tp") or 0.0)
                        confidence = 0.5
                    if self.signal_only and (sl <= 0 or tp <= 0):
                        self.telegram.send(
                            f"Oracle found {symbol} position #{existing_pos.get('ticket')}, "
                            "but cannot safely monitor it: no matching recent Oracle signal "
                            "and the position is missing a stop loss or take profit. Add both "
                            "levels in MT5 or wait for a matching Oracle signal. No changes were made.",
                            f"position:{existing_pos.get('ticket')}:missing-baseline",
                        )
                        log.warning("[%s] signal-only monitoring skipped: position lacks a "
                                    "matching signal or complete broker SL/TP", symbol)
                        continue
                    if not self.signal_only:
                        sl = sl or (entry_price * (0.995 if direction == "long" else 1.005))
                        tp = tp or (entry_price * (1.01 if direction == "long" else 0.99))
                    self._register_position(symbol, direction, entry_price=entry_price,
                                             stop=sl, target=tp, confidence=confidence,
                                             size=existing_pos.get("volume", 0.0))
                log.debug("[%s] position already open — Continuous Trade Manager "
                          "owns it; skipping new entry this cycle", symbol)
                continue   # never open a new position on the same symbol this cycle
            # ── end FIX-POS ───────────────────────────────────────────────────

            if len(self._managed_positions) >= self.max_trades:
                log.info("[%s] new entry skipped — max open positions reached; "
                         "existing positions remain monitored", symbol)
                continue

            if broker_sym.upper() in self._trading_disabled_symbols:
                reason = "broker rejected symbol with TRADING_DISABLED earlier in this session"
                log.info("[%s→%s] HOLD (trading disabled: %s)", symbol, broker_sym, reason)
                print(f"[{symbol}→{broker_sym}] HOLD   (trading disabled: {reason})")
                summary["holds"] += 1
                continue

            if self.broker.status.connected and callable(getattr(self.broker, "market_state", None)):
                try:
                    market_state = self.broker.market_state(broker_sym)
                    if not market_state.get("can_open", True):
                        reason = market_state.get("reason") or str(market_state.get("state", "closed")).lower()
                        log.info("[%s→%s] HOLD (market %s: %s)",
                                 symbol, broker_sym, market_state.get("state"), reason)
                        print(f"[{symbol}→{broker_sym}] HOLD   (market {market_state.get('state')}: {reason})")
                        summary["holds"] += 1
                        continue
                except Exception as exc:
                    log.debug("[%s] market_state pre-check failed: %s", symbol, exc)

            # ── FIX-DEDUP: check Chronicle for cross-script open positions ────
            if self._pos_log.has_open_position(symbol, broker_sym):
                log.info("[%s] DEDUP: Chronicle shows another script has an open "
                         "position — skipping new entry this cycle", symbol)
                print(f"[{symbol}→{broker_sym}] DEDUP: Chronicle shows open position "
                      f"from another trader — skipping entry (TRADER_ID={_TRADER_ID})")
                summary["holds"] += 1
                continue
            # ── end FIX-DEDUP ─────────────────────────────────────────────────

            # ── 1. SIGNAL (with timeout) ──────────────────────────────────────
            log.info("[%s] no open position — evaluating entry…", symbol)
            print(f"[{symbol}→{broker_sym}] no open position — evaluating entry…")
            timeout_reason: List[str] = []
            try:
                sig, timed_out = _call_with_timeout(
                    lambda sym=symbol: self.oracle.act(
                        "trade.propose", {"symbol": sym, "_sender": "live_trader"}
                    ),
                    self._symbol_timeout,
                    work_key=(id(self), symbol),
                    reason_out=timeout_reason,
                )
            except Exception as exc:
                # oracle.act raised — log and move on
                log.warning("[%s] ERROR during oracle.act: %s", symbol, exc)
                print(f"[{symbol}] ERROR  {exc}")
                summary["errors"] += 1
                continue

            # FIX-2: timeout path
            if timed_out:
                detail = timeout_reason[0] if timeout_reason else f"after {self._symbol_timeout:.0f}s"
                log.warning("[%s] TIMEOUT %s — skipping", symbol, detail)
                print(f"[{symbol}] TIMEOUT {detail} — skipping")
                summary["timeouts"] += 1
                continue

            # ── 2. Classify the signal outcome ────────────────────────────────
            status = (sig or {}).get("status")

            if status != "complete":
                # Distinguish hold vs risk-gate reject vs other error
                message = (sig or {}).get("message", "unknown")
                risk    = (sig or {}).get("risk") or {}
                reasons = risk.get("reasons")

                if "hold" in message.lower():
                    log.info("[%s] HOLD   (%s)", symbol, message)
                    print(f"[{symbol}] HOLD   ({message})")
                    summary["holds"] += 1

                elif "risk gate" in message.lower() or reasons:
                    conf_str = ""
                    try:
                        conf_str = f"conf={sig['signal']['confidence']:.3f}  "
                    except Exception as exc:
                        log.debug("could not format confidence for display: %s", exc)
                    log.info("[%s] REJECT %sreasons=%s", symbol, conf_str, reasons)
                    print(f"[{symbol}] REJECT {conf_str}reasons={reasons}")
                    summary["rejects"] += 1

                else:
                    log.info("[%s] ERROR  %s", symbol, message)
                    print(f"[{symbol}] ERROR  {message}")
                    summary["errors"] += 1

                continue   # always move to next symbol — never freeze here

            # ── 3. Approved candidate — collect for CIO Cross-Sectional Tournament ─
            approved_candidates.append((symbol, broker_sym, sig))

        # ── Phase 2: CIO Cross-Sectional Tournament & Portfolio Selection ──────
        if approved_candidates:
            approved_candidates.sort(
                key=lambda item: (
                    float(item[2].get("scout_score", item[2].get("effective_confidence", 0.0))),
                    float(item[2].get("effective_confidence", 0.0)),
                ),
                reverse=True,
            )
            cio = getattr(self.oracle, "cio", None)
            ledger = getattr(self.oracle, "decision_ledger", None)
            learned_slots = (
                cio.portfolio.learned_session_slot_budget(candidate_universe_size=len(self.symbols))
                if cio is not None else self.max_trades
            )
            session_slot_cap = min(self.max_trades, learned_slots) if self.max_trades else learned_slots

            if len(approved_candidates) > 1:
                ranking_preview = ", ".join(
                    f"#{idx} {sym} (scout={cand.get('scout_score', cand.get('effective_confidence', 0.0)):.3f})"
                    for idx, (sym, _, cand) in enumerate(approved_candidates, start=1)
                )
                log.info("CIO Scout Tournament ranked %d setups (session slot budget=%d): %s",
                         len(approved_candidates), session_slot_cap, ranking_preview)
                print(f"  [CIO Scout Tournament] Ranked {len(approved_candidates)} setups "
                      f"(slot cap={session_slot_cap}): {ranking_preview}")

            for rank, (symbol, broker_sym, sig) in enumerate(approved_candidates, start=1):
                plan = dict(sig["plan"])
                s = sig["signal"]
                scout_score = float(sig.get("scout_score", sig.get("effective_confidence", s.get("confidence", 0.0))))
                eff_conf = float(sig.get("effective_confidence", s.get("confidence", 0.0)))
                conf_str = f"scout={scout_score:.3f} conf={eff_conf:.3f} rank=#{rank}/{len(approved_candidates)}"

                # Check if session slot budget is now full after higher-ranked fills
                if len(self._managed_positions) >= session_slot_cap:
                    reason = (
                        f"Outranked for session slots (rank #{rank}/{len(approved_candidates)}, "
                        f"active_positions={len(self._managed_positions)} >= slot_cap={session_slot_cap})"
                    )
                    log.info("[%s] OUTRANKED %s — %s", symbol, conf_str, reason)
                    print(f"[{symbol}] OUTRANKED {conf_str}  ({reason})")
                    if ledger is not None:
                        ledger.record_trade_not_entered(
                            symbol=symbol,
                            direction=plan.get("direction", "hold"),
                            rejection_stage="outranked_for_session_slots",
                            primary_reason=reason,
                            reasons=[reason],
                            scout_score=scout_score,
                            entry_confidence=eff_conf,
                            learned_conviction_floor=float(sig.get("learned_conviction_floor", 0.5)),
                            tournament_rank=rank,
                            total_candidates=len(approved_candidates),
                            regime=str((sig.get("scout_evaluation") or {}).get("regime") or "ranging"),
                            news_impact=str(sig.get("news_impact", "none")),
                            news_reason=str(sig.get("news_reason", "")),
                            social_risk=str(sig.get("social_risk", "none")),
                            social_reason=str(sig.get("social_reason", "")),
                            hypothetical_entry_price=float(plan.get("entry") or 0.0),
                            hypothetical_stop=float(plan.get("stop") or 0.0),
                            hypothetical_target=float(plan.get("target") or 0.0),
                            atr=float(sig.get("atr") or 0.0),
                            entry_streams=dict(sig.get("_streams") or {}),
                        )
                    summary["rejects"] += 1
                    continue

                # Re-verify dynamic correlation & portfolio gate against positions opened earlier in this cycle
                if cio is not None and self.oracle.risk.portfolio.positions:
                    open_tuples = [
                        (p.symbol, p.direction) for p in self.oracle.risk.portfolio.positions
                    ]
                    corr_ok, corr_reasons, max_corr = cio.risk.evaluate_correlation_and_cooldown(
                        symbol=symbol,
                        direction=plan.get("direction", "long"),
                        open_positions=open_tuples,
                        market_data=self.oracle.data,
                        portfolio_state=self.oracle.risk.portfolio,
                    )
                    if not corr_ok:
                        reason = "; ".join(corr_reasons)
                        log.info("[%s] REJECT (intra-cycle correlation/cooldown) %s: %s",
                                 symbol, conf_str, reason)
                        print(f"[{symbol}] REJECT (correlation/cooldown) {conf_str}  reasons={corr_reasons}")
                        if ledger is not None:
                            ledger.record_trade_not_entered(
                                symbol=symbol,
                                direction=plan.get("direction", "hold"),
                                rejection_stage="correlation_or_cooldown_rejected",
                                primary_reason=corr_reasons[0] if corr_reasons else "correlation rejected",
                                reasons=corr_reasons,
                                scout_score=scout_score,
                                entry_confidence=eff_conf,
                                learned_conviction_floor=float(sig.get("learned_conviction_floor", 0.5)),
                                tournament_rank=rank,
                                total_candidates=len(approved_candidates),
                                regime=str((sig.get("scout_evaluation") or {}).get("regime") or "ranging"),
                                news_impact=str(sig.get("news_impact", "none")),
                                news_reason=str(sig.get("news_reason", "")),
                                social_risk=str(sig.get("social_risk", "none")),
                                social_reason=str(sig.get("social_reason", "")),
                                hypothetical_entry_price=float(plan.get("entry") or 0.0),
                                hypothetical_stop=float(plan.get("stop") or 0.0),
                                hypothetical_target=float(plan.get("target") or 0.0),
                                atr=float(sig.get("atr") or 0.0),
                                entry_streams=dict(sig.get("_streams") or {}),
                                correlation_snapshot={"max_signed_correlation": max_corr},
                            )
                        summary["rejects"] += 1
                        continue

                self._open_context[symbol] = sig.get("_streams", {})

                if self.signal_only:
                    alert = format_entry_alert(symbol, sig)
                    delivered = self.telegram.send(alert, f"entry:{symbol}:{plan.get('direction', '')}")
                    if delivered:
                        self._recent_signals.remember(symbol, {
                            "direction": plan.get("direction", ""),
                            "plan": dict(plan),
                            "effective_confidence": eff_conf,
                            "streams": sig.get("_streams", {}),
                        })
                    summary["signals"] += 1
                    log.info("[%s→%s] SIGNAL ONLY direction=%s %s Telegram=%s",
                             symbol, broker_sym, plan["direction"], conf_str,
                             "sent" if delivered else ("configured; delivery skipped/failed" if self.telegram.enabled else "not configured"))
                    print(f"[{symbol}→{broker_sym}] SIGNAL ONLY  direction={plan['direction']}  "
                          f"{conf_str}  Telegram={'sent' if delivered else 'not sent'}")
                    if not self.telegram.enabled:
                        print(alert)
                    continue

                broker_plan = dict(plan)
                broker_plan["broker_symbol"] = broker_sym
                entry_ref = float(plan.get("entry") or 0.0)
                atr_ref = float(sig.get("atr") or 0.0)
                if entry_ref > 0 and not getattr(self.broker, "paper", False):
                    broker_plan.setdefault("rebase_stops_to_market", True)
                    broker_plan.setdefault(
                        "max_entry_deviation",
                        max(atr_ref * 1.5, entry_ref * 0.005),
                    )

                if self.broker.status.connected:
                    result = self.broker.place_order(broker_plan, human_confirm=self.confirm_live)
                    res_status = result.get("status", "unknown")
                    res_reason = result.get("reason", "")
                    if _is_trading_disabled_rejection(result):
                        self._trading_disabled_symbols.add(broker_sym.upper())
                        log.warning(
                            "[%s→%s] broker reported TRADING_DISABLED — suppressing further entry attempts this session",
                            symbol, broker_sym,
                        )
                    log.info("[%s→%s] TRADE  direction=%s %s -> %s %s",
                             symbol, broker_sym, plan["direction"], conf_str, res_status, res_reason)
                    print(f"[{symbol}→{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                          f"  -> {res_status}  {res_reason}")
                    if res_status == "filled":
                        self._trades_this_session += 1
                        summary["trades"] += 1
                        ticket = result.get("order") or result.get("ticket") or 0
                        fill_price = float(result.get("price") or plan.get("entry", 0.0))
                        fill_vol = float(result.get("volume", plan.get("size", 0.0)))
                        fill_sl = float(result.get("sl") or plan.get("stop", 0.0))
                        fill_tp = float(result.get("tp") or plan.get("target", 0.0))
                        self._pos_log.log_opened(
                            symbol, broker_sym, ticket,
                            direction=plan["direction"],
                            volume=fill_vol,
                            price=fill_price,
                            sl=fill_sl,
                            tp=fill_tp,
                        )
                        self._register_position(
                            symbol, plan["direction"],
                            entry_price=fill_price,
                            stop=fill_sl,
                            target=fill_tp,
                            confidence=s.get("confidence", 0.0),
                            size=fill_vol,
                            news_level=sig.get("news_impact", "none"),
                            news_reason=sig.get("news_reason", ""),
                            social_level=sig.get("social_risk", "none"),
                            social_reason=sig.get("social_reason", ""),
                        )
                        self.oracle.risk.portfolio.record_trade_opened(symbol)
                        if self.telegram.enabled:
                            exec_alert = format_execution_alert(symbol, broker_sym, sig, result)
                            delivered = self.telegram.send(
                                exec_alert,
                                f"exec:filled:{symbol}:{ticket or plan.get('direction', '')}",
                            )
                            log.info("[%s→%s] Telegram execution alert=%s",
                                     symbol, broker_sym, "sent" if delivered else "skipped/failed")
                        if ledger is not None:
                            pos_obj = self._managed_positions.get(symbol)
                            ledger.record_trade_entered(
                                symbol=symbol,
                                direction=plan["direction"],
                                entry_price=fill_price,
                                stop_price=fill_sl,
                                target_price=fill_tp,
                                size=fill_vol,
                                scout_score=scout_score,
                                entry_confidence=eff_conf,
                                learned_conviction_floor=float(sig.get("learned_conviction_floor", 0.5)),
                                tournament_rank=rank,
                                total_candidates=len(approved_candidates),
                                regime=str(getattr(pos_obj, "entry_regime", "ranging")),
                                news_impact=str(sig.get("news_impact", "none")),
                                news_reason=str(sig.get("news_reason", "")),
                                social_risk=str(sig.get("social_risk", "none")),
                                social_reason=str(sig.get("social_reason", "")),
                                entry_streams=dict(sig.get("_streams") or {}),
                                journal_id=getattr(pos_obj, "journal_id", None),
                            )
                    else:
                        summary["rejects"] += 1
                        if self.telegram.enabled:
                            exec_alert = format_execution_alert(symbol, broker_sym, sig, result)
                            self.telegram.send(
                                exec_alert,
                                f"exec:{res_status}:{symbol}:{plan.get('direction', '')}:{str(res_reason)[:40]}",
                            )
                        if ledger is not None:
                            ledger.expose_failure(
                                component="MT5Broker",
                                operation="place_order",
                                error_type=str(res_status),
                                error_message=str(res_reason or "broker rejected order"),
                                symbol=symbol,
                            )
                else:
                    log.info("[%s→%s] TRADE  direction=%s %s (execution disabled: MT5 not connected)",
                             symbol, broker_sym, plan["direction"], conf_str)
                    print(f"[{symbol}→{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                          f"  (execution disabled: MT5 not connected)")
                    summary["trades"] += 1
                    if self.telegram.enabled:
                        exec_alert = format_execution_alert(
                            symbol, broker_sym, sig,
                            {"status": "blocked", "reason": "MT5 not connected"},
                        )
                        self.telegram.send(
                            exec_alert,
                            f"exec:disconnected:{symbol}:{plan.get('direction', '')}",
                        )

        return summary

    # ── kill switch ───────────────────────────────────────────────────────────

    def _kill_switch_check(self) -> bool:
        # FIX-6: once fired, stay fired
        if self._ks_fired:
            return True
        if self._start_equity is None or self._start_equity <= 0 or not self.broker.status.connected:
            return False
        _now = time.time()
        if self._ks_equity_cache is None or (_now - self._ks_equity_ts) >= self._ks_equity_ttl:
            acct = self.broker.account()
            raw_equity = acct.get("equity")
            account_ok = acct.get("account_info_available")
            try:
                equity_val = float(raw_equity) if raw_equity is not None else None
            except (TypeError, ValueError):
                equity_val = None
            if (
                not acct.get("connected", True)
                or account_ok is False
                or equity_val is None
                or not math.isfinite(equity_val)
                or equity_val <= 0
            ):
                if not self._ks_check_unavailable:
                    log.warning(
                        "Kill switch check skipped: MT5 account equity unavailable (%s)",
                        acct.get("reason") or "account_info_available=False",
                    )
                    self._ks_check_unavailable = True
                return False
            self._ks_check_unavailable = False
            self._ks_equity_cache = equity_val
            self._ks_equity_ts = _now
        equity = self._ks_equity_cache
        if equity is None or equity <= 0:
            return False
        loss = (self._start_equity - equity) / self._start_equity if self._start_equity else 0
        if loss >= self.session_max_loss_pct:
            self._ks_fired = True
        return self._ks_fired

    # ── learning ──────────────────────────────────────────────────────────────

    def _learn_from_closed(self) -> None:
        """Feed realized direction of closed positions back into adaptive fusion."""
        for canon_sym, streams in self._open_context.items():
            # FIX-10: look up positions using the broker symbol, but report back
            # to Oracle using the canonical name so AdaptiveFusion keys match.
            broker_sym = self._sym_mapper.translate(canon_sym) or canon_sym
            poss = [p for p in self.broker.positions()
                    if p["symbol"].upper().startswith(broker_sym[:6].upper())]
            if not poss:
                continue
            realized = 1 if sum(p["profit"] for p in poss) >= 0 else -1
            self.oracle.act("fusion.learn", {"symbol": canon_sym, "streams": streams,
                                            "realized_direction": realized,
                                            "trade_won": realized > 0,
                                            "_sender": "live_trader"})

    # ── external controls ─────────────────────────────────────────────────────

    def kill(self) -> Dict[str, Any]:
        if self.signal_only:
            return {"status": "signal_only", "message":
                    "Oracle is in signal-only mode; manage positions manually."}
        return self.broker.close_all()

    def shutdown(self) -> None:
        """Stop Oracle and all peer agents. Safe to call multiple times."""
        self.stop_trade_manager()
        try:
            self.oracle.stop()
        except Exception as exc:
            log.warning("failed to stop oracle: %s", exc)
        for peer in (self.pulse, self.sentinel, self.atlas, self.chronicle):
            if peer:
                try:
                    peer.stop()
                except Exception as exc:
                    log.warning("failed to stop peer %s: %s", peer, exc)
        self.broker.disconnect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="Oracle live trader on MetaTrader 5")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                    help=f"symbols to trade (default: {' '.join(DEFAULT_SYMBOLS)})")
    ap.add_argument("--interval",  type=int,   default=300,  help="seconds between cycles")
    ap.add_argument("--cycles",    type=int,   default=None, help="stop after N cycles (default: run forever)")
    ap.add_argument("--max-trades",type=int,   default=None)
    ap.add_argument("--session-max-loss", type=float, default=0.05, help="fraction; kill switch")
    ap.add_argument("--confirm-live", action="store_true",
                   help="required to place orders on a REAL account")
    ap.add_argument("--evolve-first", action="store_true",
                   help="evolve a strategy per symbol before trading")
    args = ap.parse_args()

    trader = LiveTrader(
        symbols=[s.upper() for s in args.symbols],
        interval_sec=args.interval,
        session_max_loss_pct=args.session_max_loss,
        max_trades=args.max_trades,
        confirm_live=args.confirm_live,
    )

    if args.evolve_first:
        for sym in trader.symbols:
            print(f"Evolving strategy for {sym}…")
            out = trader.oracle.act("strategy.evolve",
                                    {"symbol": sym, "generations": 6, "_sender": "live"})
            print(f"  {sym}: promoted={out.get('promoted_new_champion')} "
                  f"oos_return={(out.get('out_of_sample') or {}).get('total_return')}")

    print("=" * 64)
    print("  ORACLE LIVE TRADER (MetaTrader 5)")
    print("  Risk-gated. Broker-side stops. Kill switch. Paper by default.")
    print("=" * 64)
    print("  SAFETY: set MT5_LOGIN/PASSWORD/SERVER in .env. Real-money trading also")
    print("  requires ORACLE_PAPER_TRADING=false, ORACLE_ALLOW_LIVE=true, --confirm-live.")
    try:
        trader.run(cycles=args.cycles)
    except Exception:
        # shutdown() is called in run()'s finally block; nothing to do here
        raise
    print("Live trader shutdown complete.")


if __name__ == "__main__":
    main()
