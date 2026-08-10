"""
Oracle Continuous Trade Manager
================================
Roadmap Phase 1, Item 1 (highest priority): "Oracle's eyes."

Problem this solves
-------------------
The existing position-management path (LiveTrader._manage_existing_position)
only re-checks an open position once per full scan cycle (default every
300s), and it does so by asking Oracle for a brand-new "trade.propose"
signal from scratch. That means:
  - No real trailing-stop math -- the stop only moves if Oracle's fresh
    plan happens to contain a different stop_loss number.
  - No memory of the position's entry confidence or entry regime, so there
    is nothing to compare "now" against "then".
  - Checks are as slow as the whole-portfolio scan interval, not something
    that can run every few seconds.

This module is a standalone, broker-agnostic decision engine. It knows
nothing about MT5, Oracle's agent bus, or the internet -- you hand it a
`Position` (what we opened, and with what beliefs) and a `MarketSnapshot`
(what's true right now), and it hands back a `ManagedDecision` telling you
exactly what to do and why. That makes it fully unit-testable offline,
which matters a lot in a sandbox with no live market data.

Wiring this into LiveTrader / mt5_demo_trader (a lightweight per-position
poll every N seconds, independent of the slower full-symbol-scan interval)
is a follow-up step once you've reviewed the logic here.

Responsibilities (from the roadmap)
------------------------------------
  1. Monitor every open trade
  2. Move stop loss (real trailing-stop ratchet, price-based or ATR-based)
  3. Trail profits
  4. Detect regime changes
  5. Detect confidence drops
  6. Decide whether to hold or exit
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

log = logging.getLogger("oracle.trade_manager")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Action(str, Enum):
    HOLD = "hold"
    TIGHTEN_STOP = "tighten_stop"
    CLOSE = "close"
    PARTIAL_CLOSE = "partial_close"


# Regimes considered structurally hostile to a position of a given direction.
# If the market flips into one of these relative to entry, that's a real
# regime-change event, not noise.
_HOSTILE_REGIME = {
    Direction.BUY: {"trending_down"},
    Direction.SELL: {"trending_up"},
}


@dataclass
class Position:
    """Everything Oracle believed true at entry, plus what the position is now."""
    symbol: str
    direction: Direction
    entry_price: float
    initial_stop: float
    initial_target: float
    entry_confidence: float
    entry_regime: str
    entry_time: float = field(default_factory=time.time)
    size: float = 1.0
    entry_streams: Dict[str, Any] = field(default_factory=dict)  # per-source signal breakdown at entry, for fusion.learn
    entry_term_evidence: Dict[str, list] = field(default_factory=dict)  # {"bullish":[...],"bearish":[...]} terms live at entry — for Sentinel's term-reliability grading
    entry_atr: Optional[float] = None   # volatility at entry, for Volatility Exit (roadmap Phase 2 item 8)
    journal_id: Optional[str] = None    # links this position's Trade Journal entry to its eventual outcome
    last_journaled_hold_reason: Optional[str] = None   # dedup: only journal a hold when the reason changes

    # Mutable trailing state -- the manager updates these as it runs.
    current_stop: float = field(init=False)
    highest_price: float = field(init=False)   # best price seen (favorable direction)
    lowest_price: float = field(init=False)     # worst price seen (adverse direction)
    last_price: float = field(init=False)       # most recent snapshot price seen
    last_regime: str = field(init=False)        # most recent snapshot regime seen
    last_confidence: float = field(init=False)  # most recent snapshot confidence seen

    def __post_init__(self):
        self.current_stop = self.initial_stop
        self.highest_price = self.entry_price
        self.lowest_price = self.entry_price
        self.last_price = self.entry_price
        self.last_regime = self.entry_regime
        self.last_confidence = self.entry_confidence


@dataclass
class MarketSnapshot:
    """What's true right now for a symbol."""
    price: float
    confidence: float          # Oracle's current signal confidence, 0..1
    regime: str                # current regime label, e.g. "trending_up"
    atr: Optional[float] = None  # average true range, if available (for stop distance)
    news_impact: str = "none"    # "none" | "medium" | "high" — from intelligence.news_impact
    social_risk: str = "none"    # "none" | "medium" | "high" — from intelligence.social_risk


