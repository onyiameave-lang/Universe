"""Propose synthesized knowledge while preserving all accepted source records."""

import uuid
from typing import Any, Dict, List

from Chronicle.core.knowledge_models import (
    EvidenceLink,
    KnowledgeRecord,
    KnowledgeStatus,
    Provenance,
)


class Synthesizer:
    """Synthesize from accepted records using only ChronicleClient's public API."""

    def __init__(self, client):
        self._client = client

    def synthesize_and_propose(
        self,
        source_version_ids: List[str],
        synthesis_content: str,
        synthesis_summary: str,
        scope: Dict[str, Any],
        agent_name: str,
        repository: str,
    ) -> str:
        unique_ids = list(dict.fromkeys(source_version_ids))
        if not unique_ids:
            raise ValueError("At least one accepted source version is required.")
        source_views = []
        for version_id in unique_ids:
            view = self._client.get_knowledge_by_version(version_id)
            if view.current_status != KnowledgeStatus.ACCEPTED:
                raise ValueError(
                    f"Source version {version_id} is not ACCEPTED. "
                    "Cannot synthesize from unsettled science."
                )
            source_views.append(view)

        record = KnowledgeRecord(
            memory_id=f"syn-{uuid.uuid4().hex[:12]}",
            version_number=1,
            content=synthesis_content,
            summary=synthesis_summary,
            scope=tuple((key, value) for key, value in scope.items()),
            provenance=Provenance(agent_name=agent_name, repository=repository),
            evidence_links=tuple(
                EvidenceLink(
                    reference_type="chronicle_knowledge",
                    reference_id=view.record.version_id,
                    summary=f"Supporting evidence from {view.record.summary[:50]}",
                )
                for view in source_views
            ),
        )
        new_version_id = self._client.propose_knowledge(record)
        for view in source_views:
            self._client.relate_knowledge(
                source_version_id=new_version_id,
                target_version_id=view.record.version_id,
                relationship_type="supports",
                reason=(
                    f"Synthesis {record.memory_id} incorporates findings "
                    f"from {view.record.memory_id}"
                ),
            )
        return new_version_id
