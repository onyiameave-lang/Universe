"""
Oracle.scout_agent.structure_analyzer
=====================================
Higher-Timeframe (HTF) structure, reward-to-risk geometry, stream alignment,
and execution spread-cost efficiency scoring for OracleScoutSubAgent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def compute_stream_alignment(stream_dict: Dict[str, Any], sign: float) -> float:
    """
    Map signed directional agreement [-1, +1] of an evidence stream into [0, 1]
    weighted by the stream's own confidence.
    """
    if sign == 0.0:
        return 0.0
    s_dir = float(stream_dict.get("direction") or 0.0)
    s_conf = float(stream_dict.get("confidence") or 0.0)
    directional_match = max(-1.0, min(1.0, s_dir * sign))
    return round(0.5 + 0.5 * directional_match * s_conf, 4)


def compute_htf_structure_score(
    direction: str,
    last_price: float,
    atr: float,
    technicals: Dict[str, Any],
    stop_price: float,
    target_price: float,
) -> Tuple[float, float, List[str]]:
    """
    Evaluate structural room-to-run and multi-indicator regime alignment.
    Returns (structure_score in [0, 1], reward_risk_ratio, reasons).
    """
    reasons: List[str] = []
    risk_dist = abs(last_price - stop_price)
    reward_dist = abs(target_price - last_price)
    rr = (reward_dist / risk_dist) if risk_dist > 1e-12 else 0.0

    # Reward-to-risk structural efficiency mapped smoothly via r / (r + 1)
    rr_score = rr / (rr + 1.0) if rr > 0 else 0.0

    regime_info = technicals.get("regime") if isinstance(technicals.get("regime"), dict) else {}
    regime = str(regime_info.get("regime") or "ranging")
    slope = float(regime_info.get("slope_20") or 0.0)
    sign = 1.0 if direction in ("long", "buy") else -1.0

    alignment = 0.5
    if (sign > 0 and regime == "trending_up") or (sign < 0 and regime == "trending_down"):
        alignment = 0.90
        reasons.append(f"HTF regime ({regime}) aligns with {direction.upper()}")
    elif (sign > 0 and regime == "trending_down") or (sign < 0 and regime == "trending_up"):
        alignment = 0.15
        reasons.append(f"HTF regime ({regime}) opposes {direction.upper()}")
    elif regime == "ranging":
        rsi = technicals.get("rsi_14")
        if isinstance(rsi, (int, float)):
            if (sign > 0 and rsi < 40) or (sign < 0 and rsi > 60):
                alignment = 0.75
                reasons.append(f"Range mean-reversion supported by RSI={rsi:.1f}")

    if slope * sign > 0:
        alignment = min(1.0, alignment + 0.1)

    score = round(0.5 * rr_score + 0.5 * alignment, 4)
    return score, round(rr, 4), reasons


def compute_spread_efficiency_score(
    last_price: float,
    stop_price: float,
    spread_price: float = 0.0,
) -> Tuple[float, float]:
    """
    Compute execution spread cost relative to stop distance.
    Returns (spread_efficiency_score in [0, 1], spread_to_risk_ratio).
    """
    stop_dist = abs(last_price - stop_price)
    spread_to_risk = (spread_price / stop_dist) if (stop_dist > 1e-12 and spread_price > 0) else 0.0
    spread_score = max(0.0, min(1.0, 1.0 - spread_to_risk))
    return round(spread_score, 4), round(spread_to_risk, 4)
