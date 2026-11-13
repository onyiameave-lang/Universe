"""
Oracle.portfolio_agent.concurrency_bandit
=========================================
Empirical session concurrency slot budget learner E[R | concurrency = k] and
daily sequence overtrading decay boundary learner E[R | trade_seq = m].
(Zero Hardcoded Variables — Books I–VI.)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List


def compute_learned_session_slot_budget(
    port_state: Dict[str, Any],
    *,
    candidate_universe_size: int = 8,
    current_drawdown_fraction: float = 0.0,
    mean_open_correlation: float = 0.0,
    learned_max_drawdown_limit: float = 0.20,
) -> int:
    """
    Derive the maximum concurrent open positions for the current session
    without hardcoding '3':
      - Evaluates the empirical concurrency bandit E[R | concurrency = k].
      - Before concurrency history accumulates, derives the baseline diversification
        capacity from the square-root independent breadth law over the uncorrelated
        universe breadth: round(sqrt(universe_size) * (1 - 0.5 * mean_corr)).
      - Scales down automatically during drawdown or high cross-position correlation,
        and selects the concurrency tier with the highest posterior expectancy.
    """
    conc = port_state.get("concurrency_outcomes") or {}

    uncorrelated_breadth = max(
        1.0,
        math.sqrt(max(1, candidate_universe_size)) * (1.0 - 0.5 * abs(mean_open_correlation)),
    )
    prior_slots = max(1, int(round(uncorrelated_breadth)))

    positive_scores: Dict[int, float] = {}
    observed_negative_k: List[int] = []
    for k_str, bucket in conc.items():
        trades = int(bucket.get("trades", 0))
        if trades <= 0:
            continue
        k_val = int(k_str)
        mean_r = float(bucket.get("sum_r", 0.0)) / trades
        post_r = (trades * mean_r) / (trades + 2.0)
        if post_r > 0.05:
            positive_scores[k_val] = post_r
        elif post_r < -0.10:
            observed_negative_k.append(k_val)

    learned_slots = prior_slots
    if positive_scores:
        best_positive_k = max(positive_scores.keys(), key=lambda k: (positive_scores[k], k))
        learned_slots = best_positive_k
        negative_above = [k for k in observed_negative_k if k > best_positive_k]
        if negative_above:
            learned_slots = min(learned_slots, max(1, min(negative_above) - 1))
    elif observed_negative_k:
        worst_k = min(observed_negative_k)
        learned_slots = min(learned_slots, max(1, worst_k - 1))

    if learned_max_drawdown_limit > 0 and current_drawdown_fraction > 0:
        dd_ratio = min(1.0, current_drawdown_fraction / learned_max_drawdown_limit)
        learned_slots = max(1, int(round(learned_slots * (1.0 - 0.6 * dd_ratio))))

    return max(1, learned_slots)


def compute_learned_daily_trade_budget(
    port_state: Dict[str, Any],
    *,
    session_slots: int,
) -> int:
    """
    Derive the daily trade budget empirically from the daily trade-sequence
    expectancy curve E[R | trade_index_today = m].
    Identifies the sequence index where marginal daily trades turn negative
    (overtrading decay boundary).
    """
    seq = port_state.get("daily_sequence_outcomes") or {}
    baseline_daily = max(session_slots, session_slots * 3)

    positive_m: List[int] = []
    negative_m: List[int] = []
    for m_str, bucket in seq.items():
        trades = int(bucket.get("trades", 0))
        if trades <= 0:
            continue
        m_val = int(m_str)
        mean_r = float(bucket.get("sum_r", 0.0)) / trades
        if mean_r >= 0.0:
            positive_m.append(m_val)
        elif mean_r < -0.15 and trades >= 2:
            negative_m.append(m_val)

    budget = baseline_daily
    if positive_m:
        budget = max(budget, max(positive_m))
        negative_above = [m for m in negative_m if m > max(positive_m)]
        if negative_above:
            budget = min(budget, max(1, min(negative_above) - 1))
    elif negative_m:
        budget = min(budget, max(1, min(negative_m) - 1))

    return max(1, budget)
