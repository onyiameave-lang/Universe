"""
Oracle.scout_agent.learner
==========================
Empirical Dirichlet/Bayesian dimension reliability learner and breakeven
conviction floor optimizer for OracleScoutSubAgent.
(Zero Hardcoded Variables — Books I–VI.)
"""
from __future__ import annotations

import math
import time
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


def compute_learned_conviction_floor(
    scout_state: Dict[str, Any],
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
) -> float:
    """
    Derive the minimum conviction threshold empirically from realized outcomes
    and replayed counterfactuals:
      - Searches the empirical outcome history for the conviction threshold theta
        that maximizes cumulative realized + counterfactual R-expectancy.
      - Adjusts dynamically for the specific (symbol, regime) Bayesian posterior
        win rate when historical observations exist.
    """
    outcomes = list(scout_state.get("conviction_outcomes") or [])
    cf_outcomes = list(scout_state.get("counterfactual_outcomes") or [])

    combined: List[Tuple[float, float]] = []
    for item in outcomes[-250:]:
        if isinstance(item, dict) and "conviction" in item and "pnl_r" in item:
            combined.append((float(item["conviction"]), float(item["pnl_r"])))
    for item in cf_outcomes[-250:]:
        if isinstance(item, dict) and "conviction" in item and "counterfactual_r" in item:
            combined.append((float(item["conviction"]), float(item["counterfactual_r"])))

    if not combined:
        base_floor = float(scout_state.get("learned_conviction_floor", 0.50))
    else:
        candidate_thetas = sorted({round(c, 3) for c, _ in combined})
        best_theta = candidate_thetas[len(candidate_thetas) // 2]
        best_total_r = -1e18
        for theta in candidate_thetas:
            qualifying = [r for c, r in combined if c >= theta]
            if not qualifying:
                continue
            missed_r = sum(max(0.0, r) for c, r in combined if c < theta)
            objective = sum(qualifying) - 0.5 * missed_r
            if objective > best_total_r:
                best_total_r = objective
                best_theta = theta
        n = len(combined)
        credibility = n / (n + 5.0)
        prior_center = sum(c for c, _ in combined) / n
        base_floor = credibility * best_theta + (1.0 - credibility) * prior_center
        scout_state["learned_conviction_floor"] = round(base_floor, 4)

    if symbol and regime:
        sr_key = f"{symbol.upper()}::{regime}"
        sr = (scout_state.get("symbol_regime_stats") or {}).get(sr_key)
        if isinstance(sr, dict):
            w = int(sr.get("wins", 0))
            l = int(sr.get("losses", 0))
            total = w + l
            if total > 0:
                mean_r = float(sr.get("sum_r", 0.0)) / total
                shrink = total / (total + 3.0)
                adjustment = -shrink * math.tanh(mean_r) * (base_floor * 0.25)
                base_floor = max(0.05, min(0.95, base_floor + adjustment))

    return round(float(base_floor), 4)


def update_scout_from_outcome(
    scout_state: Dict[str, Any],
    *,
    symbol: str,
    regime: str,
    session: str,
    entry_conviction: float,
    dimension_scores_at_entry: Dict[str, float],
    won: bool,
    pnl_r: float,
) -> Dict[str, Any]:
    """
    Update Scout's empirical Dirichlet reliability weights, conviction floor
    history, and symbol::regime Bayesian statistics after a closed trade.
    """
    alphas = scout_state.setdefault("dimension_alpha", {d: 1.0 for d in SCOUT_DIMENSIONS})
    betas = scout_state.setdefault("dimension_beta", {d: 1.0 for d in SCOUT_DIMENSIONS})

    magnitude = min(3.0, max(0.25, abs(float(pnl_r))))
    for dim in SCOUT_DIMENSIONS:
        score = float((dimension_scores_at_entry or {}).get(dim, 0.5))
        contribution = max(0.0, score - 0.35)
        if won:
            alphas[dim] = round(float(alphas.get(dim, 1.0)) + contribution * magnitude, 4)
        else:
            betas[dim] = round(float(betas.get(dim, 1.0)) + contribution * magnitude, 4)

    conv_list = scout_state.setdefault("conviction_outcomes", [])
    conv_list.append({
        "conviction": round(float(entry_conviction), 4),
        "pnl_r": round(float(pnl_r), 4),
        "won": bool(won),
        "timestamp": time.time(),
    })
    if len(conv_list) > 500:
        del conv_list[:-500]

    sr_key = f"{symbol.upper()}::{regime}"
    sr_map = scout_state.setdefault("symbol_regime_stats", {})
    sr = sr_map.setdefault(sr_key, {"wins": 0, "losses": 0, "sum_r": 0.0, "sum_r2": 0.0})
    if won:
        sr["wins"] = int(sr.get("wins", 0)) + 1
    else:
        sr["losses"] = int(sr.get("losses", 0)) + 1
    sr["sum_r"] = round(float(sr.get("sum_r", 0.0)) + float(pnl_r), 4)
    sr["sum_r2"] = round(float(sr.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

    sess_map = scout_state.setdefault("session_stats", {})
    ss = sess_map.setdefault(session, {"wins": 0, "losses": 0, "sum_r": 0.0})
    if won:
        ss["wins"] = int(ss.get("wins", 0)) + 1
    else:
        ss["losses"] = int(ss.get("losses", 0)) + 1
    ss["sum_r"] = round(float(ss.get("sum_r", 0.0)) + float(pnl_r), 4)

    new_weights = compute_learned_dimension_weights(scout_state)
    new_floor = compute_learned_conviction_floor(scout_state, symbol, regime)
    return {
        "updated_dimension_weights": new_weights,
        "learned_conviction_floor": new_floor,
        "symbol_regime_record": dict(sr),
    }


def update_scout_from_counterfactual(
    scout_state: Dict[str, Any],
    *,
    symbol: str,
    regime: str,
    conviction: float,
    stage_rejected: str,
    counterfactual_r: float,
    would_have_won: bool,
) -> Dict[str, Any]:
    """
    Update Scout's conviction floor distribution when Chronicle Night Research
    or Forge Shadow Runner replays a trade that Oracle did NOT enter.
    """
    cf_list = scout_state.setdefault("counterfactual_outcomes", [])
    cf_list.append({
        "symbol": symbol.upper(),
        "regime": regime,
        "conviction": round(float(conviction), 4),
        "stage": stage_rejected,
        "counterfactual_r": round(float(counterfactual_r), 4),
        "would_have_won": bool(would_have_won),
        "timestamp": time.time(),
    })
    if len(cf_list) > 500:
        del cf_list[:-500]
    new_floor = compute_learned_conviction_floor(scout_state, symbol, regime)
    return {"learned_conviction_floor": new_floor, "counterfactual_count": len(cf_list)}
