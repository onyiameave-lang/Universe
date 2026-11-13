"""
Oracle.cio.tournament_runner
============================
Executes the multi-symbol Wall Street CIO Tournament cycle across Scout,
Portfolio, and Risk sub-agents.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from scout_agent import (  # type: ignore
        ScoutCandidateEvaluation,
        build_tournament_leaderboard,
    )
except ImportError:
    from Oracle.scout_agent import (  # type: ignore
        ScoutCandidateEvaluation,
        build_tournament_leaderboard,
    )


def record_rejection(
    decision_ledger: Any,
    cand: ScoutCandidateEvaluation,
    candidates_evaluated: int,
    stage_rejected: str,
    reason_code: str,
    reasons: List[str],
) -> None:
    decision_ledger.record_trade_not_entered(
        symbol=cand.symbol,
        direction=cand.direction if cand.direction in ("long", "short") else "hold",
        hypothetical_entry=cand.last_price,
        hypothetical_stop=cand.hypothetical_stop,
        hypothetical_target=cand.hypothetical_target,
        scout_score=cand.scout_score,
        effective_confidence=cand.effective_confidence,
        tournament_rank=cand.tournament_rank,
        candidates_evaluated=candidates_evaluated,
        regime=cand.regime,
        session=cand.session,
        strategy_id=cand.strategy_id,
        stage_rejected=stage_rejected,
        reason_code=reason_code,
        reasons=reasons,
        streams=cand.streams,
        news_assessment=cand.news_assessment,
        social_assessment=cand.social_assessment,
    )


def execute_cio_tournament_cycle(
    cio_engine: Any,
    symbols: List[str],
    *,
    open_positions: Optional[List[Dict[str, Any]]] = None,
    max_session_slots_override: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Execute a full Wall Street CIO cycle across `symbols`:
      1. Scout evaluates all candidate symbols.
      2. Scout ranks all qualifying candidates in a Cross-Sectional Tournament.
      3. Portfolio determines session slots and daily trade budget.
      4. Risk evaluates top-ranked candidates sequentially against open & batch-selected positions.
      5. Logs every selected trade, every unentered trade, and every exposed failure.
    """
    clean_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    cio_engine.sync_learned_limits_to_risk_manager(universe_size=len(clean_symbols))

    rm = getattr(cio_engine.oracle, "risk", None)
    current_open: List[Dict[str, Any]] = (
        list(open_positions)
        if open_positions is not None
        else ([p.__dict__ for p in rm.portfolio.positions] if rm is not None else [])
    )
    open_symbols = {str(p.get("symbol") or "").upper() for p in current_open}
    evaluations: List[ScoutCandidateEvaluation] = []
    price_histories: Dict[str, List[float]] = {}
    errors: List[Dict[str, Any]] = []

    for op in open_symbols:
        try:
            md = cio_engine.oracle.data.get(op)
            if md.get("status") == "complete" and md.get("series"):
                price_histories[op] = list(md["series"].closes)
        except Exception as exc:
            cio_engine.decision_ledger.expose_failure(
                component="MarketData",
                operation="fetch_open_position_history",
                error_type=type(exc).__name__,
                error_message=str(exc),
                symbol=op,
            )

    news_mults = cio_engine.risk_subagent.learned_news_multipliers()
    social_mults = cio_engine.risk_subagent.learned_social_multipliers()

    for sym in clean_symbols:
        if sym in open_symbols:
            continue
        try:
            sig = cio_engine.oracle.signal(sym)
            if sig.get("status") != "complete":
                err_msg = str(sig.get("message") or "signal incomplete")
                errors.append({"symbol": sym, "message": err_msg})
                cio_engine.decision_ledger.expose_failure(
                    component="OracleSignal",
                    operation="signal",
                    error_type="SignalError",
                    error_message=err_msg,
                    symbol=sym,
                )
                continue

            md = cio_engine.oracle.data.get(sym)
            if md.get("status") == "complete" and md.get("series"):
                price_histories[sym] = list(md["series"].closes)

            fused = sig["signal"]
            streams = fused.get("streams") or {}
            last_px = float(sig["last"])
            atr_val = float(sig.get("atr") or (last_px * 0.01))
            call = str(fused.get("call") or "hold").lower()
            dir_str = "long" if call == "buy" else "short"
            st_levels = rm.stop_target(last_px, dir_str, atr_val) if rm else {
                "stop": last_px - 2.0 * atr_val,
                "target": last_px + 3.0 * atr_val,
            }

            cand = cio_engine.scout.evaluate_candidate(
                symbol=sym,
                last_price=last_px,
                atr=atr_val,
                technicals=sig.get("_technicals") or {},
                fused_signal=fused,
                streams=streams,
                stop_price=st_levels["stop"],
                target_price=st_levels["target"],
                using_evolved_champion=bool(sig.get("using_evolved_champion")),
                strategy_id=(streams.get("technical") or {}).get("genome"),
                learned_news_multipliers=news_mults,
                learned_social_multipliers=social_mults,
            )
            evaluations.append(cand)
        except Exception as exc:
            errors.append({"symbol": sym, "message": str(exc)})
            cio_engine.decision_ledger.expose_failure(
                component="OracleScoutSubAgent",
                operation="evaluate_candidate",
                error_type=type(exc).__name__,
                error_message=str(exc),
                symbol=sym,
            )

    qualifying, non_qualifying = cio_engine.scout.rank_tournament(evaluations)

    for nq in non_qualifying:
        reason_code = (
            "no_directional_setup"
            if nq.direction == "hold"
            else "news_shock_pause"
            if nq.news_assessment.get("pause_new_entries")
            else "social_manipulation_pause"
            if nq.social_assessment.get("pause_new_entries")
            else "below_learned_conviction_floor"
        )
        record_rejection(
            cio_engine.decision_ledger, nq, len(evaluations), "scout", reason_code, nq.why_not_reasons or [reason_code]
        )

    dd_now = rm.portfolio.drawdown if rm else 0.0
    learned_slots = cio_engine.portfolio.learned_session_slot_budget(
        candidate_universe_size=len(clean_symbols),
        current_drawdown_fraction=dd_now,
    )
    session_slot_cap = (
        min(learned_slots, max_session_slots_override)
        if max_session_slots_override is not None
        else learned_slots
    )
    available_slots = max(0, session_slot_cap - len(current_open))
    daily_budget = cio_engine.portfolio.learned_daily_trade_budget(candidate_universe_size=len(clean_symbols))
    opened_today_total = sum((rm.portfolio.trades_today or {}).values()) if rm else 0
    remaining_daily_budget = max(0, daily_budget - opened_today_total)

    selected_candidates: List[ScoutCandidateEvaluation] = []
    approved_plans: List[Dict[str, Any]] = []
    skipped_qualifying: List[Dict[str, Any]] = []

    for cand in qualifying:
        if len(selected_candidates) >= remaining_daily_budget:
            reason = (
                f"Outranked for daily trade budget: ranked #{cand.tournament_rank}/{len(qualifying)} "
                f"(learned daily budget={daily_budget}, opened_today={opened_today_total})"
            )
            record_rejection(
                cio_engine.decision_ledger, cand, len(evaluations), "portfolio", "daily_trade_budget_exhausted", [reason]
            )
            skipped_qualifying.append({"symbol": cand.symbol, "rank": cand.tournament_rank, "reason": reason})
            continue

        if len(selected_candidates) >= available_slots:
            winners_str = ", ".join(
                f"{s.symbol}(#{s.tournament_rank}, score={s.scout_score:.3f})" for s in selected_candidates
            )
            reason = (
                f"Outranked in Scout Tournament: ranked #{cand.tournament_rank}/{len(qualifying)} "
                f"(score={cand.scout_score:.3f}); session slot budget ({session_slot_cap}) filled by [{winners_str}]"
            )
            record_rejection(
                cio_engine.decision_ledger, cand, len(evaluations), "portfolio", "outranked_for_session_slots", [reason]
            )
            skipped_qualifying.append({"symbol": cand.symbol, "rank": cand.tournament_rank, "reason": reason})
            continue

        since_loss = rm.portfolio.seconds_since_last_loss(cand.symbol) if rm else None
        since_win = rm.portfolio.seconds_since_last_win(cand.symbol) if rm else None
        risk_check = cio_engine.risk_subagent.evaluate_candidate_risk(
            candidate=cand,
            open_positions=current_open,
            selected_in_batch=selected_candidates,
            price_histories=price_histories,
            seconds_since_last_loss=since_loss,
            seconds_since_last_win=since_win,
        )
        if not risk_check["approved"]:
            record_rejection(
                cio_engine.decision_ledger,
                cand,
                len(evaluations),
                "risk",
                "correlation_or_cooldown_rejected",
                risk_check["reasons"],
            )
            skipped_qualifying.append({
                "symbol": cand.symbol,
                "rank": cand.tournament_rank,
                "reason": "; ".join(risk_check["reasons"]),
            })
            continue

        if rm is not None:
            rm.risk_per_trade = cio_engine.portfolio.learned_risk_fraction(
                effective_confidence=cand.effective_confidence,
                current_drawdown_fraction=dd_now,
                asset_class=cand.asset_class,
            )
            plan = rm.evaluate(
                cand.symbol,
                cand.direction,
                cand.last_price,
                cand.atr,
                cand.effective_confidence,
            )
            if not plan.get("approved"):
                reasons = plan.get("reasons") or ["risk gate rejected"]
                record_rejection(
                    cio_engine.decision_ledger, cand, len(evaluations), "risk", "account_risk_gate_rejected", reasons
                )
                skipped_qualifying.append({
                    "symbol": cand.symbol,
                    "rank": cand.tournament_rank,
                    "reason": "; ".join(reasons),
                })
                continue
        else:
            plan = {
                "approved": True,
                "symbol": cand.symbol,
                "direction": cand.direction,
                "entry": cand.last_price,
                "stop": cand.hypothetical_stop,
                "target": cand.hypothetical_target,
                "size": 1.0,
            }

        selected_candidates.append(cand)
        plan["scout_score"] = cand.scout_score
        plan["expected_net_r"] = cand.expected_net_r
        plan["tournament_rank"] = cand.tournament_rank
        plan["candidates_evaluated"] = len(evaluations)
        plan["qualifying_setups_count"] = len(qualifying)
        plan["why_trade_reasons"] = cand.why_trade_reasons
        plan["dimension_scores"] = cand.dimension_scores
        plan["highest_directional_correlation"] = risk_check["highest_directional_correlation"]

        approved_plans.append({
            "symbol": cand.symbol,
            "plan": plan,
            "signal": {
                "symbol": cand.symbol,
                "call": cand.call,
                "direction": 1.0 if cand.direction == "long" else -1.0,
                "confidence": cand.raw_confidence,
                "streams": cand.streams,
            },
            "effective_confidence": cand.effective_confidence,
            "scout_score": cand.scout_score,
            "expected_net_r": cand.expected_net_r,
            "tournament_rank": cand.tournament_rank,
            "using_evolved_champion": cand.using_evolved_champion,
            "news_impact": cand.news_assessment["level"],
            "news_reason": cand.news_assessment["reason"],
            "social_risk": cand.social_assessment["level"],
            "social_reason": cand.social_assessment["reason"],
            "atr": cand.atr,
            "_streams": cand.streams,
            "_dimension_scores": cand.dimension_scores,
            "_risk_check": risk_check,
        })

    selected_syms = {p["symbol"] for p in approved_plans}
    return {
        "status": "complete",
        "schema": "oracle.v2.cio_tournament.v1",
        "candidates_scanned": len(clean_symbols),
        "candidates_evaluated": len(evaluations),
        "qualifying_setups_count": len(qualifying),
        "learned_session_slot_budget": session_slot_cap,
        "available_session_slots": available_slots,
        "learned_daily_trade_budget": daily_budget,
        "remaining_daily_budget": remaining_daily_budget,
        "selected_count": len(approved_plans),
        "selected_trades": approved_plans,
        "skipped_qualifying_setups": skipped_qualifying,
        "leaderboard": build_tournament_leaderboard(qualifying, selected_syms),
        "errors": errors,
    }
