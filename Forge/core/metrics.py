"""Numeric validation shared by Forge's measurement components."""

import math
import random
from typing import Any, Optional

PAIRED_BOOTSTRAP_METHODS = frozenset({
    "paired_percentile_bootstrap_v1",
    "paired_circular_block_bootstrap_v1",
})


def finite_metric_value(value: Any) -> Optional[float]:
    """Return finite numeric evidence as float; reject bools and coercions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def bootstrap_metric_samples(
    y_true: Any,
    y_pred: Any,
    task_type: str,
    seed: int,
    replicates: int = 1000,
) -> dict[str, list[float]]:
    """Create deterministic metric draws over held-out rows.

    Baseline and treatment calls use the same seed and row order, producing
    paired bootstrap samples. Only aggregate metrics are returned; row-level
    labels and predictions are not included in the persisted evidence.
    """
    truth = list(y_true)
    predictions = list(y_pred)
    if len(truth) != len(predictions) or not truth:
        raise ValueError("bootstrap inputs must have matching, non-empty rows")
    if (
        isinstance(replicates, bool) or not isinstance(replicates, int)
        or not 200 <= replicates <= 20000
    ):
        raise ValueError("bootstrap replicates must be an integer from 200 through 20000")
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        return _numpy_bootstrap_metric_samples(
            np, truth, predictions, task_type, seed, replicates
        )
    rng = random.Random(seed)
    result: dict[str, list[float]] = {}
    row_count = len(truth)
    for _ in range(replicates):
        indices = [rng.randrange(row_count) for _ in range(row_count)]
        sample_truth = [truth[index] for index in indices]
        sample_predictions = [predictions[index] for index in indices]
        if task_type == "classification":
            result.setdefault("accuracy", []).append(
                sum(actual == predicted for actual, predicted in zip(sample_truth, sample_predictions)) / row_count
            )
            total = 0.0
            weighted_precision = weighted_recall = weighted_f1 = 0.0
            for label in set(sample_truth):
                support = sum(value == label for value in sample_truth)
                true_positive = sum(
                    actual == label and predicted == label
                    for actual, predicted in zip(sample_truth, sample_predictions)
                )
                predicted_count = sum(value == label for value in sample_predictions)
                precision = true_positive / predicted_count if predicted_count else 0.0
                recall = true_positive / support if support else 0.0
                f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
                total += support
                weighted_precision += support * precision
                weighted_recall += support * recall
                weighted_f1 += support * f1
            result.setdefault("precision", []).append(weighted_precision / total)
            result.setdefault("recall", []).append(weighted_recall / total)
            result.setdefault("f1", []).append(weighted_f1 / total)
        elif task_type == "regression":
            true_values = [float(value) for value in sample_truth]
            pred_values = [float(value) for value in sample_predictions]
            mean_truth = sum(true_values) / row_count
            ss_total = sum((value - mean_truth) ** 2 for value in true_values)
            ss_error = sum((actual - predicted) ** 2 for actual, predicted in zip(true_values, pred_values))
            result.setdefault("r2", []).append(1 - ss_error / (ss_total or 1.0))
            result.setdefault("mae", []).append(
                sum(abs(actual - predicted) for actual, predicted in zip(true_values, pred_values)) / row_count
            )
            result.setdefault("rmse", []).append(math.sqrt(ss_error / row_count))
        else:
            raise ValueError(f"unsupported task type for bootstrap metrics: {task_type}")
    if any(not math.isfinite(value) for draws in result.values() for value in draws):
        raise ValueError("bootstrap produced a non-finite metric draw")
    return result


def summarize_bar_return_metrics(returns: Any) -> dict[str, float]:
    """Calculate period-based metrics from one finite market-bar return path."""
    values = [finite_metric_value(value) for value in returns]
    if not values or any(value is None for value in values):
        raise ValueError("bar returns must be a non-empty finite numeric sequence")
    values = [float(value) for value in values]
    if any(value <= -1 for value in values):
        raise ValueError("bar returns must be greater than -100 percent")
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    positive = sum(value > 0 for value in values)
    gross_gain = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    metrics = {
        "period_total_return": equity - 1.0,
        "period_sharpe": mean / math.sqrt(variance) if variance else 0.0,
        "period_max_drawdown": max_drawdown,
        "period_win_rate": positive / len(values),
        "period_expectancy": mean,
    }
    if gross_loss > 0:
        metrics["period_profit_factor"] = gross_gain / gross_loss
    if any(not math.isfinite(value) for value in metrics.values()):
        raise ValueError("bar-return summary produced non-finite metrics")
    return metrics


def bootstrap_paired_bar_return_metrics(
    baseline_returns: Any,
    treatment_returns: Any,
    seed: int,
    replicates: int = 1000,
    block_length: Optional[int] = None,
) -> dict[str, Any]:
    """Create deterministic paired circular-block draws for market-bar returns.

    Both strategies are resampled with identical circular blocks so each draw
    preserves the same market periods and local serial dependence. The metric
    names include ``period`` to distinguish bar-based statistics from Oracle's
    existing trade-level statistics. Profit-factor draws are omitted if any
    replicate has no negative period return, rather than inventing an infinite
    finite value.
    """
    baseline = [finite_metric_value(value) for value in baseline_returns]
    treatment = [finite_metric_value(value) for value in treatment_returns]
    if (
        not baseline or len(baseline) != len(treatment)
        or any(value is None for value in baseline + treatment)
    ):
        raise ValueError("paired bar returns must be matching non-empty finite numeric sequences")
    baseline = [float(value) for value in baseline]
    treatment = [float(value) for value in treatment]
    if any(value <= -1 for value in baseline + treatment):
        raise ValueError("bar returns must be greater than -100 percent")
    if len(baseline) < 30:
        raise ValueError("paired bar bootstrap requires at least 30 observations")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer from 0 through 4294967295")
    if isinstance(replicates, bool) or not isinstance(replicates, int) or not 200 <= replicates <= 20000:
        raise ValueError("replicates must be an integer from 200 through 20000")
    if block_length is None:
        block_length = max(2, round(math.sqrt(len(baseline))))
    if (
        isinstance(block_length, bool) or not isinstance(block_length, int)
        or not 2 <= block_length <= len(baseline)
    ):
        raise ValueError("block_length must be an integer from 2 through the sample count")

    rng = random.Random(seed)
    sample_count = len(baseline)
    names = (
        "period_total_return", "period_sharpe", "period_max_drawdown",
        "period_win_rate", "period_profit_factor", "period_expectancy",
    )
    draws = {"baseline": {name: [] for name in names},
             "treatment": {name: [] for name in names}}
    profit_factor_complete = True
    for _ in range(replicates):
        indices = []
        while len(indices) < sample_count:
            start = rng.randrange(sample_count)
            indices.extend((start + offset) % sample_count for offset in range(block_length))
        indices = indices[:sample_count]
        base_metrics = summarize_bar_return_metrics([baseline[index] for index in indices])
        treatment_metrics = summarize_bar_return_metrics([treatment[index] for index in indices])
        for name in names:
            if name == "period_profit_factor":
                if name not in base_metrics or name not in treatment_metrics:
                    profit_factor_complete = False
                    continue
            draws["baseline"][name].append(base_metrics[name])
            draws["treatment"][name].append(treatment_metrics[name])

    if not profit_factor_complete:
        draws["baseline"].pop("period_profit_factor", None)
        draws["treatment"].pop("period_profit_factor", None)
    for side in ("baseline", "treatment"):
        for name, values in list(draws[side].items()):
            if len(values) != replicates or any(not math.isfinite(value) for value in values):
                raise ValueError(f"bootstrap produced incomplete or non-finite {side} {name} draws")
    return {
        "method": "paired_circular_block_bootstrap_v1",
        "replicates": replicates,
        "sample_count": sample_count,
        "block_length": block_length,
        "random_seed": seed,
        **draws,
    }


def _numpy_bootstrap_metric_samples(np, truth, predictions, task_type, seed, replicates):
    """Batch vectorized resampling to keep large validation sets practical."""
    array_dtype = object if task_type == "classification" else None
    truth_array = np.asarray(truth, dtype=array_dtype)
    prediction_array = np.asarray(predictions, dtype=array_dtype)
    row_count = len(truth)
    rng = np.random.default_rng(seed)
    values: dict[str, list[float]] = {}
    batch_size = max(1, min(16, replicates))
    for offset in range(0, replicates, batch_size):
        current_batch = min(batch_size, replicates - offset)
        indices = rng.integers(0, row_count, size=(current_batch, row_count))
        sampled_truth = truth_array[indices]
        sampled_predictions = prediction_array[indices]
        if task_type == "classification":
            equal = sampled_truth == sampled_predictions
            values.setdefault("accuracy", []).extend(np.mean(equal, axis=1).tolist())
            weighted_precision = np.zeros(current_batch, dtype=float)
            weighted_recall = np.zeros(current_batch, dtype=float)
            weighted_f1 = np.zeros(current_batch, dtype=float)
            for label in sorted(set(truth), key=str):
                support = np.sum(sampled_truth == label, axis=1)
                true_positive = np.sum(
                    (sampled_truth == label) & (sampled_predictions == label), axis=1
                )
                predicted_count = np.sum(sampled_predictions == label, axis=1)
                precision = np.divide(
                    true_positive, predicted_count,
                    out=np.zeros(current_batch, dtype=float), where=predicted_count != 0,
                )
                recall = np.divide(
                    true_positive, support,
                    out=np.zeros(current_batch, dtype=float), where=support != 0,
                )
                f1 = np.divide(
                    2 * precision * recall, precision + recall,
                    out=np.zeros(current_batch, dtype=float), where=(precision + recall) != 0,
                )
                weighted_precision += support * precision
                weighted_recall += support * recall
                weighted_f1 += support * f1
            values.setdefault("precision", []).extend((weighted_precision / row_count).tolist())
            values.setdefault("recall", []).extend((weighted_recall / row_count).tolist())
            values.setdefault("f1", []).extend((weighted_f1 / row_count).tolist())
        elif task_type == "regression":
            sampled_truth = sampled_truth.astype(float)
            sampled_predictions = sampled_predictions.astype(float)
            mean_truth = np.mean(sampled_truth, axis=1)
            ss_total = np.sum((sampled_truth - mean_truth[:, None]) ** 2, axis=1)
            ss_error = np.sum((sampled_truth - sampled_predictions) ** 2, axis=1)
            r2 = 1 - ss_error / np.where(ss_total == 0, 1.0, ss_total)
            values.setdefault("r2", []).extend(r2.tolist())
            values.setdefault("mae", []).extend(
                np.mean(np.abs(sampled_truth - sampled_predictions), axis=1).tolist()
            )
            values.setdefault("rmse", []).extend(np.sqrt(ss_error / row_count).tolist())
        else:
            raise ValueError(f"unsupported task type for bootstrap metrics: {task_type}")
    result = {name: [float(value) for value in draws] for name, draws in values.items()}
    if any(not math.isfinite(value) for draws in result.values() for value in draws):
        raise ValueError("bootstrap produced a non-finite metric draw")
    return result


def bootstrap_difference_summary(
    baseline_draws: Any,
    treatment_draws: Any,
    confidence_level: float,
    direction: str,
    minimum_effect: float = 0.0,
    method: str = "paired_percentile_bootstrap_v1",
) -> dict[str, Any]:
    """Summarize paired bootstrap differences with a percentile interval."""
    baseline = [finite_metric_value(value) for value in baseline_draws]
    treatment = [finite_metric_value(value) for value in treatment_draws]
    normalized_confidence = finite_metric_value(confidence_level)
    normalized_effect = finite_metric_value(minimum_effect)
    if (
        not baseline or len(baseline) != len(treatment)
        or any(value is None for value in baseline + treatment)
        or normalized_confidence is None or not 0.0 < normalized_confidence < 1.0
        or direction not in {"higher_is_better", "lower_is_better"}
        or not isinstance(method, str) or not method.strip()
        or normalized_effect is None or normalized_effect < 0
    ):
        return {"status": "unavailable"}
    confidence_level = normalized_confidence
    minimum_effect = normalized_effect
    differences = sorted(
        float(after) - float(before)
        for before, after in zip(baseline, treatment)
    )
    if any(not math.isfinite(value) for value in differences):
        return {"status": "unavailable"}

    def percentile(probability: float) -> float:
        position = probability * (len(differences) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return differences[lower]
        return differences[lower] + (differences[upper] - differences[lower]) * (position - lower)

    tail = (1.0 - confidence_level) / 2.0
    lower, upper = percentile(tail), percentile(1.0 - tail)
    if direction == "higher_is_better":
        better = lower > minimum_effect
        worse = upper < -minimum_effect
    else:
        better = upper < -minimum_effect
        worse = lower > minimum_effect
    return {
        "status": "assessed",
        "method": method,
        "replicates": len(differences),
        "confidence_level": confidence_level,
        "confidence_interval": (lower, upper),
        "minimum_effect": minimum_effect,
        "confidently_better": better,
        "confidently_worse": worse,
    }
