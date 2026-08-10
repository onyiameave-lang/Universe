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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.trade_learning")

# Structured experiment log (see record_close()'s step 7 for why this
# exists) -- a minimal, append-only JSONL file, separate from both
# TradingBenchmark (aggregate-only) and the Trade Journal (prose, in
# Chronicle). One line per closed trade: symbol, entry_streams, won, pnl_r.
_EXPERIMENT_LOG_PATH = Path(__file__).resolve().parents[1] / "memory" / "trade_experiment_log.jsonl"
_experiment_log_lock = threading.Lock()


def _append_experiment_log(symbol: str, entry_streams: Optional[Dict[str, Any]],
                            won: bool, pnl_r: float) -> None:
    _EXPERIMENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"symbol": symbol, "entry_streams": entry_streams or {}, "won": won,
              "pnl_r": pnl_r, "timestamp": time.time()}
    with _experiment_log_lock:
        with _EXPERIMENT_LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")


def load_experiment_log(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Reads the full structured trade log back -- what Forge's experiment
    templates (via the Chronicle Research Director) actually consume.
    Tolerates a corrupted/partial last line (e.g. a write interrupted
    mid-append) by skipping just that line rather than failing entirely."""
    p = path or _EXPERIMENT_LOG_PATH
    if not p.exists():
        return []
    records = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping corrupted line in trade_experiment_log.jsonl")
    return records

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
# Stage 1 auto re-evolution: minimum time between automatic re-evolve
# triggers for the SAME (symbol, regime), so a champion stuck in "retire"
# doesn't kick off a fresh evolution cycle on every single subsequent loss.
REEVOLVE_COOLDOWN_SEC = 24 * 3600
REEVOLVE_GENERATIONS = 6   # matches the CLI's own "evolve <S> [gens]" default-ish usage


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
        This only FLAGS a champion for review — see
        maybe_trigger_reevolution() below for the Stage 1 follow-up that
        actually acts on a "retire" flag.
        """
        rec = self.get(symbol, regime)
        if rec["demo_trades"] < MIN_TRADES_FOR_STATUS:
            return "insufficient_data"
        if rec["live_confidence"] < RETIRE_THRESHOLD:
            return "retire"
        if rec["live_confidence"] < WATCHLIST_THRESHOLD:
            return "watchlist"
        return "active"

    def maybe_trigger_reevolution(self, oracle_agent, symbol: str, regime: str) -> bool:
        """
        Champion Retirement, Stage 1 (roadmap): when a champion has
        genuinely earned "retire" status (enough real trades, sustained
        poor live performance), automatically kick off a fresh
        strategy.evolve cycle for it — "Oracle should come back to the
        strategy" rather than silently keep trading a champion that's
        already been flagged as stale.

        Deliberately NOT the causal/ablation analysis discussed separately
        (which needs much more trade volume to trust) — this just re-runs
        the same evolution/backtest process a human would trigger manually
        via `evolve <symbol>`, automatically, with a cooldown so a
        persistently-retired champion doesn't re-trigger this on every
        single subsequent loss.

        Runs the actual evolution in a background thread (daemon, fire-
        and-forget) so it never blocks the Continuous Trade Manager's poll
        loop, which is what calls this. Returns True if a re-evolution was
        actually triggered (False if skipped due to cooldown or wrong status).
        """
        if self.status(symbol, regime) != "retire":
            return False

        key = self._key(symbol, regime)
        rec = self._data.get(key, {})
        last_triggered = rec.get("last_reevolve_triggered_at")
        if last_triggered and (time.time() - last_triggered) < REEVOLVE_COOLDOWN_SEC:
            return False   # already triggered recently — don't spam re-evolution

        rec["last_reevolve_triggered_at"] = time.time()
        self._data[key] = rec
        self._save()

        log.warning("[%s::%s] champion RETIRED (live_confidence=%.0f%%, %d demo trades) "
                    "— auto-triggering strategy.evolve", symbol, regime,
                    rec.get("live_confidence", 0.0) * 100, rec.get("demo_trades", 0))

        def _run_evolution():
            try:
                result = oracle_agent.act("strategy.evolve", {
                    "symbol": symbol, "generations": REEVOLVE_GENERATIONS,
                    "_sender": "trade_learning_auto_reevolve",
                })
                log.info("[%s] auto re-evolution finished: status=%s",
                         symbol, (result or {}).get("status"))
            except Exception as exc:
                log.warning("[%s] auto re-evolution failed: %s", symbol, exc)

        threading.Thread(target=_run_evolution, name=f"auto-reevolve-{symbol}", daemon=True).start()
        return True


class TradeLearningEngine:
    """
    Ties a closed position's realized outcome back into:
      1. Adaptive fusion weights (oracle_agent.act("fusion.learn", ...))
      2. Sentinel's Tier 0 term reliability (news.record_term_outcomes) —
         grades individual news terms against real price movement, so
         Sentinel's lexical sentiment scoring improves over months
         (see Sentinel/intelligence/term_reliability.py)
      3. Live champion confidence (ChampionConfidenceTracker above)
      4. Trading benchmark stats (roadmap item 5 — win rate, profit factor,
         Sharpe, drawdown, avg trade, avg holding time, consecutive losses,
         recovery factor — see benchmarks/trading_benchmark.py)
      5. A structured, human-readable lesson (for Chronicle / logs)
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

        # 2. Grade Sentinel's news terms (Tier 0 self-improvement) against
        #    what price ACTUALLY did between entry and exit — independent
        #    of whether this particular trade won (a bullish term is
        #    "correct" if price rose, regardless of whether the overall
        #    fused decision happened to go short and lose anyway; grading
        #    against raw price direction is more direct than grading
        #    against the trade's own win/loss).
        evidence = getattr(pos, "entry_term_evidence", None) or {}
        outcomes = []
        price_rose = exit_price > pos.entry_price
        for term in evidence.get("bullish", []):
            outcomes.append({"term": term, "predicted_correctly": price_rose})
        for term in evidence.get("bearish", []):
            outcomes.append({"term": term, "predicted_correctly": not price_rose})
        if outcomes and oracle_agent.sentinel is not None:
            try:
                oracle_agent.sentinel.act("news.record_term_outcomes", {
                    "outcomes": outcomes, "_sender": "trade_learning"})
            except Exception as exc:
                log.warning("[%s] news.record_term_outcomes failed: %s", pos.symbol, exc)

        # 3. Update live champion confidence for this (symbol, entry regime).
        conf_rec = self.confidence.record_outcome(pos.symbol, pos.entry_regime, won, pnl_r)
        # Champion Retirement, Stage 1: if this outcome pushed the champion
        # into "retire" status, kick off a fresh evolution cycle for it
        # (cooldown-limited — see maybe_trigger_reevolution's docstring).
        self.confidence.maybe_trigger_reevolution(oracle_agent, pos.symbol, pos.entry_regime)

        # 4. Build a plain-language lesson. Deliberately descriptive rather
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

        # 5. Benchmark Everything (roadmap Phase 1 item 5): saved
        #    incrementally on every close, not just "per session" — with
        #    intermittent runtime there's no clean session boundary, so
        #    saving on every trade is strictly more robust.
        try:
            self.benchmark.record_trade(outcome)
        except Exception as exc:
            log.warning("[%s] trading benchmark update failed: %s", pos.symbol, exc)

        # 6. Trade Journal (roadmap Phase 3 groundwork: Trade Explainability):
        #    links back to the original entry via journal_id, so the win/
        #    loss outcome is reviewable alongside the reasons that led to
        #    the trade in the first place.
        journal_id = getattr(pos, "journal_id", None)
        if journal_id:
            try:
                oracle_agent.trade_journal.log_outcome(
                    journal_id, pos.symbol, won, outcome.pnl_r, exit_reason, lesson)
            except Exception as exc:
                log.warning("[%s] trade journal outcome logging failed: %s", pos.symbol, exc)

        # 7. Structured experiment log (Forge's Evidence Engine needs this):
        #    TradingBenchmark only keeps running AGGREGATE counters, never
        #    individual trades; the Trade Journal writes human prose to
        #    Chronicle, not structured numeric data. Neither has both
        #    entry_streams and the outcome together in queryable form --
        #    a real gap found while building the Chronicle Research
        #    Director. This is a minimal, append-only JSONL log purpose-
        #    built to feed Forge's experiment templates (e.g. "does the
        #    news stream actually predict wins?"), without duplicating or
        #    replacing either of the above.
        try:
            _append_experiment_log(pos.symbol, pos.entry_streams, won, outcome.pnl_r)
        except Exception as exc:
            log.warning("[%s] experiment log write failed: %s", pos.symbol, exc)

        return outcome