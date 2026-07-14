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
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ECO_ROOT = _REPO_ROOT.parent
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution.mt5_broker import MT5Broker  # type: ignore

log = logging.getLogger("oracle.live")

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


class LiveTrader:
    def __init__(self, symbols: List[str], interval_sec: int = 300,
                 session_max_loss_pct: float = 0.05, max_trades: int = 10,
                 confirm_live: bool = False):
        self.symbols = symbols
        self.interval = interval_sec
        self.session_max_loss_pct = session_max_loss_pct
        self.max_trades = max_trades
        self.confirm_live = confirm_live
        self.broker = MT5Broker()
        self._trades_this_session = 0
        self._start_equity = None
        self._open_context: Dict[str, Dict] = {}   # symbol -> streams at entry (for learning)

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

    # ---- connection ----

    def connect(self) -> Dict[str, Any]:
        status = self.broker.connect()
        if status.get("connected"):
            self._start_equity = status.get("equity") or status.get("balance")
            log.info("Connected: %s account, equity %.2f %s", status["account_type"],
                     self._start_equity or 0, status.get("currency", ""))
        else:
            log.warning("MT5 not connected: %s", status.get("reason"))
        return status

    # ---- the loop ----

    def run(self, cycles: Optional[int] = None) -> None:
        status = self.connect()
        if not status.get("connected"):
            print("Cannot start live trading:", status.get("reason"))
            print("Oracle will still compute signals; execution is disabled until MT5 connects.")
        print(f"\nLive trader started. Symbols={self.symbols} interval={self.interval}s")
        print(f"Account: {status.get('account_type')} | paper={self.broker.paper} "
              f"| allow_live={self.broker.allow_live}")
        print("Press Ctrl+C to stop (positions are NOT auto-closed on stop; use kill switch).\n")

        cycle = 0
        try:
            while cycles is None or cycle < cycles:
                cycle += 1
                self._tick()
                if self._kill_switch_check():
                    print("KILL SWITCH: session loss limit hit. Flattening + stopping.")
                    print(self.broker.close_all())
                    break
                if self._trades_this_session >= self.max_trades:
                    print("Max trades for session reached. Stopping new entries.")
                    break
                if cycles is None or cycle < cycles:
                    time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            self._learn_from_closed()
            self.broker.disconnect()

    def _tick(self) -> None:
        for symbol in self.symbols:
            # 1. signal
            sig = self.oracle.act("trade.propose", {"symbol": symbol, "_sender": "live_trader"})
            if sig.get("status") != "complete":
                log.info("[%s] no trade: %s", symbol, sig.get("message"))
                continue
            plan = sig["plan"]
            # remember streams so we can learn from the outcome later
            self._open_context[symbol] = sig.get("_streams", {})
            # 2+3. execute through the broker (which re-checks all live gates)
            if self.broker.status.connected:
                result = self.broker.place_order(plan, human_confirm=self.confirm_live)
                log.info("[%s] %s: %s", symbol, result.get("status"), result.get("reason", ""))
                if result.get("status") == "filled":
                    self._trades_this_session += 1
                print(f"[{symbol}] signal={plan['direction']} conf={sig['signal']['confidence']} "
                      f"-> {result.get('status')} {result.get('reason','')}")
            else:
                print(f"[{symbol}] signal={plan['direction']} conf={sig['signal']['confidence']} "
                      f"(execution disabled: MT5 not connected)")

    def _kill_switch_check(self) -> bool:
        if self._start_equity is None or not self.broker.status.connected:
            return False
        acct = self.broker.account()
        equity = acct.get("equity", self._start_equity)
        loss = (self._start_equity - equity) / self._start_equity if self._start_equity else 0
        return loss >= self.session_max_loss_pct

    def _learn_from_closed(self) -> None:
        """Feed realized direction of closed positions back into adaptive fusion."""
        for symbol, streams in self._open_context.items():
            # crude realized-direction proxy from current position profit (or skip)
            poss = [p for p in self.broker.positions() if p["symbol"].upper().startswith(symbol[:6].upper())]
            if not poss:
                continue
            realized = 1 if sum(p["profit"] for p in poss) >= 0 else -1
            self.oracle.act("fusion.learn", {"symbol": symbol, "streams": streams,
                                            "realized_direction": realized, "_sender": "live_trader"})

    def kill(self) -> Dict[str, Any]:
        return self.broker.close_all()

    def shutdown(self) -> None:
        self.oracle.stop()
        for peer in (self.pulse, self.sentinel, self.atlas, self.chronicle):
            if peer:
                try:
                    peer.stop()
                except Exception as exc:
                    log.warning("failed to stop peer %s: %s", peer, exc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(description="Oracle live trader on MetaTrader 5")
    ap.add_argument("--symbols", nargs="+", default=["EURUSD"], help="symbols to trade")
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    ap.add_argument("--max-trades", type=int, default=10)
    ap.add_argument("--session-max-loss", type=float, default=0.05, help="fraction; kill switch")
    ap.add_argument("--confirm-live", action="store_true",
                   help="required to place orders on a REAL account")
    ap.add_argument("--evolve-first", action="store_true",
                   help="evolve a strategy per symbol before trading")
    args = ap.parse_args()

    trader = LiveTrader(symbols=[s.upper() for s in args.symbols], interval_sec=args.interval,
                       session_max_loss_pct=args.session_max_loss, max_trades=args.max_trades,
                       confirm_live=args.confirm_live)

    if args.evolve_first:
        for sym in trader.symbols:
            print(f"Evolving strategy for {sym}...")
            out = trader.oracle.act("strategy.evolve", {"symbol": sym, "generations": 6, "_sender": "live"})
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
    finally:
        trader.shutdown()
    print("Live trader shutdown complete.")


if __name__ == "__main__":
    main()