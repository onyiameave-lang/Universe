"""
Oracle.portfolio_agent.learner
==============================
Updates Portfolio Sub-Agent's empirical concurrency, trade-sequence, and
asset-class return distributions after each closed trade.
"""
from __future__ import annotations

from typing import Any, Dict


def update_portfolio_from_outcome(
    port_state: Dict[str, Any],
    *,
    asset_class: str,
    concurrency_at_entry: int,
    daily_trade_seq: int,
    won: bool,
    pnl_r: float,
) -> None:
    """Update Portfolio Sub-Agent's empirical distributions after a trade closes."""
    port_state["realized_trades"] = int(port_state.get("realized_trades", 0)) + 1
    if won:
        port_state["realized_wins"] = int(port_state.get("realized_wins", 0)) + 1
        port_state["sum_win_r"] = round(float(port_state.get("sum_win_r", 0.0)) + max(0.0, float(pnl_r)), 4)
    else:
        port_state["sum_loss_r"] = round(float(port_state.get("sum_loss_r", 0.0)) + abs(float(pnl_r)), 4)
    port_state["sum_r"] = round(float(port_state.get("sum_r", 0.0)) + float(pnl_r), 4)
    port_state["sum_r2"] = round(float(port_state.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

    conc_key = str(max(1, min(9, int(concurrency_at_entry))))
    conc_map = port_state.setdefault("concurrency_outcomes", {})
    c_bucket = conc_map.setdefault(conc_key, {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0})
    c_bucket["trades"] = int(c_bucket.get("trades", 0)) + 1
    if won:
        c_bucket["wins"] = int(c_bucket.get("wins", 0)) + 1
    c_bucket["sum_r"] = round(float(c_bucket.get("sum_r", 0.0)) + float(pnl_r), 4)
    c_bucket["sum_r2"] = round(float(c_bucket.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

    seq_key = str(max(1, min(24, int(daily_trade_seq))))
    seq_map = port_state.setdefault("daily_sequence_outcomes", {})
    s_bucket = seq_map.setdefault(seq_key, {"trades": 0, "wins": 0, "sum_r": 0.0})
    s_bucket["trades"] = int(s_bucket.get("trades", 0)) + 1
    if won:
        s_bucket["wins"] = int(s_bucket.get("wins", 0)) + 1
    s_bucket["sum_r"] = round(float(s_bucket.get("sum_r", 0.0)) + float(pnl_r), 4)

    ac_map = port_state.setdefault("asset_class_stats", {})
    ac_bucket = ac_map.setdefault(asset_class, {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0})
    ac_bucket["trades"] = int(ac_bucket.get("trades", 0)) + 1
    if won:
        ac_bucket["wins"] = int(ac_bucket.get("wins", 0)) + 1
    ac_bucket["sum_r"] = round(float(ac_bucket.get("sum_r", 0.0)) + float(pnl_r), 4)
    ac_bucket["sum_r2"] = round(float(ac_bucket.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)
