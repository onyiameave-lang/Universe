"""
Oracle.portfolio_agent.agent
============================
Sub-Agent 2: Capital, Asset-Class Allocation, Session Concurrency Slots,
and Daily Trade Budget Manager.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from scout_agent.models import classify_asset_class  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import classify_asset_class  # type: ignore

try:
    from portfolio_agent.concurrency_bandit import (  # type: ignore
        compute_learned_daily_trade_budget,
        compute_learned_session_slot_budget,
    )
    from portfolio_agent.kelly_allocator import (  # type: ignore
        compute_learned_daily_loss_limit_fraction,
        compute_learned_max_drawdown_fraction,
        compute_learned_risk_fraction,
    )
    from portfolio_agent.learner import update_portfolio_from_outcome  # type: ignore
except ImportError:
    from Oracle.portfolio_agent.concurrency_bandit import (  # type: ignore
        compute_learned_daily_trade_budget,
        compute_learned_session_slot_budget,
    )
    from Oracle.portfolio_agent.kelly_allocator import (  # type: ignore
        compute_learned_daily_loss_limit_fraction,
        compute_learned_max_drawdown_fraction,
        compute_learned_risk_fraction,
    )
    from Oracle.portfolio_agent.learner import update_portfolio_from_outcome  # type: ignore


class OraclePortfolioSubAgent:
    """
    Sub-Agent 2: Capital, Asset-Class Allocation, Session Concurrency Slots,
    and Daily Trade Budget Manager.
    Zero hardcoded slot caps or risk percentages: learns concurrency capacity,
    daily trade budgets, and variance-penalized Kelly risk fractions from
    realized equity and trade-sequence distributions.
    """

    def __init__(
        self,
        learning_store: Any,
        decision_ledger: Any = None,
    ) -> None:
        self.store = learning_store
        self.ledger = decision_ledger

    @property
    def _port_state(self) -> Dict[str, Any]:
        return self.store.data["portfolio"]

    def learned_session_slot_budget(
        self,
        *,
        candidate_universe_size: int = 8,
        current_drawdown_fraction: float = 0.0,
        mean_open_correlation: float = 0.0,
    ) -> int:
        return compute_learned_session_slot_budget(
            self._port_state,
            candidate_universe_size=candidate_universe_size,
            current_drawdown_fraction=current_drawdown_fraction,
            mean_open_correlation=mean_open_correlation,
            learned_max_drawdown_limit=self.learned_max_drawdown_fraction(),
        )

    def learned_daily_trade_budget(self, candidate_universe_size: int = 8) -> int:
        session_slots = self.learned_session_slot_budget(
            candidate_universe_size=candidate_universe_size
        )
        return compute_learned_daily_trade_budget(
            self._port_state,
            session_slots=session_slots,
        )

    def learned_risk_fraction(
        self,
        *,
        effective_confidence: float = 0.60,
        current_drawdown_fraction: float = 0.0,
        asset_class: str = "fx",
    ) -> float:
        return compute_learned_risk_fraction(
            self._port_state,
            effective_confidence=effective_confidence,
            current_drawdown_fraction=current_drawdown_fraction,
            asset_class=asset_class,
        )

    def learned_kelly_risk_fraction(
        self,
        current_drawdown_pct: float = 0.0,
        confidence: float = 0.60,
        asset_class: str = "fx",
    ) -> float:
        return self.learned_risk_fraction(
            effective_confidence=confidence,
            current_drawdown_fraction=current_drawdown_pct,
            asset_class=asset_class,
        )

    def learned_max_drawdown_fraction(self) -> float:
        return compute_learned_max_drawdown_fraction(self._port_state)

    def learned_daily_loss_limit_fraction(self) -> float:
        return compute_learned_daily_loss_limit_fraction(self._port_state)

    def learn_from_outcome(
        self,
        *,
        asset_class: str,
        concurrency_at_entry: int,
        daily_trade_seq: int,
        won: bool,
        pnl_r: float,
    ) -> Dict[str, Any]:
        update_portfolio_from_outcome(
            self._port_state,
            asset_class=asset_class,
            concurrency_at_entry=concurrency_at_entry,
            daily_trade_seq=daily_trade_seq,
            won=won,
            pnl_r=pnl_r,
        )
        self.store.save()
        return {
            "learned_session_slot_budget": self.learned_session_slot_budget(),
            "learned_daily_trade_budget": self.learned_daily_trade_budget(),
            "learned_risk_fraction": self.learned_risk_fraction(asset_class=asset_class),
        }

    def record_trade_outcome(
        self,
        *,
        symbol: str = "",
        asset_class: Optional[str] = None,
        won: bool,
        pnl_r: float,
        active_concurrency: int = 1,
        daily_trade_index: int = 1,
    ) -> Dict[str, Any]:
        resolved_ac = asset_class or (classify_asset_class(symbol) if symbol else "fx")
        return self.learn_from_outcome(
            asset_class=resolved_ac,
            concurrency_at_entry=active_concurrency,
            daily_trade_seq=daily_trade_index,
            won=won,
            pnl_r=pnl_r,
        )

    def status(self, portfolio_state: Any = None) -> Dict[str, Any]:
        dd = float(getattr(portfolio_state, "drawdown", 0.0)) if portfolio_state is not None else 0.0
        return {
            "learned_session_slot_budget": self.learned_session_slot_budget(current_drawdown_fraction=dd),
            "learned_daily_trade_budget": self.learned_daily_trade_budget(),
            "learned_risk_fraction": self.learned_risk_fraction(current_drawdown_fraction=dd),
            "learned_max_drawdown_fraction": self.learned_max_drawdown_fraction(),
            "learned_daily_loss_limit_fraction": self.learned_daily_loss_limit_fraction(),
            "realized_trades": int(self._port_state.get("realized_trades", 0)),
        }
