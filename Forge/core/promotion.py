"""
Forge V2 - Promotion Gate (Phase 4D)
====================================
Evaluates challenge evidence and authorizes champion transitions.

Evaluator measures. Benchmark contextualizes. ChallengerEngine coordinates.
PromotionGate decides. ChampionRegistry owns state.
"""

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Forge.core.benchmark import BenchmarkReport, BenchmarkStatus
from Forge.core.challenger import (
    ChallengeResult,
    ChallengeStatus,
    ChampionRegistry,
    ChampionTenure,
)
from Forge.core.evaluator import EvaluationReport
from Forge.core.metrics import PAIRED_BOOTSTRAP_METHODS

log = logging.getLogger("forge.promotion")


class PromotionOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class CommitFailureReason(str, Enum):
    AUTHORIZATION_MISSING = "authorization_missing"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    AUTHORIZATION_CHALLENGE_MISMATCH = "authorization_challenge_mismatch"
    STALE_INCUMBENT = "stale_incumbent"
    REGISTRY_REVISION_MISMATCH = "registry_revision_mismatch"
    CHAMPION_NOT_FOUND = "champion_not_found"
    DOMAIN_CONTEXT_MISMATCH = "domain_context_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    POLICY_NOT_APPROVED = "policy_not_approved"
    EVIDENCE_CHANGED = "authoritative_evidence_changed"
    DECISION_NOT_COMMITTABLE = "decision_not_committable"
    DUPLICATE_COMMIT = "duplicate_commit"
    PERSISTENCE_FAILURE = "persistence_failure"


class PolicyVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    qualified: bool
    policy_name: str
    reasons: Tuple[str, ...] = Field(default_factory=tuple)
    evidence_issues: Tuple[str, ...] = Field(default_factory=tuple)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("reasons", "evidence_issues", mode="before")
    @classmethod
    def _ensure_tuples(cls, value):
        return tuple(value) if isinstance(value, (list, tuple)) else value


class PromotionPolicy(Protocol):
    name: str

    def evaluate(
        self,
        challenge_result: ChallengeResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
    ) -> PolicyVerdict:
        ...


class DefaultPromotionPolicy:
    """Conservative policy: require complete comparable evidence and improvement."""

    name = "default_conservative"

    def evaluate(
        self,
        challenge_result: ChallengeResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
    ) -> PolicyVerdict:
        reasons: List[str] = []
        evidence_issues: List[str] = []

        if challenge_result.status != ChallengeStatus.COMPLETED:
            evidence_issues.append(
                f"ChallengeResult status is '{challenge_result.status.value}', expected COMPLETED."
            )
        if benchmark_report.status != BenchmarkStatus.SUCCESS:
            evidence_issues.append(
                f"BenchmarkReport status is '{benchmark_report.status.value}', expected SUCCESS."
            )
        if not benchmark_report.reference_available:
            evidence_issues.append("Benchmark report does not have an available reference.")
        if benchmark_report.metrics_incomparable:
            evidence_issues.append(
                f"Incomparable benchmark metrics: {benchmark_report.metrics_incomparable}."
            )
        if evaluation_report.incomparable_metrics:
            evidence_issues.append(
                f"Incomparable evaluation metrics: {evaluation_report.incomparable_metrics}."
            )
        if not evaluation_report.statistical_evidence_complete:
            evidence_issues.append("Evaluation lacks the required paired-bootstrap evidence.")
        if not benchmark_report.statistical_evidence_complete:
            evidence_issues.append("Champion benchmark lacks matched paired-bootstrap evidence.")
        if benchmark_report.metrics_worse or benchmark_report.statistically_worse:
            reasons.append(
                "Metrics worse than champion: "
                f"point={benchmark_report.metrics_worse}, confidence={benchmark_report.statistically_worse}."
            )
        if evaluation_report.degraded_metrics or evaluation_report.statistically_degraded_metrics:
            reasons.append(
                "Metrics worse than baseline: "
                f"point={evaluation_report.degraded_metrics}, "
                f"confidence={evaluation_report.statistically_degraded_metrics}."
            )
        if not benchmark_report.metrics_better or not benchmark_report.statistically_better:
            reasons.append(
                "At least one metric must improve over the champion with bootstrap confidence."
            )

        qualified = not evidence_issues and not reasons
        if qualified:
            reasons.append(
                f"At least one metric improved, with no degradation or incomparable evidence."
            )

        return PolicyVerdict(
            qualified=qualified,
            policy_name=self.name,
            reasons=tuple(reasons),
            evidence_issues=tuple(evidence_issues),
        )


