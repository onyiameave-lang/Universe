"""Side-effect-free candidate detection for potentially conflicting claims."""

import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from Chronicle.core.knowledge_models import KnowledgeStatus, KnowledgeView


class ConflictCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version_id_a: str
    version_id_b: str
    reason: str
    scope_overlap: Dict[str, Any] = Field(default_factory=dict)


class ContradictionDetector:
    """Find plausible contradictions without changing records or relationships.

    An optional compatibility analyzer can replace the conservative built-in
    lexical heuristic. It receives two KnowledgeViews and returns ``(bool,
    reason)``. Detection never calls a mutating ChronicleClient operation.
    """

    _WORDS = re.compile(r"[a-z0-9]+")
    _NEGATIONS = {"no", "not", "never", "without", "cannot", "can't", "false", "fail", "fails", "failed"}
    _NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

    def __init__(self, client, compatibility_analyzer: Optional[Callable] = None):
        self._client = client
        self._compatibility_analyzer = compatibility_analyzer or self._analyze_claim_compatibility

    def detect_potential_conflicts(
        self,
        scope_filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConflictCandidate]:
        views = self._client.search_knowledge("", scope_filters=scope_filters)
        accepted = [view for view in views if view.current_status == KnowledgeStatus.ACCEPTED]
        conflicts = []
        for index, first in enumerate(accepted):
            for second in accepted[index + 1:]:
                overlap = self._calculate_scope_overlap(first.record.scope, second.record.scope)
                if not overlap:
                    continue
                incompatible, reason = self._compatibility_analyzer(first, second)
                if incompatible:
                    conflicts.append(ConflictCandidate(
                        version_id_a=first.record.version_id,
                        version_id_b=second.record.version_id,
                        reason=reason,
                        scope_overlap=overlap,
                    ))
        return conflicts

    @staticmethod
    def _calculate_scope_overlap(scope_a: Tuple, scope_b: Tuple) -> Dict[str, Any]:
        first, second = dict(scope_a), dict(scope_b)
        return {key: value for key, value in first.items() if key in second and second[key] == value}

    @classmethod
    def _analyze_claim_compatibility(
        cls,
        view_a: KnowledgeView,
        view_b: KnowledgeView,
    ) -> Tuple[bool, str]:
        text_a = f"{view_a.record.summary} {view_a.record.content}".lower()
        text_b = f"{view_b.record.summary} {view_b.record.content}".lower()
        words_a, words_b = set(cls._WORDS.findall(text_a)), set(cls._WORDS.findall(text_b))
        core_a, core_b = words_a - cls._NEGATIONS, words_b - cls._NEGATIONS
        overlap = len(core_a & core_b) / max(1, len(core_a | core_b))
        if overlap < 0.5:
            return False, ""
        negated_a = bool(words_a & cls._NEGATIONS)
        negated_b = bool(words_b & cls._NEGATIONS)
        if negated_a != negated_b:
            return True, "Claims share key terms but differ in negation."
        numbers_a = [float(number) for number in cls._NUMBER.findall(text_a)]
        numbers_b = [float(number) for number in cls._NUMBER.findall(text_b)]
        if numbers_a and numbers_b:
            value_a, value_b = numbers_a[-1], numbers_b[-1]
            scale = max(abs(value_a), abs(value_b), 1e-12)
            if abs(value_a - value_b) / scale >= 0.5:
                return True, f"Claims report materially different values ({value_a:g} vs {value_b:g})."
        return False, ""