@dataclass
class ManagedDecision:
    action: Action
    new_stop: Optional[float] = None
    reason: str = ""
    # Explainability fields (Phase 3 groundwork) -- cheap to attach now.
    entry_confidence: float = 0.0
    current_confidence: float = 0.0
    entry_regime: str = ""
    current_regime: str = ""
    unrealized_r: float = 0.0   # profit/loss in units of initial risk (R-multiples)


@dataclass
class TradeManagerConfig:
    # Trailing stop only activates once a position is this many R in profit
    # (R = initial risk = |entry_price - initial_stop|). Avoids trailing
    # noise around breakeven.
    trail_activation_r: float = 1.0
    # Once active, keep the stop this many R behind the best price seen.
    trail_distance_r: float = 1.0
    # Absolute confidence drop (entry - current) that counts as "a real drop".
    confidence_drop_threshold: float = 0.20
    # Relative confidence collapse (current / entry) that counts as severe,
    # even if the absolute drop is smaller (protects low-confidence entries).
    confidence_collapse_ratio: float = 0.5

    # -- Multiple Exit Strategies additions (roadmap Phase 2 item 8) --------
    # Time Exit: a position held longer than this without meaningful profit
    # is tying up capital for no reason. Default 48h fits typical swing/
    # day-trade holding windows; set to None to disable entirely.
    max_holding_sec: Optional[float] = 48 * 3600
    # "Meaningful profit" threshold below which a stale trade gets closed
    # rather than left to keep running past max_holding_sec.
    time_exit_min_r: float = 0.5

    # Volatility Exit: if current ATR has spiked to this many multiples of
    # the ATR at entry, the market's risk profile has genuinely changed
    # since the trade was planned (stops/targets were sized for the OLD
    # volatility) — treated with the same care as a news/social shock.
    volatility_spike_multiple: float = 2.5


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------

