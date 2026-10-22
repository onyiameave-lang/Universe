"""
Forge - Universal Training Request Interface (§26.1) and Training Report (§26.7)
=================================================================================
Governed, versioned schemas through which any authorized agent can request
training or improvement from Forge.

A TrainingRequest declares WHAT is needed.  Forge validates, queues, executes,
and returns a TrainingReport containing structured evidence.

This module defines the **public contract** -- specialist agents import these
schemas; they never import Forge internals.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ==============================================================================
# 1. ENUMS
# ==============================================================================

class TargetType(str, Enum):
    """What kind of artifact is being trained/optimized."""
    ML_MODEL = "ml_model"
    NEURAL_NETWORK = "neural_network"
    RL_POLICY = "rl_policy"
    TRADING_STRATEGY = "trading_strategy"
    INDICATOR_CONFIG = "indicator_config"
    LLM_PROMPT = "llm_prompt"
    TASK_CONFIG = "task_config"
    AGENT_WORKFLOW = "agent_workflow"
    TOOL_SELECTION_POLICY = "tool_selection_policy"
    FEATURE_PIPELINE = "feature_pipeline"
    ENSEMBLE_CONFIG = "ensemble_config"
    FINE_TUNE = "fine_tune"
    DISTILLATION = "distillation"
    EVALUATION_ONLY = "evaluation_only"


class RequestCategory(str, Enum):
    """Distinguishes what kind of work Forge should perform."""
    PARAMETER_TRAINING = "parameter_training"
    CONFIGURATION_OPTIMIZATION = "configuration_optimization"
    STRATEGY_EVOLUTION = "strategy_evolution"
    PROMPT_OPTIMIZATION = "prompt_optimization"
    WORKFLOW_OPTIMIZATION = "workflow_optimization"
    EVALUATION_ONLY = "evaluation_only"
    ABLATION_STUDY = "ablation_study"
    BENCHMARK_COMPARISON = "benchmark_comparison"


class RiskCategory(str, Enum):
    """Governs the approval requirements for a training request."""
    LOW = "low"                  # Automated approval acceptable
    STANDARD = "standard"        # Standard pipeline review
    ELEVATED = "elevated"        # Requires explicit authorization
    CRITICAL = "critical"        # Requires human approval via Aegis


class RequestStatus(str, Enum):
    """Lifecycle status of a training request."""
    SUBMITTED = "SUBMITTED"
    VALIDATING = "VALIDATING"
    PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in (
            RequestStatus.COMPLETED,
            RequestStatus.FAILED,
            RequestStatus.ABORTED,
            RequestStatus.REJECTED,
            RequestStatus.CANCELLED,
            RequestStatus.TIMED_OUT,
        )


class RequestPriority(str, Enum):
    """Priority class for resource scheduling."""
    BACKGROUND = "background"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# ==============================================================================
# 2. REQUEST SUB-MODELS
# ==============================================================================

class RequestorIdentity(BaseModel):
    """Who is requesting the training."""
    model_config = ConfigDict(frozen=True)

    agent_name: str
    agent_repository: str
    agent_version: str = "1.0.0"
    request_reason: str = ""


class TargetArtifact(BaseModel):
    """What artifact is being trained/optimized."""
    model_config = ConfigDict(frozen=True)

    target_type: TargetType
    artifact_id: Optional[str] = None
    artifact_version: Optional[str] = None
    artifact_description: str = ""


class TrainingObjective(BaseModel):
    """What improvement is desired."""
    model_config = ConfigDict(frozen=True)

    description: str
    primary_metric: Optional[str] = None
    metric_direction: Optional[str] = None   # "higher_is_better" / "lower_is_better"
    target_value: Optional[float] = None
    minimum_acceptable: Optional[float] = None


class BaselineReference(BaseModel):
    """Current baseline to improve upon."""
    model_config = ConfigDict(frozen=True)

    baseline_id: Optional[str] = None
    baseline_metrics: Dict[str, float] = Field(default_factory=dict)
    baseline_config: Dict[str, Any] = Field(default_factory=dict)
    baseline_description: str = ""


class DataReference(BaseModel):
    """Available and authorized data for training."""
    dataset_id: Optional[str] = None
    dataset_path: Optional[str] = None
    data_fingerprint: Optional[str] = None
    target_column: Optional[str] = None
    data_description: str = ""
    data_permissions: List[str] = Field(default_factory=list)
    data_constraints: Dict[str, Any] = Field(default_factory=dict)


class TrainingConstraints(BaseModel):
    """What Forge must NOT do."""
    model_config = ConfigDict(frozen=True)

    prohibited_changes: List[str] = Field(default_factory=list)
    required_backends: List[str] = Field(default_factory=list)
    excluded_backends: List[str] = Field(default_factory=list)
    max_parameters: Optional[int] = None
    preserve_compatibility: bool = False
    domain_specific: Dict[str, Any] = Field(default_factory=dict)


class EvaluationRequirements(BaseModel):
    """How the result should be evaluated."""
    model_config = ConfigDict(frozen=True)

    required_metrics: List[str] = Field(default_factory=list)
    metric_specs: Dict[str, Any] = Field(default_factory=dict)
    minimum_samples: Optional[int] = None
    cross_validation_folds: Optional[int] = None
    statistical_significance: bool = False
    ablation_required: bool = False
    benchmark_references: List[str] = Field(default_factory=list)


class ResourceBudget(BaseModel):
    """Resource and time constraints."""
    model_config = ConfigDict(frozen=True)

    max_duration_seconds: Optional[int] = None
    max_experiments: Optional[int] = None
    max_compute_units: Optional[float] = None
    priority: RequestPriority = RequestPriority.NORMAL
    deadline: Optional[datetime] = None


class AcceptanceCriteria(BaseModel):
    """What constitutes a successful result."""
    model_config = ConfigDict(frozen=True)

    minimum_improvement: Optional[float] = None
    required_metrics_met: List[str] = Field(default_factory=list)
    require_no_regression: bool = True
    require_reproducibility: bool = True
    custom_criteria: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# 3. TRAINING REQUEST (§26.1 — the top-level governed interface)
# ==============================================================================

class TrainingRequest(BaseModel):
    """
    Universal Training Request submitted by any authorized agent.

    This is the single governed entry point for all training, optimization,
    evaluation, and improvement workloads across the Universe ecosystem.
    """
    model_config = ConfigDict(frozen=True)

    # Identity
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "1.0.0"

    # §26.1 fields
    requestor: RequestorIdentity
    target: TargetArtifact
    objective: TrainingObjective
    baseline: BaselineReference = Field(default_factory=BaselineReference)
    data: DataReference = Field(default_factory=DataReference)
    constraints: TrainingConstraints = Field(default_factory=TrainingConstraints)
    evaluation: EvaluationRequirements = Field(default_factory=EvaluationRequirements)
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    risk_category: RiskCategory = RiskCategory.STANDARD
    acceptance: AcceptanceCriteria = Field(default_factory=AcceptanceCriteria)
    request_category: RequestCategory = RequestCategory.PARAMETER_TRAINING

    # Metadata
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tags: Tuple[str, ...] = Field(default_factory=tuple)
    context: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("tags", mode="before")
    @classmethod
    def _ensure_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    def content_digest(self) -> str:
        """Deterministic content hash for deduplication and integrity."""
        payload = self.model_dump(mode="json", exclude={"request_id", "submitted_at"})
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


# ==============================================================================
# 4. REQUEST LIFECYCLE RECORD
# ==============================================================================

class RequestLifecycle(BaseModel):
    """Tracks the full lifecycle of a training request."""

    request_id: str
    status: RequestStatus = RequestStatus.SUBMITTED
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Linkage to Forge internals (not exposed in public contract)
    experiment_id: Optional[str] = None
    run_id: Optional[str] = None
    result_id: Optional[str] = None

    # Tracking
    status_history: List[Dict[str, Any]] = Field(default_factory=list)
    error_message: Optional[str] = None
    assigned_adapter: Optional[str] = None
    resource_usage: Dict[str, Any] = Field(default_factory=dict)
    authorization_id: Optional[str] = None
    authorized_by: Optional[str] = None
    authorization_claims_digest: Optional[str] = None
    authorization_issuer: Optional[str] = None
    authorization_nonce: Optional[str] = None
    authorization_signature_digest: Optional[str] = None
    worker_id: Optional[str] = None

    def transition(self, new_status: RequestStatus, message: str = "") -> None:
        """Record a status transition with timestamp."""
        self.status_history.append({
            "from_status": self.status.value,
            "to_status": new_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
        })
        self.status = new_status
        if new_status == RequestStatus.RUNNING and self.started_at is None:
            self.started_at = datetime.now(timezone.utc)
        if new_status.is_terminal and self.completed_at is None:
            self.completed_at = datetime.now(timezone.utc)


class RequestReconciliationAuthorization(BaseModel):
    """Aegis authorization to close one abandoned request after worker verification."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str
    authorized_by: str
    decision: str = "AUTHORIZED"
    audience: str = "forge.operations"
    issuer: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    claims: Dict[str, Any]


