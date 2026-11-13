"""
Oracle.cio.counterfactual_replayer
==================================
Replays unentered trades from OracleDecisionLedger against subsequent market
bars (or on-demand ForgeShadowRunner) and feeds counterfactual outcomes
(MISSED_WINNER vs AVOIDED_LOSS) into Scout, Portfolio, and Risk sub-agents.
"""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Any, Dict, List, Optional

_ORACLE_ROOT = Path(__file__).resolve().parents[1]


def replay_unentered_counterfactuals(
    *,
    oracle_agent: Any,
    scout_agent: Any,
    decision_ledger: Any,
    max_items: int = 25,
    max_records: Optional[int] = None,
    invoke_shadow_runner_on_demand: bool = False,
) -> Dict[str, Any]:
    """
    Replay unentered trades from `OracleDecisionLedger` against subsequent
    market price bars (or on-demand `ForgeShadowRunner`) and feed the
    counterfactual outcomes (`MISSED_WINNER` vs `AVOIDED_LOSS`) into Scout.
    """
    limit = max_records if max_records is not None else max_items
    pending = decision_ledger.unentered_trades_pending_replay(max_items=limit)
    replayed: List[Dict[str, Any]] = []
    missed_winners = 0
    avoided_losses = 0

    for item in pending:
        sym = str(item.get("symbol") or "").upper()
        direction = str(item.get("direction") or "hold").lower()
        if direction not in ("long", "short", "buy", "sell"):
            continue
        entry = float(item.get("hypothetical_entry") or 0.0)
        stop = float(item.get("hypothetical_stop") or 0.0)
        target = float(item.get("hypothetical_target") or 0.0)
        risk_unit = abs(entry - stop)
        if entry <= 0 or risk_unit <= 1e-12:
            continue

        would_win = False
        cf_r = 0.0
        try:
            md = oracle_agent.data.get(sym)
            if md.get("status") == "complete" and md.get("series"):
                bars = md["series"].bars
                recent_bars = bars[-15:] if len(bars) >= 15 else bars
                sign = 1.0 if direction in ("long", "buy") else -1.0
                hit_tp = False
                hit_sl = False
                for bar in recent_bars[-5:]:
                    if sign > 0:
                        if target > entry and bar.high >= target:
                            hit_tp = True
                            break
                        if bar.low <= stop:
                            hit_sl = True
                            break
                    else:
                        if 0 < target < entry and bar.low <= target:
                            hit_tp = True
                            break
                        if bar.high >= stop:
                            hit_sl = True
                            break
                if hit_tp:
                    would_win = True
                    cf_r = abs(target - entry) / risk_unit
                elif hit_sl:
                    would_win = False
                    cf_r = -1.0
                else:
                    last_close = float(md["series"].last)
                    cf_r = sign * (last_close - entry) / risk_unit
                    would_win = cf_r > 0.25
        except Exception as exc:
            decision_ledger.expose_failure(
                component="CounterfactualReplay",
                operation="replay_bars",
                error_type=type(exc).__name__,
                error_message=str(exc),
                symbol=sym,
            )
            continue

        shadow_summary = None
        if invoke_shadow_runner_on_demand:
            try:
                from Forge.core.shadow_runner import ForgeShadowRunner  # type: ignore
                eco_root = _ORACLE_ROOT.parent
                runner = ForgeShadowRunner(
                    eco_root,
                    paper_only=True,
                    output_namespace="counterfactual_on_demand",
                )
                sr_out = runner.run_shadow_session(sym, "ALL", 160, False)
                shadow_summary = {
                    "shadow_trades": sr_out.get("shadow_trades_evaluated", 0),
                    "win_rate": sr_out.get("win_rate", 0.0),
                }
                del runner
                gc.collect()
            except Exception as exc:
                decision_ledger.expose_failure(
                    component="ForgeShadowRunner",
                    operation="on_demand_counterfactual",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=sym,
                )

        classification = (
            "MISSED_WINNER"
            if would_win and cf_r >= 0.5
            else "AVOIDED_LOSS"
            if not would_win and cf_r <= -0.25
            else "NEUTRAL"
        )
        if classification == "MISSED_WINNER":
            missed_winners += 1
        elif classification == "AVOIDED_LOSS":
            avoided_losses += 1

        adj = scout_agent.learn_from_counterfactual(
            symbol=sym,
            regime=str(item.get("regime") or "ranging"),
            conviction=float(item.get("effective_confidence") or 0.5),
            stage_rejected=str(item.get("stage_rejected") or "scout"),
            counterfactual_r=cf_r,
            would_have_won=would_win,
        )
        if shadow_summary:
            adj["on_demand_shadow_runner"] = shadow_summary

        rec = decision_ledger.record_counterfactual_replay(
            original_event_id=str(item.get("event_id")),
            symbol=sym,
            direction=direction,
            stage_rejected=str(item.get("stage_rejected") or "scout"),
            reason_code=str(item.get("reason_code") or "unknown"),
            would_have_won=would_win,
            counterfactual_r=cf_r,
            classification=classification,
            learning_adjustments=adj,
        )
        replayed.append(rec)

    updated_floor = scout_agent.learned_conviction_floor()
    return {
        "status": "complete",
        "replayed_count": len(replayed),
        "missed_winners": missed_winners,
        "avoided_losses": avoided_losses,
        "replays": replayed,
        "learned_conviction_floor": updated_floor,
        "updated_conviction_floor": updated_floor,
    }
