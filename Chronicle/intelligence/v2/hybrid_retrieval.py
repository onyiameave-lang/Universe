"""Transparent retrieval that separates accepted knowledge from unsettled claims."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from Chronicle.core.knowledge_models import KnowledgeStatus, KnowledgeView


class RetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    scope_filters: Dict[str, Any] = Field(default_factory=dict)
    accepted_records: List[KnowledgeView] = Field(default_factory=list)
    unsettled_records: List[KnowledgeView] = Field(default_factory=list)


class HybridRetriever:
    """Use only ChronicleClient's public search interface."""

    def __init__(self, client):
        self._client = client

    def retrieve(
        self,
        query: str,
        scope_filters: Optional[Dict[str, Any]] = None,
    ) -> RetrievalResult:
        filters = dict(scope_filters or {})
        candidates = self._client.search_knowledge(query, scope_filters=filters)
        accepted = []
        unsettled = []
        for view in candidates:
            if view.current_status == KnowledgeStatus.ACCEPTED:
                accepted.append(view)
            elif view.current_status == KnowledgeStatus.CONTESTED:
                unsettled.append(view)
        return RetrievalResult(
            query=query,
            scope_filters=filters,
            accepted_records=accepted,
            unsettled_records=unsettled,
        )
