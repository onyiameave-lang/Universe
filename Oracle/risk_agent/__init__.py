"""
Oracle.risk_agent
=================
Sub-Agent 3: Correlation, Currency Exposure, News/Social Defense & Cooldown Package.
"""
from __future__ import annotations

try:
    from risk_agent.correlation_matrix import (  # type: ignore
        compute_currency_leg_similarity,
        compute_learned_max_correlation,
        compute_learned_max_currency_exposure_fraction,
        compute_return_correlation,
        extract_currency_legs,
    )
    from risk_agent.cooldown_and_shocks import (  # type: ignore
        compute_learned_cooldown_seconds,
        compute_learned_news_multipliers,
        compute_learned_social_multipliers,
        compute_learned_stop_target_multipliers,
    )
    from risk_agent.learner import update_risk_from_outcome  # type: ignore
    from risk_agent.agent import OracleRiskSubAgent  # type: ignore
except ImportError:
    from Oracle.risk_agent.correlation_matrix import (  # type: ignore
        compute_currency_leg_similarity,
        compute_learned_max_correlation,
        compute_learned_max_currency_exposure_fraction,
        compute_return_correlation,
        extract_currency_legs,
    )
    from Oracle.risk_agent.cooldown_and_shocks import (  # type: ignore
        compute_learned_cooldown_seconds,
        compute_learned_news_multipliers,
        compute_learned_social_multipliers,
        compute_learned_stop_target_multipliers,
    )
    from Oracle.risk_agent.learner import update_risk_from_outcome  # type: ignore
    from Oracle.risk_agent.agent import OracleRiskSubAgent  # type: ignore

__all__ = [
    "extract_currency_legs",
    "compute_return_correlation",
    "compute_currency_leg_similarity",
    "compute_learned_max_correlation",
    "compute_learned_max_currency_exposure_fraction",
    "compute_learned_cooldown_seconds",
    "compute_learned_news_multipliers",
    "compute_learned_social_multipliers",
    "compute_learned_stop_target_multipliers",
    "update_risk_from_outcome",
    "OracleRiskSubAgent",
]
