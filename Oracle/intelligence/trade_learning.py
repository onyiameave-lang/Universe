"""
Oracle Demo Trade Learning
==========================
Roadmap Phase 1, Item 3: "This closes Oracle's learning loop."

    Open Trade -> Result -> Chronicle stores result -> Oracle analyzes ->
    Champion confidence updated -> Knowledge base updated

Winning AND losing trades should both teach Oracle something.

What was missing before this module
-------------------------------------
execution.chronicle_position_log.log_closed() only ever recorded that a
close *happened* -- no realized P&L, no win/loss, no link back to fusion
weights or champion performance. Nothing in the codebase called
`fusion.learn_from_outcome()` from a real trade outcome, and champions had
no notion of live confidence at all (no wins/losses/demo_trades counters).
This module is the piece that actually closes the loop.

Runtime model
-------------
Built with intermittent operation in mind (the bot may only run a few hours
a day): every write here is to disk (JSON), keyed so re-running after a gap
just keeps adding to the same running totals. Nothing here depends on the
process having been up continuously -- it only needs to be running at the
moment a close is detected, whenever that happens to be.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("oracle.trade_learning")

try:
    from benchmarks.trading_benchmark import TradingBenchmark  # type: ignore
except ImportError:
    from Oracle.benchmarks.trading_benchmark import TradingBenchmark  # type: ignore

DEFAULT_CONFIDENCE_STORE = Path(__file__).resolve().parents[1] / "memory" / "champion_confidence.json"

# Bayesian smoothing prior: treat a brand-new champion as if it already had
# this many "phantom" trades at 50/50, so the first few real outcomes don't
# swing live_confidence wildly. Matches the roadmap's example table shape
# (Champion Confidence / Demo Trades / Wins / Losses).
PRIOR_TRADES = 5
PRIOR_WIN_RATE = 0.5

# Champion Confidence status thresholds (roadmap Phase 1 item 4: "Champion
# Retirement" — Champion -> Watchlist -> Retest -> Retire). These only
# apply once enough real trades exist to be meaningful; a champion with
# only 1-2 unlucky trades shouldn't be flagged for retirement.
MIN_TRADES_FOR_STATUS = 10
WATCHLIST_THRESHOLD = 0.40   # live_confidence below this -> watchlist
RETIRE_THRESHOLD = 0.25      # live_confidence below this -> retire


@dataclass
class TradeOutcome:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    entry_confidence: float
    exit_confidence: float
    entry_regime: str
    exit_regime: str
    pnl_r: float
    won: bool
    exit_reason: str
    duration_sec: float
    lesson: str


class ChampionConfidenceTracker:
    """
    Persistent win/loss counters per (symbol, regime), independent of the
    static backtested fitness score already stored on each champion genome.
    This is the *live* confidence the roadmap describes -- it only moves in
    response to real (paper or demo) trade outcomes.
    """

    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = store_path or DEFAULT_CONFIDENCE_STORE
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.store_path.exists():
            try:
                return json.loads(self.store_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("champion_confidence.json unreadable (%s); starting fresh", exc)
        return {}

    def _save(self) -> None:
        try:
            self.store_path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError as exc:
            log.warning("could not persist champion_confidence.json: %s", exc)

    @staticmethod
    def _key(symbol: str, regime: str) -> str:
        return f"{symbol.upper()}::{regime}"

    def get(self, symbol: str, regime: str) -> Dict[str, Any]:
        key = self._key(symbol, regime)
        rec = self._data.get(key)
        if rec is None:
            return {
                "symbol": symbol.upper(), "regime": regime,
                "demo_trades": 0, "wins": 0, "losses": 0,
                "live_confidence": PRIOR_WIN_RATE, "current_drawdown_r": 0.0,
                "last_updated": None,
            }
        return rec

    def record_outcome(self, symbol: str, regime: str, won: bool, pnl_r: float) -> Dict[str, Any]:
        key = self._key(symbol, regime)
        rec = self.get(symbol, regime)
        rec["demo_trades"] += 1
        if won:
            rec["wins"] += 1
            rec["current_drawdown_r"] = min(0.0, rec.get("current_drawdown_r", 0.0))
        else:
            rec["losses"] += 1
            # Track a simple running drawdown-in-R (never goes positive;
            # resets toward 0 as wins come in via the min() above).
            rec["current_drawdown_r"] = rec.get("current_drawdown_r", 0.0) + pnl_r

        # Bayesian-smoothed live confidence: blends the prior (50%, weighted
        # as PRIOR_TRADES phantom trades) with real wins/losses so far. Early
        # trades nudge it gently; confidence becomes more responsive as real
        # sample size grows.
        total = rec["wins"] + rec["losses"]
        rec["live_confidence"] = round(
            (PRIOR_TRADES * PRIOR_WIN_RATE + rec["wins"]) / (PRIOR_TRADES + total), 4
        )
        rec["last_updated"] = time.time()
        self._data[key] = rec
        self._save()
        return rec

    def status(self, symbol: str, regime: str) -> str:
        """
        Champion Retirement classification (roadmap Phase 1 item 4):
        "active" | "watchlist" | "retire" | "insufficient_data".
        This only FLAGS a champion for review -- it doesn't automatically
        replace it. Actually retesting/replacing is a separate step (ties
        into evolution.py's promotion pipeline) left for a deliberate
        follow-up rather than automatic mutation here.
        """
        rec = self.get(symbol, regime)
        if rec["demo_trades"] < MIN_TRADES_FOR_STATUS:
            return "insufficient_data"
        if rec["live_confidence"] < RETIRE_THRESHOLD:
            return "retire"
        if rec["live_confidence"] < WATCHLIST_THRESHOLD:
            return "watchlist"
        return "active"


class TradeLearningEngine:
    """
    Ties a closed position's realized outcome back into:
      1. Adaptive fusion weights (oracle_agent.act("fusion.learn", ...))
      2. Live champion confidence (ChampionConfidenceTracker above)
      3. Trading benchmark stats (roadmap item 5 — win rate, profit factor,
         Sharpe, drawdown, avg trade, avg holding time, consecutive losses,
         recovery factor — see benchmarks/trading_benchmark.py)
      4. A structured, human-readable lesson (for Chronicle / logs)
    """

    def __init__(self, confidence_tracker: Optional[ChampionConfidenceTracker] = None,
                 benchmark: Optional[TradingBenchmark] = None):
        self.confidence = confidence_tracker or ChampionConfidenceTracker()
        self.benchmark = benchmark or TradingBenchmark()

    def record_close(self, oracle_agent, pos, exit_price: float, exit_confidence: float,
                      exit_regime: str, exit_reason: str) -> TradeOutcome:
        risk_per_unit = abs(pos.entry_price - pos.initial_stop) or 1e-9
        if pos.direction.value == "buy":
            pnl_r = (exit_price - pos.entry_price) / risk_per_unit
        else:
            pnl_r = (pos.entry_price - exit_price) / risk_per_unit
        won = pnl_r > 0
        duration_sec = max(0.0, time.time() - pos.entry_time)

        # 1. Feed the realized outcome back into adaptive fusion so
        #    per-stream trust weights adapt (winning and losing trades both
        #    teach it something, per the roadmap).
        try:
            oracle_agent.act("fusion.learn", {
                "symbol": pos.symbol,
                "streams": pos.entry_streams,
                "realized_direction": 1 if won else -1,
                "_sender": "trade_learning",
            })
        except Exception as exc:
            log.warning("[%s] fusion.learn failed: %s", pos.symbol, exc)

        # 2. Update live champion confidence for this (symbol, entry regime).
        conf_rec = self.confidence.record_outcome(pos.symbol, pos.entry_regime, won, pnl_r)

        # 3. Build a plain-language lesson. Deliberately descriptive rather
        #    than causal (root-cause inference is the roadmap's V2 "Causal
        #    Analysis Engine" -- this stays at the level of "what happened").
        regime_note = (f"regime held ({pos.entry_regime})" if exit_regime == pos.entry_regime
                        else f"regime shifted {pos.entry_regime} -> {exit_regime}")
        conf_note = (f"confidence held near entry ({pos.entry_confidence:.2f} -> {exit_confidence:.2f})"
                     if abs(pos.entry_confidence - exit_confidence) < 0.15
                     else f"confidence moved {pos.entry_confidence:.2f} -> {exit_confidence:.2f}")
        lesson = (
            f"{'WIN' if won else 'LOSS'} {pos.direction.value.upper()} {pos.symbol} "
            f"{pnl_r:+.2f}R in {duration_sec/60:.0f}min ({exit_reason}). {regime_note}; "
            f"{conf_note}. Live champion confidence for {pos.symbol}::{pos.entry_regime} "
            f"now {conf_rec['live_confidence']:.0%} over {conf_rec['demo_trades']} demo trades "
            f"({conf_rec['wins']}W/{conf_rec['losses']}L)."
        )
        log.info("[%s] %s", pos.symbol, lesson)

        outcome = TradeOutcome(
            symbol=pos.symbol, direction=pos.direction.value,
            entry_price=pos.entry_price, exit_price=exit_price,
            entry_confidence=pos.entry_confidence, exit_confidence=exit_confidence,
            entry_regime=pos.entry_regime, exit_regime=exit_regime,
            pnl_r=round(pnl_r, 3), won=won, exit_reason=exit_reason,
            duration_sec=round(duration_sec, 1), lesson=lesson,
        )

        # 4. Benchmark Everything (roadmap Phase 1 item 5): saved
        #    incrementally on every close, not just "per session" — with
        #    intermittent runtime there's no clean session boundary, so
        #    saving on every trade is strictly more robust.
        try:
            self.benchmark.record_trade(outcome)
        except Exception as exc:
            log.warning("[%s] trading benchmark update failed: %s", pos.symbol, exc)

        return outcome