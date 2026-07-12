"""
Oracle/tools/evolution_soak.py
===============================
Runs repeated evolve cycles across symbols on real historical data and logs
results to logs/evolution_soak.csv. No broker, no live risk — pure research.

Two things this script is careful about, based on what we learned watching
main.py's output:

1. A single `evolve` call can run MULTIPLE internal cycles (Atlas research
   escalation on stagnation). The top-level "certification"/"promoted" fields
   only reflect the LAST internal cycle, which can make a real mid-run
   champion promotion look like nothing happened. So after every evolve call
   we separately query `strategy.champion` to log the TRUE current champion
   state, not just the last cycle's verdict.

2. Every row is flagged low_confidence=True when OOS trades < MIN_TRADES,
   since verdicts on a handful of trades are statistical noise, not signal.

Usage:
    python tools/evolution_soak.py                       # one pass, all symbols
    python tools/evolution_soak.py --loop --every-hours 6
    python tools/evolution_soak.py --symbols EURUSD BTCUSD --generations 20
"""
from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ECO_ROOT = _REPO_ROOT.parent
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

DEFAULT_SYMBOLS = [
    # FX majors + crosses
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY",
    # metals / commodities
    "XAUUSD", "USOIL",
    # crypto
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
    # indices
    "SPX", "NASDAQ", "DJI", "RUT", "VIX", "FTSE", "DAX", "CAC40",
    "NIKKEI", "HSI", "SENSEX", "ASX200",
    # mega-cap stocks
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRKB",
    "LLY", "V", "JPM",
]
DEFAULT_GENERATIONS = 15
MIN_TRADES = 15  # below this, treat the verdict as low-confidence
LOG_PATH = _REPO_ROOT / "logs" / "evolution_soak.csv"

FIELDS = [
    "timestamp", "symbol", "regime", "bars",
    "cycle_promoted", "cycle_oos_return", "cycle_oos_trades", "cycle_sharpe",
    "cycle_win_rate", "cycle_max_drawdown",
    "champion_oos_return", "champion_oos_trades", "champion_sharpe",
    "champion_win_rate", "champion_genome_id", "champion_certified_at",
    "low_confidence", "note",
]


CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry",
]


def _unload_conflicting_modules():
    # Every repo (Oracle, Atlas, Sentinel, ...) ships its own core/agents/etc.
    # packages with the same names, so we must clear them from sys.modules
    # between loads or the wrong repo's module gets reused. Same fix main.py
    # already applies for the interactive CLI.
    for mod_name in CONFLICTING_MODULES:
        for m in list(sys.modules.keys()):
            if m == mod_name or m.startswith(mod_name + "."):
                del sys.modules[m]


def _load(folder, rel, cls, **kw):
    root = _ECO_ROOT / folder
    path_added = False
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", root / rel)
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    finally:
        if path_added:
            sys.path.pop(0)