# ==============================================================================
# 5. TRAINING REPORT (§26.7 — structured evidence returned to requestor)
# ==============================================================================

class TrainingReport(BaseModel):
    """
    Structured report returned for every completed training request.

    A successful training job is NOT proof of improvement. The candidate must
    pass independent evaluation and the existing governance process.
    """
    model_config = ConfigDict(frozen=True)

    # §26.7 fields
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str
    schema_version: str = "1.0.0"

    # Identifiers
    experiment_id: Optional[str] = None
    run_id: Optional[str] = None
    result_id: Optional[str] = None

    # Artifact references
    baseline_artifact_ref: Optional[str] = None
    candidate_artifact_ref: Optional[str] = None

    # Data and configuration lineage
    data_lineage: Dict[str, Any] = Field(default_factory=dict)
    configuration_lineage: Dict[str, Any] = Field(default_factory=dict)

    # Evaluation and benchmark results
    evaluation_results: Dict[str, Any] = Field(default_factory=dict)
    benchmark_results: Dict[str, Any] = Field(default_factory=dict)

    # Statistical uncertainty
    statistical_summary: Dict[str, Any] = Field(default_factory=dict)

    # Ablation and failure analysis
    ablation_findings: Dict[str, Any] = Field(default_factory=dict)
    failure_analysis: Dict[str, Any] = Field(default_factory=dict)

    # Resource consumption
    resource_consumption: Dict[str, Any] = Field(default_factory=dict)
    duration_seconds: Optional[float] = None

    # Known limitations and risks
    known_limitations: List[str] = Field(default_factory=list)
    known_risks: List[str] = Field(default_factory=list)

    # Reproducibility
    reproducibility_info: Dict[str, Any] = Field(default_factory=dict)

    # Promotion eligibility
    promotion_eligible: bool = False
    outstanding_requirements: List[str] = Field(default_factory=list)

    # Status
    success: bool = False
    error_message: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("known_limitations", "known_risks", "outstanding_requirements", mode="before")
    @classmethod
    def _ensure_list(cls, value):
        if isinstance(value, tuple):
            return list(value)
        return value
