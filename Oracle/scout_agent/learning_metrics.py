"""Pure empirical learning metrics used by Oracle Scout."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:
    from scout_agent.models import SCOUT_DIMENSIONS  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import SCOUT_DIMENSIONS  # type: ignore


def compute_learned_dimension_weights(scout_state: Dict[str, Any]) -> Dict[str, float]:
    """
    Compute normalized posterior mean reliability weights from Beta(alpha_d, beta_d)
    for each evidence dimension. Starts at maximum-entropy uniform 1/K and shifts
    toward dimensions that empirically predict profitable trades.
    """
    alphas = scout_state.get("dimension_alpha", {})
    betas = scout_state.get("dimension_beta", {})
    raw: Dict[str, float] = {}
    for dim in SCOUT_DIMENSIONS:
        a = max(0.1, float(alphas.get(dim, 1.0)))
        b = max(0.1, float(betas.get(dim, 1.0)))
        raw[dim] = a / (a + b)
    total = sum(raw.values()) or 1.0
    weights = {dim: round(val / total, 6) for dim, val in raw.items()}
    scout_state["dimension_weights"] = weights
    return weights


def compute_learned_session_edge_adjustment(
    scout_state: Dict[str, Any], session: Optional[str]
) -> float:
    """Return a small-sample-shrunk session edge relative to all sessions."""
    if not session:
        return 0.0
    sessions = scout_state.get("session_stats") or {}
    local = sessions.get(str(session).lower())
    if not isinstance(local, dict):
        return 0.0
    try:
        local_n = max(0, int(local.get("wins", 0))) + max(0, int(local.get("losses", 0)))
        if local_n <= 0:
            return 0.0
        local_sum = float(local.get("sum_r", 0.0))
        if not math.isfinite(local_sum):
            return 0.0
        total_n = 0
        total_sum = 0.0
        for row in sessions.values():
            if not isinstance(row, dict):
                continue
            row_n = int(row.get("wins", 0)) + int(row.get("losses", 0))
            row_sum = float(row.get("sum_r", 0.0))
            if row_n > 0 and math.isfinite(row_sum):
                total_n += row_n
                total_sum += row_sum
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if total_n <= 0:
        return 0.0
    session_mean = local_sum / local_n
    all_session_mean = total_sum / total_n
    credibility = local_n / (local_n + 5.0)
    # Keep a single extreme outcome from dominating cross-sectional ranking.
    return round(max(-0.25, min(0.25, credibility * (session_mean - all_session_mean))), 4)


def blend_strategy_performance(
    scout_state: Dict[str, Any],
    *,
    symbol: str,
    regime: str,
    strategy_id: Optional[str],
    prior_win_probability: float,
    prior_mean_r: float,
) -> Tuple[float, float]:
    """Shrink strategy-specific evidence toward the symbol/regime estimate."""
    if not strategy_id:
        return prior_win_probability, prior_mean_r
    key = f"{symbol.upper()}::{regime}::{strategy_id}"
    row = (scout_state.get("strategy_stats") or {}).get(key)
    if not isinstance(row, dict):
        return prior_win_probability, prior_mean_r
    try:
        wins = max(0, int(row.get("wins", 0)))
        losses = max(0, int(row.get("losses", 0)))
        sum_r = float(row.get("sum_r", 0.0))
    except (TypeError, ValueError, OverflowError):
        return prior_win_probability, prior_mean_r
    trades = wins + losses
    if trades <= 0 or not math.isfinite(sum_r):
        return prior_win_probability, prior_mean_r
    credibility = trades / (trades + 5.0)
    local_win_rate = wins / trades
    local_mean_r = sum_r / trades
    return (
        (1.0 - credibility) * prior_win_probability + credibility * local_win_rate,
        (1.0 - credibility) * prior_mean_r + credibility * local_mean_r,
    )


def compare_strategy_performance(
    scout_state: Dict[str, Any], *, symbol: str, regime: str, strategy_id: Optional[str]
) -> Dict[str, Any]:
    """Present realized and shadow evidence side by side without mixing samples."""
    if not strategy_id:
        return {"realized": None, "shadow": None, "shadow_used_for_live_scoring": False}
    key = f"{symbol.upper()}::{regime}::{strategy_id}"

    def summarize(source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        row = source.get(key)
        if not isinstance(row, dict):
            return None
        try:
            wins = max(0, int(row.get("wins", 0)))
            losses = max(0, int(row.get("losses", 0)))
            observations = max(wins + losses, int(row.get("observations", 0)))
            sum_r = float(row.get("sum_r", 0.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(sum_r):
            return None
        total = wins + losses
        return {
            "observations": observations,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 4) if total else None,
            "mean_r": round(sum_r / observations, 4) if observations else None,
        }

    return {
        "realized": summarize(scout_state.get("strategy_stats") or {}),
        "shadow": summarize(scout_state.get("shadow_strategy_stats") or {}),
        "shadow_used_for_live_scoring": False,
    }


def compute_learned_conviction_floor(
    scout_state: Dict[str, Any],
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
) -> float:
    """
    Learn a live conviction threshold from realized trades only. The most
    recent chronological portion is reserved as a holdout: a candidate learned
    on earlier outcomes is accepted only when its holdout mean-R lower bound
    remains positive. Replayed or shadow outcomes stay research-only.
    """
    outcomes = list(scout_state.get("conviction_outcomes") or [])[-250:]
    realized: List[Tuple[float, float]] = []
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        try:
            conviction = float(item["conviction"])
            pnl_r = float(item["pnl_r"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(conviction) and math.isfinite(pnl_r) and 0.0 <= conviction <= 1.0:
            realized.append((conviction, pnl_r))

    try:
        base_floor = float(scout_state.get("learned_conviction_floor", 0.50))
    except (TypeError, ValueError, OverflowError):
        base_floor = 0.50
    if not math.isfinite(base_floor):
        base_floor = 0.50
    base_floor = max(0.05, min(0.95, base_floor))

    # Require enough real outcomes to tune and then independently validate a
    # threshold. Keep ordering intact: the tail is a chronological holdout.
    holdout_n = max(10, int(len(realized) * 0.30))
    if len(realized) >= 30 and len(realized) - holdout_n >= 20:
        train, holdout = realized[:-holdout_n], realized[-holdout_n:]
        candidate_thetas = sorted({round(c, 3) for c, _ in train})
        best_theta: Optional[float] = None
        best_train_score = -math.inf
        for theta in candidate_thetas:
            qualifying = [r for c, r in train if c >= theta]
            if len(qualifying) < 8:
                continue
            # Optimize per-trade realized expectancy with a modest sample-size
            # term; do not reward a threshold merely for rejecting most data.
            mean_r = sum(qualifying) / len(qualifying)
            score = mean_r * math.sqrt(len(qualifying))
            if score > best_train_score:
                best_train_score = score
                best_theta = theta

        if best_theta is not None:
            validated = [r for c, r in holdout if c >= best_theta]
            if len(validated) >= 10:
                mean_r = sum(validated) / len(validated)
                variance = sum((r - mean_r) ** 2 for r in validated) / max(1, len(validated) - 1)
                lower_95 = mean_r - 1.96 * math.sqrt(variance / len(validated))
                if math.isfinite(lower_95) and lower_95 > 0.0:
                    prior_center = sum(c for c, _ in train) / len(train)
                    credibility = len(train) / (len(train) + 30.0)
                    base_floor = max(0.05, min(0.95,
                        credibility * best_theta + (1.0 - credibility) * prior_center))
                    scout_state["learned_conviction_floor"] = round(base_floor, 4)
                    scout_state["learned_conviction_validation"] = {
                        "method": "chronological_holdout_mean_r_lower_95",
                        "train_observations": len(train),
                        "holdout_observations": len(validated),
                        "threshold": round(best_theta, 4),
                        "holdout_mean_r": round(mean_r, 4),
                        "holdout_lower_95_r": round(lower_95, 4),
                    }

    return round(float(base_floor), 4)
