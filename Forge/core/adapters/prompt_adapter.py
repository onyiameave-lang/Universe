"""
Forge - Prompt Optimization Adapter (§26.2)
=============================================
Target-specific adapter for LLM prompt and task configuration optimization.

Accepts TrainingRequests with target_type LLM_PROMPT or TASK_CONFIG, executes
controlled prompt experiments (A/B baseline vs treatment), and produces
structured ExperimentResults with evidence.

This adapter does NOT call external LLM APIs during execution — it wraps
whatever evaluation function the requesting agent supplies in the request
context.  If no evaluation function is available, it returns a structured
template that the agent can execute independently.
"""

import copy
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentDomain,
    ExperimentResult,
    ExperimentRun,
    ExperimentStatus,
)

log = logging.getLogger("forge.adapter.prompt")


class PromptOptimizationAdapter:
    """Execute controlled prompt optimization experiments.

    The adapter compares a baseline prompt/config against a treatment
    prompt/config using the evaluation function provided in the contract's
    variables.  If no evaluation function is available, it structures
    the experiment for external execution.

    Evaluation functions are expected to accept a prompt/config dict and
    return a dict of metric_name -> float.
    """

    domain = ExperimentDomain.PROMPT_OPTIMIZATION

    def __init__(
        self,
        evaluator_registry: Optional[Dict[str, Callable]] = None,
    ):
        # Named evaluators that agents can reference by ID
        self._evaluators = evaluator_registry or {}

    def register_evaluator(self, evaluator_id: str, evaluator: Callable) -> None:
        """Register a named evaluation function."""
        self._evaluators[evaluator_id] = evaluator
        log.info("Registered prompt evaluator: %s", evaluator_id)

    def execute(
        self, contract: ExperimentContract, run: ExperimentRun
    ) -> ExperimentResult:
        """Execute the prompt optimization experiment."""

        def failed(message: str, evidence: Optional[dict] = None) -> ExperimentResult:
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=message,
                evidence=evidence or {},
            )

        # Extract configurations
        try:
            baseline_prompt = contract.baseline_config.get("prompt")
            treatment_prompt = contract.treatment_config.get("prompt")
            if baseline_prompt is None and treatment_prompt is None:
                baseline_prompt = contract.baseline_config
                treatment_prompt = contract.treatment_config
        except (KeyError, TypeError) as exc:
            return failed(f"Missing prompt configuration: {exc}")

        # Resolve evaluator
        evaluator_id = contract.variables.get("evaluator_id")
        evaluation_fn = contract.variables.get("evaluation_fn")
        test_cases = contract.variables.get("test_cases", [])
        n_iterations = contract.variables.get("n_iterations", 1)

        evaluator = None
        if evaluator_id and evaluator_id in self._evaluators:
            evaluator = self._evaluators[evaluator_id]
        elif callable(evaluation_fn):
            evaluator = evaluation_fn

        if evaluator is None:
            # Return structured template for external execution
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.COMPLETED,
                metrics={},
                evidence={
                    "mode": "template",
                    "baseline_prompt": baseline_prompt,
                    "treatment_prompt": treatment_prompt,
                    "test_cases": test_cases,
                    "n_iterations": n_iterations,
                    "metric_specs": {
                        name: spec.model_dump(mode="json")
                        for name, spec in contract.evaluation.metric_specs.items()
                    },
                    "instructions": (
                        "No evaluator was available during execution. "
                        "Execute this template externally and submit results "
                        "through a follow-up evaluation-only request."
                    ),
                },
            )

        # Execute controlled comparison
        try:
            baseline_metrics = self._evaluate_prompt(
                evaluator, baseline_prompt, test_cases, n_iterations,
                contract.evaluation.metric_specs,
            )
            treatment_metrics = self._evaluate_prompt(
                evaluator, treatment_prompt, test_cases, n_iterations,
                contract.evaluation.metric_specs,
            )
        except Exception as exc:
            return failed(
                f"Prompt evaluation failed: {exc}",
                {
                    "baseline_prompt": baseline_prompt,
                    "treatment_prompt": treatment_prompt,
                },
            )

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
                "baseline_prompt": baseline_prompt,
                "treatment_prompt": treatment_prompt,
                "test_cases_count": len(test_cases),
                "n_iterations": n_iterations,
                "evaluator_id": evaluator_id or "inline",
            },
        )

    def _evaluate_prompt(
        self,
        evaluator: Callable,
        prompt_config: Any,
        test_cases: List[Any],
        n_iterations: int,
        metric_specs: Dict[str, Any],
    ) -> Dict[str, float]:
        """Run the evaluator and aggregate metrics."""
        all_metrics: Dict[str, List[float]] = {}

        for iteration in range(n_iterations):
            if test_cases:
                for case in test_cases:
                    result = evaluator(prompt_config, case)
                    self._accumulate_metrics(all_metrics, result, metric_specs)
            else:
                result = evaluator(prompt_config)
                self._accumulate_metrics(all_metrics, result, metric_specs)

        # Aggregate: mean of all collected values
        aggregated = {}
        for name, values in all_metrics.items():
            if values:
                mean = sum(values) / len(values)
                if math.isfinite(mean):
                    aggregated[name] = mean
        return aggregated

    @staticmethod
    def _accumulate_metrics(
        accumulator: Dict[str, List[float]],
        result: Dict[str, Any],
        metric_specs: Dict[str, Any],
    ) -> None:
        """Collect metric values from an evaluator result."""
        if not isinstance(result, dict):
            return
        for name in metric_specs:
            if name in result:
                value = result[name]
                try:
                    numeric = float(value)
                    if math.isfinite(numeric):
                        accumulator.setdefault(name, []).append(numeric)
                except (TypeError, ValueError):
                    pass
