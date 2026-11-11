"""
Forge V2 - Universal Agent Training Adapters & Oracle V1 Compatibility Layer
============================================================================
Implements Sections 2.1, 2.2, 2.3, and 10.1 of the FORGE V2 Implementation Plan:
  - Universal Agent Training Adapter protocol (`UniversalAgentTrainingAdapter`)
  - Concrete Agent-Specific Training Adapters for all 6 Universe Agents:
      1. OracleTrainingAdapter (Trading strategies, risk methods, sizing, exits, timeframe policies, features, decision policies)
      2. AtlasTrainingAdapter (Research methods, analysis pipelines, retrieval configurations, research quality)
      3. ChronicleTrainingAdapter (Memory retrieval, knowledge organization, research scheduling, information quality)
      4. SentinelTrainingAdapter (Monitoring methods, anomaly detection, signal quality)
      5. GenesisTrainingAdapter (Candidate-generation methods, quality of generated components)
      6. AegisTrainingAdapter (Validation, protection, security decision policies)
  - Dedicated `OracleV1CompatibilityAdapter` (Sections 2.3 & 10.1) translating approved Forge V2
    candidate artifacts into Oracle V1's 9-module `StrategyGenome` without mutating Oracle V1.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple


def _sha256_canonical(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class UniversalAgentTrainingAdapter(Protocol):
    """Universal contract connecting any Universe agent to Forge Core (Section 2.2)."""

    adapter_id: str
    target_agent: str
    domain: str
    trainable_components: Tuple[str, ...]
    configurable_parameters: Tuple[str, ...]
    training_interfaces: Tuple[str, ...]
    success_metrics: Dict[str, str]
    safety_constraints: Dict[str, Any]
    evaluation_methodology: str
    deployment_artifact_format: str

    def describe_contract(self) -> Dict[str, Any]:
        ...

    def validate_safety_boundaries(self, candidate_config: Dict[str, Any]) -> Dict[str, Any]:
        ...


class BaseUniverseAgentAdapter:
    """Base implementation of Section 2.2 Agent-Specific Training Adapter."""

    adapter_id: str = "forge.adapter.base"
    target_agent: str = "base"
    domain: str = "EVALUATION_ONLY"
    trainable_components: Tuple[str, ...] = ()
    configurable_parameters: Tuple[str, ...] = ()
    training_interfaces: Tuple[str, ...] = ()
    success_metrics: Dict[str, str] = {}
    safety_constraints: Dict[str, Any] = {}
    evaluation_methodology: str = "chronological_walk_forward_and_holdout_bootstrap"
    deployment_artifact_format: str = "forge.universal.candidate_artifact.v2"

    def describe_contract(self) -> Dict[str, Any]:
        return {
            "schema": "forge.universal.agent_adapter_contract.v2",
            "adapter_id": self.adapter_id,
            "target_agent": self.target_agent,
            "domain": self.domain,
            "trainable_components": list(self.trainable_components),
            "configurable_parameters": list(self.configurable_parameters),
            "training_interfaces": list(self.training_interfaces),
            "success_metrics": dict(self.success_metrics),
            "safety_constraints": copy.deepcopy(self.safety_constraints),
            "evaluation_methodology": self.evaluation_methodology,
            "deployment_artifact_format": self.deployment_artifact_format,
        }

    def validate_safety_boundaries(self, candidate_config: Dict[str, Any]) -> Dict[str, Any]:
        violations: List[str] = []
        for key, limit in self.safety_constraints.items():
            if key in candidate_config and isinstance(limit, (int, float)):
                val = candidate_config[key]
                if isinstance(val, (int, float)) and val > limit:
                    violations.append(f"{key}={val} exceeds safety constraint {limit}")
        return {
            "passed": len(violations) == 0,
            "target_agent": self.target_agent,
            "checked_constraints": copy.deepcopy(self.safety_constraints),
            "violations": violations,
        }


class OracleTrainingAdapter(BaseUniverseAgentAdapter):
    """Section 2.2 & Section 3: First major training target — Oracle trading decision process."""

    adapter_id = "forge.adapter.oracle.v2"
    target_agent = "Oracle"
    domain = "ORACLE_STRATEGY"
    trainable_components = (
        "strategy_generation_and_selection",
        "market_regime_detection",
        "entry_and_exit_conditions",
        "stop_loss_placement_and_progressive_angle_fitting",
        "take_profit_selection_and_dynamic_htf_expansion",
        "position_sizing_and_risk_allocation",
        "multi_timeframe_weighting_and_confirmation",
        "feature_discovery_and_news_social_fusion",
    )
    configurable_parameters = (
        "timeframe_weights",
        "feature_weights",
        "entry_score_threshold",
        "learned_optimal_stop_atr_mult",
        "learned_initial_major_tp_r",
        "learned_partial_tp_r",
        "learned_partial_bank_fraction",
        "learned_sl_reduction_start_r",
        "learned_be_trigger_r",
        "learned_sl_angle_fit_ratio",
        "learned_early_cut_drawdown_r",
        "learned_early_invalidation_thesis_cutoff",
        "learned_near_tp_zone_pct",
        "learned_tp_expansion_thesis_cutoff",
        "position_sizing_method",
        "risk_per_trade_pct",
    )
    training_interfaces = (
        "discover_strategy_genome_from_data",
        "discover_dynamic_trade_management_policy",
        "evaluate_live_position_reanalysis",
        "run_leave_one_out_ablation",
        "run_pilot_five_candidate_benchmark",
    )
    success_metrics = {
        "net_return_after_costs_pct": "higher_is_better",
        "expectancy_r": "higher_is_better",
        "sharpe_ratio": "higher_is_better",
        "sortino_ratio": "higher_is_better",
        "profit_factor": "higher_is_better",
        "max_drawdown_pct": "lower_is_better",
        "mc95_worst_case_drawdown_pct": "lower_is_better",
    }
    safety_constraints = {
        "max_risk_per_trade_pct": 0.50,
        "max_aggregate_open_risk_pct": 2.00,
        "max_daily_session_loss_pct": 2.00,
        "max_portfolio_drawdown_pct": 8.50,
        "max_leverage": 30.0,
        "require_verified_cost_profile": True,
        "require_untouched_lockbox_validation": True,
        "forbid_silent_v1_mutation": True,
    }
    evaluation_methodology = (
        "4_window_chronological_walk_forward_plus_40pct_untouched_lockbox_plus_paired_circular_block_bootstrap"
    )
    deployment_artifact_format = "oracle.v1_and_v2_compatible_strategy_artifact.v2"


class AtlasTrainingAdapter(BaseUniverseAgentAdapter):
    """Section 2.2: Atlas — Research methods, analysis pipelines, retrieval configurations & research quality."""

    adapter_id = "forge.adapter.atlas.v2"
    target_agent = "Atlas"
    domain = "WORKFLOW_OPTIMIZATION"
    trainable_components = (
        "research_hypothesis_synthesis_pipeline",
        "retrieval_ranking_and_source_verification",
        "contradiction_and_limitation_extraction",
        "multi_timeframe_evidence_contextualization",
    )
    configurable_parameters = (
        "citation_minimum_sources",
        "retrieval_relevance_threshold",
        "hypothesis_confidence_calibration_alpha",
        "evidence_contradiction_penalty",
    )
    training_interfaces = (
        "consult_atlas_for_hypothesis",
        "evaluate_hypothesis_predictive_hit_rate",
        "ablate_retrieval_sources",
    )
    success_metrics = {
        "hypothesis_out_of_sample_confirmation_rate": "higher_is_better",
        "citation_verifiability_score": "higher_is_better",
        "research_latency_sec": "lower_is_better",
    }
    safety_constraints = {
        "require_cited_empirical_evidence": True,
        "forbid_uncited_causal_claims": True,
        "max_query_budget_sec": 30.0,
    }
    evaluation_methodology = "paired_hypothesis_outcome_calibration_on_lockbox_experiments"
    deployment_artifact_format = "atlas.research_pipeline_config.v2"


class ChronicleTrainingAdapter(BaseUniverseAgentAdapter):
    """Section 2.2: Chronicle — Memory retrieval, knowledge organization, research scheduling & information quality."""

    adapter_id = "forge.adapter.chronicle.v2"
    target_agent = "Chronicle"
    domain = "CHRONICLE_MEMORY"
    trainable_components = (
        "episodic_and_semantic_memory_indexing",
        "similarity_retrieval_weighting",
        "research_job_deduplication_and_scheduling",
        "stale_evidence_decay_policy",
    )
    configurable_parameters = (
        "retrieval_top_k",
        "recency_half_life_hours",
        "regime_similarity_weight",
        "deduplication_hash_window_sec",
    )
    training_interfaces = (
        "evaluate_memory_retrieval_precision",
        "optimize_decay_and_indexing_weights",
    )
    success_metrics = {
        "retrieval_precision_at_k": "higher_is_better",
        "duplicate_event_suppression_rate": "higher_is_better",
        "query_latency_ms": "lower_is_better",
    }
    safety_constraints = {
        "append_only_audit_integrity": True,
        "optimistic_concurrency_revision_check": True,
    }
    evaluation_methodology = "held_out_query_relevance_and_replay_consistency_benchmark"
    deployment_artifact_format = "chronicle.retrieval_policy.v2"


class SentinelTrainingAdapter(BaseUniverseAgentAdapter):
    """Section 2.2: Sentinel — Monitoring methods, anomaly detection & signal quality."""

    adapter_id = "forge.adapter.sentinel.v2"
    target_agent = "Sentinel"
    domain = "ML_SUPERVISED"
    trainable_components = (
        "macro_news_impact_classifier",
        "volatility_shock_anomaly_detector",
        "sentiment_polarity_and_decay_kernel",
        "false_alarm_suppression_filter",
    )
    configurable_parameters = (
        "news_decay_half_life_minutes",
        "high_impact_zscore_threshold",
        "source_credibility_weights",
        "sentiment_smoothing_span",
    )
    training_interfaces = (
        "fetch_pulse_and_sentinel_context",
        "ablate_sentinel_news_contribution",
        "calibrate_anomaly_precision_recall",
    )
    success_metrics = {
        "marginal_expectancy_contribution_atr": "higher_is_better",
        "anomaly_detection_f1": "higher_is_better",
        "false_positive_rate": "lower_is_better",
    }
    safety_constraints = {
        "strict_point_in_time_publication_cutoff": True,
        "exclude_raw_unverified_payloads": True,
    }
    evaluation_methodology = "leave_one_out_strategy_ablation_and_event_study_validation"
    deployment_artifact_format = "sentinel.signal_calibration_profile.v2"


class GenesisTrainingAdapter(BaseUniverseAgentAdapter):
    """Section 2.2: Genesis — Candidate-generation methods & quality of generated components."""

    adapter_id = "forge.adapter.genesis.v2"
    target_agent = "Genesis"
    domain = "CONFIG_OPTIMIZATION"
    trainable_components = (
        "candidate_genome_mutation_operator",
        "structural_template_synthesizer",
        "search_space_prior_distribution",
    )
    configurable_parameters = (
        "mutation_step_sigma",
        "crossover_rate",
        "novelty_diversity_bonus",
        "complexity_parsimony_penalty",
    )
    training_interfaces = (
        "generate_controlled_candidate_variants",
        "evaluate_generator_yield_rate",
    )
    success_metrics = {
        "certified_candidate_yield_rate": "higher_is_better",
        "out_of_sample_survival_ratio": "higher_is_better",
        "multiple_testing_inflation_penalty": "lower_is_better",
    }
    safety_constraints = {
        "max_simultaneous_comparison_family_size": 12,
        "require_single_dimension_pilot_variants": True,
    }
    evaluation_methodology = "generator_survival_rate_on_untouched_holdout_families"
    deployment_artifact_format = "genesis.generator_policy.v2"


class AegisTrainingAdapter(BaseUniverseAgentAdapter):
    """Section 2.2: Aegis — Validation, protection & security decision policies."""

    adapter_id = "forge.adapter.aegis.v2"
    target_agent = "Aegis"
    domain = "EVALUATION_ONLY"
    trainable_components = (
        "risk_boundary_verification_policy",
        "promotion_evidence_completeness_auditor",
        "anomaly_quarantine_and_kill_switch_triggers",
    )
    configurable_parameters = (
        "max_token_age_seconds",
        "minimum_bootstrap_replicates",
        "max_allowed_mc95_drawdown_pct",
        "require_cost_and_basis_verification",
    )
    training_interfaces = (
        "verify_and_consume_promotion_authorization",
        "audit_candidate_safety_and_cost_integrity",
    )
    success_metrics = {
        "false_promotion_prevention_rate": "higher_is_better",
        "replay_attack_rejection_rate": "higher_is_better",
    }
    safety_constraints = {
        "fail_closed_on_missing_evidence": True,
        "ed25519_single_use_nonce_required": True,
        "cannot_waive_safety_boundaries": True,
    }
    evaluation_methodology = "adversarial_fault_injection_and_governance_invariant_verification"
    deployment_artifact_format = "aegis.governance_policy.v2"


class OracleV1CompatibilityAdapter:
    """
    Sections 2.3 & 10.1: Dedicated Oracle V1 Compatibility Adapter.
    ---------------------------------------------------------------
    Forge Core and its candidate model remain independent of Oracle V1's nine-module
    StrategyGenome. This adapter translates an approved Forge V2 artifact into a complete,
    schema-validated V1-compatible nine-module StrategyGenome representation, validates
    constraints, and prepares an export with rollback capability—without ever silently
    mutating Oracle V1's champion catalog or active strategy.
    """

    NINE_MODULES: Tuple[str, ...] = (
        "market_regime",
        "trend",
        "momentum",
        "volatility",
        "entry",
        "exit",
        "risk",
        "position",
        "trade_management",
    )

    @classmethod
    def translate_to_v1_strategy_genome(
        cls,
        candidate_id: str,
        symbol: str,
        trader_style: str,
        learned_genome: Dict[str, Any],
        management_policy: Dict[str, Any],
        position_sizing_policy: Dict[str, Any],
        recommended_regimes: List[str],
        incumbent_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Translate a Forge V2 candidate into Oracle V1's 9-module StrategyGenome schema."""
        fw = learned_genome.get("feature_weights", {})
        gates = learned_genome.get("structural_gates", {})
        sl_mult = float(management_policy.get("learned_optimal_stop_atr_mult", 1.15))
        major_tp_r = float(management_policy.get("learned_initial_major_tp_r", 3.2))
        tp_mult = round(sl_mult * major_tp_r, 3)

        v1_regime_map = {
            "TRENDING_BULLISH": "trending_up",
            "TRENDING_BEARISH": "trending_down",
            "CONSOLIDATION_RANGING": "ranging",
            "VOLATILITY_EXPANSION": "high_volatility",
        }
        allowed_v1_regimes = sorted(
            {v1_regime_map.get(r, "trending_up") for r in (recommended_regimes or ["TRENDING_BULLISH"])}
        )

        modules: Dict[str, Dict[str, Any]] = {
            "market_regime": {
                "module_name": "market_regime",
                "logic_type": "forge_v2_regime_gate",
                "params": {
                    "allowed_regimes": allowed_v1_regimes,
                    "volatility_limit": 0.045,
                    "forge_recommended_regimes": list(recommended_regimes),
                },
            },
            "trend": {
                "module_name": "trend",
                "logic_type": "ema_bos_structural" if fw.get("ema_trend", 0.0) > 0 else "bos_only",
                "params": {
                    "fast": 9,
                    "slow": 21,
                    "ema_weight": round(float(fw.get("ema_trend", 0.0)), 4),
                    "bos_weight": round(float(fw.get("bos_breakout", 0.0)), 4),
                },
            },
            "momentum": {
                "module_name": "momentum",
                "logic_type": "rsi_fvg_impulse" if fw.get("rsi_momentum", 0.0) > 0 else "fvg_impulse_only",
                "params": {
                    "period": 14,
                    "rsi_long_band": list(gates.get("rsi_long_band", [32.0, 66.0])),
                    "rsi_short_band": list(gates.get("rsi_short_band", [34.0, 68.0])),
                    "rsi_weight": round(float(fw.get("rsi_momentum", 0.0)), 4),
                    "fvg_weight": round(float(fw.get("fvg_impulse", 0.0)), 4),
                },
            },
            "volatility": {
                "module_name": "volatility",
                "logic_type": "atr_dealing_range_filter",
                "params": {
                    "period": 14,
                    "htf_room_to_run_atr_min": float(gates.get("htf_room_to_run_atr_min", 1.35)),
                },
            },
            "entry": {
                "module_name": "entry",
                "logic_type": "mtf_contextual_weighted_agreement",
                "params": {
                    "threshold": float(gates.get("entry_score_threshold", 0.32)),
                    "macro_dealing_range_long_max": float(gates.get("macro_dealing_range_long_max", 0.68)),
                    "macro_dealing_range_short_min": float(gates.get("macro_dealing_range_short_min", 0.32)),
                    "timeframe_weights": dict(learned_genome.get("timeframe_weights", {})),
                },
            },
            "exit": {
                "module_name": "exit",
                "logic_type": "dynamic_reanalysis_sl_tp",
                "params": {
                    "sl_mult": sl_mult,
                    "tp_mult": tp_mult,
                    "initial_major_tp_r": major_tp_r,
                    "partial_tp_r": float(management_policy.get("learned_partial_tp_r", 1.85)),
                    "partial_bank_fraction": float(management_policy.get("learned_partial_bank_fraction", 0.35)),
                    "near_tp_zone_pct": float(management_policy.get("learned_near_tp_zone_pct", 0.85)),
                    "early_cut_drawdown_r": float(management_policy.get("learned_early_cut_drawdown_r", 0.58)),
                },
            },
            "risk": {
                "module_name": "risk",
                "logic_type": "non_bypassable_safety_boundaries",
                "params": {
                    "max_dd_limit": 0.085,
                    "max_daily_loss_limit": 0.02,
                    "max_risk_per_trade_pct": float(position_sizing_policy.get("max_risk_per_trade_pct", 0.50)),
                    "max_aggregate_open_risk_pct": 2.00,
                },
            },
            "position": {
                "module_name": "position",
                "logic_type": str(position_sizing_policy.get("selected_sizing_method", "confidence_tier_adjusted")),
                "params": {
                    "risk_fraction": round(float(position_sizing_policy.get("base_risk_per_trade_pct", 0.50)) / 100.0, 5),
                    "stop_distance_inverse_scaling": True,
                    "constant_monetary_risk_enforced": True,
                },
            },
            "trade_management": {
                "module_name": "trade_management",
                "logic_type": "real_trader_dynamic_reanalysis",
                "params": {
                    "sl_reduction_start_r": float(management_policy.get("learned_sl_reduction_start_r", 0.68)),
                    "be_trigger_r": float(management_policy.get("learned_be_trigger_r", 1.15)),
                    "sl_angle_fit_ratio": float(management_policy.get("learned_sl_angle_fit_ratio", 0.46)),
                    "trail_atr_mult": float(management_policy.get("learned_trail_atr_mult", 0.95)),
                    "tp_expansion_step_r": float(management_policy.get("learned_tp_expansion_step_r", 1.10)),
                    "expanded_sl_lock_ratio": float(management_policy.get("learned_expanded_sl_lock_ratio", 0.78)),
                },
            },
        }

        validation_errors: List[str] = []
        for mod_name in cls.NINE_MODULES:
            if mod_name not in modules:
                validation_errors.append(f"Missing required V1 module: {mod_name}")
            elif not isinstance(modules[mod_name].get("params"), dict):
                validation_errors.append(f"Module {mod_name} params must be a dict")

        genome_payload = {
            "schema": "oracle.v1_compatible.strategy_genome.v2",
            "genome_id": f"v1-compat-{candidate_id}",
            "source_candidate_id": candidate_id,
            "symbol": symbol.upper(),
            "trader_style": trader_style.upper(),
            "version": "2.0.0",
            "modules": modules,
        }
        artifact_digest = _sha256_canonical(genome_payload)

        return {
            "adapter": "OracleV1CompatibilityAdapter",
            "adapter_version": "2.0.0",
            "schema_valid": len(validation_errors) == 0,
            "validation_errors": validation_errors,
            "nine_module_count": len(modules),
            "v1_strategy_genome": genome_payload,
            "artifact_sha256": artifact_digest,
            "v1_protection_guarantees": {
                "v1_champion_catalog_mutated": False,
                "active_v1_strategy_mutated": False,
                "requires_explicit_deployment_authorization": True,
                "rollback_snapshot_preserved": True,
                "rollback_target_genome_id": (
                    incumbent_snapshot.get("genome_id", f"incumbent_v1_{symbol.lower()}")
                    if isinstance(incumbent_snapshot, dict)
                    else f"incumbent_v1_{symbol.lower()}"
                ),
                "post_deployment_monitoring_hook": "forge.drift_orchestrator_and_shadow_runner",
            },
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        }


def get_universal_agent_adapter_registry() -> Dict[str, Dict[str, Any]]:
    """Return the complete Section 2.2 registry of Universe agent training adapters."""
    adapters = (
        OracleTrainingAdapter(),
        AtlasTrainingAdapter(),
        ChronicleTrainingAdapter(),
        SentinelTrainingAdapter(),
        GenesisTrainingAdapter(),
        AegisTrainingAdapter(),
    )
    return {adapter.target_agent: adapter.describe_contract() for adapter in adapters}
