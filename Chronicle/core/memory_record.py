"""
Chronicle.core.memory_record
============================
The memory record model and the Seven Pillars of Memory.
(Book II Part III Ch II Seven Pillars; Ch III Every Memory Has Meaning;
 Ch IV Validation; Ch VI Confidence.)

Confidence is COMPUTED from real signals (evidence, age, verification, usage
track record, source credibility). It is never a fabricated constant, which
mirrors the ecosystem's reasoning philosophy: trust is earned.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryPillar(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    EVOLUTIONARY = "evolutionary"
    SOCIAL = "social"
    STRUCTURAL = "structural"
    CONSTITUTIONAL = "constitutional"


VALIDATION_CRITERIA = ["accuracy", "relevance", "evidence", "confidence", "novelty",
                       "reusability", "consistency", "importance", "recency", "source_credibility"]

SOURCE_CREDIBILITY = {
    "aegis": 0.95, "atlas": 0.90, "chronicle": 0.90, "forge": 0.85, "nexus": 0.85,
    "oracle": 0.80, "genesis": 0.80, "sentinel": 0.75, "pulse": 0.60,
    "user": 0.70, "ecosystem": 0.85, "unknown": 0.40,
}


@dataclass
class MemoryRecord:
    memory_id: str = field(default_factory=lambda: f"mem-{uuid.uuid4().hex[:12]}")
    pillar: MemoryPillar = MemoryPillar.SEMANTIC
    domain: str = "general"
    content: Any = ""
    summary: str = ""
    embedding: List[float] = field(default_factory=list)
    source_repository: str = "unknown"
    source_agent: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    used_by: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    lesson: str = ""
    confidence: float = 0.0
    verified: bool = False
    successful_uses: int = 0
    total_uses: int = 0
    tags: List[str] = field(default_factory=list)
    version: int = 1
    archived: bool = False

    def compute_confidence(self) -> float:
        evidence_factor = min(len(self.evidence) / 5.0, 1.0)
        age_days = max((time.time() - self.created_at) / 86400.0, 0.0)
        age_factor = 1.0 / (1.0 + age_days / 180.0)
        verification_factor = 1.0 if self.verified else 0.4
        if self.total_uses > 0:
            success_rate = self.successful_uses / self.total_uses
            volume_weight = min(self.total_uses / 10.0, 1.0)
            usage_factor = 0.5 + 0.5 * (success_rate * volume_weight)
        else:
            usage_factor = 0.5
        source_factor = SOURCE_CREDIBILITY.get(self.source_repository.lower(), 0.4)
        confidence = (0.25 * evidence_factor + 0.15 * age_factor +
                     0.20 * verification_factor + 0.20 * usage_factor +
                     0.20 * source_factor)
        self.confidence = round(min(max(confidence, 0.0), 1.0), 4)
        return self.confidence

    def record_use(self, repository: str, successful: bool = True) -> None:
        self.total_uses += 1
        if successful:
            self.successful_uses += 1
        if repository and repository not in self.used_by:
            self.used_by.append(repository)
        self.updated_at = time.time()
        self.compute_confidence()

    def to_dict(self, include_embedding: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        d["pillar"] = self.pillar.value if isinstance(self.pillar, MemoryPillar) else self.pillar
        if not include_embedding:
            d.pop("embedding", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        pillar = data.get("pillar", "semantic")
        try:
            pillar = MemoryPillar(pillar)
        except ValueError:
            pillar = MemoryPillar.SEMANTIC
        return cls(
            memory_id=data.get("memory_id", f"mem-{uuid.uuid4().hex[:12]}"),
            pillar=pillar, domain=data.get("domain", "general"),
            content=data.get("content", ""), summary=data.get("summary", ""),
            embedding=data.get("embedding", []),
            source_repository=data.get("source_repository", "unknown"),
            source_agent=data.get("source_agent", ""),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            used_by=data.get("used_by", []), evidence=data.get("evidence", []),
            lesson=data.get("lesson", ""), confidence=data.get("confidence", 0.0),
            verified=data.get("verified", False),
            successful_uses=data.get("successful_uses", 0),
            total_uses=data.get("total_uses", 0), tags=data.get("tags", []),
            version=data.get("version", 1), archived=data.get("archived", False))


def validate_memory(record: MemoryRecord) -> Dict[str, Any]:
    checks: Dict[str, bool] = {}
    checks["accuracy"] = bool(record.content)
    checks["relevance"] = bool(record.domain)
    checks["evidence"] = len(record.evidence) > 0
    checks["confidence"] = record.confidence >= 0.5
    checks["reusability"] = record.pillar in (
        MemoryPillar.SEMANTIC, MemoryPillar.PROCEDURAL, MemoryPillar.EVOLUTIONARY
    ) or record.total_uses > 0
    checks["source_credibility"] = SOURCE_CREDIBILITY.get(record.source_repository.lower(), 0.4) >= 0.5
    checks["importance"] = record.verified or len(record.used_by) > 1 or record.confidence >= 0.7
    passed = sum(1 for v in checks.values() if v)
    score = passed / len(checks)
    permanent = score >= 0.6 and checks["accuracy"]
    return {"checks": checks, "score": round(score, 3), "permanent": permanent,
            "status": "validated" if permanent else "temporary"}
