"""
Oracle.portfolio_agent
======================
Sub-Agent 2: Capital, Slot Budget & Asset Allocation Package.
"""
from __future__ import annotations

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
    from portfolio_agent.agent import OraclePortfolioSubAgent  # type: ignore
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
    from Oracle.portfolio_agent.agent import OraclePortfolioSubAgent  # type: ignore

__all__ = [
    "compute_learned_session_slot_budget",
    "compute_learned_daily_trade_budget",
    "compute_learned_max_drawdown_fraction",
    "compute_learned_daily_loss_limit_fraction",
    "compute_learned_risk_fraction",
    "update_portfolio_from_outcome",
    "OraclePortfolioSubAgent",
]
