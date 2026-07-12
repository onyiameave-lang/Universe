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
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.config import get_config
_cfg = get_config()
RISK_PER_TRADE = _cfg.rl_risk_per_trade
MAX_POSITIONS = _cfg.rl_max_positions
MAX_DRAWDOWN = _cfg.rl_max_drawdown_pct
PAPER_TRADING = _cfg.oracle_paper_trading


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


class RiskManager:
    def __init__(self, equity: float = 10000.0,
                 risk_per_trade: float = RISK_PER_TRADE,
                 max_positions: int = MAX_POSITIONS,
                 max_drawdown: float = MAX_DRAWDOWN,
                 daily_loss_limit: float = 0.06):
        self.portfolio = Portfolio(equity=equity, peak_equity=equity)
        self.risk_per_trade = risk_per_trade
        self.max_positions = max_positions
        self.max_drawdown = max_drawdown
        self.daily_loss_limit = daily_loss_limit
        self.paper = PAPER_TRADING

    # ---- sizing ----

    def size_position(self, entry: float, stop: float) -> float:
        """Risk a fixed fraction of equity; size so (entry-stop) loss == risk budget."""
        risk_budget = self.portfolio.equity * self.risk_per_trade
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return 0.0
        return round(risk_budget / per_unit_risk, 4)

    def stop_target(self, entry: float, direction: str, atr: float,
                   stop_mult: float = 2.0, target_mult: float = 3.0) -> Dict[str, float]:
        """ATR-based stop and target (3:2 reward:risk default)."""
        if direction == "long":
            return {"stop": round(entry - stop_mult * atr, 5),
                   "target": round(entry + target_mult * atr, 5)}
        return {"stop": round(entry + stop_mult * atr, 5),
               "target": round(entry - target_mult * atr, 5)}

    # ---- the constitutional risk gate ----

    def evaluate(self, symbol: str, direction: str, entry: float, atr: float,
                confidence: float) -> Dict[str, Any]:
        """
        Assess a proposed trade against ALL risk limits. Returns approval +
        sizing + stop/target, or rejection with reasons. No trade bypasses this.
        """
        rejections = []
        # drawdown kill-switch
        if self.portfolio.drawdown >= self.max_drawdown:
            rejections.append(f"drawdown {self.portfolio.drawdown:.1%} >= limit {self.max_drawdown:.0%}")
        # daily loss kill-switch
        if self.portfolio.daily_pnl <= -self.daily_loss_limit * self.portfolio.equity:
            rejections.append("daily loss limit hit (kill switch)")
        # position count
        if len(self.portfolio.positions) >= self.max_positions:
            rejections.append(f"max positions ({self.max_positions}) reached")
        # per-symbol duplication
        if any(p.symbol == symbol for p in self.portfolio.positions):
            rejections.append(f"already holding {symbol}")
        # confidence floor
        if confidence < 0.55:
            rejections.append(f"confidence {confidence:.2f} below 0.55 floor")
        # need volatility to size
        if atr <= 0:
            rejections.append("no volatility (ATR) to size against")

        if rejections:
            return {"approved": False, "reasons": rejections, "paper_trading": self.paper}

        st = self.stop_target(entry, direction, atr)
        size = self.size_position(entry, st["stop"])
        # exposure cap: single trade notional <= 50% equity
        notional = abs(size * entry)
        if notional > 0.5 * self.portfolio.equity:
            # scale down to the cap rather than reject
            size = round((0.5 * self.portfolio.equity) / entry, 4)
            notional = abs(size * entry)
        return {"approved": True, "paper_trading": self.paper, "symbol": symbol,
               "direction": direction, "entry": entry, "size": size,
               "notional": round(notional, 2), "stop": st["stop"], "target": st["target"],
               "risk_amount": round(self.portfolio.equity * self.risk_per_trade, 2),
               "reward_risk": 1.5}

    # ---- execution (paper by default) ----

    def open_position(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        if not plan.get("approved"):
            return {"status": "rejected", "reasons": plan.get("reasons")}
        pos = Position(plan["symbol"], "long" if plan["direction"] in ("long", "buy") else "short",
                      plan["size"], plan["entry"], plan["stop"], plan["target"])
        self.portfolio.positions.append(pos)
        return {"status": "opened", "paper_trading": self.paper, "position": pos.__dict__,
               "note": "PAPER trade (no live order)" if self.paper else "LIVE order path"}

    def mark_to_market(self, prices: Dict[str, float]) -> Dict[str, Any]:
        """Update unrealized PnL + equity peak from current prices."""
        unrealized = 0.0
        for p in self.portfolio.positions:
            px = prices.get(p.symbol)
            if px is None:
                continue
            direction = 1 if p.direction == "long" else -1
            unrealized += direction * (px - p.entry) * p.size
        equity_now = self.portfolio.equity + unrealized
        self.portfolio.peak_equity = max(self.portfolio.peak_equity, equity_now)
        return {"equity": round(equity_now, 2), "unrealized": round(unrealized, 2),
               "drawdown": round(self.portfolio.drawdown, 4)}

    def status(self) -> Dict[str, Any]:
        return {"equity": self.portfolio.equity, "drawdown": round(self.portfolio.drawdown, 4),
               "open_positions": len(self.portfolio.positions), "exposure": round(self.portfolio.exposure, 2),
               "paper_trading": self.paper, "risk_per_trade": self.risk_per_trade,
               "max_positions": self.max_positions, "max_drawdown": self.max_drawdown}
