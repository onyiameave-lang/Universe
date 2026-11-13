"""
Oracle.risk_agent.learner
=========================
Updates Risk Sub-Agent's empirical correlation overlap, re-entry cooldown,
news/social event, and stop/target geometry distributions after each closed trade.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional


def update_risk_from_outcome(
    risk_state: Dict[str, Any],
    scout_state: Dict[str, Any],
    *,
    max_corr_at_entry: float = 0.0,
    currency_exposure_frac: float = 0.0,
    elapsed_since_prior_close_sec: Optional[float] = None,
    prior_trade_won: Optional[bool] = None,
    stop_atr_mult: float = 2.0,
    target_atr_mult: float = 3.0,
    won: bool,
    pnl_r: float,
    concurrent_positions_count: int = 1,
    symbol: Optional[str] = None,
    news_impact: Optional[str] = None,
    social_risk: Optional[str] = None,
) -> None:
    """Update Risk Sub-Agent's empirical correlation, cooldown, news/social, and geometry priors."""
    if concurrent_positions_count >= 2 or max_corr_at_entry > 0.0:
        overlaps = risk_state.setdefault("overlap_outcomes", [])
        overlaps.append({
            "max_corr": round(float(max_corr_at_entry), 4),
            "currency_exposure_frac": round(float(currency_exposure_frac), 4),
            "joint_loss": (not won and pnl_r < -0.5),
            "pnl_r": round(float(pnl_r), 4),
            "timestamp": time.time(),
        })
        if len(overlaps) > 300:
            del overlaps[:-300]

    if elapsed_since_prior_close_sec is not None and prior_trade_won is not None:
        reentries = risk_state.setdefault("reentry_outcomes", [])
        reentries.append({
            "symbol": str(symbol).upper() if symbol else None,
            "elapsed_sec": round(float(elapsed_since_prior_close_sec), 1),
            "prior_won": bool(prior_trade_won),
            "won": bool(won),
            "pnl_r": round(float(pnl_r), 4),
            "timestamp": time.time(),
        })
        if len(reentries) > 300:
            del reentries[:-300]

    if news_impact:
        lvl = str(news_impact).strip().lower()
        n_map = scout_state.setdefault("news_event_outcomes", {})
        nb = n_map.setdefault(lvl, {"trades": 0, "wins": 0, "sum_r": 0.0})
        nb["trades"] = int(nb.get("trades", 0)) + 1
        if won:
            nb["wins"] = int(nb.get("wins", 0)) + 1
        nb["sum_r"] = round(float(nb.get("sum_r", 0.0)) + float(pnl_r), 4)

    if social_risk:
        lvl = str(social_risk).strip().lower()
        s_map = scout_state.setdefault("social_event_outcomes", {})
        sb = s_map.setdefault(lvl, {"trades": 0, "wins": 0, "sum_r": 0.0})
        sb["trades"] = int(sb.get("trades", 0)) + 1
        if won:
            sb["wins"] = int(sb.get("wins", 0)) + 1
        sb["sum_r"] = round(float(sb.get("sum_r", 0.0)) + float(pnl_r), 4)

    geom = risk_state.setdefault("geometry_outcomes", [])
    geom.append({
        "stop_atr_mult": round(float(stop_atr_mult), 4),
        "target_atr_mult": round(float(target_atr_mult), 4),
        "won": bool(won),
        "pnl_r": round(float(pnl_r), 4),
        "timestamp": time.time(),
    })
    if len(geom) > 300:
        del geom[:-300]
