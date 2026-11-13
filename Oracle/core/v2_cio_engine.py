"""
Oracle.core.v2_cio_engine
=========================
Clean modular facade re-exporting Oracle V2's 3 Sub-Agent packages and CIO package:
  - `Oracle.scout_agent`     (Opportunity Scouting & Cross-Sectional Tournament)
  - `Oracle.portfolio_agent` (Capital, Slot Budget & Asset Allocation Manager)
  - `Oracle.risk_agent`      (Correlation, Currency Exposure & Active Defense Gate)
  - `Oracle.cio`             (Wall Street Chief Investment Officer Engine & Learning Store)
"""
from __future__ import annotations

try:
    from scout_agent import (  # type: ignore
        SCOUT_DIMENSIONS,
        OracleScoutSubAgent,
        ScoutCandidateEvaluation,
        build_tournament_leaderboard,
        classify_asset_class,
        compute_htf_structure_score,
        compute_learned_conviction_floor,
        compute_learned_dimension_weights,
        compute_spread_efficiency_score,
        compute_stream_alignment,
        current_trading_session,
        rank_cross_sectional_tournament,
    )
    from portfolio_agent import (  # type: ignore
        OraclePortfolioSubAgent,
        compute_learned_daily_loss_limit_fraction,
        compute_learned_daily_trade_budget,
        compute_learned_max_drawdown_fraction,
        compute_learned_risk_fraction,
        compute_learned_session_slot_budget,
    )
    from risk_agent import (  # type: ignore
        OracleRiskSubAgent,
        compute_currency_leg_similarity,
        compute_learned_cooldown_seconds,
        compute_learned_max_correlation,
        compute_learned_max_currency_exposure_fraction,
        compute_learned_news_multipliers,
        compute_learned_social_multipliers,
        compute_learned_stop_target_multipliers,
        compute_return_correlation,
        extract_currency_legs,
    )
    from cio import (  # type: ignore
        OracleCIOEngine,
        OracleV2LearningStore,
        conduct_atlas_forge_review,
        replay_unentered_counterfactuals,
    )
except ImportError:
    from Oracle.scout_agent import (  # type: ignore
        SCOUT_DIMENSIONS,
        OracleScoutSubAgent,
        ScoutCandidateEvaluation,
        build_tournament_leaderboard,
        classify_asset_class,
        compute_htf_structure_score,
        compute_learned_conviction_floor,
        compute_learned_dimension_weights,
        compute_spread_efficiency_score,
        compute_stream_alignment,
        current_trading_session,
        rank_cross_sectional_tournament,
    )
    from Oracle.portfolio_agent import (  # type: ignore
        OraclePortfolioSubAgent,
        compute_learned_daily_loss_limit_fraction,
        compute_learned_daily_trade_budget,
        compute_learned_max_drawdown_fraction,
        compute_learned_risk_fraction,
        compute_learned_session_slot_budget,
    )
    from Oracle.risk_agent import (  # type: ignore
        OracleRiskSubAgent,
        compute_currency_leg_similarity,
        compute_learned_cooldown_seconds,
        compute_learned_max_correlation,
        compute_learned_max_currency_exposure_fraction,
        compute_learned_news_multipliers,
        compute_learned_social_multipliers,
        compute_learned_stop_target_multipliers,
        compute_return_correlation,
        extract_currency_legs,
    )
    from Oracle.cio import (  # type: ignore
        OracleCIOEngine,
        OracleV2LearningStore,
        conduct_atlas_forge_review,
        replay_unentered_counterfactuals,
    )

__all__ = [
    "SCOUT_DIMENSIONS",
    "ScoutCandidateEvaluation",
    "classify_asset_class",
    "current_trading_session",
    "extract_currency_legs",
    "compute_return_correlation",
    "compute_currency_leg_similarity",
    "OracleV2LearningStore",
    "OracleScoutSubAgent",
    "OraclePortfolioSubAgent",
    "OracleRiskSubAgent",
    "OracleCIOEngine",
    "conduct_atlas_forge_review",
    "replay_unentered_counterfactuals",
]
