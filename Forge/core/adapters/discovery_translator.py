"""
Forge V2 - Discovery Translator (Phase 4B)
============================================
Translates V1 MethodDiscovery intelligence into V2 ChallengeRequests.

Principle:
  V1 discovers candidates. V2 controls the scientific process.
  Challenging ≠ Promotion. Migration ≠ Promotion.

The DiscoveryTranslator:
  - Calls V1 MethodDiscovery.discover() (READ-ONLY).
  - Reads V2 ChampionRegistry for the current incumbent.
  - Constructs immutable ChallengeRequest + BenchmarkContext pairs.
  - Does NOT execute experiments during generate_challenges().
  - Does NOT call MethodDiscovery.challenge() (auto-adoption forbidden).
  - Does NOT mutate V1 or V2 champion state during generate_challenges().

Founding a V2 champion requires persisted experiment evidence and signed Aegis approval.
After initialization, V2 ChampionRegistry is authoritative.
V1 preferred_method() becomes discovery/history information only.
"""

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentData,
    ExperimentDomain,
    ExperimentEnvironment,
    ExperimentEvaluation,
    MetricDirection,
    MetricSpec,
)
from Forge.core.benchmark import BenchmarkContext
from Forge.core.challenger import (
    ChallengeRequest,
    ChampionBootstrapRequest,
    ChampionRecord,
    ChampionRegistry,
    default_owner_for_domain,
)
from Forge.core.promotion import AuthorizationToken


