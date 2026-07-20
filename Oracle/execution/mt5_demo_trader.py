"""
Oracle.execution.mt5_demo_trader
================================
Demo / paper trading loop — identical signal pipeline to live_trader.py but
with 41 symbols (forex, crypto, indices, commodities) and MetaQuotes-Demo
broker symbol overrides pre-loaded.

The ONLY differences from live_trader.py:
  1. DEFAULT_SYMBOLS has 41 symbols instead of 8.
  2. MetaQuotes-Demo symbol overrides are pre-loaded in _DEMO_BROKER_MAP so
     you don't need to set BROKER_SYMBOL_MAP in .env.
  3. Class is named DemoTrader (not LiveTrader).
  4. Log name is oracle.demo.
  5. Paper trading is forced ON by default (ORACLE_PAPER_TRADING=false still
     overrides if you explicitly want live execution on a demo account).

Same pipeline as live_trader.py:
  1. SIGNAL     Oracle produces an evidence-fused, evolved-strategy signal.
  2. RISK GATE  RiskManager sizes + gates the trade (nothing bypasses it).
  3. EXECUTE    MT5Broker submits a market order WITH broker-side stop + target.
  4. MONITOR    positions + account equity are polled; a session drawdown limit
                acts as a kill switch (close_all + stop).
  5. LEARN      realized outcomes feed Oracle's adaptive fusion.

Champion genomes: loaded from Oracle/benchmarks/ exactly as in live_trader.
Agent correlation: Sentinel (news), Pulse (social), Atlas (research) are all
  loaded and wired into OracleAgent for confidence boosting — same as live.

Run:
    python -m Oracle.execution.mt5_demo_trader           # from ecosystem root
    python -m Oracle.execution.mt5_demo_trader --symbols EURUSD GBPUSD
    python -m Oracle.execution.mt5_demo_trader --preset live   # same 8 as live_trader
    python -m Oracle.execution.mt5_demo_trader --evolve-first  # evolve then trade
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

_REPO_ROOT = Path(__file__).resolve().parents[1]   # Oracle/
_ECO_ROOT  = _REPO_ROOT.parent                     # Universe/ (ecosystem root)
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution.mt5_broker import MT5Broker  # type: ignore
from execution.chronicle_position_log import ChroniclePositionLog  # type: ignore  # FIX-DEDUP

log = logging.getLogger("oracle.demo")

# ── TRADER_ID: tags every Chronicle event so cross-script conflicts are visible ──
# Set TRADER_ID=demo_trader_1 in .env (or leave as default "demo_trader")
_TRADER_ID: str = os.getenv("TRADER_ID", "demo_trader")

# ── 41-symbol default watchlist ───────────────────────────────────────────────
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
]

# Same 8 symbols as live_trader.py (use --preset live)
_LIVE_PRESET: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "USOIL",
    "BTCUSD", "ETHUSD",
]

# ── ONE position per symbol cap ───────────────────────────────────────────────
MAX_POSITIONS_PER_SYMBOL: int = 1   # never stack more than this many positions per symbol

# ── MetaQuotes-Demo broker symbol overrides ───────────────────────────────────
# These are pre-loaded so you don't need BROKER_SYMBOL_MAP in .env.
# The SymbolMapper env-var overrides take priority over these if set.
_DEMO_BROKER_MAP: Dict[str, str] = {
    # Crypto (MetaQuotes-Demo uses 'm' suffix)
    "BTCUSD":  "BTCUSDm",
    "ETHUSD":  "ETHUSDm",
    "SOLUSD":  "SOLUSDm",
    "XRPUSD":  "XRPUSDm",
    "BNBUSD":  "BNBUSDm",
    "ADAUSD":  "ADAUSDm",
    # Commodities
    "USOIL":   "XTIUSD",
    "UKOIL":   "XBRUSD",
    "NATGAS":  "XNGUSD",
    # Indices
    "NAS100":  "NAS100",   # same on MetaQuotes-Demo
    "US30":    "US30",
    "US500":   "US500",
    "GER40":   "GER40",
    "UK100":   "UK100",
    "JPN225":  "JPN225",
    "AUS200":  "AUS200",
}

# ── per-symbol oracle call timeout ────────────────────────────────────────────
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
        return None, True
    if exc_box[0] is not None:
        raise exc_box[0]
    return result_box[0], False


# ══════════════════════════════════════════════════════════════════════════════
#  SymbolMapper — Oracle canonical name → broker symbol name
# ══════════════════════════════════════════════════════════════════════════════

_FALLBACK_ALIASES: Dict[str, List[str]] = {
    "USOIL":   ["XTIUSD", "WTI", "WTIOIL", "CRUDEOIL", "OILCash", "USOIL.cash", "OIL"],
    "UKOIL":   ["XBRUSD", "BRENT", "BRENTOIL"],
    "XAUUSD":  ["XAUUSDm", "XAUUSD.", "GOLD", "GOLDm"],
    "XAGUSD":  ["XAGUSDm", "XAGUSD.", "SILVER"],
    "XPTUSD":  ["XPTUSDm", "PLATINUM"],
    "NATGAS":  ["XNGUSD", "NATGAS.", "NG", "NGas"],
    "BTCUSD":  ["BTCUSDm", "BTCUSD.", "BTC/USD", "BTCUSDT"],
    "ETHUSD":  ["ETHUSDm", "ETHUSD.", "ETH/USD", "ETHUSDT"],
    "SOLUSD":  ["SOLUSDm", "SOLUSD.", "SOL/USD"],
    "XRPUSD":  ["XRPUSDm", "XRPUSD.", "XRP/USD"],
    "BNBUSD":  ["BNBUSDm", "BNBUSD.", "BNB/USD"],
    "ADAUSD":  ["ADAUSDm", "ADAUSD.", "ADA/USD"],
    "LTCUSD":  ["LTCUSDm", "LTCUSD.", "LTC/USD"],
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
    "AUDJPY":  ["AUDJPYm", "AUDJPY.", "AUD/JPY"],
    "CADJPY":  ["CADJPYm", "CADJPY.", "CAD/JPY"],
    "CHFJPY":  ["CHFJPYm", "CHFJPY.", "CHF/JPY"],
    "EURAUD":  ["EURAUDm", "EURAUD.", "EUR/AUD"],
    "EURCAD":  ["EURCADm", "EURCAD.", "EUR/CAD"],
    "EURCHF":  ["EURCHFm", "EURCHF.", "EUR/CHF"],
    "GBPAUD":  ["GBPAUDm", "GBPAUD.", "GBP/AUD"],
    "GBPCAD":  ["GBPCADm", "GBPCAD.", "GBP/CAD"],
    "GBPCHF":  ["GBPCHFm", "GBPCHF.", "GBP/CHF"],
    "AUDCAD":  ["AUDCADm", "AUDCAD.", "AUD/CAD"],
    "AUDCHF":  ["AUDCHFm", "AUDCHF.", "AUD/CHF"],
    "AUDNZD":  ["AUDNZDm", "AUDNZD.", "AUD/NZD"],
    "US30":    ["US30.", "DJ30", "DJIA", "WallSt30"],
    "US500":   ["US500.", "SPX500", "SP500"],
    "NAS100":  ["NAS100.", "NASDAQ", "NDX100"],
    "GER40":   ["GER40.", "DAX40", "GER30", "DAX"],
    "UK100":   ["UK100.", "FTSE100", "FTSE"],
    "JPN225":  ["JPN225.", "NIKKEI", "JP225"],
    "AUS200":  ["AUS200.", "ASX200"],
}


class SymbolMapper:
    def __init__(self, demo_overrides: Optional[Dict[str, str]] = None):
        self._map: Dict[str, str] = {}
        self._broker_symbols: List[str] = []
        self._env_overrides: Dict[str, str] = self._parse_env_overrides()
        # Pre-load MetaQuotes-Demo overrides (lower priority than env-var)
        self._demo_overrides: Dict[str, str] = {
            k.upper(): v for k, v in (demo_overrides or _DEMO_BROKER_MAP).items()
        }

    @staticmethod
    def _parse_env_overrides() -> Dict[str, str]:
        raw = os.getenv("BROKER_SYMBOL_MAP", "").strip()
        result: Dict[str, str] = {}
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

    def build(self, broker_symbols: List[str], oracle_symbols: List[str]) -> None:
        self._broker_symbols = [s.upper() for s in broker_symbols]
        broker_set = set(self._broker_symbols)
        broker_orig: Dict[str, str] = {s.upper(): s for s in broker_symbols}
        self._map.clear()
        for canon in oracle_symbols:
            canon_up = canon.upper()
            resolved = self._resolve_one(canon_up, broker_set, broker_orig)
            if resolved:
                self._map[canon_up] = resolved

    def _resolve_one(self, canon: str, broker_set: set,
                     broker_orig: Dict[str, str]) -> Optional[str]:
        # 1. Env-var override (highest priority)
        if canon in self._env_overrides:
            candidate = self._env_overrides[canon].upper()
            if candidate in broker_set:
                return broker_orig[candidate]

        # 2. Exact match
        if canon in broker_set:
            return broker_orig[canon]

        # 3. MetaQuotes-Demo pre-loaded overrides
        if canon in self._demo_overrides:
            candidate = self._demo_overrides[canon].upper()
            if candidate in broker_set:
                return broker_orig[candidate]
            # Demo override not in broker list — fall through to fuzzy

        # 4. Hardcoded fallback aliases
        for alias in _FALLBACK_ALIASES.get(canon, []):
            if alias.upper() in broker_set:
                return broker_orig[alias.upper()]

        # 5/6/7. Fuzzy matching (quality-gated)
        def _quality_prefix(bsym: str) -> bool:
            return len(bsym) >= 4 and bsym.startswith(canon)

        def _quality_root(bsym: str) -> bool:
            return len(bsym) >= 5 and canon.startswith(bsym)

        for bsym in broker_set:
            if _quality_prefix(bsym) and len(bsym) <= len(canon) + 4:
                return broker_orig[bsym]

        for bsym in broker_set:
            if _quality_root(bsym):
                return broker_orig[bsym]

        for bsym in broker_set:
            if len(bsym) >= 4 and canon in bsym and len(bsym) <= len(canon) + 6:
                return broker_orig[bsym]

        return None

    def translate(self, oracle_symbol: str) -> Optional[str]:
        return self._map.get(oracle_symbol.upper())

    def is_mapped(self, oracle_symbol: str) -> bool:
        return oracle_symbol.upper() in self._map

    def log_map(self, oracle_symbols: List[str]) -> None:
        print("\n── Broker Symbol Map ──────────────────────────────────────────")
        for canon in oracle_symbols:
            broker = self._map.get(canon.upper())
            if broker:
                src = "env" if canon.upper() in self._env_overrides else \
                      "demo" if canon.upper() in self._demo_overrides else "auto"
                same = " (same)" if broker.upper() == canon.upper() else ""
                print(f"  {canon:12s} → {broker}{same}  [{src}]")
            else:
                print(f"  {canon:12s} → *** UNMAPPED — will be skipped ***")
        print("───────────────────────────────────────────────────────────────\n")

    @property
    def unmapped(self) -> List[str]:
        return [s for s in self._map if self._map[s] is None]


# ══════════════════════════════════════════════════════════════════════════════
#  DemoTrader  (identical pipeline to LiveTrader — 41 symbols, paper by default)
# ══════════════════════════════════════════════════════════════════════════════

class DemoTrader:
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
        self._open_context: Dict[str, Dict] = {}

        self._symbol_timeout: float = float(
            os.getenv("ORACLE_SYMBOL_TIMEOUT_SEC", str(_DEFAULT_SYMBOL_TIMEOUT))
        )

        _ks_ttl = float(os.getenv("KILL_SWITCH_EQUITY_CACHE_SEC", "60"))
        self._ks_equity_ttl:   float = _ks_ttl
        self._ks_equity_cache: Optional[float] = None
        self._ks_equity_ts:    float = 0.0
        self._ks_fired: bool = False

        self._sym_mapper = SymbolMapper()

        # Boot Oracle + evidence peers (same as live_trader)
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
        self.oracle = OracleAgent(
            chronicle_client=self.chronicle,
            sentinel_client=self.sentinel,
            pulse_client=self.pulse,
            atlas_client=self.atlas,
        )
        self.oracle.start()

        # FIX-DEDUP: Chronicle position log — cross-script duplicate prevention
        self._pos_log = ChroniclePositionLog(
            chronicle_agent=self.chronicle,
            trader_id=_TRADER_ID,
        )
        log.info("DemoTrader: TRADER_ID=%r", _TRADER_ID)

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self) -> Dict[str, Any]:
        status = self.broker.connect()
        if status.get("connected"):
            self._start_equity = status.get("equity") or status.get("balance")
            log.info("Connected: %s account, equity %.2f %s", status["account_type"],
                     self._start_equity or 0, status.get("currency", ""))
            self._build_symbol_map()
        else:
            log.warning("MT5 not connected: %s", status.get("reason"))
            self._build_symbol_map(broker_symbols=[])
        return status

    def _build_symbol_map(self, broker_symbols: Optional[List[str]] = None) -> None:
        if broker_symbols is None:
            try:
                raw = self.broker.symbols()
                broker_symbols = raw if isinstance(raw, list) else []
            except Exception as exc:
                log.warning("Could not fetch broker symbol list: %s", exc)
                broker_symbols = []

        if not broker_symbols:
            log.warning("Broker returned 0 symbols — using demo overrides + canonical names")
            # Seed with demo override values so they appear in the broker set
            broker_symbols = list(self.symbols) + list(_DEMO_BROKER_MAP.values())

        self._sym_mapper.build(broker_symbols, self.symbols)
        self._sym_mapper.log_map(self.symbols)

    # ── position helpers ──────────────────────────────────────────────────────

    def _get_open_position(self, broker_sym: str) -> Optional[Dict]:
        """
        Return the first open position for *broker_sym*, or None.

        FIX-DEDUP: Now uses broker.get_positions_by_symbol() which ALWAYS
        queries MT5 directly — this catches positions opened by the OTHER
        script (live_trader) that aren't in our internal tracking dict.

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
        pos_id     = pos.get("ticket") or pos.get("id") or "?"
        pos_dir    = pos.get("type", "").lower()   # "buy" or "sell"
        pos_profit = pos.get("profit", 0.0)

        log.info("[%s] existing %s position #%s (P&L %.2f) — re-analysing…",
                 symbol, pos_dir.upper(), pos_id, pos_profit)
        print(f"[{symbol}→{broker_sym}] EXISTING {pos_dir.upper()} position "
              f"#{pos_id}  P&L={pos_profit:+.2f}  — re-analysing…")

        # Ask Oracle for a fresh signal
        try:
            sig, timed_out = _call_with_timeout(
                lambda sym=symbol: self.oracle.act(
                    "trade.propose", {"symbol": sym, "_sender": "demo_trader"}
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
            print("Cannot start trading:", status.get("reason"))
            print("Oracle will still compute signals; execution is disabled until MT5 connects.")
        print(f"\nDemo trader started. Symbols={self.symbols} interval={self.interval}s")
        print(f"Account: {status.get('account_type')} | paper={self.broker.paper} "
              f"| allow_live={self.broker.allow_live}")
        print(f"Symbol timeout: {self._symbol_timeout}s | max_trades: {self.max_trades}")
        print("Press Ctrl+C to stop.\n")

        cycle = 0
        try:
            while cycles is None or cycle < cycles:
                cycle += 1
                cycle_label = f"{cycle}/{cycles}" if cycles else str(cycle)
                print(f"\n{'─'*56}")
                print(f"  Cycle {cycle_label} — scanning {len(self.symbols)} symbols")
                print(f"{'─'*56}")

                summary = self._tick()

                print(
                    f"\nCycle {cycle_label} done — scanned {summary['scanned']} symbols | "
                    f"{summary['trades']} trade(s) | {summary['holds']} hold | "
                    f"{summary['rejects']} reject | {summary['errors']} error | "
                    f"{summary['timeouts']} timeout | {summary['unmapped']} unmapped | "
                    f"{summary['managed']} managed"
                )

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
        FIX-POS: Before opening any new position, check if one already exists
        for that symbol.  If yes → re-analyse and manage (hold/modify/close).
        If no → proceed with normal entry logic.
        MAX_POSITIONS_PER_SYMBOL = 1 is enforced.

        FIX-DEDUP: Before opening, also query Chronicle to check if the OTHER
        script (live_trader) has already opened a position for this symbol.
        After a successful fill, log the opened event to Chronicle.
        """
        summary = dict(scanned=0, trades=0, holds=0, rejects=0,
                       errors=0, timeouts=0, unmapped=0, managed=0,
                       kill_switch=False)

        for symbol in self.symbols:
            if self._kill_switch_check():
                summary["kill_switch"] = True
                return summary

            if self._trades_this_session >= self.max_trades:
                log.info("[%s] skipped — session trade cap reached", symbol)
                break

            broker_sym = self._sym_mapper.translate(symbol)
            if broker_sym is None:
                log.warning("[%s] UNMAPPED — no matching broker symbol found; skipping", symbol)
                print(f"[{symbol}] UNMAPPED  (add BROKER_SYMBOL_MAP={symbol}:<broker_name> to .env)")
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

            # 1. SIGNAL (with timeout)
            log.info("[%s] no open position — evaluating entry…", symbol)
            print(f"[{symbol}→{broker_sym}] no open position — evaluating entry…")
            try:
                sig, timed_out = _call_with_timeout(
                    lambda sym=symbol: self.oracle.act(
                        "trade.propose", {"symbol": sym, "_sender": "demo_trader"}
                    ),
                    self._symbol_timeout,
                )
            except Exception as exc:
                log.warning("[%s] ERROR during oracle.act: %s", symbol, exc)
                print(f"[{symbol}] ERROR  {exc}")
                summary["errors"] += 1
                continue

            if timed_out:
                log.warning("[%s] TIMEOUT after %.0fs — skipping", symbol, self._symbol_timeout)
                print(f"[{symbol}] TIMEOUT after {self._symbol_timeout:.0f}s — skipping")
                summary["timeouts"] += 1
                continue

            # 2. Classify signal outcome
            status = (sig or {}).get("status")

            if status != "complete":
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

                continue

            # 3. Approved plan — execute
            plan = sig["plan"]
            s    = sig["signal"]
            self._open_context[symbol] = sig.get("_streams", {})

            conf_str = f"conf={s.get('confidence', 0):.3f}"

            broker_plan = dict(plan)
            broker_plan["broker_symbol"] = broker_sym

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
                    summary["rejects"] += 1
            else:
                log.info("[%s→%s] TRADE  direction=%s %s (execution disabled: MT5 not connected)",
                         symbol, broker_sym, plan["direction"], conf_str)
                print(f"[{symbol}→{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                      f"  (execution disabled: MT5 not connected)")
                summary["trades"] += 1

        return summary

    # ── kill switch ───────────────────────────────────────────────────────────

    def _kill_switch_check(self) -> bool:
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
        for canon_sym, streams in self._open_context.items():
            broker_sym = self._sym_mapper.translate(canon_sym) or canon_sym
            poss = [p for p in self.broker.positions()
                    if p["symbol"].upper().startswith(broker_sym[:6].upper())]
            if not poss:
                continue
            realized = 1 if sum(p["profit"] for p in poss) >= 0 else -1
            self.oracle.act("fusion.learn", {"symbol": canon_sym, "streams": streams,
                                             "realized_direction": realized,
                                             "_sender": "demo_trader"})

    # ── external controls ─────────────────────────────────────────────────────

    def kill(self) -> Dict[str, Any]:
        return self.broker.close_all()

    def shutdown(self) -> None:
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
    ap = argparse.ArgumentParser(description="Oracle demo trader on MetaTrader 5 (41 symbols)")
    ap.add_argument("--symbols", nargs="+", default=None,
                    help="override symbol list (default: all 41)")
    ap.add_argument("--preset", choices=["all", "live"], default="all",
                    help="'all' = 41 symbols (default), 'live' = same 8 as live_trader")
    ap.add_argument("--interval",  type=int,   default=300,  help="seconds between cycles")
    ap.add_argument("--cycles",    type=int,   default=None, help="stop after N cycles")
    ap.add_argument("--max-trades", type=int,  default=10)
    ap.add_argument("--session-max-loss", type=float, default=0.05)
    ap.add_argument("--confirm-live", action="store_true",
                    help="required to place orders on a REAL account")
    ap.add_argument("--evolve-first", action="store_true",
                    help="evolve a strategy per symbol before trading")
    args = ap.parse_args()

    # Resolve symbol list
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif args.preset == "live":
        symbols = _LIVE_PRESET
    else:
        symbols = DEFAULT_SYMBOLS

    # Force paper trading on by default for demo
    if os.getenv("ORACLE_PAPER_TRADING", "").lower() not in ("false", "0", "no"):
        os.environ["ORACLE_PAPER_TRADING"] = "true"

    trader = DemoTrader(
        symbols=symbols,
        interval_sec=args.interval,
        session_max_loss_pct=args.session_max_loss,
        max_trades=args.max_trades,
        confirm_live=args.confirm_live,
    )

    if args.evolve_first:
        for sym in trader.symbols:
            print(f"Evolving strategy for {sym}…")
            out = trader.oracle.act("strategy.evolve",
                                    {"symbol": sym, "generations": 6, "_sender": "demo"})
            print(f"  {sym}: promoted={out.get('promoted_new_champion')} "
                  f"oos_return={(out.get('out_of_sample') or {}).get('total_return')}")

    print("=" * 64)
    print("  ORACLE DEMO TRADER (MetaTrader 5 — 41 symbols)")
    print("  Same pipeline as live_trader: champions + agent correlation.")
    print("  Paper trading ON by default. Set ORACLE_PAPER_TRADING=false")
    print("  + ORACLE_ALLOW_LIVE=true + --confirm-live for real execution.")
    print("=" * 64)
    try:
        trader.run(cycles=args.cycles)
    except Exception:
        raise
    print("Demo trader shutdown complete.")


if __name__ == "__main__":
    main()