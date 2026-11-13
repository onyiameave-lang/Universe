"""
Oracle.scout_agent.tournament
=============================
Cross-Sectional Opportunity Tournament ranking and leaderboard builder for
OracleScoutSubAgent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

try:
    from scout_agent.models import ScoutCandidateEvaluation  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import ScoutCandidateEvaluation  # type: ignore


def rank_cross_sectional_tournament(
    evaluations: List[ScoutCandidateEvaluation],
) -> Tuple[List[ScoutCandidateEvaluation], List[ScoutCandidateEvaluation]]:
    """
    Rank all evaluated symbols in a cross-sectional tournament.
    Returns (qualifying_ranked, non_qualifying).
    """
    qualifying = [e for e in evaluations if e.passes_conviction_floor]
    non_qualifying = [e for e in evaluations if not e.passes_conviction_floor]
    qualifying.sort(
        key=lambda e: (e.scout_score, e.expected_net_r, e.effective_confidence),
        reverse=True,
    )
    for idx, cand in enumerate(qualifying, start=1):
        cand.tournament_rank = idx
    return qualifying, non_qualifying


def build_tournament_leaderboard(
    qualifying: List[ScoutCandidateEvaluation],
    selected_symbols: set[str],
) -> List[Dict[str, Any]]:
    """Build serializable tournament leaderboard of qualifying setups."""
    return [
        {
            "rank": c.tournament_rank,
            "symbol": c.symbol,
            "direction": c.direction,
            "scout_score": c.scout_score,
            "expected_net_r": c.expected_net_r,
            "effective_confidence": c.effective_confidence,
            "learned_conviction_floor": c.learned_conviction_floor,
            "regime": c.regime,
            "selected": c.symbol in selected_symbols,
        }
        for c in qualifying
    ]
