"""Small execution-loop hook for replaying matured shadow trade candidates."""
from __future__ import annotations

import logging
from typing import Any, Dict


def replay_shadow_candidates(
    oracle: Any, *, max_items: int = 100, trade_manager: Any = None,
    current_snapshots: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Update Scout from completed paper-only outcomes after each scan cycle."""
    cio = getattr(oracle, "cio", None)
    replay = getattr(cio, "replay_pending_counterfactuals", None)
    if not callable(replay):
        return {"status": "unavailable", "replayed_count": 0}
    try:
        result = replay(
            max_items=max_items, shadow_only=True,
            trade_manager=trade_manager, current_snapshots=current_snapshots or {},
        )
        logging.getLogger("oracle.shadow_feedback").info(
            "Shadow feedback: settled=%d still_open=%d data_unavailable=%d",
            int(result.get("replayed_count", 0)),
            int(result.get("still_open", 0)),
            int(result.get("data_unavailable", 0)),
        )
        return result
    except Exception as exc:
        logging.getLogger("oracle.shadow_feedback").warning(
            "Shadow feedback replay failed: %s", type(exc).__name__
        )
        return {"status": "error", "replayed_count": 0, "error": type(exc).__name__}


def record_max_positions_shadow(
    ledger: Any, *, symbol: str, plan: Dict[str, Any], signal: Dict[str, Any], reason: str,
    tournament_rank: int | None = None, candidates_evaluated: int = 1,
) -> bool:
    """Persist one candidate rejected only because the account cap was full."""
    if ledger is None:
        return False
    evaluation = signal.get("scout_evaluation") or {}
    direction = str(plan.get("direction") or "hold")
    try:
        entry, stop, target = (float(plan.get(level) or 0.0) for level in ("entry", "stop", "target"))
        valid_levels = all(value > 0 for value in (entry, stop, target))
    except (TypeError, ValueError, OverflowError):
        valid_levels = False
        entry = stop = target = 0.0
    if direction in ("long", "buy"):
        valid_levels = valid_levels and stop < entry < target
    elif direction in ("short", "sell"):
        valid_levels = valid_levels and target < entry < stop
    if direction not in ("long", "short", "buy", "sell") or not valid_levels:
        return False
    strategy_id = evaluation.get("strategy_id")
    is_pending = getattr(ledger, "has_pending_shadow_candidate", None)
    if callable(is_pending) and is_pending(
        symbol=symbol, direction=direction, strategy_id=strategy_id
    ):
        record_overlap = getattr(ledger, "record_shadow_candidate_overlap", None)
        if callable(record_overlap):
            record_overlap(
                symbol=symbol, direction=direction, strategy_id=strategy_id,
                candidate_context={
                    "entry": entry, "stop": stop, "target": target,
                    "reason": reason,
                    "scout_score": signal.get("scout_score"),
                },
            )
        return False
    try:
        ledger.record_trade_not_entered(
            symbol=symbol,
            direction=direction,
            rejection_stage="max_positions_shadow",
            primary_reason=reason,
            reasons=[reason],
            scout_score=float(signal.get("scout_score") or signal.get("effective_confidence") or 0.0),
            entry_confidence=float(signal.get("effective_confidence") or 0.0),
            learned_conviction_floor=float(signal.get("learned_conviction_floor", 0.5)),
            tournament_rank=tournament_rank,
            candidates_evaluated=candidates_evaluated,
            regime=str(evaluation.get("regime") or "ranging"),
            session=str(evaluation.get("session") or "unknown"),
            strategy_id=str(strategy_id) if strategy_id else None,
            hypothetical_entry_price=float(plan.get("entry") or 0.0),
            hypothetical_stop=float(plan.get("stop") or 0.0),
            hypothetical_target=float(plan.get("target") or 0.0),
            atr=float(signal.get("atr") or 0.0),
            entry_streams=dict(signal.get("_streams") or {}),
            shadow_observation=True,
            dimension_scores=dict(evaluation.get("dimension_scores") or {}),
            learned_dimension_weights=dict(evaluation.get("learned_dimension_weights") or {}),
        )
        return True
    except Exception as exc:
        logging.getLogger("oracle.shadow_feedback").warning(
            "Could not persist max-position shadow candidate for %s: %s",
            symbol, type(exc).__name__,
        )
        return False
