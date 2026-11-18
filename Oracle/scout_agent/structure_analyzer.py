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
    Evaluate structural room-to-run, entry precision (minimal stop distance),
    asymmetric reward-to-risk geometry (SL < TP), and multi-indicator regime alignment.
    Returns (structure_score in [0, 1], reward_risk_ratio, reasons).
    """
    reasons: List[str] = []
    risk_dist = abs(last_price - stop_price)
    reward_dist = abs(target_price - last_price)
    rr = (reward_dist / risk_dist) if risk_dist > 1e-12 else 0.0

    # Constitutional Law: Stop Loss must be strictly smaller than Take Profit (SL < TP)
    if rr <= 1.0 + 1e-9:
        rr_score = 0.0
        reasons.append(
            f"Constitutional violation: Stop Loss ({risk_dist:.5f}) >= Take Profit ({reward_dist:.5f}) (R:R={rr:.2f} <= 1.0)"
        )
    else:
        # Reward high asymmetric payoff (minimum 1.5R target preferred)
        rr_score = min(1.0, (rr / (rr + 1.0)) * (1.08 if rr >= 1.5 else 0.75))
        reasons.append(
            f"Asymmetric payoff R:R={rr:.2f}x (SL dist={risk_dist:.5f} < TP dist={reward_dist:.5f})"
        )

    # Evaluate entry precision & stop tightness in ATR units (minimal SL near invalidation)
    safe_atr = max(float(atr or 0.0), abs(last_price) * 1e-4, 1e-9)
    stop_atr = risk_dist / safe_atr
    if 0.4 <= stop_atr <= 1.85:
        tight_stop_bonus = 0.08
        reasons.append(f"Tight structural stop loss ({stop_atr:.2f} ATR)")
    elif stop_atr > 2.5:
        tight_stop_bonus = -0.10
        reasons.append(f"Wide stop distance ({stop_atr:.2f} ATR) penalized")
    else:
        tight_stop_bonus = 0.0

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

    raw_score = 0.5 * rr_score + 0.5 * alignment + tight_stop_bonus
    score = round(max(0.0, min(1.0, raw_score)), 4)
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
