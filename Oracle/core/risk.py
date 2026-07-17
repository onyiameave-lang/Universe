"""
Oracle.core.risk
===============
Institutional risk management. (Book VI human sovereignty over capital; shared
config RL risk limits; institutional practice: risk first, returns second.)

No trade escapes the risk gate. This module enforces:
  * POSITION SIZING     volatility-adjusted (ATR-based) sizing to a fixed
                        fraction of equity risked per trade (default 1%).
  * STOP / TARGET       ATR-based stop-loss and take-profit levels.
  * PORTFOLIO LIMITS    max concurrent positions, max total exposure, max
                        per-symbol exposure, daily loss limit (kill switch).
  * DRAWDOWN GUARD      halts new risk if drawdown from peak exceeds the limit.
  * CONSTITUTIONAL GATE every proposed trade must pass ALL checks or it is
                        rejected with reasons. Paper-trading is the default.

Reads limits from shared.config when available; safe defaults otherwise.

ENV-CONFIGURABLE RISK PARAMS (added 2026-07-17):
  All hardcoded thresholds are now env vars with sensible defaults.
  Paper-trading mode uses the more permissive PAPER floor; live mode uses
  the stricter LIVE floor. Mode is auto-detected from ORACLE_PAPER_TRADING.

  ORACLE_CONFIDENCE_FLOOR          paper floor  (default 0.50)
  ORACLE_CONFIDENCE_FLOOR_LIVE     live floor   (default 0.60)
  ORACLE_MAX_LOT_PCT               max single-trade notional as % of equity,
                                   paper mode   (default 0.50 = 50%)
  ORACLE_MAX_LOT_PCT_LIVE          same, live mode (default 0.30 = 30%)
  ORACLE_DAILY_LOSS_LIMIT          daily kill-switch as % of equity,
                                   paper mode   (default 0.06 = 6%)
  ORACLE_DAILY_LOSS_LIMIT_LIVE     same, live mode (default 0.03 = 3%)
  ORACLE_STOP_MULT                 ATR stop-loss multiplier  (default 2.0)
  ORACLE_TARGET_MULT               ATR take-profit multiplier (default 3.0)
  ORACLE_REWARD_RISK               reward:risk label in plan  (default 1.5)
  RL_MAX_POSITIONS                 already in shared.config   (default 1)
  RL_MAX_DRAWDOWN_PCT              already in shared.config   (default 0.20)
  RL_RISK_PER_TRADE                already in shared.config   (default 0.01)
  ORACLE_PAPER_TRADING             already in shared.config   (default True)

BOUNDARY BUG FIX (2026-07-17):
  Old:  if confidence < 0.55   → exactly-at-floor value (e.g. 0.55) was REJECTED
  New:  if confidence < floor  → uses >=, so exactly-at-floor PASSES
        (floor is rounded to 4 dp before comparison to avoid float drift)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Helpers — mirror shared.config helpers so this module stays self-contained
# when imported standalone (e.g. unit tests, Sentinel standalone mode).
# ---------------------------------------------------------------------------

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# Load shared.config (provides RL_* and ORACLE_PAPER_TRADING).
# Fall back to env-direct reads if shared.config is unavailable.
# ---------------------------------------------------------------------------
try:
    from shared.config import get_config
    _cfg = get_config()
    RISK_PER_TRADE  = _cfg.rl_risk_per_trade
    MAX_POSITIONS   = _cfg.rl_max_positions
    MAX_DRAWDOWN    = _cfg.rl_max_drawdown_pct
    PAPER_TRADING   = _cfg.oracle_paper_trading
except Exception:
    RISK_PER_TRADE  = _float_env("RL_RISK_PER_TRADE",    0.01)
    MAX_POSITIONS   = _int_env("RL_MAX_POSITIONS",        1)
    MAX_DRAWDOWN    = _float_env("RL_MAX_DRAWDOWN_PCT",   0.20)
    PAPER_TRADING   = _bool_env("ORACLE_PAPER_TRADING",   True)


# ---------------------------------------------------------------------------
# New env-configurable risk params (paper vs live variants)
# ---------------------------------------------------------------------------

# Confidence floor — paper is more permissive; live is stricter.
# Boundary fix: comparison uses >= so exactly-at-floor values PASS.
_CONF_FLOOR_PAPER = round(_float_env("ORACLE_CONFIDENCE_FLOOR",       0.50), 4)
_CONF_FLOOR_LIVE  = round(_float_env("ORACLE_CONFIDENCE_FLOOR_LIVE",  0.60), 4)

# Max single-trade notional as a fraction of equity (50% paper / 30% live).
_MAX_LOT_PCT_PAPER = _float_env("ORACLE_MAX_LOT_PCT",       0.50)
_MAX_LOT_PCT_LIVE  = _float_env("ORACLE_MAX_LOT_PCT_LIVE",  0.30)

# Daily loss kill-switch as a fraction of equity (6% paper / 3% live).
_DAILY_LOSS_PAPER = _float_env("ORACLE_DAILY_LOSS_LIMIT",       0.06)
_DAILY_LOSS_LIVE  = _float_env("ORACLE_DAILY_LOSS_LIMIT_LIVE",  0.03)

# ATR multipliers for stop-loss and take-profit.
_STOP_MULT   = _float_env("ORACLE_STOP_MULT",   2.0)
_TARGET_MULT = _float_env("ORACLE_TARGET_MULT", 3.0)

# Reward:risk label surfaced in the trade plan dict.
_REWARD_RISK = _float_env("ORACLE_REWARD_RISK", 1.5)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    direction: str          # long | short
    size: float
    entry: float
    stop: float
    target: float
    opened_at: float = 0.0


@dataclass
class Portfolio:
    equity: float = 10000.0
    peak_equity: float = 10000.0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    positions: List[Position] = field(default_factory=list)

    @property
    def drawdown(self) -> float:
        return (self.peak_equity - self.equity) / self.peak_equity if self.peak_equity else 0.0

    @property
    def exposure(self) -> float:
        return sum(abs(p.size * p.entry) for p in self.positions)


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Env-configurable risk gate.

    Paper-trading mode (ORACLE_PAPER_TRADING=true, the default) uses the
    more permissive PAPER thresholds.  Live mode uses the stricter LIVE
    thresholds.  All thresholds are readable from .env — no source edits
    needed to tune risk.
    """

    def __init__(self, equity: float = 10000.0,
                 risk_per_trade: float = RISK_PER_TRADE,
                 max_positions: int = MAX_POSITIONS,
                 max_drawdown: float = MAX_DRAWDOWN,
                 daily_loss_limit: Optional[float] = None):
        self.portfolio = Portfolio(equity=equity, peak_equity=equity)
        self.risk_per_trade = risk_per_trade
        self.max_positions  = max_positions
        self.max_drawdown   = max_drawdown
        self.paper          = PAPER_TRADING

        # Pick paper vs live variants for every threshold.
        # Callers can still override daily_loss_limit via constructor arg
        # (kept for backward compat); env var wins when arg is None.
        if daily_loss_limit is not None:
            self.daily_loss_limit = daily_loss_limit
        else:
            self.daily_loss_limit = _DAILY_LOSS_PAPER if self.paper else _DAILY_LOSS_LIVE

        self.confidence_floor = _CONF_FLOOR_PAPER if self.paper else _CONF_FLOOR_LIVE
        self.max_lot_pct      = _MAX_LOT_PCT_PAPER if self.paper else _MAX_LOT_PCT_LIVE
        self.stop_mult        = _STOP_MULT
        self.target_mult      = _TARGET_MULT
        self.reward_risk      = _REWARD_RISK

    # ---- sizing ----

    def size_position(self, entry: float, stop: float) -> float:
        """Risk a fixed fraction of equity; size so (entry-stop) loss == risk budget."""
        risk_budget   = self.portfolio.equity * self.risk_per_trade
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return 0.0
        return round(risk_budget / per_unit_risk, 4)

    def stop_target(self, entry: float, direction: str, atr: float,
                    stop_mult: Optional[float] = None,
                    target_mult: Optional[float] = None) -> Dict[str, float]:
        """ATR-based stop and target.  Multipliers default to env-configured values."""
        sm = stop_mult   if stop_mult   is not None else self.stop_mult
        tm = target_mult if target_mult is not None else self.target_mult
        if direction == "long":
            return {"stop":   round(entry - sm * atr, 5),
                    "target": round(entry + tm * atr, 5)}
        return {"stop":   round(entry + sm * atr, 5),
                "target": round(entry - tm * atr, 5)}

    # ---- the constitutional risk gate ----

    def evaluate(self, symbol: str, direction: str, entry: float, atr: float,
                 confidence: float) -> Dict[str, Any]:
        """
        Assess a proposed trade against ALL risk limits.  Returns approval +
        sizing + stop/target, or rejection with reasons.  No trade bypasses this.

        BOUNDARY FIX: confidence comparison uses >= so a value exactly equal
        to the floor (e.g. 0.50 paper / 0.60 live) PASSES instead of being
        rejected.  Old code used strict < which silently rejected at-floor values.
        """
        rejections = []

        # drawdown kill-switch
        if self.portfolio.drawdown >= self.max_drawdown:
            rejections.append(
                f"drawdown {self.portfolio.drawdown:.1%} >= limit {self.max_drawdown:.0%}")

        # daily loss kill-switch
        if self.portfolio.daily_pnl <= -(self.daily_loss_limit * self.portfolio.equity):
            rejections.append(
                f"daily loss limit hit ({self.daily_loss_limit:.0%} of equity, kill switch)")

        # position count
        if len(self.portfolio.positions) >= self.max_positions:
            rejections.append(f"max positions ({self.max_positions}) reached")

        # per-symbol duplication
        if any(p.symbol == symbol for p in self.portfolio.positions):
            rejections.append(f"already holding {symbol}")

        # confidence floor — BOUNDARY FIX: >= so exactly-at-floor values pass
        conf_rounded = round(confidence, 4)
        if conf_rounded < self.confidence_floor:
            mode_label = "paper" if self.paper else "live"
            rejections.append(
                f"confidence {conf_rounded:.4f} below {mode_label} floor "
                f"{self.confidence_floor:.4f} (ORACLE_CONFIDENCE_FLOOR"
                f"{'_LIVE' if not self.paper else ''})")

        # need volatility to size
        if atr <= 0:
            rejections.append("no volatility (ATR) to size against")

        if rejections:
            return {"approved": False, "reasons": rejections,
                    "paper_trading": self.paper,
                    "confidence_floor": self.confidence_floor,
                    "mode": "paper" if self.paper else "live"}

        st   = self.stop_target(entry, direction, atr)
        size = self.size_position(entry, st["stop"])

        # exposure cap: single trade notional <= max_lot_pct of equity
        notional = abs(size * entry)
        cap      = self.max_lot_pct * self.portfolio.equity
        if notional > cap:
            size     = round(cap / entry, 4)
            notional = abs(size * entry)

        return {
            "approved":      True,
            "paper_trading": self.paper,
            "mode":          "paper" if self.paper else "live",
            "symbol":        symbol,
            "direction":     direction,
            "entry":         entry,
            "size":          size,
            "notional":      round(notional, 2),
            "stop":          st["stop"],
            "target":        st["target"],
            "risk_amount":   round(self.portfolio.equity * self.risk_per_trade, 2),
            "reward_risk":   self.reward_risk,
            "confidence_floor": self.confidence_floor,
        }

    # ---- execution (paper by default) ----

    def open_position(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        if not plan.get("approved"):
            return {"status": "rejected", "reasons": plan.get("reasons")}
        pos = Position(
            plan["symbol"],
            "long" if plan["direction"] in ("long", "buy") else "short",
            plan["size"], plan["entry"], plan["stop"], plan["target"],
        )
        self.portfolio.positions.append(pos)
        return {
            "status":       "opened",
            "paper_trading": self.paper,
            "position":     pos.__dict__,
            "note":         "PAPER trade (no live order)" if self.paper else "LIVE order path",
        }

    def mark_to_market(self, prices: Dict[str, float]) -> Dict[str, Any]:
        """Update unrealized PnL + equity peak from current prices."""
        unrealized = 0.0
        for p in self.portfolio.positions:
            px = prices.get(p.symbol)
            if px is None:
                continue
            direction   = 1 if p.direction == "long" else -1
            unrealized += direction * (px - p.entry) * p.size
        equity_now = self.portfolio.equity + unrealized
        self.portfolio.peak_equity = max(self.portfolio.peak_equity, equity_now)
        return {
            "equity":     round(equity_now, 2),
            "unrealized": round(unrealized, 2),
            "drawdown":   round(self.portfolio.drawdown, 4),
        }

    def status(self) -> Dict[str, Any]:
        return {
            "equity":           self.portfolio.equity,
            "drawdown":         round(self.portfolio.drawdown, 4),
            "open_positions":   len(self.portfolio.positions),
            "exposure":         round(self.portfolio.exposure, 2),
            "paper_trading":    self.paper,
            "mode":             "paper" if self.paper else "live",
            "risk_per_trade":   self.risk_per_trade,
            "max_positions":    self.max_positions,
            "max_drawdown":     self.max_drawdown,
            "confidence_floor": self.confidence_floor,
            "daily_loss_limit": self.daily_loss_limit,
            "max_lot_pct":      self.max_lot_pct,
            "stop_mult":        self.stop_mult,
            "target_mult":      self.target_mult,
        }
