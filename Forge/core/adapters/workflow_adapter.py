"""
Forge - Workflow Optimization Adapter (§26.2)
===============================================
Target-specific adapter for agent workflow and tool-selection policy
optimization.

Accepts experiments with domain WORKFLOW_OPTIMIZATION.  Compares baseline
and treatment workflow configurations by running them through a supplied
workflow executor or returning a structured template for external execution.
"""

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

log = logging.getLogger("forge.adapter.workflow")


class WorkflowOptimizationAdapter:
    """Execute controlled workflow optimization experiments.

    Compares a baseline workflow/tool-selection policy against a treatment
    configuration using the workflow executor supplied in the contract
    variables or registered by name.

    Workflow executors are expected to accept a workflow config dict and a
    list of test scenarios, and return a dict of metric_name -> float.
    """

    domain = ExperimentDomain.WORKFLOW_OPTIMIZATION

    def __init__(
        self,
        executor_registry: Optional[Dict[str, Callable]] = None,
    ):
        self._executors = executor_registry or {}

    def register_executor(self, executor_id: str, executor: Callable) -> None:
        """Register a named workflow executor."""
        self._executors[executor_id] = executor
        log.info("Registered workflow executor: %s", executor_id)

    def execute(
        self, contract: ExperimentContract, run: ExperimentRun
    ) -> ExperimentResult:
        """Execute the workflow optimization experiment."""

        def failed(message: str, evidence: Optional[dict] = None) -> ExperimentResult:
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=message,
                evidence=evidence or {},
            )

        # Extract configurations
        baseline_config = contract.baseline_config
        treatment_config = contract.treatment_config
        test_scenarios = contract.variables.get("test_scenarios", [])

        if not baseline_config or not treatment_config:
            return failed("Both baseline_config and treatment_config are required")

        # Resolve executor
        executor_id = contract.variables.get("executor_id")
        executor = None
        if executor_id and executor_id in self._executors:
            executor = self._executors[executor_id]

        if executor is None:
            # Return structured template
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.COMPLETED,
                metrics={},
                evidence={
                    "mode": "template",
                    "baseline_config": baseline_config,
                    "treatment_config": treatment_config,
                    "test_scenarios_count": len(test_scenarios),
                    "metric_specs": {
                        name: spec.model_dump(mode="json")
                        for name, spec in contract.evaluation.metric_specs.items()
                    },
                    "instructions": (
                        "No workflow executor was available during execution. "
                        "Execute this template externally and submit results "
                        "through a follow-up evaluation-only request."
                    ),
                },
            )

        # Execute controlled comparison
        try:
            baseline_metrics = self._run_workflow(
                executor, baseline_config, test_scenarios,
                contract.evaluation.metric_specs,
            )
            treatment_metrics = self._run_workflow(
                executor, treatment_config, test_scenarios,
                contract.evaluation.metric_specs,
            )
        except Exception as exc:
            return failed(f"Workflow execution failed: {exc}")

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
                "test_scenarios_count": len(test_scenarios),
                "executor_id": executor_id or "unknown",
            },
        )

    def _run_workflow(
        self,
        executor: Callable,
        config: Dict[str, Any],
        scenarios: List[Any],
        metric_specs: Dict[str, Any],
    ) -> Dict[str, float]:
        """Execute a workflow config against test scenarios and collect metrics."""
        result = executor(config, scenarios)
        if not isinstance(result, dict):
            raise ValueError(
                f"Workflow executor must return a dict of metrics, got {type(result)}"
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
