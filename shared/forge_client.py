"""
shared.forge_client - Public Forge Training Client (§26.4)
============================================================
Thin facade through which any specialist agent submits training requests,
checks status, retrieves reports, and cancels jobs.

Specialist agents import **this module only**.  They never import Forge's
internal classes, storage, or adapters.

Usage::

    from shared.forge_client import ForgeClient, build_training_request

    client = ForgeClient(forge_agent)

    request = build_training_request(
        agent_name="oracle",
        agent_repository="Oracle",
        target_type="trading_strategy",
        objective="Improve sharpe ratio on BTC/USDT 1h",
        baseline_metrics={"sharpe_ratio": 1.2, "max_drawdown": 0.15},
        dataset_path="/data/btc_1h.csv",
        target_column="close",
        risk_category="standard",
    )

    result = client.submit(request)
    # result = {"status": "queued", "request_id": "...", "queue_position": 0}

    status = client.get_status(result["request_id"])
    report = client.get_report(result["request_id"])

Design:
    - The ForgeClient wraps a ForgeAgent instance (or any object that has
      an ``act()`` method accepting Forge task names).
    - ``build_training_request`` is a convenience builder that constructs a
      validated TrainingRequest without requiring the caller to know every
      sub-model.
    - All data crosses the boundary as plain dicts (JSON-serializable).
      The client never exposes Pydantic models or internal IDs to callers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("shared.forge_client")


# ---------------------------------------------------------------------------
# Lazy import of Forge schemas — avoids hard dependency at module load
# ---------------------------------------------------------------------------

def _import_schemas():
    """Import Forge training request schemas (deferred)."""
    from Forge.core.training_request import (
        AcceptanceCriteria,
        BaselineReference,
        DataReference,
        EvaluationRequirements,
        RequestCategory,
        RequestPriority,
        RequestorIdentity,
        ResourceBudget,
        RiskCategory,
        TargetArtifact,
        TargetType,
        TrainingConstraints,
        TrainingObjective,
        TrainingRequest,
    )
    return {
        "AcceptanceCriteria": AcceptanceCriteria,
        "BaselineReference": BaselineReference,
        "DataReference": DataReference,
        "EvaluationRequirements": EvaluationRequirements,
        "RequestCategory": RequestCategory,
        "RequestPriority": RequestPriority,
        "RequestorIdentity": RequestorIdentity,
        "ResourceBudget": ResourceBudget,
        "RiskCategory": RiskCategory,
        "TargetArtifact": TargetArtifact,
        "TargetType": TargetType,
        "TrainingConstraints": TrainingConstraints,
        "TrainingObjective": TrainingObjective,
        "TrainingRequest": TrainingRequest,
    }


# ==============================================================================
# 1. CONVENIENCE REQUEST BUILDER
# ==============================================================================

def build_training_request(
    # Required
    agent_name: str,
    agent_repository: str,
    target_type: str,
    objective: str,
    # Optional identity
    agent_version: str = "1.0.0",
    request_reason: str = "",
    # Target
    artifact_id: Optional[str] = None,
    artifact_version: Optional[str] = None,
    artifact_description: str = "",
    # Objective detail
    primary_metric: Optional[str] = None,
    metric_direction: Optional[str] = None,
    target_value: Optional[float] = None,
    minimum_acceptable: Optional[float] = None,
    # Baseline
    baseline_id: Optional[str] = None,
    baseline_metrics: Optional[Dict[str, float]] = None,
    baseline_config: Optional[Dict[str, Any]] = None,
    # Data
    dataset_id: Optional[str] = None,
    dataset_path: Optional[str] = None,
    data_fingerprint: Optional[str] = None,
    target_column: Optional[str] = None,
    data_description: str = "",
    data_permissions: Optional[List[str]] = None,
    # Constraints
    prohibited_changes: Optional[List[str]] = None,
    required_backends: Optional[List[str]] = None,
    excluded_backends: Optional[List[str]] = None,
    preserve_compatibility: bool = False,
    # Evaluation
    required_metrics: Optional[List[str]] = None,
    metric_specs: Optional[Dict[str, Any]] = None,
    cross_validation_folds: Optional[int] = None,
    ablation_required: bool = False,
    # Budget
    max_duration_seconds: Optional[int] = None,
    max_experiments: Optional[int] = None,
    priority: str = "normal",
    deadline: Optional[datetime] = None,
    # Risk
    risk_category: str = "standard",
    request_category: str = "parameter_training",
    # Acceptance
    minimum_improvement: Optional[float] = None,
    require_no_regression: bool = True,
    # Metadata
    tags: Optional[List[str]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a TrainingRequest dict from simple arguments.

    This is the recommended way for specialist agents to construct
    training requests -- it avoids importing Forge internals and
    validates all fields before submission.

    Returns a plain dict representation of the TrainingRequest.
    """
    schemas = _import_schemas()

    request = schemas["TrainingRequest"](
        requestor=schemas["RequestorIdentity"](
            agent_name=agent_name,
            agent_repository=agent_repository,
            agent_version=agent_version,
            request_reason=request_reason,
        ),
        target=schemas["TargetArtifact"](
            target_type=schemas["TargetType"](target_type),
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            artifact_description=artifact_description,
        ),
        objective=schemas["TrainingObjective"](
            description=objective,
            primary_metric=primary_metric,
            metric_direction=metric_direction,
            target_value=target_value,
            minimum_acceptable=minimum_acceptable,
        ),
        baseline=schemas["BaselineReference"](
            baseline_id=baseline_id,
            baseline_metrics=baseline_metrics or {},
            baseline_config=baseline_config or {},
        ),
        data=schemas["DataReference"](
            dataset_id=dataset_id,
            dataset_path=dataset_path,
            data_fingerprint=data_fingerprint,
            target_column=target_column,
            data_description=data_description,
            data_permissions=data_permissions or [],
        ),
        constraints=schemas["TrainingConstraints"](
            prohibited_changes=prohibited_changes or [],
            required_backends=required_backends or [],
            excluded_backends=excluded_backends or [],
            preserve_compatibility=preserve_compatibility,
        ),
        evaluation=schemas["EvaluationRequirements"](
            required_metrics=required_metrics or [],
            metric_specs=metric_specs or {},
            cross_validation_folds=cross_validation_folds,
            ablation_required=ablation_required,
        ),
        budget=schemas["ResourceBudget"](
            max_duration_seconds=max_duration_seconds,
            max_experiments=max_experiments,
            priority=schemas["RequestPriority"](priority),
            deadline=deadline,
        ),
        risk_category=schemas["RiskCategory"](risk_category),
        request_category=schemas["RequestCategory"](request_category),
        acceptance=schemas["AcceptanceCriteria"](
            minimum_improvement=minimum_improvement,
            require_no_regression=require_no_regression,
        ),
        tags=tags or [],
        context=context or {},
    )

    return request.model_dump(mode="json")


