"""Immutable contract models for Chronicle's versioned knowledge records.

Lifecycle status is deliberately kept outside KnowledgeRecord. StateStore
consumers persist one CompositeKnowledgeState per memory ID so a version,
its lifecycle events, and relationships share one atomic key boundary.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KnowledgeStatus(str, Enum):
    PROPOSED = "proposed"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    CONTESTED = "contested"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ValidationOutcome(BaseModel):
    """Result returned by an injected knowledge validation policy."""

    model_config = ConfigDict(frozen=True)

    accepted: bool
    reason: str = ""


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str
    repository: str
    experiment_id: Optional[str] = None
    hypothesis_id: Optional[str] = None
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference_type: str
    reference_id: str
    summary: str


class ConfidenceProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_reliability: float = 0.5
    evidence_strength: float = 0.5
    claim_confidence: float = 0.5
    scope_description: str = ""
    freshness_decay: float = 1.0


class KnowledgeRecord(BaseModel):
    """Immutable content and provenance for a single knowledge version."""

    model_config = ConfigDict(frozen=True)

    memory_id: str
    version_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version_number: int = 1
    pillar: str = "evolutionary"
    domain: str = "general"
    content: str = ""
    summary: str = ""
    tags: Tuple[str, ...] = Field(default_factory=tuple)
    scope: Tuple[Tuple[str, Any], ...] = Field(default_factory=tuple)
    provenance: Provenance
    evidence_links: Tuple[EvidenceLink, ...] = Field(default_factory=tuple)
    confidence_profile: ConfidenceProfile = Field(default_factory=ConfidenceProfile)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("scope", mode="before")
    @classmethod
    def _ensure_deep_immutability(cls, value):
        """Reject mutable containers before Pydantic can coerce them to tuples."""
        if not isinstance(value, tuple):
            raise ValueError("Scope must be a tuple of immutable key/value pairs")
        for pair in value:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("Each scope entry must be a (string, immutable value) tuple")
            key, item = pair
            if not isinstance(key, str):
                raise ValueError(f"Scope keys must be strings. Got {type(key)}")
            if not cls._is_immutable_scope_value(item):
                raise ValueError(
                    f"Scope values must be immutable primitives or tuples. "
                    f"Got {type(item)} for key '{key}'"
                )
        return value

    @staticmethod
    def _is_immutable_scope_value(value: Any) -> bool:
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return True
        if isinstance(value, tuple):
            return all(KnowledgeRecord._is_immutable_scope_value(item) for item in value)
        return False


class LifecycleEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    memory_id: str
    version_id: str
    event_type: str
    previous_status: Optional[KnowledgeStatus] = None
    new_status: KnowledgeStatus
    reason: str = ""
    actor: str = "system"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KnowledgeRelationship(BaseModel):
    model_config = ConfigDict(frozen=True)

    relationship_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    relationship_type: str
    source_version_id: str
    target_version_id: str
    reason: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KnowledgeView(BaseModel):
    """Read-only projection of immutable content plus derived lifecycle state."""

    model_config = ConfigDict(frozen=True)

    record: KnowledgeRecord
    current_status: KnowledgeStatus
    lifecycle_history: Tuple[LifecycleEvent, ...]


def to_v1_compatible_dict(record: KnowledgeRecord, view: KnowledgeView) -> Dict[str, Any]:
    """Convert Chronicle V2 models to the shape accepted by MemoryRecord.from_dict."""
    if view.record.memory_id != record.memory_id or view.record.version_id != record.version_id:
        raise ValueError("KnowledgeView must describe the supplied KnowledgeRecord")
    return {
        "memory_id": record.memory_id,
        "pillar": record.pillar,
        "domain": record.domain,
        "content": record.content,
        "summary": record.summary,
        "embedding": [],
        "source_repository": record.provenance.repository,
        "source_agent": record.provenance.agent_name,
        "created_at": record.created_at.timestamp(),
        "updated_at": record.created_at.timestamp(),
        "used_by": [],
        "evidence": [link.reference_id for link in record.evidence_links],
        "lesson": record.summary,
        "confidence": record.confidence_profile.claim_confidence,
        "verified": view.current_status == KnowledgeStatus.ACCEPTED,
        "successful_uses": 0,
        "total_uses": 0,
        "tags": list(record.tags),
        "version": record.version_number,
        "archived": view.current_status == KnowledgeStatus.SUPERSEDED,
    }


class CompositeKnowledgeState(BaseModel):
    """One atomic StateStore value for all state belonging to a memory ID."""

    model_config = ConfigDict(frozen=True)

    memory_id: str
    current_version_id: str
    version_number: int
    revision: int = 0
    versions: Dict[str, KnowledgeRecord]
    lifecycle_events: Tuple[LifecycleEvent, ...]
    relationships: Tuple[KnowledgeRelationship, ...]

    @model_validator(mode="before")
    @classmethod
    def _rehydrate_serialized_scope_tuples(cls, value):
        """Restore tuple encoding after StateStore's JSON serialization.

        KnowledgeRecord itself remains strict about mutable scope input. This
        conversion is limited to the trusted composite persistence boundary,
        where JSON arrays are the serialized representation of immutable tuples.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data.setdefault("revision", data.get("version_number", 0))
        versions = data.get("versions")
        if isinstance(versions, dict):
            restored = {}
            for version_id, raw_record in versions.items():
                if not isinstance(raw_record, dict):
                    restored[version_id] = raw_record
                    continue
                record = dict(raw_record)
                raw_scope = record.get("scope", ())
                if isinstance(raw_scope, list):
                    record["scope"] = tuple(
                        (entry[0], cls._restore_tuple_value(entry[1]))
                        if isinstance(entry, (list, tuple)) and len(entry) == 2
                        else entry
                        for entry in raw_scope
                    )
                restored[version_id] = record
            data["versions"] = restored
        return data

    @staticmethod
    def _restore_tuple_value(value):
        if isinstance(value, list):
            return tuple(CompositeKnowledgeState._restore_tuple_value(item) for item in value)
        return value