class AuthorizationToken(BaseModel):
    """Signed authorization artifact supplied by the trusted governance system.

    The fields are claims, not proof. ChampionRegistry accepts this artifact only
    when an injected trusted verifier validates its signature and consumes its
    nonce at the mutation boundary. Constructing this model is not authorization.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    challenge_id: str
    authorized_by: str
    decision: str
    risk_category: str = "general"
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    issuer: Optional[str] = None
    audience: str = "forge.promotion"
    signature: Optional[str] = None
    nonce: Optional[str] = None
    claims: Dict[str, Any] = Field(default_factory=dict)
    conditions: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, value):
        if value not in ("AUTHORIZED", "DENIED"):
            raise ValueError("decision must be 'AUTHORIZED' or 'DENIED'")
        return value

    @field_validator("authorized_by", "risk_category", "audience")
    @classmethod
    def _validate_nonempty(cls, value):
        if not value.strip():
            raise ValueError("authorization identity and risk category must not be empty")
        return value

    @field_validator("claims", "conditions", mode="before")
    @classmethod
    def _copy_mappings(cls, value):
        return copy.deepcopy(value) if isinstance(value, dict) else value


class PromotionDecision(BaseModel):
    """Immutable policy result and registry snapshot for a later commit."""

    model_config = ConfigDict(frozen=True)

    challenge_id: str
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    champion_id: str
    candidate_id: str
    outcome: PromotionOutcome
    policy_name: str
    policy_verdict: PolicyVerdict
    authorization_required: bool
    authorization_present: bool = False
    authorization_id: Optional[str] = None
    risk_category: str = "general"
    experiment_id: str
    run_id: str = ""
    result_id: str
    evaluation_report_id: Optional[str] = None
    benchmark_report_id: Optional[str] = None
    authoritative_evidence_digest: str = ""
    benchmark_metrics: Dict[str, float] = Field(default_factory=dict)
    candidate_bootstrap: Dict[str, List[float]] = Field(default_factory=dict)
    bootstrap_evidence: Dict[str, Any] = Field(default_factory=dict)
    expected_incumbent_tenure_id: Optional[str] = None
    expected_revision: Optional[int] = None
    expected_domain: Optional[str] = None
    expected_context_key: Optional[str] = None
    expected_owner_agent: Optional[str] = None
    reasons: Tuple[str, ...] = Field(default_factory=tuple)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("reasons", mode="before")
    @classmethod
    def _ensure_tuple(cls, value):
        return tuple(value) if isinstance(value, (list, tuple)) else value


class CommitResult(BaseModel):
    """Immutable outcome of attempting a champion registry transition."""

    model_config = ConfigDict(frozen=True)

    success: bool
    challenge_id: str
    champion_id: str
    outcome: PromotionOutcome
    failure_reason: Optional[CommitFailureReason] = None
    failure_detail: Optional[str] = None
    authorization_id: Optional[str] = None
    authorization_consumed: bool = False
    failure_audit_recorded: bool = False
    new_tenure_id: Optional[str] = None
    retired_tenure_id: Optional[str] = None
    registry_revision: Optional[int] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AegisRequirementConfig(BaseModel):
    """Promotion approval policy; Aegis approval cannot be disabled by override."""

    model_config = ConfigDict(frozen=True)

    default_requirement: str = "REQUIRED"
    domain_overrides: Dict[str, str] = Field(default_factory=dict)
    risk_category_overrides: Dict[str, str] = Field(default_factory=dict)

    @field_validator("default_requirement")
    @classmethod
    def _validate_default(cls, value):
        if value != "REQUIRED":
            raise ValueError("Champion promotion authorization must remain REQUIRED")
        return value

    @field_validator("domain_overrides", "risk_category_overrides")
    @classmethod
    def _validate_overrides(cls, value):
        if any(requirement != "REQUIRED" for requirement in value.values()):
            raise ValueError("Authorization overrides cannot disable champion promotion approval")
        return dict(value)

    def is_required(self, domain: str, risk_category: str = "general") -> bool:
        composite_key = f"{domain}:{risk_category}"
        requirement = self.risk_category_overrides.get(
            composite_key,
            self.domain_overrides.get(domain, self.default_requirement),
        )
        return requirement == "REQUIRED"


class AuthorizationVerifier(Protocol):
    """Trusted Aegis adapter for signature validation and one-time use.

    Implementations must verify the issuer, signature, intended audience,
    expiry, exact required claims, and atomically consume the nonce. They must
    return ``False`` on any validation or persistence error.
    """

    def verify_and_consume(
        self,
        authorization: AuthorizationToken,
        required_claims: Dict[str, Any],
    ) -> bool:
        ...


class PromotionEvidenceProvider(Protocol):
    """Read-only source of completed challenge records and their reports."""

    def get_result(self, challenge_id: str) -> Optional[ChallengeResult]:
        ...

    def get_evaluation_report(self, report_id: str) -> Optional[EvaluationReport]:
        ...

    def get_benchmark_report(self, report_id: str) -> Optional[BenchmarkReport]:
        ...


class PromotionGate:
    """The single policy and authorization gate for champion promotion."""

    def __init__(
        self,
        registry: ChampionRegistry,
        policy: PromotionPolicy,
        aegis_config: AegisRequirementConfig,
        authorization_verifier: Optional[AuthorizationVerifier] = None,
        evidence_provider: Optional[PromotionEvidenceProvider] = None,
    ):
        self._registry = registry
        self._policy = policy
        self._aegis_config = aegis_config
        self._authorization_verifier = authorization_verifier
        self._evidence_provider = evidence_provider
        configure_verifier = getattr(registry, "configure_authorization_verifier", None)
        if configure_verifier is not None:
            configure_verifier(authorization_verifier)

    @property
    def authorization_configured(self) -> bool:
        return self._authorization_verifier is not None

    def evaluate(
        self,
        challenge_result: ChallengeResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
        risk_category: str = "general",
    ) -> PromotionDecision:
        """Evaluate evidence and persist the exact immutable decision for later commit."""
        champion = self._registry.get_champion(challenge_result.champion_id)
        expected_tenure_id = (
            champion.current_tenure.tenure_id
            if champion is not None and champion.current_tenure is not None
            else None
        )
        expected_revision = champion.revision if champion is not None else None
        expected_domain = champion.domain if champion is not None else None
        expected_context = champion.context_key if champion is not None else None
        expected_owner = champion.owner_agent if champion is not None else None
        # Champion changes alter an agent's learned behavior. A risk override
        # cannot waive the signed approval required at this mutation boundary.
        auth_required = True

        evidence_issues = self._evidence_issues(
            challenge_result, evaluation_report, benchmark_report, champion
        )
        evidence_issues.extend(self._authoritative_evidence_issues(
            challenge_result, evaluation_report, benchmark_report
        ))
        if expected_owner is None or expected_owner == "legacy_unassigned":
            evidence_issues.append("Champion owner is unresolved; promotion is blocked.")
        if evidence_issues:
            verdict = PolicyVerdict(
                qualified=False,
                policy_name=self._policy.name,
                evidence_issues=tuple(evidence_issues),
            )
            outcome = PromotionOutcome.BLOCKED
        else:
            verdict = self._policy.evaluate(
                challenge_result, evaluation_report, benchmark_report
            )
            if verdict.evidence_issues:
                outcome = PromotionOutcome.BLOCKED
            elif verdict.qualified:
                outcome = PromotionOutcome.APPROVED
            else:
                outcome = PromotionOutcome.REJECTED

        decision = PromotionDecision(
            challenge_id=challenge_result.challenge_id,
            champion_id=challenge_result.champion_id,
            candidate_id=challenge_result.candidate_id,
            outcome=outcome,
            policy_name=self._policy.name,
            policy_verdict=verdict,
            authorization_required=auth_required,
            risk_category=risk_category,
            experiment_id=challenge_result.experiment_id,
            run_id=challenge_result.run_id,
            result_id=challenge_result.result_id,
            evaluation_report_id=challenge_result.evaluation_report_id,
            benchmark_report_id=challenge_result.benchmark_report_id,
            authoritative_evidence_digest=self._evidence_digest(
                challenge_result, evaluation_report, benchmark_report
            ),
            benchmark_metrics={
                item.metric: item.treatment_value
                for item in benchmark_report.comparisons
                if item.comparable and item.treatment_value is not None
            },
            candidate_bootstrap=benchmark_report.candidate_bootstrap,
            bootstrap_evidence=benchmark_report.bootstrap_evidence,
            expected_incumbent_tenure_id=expected_tenure_id,
            expected_revision=expected_revision,
            expected_domain=expected_domain,
            expected_context_key=expected_context,
            expected_owner_agent=expected_owner,
            reasons=tuple((*verdict.reasons, *verdict.evidence_issues)),
        )
        self._registry.record_promotion_decision(decision)
        return decision

    def _authoritative_evidence_issues(
        self,
        challenge_result: ChallengeResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
    ) -> List[str]:
        """Reject caller-supplied evidence unless it matches the durable ledger."""
        provider = self._evidence_provider
        if provider is None:
            return ["No authoritative challenge evidence provider is configured."]
        if not challenge_result.evaluation_report_id or not challenge_result.benchmark_report_id:
            return ["Challenge is missing durable evaluation or benchmark report references."]
        stored_result = provider.get_result(challenge_result.challenge_id)
        stored_evaluation = provider.get_evaluation_report(challenge_result.evaluation_report_id)
        stored_benchmark = provider.get_benchmark_report(challenge_result.benchmark_report_id)
        issues = []
        if stored_result is None or stored_result != challenge_result:
            issues.append("Challenge result does not match the authoritative challenge ledger.")
        if stored_evaluation is None or stored_evaluation != evaluation_report:
            issues.append("Evaluation report does not match the authoritative challenge ledger.")
        if stored_benchmark is None or stored_benchmark != benchmark_report:
            issues.append("Benchmark report does not match the authoritative challenge ledger.")
        return issues

    @staticmethod
    def _evidence_digest(
        challenge_result: ChallengeResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
    ) -> str:
        payload = {
            "challenge_result": challenge_result.model_dump(mode="json"),
            "evaluation_report": evaluation_report.model_dump(mode="json"),
            "benchmark_report": benchmark_report.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()

    def _current_evidence_digest(self, decision: PromotionDecision) -> Optional[str]:
        provider = self._evidence_provider
        if provider is None or not decision.evaluation_report_id or not decision.benchmark_report_id:
            return None
        challenge = provider.get_result(decision.challenge_id)
        evaluation = provider.get_evaluation_report(decision.evaluation_report_id)
        benchmark = provider.get_benchmark_report(decision.benchmark_report_id)
        if challenge is None or evaluation is None or benchmark is None:
            return None
        return self._evidence_digest(challenge, evaluation, benchmark)

    def _evidence_issues(
        self,
        challenge_result: ChallengeResult,
        evaluation_report: EvaluationReport,
        benchmark_report: BenchmarkReport,
        champion,
    ) -> List[str]:
        issues: List[str] = []
        if challenge_result.status != ChallengeStatus.COMPLETED:
            issues.append(f"Challenge status is {challenge_result.status.value}, not COMPLETED.")
        if not evaluation_report.statistical_evidence_complete:
            issues.append("Evaluation report does not contain complete statistical evidence.")
        if not benchmark_report.statistical_evidence_complete:
            issues.append("Benchmark report does not contain matched incumbent statistical evidence.")
        if not evaluation_report.comparisons:
            issues.append("Evaluation report has no metric comparisons.")
        if not benchmark_report.comparisons:
            issues.append("Benchmark report has no metric comparisons.")
        if champion is not None and champion.domain == "ORACLE_STRATEGY":
            design = evaluation_report.evaluation_design
            partition = design.get("evaluation_partition") if isinstance(design, dict) else None
            fingerprints = (
                partition.get("development_prefix_fingerprint"),
                partition.get("evaluation_suffix_fingerprint"),
            ) if isinstance(partition, dict) else ()
            valid_fingerprints = (
                len(fingerprints) == 2
                and all(
                    isinstance(value, str)
                    and len(value) == 64
                    and all(char in "0123456789abcdef" for char in value.lower())
                    for value in fingerprints
                )
            )
            start_index = partition.get("start_index") if isinstance(partition, dict) else None
            if (
                not isinstance(partition, dict)
                or partition.get("method") != "chronological_suffix_holdout_v1"
                or not isinstance(start_index, int)
                or isinstance(start_index, bool)
                or start_index < 40
                or not valid_fingerprints
                or design.get("dataset_fingerprint") != benchmark_report.bootstrap_evidence.get(
                    "dataset_fingerprint"
                )
            ):
                issues.append(
                    "Oracle promotion requires a fingerprinted chronological holdout partition."
                )
        elif champion is not None and champion.domain == "ML_SUPERVISED":
            design = evaluation_report.evaluation_design
            partition = design.get("evaluation_partition") if isinstance(design, dict) else None
            fingerprints = (
                partition.get("train_indices_fingerprint"),
                partition.get("validation_indices_fingerprint"),
            ) if isinstance(partition, dict) else ()
            valid_fingerprints = (
                len(fingerprints) == 2
                and all(
                    isinstance(value, str)
                    and len(value) == 64
                    and all(char in "0123456789abcdef" for char in value.lower())
                    for value in fingerprints
                )
            )
            sample_count = partition.get("sample_count") if isinstance(partition, dict) else None
            validation_count = (
                partition.get("validation_sample_count") if isinstance(partition, dict) else None
            )
            if (
                not isinstance(partition, dict)
                or partition.get("method") not in {
                    "sklearn_stratified_random_holdout_v1",
                    "sklearn_random_holdout_v1",
                    "python_seeded_random_holdout_v1",
                    "sklearn_stratified_group_holdout_v1",
                    "sklearn_group_holdout_v1",
                    "python_seeded_group_holdout_v1",
                }
                or not isinstance(partition.get("random_seed"), int)
                or isinstance(partition.get("random_seed"), bool)
                or partition.get("random_seed") != benchmark_report.bootstrap_evidence.get(
                    "random_seed"
                )
                or not isinstance(sample_count, int)
                or isinstance(sample_count, bool)
                or sample_count < 30
                or not isinstance(validation_count, int)
                or isinstance(validation_count, bool)
                or not 30 <= validation_count < sample_count
                or not valid_fingerprints
                or design.get("dataset_fingerprint") != benchmark_report.bootstrap_evidence.get(
                    "dataset_fingerprint"
                )
            ):
                issues.append(
                    "ML promotion requires a seeded, fingerprinted train/validation partition."
                )
        if any(
            comparison.statistical_status != "assessed"
            or comparison.confidence_interval is None
            or comparison.bootstrap_replicates is None
            or comparison.bootstrap_replicates < 200
            for comparison in evaluation_report.comparisons
        ):
            issues.append("Evaluation report contains a metric without assessed bootstrap evidence.")
        if any(
            comparison.statistical_status != "assessed"
            or comparison.confidence_interval is None
            or comparison.bootstrap_replicates is None
            or comparison.bootstrap_replicates < 200
            for comparison in benchmark_report.comparisons
        ):
            issues.append("Benchmark report contains a metric without assessed bootstrap evidence.")
        if evaluation_report.statistically_improved_metrics != [
            item.metric for item in evaluation_report.comparisons if item.confidently_better is True
        ] or evaluation_report.statistically_degraded_metrics != [
            item.metric for item in evaluation_report.comparisons if item.confidently_worse is True
        ]:
            issues.append("Evaluation statistical summary does not match its metric comparisons.")
        if benchmark_report.statistically_better != [
            item.metric for item in benchmark_report.comparisons if item.confidently_better is True
        ] or benchmark_report.statistically_worse != [
            item.metric for item in benchmark_report.comparisons if item.confidently_worse is True
        ]:
            issues.append("Benchmark statistical summary does not match its metric comparisons.")
        bootstrap_meta = benchmark_report.bootstrap_evidence
        bootstrap_replicates = bootstrap_meta.get("replicates")
        statistical_design = evaluation_report.evaluation_design.get("statistical_design")
        design_is_valid = isinstance(statistical_design, dict)
        if design_is_valid:
            nominal_confidence = statistical_design.get("nominal_confidence_level")
            adjusted_confidence = statistical_design.get("adjusted_confidence_level")
            metric_count = statistical_design.get("declared_metric_count")
            candidate_family_size = statistical_design.get("candidate_family_size")
            comparison_family_id = statistical_design.get("comparison_family_id")
            comparison_family_members = statistical_design.get("comparison_family_members")
            comparison_count = statistical_design.get("simultaneous_comparison_count")
            minimum_samples = statistical_design.get("minimum_sample_count")
            design_samples = statistical_design.get("validation_sample_count")
            design_replicates = statistical_design.get("bootstrap_replicates")
            requested_replicates = statistical_design.get("requested_bootstrap_replicates")
            design_method = statistical_design.get("bootstrap_method")
            family_members_valid = (
                isinstance(comparison_family_members, list)
                and all(
                    isinstance(member, str) and member.strip()
                    for member in comparison_family_members
                )
                and len(set(comparison_family_members)) == len(comparison_family_members)
            )
            if candidate_family_size == 1 and family_members_valid:
                family_scope_valid = (
                    comparison_family_id is None and comparison_family_members == []
                ) or (
                    isinstance(comparison_family_id, str)
                    and bool(comparison_family_id.strip())
                    and comparison_family_members == [challenge_result.candidate_id]
                )
            elif (
                isinstance(candidate_family_size, int)
                and not isinstance(candidate_family_size, bool)
                and candidate_family_size > 1
                and family_members_valid
            ):
                family_scope_valid = (
                    isinstance(comparison_family_id, str)
                    and bool(comparison_family_id.strip())
                    and len(comparison_family_members) == candidate_family_size
                    and challenge_result.candidate_id in comparison_family_members
                )
            else:
                family_scope_valid = False
            design_is_valid = (
                isinstance(nominal_confidence, (int, float))
                and not isinstance(nominal_confidence, bool)
                and 0.0 < nominal_confidence < 1.0
                and isinstance(adjusted_confidence, (int, float))
                and not isinstance(adjusted_confidence, bool)
                and 0.0 < adjusted_confidence < 1.0
                and isinstance(metric_count, int)
                and not isinstance(metric_count, bool)
                and metric_count == len(evaluation_report.comparisons)
                and isinstance(candidate_family_size, int)
                and not isinstance(candidate_family_size, bool)
                and candidate_family_size >= 1
                and family_scope_valid
                and isinstance(comparison_count, int)
                and not isinstance(comparison_count, bool)
                and comparison_count == metric_count * candidate_family_size
                and {
                    item.metric for item in evaluation_report.comparisons
                } == {
                    item.metric for item in benchmark_report.comparisons
                }
                and isinstance(minimum_samples, int)
                and not isinstance(minimum_samples, bool)
                and minimum_samples >= 30
                and isinstance(design_samples, int)
                and not isinstance(design_samples, bool)
                and design_samples >= minimum_samples
                and isinstance(design_replicates, int)
                and not isinstance(design_replicates, bool)
                and isinstance(requested_replicates, int)
                and not isinstance(requested_replicates, bool)
                and 200 <= requested_replicates <= 20000
                and design_replicates >= requested_replicates
                and isinstance(design_method, str)
                and design_method in PAIRED_BOOTSTRAP_METHODS
                and isinstance(bootstrap_meta.get("sample_count"), int)
                and not isinstance(bootstrap_meta.get("sample_count"), bool)
                and design_samples == bootstrap_meta.get("sample_count")
                and design_replicates == bootstrap_meta.get("replicates")
                and design_method == bootstrap_meta.get("method")
            )
            if design_is_valid:
                expected_adjusted_confidence = 1.0 - (
                    1.0 - nominal_confidence
                ) / comparison_count
                design_is_valid = abs(
                    adjusted_confidence - expected_adjusted_confidence
                ) <= 1e-12 and all(
                    item.confidence_level is not None
                    and abs(item.confidence_level - adjusted_confidence) <= 1e-12
                    for item in (
                        evaluation_report.comparisons + benchmark_report.comparisons
                    )
                )
        if not design_is_valid:
            issues.append(
                "Evaluation report does not preserve a valid, consistently applied statistical design."
            )
        evaluation_design = evaluation_report.evaluation_design
        holdout = evaluation_design.get("evaluation_holdout")
        partition = evaluation_design.get("evaluation_partition")
        try:
            partition_fingerprint = hashlib.sha256(json.dumps(
                partition,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")).hexdigest() if isinstance(partition, dict) else None
        except (TypeError, ValueError):
            partition_fingerprint = None
        expected_family_id = (
            statistical_design.get("comparison_family_id")
            if isinstance(statistical_design, dict)
            else None
        ) or evaluation_report.experiment_id
        family_size_for_holdout = (
            statistical_design.get("candidate_family_size")
            if isinstance(statistical_design, dict)
            else None
        )
        family_members_for_holdout = (
            statistical_design.get("comparison_family_members")
            if isinstance(statistical_design, dict)
            else None
        )
        holdout_is_valid = (
            isinstance(holdout, dict)
            and holdout.get("disposition") == "unique_family"
            and holdout.get("family_id") == expected_family_id
            and isinstance(holdout.get("dataset_fingerprint"), str)
            and holdout.get("dataset_fingerprint") == evaluation_design.get(
                "dataset_fingerprint"
            )
            and isinstance(holdout.get("partition_fingerprint"), str)
            and holdout.get("partition_fingerprint") == partition_fingerprint
            and isinstance(holdout.get("members"), list)
        )
        observed_holdout_fingerprint = (
            partition.get("holdout_fingerprint")
            or partition.get("evaluation_suffix_fingerprint")
            if isinstance(partition, dict) else None
        )
        holdout_is_valid = holdout_is_valid and (
            isinstance(observed_holdout_fingerprint, str)
            and len(observed_holdout_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in observed_holdout_fingerprint.lower())
            and holdout.get("holdout_fingerprint") == observed_holdout_fingerprint.lower()
        )
        if holdout_is_valid:
            if (
                not isinstance(family_size_for_holdout, int)
                or isinstance(family_size_for_holdout, bool)
            ):
                holdout_is_valid = False
            elif family_size_for_holdout > 1:
                holdout_is_valid = (
                    holdout.get("members") == family_members_for_holdout
                    and holdout.get("candidate_id") == challenge_result.candidate_id
                )
        if not holdout_is_valid:
            issues.append(
                "Evaluation holdout is untracked, reused, or not bound to this candidate family."
            )
        if (
            bootstrap_meta.get("method") not in PAIRED_BOOTSTRAP_METHODS
            or not isinstance(bootstrap_replicates, int)
            or isinstance(bootstrap_replicates, bool)
            or not 200 <= bootstrap_replicates <= 20000
            or not isinstance(bootstrap_meta.get("sample_count"), int)
            or isinstance(bootstrap_meta.get("sample_count"), bool)
            or bootstrap_meta.get("sample_count", 0) < 30
            or not isinstance(bootstrap_meta.get("dataset_fingerprint"), str)
            or not bootstrap_meta.get("dataset_fingerprint")
            or not isinstance(bootstrap_meta.get("random_seed"), int)
            or isinstance(bootstrap_meta.get("random_seed"), bool)
            or not 0 <= bootstrap_meta.get("random_seed", -1) <= 2**32 - 1
            or not isinstance(bootstrap_meta.get("dataset_fingerprint"), str)
            or len(bootstrap_meta.get("dataset_fingerprint", "")) != 64
            or not all(
                char in "0123456789abcdef"
                for char in bootstrap_meta.get("dataset_fingerprint", "").lower()
            )
        ) or any(
            not isinstance(benchmark_report.candidate_bootstrap.get(item.metric), list)
            or len(benchmark_report.candidate_bootstrap.get(item.metric, [])) != bootstrap_replicates
            for item in benchmark_report.comparisons
        ):
            issues.append("Benchmark candidate bootstrap payload is incomplete or malformed.")
        if not challenge_result.evaluation_report_id:
            issues.append("Challenge does not reference a persisted evaluation report.")
        if not challenge_result.benchmark_report_id:
            issues.append("Challenge does not reference a persisted benchmark report.")
        if champion is None:
            issues.append(f"Champion '{challenge_result.champion_id}' does not exist.")
        elif champion.current_tenure is None:
            issues.append(f"Champion '{challenge_result.champion_id}' has no incumbent tenure.")

        expected_ids = (
            ("experiment_id", challenge_result.experiment_id),
            ("run_id", challenge_result.run_id),
            ("result_id", challenge_result.result_id),
        )
        for report_name, report in (
            ("evaluation", evaluation_report),
            ("benchmark", benchmark_report),
        ):
            for field_name, expected in expected_ids:
                if getattr(report, field_name) != expected:
                    issues.append(
                        f"{report_name} report {field_name} does not match challenge evidence."
                    )
        if benchmark_report.reference_id != challenge_result.champion_id:
            issues.append("Benchmark reference does not match the challenged champion.")
        return issues

    def commit(
        self,
        decision: PromotionDecision,
        authorization: Optional[AuthorizationToken] = None,
    ) -> CommitResult:
        """Verify authorization and ask ChampionRegistry to atomically promote."""
        persisted = self._registry.get_promotion_decision(decision.decision_id)
        if persisted is None:
            return self._blocked(
                decision,
                CommitFailureReason.DECISION_NOT_COMMITTABLE,
                "Promotion decision was not issued and persisted by this gate.",
            )
        stored_decision = PromotionDecision.model_validate(persisted["decision"])
        if stored_decision != decision:
            return self._blocked(
                decision,
                CommitFailureReason.DECISION_NOT_COMMITTABLE,
                "Submitted promotion decision differs from its persisted evidence-bound decision.",
            )
        decision = stored_decision

        if (
            not decision.authoritative_evidence_digest
            or self._current_evidence_digest(decision) != decision.authoritative_evidence_digest
        ):
            return self._blocked(
                decision,
                CommitFailureReason.EVIDENCE_CHANGED,
                "Persisted challenge or evaluation evidence changed after the decision was issued.",
            )

        # Report idempotent replays before asking the single-use verifier to
        # consume a nonce. ChampionRegistry repeats this check transactionally
        # to close races between this read and the actual promotion.
        if self._registry.is_challenge_committed(decision.challenge_id):
            return self._blocked(
                decision,
                CommitFailureReason.DUPLICATE_COMMIT,
                f"Challenge '{decision.challenge_id}' has already been committed.",
            )
        if persisted["status"] != "EVALUATED":
            return self._blocked(
                decision,
                CommitFailureReason.DECISION_NOT_COMMITTABLE,
                f"Promotion decision is already {persisted['status'].lower()}.",
            )
        if decision.authorization_required is not True:
            return self._blocked(
                decision,
                CommitFailureReason.DECISION_NOT_COMMITTABLE,
                "Champion promotions require mandatory Aegis authorization.",
            )

        if (
            decision.outcome != PromotionOutcome.APPROVED
            or not decision.policy_verdict.qualified
            or decision.policy_verdict.policy_name != decision.policy_name
        ):
            return self._blocked(
                decision, CommitFailureReason.POLICY_NOT_APPROVED,
                "Only a qualified APPROVED policy decision can be committed.",
            )

        if (
            decision.expected_incumbent_tenure_id is None
            or decision.expected_revision is None
            or decision.expected_domain is None
            or decision.expected_context_key is None
        ):
            return self._blocked(
                decision, CommitFailureReason.STALE_INCUMBENT,
                "Promotion decision is missing the registry snapshot required for stale-state checks.",
            )
        champion = self._registry.get_champion(decision.champion_id)
        if (
            champion is None
            or champion.domain != decision.expected_domain
            or champion.context_key != decision.expected_context_key
            or champion.owner_agent == "legacy_unassigned"
            or champion.owner_agent != decision.expected_owner_agent
            or champion.revision != decision.expected_revision
            or champion.current_tenure is None
            or champion.current_tenure.tenure_id != decision.expected_incumbent_tenure_id
        ):
            return self._blocked(
                decision,
                CommitFailureReason.STALE_INCUMBENT,
                "Champion state changed after evaluation; request a fresh decision and authorization.",
            )

        authorization_consumed = False
        auth_failure = self._verify_authorization(decision, authorization)
        if auth_failure is not None:
            return auth_failure

        new_tenure = ChampionTenure(
            candidate_id=decision.candidate_id,
            source_experiment_id=decision.experiment_id,
            source_challenge_id=decision.challenge_id,
            evidence_reference={
                "promotion_decision_timestamp": decision.timestamp.isoformat(),
                "policy_name": decision.policy_name,
                "policy_qualified": decision.policy_verdict.qualified,
                "benchmark_metrics": decision.benchmark_metrics,
                "metric_bootstrap": decision.candidate_bootstrap,
                "bootstrap_evidence": decision.bootstrap_evidence,
                "evaluation_report_id": decision.evaluation_report_id,
                "benchmark_report_id": decision.benchmark_report_id,
                "result_id": decision.result_id,
                "owner_agent": decision.expected_owner_agent,
            },
        )
        commit_result = self._registry.promote(
            champion_id=decision.champion_id,
            new_tenure=new_tenure,
            expected_incumbent_tenure_id=decision.expected_incumbent_tenure_id,
            expected_revision=decision.expected_revision,
            expected_domain=decision.expected_domain,
            expected_context_key=decision.expected_context_key,
            retirement_reason=f"superseded_by_challenge_{decision.challenge_id}",
            challenge_id=decision.challenge_id,
            expected_owner_agent=decision.expected_owner_agent,
            promotion_decision_id=decision.decision_id,
            authorization=authorization,
        )
        authorization_consumed = commit_result.authorization_consumed
        if not commit_result.success and not commit_result.failure_audit_recorded:
            self._record_failed_attempt(
                decision,
                commit_result.failure_reason.value if commit_result.failure_reason else "unknown",
                commit_result.failure_detail or "Promotion registry rejected the commit.",
                authorization.authorization_id if authorization is not None else None,
                authorization_consumed,
            )
        return commit_result

    def _verify_authorization(
        self,
        decision: PromotionDecision,
        authorization: Optional[AuthorizationToken],
    ) -> Optional[CommitResult]:
        if authorization is None:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_MISSING,
                "Aegis authorization is required, but no authorization token was provided.",
            )
        if authorization.challenge_id != decision.challenge_id:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_CHALLENGE_MISMATCH,
                "Authorization token is for a different challenge.",
                authorization.authorization_id,
            )
        if authorization.risk_category != decision.risk_category:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "Authorization token risk category does not match the evaluated decision.",
                authorization.authorization_id,
            )
        if authorization.audience != "forge.promotion":
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "Authorization audience does not match Forge promotion.",
                authorization.authorization_id,
            )
        if authorization.decision != "AUTHORIZED":
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                f"Authorization decision is '{authorization.decision}'.",
                authorization.authorization_id,
            )
        now = datetime.now(timezone.utc)
        issued_at = authorization.issued_at
        expires_at = authorization.expires_at
        if issued_at.tzinfo is None or (expires_at is not None and expires_at.tzinfo is None):
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "Authorization timestamps must be timezone-aware.",
                authorization.authorization_id,
            )
        if issued_at > now:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "Authorization token was issued in the future.",
                authorization.authorization_id,
            )
        if expires_at is not None and now > expires_at:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_EXPIRED,
                f"Authorization expired at {expires_at.isoformat()}.",
                authorization.authorization_id,
            )

        if not authorization.issuer or not authorization.signature or not authorization.nonce:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "Authorization must include a trusted issuer, signature, and one-time nonce.",
                authorization.authorization_id,
            )

        required_claims = self.authorization_claims(decision, authorization.authorized_by)
        if authorization.claims != required_claims:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "Authorization claims do not exactly match this promotion decision.",
                authorization.authorization_id,
            )

        if self._authorization_verifier is None:
            return self._blocked(
                decision, CommitFailureReason.AUTHORIZATION_DENIED,
                "No trusted Aegis authorization verifier is configured; promotion is disabled.",
                authorization.authorization_id,
            )
        return None

    @staticmethod
    def authorization_claims(decision: PromotionDecision, authorized_by: str) -> Dict[str, Any]:
        """Return the exact decision-bound claims an external Aegis signer must approve."""
        return {
            "promotion_decision_id": decision.decision_id,
            "challenge_id": decision.challenge_id,
            "champion_id": decision.champion_id,
            "candidate_id": decision.candidate_id,
            "experiment_id": decision.experiment_id,
            "run_id": decision.run_id,
            "result_id": decision.result_id,
            "evaluation_report_id": decision.evaluation_report_id,
            "benchmark_report_id": decision.benchmark_report_id,
            "authoritative_evidence_digest": decision.authoritative_evidence_digest,
            "benchmark_metrics": decision.benchmark_metrics,
            "candidate_bootstrap_digest": hashlib.sha256(json.dumps(
                decision.candidate_bootstrap,
                sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest(),
            "bootstrap_evidence": decision.bootstrap_evidence,
            "policy_verdict_digest": hashlib.sha256(json.dumps(
                decision.policy_verdict.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")).hexdigest(),
            "policy_name": decision.policy_name,
            "risk_category": decision.risk_category,
            "authorized_by": authorized_by,
            "expected_incumbent_tenure_id": decision.expected_incumbent_tenure_id,
            "expected_revision": decision.expected_revision,
            "expected_domain": decision.expected_domain,
            "expected_context_key": decision.expected_context_key,
            "expected_owner_agent": decision.expected_owner_agent,
            "outcome": decision.outcome.value,
        }

    def _blocked(
        self,
        decision: PromotionDecision,
        reason: CommitFailureReason,
        detail: str,
        authorization_id: Optional[str] = None,
    ) -> CommitResult:
        result = CommitResult(
            success=False,
            challenge_id=decision.challenge_id,
            champion_id=decision.champion_id,
            outcome=PromotionOutcome.BLOCKED,
            failure_reason=reason,
            failure_detail=detail,
            authorization_id=authorization_id,
        )
        self._record_failed_attempt(
            decision, reason.value, detail, authorization_id, authorization_consumed=False
        )
        return result

    def _record_failed_attempt(
        self,
        decision: PromotionDecision,
        reason: str,
        detail: str,
        authorization_id: Optional[str],
        authorization_consumed: bool,
    ) -> None:
        try:
            self._registry.record_promotion_attempt(
                decision_id=decision.decision_id,
                challenge_id=decision.challenge_id,
                champion_id=decision.champion_id,
                authorization_id=authorization_id,
                authorization_consumed=authorization_consumed,
                failure_reason=reason,
                failure_detail=detail,
            )
        except Exception:
            log.exception(
                "failed to persist blocked promotion attempt: decision_id=%s reason=%s",
                decision.decision_id,
                reason,
            )