# ==============================================================================
# 2. FORGE CLIENT
# ==============================================================================

class ForgeClient:
    """Public client for specialist agents to interact with Forge.

    This client is the ONLY interface specialist agents should use.
    It consumes Forge's ``act()`` task interface and returns plain dicts.

    Each agent remains responsible for its domain-specific work, live
    operations, and decision authority (§26.4).  Forge performs the
    authorized training workload and returns candidates, evidence,
    limitations, and recommendations.
    """

    def __init__(self, forge_agent: Any):
        """
        Args:
            forge_agent: A ForgeAgent instance or any object with an
                ``act(task, context)`` method.
        """
        if forge_agent is None:
            raise ValueError("ForgeClient requires a Forge agent instance")
        if not hasattr(forge_agent, "act"):
            raise ValueError("Forge agent must have an act() method")
        self._forge = forge_agent

    # ------------------------------------------------------------------
    # Training Request Lifecycle
    # ------------------------------------------------------------------

    def submit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a training request to Forge.

        Args:
            request: A TrainingRequest dict (from build_training_request
                     or manually constructed).

        Returns:
            Submission result with status, request_id, queue_position.
        """
        return self._forge.act("training.request.submit", {
            "request": request,
            "_sender": request.get("requestor", {}).get("agent_name", "unknown"),
        })

    def get_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get the current status of a training request.

        Returns:
            Status dict with lifecycle information, or None if not found.
        """
        result = self._forge.act("training.request.status", {
            "request_id": request_id,
            "_sender": "forge_client",
        })
        return result if result and result.get("status") != "error" else None

    def get_authorization_request(
        self, request_id: str, authorized_by: str
    ) -> Dict[str, Any]:
        """Build exact Aegis claims for an elevated or critical request."""
        return self._forge.act("training.request.authorization_request", {
            "request_id": request_id,
            "authorized_by": authorized_by,
            "_sender": "forge_client",
        })

    def authorize_request(
        self, request_id: str, authorization: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Submit a signed Aegis authorization for a pending training request."""
        return self._forge.act("training.request.authorize", {
            "request_id": request_id,
            "authorization": authorization,
            "_sender": "forge_client",
        })

    def reconcile_abandoned_request(
        self, request_id: str, authorization: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Close a crashed request only with worker-termination attestation and Aegis authorization."""
        return self._forge.act("training.request.reconciliation.reconcile", {
            "request_id": request_id,
            "authorization": authorization,
            "_sender": "forge_client",
        })

    def get_request_reconciliation_authorization_request(
        self, request_id: str, authorized_by: str, reason: str,
        worker_terminated: bool,
    ) -> Dict[str, Any]:
        """Build claims to authorize reconciliation after worker termination is confirmed."""
        return self._forge.act(
            "training.request.reconciliation.authorization_request",
            {
                "request_id": request_id,
                "authorized_by": authorized_by,
                "reason": reason,
                "worker_terminated": worker_terminated,
                "_sender": "forge_client",
            },
        )

    def submit_experiment(self, contract: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a domain-neutral experiment contract to a registered Forge adapter."""
        return self._forge.act("experiment.contract.submit", {
            "contract": contract,
            "_sender": contract.get("created_by", "forge_client"),
        })

    def execute_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """Execute one persisted experiment and retrieve its durable evaluation."""
        return self._forge.act("experiment.contract.execute", {
            "experiment_id": experiment_id,
            "_sender": "forge_client",
        })

    def evaluate_experiment_run(self, run_id: str) -> Dict[str, Any]:
        """Recompute and persist the declared-metric evaluation for a completed run."""
        return self._forge.act("experiment.contract.evaluate", {
            "run_id": run_id,
            "_sender": "forge_client",
        })

    def get_experiment_report(self, experiment_id: str) -> Dict[str, Any]:
        """Retrieve all persisted runs, results, and evaluations for an experiment."""
        return self._forge.act("experiment.contract.report", {
            "experiment_id": experiment_id,
            "_sender": "forge_client",
        })

    def request_oracle_improvement(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Ask Forge to investigate an underperforming Oracle champion.

        The request is evidence and a read-only incumbent snapshot. Forge owns
        candidate generation and evaluation; Oracle retains champion ownership.
        """
        if not isinstance(request, dict):
            raise TypeError("Oracle improvement request must be a dictionary")
        return self._forge.act("oracle.improvement.request", {
            "request": request,
            "_sender": "oracle",
        })

    def record_oracle_demo_outcome(self, outcome: Dict[str, Any]) -> Dict[str, Any]:
        """Send one identified Oracle demo-trade result to Forge for learning."""
        if not isinstance(outcome, dict):
            raise TypeError("Oracle demo outcome must be a dictionary")
        return self._forge.act("oracle.strategy.demo_outcome", {
            "outcome": outcome,
            "_sender": "oracle",
        })

    def get_report(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get the training report for a completed request.

        Returns:
            TrainingReport dict, or None if not available.
        """
        result = self._forge.act("training.request.report", {
            "request_id": request_id,
            "_sender": "forge_client",
        })
        return result if result and result.get("status") != "error" else None

    def cancel(self, request_id: str) -> Dict[str, Any]:
        """Cancel a queued or running training request.

        Returns:
            Cancellation result dict.
        """
        return self._forge.act("training.request.cancel", {
            "request_id": request_id,
            "_sender": "forge_client",
        })

    def list_requests(
        self,
        agent_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List training requests, optionally filtered.

        Returns:
            List of request summary dicts.
        """
        result = self._forge.act("training.request.list", {
            "agent_name": agent_name,
            "status": status,
            "_sender": "forge_client",
        })
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "requests" in result:
            return result["requests"]
        return []

    # ------------------------------------------------------------------
    # Scheduler observability
    # ------------------------------------------------------------------

    def scheduler_status(self) -> Dict[str, Any]:
        """Get the current scheduler state (queue depth, running jobs, etc.)."""
        return self._forge.act("training.request.scheduler_status", {
            "_sender": "forge_client",
        })

    # ------------------------------------------------------------------
    # Reuse discovery (§26.5)
    # ------------------------------------------------------------------

    def search_reusable_artifacts(
        self,
        kind: Optional[str] = None,
        domain: Optional[str] = None,
        source_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search for reusable artifacts across agents."""
        result = self._forge.act("training.reuse.search", {
            "kind": kind,
            "domain": domain,
            "source_agent": source_agent,
            "_sender": "forge_client",
        })
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "artifacts" in result:
            return result["artifacts"]
        return []

    def check_reuse_compatibility(
        self,
        artifact_id: str,
        requesting_agent: str,
        target_domain: str,
    ) -> Dict[str, Any]:
        """Check whether a reusable artifact is compatible with a target."""
        return self._forge.act("training.reuse.check", {
            "artifact_id": artifact_id,
            "requesting_agent": requesting_agent,
            "target_domain": target_domain,
            "_sender": "forge_client",
        })
