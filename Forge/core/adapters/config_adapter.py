"""
Forge - Configuration Optimization Adapter (§26.2)
=====================================================
Target-specific adapter for indicator configurations, feature pipelines,
ensemble configurations, and general configuration optimization.

Accepts experiments with domain CONFIG_OPTIMIZATION.  Compares baseline
and treatment configurations by running them through a supplied evaluator.
"""

import logging
import math
from typing import Any, Callable, Dict, List, Optional

from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentDomain,
    ExperimentResult,
    ExperimentRun,
    ExperimentStatus,
)

log = logging.getLogger("forge.adapter.config")


class ConfigOptimizationAdapter:
    """Execute controlled configuration optimization experiments.

    Compares a baseline configuration against a treatment configuration
    by running both through a config evaluator that produces comparable
    metrics.

    Config evaluators are expected to accept a config dict and optional
    data reference, and return a dict of metric_name -> float.
    """

    domain = ExperimentDomain.CONFIG_OPTIMIZATION

    def __init__(
        self,
        evaluator_registry: Optional[Dict[str, Callable]] = None,
    ):
        self._evaluators = evaluator_registry or {}

    def register_evaluator(self, evaluator_id: str, evaluator: Callable) -> None:
        """Register a named config evaluator."""
        self._evaluators[evaluator_id] = evaluator
        log.info("Registered config evaluator: %s", evaluator_id)

    def execute(
        self, contract: ExperimentContract, run: ExperimentRun
    ) -> ExperimentResult:
        """Execute the configuration optimization experiment."""

        def failed(message: str, evidence: Optional[dict] = None) -> ExperimentResult:
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=message,
                evidence=evidence or {},
            )

        baseline_config = contract.baseline_config
        treatment_config = contract.treatment_config

        if not baseline_config or not treatment_config:
            return failed("Both baseline_config and treatment_config are required")

        # Resolve evaluator
        evaluator_id = contract.variables.get("evaluator_id")
        data_ref = contract.variables.get("data_reference")
        evaluator = None
        if evaluator_id and evaluator_id in self._evaluators:
            evaluator = self._evaluators[evaluator_id]

        if evaluator is None:
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.COMPLETED,
                metrics={},
                evidence={
                    "mode": "template",
                    "baseline_config": baseline_config,
                    "treatment_config": treatment_config,
                    "metric_specs": {
                        name: spec.model_dump(mode="json")
                        for name, spec in contract.evaluation.metric_specs.items()
                    },
                    "instructions": (
                        "No config evaluator was available during execution. "
                        "Execute this template externally and submit results "
                        "through a follow-up evaluation-only request."
                    ),
                },
            )

        # Execute controlled comparison
        try:
            baseline_metrics = self._evaluate_config(
                evaluator, baseline_config, data_ref,
                contract.evaluation.metric_specs,
            )
            treatment_metrics = self._evaluate_config(
                evaluator, treatment_config, data_ref,
                contract.evaluation.metric_specs,
            )
        except Exception as exc:
            return failed(f"Configuration evaluation failed: {exc}")

        return ExperimentResult(
            run_id=run.run_id,
            experiment_id=contract.experiment_id,
            status=ExperimentStatus.COMPLETED,
            metrics={
                "baseline": baseline_metrics,
                "treatment": treatment_metrics,
            },
            evidence={
                "mode": "evaluated",
                "evaluator_id": evaluator_id or "unknown",
                "data_reference": data_ref,
            },
        )

    def _evaluate_config(
        self,
        evaluator: Callable,
        config: Dict[str, Any],
        data_ref: Any,
        metric_specs: Dict[str, Any],
    ) -> Dict[str, float]:
        """Evaluate a configuration and collect declared metrics."""
        if data_ref is not None:
            result = evaluator(config, data_ref)
        else:
            result = evaluator(config)

        if not isinstance(result, dict):
            raise ValueError(
                f"Config evaluator must return a dict of metrics, got {type(result)}"
            )
        metrics = {}
        for name in metric_specs:
            if name in result:
                try:
                    value = float(result[name])
                    if math.isfinite(value):
                        metrics[name] = value
                except (TypeError, ValueError):
                    pass
        return metrics
