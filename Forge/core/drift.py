"""
Forge V2 - Drift Detection (Phase 6)
=====================================
Passive drift detection and policy-based severity classification.

DriftMonitor observes. Orchestrator coordinates. Promotion Gate decides.

The V1 detector computes a standardized mean shift, not PSI. This module
preserves the V1 calculation and identifies its provenance explicitly.
"""

import math
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DriftSeverity(str, Enum):
    NONE = "none"
    WARNING = "warning"
    ACTIONABLE = "actionable"
    INSUFFICIENT_DATA = "insufficient_data"


class DriftMetricMethod(str, Enum):
    STANDARDIZED_MEAN_SHIFT = "standardized_mean_shift"
    PSI = "population_stability_index"


class DriftPolicy(BaseModel):
    """Immutable, versioned thresholds and investigation limits."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    policy_version: str = "1.0.0"
    warning_threshold: float = 0.10
    actionable_threshold: float = 0.25
    minimum_observations: int = 30
    cooldown_seconds: int = 86400
    max_active_investigations: int = 3
    applies_to_domain: Optional[str] = None
    applies_to_champion_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_policy(self):
        if not math.isfinite(self.warning_threshold) or self.warning_threshold < 0:
            raise ValueError("warning_threshold must be finite and non-negative")
        if (
            not math.isfinite(self.actionable_threshold)
            or self.warning_threshold >= self.actionable_threshold
        ):
            raise ValueError(
                "actionable_threshold must be finite and greater than warning_threshold"
            )
        if self.minimum_observations <= 0:
            raise ValueError("minimum_observations must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if self.max_active_investigations < 0:
            raise ValueError("max_active_investigations must be non-negative")
        return self


class DriftPolicyResolver:
    """Resolve champion-specific policies before domain and global policies."""

    def __init__(self, policies: List[DriftPolicy]):
        self._policies = tuple(policies)

    def resolve(self, champion_id: Optional[str], domain: Optional[str]) -> DriftPolicy:
        champion_matches = [
            policy for policy in self._policies
            if policy.applies_to_champion_id == champion_id
            and policy.applies_to_champion_id is not None
        ]
        if len(champion_matches) > 1:
            raise ValueError(f"Multiple drift policies match champion '{champion_id}'")
        if champion_matches:
            return champion_matches[0]

        domain_matches = [
            policy for policy in self._policies
            if policy.applies_to_domain == domain and policy.applies_to_domain is not None
        ]
        if len(domain_matches) > 1:
            raise ValueError(f"Multiple drift policies match domain '{domain}'")
        if domain_matches:
            return domain_matches[0]

        defaults = [
            policy for policy in self._policies
            if policy.applies_to_domain is None and policy.applies_to_champion_id is None
        ]
        if len(defaults) > 1:
            raise ValueError("Multiple global drift policies are configured")
        return defaults[0] if defaults else DriftPolicy()


class FeatureDriftDetail(BaseModel):
    """Immutable drift measurement for one feature."""

    model_config = ConfigDict(frozen=True)

    feature_index: int
    feature_name: Optional[str] = None
    baseline_mean: Optional[float] = None
    baseline_std: Optional[float] = None
    live_mean: Optional[float] = None
    live_std: Optional[float] = None
    shift_value: float
    metric_name: str = "standardized_mean_shift"
    calculation_method: DriftMetricMethod = DriftMetricMethod.STANDARDIZED_MEAN_SHIFT
    severity: DriftSeverity = DriftSeverity.NONE


class DriftReport(BaseModel):
    """Immutable, traceable drift observation."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    model_id: str
    champion_id: Optional[str] = None
    model_version: Optional[str] = None
    model_domain: Optional[str] = None
    observation_count: int = Field(ge=0)
    observation_window_start: Optional[datetime] = None
    observation_window_end: Optional[datetime] = None
    reference_source: str = "training_baseline"
    reference_established_at: Optional[datetime] = None
    policy_id: str
    policy_version: str
    feature_details: List[FeatureDriftDetail] = Field(default_factory=list)
    drifted_feature_indices: List[int] = Field(default_factory=list)
    drifted_feature_names: List[str] = Field(default_factory=list)
    overall_severity: DriftSeverity
    drift_detected: bool
    monitor_version: str = "6.0.0"
    calculated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    v1_raw_output: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_observation_window(self):
        start = self.observation_window_start
        end = self.observation_window_end
        if (start is None) != (end is None):
            raise ValueError("observation window start and end must be supplied together")
        if start is not None and end is not None:
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError("observation window timestamps must be timezone-aware")
            if end < start:
                raise ValueError("observation window end must not precede its start")
        return self


