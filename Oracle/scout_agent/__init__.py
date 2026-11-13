"""
Oracle.scout_agent
==================
Sub-Agent 1: Opportunity Scouting & Cross-Sectional Tournament Package.
"""
from __future__ import annotations

try:
    from scout_agent.models import (  # type: ignore
        SCOUT_DIMENSIONS,
        ScoutCandidateEvaluation,
        classify_asset_class,
        current_trading_session,
    )
    from scout_agent.structure_analyzer import (  # type: ignore
        compute_htf_structure_score,
        compute_spread_efficiency_score,
        compute_stream_alignment,
    )
    from scout_agent.tournament import (  # type: ignore
        build_tournament_leaderboard,
        rank_cross_sectional_tournament,
    )
    from scout_agent.learner import (  # type: ignore
        compute_learned_conviction_floor,
        compute_learned_dimension_weights,
        update_scout_from_counterfactual,
        update_scout_from_outcome,
    )
    from scout_agent.agent import OracleScoutSubAgent  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import (  # type: ignore
        SCOUT_DIMENSIONS,
        ScoutCandidateEvaluation,
        classify_asset_class,
        current_trading_session,
    )
    from Oracle.scout_agent.structure_analyzer import (  # type: ignore
        compute_htf_structure_score,
        compute_spread_efficiency_score,
        compute_stream_alignment,
    )
    from Oracle.scout_agent.tournament import (  # type: ignore
        build_tournament_leaderboard,
        rank_cross_sectional_tournament,
    )
    from Oracle.scout_agent.learner import (  # type: ignore
        compute_learned_conviction_floor,
        compute_learned_dimension_weights,
        update_scout_from_counterfactual,
        update_scout_from_outcome,
    )
    from Oracle.scout_agent.agent import OracleScoutSubAgent  # type: ignore

__all__ = [
    "SCOUT_DIMENSIONS",
    "ScoutCandidateEvaluation",
    "classify_asset_class",
    "current_trading_session",
    "compute_htf_structure_score",
    "compute_spread_efficiency_score",
    "compute_stream_alignment",
    "rank_cross_sectional_tournament",
    "build_tournament_leaderboard",
    "compute_learned_dimension_weights",
    "compute_learned_conviction_floor",
    "update_scout_from_outcome",
    "update_scout_from_counterfactual",
    "OracleScoutSubAgent",
]
