"""
Oracle.portfolio_agent.kelly_allocator
======================================
Empirical Variance-Penalized Fractional Kelly Criterion sizing, asset-class
multipliers, and portfolio drawdown / daily-loss kill-switch boundary learners.
"""
from __future__ import annotations

import math
from typing import Any, Dict


def compute_learned_max_drawdown_fraction(port_state: Dict[str, Any]) -> float:
    """
    Derive the portfolio drawdown kill-switch boundary from the empirical
    distribution of realized session/trade excursions.
    """
    n_trades = int(port_state.get("realized_trades", 0))
    if n_trades < 5:
        return 0.20
    sum_r = float(port_state.get("sum_r", 0.0))
    sum_r2 = float(port_state.get("sum_r2", 0.0))
    mean_r = sum_r / n_trades
    std_r = math.sqrt(max(0.1, (sum_r2 / n_trades) - mean_r ** 2))
    derived = min(0.25, max(0.05, 3.0 * std_r * 0.02))
    return round(derived, 4)


def compute_learned_daily_loss_limit_fraction(port_state: Dict[str, Any]) -> float:
    """
    Derive the daily kill-switch fraction as a function of the learned max
    drawdown boundary and daily session count (1 / sqrt(trading_days_per_week)).
    """
    dd = compute_learned_max_drawdown_fraction(port_state)
    return round(max(0.015, min(0.10, dd / math.sqrt(5.0))), 4)


def compute_learned_risk_fraction(
    port_state: Dict[str, Any],
    *,
    effective_confidence: float = 0.60,
    current_drawdown_fraction: float = 0.0,
    asset_class: str = "fx",
) -> float:
    """
    Compute the fraction of account equity to risk on a single trade using
    the Empirical Variance-Penalized Fractional Kelly Criterion:
      f* = ((p * b - (1 - p)) / (b * (1 + sigma_R^2))) * drawdown_stability * asset_weight
    """
    n_trades = int(port_state.get("realized_trades", 0))
    n_wins = int(port_state.get("realized_wins", 0))
    sum_win_r = float(port_state.get("sum_win_r", 0.0))
    sum_loss_r = abs(float(port_state.get("sum_loss_r", 0.0)))
    sum_r = float(port_state.get("sum_r", 0.0))
    sum_r2 = float(port_state.get("sum_r2", 0.0))

    p = (n_wins + 3.0 * effective_confidence) / (n_trades + 3.0)
    avg_win = (sum_win_r / n_wins) if n_wins > 0 else 1.5
    n_losses = max(0, n_trades - n_wins)
    avg_loss = (sum_loss_r / n_losses) if n_losses > 0 else 1.0
    b = max(0.5, avg_win / max(0.1, avg_loss))

    if n_trades >= 2:
        mean_r = sum_r / n_trades
        var_r = max(0.0, (sum_r2 / n_trades) - (mean_r ** 2))
    else:
        var_r = 1.0

    kelly_edge = (p * b - (1.0 - p)) / b
    raw_fraction = max(0.002, (max(0.05, kelly_edge) / (1.0 + var_r)) * 0.05)

    ac_stats = (port_state.get("asset_class_stats") or {}).get(asset_class, {})
    ac_trades = int(ac_stats.get("trades", 0))
    if ac_trades > 0:
        ac_mean_r = float(ac_stats.get("sum_r", 0.0)) / ac_trades
        ac_mult = max(0.5, min(1.5, 1.0 + 0.25 * math.tanh(ac_mean_r)))
        raw_fraction *= ac_mult

    dd_limit = compute_learned_max_drawdown_fraction(port_state)
    if dd_limit > 0 and current_drawdown_fraction > 0:
        dd_scale = max(0.25, 1.0 - (current_drawdown_fraction / dd_limit))
        raw_fraction *= dd_scale

    return round(max(0.002, min(0.025, raw_fraction)), 5)
