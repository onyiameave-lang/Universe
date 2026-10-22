"""
Forge V2 - Benchmark (Phase 3B)
================================
Contextual comparison of an experiment against an external reference.

Evaluator measures. Benchmark contextualizes. Orchestration decides.
"""

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Forge.core.evaluator import EvaluationReport
from Forge.core.experimentation import ExperimentResult, MetricDirection
from Forge.core.metrics import (
    PAIRED_BOOTSTRAP_METHODS,
    bootstrap_difference_summary,
    finite_metric_value,
)


class BenchmarkStatus(str, Enum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class BenchmarkContext(BaseModel):
    """Passive, provenance-bearing reference data supplied by an orchestrator."""

    reference_id: str
    reference_type: str
    reference_metrics: Dict[str, float]
    reference_bootstrap: Dict[str, List[float]] = Field(default_factory=dict)
    bootstrap_evidence: Dict[str, Any] = Field(default_factory=dict)
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_by: str = "orchestrator"
    provenance: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("reference_metrics", mode="before")
    @classmethod
    def _validate_reference_metrics(cls, value):
        if not isinstance(value, dict):
            raise ValueError("reference_metrics must be a mapping of metric names to finite numbers")
        normalized = {}
        for metric_name, metric_value in value.items():
            finite_value = finite_metric_value(metric_value)
            if finite_value is None:
                raise ValueError(
                    f"Reference metric '{metric_name}' must be a finite numeric value."
                )
            normalized[metric_name] = finite_value
        return normalized

    @field_validator("reference_bootstrap", mode="before")
    @classmethod
    def _validate_reference_bootstrap(cls, value):
        if not isinstance(value, dict):
            raise ValueError("reference_bootstrap must map metric names to finite numeric draws")
        normalized = {}
        for metric_name, draws in value.items():
            if not isinstance(metric_name, str) or not metric_name.strip() or not isinstance(draws, list):
                raise ValueError("reference_bootstrap entries require a metric name and draw list")
            finite_draws = [finite_metric_value(draw) for draw in draws]
            if len(finite_draws) < 200 or any(draw is None for draw in finite_draws):
                raise ValueError("reference bootstrap draws must contain at least 200 finite values")
            normalized[metric_name] = finite_draws
        return normalized


class ReferenceComparison(BaseModel):
    """Immutable factual comparison of a treatment metric to its reference."""

    model_config = ConfigDict(frozen=True)

    metric: str
    direction: MetricDirection
    reference_value: Optional[float] = None
    treatment_value: Optional[float] = None
    absolute_change: Optional[float] = None
    relative_change: Optional[float] = None
    better_than_reference: Optional[bool] = None
    comparable: bool = True
    error_message: Optional[str] = None
    statistical_status: str = "unavailable"
    confidence_level: Optional[float] = None
    confidence_interval: Optional[tuple[float, float]] = None
    bootstrap_replicates: Optional[int] = None
    confidently_better: Optional[bool] = None
    confidently_worse: Optional[bool] = None


class BenchmarkReport(BaseModel):
    """Immutable factual benchmark output; it makes no ecosystem decisions."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    run_id: str
    result_id: str
    reference_id: Optional[str] = None
    reference_type: Optional[str] = None
    status: BenchmarkStatus
    reference_available: bool
    error_message: Optional[str] = None
    comparisons: List[ReferenceComparison] = Field(default_factory=list)
    metrics_better: List[str] = Field(default_factory=list)
    metrics_worse: List[str] = Field(default_factory=list)
    metrics_unchanged: List[str] = Field(default_factory=list)
    metrics_incomparable: List[str] = Field(default_factory=list)
    benchmark_version: str = "3.0.0"
    statistical_evidence_complete: bool = False
    statistically_better: List[str] = Field(default_factory=list)
    statistically_worse: List[str] = Field(default_factory=list)
    candidate_bootstrap: Dict[str, List[float]] = Field(default_factory=dict)
    bootstrap_evidence: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Benchmark:
    """Pure comparison of evaluated treatment metrics against a supplied reference."""

    def evaluate(
        self,
        evaluation_report: EvaluationReport,
        result: ExperimentResult,
        context: Optional[BenchmarkContext],
    ) -> BenchmarkReport:
        identity_errors = []
        if evaluation_report.experiment_id != result.experiment_id:
            identity_errors.append("experiment_id mismatch")
        if evaluation_report.run_id != result.run_id:
            identity_errors.append("run_id mismatch")
        if evaluation_report.result_id != result.result_id:
            identity_errors.append("result_id mismatch")
        if result.status.value != "COMPLETED":
            identity_errors.append(f"result status is {result.status.value}, not COMPLETED")
        if identity_errors:
            return BenchmarkReport(
                experiment_id=evaluation_report.experiment_id,
                run_id=evaluation_report.run_id,
                result_id=evaluation_report.result_id,
                status=BenchmarkStatus.FAILED,
                reference_available=False,
                error_message="Cannot benchmark inconsistent evidence: "
                + "; ".join(identity_errors),
            )

        if context is None:
            return BenchmarkReport(
                experiment_id=evaluation_report.experiment_id,
                run_id=evaluation_report.run_id,
                result_id=evaluation_report.result_id,
                status=BenchmarkStatus.UNAVAILABLE,
                reference_available=False,
                error_message="No BenchmarkContext provided.",
            )

        if not context.reference_metrics:
            return BenchmarkReport(
                experiment_id=evaluation_report.experiment_id,
                run_id=evaluation_report.run_id,
                result_id=evaluation_report.result_id,
                reference_id=context.reference_id,
                reference_type=context.reference_type,
                status=BenchmarkStatus.UNAVAILABLE,
                reference_available=False,
                error_message="BenchmarkContext provided but reference_metrics is empty.",
            )

        treatment_metrics = result.metrics.get("treatment", {})
        if not isinstance(treatment_metrics, dict):
            treatment_metrics = {}
        comparisons: List[ReferenceComparison] = []
        metrics_better: List[str] = []
        metrics_worse: List[str] = []
        metrics_unchanged: List[str] = []
        metrics_incomparable: List[str] = []
        statistical_better: List[str] = []
        statistical_worse: List[str] = []
        statistical_complete = True
        reference_evidence = context.bootstrap_evidence
        result_bootstrap = result.metrics.get("bootstrap", {})
        if not isinstance(result_bootstrap, dict):
            result_bootstrap = {}
        treatment_draws = result_bootstrap.get("treatment", {})
        if not isinstance(treatment_draws, dict):
            treatment_draws = {}
        current_evidence = {
            "method": result_bootstrap.get("method"),
            "replicates": result_bootstrap.get("replicates"),
            "sample_count": result_bootstrap.get("sample_count"),
            "random_seed": result.evidence.get("random_seed"),
            "dataset_fingerprint": result.evidence.get("dataset_fingerprint"),
            "evaluation_partition": result.evidence.get("evaluation_partition"),
        }
        metadata_valid = (
            current_evidence["method"] in PAIRED_BOOTSTRAP_METHODS
            and isinstance(current_evidence["replicates"], int)
            and not isinstance(current_evidence["replicates"], bool)
            and 200 <= current_evidence["replicates"] <= 20000
            and isinstance(current_evidence["sample_count"], int)
            and not isinstance(current_evidence["sample_count"], bool)
            and current_evidence["sample_count"] >= 30
            and isinstance(current_evidence["random_seed"], int)
            and not isinstance(current_evidence["random_seed"], bool)
            and 0 <= current_evidence["random_seed"] <= 2**32 - 1
            and isinstance(current_evidence["dataset_fingerprint"], str)
            and len(current_evidence["dataset_fingerprint"]) == 64
            and all(char in "0123456789abcdef" for char in current_evidence["dataset_fingerprint"].lower())
        )
        evidence_matches = metadata_valid and bool(reference_evidence) and all(
            reference_evidence.get(key) == current_evidence.get(key)
            for key in (
                "method", "replicates", "sample_count", "random_seed",
                "dataset_fingerprint", "evaluation_partition",
            )
        )
        candidate_bootstrap = {}

        for evaluated_metric in evaluation_report.comparisons:
            metric_name = evaluated_metric.metric
            comparison = self._compare_metric(
                metric_name=metric_name,
                direction=evaluated_metric.direction,
                reference_value=context.reference_metrics.get(metric_name),
                treatment_value=treatment_metrics.get(metric_name),
            )
            current_draws = treatment_draws.get(metric_name)
            reference_draws = context.reference_bootstrap.get(metric_name)
            draws_valid = (
                isinstance(current_draws, list)
                and isinstance(reference_draws, list)
                and len(current_draws) == current_evidence["replicates"]
                and len(reference_draws) == current_evidence["replicates"]
            )
            eval_comparison = next(
                (item for item in evaluation_report.comparisons if item.metric == metric_name), None
            )
            statistical = (
                bootstrap_difference_summary(
                    reference_draws, current_draws,
                    confidence_level=(
                        eval_comparison.confidence_level
                        if eval_comparison and eval_comparison.confidence_level is not None
                        else 0.95
                    ),
                    direction=evaluated_metric.direction.value,
                    minimum_effect=(eval_comparison.minimum_effect or 0.0) if eval_comparison else 0.0,
                    method=current_evidence["method"],
                )
                if evidence_matches and draws_valid else {"status": "unavailable"}
            )
            if statistical.get("status") != "assessed":
                statistical_complete = False
            else:
                comparison = comparison.model_copy(update={
                    "statistical_status": "assessed",
                    "confidence_level": statistical["confidence_level"],
                    "confidence_interval": statistical["confidence_interval"],
                    "bootstrap_replicates": statistical["replicates"],
                    "confidently_better": statistical["confidently_better"],
                    "confidently_worse": statistical["confidently_worse"],
                })
                if statistical["confidently_better"]:
                    statistical_better.append(metric_name)
                if statistical["confidently_worse"]:
                    statistical_worse.append(metric_name)
            if isinstance(current_draws, list):
                candidate_bootstrap[metric_name] = current_draws
            comparisons.append(comparison)

            if not comparison.comparable:
                metrics_incomparable.append(metric_name)
            elif comparison.absolute_change == 0:
                metrics_unchanged.append(metric_name)
            elif comparison.better_than_reference:
                metrics_better.append(metric_name)
            else:
                metrics_worse.append(metric_name)

        return BenchmarkReport(
            experiment_id=evaluation_report.experiment_id,
            run_id=evaluation_report.run_id,
            result_id=evaluation_report.result_id,
            reference_id=context.reference_id,
            reference_type=context.reference_type,
            status=BenchmarkStatus.SUCCESS,
            reference_available=True,
            comparisons=comparisons,
            metrics_better=metrics_better,
            metrics_worse=metrics_worse,
            metrics_unchanged=metrics_unchanged,
            metrics_incomparable=metrics_incomparable,
            statistical_evidence_complete=statistical_complete and evidence_matches,
            statistically_better=statistical_better,
            statistically_worse=statistical_worse,
            candidate_bootstrap=candidate_bootstrap,
            bootstrap_evidence=current_evidence,
        )

    def _compare_metric(
        self,
        metric_name: str,
        direction: MetricDirection,
        reference_value: Optional[float],
        treatment_value: Optional[float],
    ) -> ReferenceComparison:
        raw_values = (("reference", reference_value), ("treatment", treatment_value))
        normalized = {label: finite_metric_value(value) for label, value in raw_values}
        invalid = []
        for label, value in raw_values:
            if value is None:
                invalid.append(f"missing {label} value")
            elif normalized[label] is None:
                invalid.append(f"{label} value is not a finite number")
        if invalid:
            return ReferenceComparison(
                metric=metric_name,
                direction=direction,
                reference_value=normalized["reference"],
                treatment_value=normalized["treatment"],
                comparable=False,
                error_message="; ".join(invalid),
            )

        reference_value = normalized["reference"]
        treatment_value = normalized["treatment"]
        assert reference_value is not None and treatment_value is not None
        absolute_change = treatment_value - reference_value
        if not math.isfinite(absolute_change):
            return ReferenceComparison(
                metric=metric_name,
                direction=direction,
                reference_value=reference_value,
                treatment_value=treatment_value,
                comparable=False,
                error_message="Metric change exceeds the finite numeric range.",
            )
        relative_change = (
            absolute_change / reference_value if reference_value != 0 else None
        )
        if relative_change is not None and not math.isfinite(relative_change):
            relative_change = None
        if absolute_change == 0:
            better_than_reference = False
        elif direction == MetricDirection.HIGHER_IS_BETTER:
            better_than_reference = absolute_change > 0
        else:
            better_than_reference = absolute_change < 0

        return ReferenceComparison(
            metric=metric_name,
            direction=direction,
            reference_value=reference_value,
            treatment_value=treatment_value,
            absolute_change=absolute_change,
            relative_change=relative_change,
            better_than_reference=better_than_reference,
            comparable=True,
        )
