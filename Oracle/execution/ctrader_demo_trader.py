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
import logging
import os
import sys
import time
import threading
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
)
from intelligence.news_impact import assess_news_impact  # type: ignore
from intelligence.social_risk import assess_social_risk  # type: ignore
from intelligence.trade_learning import TradeLearningEngine  # type: ignore
from core.risk import Position as RiskPosition  # type: ignore

log = logging.getLogger("oracle.ctrader_demo")

# ── Constants ─────────────────────────────────────────────────────────────────
_DEFAULT_MANAGE_INTERVAL = 15   # seconds
_TRADER_ID: str = os.getenv("TRADER_ID", "ctrader_demo")
_DEFAULT_SYMBOL_TIMEOUT = 60

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

_LIVE_PRESET: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "USOIL",
    "BTCUSD", "ETHUSD",
]

MAX_POSITIONS_PER_SYMBOL: int = 1

# ── cTrader-specific broker symbol overrides ──────────────────────────────────
_CTRADER_BROKER_MAP: Dict[str, str] = {
    "USOIL":   "USOIL.cash",
    "UKOIL":   "UKOIL.cash",
    "NATGAS":  "NATGAS.cash",
    "NAS100":  "US100",
    "GER40":   "DE40",
    "JPN225":  "JP225",
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


def _call_with_timeout(fn, timeout_sec: float) -> Tuple[Any, bool]:
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


class CTraderDemoTrader:
    """Demo trader for cTrader. Same pipeline as DemoTrader, CTraderBroker adapter."""

    def __init__(self, symbols, interval_sec=300, session_max_loss_pct=0.05,
                 max_trades=10, confirm_live=False):
        self.symbols = [s.upper() for s in symbols]
        self.interval = interval_sec
        self.session_max_loss_pct = session_max_loss_pct
        self.max_trades = max_trades
        self.confirm_live = confirm_live
        self.broker = CTraderBroker()
        self._trades_this_session = 0
        self._start_equity: Optional[float] = None
        self._open_context: Dict[str, Dict] = {}

        self._symbol_timeout = float(
            os.getenv("ORACLE_SYMBOL_TIMEOUT_SEC", str(_DEFAULT_SYMBOL_TIMEOUT)))

        _ks_ttl = float(os.getenv("KILL_SWITCH_EQUITY_CACHE_SEC", "60"))
        self._ks_equity_ttl = _ks_ttl
        self._ks_equity_cache: Optional[float] = None
        self._ks_equity_ts: float = 0.0
        self._ks_fired = False

        self._sym_mapper = SymbolMapper()

        self._trade_manager = TradeManager(TradeManagerConfig())
        self._trade_learning = TradeLearningEngine()
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
        self._trade_learning.confidence = self.oracle._champion_confidence

        self._pos_log = ChroniclePositionLog(
            chronicle_agent=self.chronicle, trader_id=_TRADER_ID)
        log.info("CTraderDemoTrader: TRADER_ID=%r", _TRADER_ID)

    def connect(self):
        status = self.broker.connect()
        if status.get("connected"):
            self._start_equity = status.get("equity") or status.get("balance")
            log.info("Connected: %s account, equity %.2f %s",
                     status["account_type"], self._start_equity or 0, status.get("currency", ""))
            self._build_symbol_map()
        else:
            log.warning("cTrader not connected: %s", status.get("reason"))
            self._build_symbol_map(broker_symbols=[])
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

    def _get_open_position(self, broker_sym):
        try:
            positions = self.broker.positions(symbol=broker_sym)
        except Exception as exc:
            log.warning("Could not fetch positions for %s: %s", broker_sym, exc)
            return None
        if not positions:
            return None
        return positions[0]

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
                entry_term_evidence=entry_term_evidence)
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
        with self._manage_lock:
            symbols = list(self._managed_positions.keys())
        for symbol in symbols:
            with self._manage_lock:
                pos = self._managed_positions.get(symbol)
            if pos is None:
                continue
            broker_sym = self._sym_mapper.translate(symbol) or symbol
            live_pos = self._get_open_position(broker_sym)
            if live_pos is None:
                log.info("[%s] no longer open — deregistering from CTM", symbol)
                try:
                    outcome = self._trade_learning.record_close(
                        self.oracle, pos, exit_price=pos.last_price,
                        exit_confidence=pos.last_confidence, exit_regime=pos.last_regime,
                        exit_reason="closed at broker (SL/TP hit or manual)")
                    if not outcome.won:
                        self.oracle.risk.portfolio.record_loss(symbol)
                except Exception as exc:
                    log.warning("[%s] Trade Learning failed on close: %s", symbol, exc)
                self.oracle.risk.portfolio.remove_by_symbol(symbol)
                with self._manage_lock:
                    self._managed_positions.pop(symbol, None)
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
                print(f"[{symbol}->{broker_sym}] CTM TIGHTEN #{pos_id} "
                      f"stop->{decision.new_stop:.5f}  {decision.reason}")
                try:
                    mod = self.broker.modify_position(pos_id, stop_loss=decision.new_stop)
                    if mod.get("status") == "modified":
                        self._pos_log.log_modified(symbol, broker_sym, pos_id,
                                                    sl=decision.new_stop or 0.0, tp=pos.initial_target)
                except Exception as exc:
                    log.warning("[%s] CTM: modify failed #%s: %s", symbol, pos_id, exc)
            elif decision.action == Action.CLOSE:
                log.info("[%s] CTM close #%s: %s", symbol, pos_id, decision.reason)
                print(f"[{symbol}->{broker_sym}] CTM CLOSE #{pos_id}  {decision.reason}")
                try:
                    close_result = self.broker.close_position(pos_id)
                    if close_result.get("status") == "closed":
                        self._pos_log.log_closed(symbol, broker_sym, pos_id, reason=decision.reason)
                        try:
                            outcome = self._trade_learning.record_close(
                                self.oracle, pos, exit_price=snap.price,
                                exit_confidence=snap.confidence, exit_regime=snap.regime,
                                exit_reason=decision.reason)
                            if not outcome.won:
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
        print(f"Symbol timeout: {self._symbol_timeout}s | max_trades: {self.max_trades}")
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
                      f"{summary['rejects']} reject | {summary['errors']} error | "
                      f"{summary['timeouts']} timeout | {summary['unmapped']} unmapped | "
                      f"{summary['managed']} managed")
                if summary["kill_switch"] or self._kill_switch_check():
                    print("KILL SWITCH: session loss limit hit. Flattening + stopping.")
                    print(self._close_all_positions())
                    break
                if self._trades_this_session >= self.max_trades:
                    print("Max trades for session reached. Stopping new entries.")
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
        summary = dict(scanned=0, trades=0, holds=0, rejects=0,
                       errors=0, timeouts=0, unmapped=0, managed=0, kill_switch=False)
        for symbol in self.symbols:
            if self._kill_switch_check():
                summary["kill_switch"] = True
                return summary
            if self._trades_this_session >= self.max_trades:
                break
            broker_sym = self._sym_mapper.translate(symbol)
            if broker_sym is None:
                print(f"[{symbol}] UNMAPPED — add BROKER_SYMBOL_MAP={symbol}:<name> to .env")
                summary["unmapped"] += 1
                continue
            summary["scanned"] += 1
            existing_pos = self._get_open_position(broker_sym)
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
            if self._pos_log.has_open_position(symbol, broker_sym):
                print(f"[{symbol}->{broker_sym}] DEDUP: Chronicle shows open position")
                summary["holds"] += 1
                continue
            print(f"[{symbol}->{broker_sym}] evaluating entry...")
            try:
                sig, timed_out = _call_with_timeout(
                    lambda sym=symbol: self.oracle.act(
                        "trade.propose", {"symbol": sym, "_sender": "ctrader_demo"}),
                    self._symbol_timeout)
            except Exception as exc:
                print(f"[{symbol}] ERROR  {exc}")
                summary["errors"] += 1
                continue
            if timed_out:
                print(f"[{symbol}] TIMEOUT after {self._symbol_timeout:.0f}s")
                summary["timeouts"] += 1
                continue
            status = (sig or {}).get("status")
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
            self._open_context[symbol] = sig.get("_streams", {})
            conf_str = f"conf={s.get('confidence', 0):.3f}"
            broker_plan = dict(plan)
            broker_plan["broker_symbol"] = broker_sym
            if self.broker.status.connected:
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
                    self._register_position(symbol, plan["direction"],
                        entry_price=result.get("price") or plan.get("entry", 0.0),
                        stop=plan.get("stop", 0.0), target=plan.get("target", 0.0),
                        confidence=s.get("confidence", 0.0),
                        size=result.get("volume", plan.get("size", 0.0)),
                        news_level=sig.get("news_impact", "none"),
                        news_reason=sig.get("news_reason", ""),
                        social_level=sig.get("social_risk", "none"),
                        social_reason=sig.get("social_reason", ""))
                    self.oracle.risk.portfolio.record_trade_opened(symbol)
                else:
                    summary["rejects"] += 1
            else:
                print(f"[{symbol}->{broker_sym}] TRADE  direction={plan['direction']}  {conf_str}"
                      f"  (cTrader not connected)")
                summary["trades"] += 1
        return summary

    def _kill_switch_check(self):
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
    ap = argparse.ArgumentParser(description="Oracle demo trader on cTrader (41 symbols)")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--preset", choices=["all", "live"], default="all")
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    ap.add_argument("--max-trades", type=int, default=10)
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
    print("  ORACLE DEMO TRADER (cTrader — 41 symbols)")
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
