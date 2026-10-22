"""Fit a Forge-owned multi-timeframe signal policy from development data.

The learner uses only completed-timeframe genome votes and the next M15
close-to-close return. Its coefficients are selected on a chronological
development validation slice and then refit on the full development prefix.
The experiment holdout is never passed to this module.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any, Dict, List


TIMEFRAMES = ("W1", "D1", "H4", "H1", "M45", "M30", "M15")
INTERVAL_MINUTES = {
    "W1": 10080, "D1": 1440, "H4": 240, "H1": 60,
    "M45": 45, "M30": 30, "M15": 15,
}
RIDGE_CANDIDATES = (0.001, 0.01, 0.1, 1.0, 10.0)


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _solve(matrix: List[List[float]], target: List[float]) -> List[float]:
    """Solve a small positive-definite system with pivoted elimination."""
    size = len(target)
    augmented = [list(matrix[row]) + [target[row]] for row in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-14:
            raise ValueError("timeframe policy training matrix is singular")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor:
                augmented[row] = [
                    augmented[row][idx] - factor * augmented[col][idx]
                    for idx in range(size + 1)
                ]
    return [augmented[row][-1] for row in range(size)]


def _fit(rows: List[List[float]], labels: List[float], ridge: float) -> List[float]:
    columns = len(rows[0])
    gram = [[0.0] * columns for _ in range(columns)]
    cross = [0.0] * columns
    for row, label in zip(rows, labels):
        for i, value_i in enumerate(row):
            cross[i] += value_i * label
            for j, value_j in enumerate(row):
                gram[i][j] += value_i * value_j
    # Column zero is the intercept; do not shrink it.
    for idx in range(1, columns):
        gram[idx][idx] += ridge
    return _solve(gram, cross)


def _predict(coefficients: List[float], row: List[float]) -> float:
    return sum(coefficient * value for coefficient, value in zip(coefficients, row))


def _percentile90(values: List[float]) -> float:
    ordered = sorted(abs(value) for value in values if math.isfinite(value))
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(0.9 * (len(ordered) - 1)))]


def _risk_adjusted_validation_score(
    coefficients: List[float], rows: List[List[float]], labels: List[float],
    scale_bps: float, entry_threshold: float, round_trip_cost_bps: float,
) -> float:
    returns = []
    for row, label in zip(rows, labels):
        prediction = _predict(coefficients, row)
        normalized = max(-1.0, min(1.0, prediction / scale_bps)) if scale_bps > 0 else 0.0
        if abs(normalized) >= entry_threshold:
            direction = 1.0 if normalized > 0 else -1.0
            returns.append(direction * label - round_trip_cost_bps)
    if len(returns) < 30:
        return float("-inf")
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    deviation = math.sqrt(variance)
    if deviation <= 0:
        return mean if mean > 0 else float("-inf")
    return mean / deviation


def fit_timeframe_policy(
    genome: Any,
    timeframe_series: Dict[str, Any],
    development_m15_bars: int,
    entry_threshold: float,
    commission_bps_per_side: float,
    slippage_bps_per_side: float,
    spread_bps_full: float,
) -> Dict[str, Any]:
    """Learn timeframe coefficients using only the declared development prefix."""
    if set(timeframe_series) != set(TIMEFRAMES):
        raise ValueError("timeframe learner requires all seven timeframe datasets")
    m15_bars = timeframe_series["M15"].bars
    if development_m15_bars < 100 or development_m15_bars >= len(m15_bars):
        raise ValueError("timeframe learner needs at least 100 development M15 bars")

    end_times: Dict[str, List[float]] = {}
    for timeframe in TIMEFRAMES:
        interval = INTERVAL_MINUTES[timeframe] * 60
        end_times[timeframe] = [
            _timestamp(bar.ts).timestamp() + interval
            for bar in timeframe_series[timeframe].bars
        ]

    module_periods = []
    for module_name in ("market_regime", "trend", "momentum", "volatility", "entry",
                        "exit", "risk", "position", "trade_management"):
        params = getattr(getattr(genome, module_name, None), "params", {}) or {}
        if isinstance(params, dict):
            module_periods.extend(
                value for key, value in params.items()
                if key.lower() in {"period", "fast", "slow", "signal", "k_period", "d_period"}
                and isinstance(value, int) and not isinstance(value, bool) and value > 0
            )
    context_window = max(100, min(2000, max(module_periods or [50]) + 50))
    cached: Dict[str, tuple[int, float]] = {}
    rows: List[List[float]] = []
    labels: List[float] = []

    class VoteSeries:
        def __init__(self, bars):
            self.closes = [bar.close for bar in bars]
            self.highs = [bar.high for bar in bars]
            self.lows = [bar.low for bar in bars]

    for index in range(development_m15_bars - 1):
        bar = m15_bars[index]
        signal_close = end_times["M15"][index]
        feature_row = [1.0]
        ready = True
        for timeframe in TIMEFRAMES:
            series = timeframe_series[timeframe]
            end_index = index + 1 if timeframe == "M15" else bisect_right(end_times[timeframe], signal_close)
            previous = cached.get(timeframe)
            if previous is not None and previous[0] == end_index:
                vote = previous[1]
            else:
                if end_index < 40:
                    ready = False
                    break
                context_bars = series.bars[max(0, end_index - context_window):end_index]
                vote = float(genome.vote(VoteSeries(context_bars)))
                if not math.isfinite(vote):
                    raise ValueError(f"non-finite {timeframe} vote during policy training")
                cached[timeframe] = (end_index, vote)
            feature_row.append(vote)
        if not ready:
            continue
        next_close = float(m15_bars[index + 1].close)
        current_close = float(bar.close)
        if current_close <= 0 or not math.isfinite(next_close):
            continue
        rows.append(feature_row)
        labels.append((next_close / current_close - 1.0) * 10000.0)

    validation_start = int(len(rows) * 0.7)
    if validation_start < 70 or len(rows) - validation_start < 30:
        raise ValueError("timeframe learner development split is too small")
    training_rows, training_labels = rows[:validation_start], labels[:validation_start]
    validation_rows, validation_labels = rows[validation_start:], labels[validation_start:]
    round_trip_cost = 2.0 * float(commission_bps_per_side) + 2.0 * float(slippage_bps_per_side) + float(spread_bps_full)

    candidates = []
    for ridge in RIDGE_CANDIDATES:
        coefficients = _fit(training_rows, training_labels, ridge)
        training_scale = _percentile90([_predict(coefficients, row) for row in training_rows])
        score = _risk_adjusted_validation_score(
            coefficients, validation_rows, validation_labels,
            training_scale, float(entry_threshold), round_trip_cost,
        )
        candidates.append((score, -ridge, ridge))
    best_score, _, best_ridge = max(candidates)
    if not math.isfinite(best_score):
        raise ValueError("no timeframe policy met the minimum development-validation activity")

    coefficients = _fit(rows, labels, best_ridge)
    scale_bps = _percentile90([_predict(coefficients, row) for row in rows])
    if not math.isfinite(scale_bps) or scale_bps <= 0:
        raise ValueError("learned timeframe policy has no usable signal scale")
    return {
        "schema": "forge.oracle.learned_timeframe_policy.v1",
        "method": "ridge_return_prediction_with_chronological_development_validation",
        "target": "next_completed_M15_close_return_bps",
        "training_rows": len(rows),
        "validation_rows": len(validation_rows),
        "validation_risk_adjusted_score": best_score,
        "ridge_alpha": best_ridge,
        "intercept_bps": coefficients[0],
        "weights": {timeframe: coefficients[index + 1] for index, timeframe in enumerate(TIMEFRAMES)},
        "signal_scale_bps": scale_bps,
        "entry_threshold": float(entry_threshold),
        "round_trip_cost_bps": round_trip_cost,
        "fit_data_end_index_exclusive": development_m15_bars,
        "holdout_used_for_fit_or_selection": False,
    }


def score_timeframe_policy(policy: Dict[str, Any], votes: Dict[str, float]) -> float:
    """Convert the fitted return estimate to the bounded score Oracle expects."""
    prediction = float(policy["intercept_bps"])
    weights = policy["weights"]
    for timeframe in TIMEFRAMES:
        prediction += float(weights[timeframe]) * float(votes[timeframe])
    normalized = prediction / float(policy["signal_scale_bps"])
    return max(-1.0, min(1.0, normalized))