# ==============================================================================
# 1. DISCOVERY PROFILE (Typed Input)
# ==============================================================================
class DiscoveryProfile(BaseModel):
    """
    Typed input for the DiscoveryTranslator.
    Provides all information needed to discover candidates and construct challenges.
    """
    task_type: str                          # e.g., "classification", "regression"
    n_samples: int
    n_features: int
    context_key: str                        # Competition arena scope for ChampionRegistry
    domain: str = "ML_SUPERVISED"           # ExperimentDomain value
    owner_agent: Optional[str] = None        # Immutable owner of the champion position
    champion_id: Optional[str] = None        # Required when multiple slots share this scope
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)

    # Dataset information (needed for ExperimentContract construction)
    dataset_path: str
    target_column: str

    # Evaluation configuration (needed for ExperimentContract)
    metric_specs: Dict[str, MetricSpec] = Field(default_factory=dict)

    # Provenance
    created_by: str = "DiscoveryTranslator"

    @model_validator(mode="before")
    @classmethod
    def _resolve_domain_owner(cls, value):
        if isinstance(value, dict) and not value.get("owner_agent"):
            value = dict(value)
            value["owner_agent"] = default_owner_for_domain(value.get("domain", "ML_SUPERVISED"))
        return value

    @field_validator("owner_agent")
    @classmethod
    def _require_domain_owner(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("owner_agent is required for this domain")
        return value.strip()


# ==============================================================================
# 2. DISCOVERY RESULT (Structured Output)
# ==============================================================================
class DiscoveryResult(BaseModel):
    """
    Structured output from generate_challenges().
    Either contains ready-to-execute challenges, or indicates initialization is required.
    """
    initialization_required: bool = False
    initialization_reason: Optional[str] = None

    challenges: List[Tuple[ChallengeRequest, BenchmarkContext]] = Field(default_factory=list)

    # Discovery metadata
    candidates_discovered: int = 0
    candidates_skipped: int = 0
    skipped_reasons: List[str] = Field(default_factory=list)


# ==============================================================================
# 3. DISCOVERY TRANSLATOR
# ==============================================================================
class DiscoveryTranslator:
    """
    Translates V1 MethodDiscovery intelligence into V2 challenge inputs.

    HARD INVARIANTS:
      MAY:
        - Call MethodDiscovery.discover() (read-only)
        - Read V1 preferred-method information during migration
        - Read V2 ChampionRegistry
        - Read ModelRegistry / frozen champion evidence
        - Construct ExperimentContract, ChallengeRequest, BenchmarkContext

      MUST NOT:
        - Call MethodDiscovery.challenge()
        - Mutate V1 champion state
        - Promote candidates
        - Mutate V2 champion state during generate_challenges()
        - Execute experiments during generate_challenges()
        - Bypass MLExperimentAdapter
        - Create a parallel discovery experiment domain
    """

    def __init__(
        self,
        method_discovery,
        champion_registry: ChampionRegistry,
        forge_agent=None,
        experiment_runner=None,
        authorization_verifier=None,
    ):
        """
        Args:
            method_discovery: V1 MethodDiscovery instance (READ-ONLY usage).
            champion_registry: V2 ChampionRegistry (authoritative for V2 champions).
            forge_agent: ForgeAgent used by the standard ML experiment adapter.
            experiment_runner: Durable V2 experiment runner.
        """
        self._discovery = method_discovery
        self._registry = champion_registry
        self._forge_agent = forge_agent
        self._runner = experiment_runner
        self._authorization_verifier = authorization_verifier
        champion_registry.configure_authorization_verifier(authorization_verifier)

    # =========================================================================
    # PUBLIC API: generate_challenges (SIDE-EFFECT FREE)
    # =========================================================================

    def generate_challenges(self, profile: DiscoveryProfile) -> DiscoveryResult:
        """
        Discovers V1 candidates and translates them into V2 challenge inputs.

        This method is SIDE-EFFECT FREE:
          - Does NOT execute experiments.
          - Does NOT mutate V1 or V2 state.
          - Does NOT call MethodDiscovery.challenge().

        If no V2 champion exists for the profile's context_key, returns
        a DiscoveryResult with initialization_required=True.

        Args:
            profile: Typed discovery input.

        Returns:
            DiscoveryResult containing challenges or initialization requirement.
        """
        # Step 1: Check if V2 champion exists
        champion = self._find_champion(profile)
        if champion is None or champion.current_tenure is None:
            return DiscoveryResult(
                initialization_required=True,
                initialization_reason=(
                    f"No V2 champion exists for context_key='{profile.context_key}'. "
                    "Call evolution.bootstrap.prepare, request Aegis authorization for the "
                    "persisted evidence, then call evolution.bootstrap.commit."
                ),
                challenges=[],
            )

        # Step 2: Discover V1 candidates (READ-ONLY)
        discovery_output = self._discovery.discover(
            profile.task_type, profile.n_samples, profile.n_features
        )
        runnable_candidates = list(dict.fromkeys(
            m["method"] for m in discovery_output.get("runnable_now", [])
        ))

        if not runnable_candidates:
            return DiscoveryResult(
                initialization_required=False,
                challenges=[],
                candidates_discovered=0,
                skipped_reasons=["No runnable candidates discovered by V1 MethodDiscovery."],
            )

        # Step 3: Build BenchmarkContext from V2 champion's evidence
        benchmark_context = self._build_benchmark_context(champion)
        if benchmark_context is None:
            return DiscoveryResult(
                initialization_required=True,
                initialization_reason=(
                    f"V2 champion '{champion.champion_id}' exists but has no reference metrics "
                    f"in its tenure evidence. Re-initialization may be required."
                ),
                challenges=[],
            )

        # Step 4: Construct ChallengeRequest for each candidate
        challenges: List[Tuple[ChallengeRequest, BenchmarkContext]] = []
        skipped_reasons: List[str] = []
        current_candidate = champion.current_tenure.candidate_id

        family_members = [
            candidate_method for candidate_method in runnable_candidates
            if candidate_method != current_candidate
        ]
        family_size = max(1, len(family_members))
        family_id = uuid.uuid4().hex
        for candidate_method in runnable_candidates:
            # Skip if candidate is the same as the current champion
            if candidate_method == current_candidate:
                skipped_reasons.append(
                    f"Candidate '{candidate_method}' is the current champion; skipping."
                )
                continue

            # Construct the ExperimentContract
            contract = self._build_experiment_contract(
                profile,
                champion,
                candidate_method,
                family_size=family_size,
                family_id=family_id,
                family_members=family_members,
            )

            # Construct the ChallengeRequest
            request = ChallengeRequest(
                champion_id=champion.champion_id,
                domain=profile.domain,
                context_key=profile.context_key,
                owner_agent=profile.owner_agent,
                candidate_id=candidate_method,
                candidate_config={"backend": candidate_method},
                experiment=contract,
                created_by=profile.created_by,
            )

            challenges.append((request, benchmark_context))

        return DiscoveryResult(
            initialization_required=False,
            challenges=challenges,
            candidates_discovered=len(runnable_candidates),
            candidates_skipped=len(skipped_reasons),
            skipped_reasons=skipped_reasons,
        )

    # =========================================================================
    # PUBLIC API: governed champion bootstrap
    # =========================================================================

    def prepare_v2_champion(
        self, profile: DiscoveryProfile, backend: str = "sklearn"
    ) -> ChampionBootstrapRequest:
        """Run and persist founding evidence; this method never installs a champion."""
        if self._forge_agent is None:
            raise RuntimeError(
                "prepare_v2_champion requires a forge_agent instance."
            )
        if self._runner is None:
            raise RuntimeError(
                "prepare_v2_champion requires an experiment_runner instance."
            )
        if backend not in self._forge_agent._backends:
            raise ValueError(f"Unknown or unavailable founding backend: {backend}")
        if profile.champion_id:
            existing = self._registry.get_champion(profile.champion_id)
            if existing is None or (existing.domain, existing.context_key) != (profile.domain, profile.context_key):
                raise ValueError("champion_id does not identify a slot in the requested domain/context")
            if existing.owner_agent != profile.owner_agent:
                raise PermissionError("champion slot is owned by another agent")
            if any(
                item.current_tenure is not None
                for item in self._registry.list_champions(domain=profile.domain)
                if item.context_key == profile.context_key and item.champion_id != existing.champion_id
            ):
                raise ValueError("Another incumbent already exists for this domain/context")
        else:
            matching = [item for item in self._registry.list_champions(domain=profile.domain)
                        if item.context_key == profile.context_key]
            incumbents = [item for item in matching if item.current_tenure is not None]
            if incumbents:
                raise ValueError(f"An incumbent already exists for this domain/context: {incumbents[0].champion_id}")
            if len(matching) > 1:
                raise ValueError("Multiple empty champion slots match this context; provide champion_id explicitly")
            existing = matching[0] if matching else None
        if existing is not None and existing.current_tenure is not None:
            raise ValueError("Cannot bootstrap a champion slot that already has an incumbent")
        if existing is not None and existing.owner_agent != profile.owner_agent:
            raise PermissionError("champion slot is owned by another agent")
        if existing is None:
            existing = self._registry.register_champion(
                profile.domain, profile.context_key, owner_agent=profile.owner_agent
            )

        contract = self._build_founding_experiment_contract(profile, backend)

        experiment_id = self._runner.submit_experiment(contract)
        stored_contract = self._runner.get_experiment(experiment_id)
        if stored_contract is None:
            raise RuntimeError("Submitted founding experiment was not found in the durable ledger")
        result = self._runner.execute_experiment(experiment_id)

        if result.status.value != "COMPLETED":
            raise RuntimeError(
                f"Founding experiment did not complete: {result.error_message or result.status.value}"
            )
        metrics = result.metrics.get("treatment") or result.metrics.get("baseline")
        if not isinstance(metrics, dict) or not metrics:
            raise RuntimeError("Founding experiment completed without a usable metric snapshot")
        request = ChampionBootstrapRequest(
            champion_id=existing.champion_id,
            domain=existing.domain,
            context_key=existing.context_key,
            owner_agent=existing.owner_agent,
            candidate_id=backend,
            candidate_config={"backend": backend},
            experiment_id=experiment_id,
            run_id=result.run_id,
            result_id=result.result_id,
            metrics=metrics,
            metric_bootstrap=result.metrics.get("bootstrap", {}).get("treatment", {}),
            bootstrap_evidence={
                "method": result.metrics.get("bootstrap", {}).get("method"),
                "replicates": result.metrics.get("bootstrap", {}).get("replicates"),
                "sample_count": result.metrics.get("bootstrap", {}).get("sample_count"),
                "random_seed": result.evidence.get("random_seed"),
                "dataset_fingerprint": result.evidence.get("dataset_fingerprint"),
                "evaluation_partition": result.evidence.get("evaluation_partition"),
            },
            dataset_fingerprint=stored_contract.data.data_fingerprint,
            expected_revision=existing.revision,
            created_by=profile.created_by,
        )
        self._registry.record_champion_bootstrap(request)
        return request

    @staticmethod
    def bootstrap_authorization_claims(request: ChampionBootstrapRequest, authorized_by: str) -> Dict[str, Any]:
        evidence = {
            "bootstrap_id": request.bootstrap_id,
            "champion_id": request.champion_id,
            "domain": request.domain,
            "context_key": request.context_key,
            "owner_agent": request.owner_agent,
            "candidate_id": request.candidate_id,
            "candidate_config": request.candidate_config,
            "experiment_id": request.experiment_id,
            "run_id": request.run_id,
            "result_id": request.result_id,
            "metrics": request.metrics,
            "metric_bootstrap": request.metric_bootstrap,
            "bootstrap_evidence": request.bootstrap_evidence,
            "dataset_fingerprint": request.dataset_fingerprint,
            "expected_revision": request.expected_revision,
            "created_by": request.created_by,
            "created_at": request.created_at.isoformat(),
        }
        return {
            "action": "initialize_champion",
            "bootstrap_id": request.bootstrap_id,
            "champion_id": request.champion_id,
            "domain": request.domain,
            "context_key": request.context_key,
            "owner_agent": request.owner_agent,
            "candidate_id": request.candidate_id,
            "experiment_id": request.experiment_id,
            "run_id": request.run_id,
            "result_id": request.result_id,
            "metrics_digest": hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "expected_revision": request.expected_revision,
            "risk_category": "initialization",
            "authorized_by": authorized_by,
        }

    def commit_v2_champion_bootstrap(self, bootstrap_id: str, authorization: AuthorizationToken) -> ChampionRecord:
        """Delegate approval verification and mutation to the registry boundary."""
        return self._registry.commit_champion_bootstrap(bootstrap_id, authorization)

    def initialize_v2_champion(self, profile: DiscoveryProfile) -> ChampionRecord:
        """Unsafe one-phase initialization is intentionally disabled."""
        raise RuntimeError(
            "Direct initialization is disabled. Use prepare_v2_champion, obtain signed Aegis "
            "authorization for the persisted claims, then commit_v2_champion_bootstrap."
        )

    # =========================================================================
    # PRIVATE HELPERS
    # =========================================================================

    def _find_champion(self, profile: DiscoveryProfile) -> Optional[ChampionRecord]:
        """Finds the V2 champion for the given profile's context_key."""
        champions = self._registry.list_champions(domain=profile.domain)
        matches = [
            champion for champion in champions
            if champion.context_key == profile.context_key
            and champion.owner_agent == profile.owner_agent
        ]
        if profile.champion_id:
            return next((champion for champion in matches if champion.champion_id == profile.champion_id), None)
        incumbents = [champion for champion in matches if champion.current_tenure is not None]
        if len(incumbents) > 1:
            raise ValueError("Multiple active champion records match this domain/context")
        if incumbents:
            return incumbents[0]
        return matches[0] if len(matches) == 1 else None

    def _build_benchmark_context(self, champion: ChampionRecord) -> Optional[BenchmarkContext]:
        """
        Builds a BenchmarkContext from the champion's tenure evidence.
        Returns None if no reference metrics are available.
        """
        if champion.current_tenure is None:
            return None

        evidence = champion.current_tenure.evidence_reference
        founding_metrics = evidence.get("benchmark_metrics") or evidence.get("founding_metrics", {})

        if not founding_metrics:
            return None

        return BenchmarkContext(
            reference_id=champion.champion_id,
            reference_type="v2_champion",
            reference_metrics=founding_metrics,
            reference_bootstrap=evidence.get("metric_bootstrap", {}),
            bootstrap_evidence=evidence.get("bootstrap_evidence", {}),
            resolved_by="DiscoveryTranslator",
            provenance={
                "champion_id": champion.champion_id,
                "candidate_id": champion.current_tenure.candidate_id,
                "tenure_id": champion.current_tenure.tenure_id,
                "source_experiment_id": champion.current_tenure.source_experiment_id,
                "source_challenge_id": champion.current_tenure.source_challenge_id,
            },
        )

    def _build_experiment_contract(
        self,
        profile: DiscoveryProfile,
        champion: ChampionRecord,
        candidate_method: str,
        family_size: int = 1,
        family_id: Optional[str] = None,
        family_members: Optional[List[str]] = None,
    ) -> ExperimentContract:
        """
        Constructs an ExperimentContract for a challenger match.
        Baseline = current champion configuration.
        Treatment = discovered candidate configuration.
        """
        # Baseline: the current champion's configuration
        baseline_config = dict(champion.current_tenure.candidate_config) if champion.current_tenure else {}

        # Treatment: the discovered candidate
        treatment_config = {"backend": candidate_method}

        return ExperimentContract(
            hypothesis_id=None,
            domain=ExperimentDomain(profile.domain),
            baseline_config=baseline_config,
            treatment_config=treatment_config,
            variables={
                "challenge_type": "method_discovery",
                "candidate_method": candidate_method,
                "task_type": profile.task_type,
            },
            ablation_targets=[],
            data=ExperimentData(
                dataset_path=profile.dataset_path,
                target_column=profile.target_column,
            ),
            environment=ExperimentEnvironment(random_seed=profile.random_seed),
            evaluation=ExperimentEvaluation(
                metric_specs=profile.metric_specs,
                multiple_comparison_family_size=family_size,
                comparison_family_id=family_id,
                comparison_family_members=family_members or [candidate_method],
            ),
            created_by=profile.created_by,
        )

    def _build_founding_experiment_contract(
        self,
        profile: DiscoveryProfile,
        backend: str,
    ) -> ExperimentContract:
        """
        Constructs a degenerate ExperimentContract for founding champion establishment.
        Baseline == Treatment (both use the explicitly selected backend).
        This establishes the champion's reference metrics through the standard V2 pipeline.
        """
        config = {"backend": backend}

        return ExperimentContract(
            hypothesis_id=None,
            domain=ExperimentDomain(profile.domain),
            baseline_config=config,
            treatment_config=config,  # Degenerate: same as baseline
            variables={
                "purpose": "v2_champion_initialization",
                "founding_backend": backend,
                "task_type": profile.task_type,
            },
            ablation_targets=[],
            data=ExperimentData(
                dataset_path=profile.dataset_path,
                target_column=profile.target_column,
            ),
            environment=ExperimentEnvironment(random_seed=profile.random_seed),
            evaluation=ExperimentEvaluation(
                metric_specs=profile.metric_specs,
            ),
            created_by=f"{profile.created_by}.migration",
        )