class TradeManager:
    """
    Stateless decision engine over a single (Position, MarketSnapshot) pair.
    Call `evaluate()` on every poll tick for every open position. The
    Position object accumulates trailing-stop state across calls -- keep
    reusing the same Position instance for a given open trade.
    """

    def __init__(self, config: Optional[TradeManagerConfig] = None):
        self.config = config or TradeManagerConfig()

    def evaluate(self, pos: Position, snap: MarketSnapshot) -> ManagedDecision:
        cfg = self.config

        # Remember the most recent snapshot, in case this position later
        # closes at the broker (native SL/TP hit) without us ever seeing the
        # exact close price — see intelligence.trade_learning, which uses
        # this as a best-effort exit price for the learning signal.
        pos.last_price = snap.price
        pos.last_regime = snap.regime
        pos.last_confidence = snap.confidence

        # Track best/worst excursion for trailing-stop purposes.
        if pos.direction == Direction.BUY:
            pos.highest_price = max(pos.highest_price, snap.price)
        else:
            pos.lowest_price = min(pos.lowest_price, snap.price)

        risk_per_unit = abs(pos.entry_price - pos.initial_stop) or 1e-9
        if pos.direction == Direction.BUY:
            unrealized_r = (snap.price - pos.entry_price) / risk_per_unit
        else:
            unrealized_r = (pos.entry_price - snap.price) / risk_per_unit

        base = dict(
            entry_confidence=pos.entry_confidence,
            current_confidence=snap.confidence,
            entry_regime=pos.entry_regime,
            current_regime=snap.regime,
            unrealized_r=unrealized_r,
        )

        # ---- 1. Hard stop / target check (safety net; broker SL/TP should
        #         normally catch this first, but the manager must agree). ----
        if pos.direction == Direction.BUY and snap.price <= pos.current_stop:
            return ManagedDecision(Action.CLOSE, reason=(
                f"price {snap.price:.5f} hit stop {pos.current_stop:.5f}"), **base)
        if pos.direction == Direction.SELL and snap.price >= pos.current_stop:
            return ManagedDecision(Action.CLOSE, reason=(
                f"price {snap.price:.5f} hit stop {pos.current_stop:.5f}"), **base)

        # ---- 2. High-impact news check (checked first among the "soft"
        #         triggers -- roadmap: "Oracle should never ignore major
        #         news"). A "medium" impact event alone doesn't force
        #         action here; it already dampens NEW entries via
        #         news_impact.assess_news_impact() feeding risk.evaluate().
        #         For an OPEN position, only "high" forces a reaction. ----
        if snap.news_impact == "high":
            if unrealized_r <= 0:
                return ManagedDecision(Action.CLOSE, reason=(
                    f"high-impact news event while position is at "
                    f"{unrealized_r:.2f}R -- cutting risk immediately"), **base)
            else:
                new_stop = self._breakeven_or_better(pos, unrealized_r)
                pos.current_stop = new_stop
                return ManagedDecision(Action.TIGHTEN_STOP, new_stop=new_stop, reason=(
                    f"high-impact news event but position is at {unrealized_r:.2f}R "
                    f"-- locking in gains before volatility hits"), **base)

        # ---- 3. Social risk check (coordinated pump/dump-style activity,
        #         from Pulse's manipulation detector). Same treatment as
        #         high-impact news: a detected pump means the market can
        #         unwind sharply once the coordinated push fades. ----
        if snap.social_risk == "high":
            if unrealized_r <= 0:
                return ManagedDecision(Action.CLOSE, reason=(
                    f"high social-manipulation risk detected while position is at "
                    f"{unrealized_r:.2f}R -- cutting risk before the pump unwinds"), **base)
            else:
                new_stop = self._breakeven_or_better(pos, unrealized_r)
                pos.current_stop = new_stop
                return ManagedDecision(Action.TIGHTEN_STOP, new_stop=new_stop, reason=(
                    f"high social-manipulation risk detected but position is at "
                    f"{unrealized_r:.2f}R -- locking in gains before the pump unwinds"), **base)

        # ---- 4. Volatility Exit (roadmap Phase 2 item 8): if current ATR
        #         has spiked well beyond what it was at entry, the stop/
        #         target sizing done at entry time no longer matches the
        #         market's actual risk -- treated with the same care as a
        #         news/social shock. Skipped if we don't have both ATR
        #         readings (e.g. backfilled/orphaned positions never
        #         captured an entry_atr). ----
        if pos.entry_atr and snap.atr and pos.entry_atr > 0:
            vol_ratio = snap.atr / pos.entry_atr
            if vol_ratio >= cfg.volatility_spike_multiple:
                if unrealized_r <= 0:
                    return ManagedDecision(Action.CLOSE, reason=(
                        f"volatility spiked {vol_ratio:.1f}x since entry while position is at "
                        f"{unrealized_r:.2f}R -- stop/target no longer match current risk"), **base)
                else:
                    new_stop = self._breakeven_or_better(pos, unrealized_r)
                    pos.current_stop = new_stop
                    return ManagedDecision(Action.TIGHTEN_STOP, new_stop=new_stop, reason=(
                        f"volatility spiked {vol_ratio:.1f}x since entry but position is at "
                        f"{unrealized_r:.2f}R -- locking in gains before it gets more unpredictable"), **base)

        # ---- 5. Regime change detection ----
        regime_flipped_hostile = snap.regime in _HOSTILE_REGIME[pos.direction]
        regime_changed_at_all = snap.regime != pos.entry_regime

        if regime_flipped_hostile:
            # Structural change working against us. If we're already losing,
            # get out now rather than hoping it reverts. If we're in profit,
            # lock in gains by yanking the stop to breakeven-or-better
            # instead of closing outright (still gives the trade a chance).
            if unrealized_r <= 0:
                return ManagedDecision(Action.CLOSE, reason=(
                    f"regime flipped hostile ({pos.entry_regime} -> {snap.regime}) "
                    f"while position is at {unrealized_r:.2f}R"), **base)
            else:
                new_stop = self._breakeven_or_better(pos, unrealized_r)
                pos.current_stop = new_stop
                return ManagedDecision(Action.TIGHTEN_STOP, new_stop=new_stop, reason=(
                    f"regime flipped hostile ({pos.entry_regime} -> {snap.regime}) "
                    f"but position is at {unrealized_r:.2f}R -- locking in gains"), **base)

        # ---- 6. Confidence drop detection ----
        abs_drop = pos.entry_confidence - snap.confidence
        collapsed = (snap.confidence / pos.entry_confidence) < cfg.confidence_collapse_ratio \
            if pos.entry_confidence > 0 else False
        confidence_dropped = abs_drop >= cfg.confidence_drop_threshold or collapsed

        if confidence_dropped:
            if unrealized_r <= 0:
                return ManagedDecision(Action.CLOSE, reason=(
                    f"confidence dropped {pos.entry_confidence:.2f} -> {snap.confidence:.2f} "
                    f"while position is at {unrealized_r:.2f}R -- cutting the loss early"), **base)
            else:
                new_stop = self._breakeven_or_better(pos, unrealized_r)
                pos.current_stop = new_stop
                return ManagedDecision(Action.TIGHTEN_STOP, new_stop=new_stop, reason=(
                    f"confidence dropped {pos.entry_confidence:.2f} -> {snap.confidence:.2f} "
                    f"but position is at {unrealized_r:.2f}R -- protecting gains"), **base)

        # ---- 7. Time Exit (roadmap Phase 2 item 8): a position that's been
        #         open a long time without meaningful profit is tying up
        #         capital and margin for no clear reason. Only closes if it
        #         genuinely isn't working (unrealized_r below the minimum)
        #         -- a big winner that's just taking a while is left alone. ----
        if cfg.max_holding_sec is not None:
            held_sec = time.time() - pos.entry_time
            if held_sec >= cfg.max_holding_sec and unrealized_r < cfg.time_exit_min_r:
                return ManagedDecision(Action.CLOSE, reason=(
                    f"time exit: held {held_sec/3600:.1f}h (limit {cfg.max_holding_sec/3600:.0f}h) "
                    f"at only {unrealized_r:.2f}R -- freeing up capital"), **base)

        # ---- 8. Ordinary profit trailing ----
        if unrealized_r >= cfg.trail_activation_r:
            trailed_stop = self._trailing_stop(pos, cfg)
            moved = (
                (pos.direction == Direction.BUY and trailed_stop > pos.current_stop) or
                (pos.direction == Direction.SELL and trailed_stop < pos.current_stop)
            )
            if moved:
                pos.current_stop = trailed_stop
                return ManagedDecision(Action.TIGHTEN_STOP, new_stop=trailed_stop, reason=(
                    f"trailing stop: {unrealized_r:.2f}R in profit, "
                    f"ratcheting stop to {trailed_stop:.5f}"), **base)

        # ---- 9. Nothing to do ----
        note = " (regime changed but not hostile)" if regime_changed_at_all else ""
        return ManagedDecision(Action.HOLD, reason=(
            f"holding: {unrealized_r:.2f}R, confidence {snap.confidence:.2f}"
            f"{note}"), **base)

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _breakeven_or_better(pos: Position, unrealized_r: float) -> float:
        """Move stop to entry price (breakeven), never loosening it."""
        if pos.direction == Direction.BUY:
            return max(pos.current_stop, pos.entry_price)
        return min(pos.current_stop, pos.entry_price)

    @staticmethod
    def _trailing_stop(pos: Position, cfg: TradeManagerConfig) -> float:
        risk_per_unit = abs(pos.entry_price - pos.initial_stop) or 1e-9
        distance = cfg.trail_distance_r * risk_per_unit
        if pos.direction == Direction.BUY:
            return pos.highest_price - distance
        return pos.lowest_price + distance