def _boot_agent():
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                       storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
    _unload_conflicting_modules()

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
    _unload_conflicting_modules()

    sentinel = _load("Sentinel", "agents/sentinel_agent.py", "SentinelAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    pulse = _load("Pulse", "agents/pulse_agent.py", "PulseAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    from agents.oracle_agent import OracleAgent  # type: ignore
    agent = OracleAgent(chronicle_client=chronicle, sentinel_client=sentinel,
                         pulse_client=pulse, atlas_client=atlas)
    agent.start()
    peers = (chronicle, atlas, sentinel, pulse)
    return agent, peers


def _ensure_log_header():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        with open(LOG_PATH, "w", newline="") as f:
            csv.writer(f).writerow(FIELDS)


def run_one_symbol(agent, symbol: str, generations: int, writer, f) -> None:
    print(f"\nEvolving {symbol} ({generations} generations)...")
    out = agent.act("strategy.evolve", {"symbol": symbol, "generations": generations, "_sender": "soak"})
    evo = out.get("evolution", {})
    ctx = out.get("context", {})
    cycle_oos = evo.get("out_of_sample", {}) or {}

    regime = ctx.get("regime", "unknown")
    bars = ctx.get("bars", "")

    # The last internal cycle's own verdict (may under-report a real
    # mid-run promotion if Atlas escalation ran a second cycle after it).
    cycle_promoted = evo.get("promoted_new_champion", False)
    cycle_oos_return = cycle_oos.get("total_return", "")
    cycle_oos_trades = cycle_oos.get("trades", "")
    cycle_sharpe = cycle_oos.get("sharpe_proxy", "")
    cycle_win_rate = cycle_oos.get("win_rate", "")
    cycle_max_dd = cycle_oos.get("max_drawdown", "")

    # Ground truth: what's actually persisted as champion right now,
    # regardless of how many internal cycles ran or how the last one printed.
    champ_result = agent.act("strategy.champion", {"symbol": symbol, "_sender": "soak"})
    champ = champ_result.get("champion") or {}
    champ_oos = champ.get("out_of_sample", {}) or {}
    champion_oos_return = champ_oos.get("total_return", "")
    champion_oos_trades = champ_oos.get("trades", "")
    champion_sharpe = champ_oos.get("sharpe_proxy", "")
    champion_win_rate = champ_oos.get("win_rate", "")
    champion_genome_id = (champ.get("genome") or {}).get("genome_id", "")
    champion_certified_at = champ.get("certified_at", "")

    trades_for_confidence = champion_oos_trades if champion_oos_trades != "" else cycle_oos_trades
    low_confidence = (not isinstance(trades_for_confidence, int)) or (trades_for_confidence < MIN_TRADES)

    note = ""
    if cycle_promoted and champion_genome_id and champion_genome_id != evo.get("best_genome", {}).get("genome_id"):
        note = "champion changed after cycle; see champion_* columns for current state"

    row = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "symbol": symbol, "regime": regime, "bars": bars,
        "cycle_promoted": cycle_promoted,
        "cycle_oos_return": cycle_oos_return, "cycle_oos_trades": cycle_oos_trades,
        "cycle_sharpe": cycle_sharpe, "cycle_win_rate": cycle_win_rate,
        "cycle_max_drawdown": cycle_max_dd,
        "champion_oos_return": champion_oos_return, "champion_oos_trades": champion_oos_trades,
        "champion_sharpe": champion_sharpe, "champion_win_rate": champion_win_rate,
        "champion_genome_id": champion_genome_id, "champion_certified_at": champion_certified_at,
        "low_confidence": low_confidence, "note": note,
    }
    writer.writerow(row)
    f.flush()

    flag = " ⚠ LOW CONFIDENCE (few trades)" if low_confidence else ""
    print(f"  {symbol}: champion_oos_return={champion_oos_return} "
          f"trades={champion_oos_trades} sharpe={champion_sharpe} win_rate={champion_win_rate}{flag}")


def run_pass(agent, symbols, generations, delay_sec: float = 3.0):
    _ensure_log_header()
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        for i, symbol in enumerate(symbols):
            try:
                run_one_symbol(agent, symbol, generations, writer, f)
            except Exception as exc:
                print(f"  {symbol}: ERROR {exc}")
            if i < len(symbols) - 1:
                time.sleep(delay_sec)


def main():
    ap = argparse.ArgumentParser(description="Oracle evolution soak test (paper research only)")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--generations", type=int, default=DEFAULT_GENERATIONS)
    ap.add_argument("--loop", action="store_true", help="keep running indefinitely")
    ap.add_argument("--every-hours", type=float, default=6.0)
    ap.add_argument("--delay-sec", type=float, default=3.0,
                    help="pause between symbols to avoid tripping data-source rate limits")
    args = ap.parse_args()

    agent, peers = _boot_agent()
    print(f"Oracle soak test booted. Logging to {LOG_PATH}")
    print(f"Symbols: {args.symbols} | generations={args.generations} | "
          f"low-confidence threshold: <{MIN_TRADES} OOS trades")

    try:
        while True:
            run_pass(agent, args.symbols, args.generations, args.delay_sec)
            if not args.loop:
                break
            print(f"\nSleeping {args.every_hours}h until next pass...")
            time.sleep(args.every_hours * 3600)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        agent.stop()
        for peer in peers:
            if peer:
                try:
                    peer.stop()
                except Exception as exc:
                    print(f"  Warning: failed to stop peer {peer}: {exc}")
    print("Soak test shutdown complete.")


if __name__ == "__main__":
    main()