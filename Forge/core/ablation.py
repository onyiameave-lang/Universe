"""
Forge V2 - Ablation Engine (Phase 5)
====================================
Determines which component actually caused the observed difference
through controlled component-removal experiments.

The AblationEngine is a planner and evidence synthesizer only. It never
independently promotes a model or overrides promotion authority.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from Forge.core.evaluator import EvaluationReport
from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentDomain,
    ExperimentData,
    ExperimentEnvironment,
    ExperimentEvaluation,
    ExperimentResult,
    MetricDirection,
    MetricSpec,
)


class ComponentType(str, Enum):
    SIGNAL = "signal"
    DATA_SOURCE = "data_source"
    FEATURE_GROUP = "feature_group"
    MODEL_MODULE = "model_module"
    PROCESSING_STAGE = "processing_stage"


class AblationStudyStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING_INDIVIDUAL = "RUNNING_INDIVIDUAL"
    RUNNING_COMBINATIONS = "RUNNING_COMBINATIONS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class AblationValidationError(str, Enum):
    UNKNOWN_TARGET = "unknown_target"
    MISSING_CAPABILITY = "missing_capability"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    GROUP_NOT_APPROVED = "group_not_approved"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_PARENT_REFERENCE = "invalid_parent_reference"
    MANIFEST_VERSION_MISMATCH = "manifest_version_mismatch"
    CONSTRAINT_VIOLATION = "constraint_violation"


class ComponentDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    component_id: str
    component_type: ComponentType
    disable_capability: str
    constraints: Dict[str, Any] = Field(default_factory=dict)
    dependencies: Tuple[str, ...] = Field(default_factory=tuple)
    version: str = "1.0.0"

    @field_validator("dependencies", mode="before")
    @classmethod
    def _ensure_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class ComponentGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: str
    component_ids: Tuple[str, ...]
    reason: str

    @field_validator("component_ids", mode="before")
    @classmethod
    def _ensure_tuple(cls, value):
        if isinstance(value, list):
            return tuple(sorted(value))
        return value

    def matches(self, requested_ids: Set[str]) -> bool:
        return set(self.component_ids) == requested_ids


class ComponentManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str
    domain: str
    version: str = "1.0.0"
    components: Dict[str, ComponentDeclaration] = Field(default_factory=dict)
    approved_groups: Tuple[ComponentGroup, ...] = Field(default_factory=tuple)
    max_individual_ablations: int = 20
    max_combination_ablations: int = 10
    max_total_runs: int = 50
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("approved_groups", mode="before")
    @classmethod
    def _ensure_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _validate_budget(self):
        if self.max_total_runs < 1:
            raise ValueError("max_total_runs must be at least 1")
        if self.max_individual_ablations < 0:
            raise ValueError("max_individual_ablations cannot be negative")
        if self.max_combination_ablations < 0:
            raise ValueError("max_combination_ablations cannot be negative")
        return self


class AblationVariant(BaseModel):
    model_config = ConfigDict(frozen=True)

    variant_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_experiment_id: str
    disabled_components: Tuple[str, ...] = Field(default_factory=tuple)
    is_baseline: bool = False
    is_combination: bool = False
    variant_experiment_id: Optional[str] = None
    manifest_version: str = "1.0.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("disabled_components", mode="before")
    @classmethod
    def _ensure_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class AblationStudy(BaseModel):
    study_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_experiment_id: str
    manifest_id: str
    manifest_version: str
    status: AblationStudyStatus = AblationStudyStatus.PLANNED
    baseline_variant: Optional[AblationVariant] = None
    individual_variants: List[AblationVariant] = Field(default_factory=list)
    combination_variants: List[AblationVariant] = Field(default_factory=list)
    runs_executed: int = 0
    individual_runs_executed: int = 0
    combination_runs_executed: int = 0
    variant_experiment_ids: Dict[str, str] = Field(default_factory=dict)
    variant_result_ids: Dict[str, str] = Field(default_factory=dict)
    variant_evaluation_ids: Dict[str, str] = Field(default_factory=dict)
    combination_justifications: List[str] = Field(default_factory=list)
    validation_errors: Dict[str, AblationValidationError] = Field(default_factory=dict)
    validation_details: Dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


class ComponentImpact(BaseModel):
    model_config = ConfigDict(frozen=True)

    component_id: str
    full_system_metric: Optional[float] = None
    ablated_metric: Optional[float] = None
    marginal_impact: Optional[float] = None
    relative_impact: Optional[float] = None
    direction: MetricDirection
    interaction_suspected: bool = False
    evidence_quality: str = "single_run"


class AblationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    study_id: str
    parent_experiment_id: str
    manifest_id: str
    manifest_version: str
    component_impacts: List[ComponentImpact] = Field(default_factory=list)
    interaction_candidates: List[str] = Field(default_factory=list)
    unablatable_components: List[str] = Field(default_factory=list)
    unablatable_reasons: Dict[str, str] = Field(default_factory=dict)
    total_runs_executed: int = 0
    individual_runs_executed: int = 0
    combination_runs_executed: int = 0
    missing_runs: List[str] = Field(default_factory=list)
    incomparable_results: List[str] = Field(default_factory=list)
    ablation_engine_version: str = "5.0.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    causality_disclaimer: str = (
        "Ablation evidence measures marginal contribution in context, not independent causation. "
        "It cannot independently authorize promotion."
    )


class AblationEngine:
    def __init__(self, experiment_runner, evaluator):
        self._runner = experiment_runner
        self._evaluator = evaluator

    def validate_targets(
        self,
        contract: ExperimentContract,
        manifest: ComponentManifest,
    ) -> Tuple[bool, Dict[str, AblationValidationError], Dict[str, str]]:
        errors: Dict[str, AblationValidationError] = {}
        details: Dict[str, str] = {}

        if contract.parent_experiment_id is not None and contract.parent_experiment_id == contract.experiment_id:
            errors["__parent__"] = AblationValidationError.INVALID_PARENT_REFERENCE
            details["__parent__"] = (
                f"parent_experiment_id cannot reference the experiment's own ID '{contract.experiment_id}'."
            )

        for target in contract.ablation_targets:
            if target not in manifest.components:
                errors[target] = AblationValidationError.UNKNOWN_TARGET
                details[target] = (
                    f"Component '{target}' is not declared in manifest '{manifest.manifest_id}'."
                )
                continue

            declaration = manifest.components[target]
            if not declaration.disable_capability:
                errors[target] = AblationValidationError.MISSING_CAPABILITY
                details[target] = (
                    f"Component '{target}' has no registered disable_capability."
                )
                continue

            if declaration.dependencies:
                requested_with_deps = {target} | set(declaration.dependencies)
                group_found = any(
                    group.matches(requested_with_deps)
                    for group in manifest.approved_groups
                )
                if not group_found:
                    errors[target] = AblationValidationError.DEPENDENCY_CONFLICT
                    details[target] = (
                        f"Component '{target}' depends on {list(declaration.dependencies)}; "
                        f"an exact approved group is required for the set {sorted(requested_with_deps)}."
                    )

        return len(errors) == 0, errors, details

    def plan_study(
        self,
        contract: ExperimentContract,
        manifest: ComponentManifest,
    ) -> AblationStudy:
        valid, errors, details = self.validate_targets(contract, manifest)
        if not valid:
            raise ValueError(
                "Ablation targets failed manifest validation: "
                + "; ".join(f"{k}={v.value}" for k, v in errors.items())
            )

        study = AblationStudy(
            parent_experiment_id=contract.experiment_id,
            manifest_id=manifest.manifest_id,
            manifest_version=manifest.version,
            validation_errors=errors,
            validation_details=details,
        )

        study.baseline_variant = AblationVariant(
            parent_experiment_id=contract.experiment_id,
            disabled_components=(),
            is_baseline=True,
            manifest_version=manifest.version,
        )

        valid_targets = [t for t in contract.ablation_targets if t not in errors]
        if len(valid_targets) > manifest.max_individual_ablations:
            raise ValueError(
                f"Planned individual ablations ({len(valid_targets)}) exceed the manifest limit "
                f"({manifest.max_individual_ablations})."
            )
        if 1 + len(valid_targets) > manifest.max_total_runs:
            raise ValueError(
                f"Baseline plus planned variants ({1 + len(valid_targets)}) exceed the manifest total "
                f"({manifest.max_total_runs})."
            )

        for target in valid_targets:
            study.individual_variants.append(
                AblationVariant(
                    parent_experiment_id=contract.experiment_id,
                    disabled_components=(target,),
                    is_baseline=False,
                    manifest_version=manifest.version,
                )
            )
        return study

    def execute_study(
        self,
        study: AblationStudy,
        parent_contract: ExperimentContract,
        manifest: ComponentManifest,
    ) -> AblationStudy:
        study.status = AblationStudyStatus.RUNNING_INDIVIDUAL

        if study.baseline_variant is not None:
            if not self._check_budget(study, manifest, is_combination=False):
                study.status = AblationStudyStatus.ABORTED
                return study
            contract = self._derive_variant_contract(parent_contract, study.baseline_variant, study.study_id)
            self._execute_variant(study, contract, study.baseline_variant)

        for variant in study.individual_variants:
            if not self._check_budget(study, manifest, is_combination=False):
                study.status = AblationStudyStatus.ABORTED
                return study
            contract = self._derive_variant_contract(parent_contract, variant, study.study_id)
            self._execute_variant(study, contract, variant)

        study.status = AblationStudyStatus.COMPLETED
        study.completed_at = datetime.now(timezone.utc)
        return study

    def plan_combinations(
        self,
        study: AblationStudy,
        report: AblationReport,
        manifest: ComponentManifest,
    ) -> List[AblationVariant]:
        if not report.interaction_candidates:
            return []

        proposed: List[AblationVariant] = []
        candidate_pool = list(report.interaction_candidates)
        remaining_total_budget = manifest.max_total_runs - study.runs_executed
        remaining_combination_budget = manifest.max_combination_ablations - study.combination_runs_executed

        for i, candidate in enumerate(candidate_pool):
            if remaining_total_budget <= 0 or remaining_combination_budget <= 0:
                break
            for other in candidate_pool[i + 1 :]:
                if remaining_total_budget <= 0 or remaining_combination_budget <= 0:
                    break
                proposed.append(
                    AblationVariant(
                        parent_experiment_id=study.parent_experiment_id,
                        disabled_components=tuple(sorted([candidate, other])),
                        is_baseline=False,
                        is_combination=True,
                        manifest_version=manifest.version,
                    )
                )
                remaining_total_budget -= 1
                remaining_combination_budget -= 1
        return proposed

    def synthesize_report(
        self,
        study: AblationStudy,
        evaluation_reports: Dict[str, EvaluationReport],
        manifest: ComponentManifest,
    ) -> AblationReport:
        component_impacts: List[ComponentImpact] = []
        interaction_candidates: List[str] = []
        unablatable: List[str] = []
        unablatable_reasons: Dict[str, str] = {}
        missing_runs: List[str] = []
        incomparable: List[str] = []

        baseline_eval = None
        if study.baseline_variant and study.baseline_variant.variant_id in evaluation_reports:
            baseline_eval = evaluation_reports[study.baseline_variant.variant_id]

        for variant in study.individual_variants:
            if variant.variant_id not in evaluation_reports:
                missing_runs.append(variant.variant_id)
                continue

            variant_eval = evaluation_reports[variant.variant_id]
            disabled = variant.disabled_components[0] if variant.disabled_components else None
            if disabled is None:
                continue

            if disabled in study.validation_errors:
                unablatable.append(disabled)
                unablatable_reasons[disabled] = study.validation_details.get(disabled, "Unknown")
                continue

            if baseline_eval is None:
                continue

            for comparison in baseline_eval.comparisons:
                metric_name = comparison.metric
                baseline_value = comparison.baseline_value
                variant_value = next(
                    (vc.treatment_value for vc in variant_eval.comparisons if vc.metric == metric_name),
                    None,
                )
                if baseline_value is None or variant_value is None:
                    incomparable.append(f"{disabled}:{metric_name}")
                    continue

                marginal = variant_value - baseline_value
                relative = marginal / baseline_value if baseline_value != 0 else None
                interaction_suspected = abs(marginal) > 0.1 * abs(baseline_value) if baseline_value != 0 else False
                impact = ComponentImpact(
                    component_id=disabled,
                    full_system_metric=baseline_value,
                    ablated_metric=variant_value,
                    marginal_impact=marginal,
                    relative_impact=relative,
                    direction=comparison.direction,
                    interaction_suspected=interaction_suspected,
                    evidence_quality="single_run",
                )
                component_impacts.append(impact)
                if interaction_suspected:
                    interaction_candidates.append(disabled)

        return AblationReport(
            study_id=study.study_id,
            parent_experiment_id=study.parent_experiment_id,
            manifest_id=study.manifest_id,
            manifest_version=study.manifest_version,
            component_impacts=component_impacts,
            interaction_candidates=list(set(interaction_candidates)),
            unablatable_components=unablatable,
            unablatable_reasons=unablatable_reasons,
            total_runs_executed=study.runs_executed,
            individual_runs_executed=study.individual_runs_executed,
            combination_runs_executed=study.combination_runs_executed,
            missing_runs=missing_runs,
            incomparable_results=incomparable,
        )

    def _check_budget(
        self,
        study: AblationStudy,
        manifest: ComponentManifest,
        is_combination: bool,
    ) -> bool:
        if study.runs_executed >= manifest.max_total_runs:
            return False
        if not is_combination and study.individual_runs_executed >= manifest.max_individual_ablations:
            return False
        if is_combination and study.combination_runs_executed >= manifest.max_combination_ablations:
            return False
        return True

    def _derive_variant_contract(
        self,
        parent_contract: ExperimentContract,
        variant: AblationVariant,
        study_id: str,
    ) -> ExperimentContract:
        treatment_config = dict(parent_contract.treatment_config)
        treatment_config["_ablation_disabled_components"] = list(variant.disabled_components)
        treatment_config["_ablation_study_id"] = study_id
        treatment_config["_ablation_variant_id"] = variant.variant_id
        return ExperimentContract(
            hypothesis_id=parent_contract.hypothesis_id,
            domain=parent_contract.domain,
            baseline_config=dict(parent_contract.baseline_config),
            treatment_config=treatment_config,
            variables=dict(parent_contract.variables),
            ablation_targets=[],
            parent_experiment_id=parent_contract.experiment_id,
            data=parent_contract.data,
            environment=parent_contract.environment,
            evaluation=parent_contract.evaluation,
            created_by=f"AblationEngine.{study_id}",
        )

    def _execute_variant(
        self,
        study: AblationStudy,
        variant_contract: ExperimentContract,
        variant: AblationVariant,
    ) -> None:
        try:
            experiment_id = self._runner.submit_experiment(variant_contract)
            result = self._runner.execute_experiment(experiment_id)
            study.runs_executed += 1
            if variant.is_combination:
                study.combination_runs_executed += 1
            elif not variant.is_baseline:
                study.individual_runs_executed += 1
            study.variant_experiment_ids[variant.variant_id] = experiment_id
            study.variant_result_ids[variant.variant_id] = result.result_id
            study.variant_evaluation_ids[variant.variant_id] = str(uuid.uuid4())
        except Exception as exc:  # preserve study state without swallowing the root cause entirely
            study.variant_experiment_ids[variant.variant_id] = f"FAILED: {exc}"
            study.status = AblationStudyStatus.FAILED
