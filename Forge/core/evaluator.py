"""
Forge V2 - Evaluator (Phase 3A)
================================
Pure, deterministic, read-only measurement component.

Principle:
  Evaluator measures. Benchmark contextualizes. Orchestration decides.

The Evaluator:
  - Iterates over DECLARED metric_specs from the contract (not result metrics).
  - Calculates direction-adjusted comparisons.
  - Produces an immutable EvaluationReport.
  - Does NOT mutate ModelRegistry, HypothesisQueue, Chronicle, or any ecosystem state.
  - Summarizes paired bootstrap draws supplied by the experiment adapter.
  - Does NOT generate resamples, choose metrics, or make promotion decisions.

Same contract + same result + same evaluator implementation → same metric
comparisons and statistical summaries. Report timestamps are audit metadata.
"""

import copy
import math
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentResult,
    MetricDirection,
)
from Forge.core.metrics import (
    PAIRED_BOOTSTRAP_METHODS,
    bootstrap_difference_summary,
    finite_metric_value,
)


# ==============================================================================
# 1. EVALUATION MODELS (Immutable)
# ==============================================================================

class MetricComparison(BaseModel):
    """
    Deterministic comparison of a single metric between baseline and treatment.
    This is a pure mathematical observation, not a judgment.
    Immutable once created.
    """
    model_config = ConfigDict(frozen=True)

    metric: str
    direction: MetricDirection

    baseline_value: Optional[float] = None
    treatment_value: Optional[float] = None

    # Calculated fields
    absolute_change: Optional[float] = None       # treatment - baseline
    relative_change: Optional[float] = None       # (treatment - baseline) / baseline
    improved: Optional[bool] = None               # direction-adjusted improvement

    # Handling missing or incomparable data
    comparable: bool = True                        # False if either value is missing
    error_message: Optional[str] = None            # Explanation if not comparable
    statistical_status: str = "unavailable"
    statistical_method: Optional[str] = None
    confidence_level: Optional[float] = None
    confidence_interval: Optional[tuple[float, float]] = None
    bootstrap_replicates: Optional[int] = None
    validation_sample_count: Optional[int] = None
    minimum_effect: Optional[float] = None
    confidently_better: Optional[bool] = None
    confidently_worse: Optional[bool] = None


class EvaluationReport(BaseModel):
    """
    The complete evaluation of a single ExperimentRun.
    Immutable scientific observation. Read-only output.
    Does not reference any ecosystem state.
    """
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    run_id: str
    result_id: str  # Links back to the ExperimentResult being evaluated

    # Contract and evaluator provenance
    contract_version: str = "2.0.0"
    evaluator_version: str = "3.0.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Per-metric comparisons
    comparisons: List[MetricComparison] = Field(default_factory=list)

    # Summary lists (derived from comparisons)
    improved_metrics: List[str] = Field(default_factory=list)
    degraded_metrics: List[str] = Field(default_factory=list)
    unchanged_metrics: List[str] = Field(default_factory=list)
    incomparable_metrics: List[str] = Field(default_factory=list)

    # Overall verdict (Optional — only set if aggregation policy is defined)
    overall_improved: Optional[bool] = None
    aggregation_policy: Optional[str] = None
    statistical_evidence_complete: bool = False
    statistically_improved_metrics: List[str] = Field(default_factory=list)
    statistically_degraded_metrics: List[str] = Field(default_factory=list)
    evaluation_design: dict = Field(default_factory=dict)


# ==============================================================================
# 2. EVALUATOR (Pure Measurement Component)
# ==============================================================================