# ---------------------------------------------------------------------------
# Demo / self-test with a simulated price path (no broker, no network needed)
# ---------------------------------------------------------------------------

def _demo():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mgr = TradeManager()

    print("=" * 70)
    print("SCENARIO 1: Winning trade that trails and eventually gets stopped out")
    print("=" * 70)
    pos = Position(
        symbol="EURUSD", direction=Direction.BUY,
        entry_price=1.1000, initial_stop=1.0950, initial_target=1.1150,
        entry_confidence=0.72, entry_regime="trending_up",
    )
    # Price rallies, pulls back, rallies more, then reverses hard.
    price_path = [1.1000, 1.1020, 1.1055, 1.1090, 1.1130, 1.1160,
                  1.1140, 1.1100, 1.1060, 1.1030]
    confidences = [0.72, 0.70, 0.68, 0.66, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40]
    regimes = ["trending_up"] * 6 + ["ranging"] * 2 + ["trending_down"] * 2

    for i, (p, c, r) in enumerate(zip(price_path, confidences, regimes), 1):
        snap = MarketSnapshot(price=p, confidence=c, regime=r)
        decision = mgr.evaluate(pos, snap)
        print(f"  t={i:02d} price={p:.4f} conf={c:.2f} regime={r:<14} "
              f"-> {decision.action.value:<14} stop={pos.current_stop:.5f}  {decision.reason}")
        if decision.action == Action.CLOSE:
            print(f"  ** POSITION CLOSED at t={i} **")
            break

    print()
    print("=" * 70)
    print("SCENARIO 2: Losing trade, confidence collapses, cut early")
    print("=" * 70)
    pos2 = Position(
        symbol="GBPUSD", direction=Direction.SELL,
        entry_price=1.2700, initial_stop=1.2750, initial_target=1.2600,
        entry_confidence=0.65, entry_regime="trending_down",
    )
    price_path2 = [1.2700, 1.2710, 1.2725, 1.2718]
    confidences2 = [0.65, 0.55, 0.30, 0.28]
    regimes2 = ["trending_down", "trending_down", "ranging", "ranging"]

    for i, (p, c, r) in enumerate(zip(price_path2, confidences2, regimes2), 1):
        snap = MarketSnapshot(price=p, confidence=c, regime=r)
        decision = mgr.evaluate(pos2, snap)
        print(f"  t={i:02d} price={p:.4f} conf={c:.2f} regime={r:<14} "
              f"-> {decision.action.value:<14} stop={pos2.current_stop:.5f}  {decision.reason}")
        if decision.action == Action.CLOSE:
            print(f"  ** POSITION CLOSED at t={i} **")
            break

    print()
    print("=" * 70)
    print("SCENARIO 3: Regime flips hostile while in profit -> lock in gains, don't close")
    print("=" * 70)
    pos3 = Position(
        symbol="XAUUSD", direction=Direction.BUY,
        entry_price=2400.0, initial_stop=2385.0, initial_target=2450.0,
        entry_confidence=0.70, entry_regime="trending_up",
    )
    price_path3 = [2400, 2410, 2425, 2440]
    confidences3 = [0.70, 0.69, 0.68, 0.67]
    regimes3 = ["trending_up", "trending_up", "trending_up", "trending_down"]

    for i, (p, c, r) in enumerate(zip(price_path3, confidences3, regimes3), 1):
        snap = MarketSnapshot(price=p, confidence=c, regime=r)
        decision = mgr.evaluate(pos3, snap)
        print(f"  t={i:02d} price={p:.4f} conf={c:.2f} regime={r:<14} "
              f"-> {decision.action.value:<14} stop={pos3.current_stop:.5f}  {decision.reason}")


if __name__ == "__main__":
    _demo()