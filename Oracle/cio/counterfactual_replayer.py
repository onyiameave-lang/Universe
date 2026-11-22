"""
Oracle.cio.counterfactual_replayer
==================================
Replays unentered trades from OracleDecisionLedger against subsequent market
bars (or on-demand ForgeShadowRunner) and feeds counterfactual outcomes
(MISSED_WINNER vs AVOIDED_LOSS) into Scout, Portfolio, and Risk sub-agents.
"""
from __future__ import annotations

import gc
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from Oracle.execution.trade_manager import Action, Direction, MarketSnapshot, Position, TradeManager

_ORACLE_ROOT = Path(__file__).resolve().parents[1]
_MAX_REPLAY_PRICE_SCALE_RATIO = 10.0


def _timestamp_epoch(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _price_scale_mismatch(bar: Any, reference_price: float) -> Optional[Dict[str, float]]:
    """Reject replay bars whose quote scale is implausible for the setup."""
    if not isinstance(reference_price, (int, float)) or reference_price <= 0:
        return {"reference_price": float(reference_price or 0.0)}
    prices = {
        name: float(getattr(bar, name))
        for name in ("open", "high", "low", "close")
    }
    if any(not (price > 0 and price < float("inf")) for price in prices.values()):
        return prices
    lower = reference_price / _MAX_REPLAY_PRICE_SCALE_RATIO
    upper = reference_price * _MAX_REPLAY_PRICE_SCALE_RATIO
    if any(price < lower or price > upper for price in prices.values()):
        return prices
    return None


def replay_unentered_counterfactuals(
    *,
    oracle_agent: Any,
    scout_agent: Any,
    decision_ledger: Any,
    max_items: int = 25,
    max_records: Optional[int] = None,
    invoke_shadow_runner_on_demand: bool = False,
    shadow_only: bool = False,
    trade_manager: Optional[TradeManager] = None,
    current_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Replay unentered trades from `OracleDecisionLedger` against subsequent
    market price bars (or on-demand `ForgeShadowRunner`) and feed the
    counterfactual outcomes (`MISSED_WINNER` vs `AVOIDED_LOSS`) into Scout.
    """
    limit = max_records if max_records is not None else max_items
    pending = decision_ledger.unentered_trades_pending_replay(
        max_items=limit, shadow_only=shadow_only
    )
    replayed: List[Dict[str, Any]] = []
    missed_winners = 0
    avoided_losses = 0
    still_open = 0
    unavailable = 0
    series_cache: Dict[str, Any] = {}
    manager = trade_manager or TradeManager()
    current_snapshots = current_snapshots or {}

    for item in pending:
        sym = str(item.get("symbol") or "").upper()
        direction = str(item.get("direction") or "hold").lower()
        if direction not in ("long", "short", "buy", "sell"):
            continue
        entry = float(item.get("hypothetical_entry") or 0.0)
        stop = float(item.get("hypothetical_stop") or 0.0)
        target = float(item.get("hypothetical_target") or 0.0)
        risk_unit = abs(entry - stop)
        if (entry <= 0 or risk_unit <= 1e-12
                or (direction in ("long", "buy") and not stop < entry < target)
                or (direction in ("short", "sell") and not target < entry < stop)):
            unavailable += 1
            continue

        would_win = False
        cf_r = 0.0
        exit_price: Optional[float] = None
        exit_reason = ""
        bars_observed = int(item.get("_shadow_bars_observed") or 0)
        bars_observed_this_pass = 0
        ambiguous_bar = False
        data_source = ""
        position_state = dict(item.get("_shadow_position_state") or {})
        actions = list(position_state.get("management_actions") or [])
        mfe_r = float(position_state.get("max_favorable_excursion_r") or 0.0)
        mae_r = float(position_state.get("max_adverse_excursion_r") or 0.0)
        entry_confidence = float(item.get("entry_confidence") or 0.5)
        entry_regime = str(item.get("regime") or "ranging")
        entry_atr = float(item.get("atr") or 0.0) or None
        decision_ts = _timestamp_epoch(item.get("timestamp"))
        if decision_ts is None:
            unavailable += 1
            continue
        direction_enum = Direction.BUY if direction in ("long", "buy") else Direction.SELL
        try:
            pos = Position(
                symbol=sym, direction=direction_enum, entry_price=entry,
                initial_stop=stop, initial_target=target,
                entry_confidence=entry_confidence, entry_regime=entry_regime,
                entry_time=decision_ts, entry_atr=entry_atr,
                strategy_id=str(item.get("strategy_id") or "") or None,
            )
            if position_state:
                pos.current_stop = float(position_state.get("current_stop", stop))
                pos.highest_price = float(position_state.get("highest_price", entry))
                pos.lowest_price = float(position_state.get("lowest_price", entry))
                pos.last_price = float(position_state.get("last_price", entry))
                pos.last_regime = str(position_state.get("last_regime", entry_regime))
                pos.last_confidence = float(position_state.get("last_confidence", entry_confidence))
        except (TypeError, ValueError, OverflowError):
            unavailable += 1
            continue
        data_timeframe = "M15"
        try:
            if item.get("shadow_observation") and callable(
                getattr(oracle_agent, "_get_ctrader_replay_series", None)
            ):
                replay_from_ts = (
                    _timestamp_epoch(item.get("_shadow_last_bar_ts")) or decision_ts
                )
                md = oracle_agent._get_ctrader_replay_series(
                    sym, replay_from_ts, time.time()
                )
            else:
                if sym not in series_cache:
                    try:
                        series_cache[sym] = oracle_agent.data.get(sym, period="3mo", interval="15m")
                    except TypeError:
                        series_cache[sym] = oracle_agent.data.get(sym)
                md = series_cache[sym]
            series = md.get("series") if isinstance(md, dict) else None
            if not isinstance(md, dict) or md.get("status") != "complete" or series is None:
                unavailable += 1
                continue
            data_source = str(md.get("source") or getattr(series, "source", "unknown"))
            source_key = data_source.lower()
            if not (
                "broker" in source_key
                or source_key.startswith(("ctrader", "mt5"))
                or source_key == "unit_test"
            ):
                # Shadow outcomes must use broker-native bars (unit_test is
                # accepted only by deterministic regression tests).
                unavailable += 1
                continue
            last_bar_ts = _timestamp_epoch(item.get("_shadow_last_bar_ts"))
            replay_after_ts = max(decision_ts, last_bar_ts or decision_ts)
            replay_cutoff = time.time()
            future_bars = []
            for bar in getattr(series, "bars", []):
                bar_ts = _timestamp_epoch(getattr(bar, "ts", None))
                if (bar_ts is not None and replay_after_ts < bar_ts
                        and bar_ts + 15 * 60 <= replay_cutoff):
                    future_bars.append((bar_ts, bar))
            future_bars.sort(key=lambda pair: pair[0])
            live = current_snapshots.get(sym)
            if not future_bars and not live:
                if item.get("shadow_observation"):
                    state = {
                        "current_stop": pos.current_stop,
                        "highest_price": pos.highest_price,
                        "lowest_price": pos.lowest_price,
                        "last_price": pos.last_price,
                        "last_regime": pos.last_regime,
                        "last_confidence": pos.last_confidence,
                        "max_favorable_excursion_r": mfe_r,
                        "max_adverse_excursion_r": mae_r,
                        "management_actions": actions[-100:],
                    }
                    decision_ledger.record_shadow_replay_progress(
                        original_event_id=str(item.get("event_id")),
                        symbol=sym,
                        last_bar_ts=last_bar_ts or decision_ts,
                        bars_observed=bars_observed,
                        position_state=state,
                    )
                still_open += 1
                continue

            sign = 1.0 if direction in ("long", "buy") else -1.0
            invalid_bar = None
            for bar_ts, bar in future_bars:
                invalid_prices = _price_scale_mismatch(bar, entry)
                if invalid_prices is not None:
                    invalid_bar = (bar_ts, invalid_prices)
                    break
                bars_observed += 1
                bars_observed_this_pass += 1
                risk_unit = abs(entry - stop)
                favorable_price = float(bar.high) if sign > 0 else float(bar.low)
                adverse_price = float(bar.low) if sign > 0 else float(bar.high)
                mfe_r = max(mfe_r, (favorable_price - entry) * sign / risk_unit)
                mae_r = min(mae_r, (adverse_price - entry) * sign / risk_unit)
                if sign > 0:
                    pos.highest_price = max(pos.highest_price, float(bar.high))
                    pos.lowest_price = min(pos.lowest_price, float(bar.low))
                else:
                    pos.lowest_price = min(pos.lowest_price, float(bar.low))
                    pos.highest_price = max(pos.highest_price, float(bar.high))
                if sign > 0:
                    active_stop = pos.current_stop
                    hit_sl = float(bar.low) <= active_stop
                    hit_tp = target > entry and float(bar.high) >= target
                else:
                    active_stop = pos.current_stop
                    hit_sl = float(bar.high) >= active_stop
                    hit_tp = 0 < target < entry and float(bar.low) <= target
                if not hit_sl and not hit_tp:
                    decision = manager.evaluate(pos, MarketSnapshot(
                        price=float(bar.close), confidence=entry_confidence,
                        regime=entry_regime, atr=entry_atr,
                    ), at_time=bar_ts + 15 * 60)
                    if decision.action == Action.TIGHTEN_STOP:
                        actions.append({"at_unix": bar_ts + 15 * 60,
                                        "action": decision.action.value,
                                        "reason": decision.reason,
                                        "new_stop": decision.new_stop})
                    elif decision.action == Action.CLOSE:
                        exit_price = float(bar.close)
                        exit_reason = "trade_manager:" + decision.reason
                        break
                    continue
                # OHLC bars cannot reveal which boundary was first when both
                # were touched. Resolve ambiguity against the virtual position.
                ambiguous_bar = bool(hit_sl and hit_tp)
                if hit_sl:
                    o = float(bar.open)
                    exit_price = min(o, active_stop) if sign > 0 else max(o, active_stop)
                    exit_reason = "stop_loss" if not hit_tp else "ambiguous_bar_stop_first"
                else:
                    o = float(bar.open)
                    exit_price = max(o, target) if sign > 0 else min(o, target)
                    exit_reason = "take_profit"
                break

            if invalid_bar is not None:
                decision_ledger.record_counterfactual_replay_invalidated(
                    original_event_id=str(item.get("event_id")),
                    symbol=sym,
                    reason_code="replay_bar_price_scale_mismatch",
                    details={
                        "reference_entry": entry,
                        "max_scale_ratio": _MAX_REPLAY_PRICE_SCALE_RATIO,
                        "bar_timestamp": invalid_bar[0],
                        "bar_ohlc": invalid_bar[1],
                        "data_source": data_source,
                        "data_timeframe": data_timeframe,
                    },
                )
                unavailable += 1
                continue

            # Apply only the live snapshot received in this scan cycle after
            # historical bars. Past bars use entry context, avoiding future
            # news/regime/confidence leaking backward through the replay.
            if exit_price is None and live:
                try:
                    decision = manager.evaluate(pos, MarketSnapshot(
                        price=float(live["price"]),
                        confidence=float(live.get("confidence", entry_confidence)),
                        regime=str(live.get("regime", entry_regime)),
                        atr=float(live["atr"]) if live.get("atr") is not None else entry_atr,
                        news_impact=str(live.get("news_impact") or "none"),
                        social_risk=str(live.get("social_risk") or "none"),
                    ), at_time=float(live.get("observed_at_unix") or time.time()))
                    if decision.action == Action.TIGHTEN_STOP:
                        actions.append({"at_unix": float(live.get("observed_at_unix") or time.time()),
                                        "action": decision.action.value,
                                        "reason": decision.reason,
                                        "new_stop": decision.new_stop})
                    elif decision.action == Action.CLOSE:
                        exit_price = float(live["price"])
                        exit_reason = "trade_manager:" + decision.reason
                except (TypeError, ValueError, KeyError, OverflowError):
                    pass

            if exit_price is None:
                # Keep the virtual position open until its actual hypothetical
                # stop or target is reached. Current mark-to-market is not a
                # completed win/loss and must not train Scout as one.
                if item.get("shadow_observation") and (bars_observed_this_pass or live):
                    state = {
                        "current_stop": pos.current_stop,
                        "highest_price": pos.highest_price,
                        "lowest_price": pos.lowest_price,
                        "last_price": pos.last_price,
                        "last_regime": pos.last_regime,
                        "last_confidence": pos.last_confidence,
                        "max_favorable_excursion_r": mfe_r,
                        "max_adverse_excursion_r": mae_r,
                        "management_actions": actions[-100:],
                    }
                    decision_ledger.record_shadow_replay_progress(
                        original_event_id=str(item.get("event_id")),
                        symbol=sym,
                        last_bar_ts=(future_bars[-1][0] if future_bars else (last_bar_ts or decision_ts)),
                        bars_observed=bars_observed,
                        position_state=state,
                    )
                still_open += 1
                continue
            risk_unit = abs(entry - stop)
            cf_r = ((float(exit_price) - entry) * sign) / risk_unit
            would_win = cf_r > 0
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
            strategy_id=str(item.get("strategy_id") or "") or None,
            session=str(item.get("session") or "") or None,
            shadow_observation=bool(item.get("shadow_observation")),
            original_event_id=str(item.get("event_id") or "") or None,
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
            strategy_id=str(item.get("strategy_id") or "") or None,
            entry_session=str(item.get("session") or "") or None,
            exit_price=exit_price,
            exit_reason=exit_reason,
            bars_observed=bars_observed,
            data_source=data_source,
            data_timeframe=data_timeframe,
            pnl_basis="theoretical_stop_or_target_price; costs_and_slippage_not_modeled",
            ambiguous_bar=ambiguous_bar,
            final_stop_price=pos.current_stop,
            max_favorable_excursion_r=mfe_r,
            max_adverse_excursion_r=mae_r,
            management_actions=actions,
        )
        replayed.append(rec)

    reconcile_fn = getattr(scout_agent, "reconcile_with_ledger", None)
    reconciliation = reconcile_fn() if callable(reconcile_fn) else None
    updated_floor = scout_agent.learned_conviction_floor()
    return {
        "status": "complete",
        "replayed_count": len(replayed),
        "missed_winners": missed_winners,
        "avoided_losses": avoided_losses,
        "still_open": still_open,
        "data_unavailable": unavailable,
        "replays": replayed,
        "learned_conviction_floor": updated_floor,
        "updated_conviction_floor": updated_floor,
        "ledger_reconciliation": reconciliation,
    }