class Evaluator:
    """
    Pure, deterministic, read-only measurement component.

    The Evaluator iterates over the DECLARED metric_specs from the contract,
    not over whatever metrics happen to appear in the result. This prevents
    an adapter from accidentally introducing an undocumented metric into the
    scientific evaluation.

    The Evaluator does NOT:
      - Modify ModelRegistry
      - Modify HypothesisQueue
      - Modify Chronicle
      - Promote or reject models
      - Deploy anything
      - Generate statistical resamples (adapters provide the paired draws)
      - Choose metrics or interpret direction outside the contract
      - Enforce thresholds
    """

    def evaluate(
        self,
        contract: ExperimentContract,
        result: ExperimentResult
    ) -> EvaluationReport:
        """
        Evaluates an ExperimentResult against the declared metric_specs
        in the ExperimentContract.

        Args:
            contract: The experiment definition containing metric_specs.
            result: The execution result containing baseline/treatment metrics.

        Returns:
            An immutable EvaluationReport.

        Raises:
            ValueError: If there are no declared metrics or result provenance/status
                does not match the supplied contract.
        """
        # Validate that the contract declares metrics to evaluate.
        metric_specs = contract.evaluation.metric_specs
        if result.experiment_id != contract.experiment_id:
            raise ValueError(
                "ExperimentResult does not belong to the supplied ExperimentContract."
            )
        if result.status.value != "COMPLETED":
            raise ValueError(
                f"Only COMPLETED results can be evaluated; received '{result.status.value}'."
            )
        if not result.run_id or not result.result_id:
            raise ValueError("Evaluation requires non-empty run_id and result_id provenance.")
        if not metric_specs:
            raise ValueError(
                f"Experiment {contract.experiment_id} has no declared metric_specs. "
                f"Cannot evaluate an experiment with nothing to measure."
            )

        # Extract baseline and treatment metrics from the result
        baseline_metrics = result.metrics.get("baseline", {})
        treatment_metrics = result.metrics.get("treatment", {})
        if not isinstance(baseline_metrics, dict):
            baseline_metrics = {}
        if not isinstance(treatment_metrics, dict):
            treatment_metrics = {}

        # Iterate over DECLARED metrics (not result metrics)
        comparisons: List[MetricComparison] = []
        improved_metrics: List[str] = []
        degraded_metrics: List[str] = []
        unchanged_metrics: List[str] = []
        incomparable_metrics: List[str] = []
        statistical_improved: List[str] = []
        statistical_degraded: List[str] = []
        bootstrap = result.metrics.get("bootstrap", {})
        if not isinstance(bootstrap, dict):
            bootstrap = {}
        bootstrap_baseline = bootstrap.get("baseline", {})
        bootstrap_treatment = bootstrap.get("treatment", {})
        if not isinstance(bootstrap_baseline, dict):
            bootstrap_baseline = {}
        if not isinstance(bootstrap_treatment, dict):
            bootstrap_treatment = {}
        declared_count = len(metric_specs)
        family_size = declared_count * contract.evaluation.multiple_comparison_family_size
        confidence_level = 1.0 - (1.0 - contract.evaluation.confidence_level) / max(family_size, 1)
        required_samples = max(
            contract.evaluation.minimum_samples,
            contract.evaluation.sample_requirement or 0,
        )
        sample_count = bootstrap.get("sample_count")
        replicates = bootstrap.get("replicates")
        bootstrap_method = bootstrap.get("method")
        valid_design = (
            bootstrap_method in PAIRED_BOOTSTRAP_METHODS
            and isinstance(sample_count, int) and not isinstance(sample_count, bool)
            and sample_count >= required_samples
            and isinstance(replicates, int) and not isinstance(replicates, bool)
            and replicates >= contract.evaluation.bootstrap_replicates
        )
        statistical_complete = valid_design

        for metric_name, spec in metric_specs.items():
            comparison = self._compare_metric(
                metric_name=metric_name,
                direction=spec.direction,
                baseline_value=baseline_metrics.get(metric_name),
                treatment_value=treatment_metrics.get(metric_name),
            )
            baseline_draws = bootstrap_baseline.get(metric_name, [])
            treatment_draws = bootstrap_treatment.get(metric_name, [])
            metric_bootstrap = bootstrap_difference_summary(
                baseline_draws,
                treatment_draws,
                confidence_level=confidence_level,
                direction=spec.direction.value,
                minimum_effect=spec.minimum_effect,
                method=bootstrap_method or "paired_percentile_bootstrap_v1",
            )
            metric_draws_valid = (
                isinstance(baseline_draws, list)
                and isinstance(treatment_draws, list)
                and isinstance(replicates, int)
                and len(baseline_draws) == replicates
                and len(treatment_draws) == replicates
            )
            if not valid_design or not metric_draws_valid or metric_bootstrap.get("status") != "assessed":
                statistical_complete = False
                comparison = comparison.model_copy(update={
                    "statistical_status": "unavailable",
                    "statistical_method": bootstrap_method if isinstance(bootstrap_method, str) else None,
                    "validation_sample_count": sample_count if isinstance(sample_count, int) else None,
                    "minimum_effect": spec.minimum_effect,
                })
            else:
                comparison = comparison.model_copy(update={
                    "statistical_status": "assessed",
                    "statistical_method": metric_bootstrap["method"],
                    "confidence_level": metric_bootstrap["confidence_level"],
                    "confidence_interval": metric_bootstrap["confidence_interval"],
                    "bootstrap_replicates": metric_bootstrap["replicates"],
                    "validation_sample_count": sample_count,
                    "minimum_effect": spec.minimum_effect,
                    "confidently_better": metric_bootstrap["confidently_better"],
                    "confidently_worse": metric_bootstrap["confidently_worse"],
                })
                if metric_bootstrap["confidently_better"]:
                    statistical_improved.append(metric_name)
                if metric_bootstrap["confidently_worse"]:
                    statistical_degraded.append(metric_name)
            comparisons.append(comparison)

            # Categorize
            if not comparison.comparable:
                incomparable_metrics.append(metric_name)
            elif comparison.absolute_change == 0:
                unchanged_metrics.append(metric_name)
            elif comparison.improved is True:
                improved_metrics.append(metric_name)
            elif comparison.improved is False:
                degraded_metrics.append(metric_name)
            else:
                # improved is None (shouldn't happen if comparable, but handle it)
                unchanged_metrics.append(metric_name)

        result_evidence = result.model_dump(mode="json").get("evidence", {})
        return EvaluationReport(
            experiment_id=contract.experiment_id,
            run_id=result.run_id,
            result_id=result.result_id,
            comparisons=comparisons,
            improved_metrics=improved_metrics,
            degraded_metrics=degraded_metrics,
            unchanged_metrics=unchanged_metrics,
            incomparable_metrics=incomparable_metrics,
            # overall_improved remains None — no aggregation policy in Phase 3A
            overall_improved=None,
            aggregation_policy=None,
            statistical_evidence_complete=statistical_complete,
            statistically_improved_metrics=statistical_improved,
            statistically_degraded_metrics=statistical_degraded,
            evaluation_design=copy.deepcopy({
                **{
                    key: result_evidence[key]
                    for key in (
                        "dataset_fingerprint", "random_seed", "environment_fingerprint",
                        "evaluation_partition", "evaluation_holdout",
                    )
                    if key in result_evidence
                },
                "statistical_design": {
                    "nominal_confidence_level": contract.evaluation.confidence_level,
                    "adjusted_confidence_level": confidence_level,
                    "declared_metric_count": declared_count,
                    "candidate_family_size": (
                        contract.evaluation.multiple_comparison_family_size
                    ),
                    "comparison_family_id": contract.evaluation.comparison_family_id,
                    "comparison_family_members": list(
                        contract.evaluation.comparison_family_members
                    ),
                    "simultaneous_comparison_count": family_size,
                    "minimum_sample_count": required_samples,
                    "validation_sample_count": (
                        sample_count if isinstance(sample_count, int)
                        and not isinstance(sample_count, bool) else None
                    ),
                    "bootstrap_method": (
                        bootstrap_method if isinstance(bootstrap_method, str) else None
                    ),
                    "bootstrap_replicates": (
                        replicates if isinstance(replicates, int)
                        and not isinstance(replicates, bool) else None
                    ),
                    "requested_bootstrap_replicates": (
                        contract.evaluation.bootstrap_replicates
                    ),
                },
            }),
        )

    def _compare_metric(
        self,
        metric_name: str,
        direction: MetricDirection,
        baseline_value: Optional[float],
        treatment_value: Optional[float],
    ) -> MetricComparison:
        """
        Compares a single metric between baseline and treatment.

        Handles edge cases:
          - Missing values (baseline or treatment is None)
          - Zero baseline (relative_change undefined but still comparable)
          - Identical values (unchanged)
        """
        # Missing or malformed values are explicitly incomparable. In
        # particular, NaN and infinity must never be categorized as evidence.
        raw_values = (("baseline", baseline_value), ("treatment", treatment_value))
        normalized = {label: finite_metric_value(value) for label, value in raw_values}
        invalid = []
        for label, value in raw_values:
            if value is None:
                invalid.append(f"missing {label} value")
            elif normalized[label] is None:
                invalid.append(f"{label} value is not a finite number")
        if invalid:
            return MetricComparison(
                metric=metric_name,
                direction=direction,
                baseline_value=normalized["baseline"],
                treatment_value=normalized["treatment"],
                comparable=False,
                error_message="; ".join(invalid),
            )

        baseline_value = normalized["baseline"]
        treatment_value = normalized["treatment"]
        assert baseline_value is not None and treatment_value is not None
        absolute_change = treatment_value - baseline_value
        if not math.isfinite(absolute_change):
            return MetricComparison(
                metric=metric_name,
                direction=direction,
                baseline_value=baseline_value,
                treatment_value=treatment_value,
                comparable=False,
                error_message="Metric change exceeds the finite numeric range.",
            )

        # Relative change (undefined when baseline is zero)
        relative_change: Optional[float] = None
        if baseline_value != 0:
            relative_change = absolute_change / baseline_value
            if not math.isfinite(relative_change):
                relative_change = None

        # Direction-adjusted improvement
        if direction == MetricDirection.HIGHER_IS_BETTER:
            improved = absolute_change > 0
        elif direction == MetricDirection.LOWER_IS_BETTER:
            improved = absolute_change < 0
        else:
            improved = None

        # Handle unchanged case (absolute_change == 0)
        if absolute_change == 0:
            improved = False  # Not improved, not degraded — unchanged

        return MetricComparison(
            metric=metric_name,
            direction=direction,
            baseline_value=baseline_value,
            treatment_value=treatment_value,
            absolute_change=absolute_change,
            relative_change=relative_change,
            improved=improved,
            comparable=True,
        )
