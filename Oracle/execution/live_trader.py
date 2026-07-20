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

log = logging.getLogger("oracle.live")

# ── TRADER_ID: tags every Chronicle event so cross-script conflicts are visible ──
# Set TRADER_ID=live_trader_1 in .env (or leave as default "live_trader")
_TRADER_ID: str = os.getenv("TRADER_ID", "live_trader")

# ── FIX-1: expanded default watchlist ────────────────────────────────────────
DEFAULT_SYMBOLS: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",   # major forex pairs
    "XAUUSD", "USOIL",                          # commodities
    "BTCUSD", "ETHUSD",                         # crypto
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


def _call_with_timeout(fn, timeout_sec: float) -> Tuple[Any, bool]:
    """
    Call fn() in a daemon thread.  Returns (result, timed_out).
    If the thread doesn't finish within timeout_sec, returns (None, True).
    The thread is left to die on its own (daemon=True) — we never block on it.
    """
    result_box: List[Any] = [None]
    exc_box:    List[Optional[BaseException]] = [None]

    def _run():
        try:
            result_box[0] = fn()
        except Exception as e:
            exc_box[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        return None, True          # timed out
    if exc_box[0] is not None:
        raise exc_box[0]           # re-raise so caller can log it
    return result_box[0], False


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
    "BTCUSD":  ["BTCUSDm", "BTCUSD.", "BTC/USD", "BTCUSDT", "BTCUSDx"],
    "ETHUSD":  ["ETHUSDm", "ETHUSD.", "ETH/USD", "ETHUSDT", "ETHUSDx"],
    "LTCUSD":  ["LTCUSDm", "LTCUSD.", "LTC/USD"],
    "XRPUSD":  ["XRPUSDm", "XRPUSD.", "XRP/USD"],
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
                 session_max_loss_pct: float = 0.05, max_trades: int = 10,
                 confirm_live: bool = False):
        self.symbols   = [s.upper() for s in symbols]
        self.interval  = interval_sec
        self.session_max_loss_pct = session_max_loss_pct
        self.max_trades   = max_trades
        self.confirm_live = confirm_live
        self.broker = MT5Broker()
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

        # FIX-8: symbol mapper (populated in connect())
        self._sym_mapper = SymbolMapper()

        # boot Oracle + its evidence peers
        _unload_conflicting_modules()
        self.chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent")
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
            log.info("Connected: %s account, equity %.2f %s", status["account_type"],
                     self._start_equity or 0, status.get("currency", ""))

            # FIX-8: build symbol map now that MT5 is live
            self._build_symbol_map()
        else:
            log.warning("MT5 not connected: %s", status.get("reason"))
            # Still build map with empty broker list so unmapped warnings show
            self._build_symbol_map(broker_symbols=[])
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
            positions = self.broker.get_positions_by_symbol(broker_sym)
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

    def _manage_existing_position(self, symbol: str, broker_sym: str,
                                  pos: Dict) -> str:
        """
        Re-analyse an existing position and decide: HOLD, MODIFY SL/TP, or CLOSE.

        Returns one of: "hold", "modified", "closed", "error"
        """
        pos_id    = pos.get("ticket") or pos.get("id") or "?"
        pos_dir   = pos.get("type", "").lower()   # "buy" or "sell"
        pos_profit = pos.get("profit", 0.0)

        log.info("[%s] existing %s position #%s (P&L %.2f) — re-analysing…",
                 symbol, pos_dir.upper(), pos_id, pos_profit)
        print(f"[{symbol}→{broker_sym}] EXISTING {pos_dir.upper()} position "
              f"#{pos_id}  P&L={pos_profit:+.2f}  — re-analysing…")

        # Ask Oracle for a fresh signal
        try:
            sig, timed_out = _call_with_timeout(
                lambda sym=symbol: self.oracle.act(
                    "trade.propose", {"symbol": sym, "_sender": "live_trader"}
                ),
                self._symbol_timeout,
            )
        except Exception as exc:
            log.warning("[%s] ERROR re-analysing existing position: %s", symbol, exc)
            print(f"[{symbol}] ERROR re-analysing: {exc}  — keeping position")
            return "error"

        if timed_out:
            log.warning("[%s] TIMEOUT re-analysing existing position — keeping", symbol)
            print(f"[{symbol}] TIMEOUT re-analysing — keeping position")
            return "hold"

        new_status = (sig or {}).get("status")

        # Defensive: oracle may return a non-dict for "signal" (float, str, None)
        _raw_signal = (sig or {}).get("signal")
        if not isinstance(_raw_signal, dict):
            if _raw_signal is not None:
                log.warning(
                    "[%s] unexpected type for 'signal' key: %s (%r) — treating as no signal",
                    symbol, type(_raw_signal).__name__, _raw_signal,
                )
                print(f"[{symbol}] WARNING: oracle returned signal={_raw_signal!r} "
                      f"(type={type(_raw_signal).__name__}) — expected dict; treating as HOLD")
            _raw_signal = {}
        new_signal = _raw_signal

        # Safely coerce direction to str — oracle may put a float/int/None there
        _raw_dir = new_signal.get("direction", "hold")
        if not isinstance(_raw_dir, str):
            log.warning(
                "[%s] 'direction' is %s (%r) — coercing to str",
                symbol, type(_raw_dir).__name__, _raw_dir,
            )
            print(f"[{symbol}] WARNING: oracle direction={_raw_dir!r} "
                  f"(type={type(_raw_dir).__name__}) — coercing to str")
        new_dir  = str(_raw_dir).strip().lower() if _raw_dir is not None else "hold"
        new_conf = new_signal.get("confidence", 0.0)
        if not isinstance(new_conf, (int, float)):
            new_conf = 0.0

        # Map "long"/"short" to "buy"/"sell" for comparison
        _dir_map = {"long": "buy", "short": "sell", "buy": "buy", "sell": "sell"}
        new_dir_norm = _dir_map.get(new_dir, new_dir)
        pos_dir_norm = _dir_map.get(pos_dir, pos_dir)

        # Signal reversed or went neutral → close the position
        if new_status != "complete" or new_dir_norm in ("hold", "") or \
                (new_dir_norm and new_dir_norm != pos_dir_norm):
            reason = (
                f"signal reversed to {new_dir.upper()}" if new_dir_norm and new_dir_norm != pos_dir_norm
                else f"signal is now {new_dir.upper() or 'HOLD'}"
            )
            log.info("[%s] CLOSE position #%s — %s (conf=%.3f)",
                     symbol, pos_id, reason, new_conf)
            print(f"[{symbol}→{broker_sym}] CLOSE position #{pos_id} — {reason}  conf={new_conf:.3f}")
            try:
                close_result = self.broker.close_position(pos_id)
                log.info("[%s] close result: %s", symbol, close_result)
                print(f"[{symbol}→{broker_sym}] close → {close_result.get('status', close_result)}")
                # FIX-DEDUP: log close event to Chronicle
                if close_result.get("status") == "closed":
                    self._pos_log.log_closed(symbol, broker_sym, pos_id, reason=reason)
                return "closed"
            except AttributeError:
                log.warning("[%s] broker.close_position() not available; "
                            "position kept (add it to MT5Broker)", symbol)
                print(f"[{symbol}] WARNING: broker.close_position() not implemented — "
                      f"position kept. Add close_position(ticket) to MT5Broker.")
                return "hold"
            except Exception as exc:
                log.warning("[%s] ERROR closing position #%s: %s", symbol, pos_id, exc)
                print(f"[{symbol}] ERROR closing #{pos_id}: {exc}")
                return "error"

        # Signal agrees with position direction → optionally update SL/TP
        new_plan = (sig or {}).get("plan") or {}
        new_sl   = new_plan.get("stop_loss")
        new_tp   = new_plan.get("take_profit")
        old_sl   = pos.get("sl")
        old_tp   = pos.get("tp")

        sl_changed = new_sl is not None and new_sl != old_sl
        tp_changed = new_tp is not None and new_tp != old_tp

        if (sl_changed or tp_changed) and self.broker.status.connected:
            log.info("[%s] MODIFY position #%s — SL %s→%s  TP %s→%s  conf=%.3f",
                     symbol, pos_id, old_sl, new_sl, old_tp, new_tp, new_conf)
            print(f"[{symbol}→{broker_sym}] MODIFY #{pos_id}  "
                  f"SL {old_sl}→{new_sl}  TP {old_tp}→{new_tp}  conf={new_conf:.3f}")
            try:
                mod_result = self.broker.modify_position(pos_id,
                                                         stop_loss=new_sl,
                                                         take_profit=new_tp)
                print(f"[{symbol}→{broker_sym}] modify → {mod_result.get('status', mod_result)}")
                # FIX-DEDUP: log modify event to Chronicle
                if mod_result.get("status") == "modified":
                    self._pos_log.log_modified(symbol, broker_sym, pos_id,
                                               sl=new_sl or 0.0, tp=new_tp or 0.0)
                return "modified"
            except AttributeError:
                log.warning("[%s] broker.modify_position() not available; "
                            "SL/TP not updated (add it to MT5Broker)", symbol)
                print(f"[{symbol}] WARNING: broker.modify_position() not implemented — "
                      f"SL/TP not updated. Add modify_position(ticket, sl, tp) to MT5Broker.")
                return "hold"
            except Exception as exc:
                log.warning("[%s] ERROR modifying position #%s: %s", symbol, pos_id, exc)
                return "error"

        # Signal agrees, SL/TP unchanged → just hold
        log.info("[%s] HOLD existing %s position #%s — signal still %s  conf=%.3f",
                 symbol, pos_dir.upper(), pos_id, new_dir.upper(), new_conf)
        print(f"[{symbol}→{broker_sym}] HOLD existing {pos_dir.upper()} #{pos_id}  "
              f"signal={new_dir.upper()}  conf={new_conf:.3f}")
        return "hold"

    # ── the loop ──────────────────────────────────────────────────────────────

    def run(self, cycles: Optional[int] = None) -> None:
        status = self.connect()
        if not status.get("connected"):
            print("Cannot start live trading:", status.get("reason"))
            print("Oracle will still compute signals; execution is disabled until MT5 connects.")
        print(f"\nLive trader started. Symbols={self.symbols} interval={self.interval}s")
        print(f"Account: {status.get('account_type')} | paper={self.broker.paper} "
              f"| allow_live={self.broker.allow_live}")
        print(f"Symbol timeout: {self._symbol_timeout}s | max_trades: {self.max_trades}")
        print("Press Ctrl+C to stop (positions are NOT auto-closed on stop; use kill switch).\n")

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
                    f"{summary['trades']} trade(s) | {summary['holds']} hold | "
                    f"{summary['rejects']} reject | {summary['errors']} error | "
                    f"{summary['timeouts']} timeout | {summary['unmapped']} unmapped | "
                    f"{summary['managed']} managed"
                )

                # FIX-6: kill-switch check uses latched state
                if summary["kill_switch"] or self._kill_switch_check():
                    print("KILL SWITCH: session loss limit hit. Flattening + stopping.")
                    print(self.broker.close_all())
                    break

                if self._trades_this_session >= self.max_trades:
                    print("Max trades for session reached. Stopping new entries.")
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
        summary = dict(scanned=0, trades=0, holds=0, rejects=0,
                       errors=0, timeouts=0, unmapped=0, managed=0,
                       kill_switch=False)

        for symbol in self.symbols:
            # kill-switch check before each symbol (uses cached equity)
            if self._kill_switch_check():
                summary["kill_switch"] = True
                return summary

            # FIX-5: skip expensive signal call once session cap is reached
            if self._trades_this_session >= self.max_trades:
                log.info("[%s] skipped — session trade cap reached", symbol)
                break

            # FIX-9: resolve broker symbol before doing anything
            broker_sym = self._sym_mapper.translate(symbol)
            if broker_sym is None:
                log.warning("[%s] UNMAPPED — no matching broker symbol found; skipping", symbol)
                print(f"[{symbol}] UNMAPPED  (no broker symbol found — add "
                      f"BROKER_SYMBOL_MAP={symbol}:<broker_name> to .env to fix)")
                summary["unmapped"] += 1
                continue

            summary["scanned"] += 1

            # ── FIX-POS: check for existing position FIRST ────────────────────
            existing_pos = self._get_open_position(broker_sym)
            if existing_pos is not None:
                outcome = self._manage_existing_position(symbol, broker_sym, existing_pos)
                summary["managed"] += 1
                if outcome == "hold":
                    summary["holds"] += 1
                elif outcome in ("modified", "closed"):
                    pass   # position managed; don't open a new one this cycle
                else:
                    summary["errors"] += 1
                continue   # never open a new position on the same symbol this cycle
            # ── end FIX-POS ───────────────────────────────────────────────────

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
            try:
                sig, timed_out = _call_with_timeout(
                    lambda sym=symbol: self.oracle.act(
                        "trade.propose", {"symbol": sym, "_sender": "live_trader"}
                    ),
                    self._symbol_timeout,
                )
            except Exception as exc:
                # oracle.act raised — log and move on
                log.warning("[%s] ERROR during oracle.act: %s", symbol, exc)
                print(f"[{symbol}] ERROR  {exc}")
                summary["errors"] += 1
                continue

            # FIX-2: timeout path
            if timed_out:
                log.warning("[%s] TIMEOUT after %.0fs — skipping", symbol, self._symbol_timeout)
                print(f"[{symbol}] TIMEOUT after {self._symbol_timeout:.0f}s — skipping")
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
                    except Exception:
                        pass
                    log.info("[%s] REJECT %sreasons=%s", symbol, conf_str, reasons)
                    print(f"[{symbol}] REJECT {conf_str}reasons={reasons}")
                    summary["rejects"] += 1

                else:
                    log.info("[%s] ERROR  %s", symbol, message)
                    print(f"[{symbol}] ERROR  {message}")
                    summary["errors"] += 1

                continue   # always move to next symbol — never freeze here

            # ── 3. We have an approved plan — execute ─────────────────────────
            plan = sig["plan"]
            s    = sig["signal"]
            self._open_context[symbol] = sig.get("_streams", {})

            conf_str = f"conf={s.get('confidence', 0):.3f}"

            # FIX-9/11: build broker_plan with BOTH keys:
            #   "symbol"        = Oracle canonical name  (kept for learning loop + logging)
            #   "broker_symbol" = translated broker name (used by MT5Broker.place_order v2)
            broker_plan = dict(plan)
            broker_plan["broker_symbol"] = broker_sym   # FIX-11: separate key, not overwrite

            if self.broker.status.connected:
                result = self.broker.place_order(broker_plan, human_confirm=self.confirm_live)
                res_status = result.get("status", "unknown")
                res_reason = result.get("reason", "")
                log.info("[%s→%s] TRADE  direction=%s %s -> %s %s",
                         symbol, broker_sym, plan["direction"], conf_str, res_status, res_reason)
                print(f"[{symbol}→{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                      f"  -> {res_status}  {res_reason}")
                if res_status == "filled":
                    self._trades_this_session += 1
                    summary["trades"] += 1
                    # FIX-DEDUP: log opened event to Chronicle so other script sees it
                    ticket = result.get("order") or result.get("ticket") or 0
                    self._pos_log.log_opened(
                        symbol, broker_sym, ticket,
                        direction=plan["direction"],
                        volume=result.get("volume", plan.get("size", 0)),
                        price=result.get("price", 0.0),
                        sl=plan.get("stop", 0.0),
                        tp=plan.get("target", 0.0),
                    )
                else:
                    # broker rejected (e.g. market closed, insufficient margin)
                    summary["rejects"] += 1
            else:
                log.info("[%s→%s] TRADE  direction=%s %s (execution disabled: MT5 not connected)",
                         symbol, broker_sym, plan["direction"], conf_str)
                print(f"[{symbol}→{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                      f"  (execution disabled: MT5 not connected)")
                # count as a "trade signal" even though we couldn't execute
                summary["trades"] += 1

        return summary

    # ── kill switch ───────────────────────────────────────────────────────────

    def _kill_switch_check(self) -> bool:
        # FIX-6: once fired, stay fired
        if self._ks_fired:
            return True
        if self._start_equity is None or not self.broker.status.connected:
            return False
        _now = time.time()
        if self._ks_equity_cache is None or (_now - self._ks_equity_ts) >= self._ks_equity_ttl:
            acct = self.broker.account()
            self._ks_equity_cache = acct.get("equity", self._start_equity)
            self._ks_equity_ts = _now
        equity = self._ks_equity_cache
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
                                            "_sender": "live_trader"})

    # ── external controls ─────────────────────────────────────────────────────

    def kill(self) -> Dict[str, Any]:
        return self.broker.close_all()

    def shutdown(self) -> None:
        """Stop Oracle and all peer agents. Safe to call multiple times."""
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
    ap.add_argument("--max-trades",type=int,   default=10)
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