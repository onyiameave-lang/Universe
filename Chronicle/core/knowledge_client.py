"""Explicit, revision-checked API for Chronicle's versioned knowledge state."""

import logging
from typing import Any, Dict, List, Optional, Protocol

from Chronicle.core.knowledge_models import (
    CompositeKnowledgeState,
    KnowledgeRecord,
    KnowledgeRelationship,
    KnowledgeStatus,
    KnowledgeView,
    LifecycleEvent,
    ValidationOutcome,
)
from Chronicle.core.state_store import StateStore
from Chronicle.core.embeddings import cosine_similarity, get_embedding_model


log = logging.getLogger("chronicle.knowledge_client")


class ValidationPolicy(Protocol):
    def validate(self, record: KnowledgeRecord) -> ValidationOutcome: ...


class ChronicleClient:
    """Persist knowledge records and ledger changes as one CAS-protected value."""

    def __init__(
        self,
        state_store: StateStore,
        validation_policy: ValidationPolicy,
        embedding_model=None,
    ):
        self._store = state_store
        self._policy = validation_policy
        self._embedding_model = embedding_model or get_embedding_model()

    def _key(self, memory_id: str) -> str:
        return f"knowledge:{memory_id}"

    def _get_state(self, memory_id: str) -> Optional[CompositeKnowledgeState]:
        raw = self._store.get(self._key(memory_id))
        if raw is None:
            return None
        return CompositeKnowledgeState(**raw)

    def _save_state(
        self,
        state: CompositeKnowledgeState,
        expected_revision: int,
    ) -> bool:
        return self._store.compare_and_set_version(
            key=self._key(state.memory_id),
            expected_version=expected_revision,
            new_value=state.model_dump(mode="json"),
            owner="chronicle_client",
        )

    @staticmethod
    def _with_revision(state: CompositeKnowledgeState, **changes) -> CompositeKnowledgeState:
        values = state.model_dump()
        values.update(changes)
        values["revision"] = state.revision + 1
        return CompositeKnowledgeState(**values)

    def propose_knowledge(self, record: KnowledgeRecord) -> str:
        if self._get_state(record.memory_id) is not None:
            raise ValueError(f"Record {record.memory_id} already exists. Use supersede_knowledge().")
        event = LifecycleEvent(
            memory_id=record.memory_id,
            version_id=record.version_id,
            event_type="proposed",
            new_status=KnowledgeStatus.PROPOSED,
            actor=record.provenance.agent_name,
        )
        state = CompositeKnowledgeState(
            memory_id=record.memory_id,
            current_version_id=record.version_id,
            version_number=record.version_number,
            revision=1,
            versions={record.version_id: record},
            lifecycle_events=(event,),
            relationships=(),
        )
        if not self._save_state(state, expected_revision=0):
            raise RuntimeError("Failed to propose knowledge: concurrent modification or persistence failure.")
        return record.version_id

    def validate_knowledge(self, memory_id: str, version_id: str) -> LifecycleEvent:
        state = self._get_state(memory_id)
        if state is None or version_id not in state.versions:
            raise KeyError(f"Version {version_id} not found in {memory_id}.")
        record = state.versions[version_id]
        outcome = self._policy.validate(record)
        prior_status = self._current_status(state, version_id)
        new_status = KnowledgeStatus.ACCEPTED if outcome.accepted else KnowledgeStatus.REJECTED
        event = LifecycleEvent(
            memory_id=memory_id,
            version_id=version_id,
            event_type="validated",
            previous_status=prior_status,
            new_status=new_status,
            reason=outcome.reason,
            actor="validation_policy",
        )
        updated = self._with_revision(state, lifecycle_events=state.lifecycle_events + (event,))
        if not self._save_state(updated, expected_revision=state.revision):
            raise RuntimeError("Concurrent modification detected during validation.")
        return event

    def get_knowledge(self, memory_id: str, version_id: Optional[str] = None) -> KnowledgeView:
        state = self._get_state(memory_id)
        if state is None:
            raise KeyError(f"Knowledge record {memory_id} not found.")
        target_version = version_id or state.current_version_id
        if target_version not in state.versions:
            raise KeyError(f"Version {target_version} not found.")
        history = tuple(event for event in state.lifecycle_events if event.version_id == target_version)
        status = history[-1].new_status if history else KnowledgeStatus.PROPOSED
        return KnowledgeView(
            record=state.versions[target_version],
            current_status=status,
            lifecycle_history=history,
        )

    def search_knowledge(
        self,
        query: str,
        scope_filters: Optional[Dict[str, Any]] = None,
    ) -> List[KnowledgeView]:
        """Search governed knowledge records and return views with derived status.

        The current StateStore is the authoritative corpus. Query and record
        text use Chronicle's configured embedding backend; exact scope filters
        are applied before ranking. Empty queries enumerate scope matches.
        """
        filters = scope_filters or {}
        query_vector = self._embedding_model.encode(query) if query.strip() else None
        scored = []
        for key in self._store.list_keys(prefix="knowledge:"):
            memory_id = key.removeprefix("knowledge:")
            try:
                state = self._get_state(memory_id)
                if state is None:
                    continue
                record = state.versions.get(state.current_version_id)
                if record is None:
                    continue
                scope = dict(record.scope)
                if any(name not in scope or scope[name] != expected for name, expected in filters.items()):
                    continue
                view = self.get_knowledge(memory_id)
                if query_vector is None:
                    score = 0.0
                else:
                    text = " ".join((record.summary, record.content, " ".join(record.tags)))
                    score = cosine_similarity(query_vector, self._embedding_model.encode(text))
                scored.append((view, score))
            except Exception:
                log.warning("Skipping unreadable Chronicle knowledge key %s", key, exc_info=True)
        scored.sort(key=lambda item: item[1], reverse=True)
        return [view for view, _ in scored]

    def get_knowledge_by_version(self, version_id: str) -> KnowledgeView:
        """Resolve an immutable version ID through the authoritative state keys."""
        for key in self._store.list_keys(prefix="knowledge:"):
            memory_id = key.removeprefix("knowledge:")
            state = self._get_state(memory_id)
            if state is not None and version_id in state.versions:
                return self.get_knowledge(memory_id, version_id)
        raise KeyError(f"Knowledge version {version_id} not found.")

    def relate_knowledge(
        self,
        source_version_id: str,
        target_version_id: str,
        relationship_type: str,
        reason: str,
    ) -> KnowledgeRelationship:
        """Add a relationship to the source record's composite state via CAS."""
        if not relationship_type.strip():
            raise ValueError("relationship_type must not be empty")
        source_view = self.get_knowledge_by_version(source_version_id)
        # Verify the target exists, even when it belongs to a different memory.
        self.get_knowledge_by_version(target_version_id)
        for _attempt in range(3):
            state = self._get_state(source_view.record.memory_id)
            if state is None or source_version_id not in state.versions:
                raise KeyError(f"Source version {source_version_id} no longer exists.")
            relationship = KnowledgeRelationship(
                relationship_type=relationship_type,
                source_version_id=source_version_id,
                target_version_id=target_version_id,
                reason=reason,
            )
            updated = self._with_revision(
                state,
                relationships=state.relationships + (relationship,),
            )
            if self._save_state(updated, expected_revision=state.revision):
                return relationship
        raise RuntimeError("Concurrent modification detected while relating knowledge.")

    @staticmethod
    def _current_status(state: CompositeKnowledgeState, version_id: str) -> KnowledgeStatus:
        matching = [event for event in state.lifecycle_events if event.version_id == version_id]
        return matching[-1].new_status if matching else KnowledgeStatus.PROPOSED

    def contest_knowledge(
        self,
        memory_id: str,
        target_version_id: str,
        conflicting_version_id: str,
        reason: str,
    ) -> KnowledgeRelationship:
        state = self._get_state(memory_id)
        if state is None:
            raise KeyError(f"Knowledge record {memory_id} not found.")
        if target_version_id not in state.versions:
            raise KeyError(f"Target version {target_version_id} not found.")
        if conflicting_version_id not in state.versions:
            raise KeyError(f"Conflicting version {conflicting_version_id} not found.")
        relationship = KnowledgeRelationship(
            relationship_type="conflicts_with",
            source_version_id=conflicting_version_id,
            target_version_id=target_version_id,
            reason=reason,
        )
        updated = self._with_revision(
            state,
            relationships=state.relationships + (relationship,),
        )
        if not self._save_state(updated, expected_revision=state.revision):
            raise RuntimeError("Concurrent modification detected during contest.")
        return relationship

    def supersede_knowledge(
        self,
        memory_id: str,
        old_version_id: str,
        new_record: KnowledgeRecord,
        *,
        expected_revision: Optional[int] = None,
    ) -> str:
        state = self._get_state(memory_id)
        if state is None or old_version_id not in state.versions:
            raise KeyError(f"Old version {old_version_id} not found.")
        if expected_revision is not None and expected_revision != state.revision:
            raise RuntimeError("Concurrent modification detected during supersession.")
        if new_record.memory_id != memory_id or new_record.version_number != state.version_number + 1:
            raise ValueError("New record must match memory_id and increment version_number.")
        if old_version_id != state.current_version_id:
            raise ValueError("Only the current version can be superseded.")
        if self._current_status(state, old_version_id) != KnowledgeStatus.ACCEPTED:
            raise ValueError("Only accepted knowledge can be superseded.")

        event = LifecycleEvent(
            memory_id=memory_id,
            version_id=old_version_id,
            event_type="superseded",
            previous_status=KnowledgeStatus.ACCEPTED,
            new_status=KnowledgeStatus.SUPERSEDED,
            reason=f"Superseded by {new_record.version_id}",
        )
        relationship = KnowledgeRelationship(
            relationship_type="supersedes",
            source_version_id=new_record.version_id,
            target_version_id=old_version_id,
            reason="Updated finding.",
        )
        updated = self._with_revision(
            state,
            current_version_id=new_record.version_id,
            version_number=new_record.version_number,
            versions={**state.versions, new_record.version_id: new_record},
            lifecycle_events=state.lifecycle_events + (event,),
            relationships=state.relationships + (relationship,),
        )
        expected = state.revision if expected_revision is None else expected_revision
        if not self._save_state(updated, expected_revision=expected):
            raise RuntimeError("Concurrent modification detected during supersession.")
        return new_record.version_id