class DriftMonitor:
    """Passive wrapper around the existing V1 ModelRegistry drift detector."""

    def __init__(self, model_registry, policy_resolver: DriftPolicyResolver):
        self._registry = model_registry
        self._resolver = policy_resolver

    def detect(
        self,
        model_id: str,
        live_features: List[List[float]],
        observation_window_start: Optional[datetime] = None,
        observation_window_end: Optional[datetime] = None,
        champion_id: Optional[str] = None,
        feature_names: Optional[List[str]] = None,
    ) -> DriftReport:
        _validate_observation_window(observation_window_start, observation_window_end)
        _validate_feature_batch(live_features)

        model_record = self._registry.get_model(model_id)
        domain = model_record.get("domain") if model_record else None
        model_version = model_record.get("version") if model_record else None
        policy = self._resolver.resolve(champion_id, domain)
        observation_count = len(live_features)
        reference_established = _registered_at(model_record)

        common = {
            "model_id": model_id,
            "champion_id": champion_id,
            "model_version": model_version,
            "model_domain": domain,
            "observation_count": observation_count,
            "observation_window_start": observation_window_start,
            "observation_window_end": observation_window_end,
            "reference_established_at": reference_established,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
        }

        if observation_count < policy.minimum_observations:
            return DriftReport(
                **common,
                overall_severity=DriftSeverity.INSUFFICIENT_DATA,
                drift_detected=False,
                v1_raw_output={
                    "status": "skipped",
                    "message": "insufficient_observations",
                    "required_observations": policy.minimum_observations,
                    "actual_observations": observation_count,
                },
            )

        v1_result = self._registry.detect_drift(model_id, live_features)
        if v1_result.get("status") != "complete":
            return DriftReport(
                **common,
                overall_severity=DriftSeverity.INSUFFICIENT_DATA,
                drift_detected=False,
                v1_raw_output=v1_result,
            )

        if model_record is None:
            raise RuntimeError("ModelRegistry returned a successful drift result without a model")
        baseline = model_record.get("feature_baseline", {})
        means = baseline.get("means", [])
        stds = baseline.get("stds", [])
        feature_count = len(live_features[0])
        if feature_names is not None and len(feature_names) != feature_count:
            raise ValueError(
                f"feature_names has {len(feature_names)} entries for {feature_count} features"
            )

        details: List[FeatureDriftDetail] = []
        drifted_indices: List[int] = []
        drifted_names: List[str] = []
        overall_severity = DriftSeverity.NONE

        for index in range(min(feature_count, len(means))):
            column = [row[index] for row in live_features]
            live_mean = sum(column) / observation_count
            live_variance = sum((value - live_mean) ** 2 for value in column) / observation_count
            live_std = math.sqrt(live_variance)
            baseline_mean = float(means[index])
            baseline_std = float(stds[index]) if index < len(stds) and stds[index] else 1.0
            if not math.isfinite(baseline_mean) or not math.isfinite(baseline_std):
                raise ValueError(f"Non-finite baseline statistics for feature {index}")
            shift = abs(live_mean - baseline_mean) / baseline_std
            if not math.isfinite(shift):
                raise ValueError(f"Non-finite drift measurement for feature {index}")

            if shift >= policy.actionable_threshold:
                severity = DriftSeverity.ACTIONABLE
                drifted_indices.append(index)
                if feature_names is not None:
                    drifted_names.append(feature_names[index])
                overall_severity = DriftSeverity.ACTIONABLE
            elif shift >= policy.warning_threshold:
                severity = DriftSeverity.WARNING
                if overall_severity == DriftSeverity.NONE:
                    overall_severity = DriftSeverity.WARNING
            else:
                severity = DriftSeverity.NONE

            details.append(
                FeatureDriftDetail(
                    feature_index=index,
                    feature_name=feature_names[index] if feature_names is not None else None,
                    baseline_mean=baseline_mean,
                    baseline_std=baseline_std,
                    live_mean=live_mean,
                    live_std=live_std,
                    shift_value=round(shift, 3),
                    calculation_method=DriftMetricMethod.STANDARDIZED_MEAN_SHIFT,
                    severity=severity,
                )
            )

        return DriftReport(
            **common,
            feature_details=details,
            drifted_feature_indices=drifted_indices,
            drifted_feature_names=drifted_names,
            overall_severity=overall_severity,
            drift_detected=bool(v1_result.get("drift_detected", False)),
            v1_raw_output=v1_result,
        )


def _validate_observation_window(
    start: Optional[datetime], end: Optional[datetime]
) -> None:
    if (start is None) != (end is None):
        raise ValueError("observation window start and end must be supplied together")
    if start is not None and end is not None:
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("observation window timestamps must be timezone-aware")
        if end < start:
            raise ValueError("observation window end must not precede its start")


def _validate_feature_batch(live_features: List[List[float]]) -> None:
    if not isinstance(live_features, list):
        raise TypeError("live_features must be a list of feature rows")
    if not live_features:
        return
    if not all(isinstance(row, (list, tuple)) for row in live_features):
        raise TypeError("each live_features row must be a list or tuple")
    feature_count = len(live_features[0])
    if feature_count == 0:
        raise ValueError("live feature rows must contain at least one feature")
    for row_index, row in enumerate(live_features):
        if len(row) != feature_count:
            raise ValueError("live feature rows must all have the same number of features")
        for column_index, value in enumerate(row):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(
                    f"feature value at row {row_index}, column {column_index} must be numeric"
                )
            if not math.isfinite(value):
                raise ValueError(
                    f"feature value at row {row_index}, column {column_index} must be finite"
                )


def _registered_at(model_record: Optional[Dict[str, Any]]) -> Optional[datetime]:
    if model_record is None:
        return None
    timestamp = model_record.get("registered_at")
    if timestamp is None:
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValueError("ModelRegistry registered_at must be a numeric Unix timestamp")
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("ModelRegistry registered_at is outside the supported timestamp range") from exc
