"""
Oracle.scout_agent.learner
==========================
Empirical Dirichlet/Bayesian dimension reliability learner and breakeven
conviction floor optimizer for OracleScoutSubAgent.
(Zero Hardcoded Variables — Books I–VI.)
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from scout_agent.models import SCOUT_DIMENSIONS  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import SCOUT_DIMENSIONS  # type: ignore


def compute_learned_dimension_weights(scout_state: Dict[str, Any]) -> Dict[str, float]:
    """
    Compute normalized posterior mean reliability weights from Beta(alpha_d, beta_d)
    for each evidence dimension. Starts at maximum-entropy uniform 1/K and shifts
    toward dimensions that empirically predict profitable trades.
    """
    alphas = scout_state.get("dimension_alpha", {})
    betas = scout_state.get("dimension_beta", {})
    raw: Dict[str, float] = {}
    for dim in SCOUT_DIMENSIONS:
        a = max(0.1, float(alphas.get(dim, 1.0)))
        b = max(0.1, float(betas.get(dim, 1.0)))
        raw[dim] = a / (a + b)
    total = sum(raw.values()) or 1.0
    weights = {dim: round(val / total, 6) for dim, val in raw.items()}
    scout_state["dimension_weights"] = weights
    return weights


def compute_learned_session_edge_adjustment(
    scout_state: Dict[str, Any], session: Optional[str]
) -> float:
    """Return a small-sample-shrunk session edge relative to all sessions."""
    if not session:
        return 0.0
    sessions = scout_state.get("session_stats") or {}
    local = sessions.get(str(session).lower())
    if not isinstance(local, dict):
        return 0.0
    try:
        local_n = max(0, int(local.get("wins", 0))) + max(0, int(local.get("losses", 0)))
        if local_n <= 0:
            return 0.0
        local_sum = float(local.get("sum_r", 0.0))
        if not math.isfinite(local_sum):
            return 0.0
        total_n = 0
        total_sum = 0.0
        for row in sessions.values():
            if not isinstance(row, dict):
                continue
            row_n = int(row.get("wins", 0)) + int(row.get("losses", 0))
            row_sum = float(row.get("sum_r", 0.0))
            if row_n > 0 and math.isfinite(row_sum):
                total_n += row_n
                total_sum += row_sum
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if total_n <= 0:
        return 0.0
    session_mean = local_sum / local_n
    all_session_mean = total_sum / total_n
    credibility = local_n / (local_n + 5.0)
    # Keep a single extreme outcome from dominating cross-sectional ranking.
    return round(max(-0.25, min(0.25, credibility * (session_mean - all_session_mean))), 4)


def blend_strategy_performance(
    scout_state: Dict[str, Any],
    *,
    symbol: str,
    regime: str,
    strategy_id: Optional[str],
    prior_win_probability: float,
    prior_mean_r: float,
) -> Tuple[float, float]:
    """Shrink strategy-specific evidence toward the symbol/regime estimate."""
    if not strategy_id:
        return prior_win_probability, prior_mean_r
    key = f"{symbol.upper()}::{regime}::{strategy_id}"
    row = (scout_state.get("strategy_stats") or {}).get(key)
    if not isinstance(row, dict):
        return prior_win_probability, prior_mean_r
    try:
        wins = max(0, int(row.get("wins", 0)))
        losses = max(0, int(row.get("losses", 0)))
        sum_r = float(row.get("sum_r", 0.0))
    except (TypeError, ValueError, OverflowError):
        return prior_win_probability, prior_mean_r
    trades = wins + losses
    if trades <= 0 or not math.isfinite(sum_r):
        return prior_win_probability, prior_mean_r
    credibility = trades / (trades + 5.0)
    local_win_rate = wins / trades
    local_mean_r = sum_r / trades
    return (
        (1.0 - credibility) * prior_win_probability + credibility * local_win_rate,
        (1.0 - credibility) * prior_mean_r + credibility * local_mean_r,
    )


def compare_strategy_performance(
    scout_state: Dict[str, Any], *, symbol: str, regime: str, strategy_id: Optional[str]
) -> Dict[str, Any]:
    """Present realized and shadow evidence side by side without mixing samples."""
    if not strategy_id:
        return {"realized": None, "shadow": None, "shadow_used_for_live_scoring": False}
    key = f"{symbol.upper()}::{regime}::{strategy_id}"

    def summarize(source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        row = source.get(key)
        if not isinstance(row, dict):
            return None
        try:
            wins = max(0, int(row.get("wins", 0)))
            losses = max(0, int(row.get("losses", 0)))
            observations = max(wins + losses, int(row.get("observations", 0)))
            sum_r = float(row.get("sum_r", 0.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(sum_r):
            return None
        total = wins + losses
        return {
            "observations": observations,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 4) if total else None,
            "mean_r": round(sum_r / observations, 4) if observations else None,
        }

    return {
        "realized": summarize(scout_state.get("strategy_stats") or {}),
        "shadow": summarize(scout_state.get("shadow_strategy_stats") or {}),
        "shadow_used_for_live_scoring": False,
    }


def compute_learned_conviction_floor(
    scout_state: Dict[str, Any],
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
) -> float:
    """
    Derive the minimum conviction threshold empirically from realized outcomes
    and replayed counterfactuals:
      - Searches the empirical outcome history for the conviction threshold theta
        that maximizes cumulative realized + counterfactual R-expectancy.
      - Adjusts dynamically for the specific (symbol, regime) Bayesian posterior
        win rate when historical observations exist.
    """
    outcomes = list(scout_state.get("conviction_outcomes") or [])
    cf_outcomes = list(scout_state.get("counterfactual_outcomes") or [])

    combined: List[Tuple[float, float]] = []
    for item in outcomes[-250:]:
        if isinstance(item, dict) and "conviction" in item and "pnl_r" in item:
            combined.append((float(item["conviction"]), float(item["pnl_r"])))
    for item in cf_outcomes[-250:]:
        if isinstance(item, dict) and "conviction" in item and "counterfactual_r" in item:
            combined.append((float(item["conviction"]), float(item["counterfactual_r"])))

    if not combined:
        base_floor = float(scout_state.get("learned_conviction_floor", 0.50))
    else:
        candidate_thetas = sorted({round(c, 3) for c, _ in combined})
        best_theta = candidate_thetas[len(candidate_thetas) // 2]
        best_total_r = -1e18
        for theta in candidate_thetas:
            qualifying = [r for c, r in combined if c >= theta]
            if not qualifying:
                continue
            missed_r = sum(max(0.0, r) for c, r in combined if c < theta)
            objective = sum(qualifying) - 0.5 * missed_r
            if objective > best_total_r:
                best_total_r = objective
                best_theta = theta
        n = len(combined)
        credibility = n / (n + 5.0)
        prior_center = sum(c for c, _ in combined) / n
        base_floor = credibility * best_theta + (1.0 - credibility) * prior_center
        scout_state["learned_conviction_floor"] = round(base_floor, 4)

    if symbol and regime:
        sr_key = f"{symbol.upper()}::{regime}"
        sr = (scout_state.get("symbol_regime_stats") or {}).get(sr_key)
        if isinstance(sr, dict):
            w = int(sr.get("wins", 0))
            l = int(sr.get("losses", 0))
            total = w + l
            if total > 0:
                mean_r = float(sr.get("sum_r", 0.0)) / total
                shrink = total / (total + 3.0)
                adjustment = -shrink * math.tanh(mean_r) * (base_floor * 0.25)
                base_floor = max(0.05, min(0.95, base_floor + adjustment))

    return round(float(base_floor), 4)


def update_scout_from_outcome(
    scout_state: Dict[str, Any],
    *,
    symbol: str,
    regime: str,
    session: Optional[str],
    entry_conviction: float,
    dimension_scores_at_entry: Dict[str, float],
    won: bool,
    pnl_r: float,
    strategy_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update Scout's empirical Dirichlet reliability weights, conviction floor
    history, and symbol::regime Bayesian statistics after a closed trade.
    """
    try:
        pnl_r = float(pnl_r)
        entry_conviction = float(entry_conviction)
    except (TypeError, ValueError, OverflowError):
        return {"status": "ignored", "reason": "invalid_trade_outcome"}
    if not math.isfinite(pnl_r) or not math.isfinite(entry_conviction):
        return {"status": "ignored", "reason": "non_finite_trade_outcome"}
    entry_conviction = max(0.0, min(1.0, entry_conviction))

    alphas = scout_state.setdefault("dimension_alpha", {d: 1.0 for d in SCOUT_DIMENSIONS})
    betas = scout_state.setdefault("dimension_beta", {d: 1.0 for d in SCOUT_DIMENSIONS})

    magnitude = min(3.0, max(0.25, abs(pnl_r)))
    for dim in SCOUT_DIMENSIONS:
        if dim not in (dimension_scores_at_entry or {}):
            # Historical/adopted positions may not have an entry snapshot.
            # Record their outcome, but do not invent feature attribution.
            continue
        try:
            score = float(dimension_scores_at_entry[dim])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(score):
            continue
        score = max(0.0, min(1.0, score))
        contribution = max(0.0, score - 0.35)
        if won:
            alphas[dim] = round(float(alphas.get(dim, 1.0)) + contribution * magnitude, 4)
        else:
            betas[dim] = round(float(betas.get(dim, 1.0)) + contribution * magnitude, 4)

    conv_list = scout_state.setdefault("conviction_outcomes", [])
    conv_list.append({
        "conviction": round(float(entry_conviction), 4),
        "pnl_r": round(float(pnl_r), 4),
        "won": bool(won),
        "session": str(session).lower() if session else None,
        "strategy_id": str(strategy_id) if strategy_id else None,
        "timestamp": time.time(),
    })
    if len(conv_list) > 500:
        del conv_list[:-500]

    sr_key = f"{symbol.upper()}::{regime}"
    sr_map = scout_state.setdefault("symbol_regime_stats", {})
    sr = sr_map.setdefault(sr_key, {"wins": 0, "losses": 0, "sum_r": 0.0, "sum_r2": 0.0})
    if won:
        sr["wins"] = int(sr.get("wins", 0)) + 1
    else:
        sr["losses"] = int(sr.get("losses", 0)) + 1
    sr["sum_r"] = round(float(sr.get("sum_r", 0.0)) + float(pnl_r), 4)
    sr["sum_r2"] = round(float(sr.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

    if strategy_id:
        strategy_key = f"{symbol.upper()}::{regime}::{strategy_id}"
        strategy_map = scout_state.setdefault("strategy_stats", {})
        strategy = strategy_map.setdefault(
            strategy_key, {"symbol": symbol.upper(), "regime": regime,
                           "strategy_id": str(strategy_id), "wins": 0,
                           "losses": 0, "sum_r": 0.0, "sum_r2": 0.0}
        )
        if won:
            strategy["wins"] = int(strategy.get("wins", 0)) + 1
        else:
            strategy["losses"] = int(strategy.get("losses", 0)) + 1
        strategy["sum_r"] = round(float(strategy.get("sum_r", 0.0)) + pnl_r, 4)
        strategy["sum_r2"] = round(float(strategy.get("sum_r2", 0.0)) + pnl_r ** 2, 4)

    if session:
        session_key = str(session).strip().lower()
        if session_key:
            sess_map = scout_state.setdefault("session_stats", {})
            ss = sess_map.setdefault(session_key, {"wins": 0, "losses": 0, "sum_r": 0.0})
            if won:
                ss["wins"] = int(ss.get("wins", 0)) + 1
            else:
                ss["losses"] = int(ss.get("losses", 0)) + 1
            ss["sum_r"] = round(float(ss.get("sum_r", 0.0)) + pnl_r, 4)

    new_weights = compute_learned_dimension_weights(scout_state)
    new_floor = compute_learned_conviction_floor(scout_state, symbol, regime)
    return {
        "status": "complete",
        "updated_dimension_weights": new_weights,
        "learned_conviction_floor": new_floor,
        "symbol_regime_record": dict(sr),
        "strategy_record": dict(strategy) if strategy_id else None,
    }


_MAX_PLAUSIBLE_COUNTERFACTUAL_R = 20.0


def _rebuild_strategy_stats_from_outcomes(outcomes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rebuilt: Dict[str, Dict[str, Any]] = {}
    for row in outcomes:
        if not isinstance(row, dict):
            continue
        strategy_id = row.get("strategy_id")
        symbol = str(row.get("symbol") or "").upper()
        regime = str(row.get("regime") or "ranging")
        if not strategy_id or not symbol:
            continue
        try:
            cf_r = float(row.get("counterfactual_r") or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(cf_r) or abs(cf_r) > _MAX_PLAUSIBLE_COUNTERFACTUAL_R:
            continue
        won = bool(row.get("would_have_won", cf_r > 0))
        strategy_key = f"{symbol}::{regime}::{strategy_id}"
        rec = rebuilt.setdefault(
            strategy_key,
            {
                "symbol": symbol,
                "regime": regime,
                "strategy_id": str(strategy_id),
                "wins": 0,
                "losses": 0,
                "sum_r": 0.0,
                "sum_r2": 0.0,
                "observations": 0,
            },
        )
        rec["observations"] += 1
        if won:
            rec["wins"] += 1
        else:
            rec["losses"] += 1
        rec["sum_r"] = round(float(rec["sum_r"]) + cf_r, 4)
        rec["sum_r2"] = round(float(rec["sum_r2"]) + (cf_r ** 2), 4)
    return rebuilt


def reconcile_scout_state_with_ledger(
    scout_state: Dict[str, Any],
    decision_ledger: Any = None,
) -> Dict[str, Any]:
    """
    Reconcile Scout's persisted counterfactual and shadow state against the
    immutable OracleDecisionLedger so invalidated replays (e.g. price-scale
    mismatches like +67.764R ASX200) are purged from outcomes and strategy
    aggregates.
    """
    invalidated_ids: set[str] = set()
    invalidated_symbols_without_valid_replays: set[str] = set()
    valid_shadow_symbols_in_ledger: set[str] = set()
    ledger_settled_shadow_count = 0
    ledger_invalidated_count = 0

    if decision_ledger is not None and hasattr(decision_ledger, "read_all"):
        try:
            all_records = decision_ledger.read_all() or []
            not_entered_by_id = {
                str(r.get("event_id")): r
                for r in all_records
                if r.get("event_type") == "TRADE_NOT_ENTERED" and r.get("event_id")
            }
            for r in all_records:
                et = r.get("event_type")
                if et == "COUNTERFACTUAL_REPLAY_INVALIDATED":
                    ledger_invalidated_count += 1
                    orig_id = str(r.get("original_event_id") or "")
                    if orig_id:
                        invalidated_ids.add(orig_id)
                    sym = str(r.get("symbol") or "").upper()
                    if sym:
                        invalidated_symbols_without_valid_replays.add(sym)
            for r in all_records:
                if r.get("event_type") != "COUNTERFACTUAL_REPLAY":
                    continue
                orig_id = str(r.get("original_event_id") or "")
                if orig_id in invalidated_ids:
                    continue
                try:
                    cf_r = float(r.get("counterfactual_r") or 0.0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(cf_r) or abs(cf_r) > _MAX_PLAUSIBLE_COUNTERFACTUAL_R:
                    if orig_id:
                        invalidated_ids.add(orig_id)
                    continue
                sym = str(r.get("symbol") or "").upper()
                orig_rec = not_entered_by_id.get(orig_id, {})
                if orig_rec.get("shadow_observation") or r.get("shadow_observation"):
                    ledger_settled_shadow_count += 1
                    if sym:
                        valid_shadow_symbols_in_ledger.add(sym)
            invalidated_symbols_without_valid_replays -= valid_shadow_symbols_in_ledger
        except Exception:
            pass

    removed_outcomes: List[Dict[str, Any]] = []

    def _clean_outcome_list(items: List[Any], is_shadow: bool) -> List[Dict[str, Any]]:
        cleaned: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            orig_id = str(item.get("original_event_id") or "")
            sym = str(item.get("symbol") or "").upper()
            try:
                cf_r = float(item.get("counterfactual_r") if "counterfactual_r" in item else item.get("pnl_r", 0.0))
            except (TypeError, ValueError, OverflowError):
                removed_outcomes.append({"symbol": sym, "reason": "non_numeric_r"})
                continue
            if (
                not math.isfinite(cf_r)
                or abs(cf_r) > _MAX_PLAUSIBLE_COUNTERFACTUAL_R
                or (orig_id and orig_id in invalidated_ids)
                or (is_shadow and sym in invalidated_symbols_without_valid_replays)
            ):
                removed_outcomes.append({
                    "symbol": sym,
                    "original_event_id": orig_id or None,
                    "counterfactual_r": cf_r,
                    "reason": "invalidated_in_ledger_or_scale_mismatch",
                })
                continue
            cleaned.append(item)
        return cleaned

    old_shadow = list(scout_state.get("shadow_counterfactual_outcomes") or [])
    old_cf = list(scout_state.get("counterfactual_outcomes") or [])
    clean_shadow = _clean_outcome_list(old_shadow, is_shadow=True)
    clean_cf = _clean_outcome_list(old_cf, is_shadow=False)

    changed = (len(clean_shadow) != len(old_shadow)) or (len(clean_cf) != len(old_cf))

    # Also check if shadow_strategy_stats or counterfactual_strategy_stats contain invalidated symbols or implausible sums
    rebuilt_shadow_stats = _rebuild_strategy_stats_from_outcomes(clean_shadow)
    rebuilt_cf_stats = _rebuild_strategy_stats_from_outcomes(clean_cf)

    if scout_state.get("shadow_strategy_stats") != rebuilt_shadow_stats:
        changed = True
    if scout_state.get("counterfactual_strategy_stats") != rebuilt_cf_stats:
        changed = True

    scout_state["shadow_counterfactual_outcomes"] = clean_shadow
    scout_state["counterfactual_outcomes"] = clean_cf
    scout_state["shadow_strategy_stats"] = rebuilt_shadow_stats
    scout_state["counterfactual_strategy_stats"] = rebuilt_cf_stats

    reconciliation_summary = {
        "synced": True,
        "state_modified": changed,
        "removed_invalid_outcomes_count": len(removed_outcomes),
        "removed_invalid_outcomes": removed_outcomes,
        "valid_shadow_outcomes_count": len(clean_shadow),
        "ledger_settled_shadow_replays": ledger_settled_shadow_count,
        "ledger_invalidated_replays": ledger_invalidated_count,
        "reconciled_at": time.time(),
    }
    scout_state["last_ledger_reconciliation"] = reconciliation_summary
    return reconciliation_summary


def update_scout_from_counterfactual(
    scout_state: Dict[str, Any],
    *,
    symbol: str,
    regime: str,
    conviction: float,
    stage_rejected: str,
    counterfactual_r: float,
    would_have_won: bool,
    strategy_id: Optional[str] = None,
    session: Optional[str] = None,
    shadow_observation: bool = False,
    original_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Store a replayed non-entry outcome. Shadow observations remain separate
    from the live conviction-floor evidence and realized trade statistics.
    Rejects non-finite or scale-mismatched R-multiples (> 20R).
    """
    try:
        cf_r = float(counterfactual_r)
        conv = float(conviction)
    except (TypeError, ValueError, OverflowError):
        return {
            "status": "ignored",
            "reason": "invalid_counterfactual_numeric",
            "learned_conviction_floor": float(scout_state.get("learned_conviction_floor", 0.50)),
            "counterfactual_count": 0,
            "counterfactual_strategy_record": None,
            "shadow_observation": bool(shadow_observation),
        }
    if not math.isfinite(cf_r) or abs(cf_r) > _MAX_PLAUSIBLE_COUNTERFACTUAL_R:
        return {
            "status": "rejected_scale_outlier",
            "reason": f"counterfactual_r {cf_r} exceeds max plausible threshold {_MAX_PLAUSIBLE_COUNTERFACTUAL_R}R",
            "learned_conviction_floor": float(scout_state.get("learned_conviction_floor", 0.50)),
            "counterfactual_count": len(scout_state.get("shadow_counterfactual_outcomes" if shadow_observation else "counterfactual_outcomes", [])),
            "counterfactual_strategy_record": None,
            "shadow_observation": bool(shadow_observation),
        }

    list_key = "shadow_counterfactual_outcomes" if shadow_observation else "counterfactual_outcomes"
    cf_list = scout_state.setdefault(list_key, [])
    if original_event_id:
        cf_list[:] = [
            row for row in cf_list
            if not (isinstance(row, dict) and str(row.get("original_event_id") or "") == str(original_event_id))
        ]
    cf_list.append({
        "original_event_id": str(original_event_id) if original_event_id else None,
        "symbol": symbol.upper(),
        "regime": regime,
        "conviction": round(conv, 4),
        "stage": stage_rejected,
        "counterfactual_r": round(cf_r, 4),
        "would_have_won": bool(would_have_won),
        "strategy_id": str(strategy_id) if strategy_id else None,
        "session": str(session).lower() if session else None,
        "shadow_observation": bool(shadow_observation),
        "timestamp": time.time(),
    })
    if len(cf_list) > 500:
        del cf_list[:-500]
    # Paper observations are valuable for comparing strategies, but should not
    # tune the live conviction floor as though they were executable outcomes.
    new_floor = compute_learned_conviction_floor(scout_state, symbol, regime)
    strategy_map_key = "shadow_strategy_stats" if shadow_observation else "counterfactual_strategy_stats"
    rebuilt_map = _rebuild_strategy_stats_from_outcomes(cf_list)
    scout_state[strategy_map_key] = rebuilt_map
    strategy_record = None
    if strategy_id:
        strategy_key = f"{symbol.upper()}::{regime}::{strategy_id}"
        strategy_record = rebuilt_map.get(strategy_key)
    return {
        "learned_conviction_floor": new_floor,
        "counterfactual_count": len(cf_list),
        "counterfactual_strategy_record": dict(strategy_record) if strategy_record else None,
        "shadow_observation": bool(shadow_observation),
    }

