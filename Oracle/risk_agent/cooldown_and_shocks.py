"""
Oracle.risk_agent.cooldown_and_shocks
=====================================
Empirical post-trade recovery cooldowns, Sentinel news & Pulse social shock
confidence multipliers, and ATR stop/target geometry learners for
OracleRiskSubAgent.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple


def compute_learned_cooldown_seconds(
    risk_state: Dict[str, Any],
    prior_won: Any = False,
    kind: Optional[str] = None,
    bar_interval_sec: float = 900.0,
) -> float:
    """
    Learn the minimum recovery interval before re-entering the same symbol
    from empirical re-entry outcomes. Starts at 2 M15 bars (2 * bar_interval_sec)
    and expands if rapid re-entries empirically lose money.
    Supports both `learned_cooldown_seconds(prior_won=False)` and
    `learned_cooldown_seconds("EURUSD", "loss")`.
    """
    symbol_filter: Optional[str] = None
    if isinstance(prior_won, str):
        symbol_filter = prior_won.strip().upper()
        resolved_prior_won = str(kind or "loss").strip().lower() in ("win", "true", "1")
    else:
        resolved_prior_won = bool(prior_won)

    reentries = [
        r for r in (risk_state.get("reentry_outcomes") or [])
        if isinstance(r, dict)
        and bool(r.get("prior_won")) == resolved_prior_won
        and (symbol_filter is None or not r.get("symbol") or str(r.get("symbol")).upper() == symbol_filter)
    ]
    base_sec = 2.0 * bar_interval_sec
    if not reentries:
        return round(base_sec, 1)

    losing_intervals = [float(r["elapsed_sec"]) for r in reentries if not r.get("won") and "elapsed_sec" in r]
    winning_intervals = [float(r["elapsed_sec"]) for r in reentries if r.get("won") and "elapsed_sec" in r]

    if losing_intervals:
        avg_loss_dt = sum(losing_intervals) / len(losing_intervals)
        w = len(losing_intervals) / (len(losing_intervals) + 3.0)
        target_cd = max(base_sec * (1.0 + 0.15 * len(losing_intervals)), avg_loss_dt * 1.35)
        base_sec = (1.0 - w) * base_sec + w * target_cd
    elif winning_intervals:
        avg_win_dt = sum(winning_intervals) / len(winning_intervals)
        w = len(winning_intervals) / (len(winning_intervals) + 4.0)
        base_sec = (1.0 - w) * base_sec + w * min(base_sec, max(bar_interval_sec, avg_win_dt))

    return round(max(60.0, min(14400.0, base_sec)), 1)


def compute_learned_news_multipliers(scout_state: Dict[str, Any]) -> Dict[str, float]:
    """
    Learn empirical confidence multipliers for Sentinel news impact levels
    ('none', 'low', 'medium', 'high') from realized outcomes under each level.
    """
    outcomes = scout_state.get("news_event_outcomes") or {}
    priors = {"none": 1.0, "low": 0.96, "medium": 0.85, "high": 0.65}
    result: Dict[str, float] = {}
    for lvl, prior_m in priors.items():
        b = outcomes.get(lvl) or {}
        trades = int(b.get("trades", 0))
        if trades <= 0:
            result[lvl] = prior_m
            continue
        mean_r = float(b.get("sum_r", 0.0)) / trades
        w = trades / (trades + 2.0)
        empirical_m = max(0.25, min(1.05, prior_m * (1.0 + 0.55 * math.tanh(mean_r))))
        result[lvl] = round((1.0 - w) * prior_m + w * empirical_m, 4)
    return result


def compute_learned_social_multipliers(scout_state: Dict[str, Any]) -> Dict[str, float]:
    """
    Learn empirical confidence multipliers for Pulse social risk levels
    ('none', 'low', 'medium', 'high') from realized outcomes under each level.
    """
    outcomes = scout_state.get("social_event_outcomes") or {}
    priors = {"none": 1.0, "low": 0.97, "medium": 0.88, "high": 0.68}
    result: Dict[str, float] = {}
    for lvl, prior_m in priors.items():
        b = outcomes.get(lvl) or {}
        trades = int(b.get("trades", 0))
        if trades <= 0:
            result[lvl] = prior_m
            continue
        mean_r = float(b.get("sum_r", 0.0)) / trades
        w = trades / (trades + 2.0)
        empirical_m = max(0.25, min(1.05, prior_m * (1.0 + 0.55 * math.tanh(mean_r))))
        result[lvl] = round((1.0 - w) * prior_m + w * empirical_m, 4)
    return result


def compute_learned_stop_target_multipliers(risk_state: Dict[str, Any]) -> Tuple[float, float]:
    """
    Learn optimal ATR stop and target multipliers from realized geometry outcomes.
    Starts at golden-ratio structural geometry (phi, phi^2) before outcomes accumulate.
    """
    geom = list(risk_state.get("geometry_outcomes") or [])
    winning = [g for g in geom if isinstance(g, dict) and g.get("won") and float(g.get("pnl_r", 0.0)) > 0]
    if not winning:
        phi = (1.0 + math.sqrt(5.0)) / 2.0
        return round(phi, 3), round(phi * phi, 3)

    total_w = sum(max(0.1, float(g.get("pnl_r", 1.0))) for g in winning)
    stop_m = sum(float(g["stop_atr_mult"]) * max(0.1, float(g.get("pnl_r", 1.0))) for g in winning) / total_w
    targ_m = sum(float(g["target_atr_mult"]) * max(0.1, float(g.get("pnl_r", 1.0))) for g in winning) / total_w
    return round(max(0.8, min(4.0, stop_m)), 3), round(max(1.2, min(6.0, targ_m)), 3)
