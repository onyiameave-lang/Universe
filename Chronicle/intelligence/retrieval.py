"""
Chronicle.intelligence.retrieval
================================
The five-stage constitutional retrieval process and contradiction detection.
(Book II Part III Ch V Retrieval; Ch VII Contradiction Detection.)

Five stages before any generation:
  I   Identity   - who needs this? (requester trust)
  II  Intent     - why? (maps intent -> relevant pillars)
  III Semantic   - which knowledge matches? (embedding search)
  IV  Relationship - which connected memories help? (knowledge graph)
  V   Evolution  - has this knowledge already evolved? (supersedes edges)

Real logic over the vector store and knowledge graph. Explainable trace.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from core.embeddings import get_embedding_model, cosine_similarity  # type: ignore
from core.memory_record import MemoryRecord, MemoryPillar, SOURCE_CREDIBILITY  # type: ignore
from core.vector_store import VectorStore                             # type: ignore
from core.knowledge_graph import KnowledgeGraph                       # type: ignore


def _record_retrieval(rec, requester: str) -> None:
    """
    FIX-CH-FB-02: Register that a memory was RETRIEVED without asserting the
    retrieval was successful. Increments total_uses (exposure) but leaves
    successful_uses untouched, so an unverified retrieval no longer inflates
    confidence. Real success is credited later by relevance feedback.
    Falls back to the record's own record_use() if the fields differ.
    """
    import time as _t
    try:
        rec.total_uses += 1
        if requester and requester not in rec.used_by:
            rec.used_by.append(requester)
        rec.updated_at = _t.time()
        rec.compute_confidence()
    except Exception:
        try:
            rec.record_use(requester, successful=True)
        except Exception:
            pass


class RetrievalEngine:
    def __init__(self, store: VectorStore, graph: KnowledgeGraph):
        self.store = store
        self.graph = graph
        self.embedder = get_embedding_model()
        # FIX-CH-FB-02: optional callback set by ChronicleAgent -> mismatch_penalty.
        # Given (query_embedding, memory_id) it returns a penalty in [0,1] for
        # memories previously judged irrelevant to a look-alike query. Kept as an
        # attribute (not a ctor arg) so existing callers/tests remain compatible.
        self.penalty_fn = None

    def retrieve(self, query: str, requester: str = "unknown", intent: str = "",
                 domain: Optional[str] = None, pillar: Optional[str] = None,
                 top_k: int = 5) -> Dict[str, Any]:
        trace: Dict[str, Any] = {}
        started = time.time()

        identity = {"requester": requester,
                    "requester_trust": SOURCE_CREDIBILITY.get(requester.lower(), 0.4),
                    "domain_scope": domain or "all"}
        trace["stage_1_identity"] = identity

        intent_pillars = self._intent(intent, pillar)
        trace["stage_2_intent"] = {"resolved_pillars": [p.value for p in intent_pillars]}

        q_emb = self.embedder.encode(query)
        hits: List[Tuple[MemoryRecord, float]] = []
        for p in (intent_pillars or [None]):  # type: ignore
            hits.extend(self.store.search(q_emb, top_k=top_k * 2, domain=domain,
                                         pillar=(p.value if isinstance(p, MemoryPillar) else None)))
        best: Dict[str, Tuple[MemoryRecord, float]] = {}
        for rec, score in hits:
            if rec.memory_id not in best or score > best[rec.memory_id][1]:
                best[rec.memory_id] = (rec, score)
        semantic = sorted(best.values(), key=lambda x: x[1], reverse=True)[:top_k]
        trace["stage_3_semantic"] = {"matches": len(semantic)}
        if not semantic:
            kw = self.store.keyword_search(query.split(), top_k=top_k)
            semantic = [(r, 0.3) for r in kw]
            trace["stage_3_semantic"]["fallback"] = "keyword"

        related: List[Tuple[MemoryRecord, float]] = []
        seen = {r.memory_id for r, _ in semantic}
        for rec, _ in semantic:
            for rel_id, weight, _ in self.graph.related(rec.memory_id, max_depth=2, limit=5):
                if rel_id in seen:
                    continue
                rr = self.store.get(rel_id)
                if rr and not rr.archived:
                    seen.add(rel_id)
                    related.append((rr, weight * 0.5))
        trace["stage_4_relationship"] = {"connected": len(related)}

        evolved = sum(1 for rec, _ in semantic if self.graph.neighbors(rec.memory_id, "supersedes"))
        trace["stage_5_evolution"] = {"evolved_memories": evolved}

        # FIX-CH-FB-02: apply negative-feedback penalty from prior relevance judgements
        # so memories already rejected for a look-alike query are down-weighted BEFORE
        # ranking, not just after their confidence slowly decays.
        candidates = list(semantic) + related
        if self.penalty_fn is not None:
            adjusted = []
            for rec, score in candidates:
                try:
                    pen = self.penalty_fn(q_emb, rec.memory_id)
                except Exception:
                    pen = 0.0
                adjusted.append((rec, score * (1.0 - pen)))
            candidates = adjusted
        trace["stage_penalty"] = {"applied": self.penalty_fn is not None}

        ranked = self._rank(candidates)
        # FIX-CH-FB-02: Do NOT pre-reward retrieval as successful=True. The old code
        # inflated successful_uses on EVERY retrieval -- relevant or not -- which raised
        # confidence for bad matches and made Chronicle unable to learn. We now only
        # register that the memory was RETRIEVED (total_uses via record_retrieval),
        # leaving successful_uses to be set later by real relevance feedback
        # (apply_relevance_feedback). Ranking/confidence thus reflect earned relevance.
        # (Book II Ch VI Memory Evolution -- knowledge is refined by verified use.)
        for rec, _ in ranked[:top_k]:
            _record_retrieval(rec, requester)
            self.store.update(rec)

        results = [{"memory_id": rec.memory_id,
                    "summary": rec.summary or str(rec.content)[:120],
                    "pillar": rec.pillar.value, "domain": rec.domain,
                    "confidence": rec.confidence, "score": round(score, 4),
                    "evidence": rec.evidence, "used_by": rec.used_by}
                   for rec, score in ranked[:top_k]]

        return {"query": query, "requester": requester, "results": results,
                "count": len(results), "trace": trace,
                "duration_ms": round((time.time() - started) * 1000, 1),
                "generation_advised": len(results) == 0}

    def _intent(self, intent: str, pillar: Optional[str]) -> List[MemoryPillar]:
        if pillar:
            try:
                return [MemoryPillar(pillar)]
            except ValueError:
                pass
        il = (intent or "").lower()
        mapping = {"trade": MemoryPillar.EPISODIC, "history": MemoryPillar.EPISODIC,
                   "strategy": MemoryPillar.SEMANTIC, "fact": MemoryPillar.SEMANTIC,
                   "how": MemoryPillar.PROCEDURAL, "workflow": MemoryPillar.PROCEDURAL,
                   "optimize": MemoryPillar.EVOLUTIONARY, "improve": MemoryPillar.EVOLUTIONARY,
                   "sentiment": MemoryPillar.SOCIAL, "feedback": MemoryPillar.SOCIAL,
                   "architecture": MemoryPillar.STRUCTURAL, "dependency": MemoryPillar.STRUCTURAL,
                   "governance": MemoryPillar.CONSTITUTIONAL, "policy": MemoryPillar.CONSTITUTIONAL}
        return [p for kw, p in mapping.items() if kw in il]

    def _rank(self, candidates: List[Tuple[MemoryRecord, float]]) -> List[Tuple[MemoryRecord, float]]:
        now = time.time()
        ranked = []
        for rec, sim in candidates:
            recency = 1.0 / (1.0 + (now - rec.updated_at) / 86400.0 / 90.0)
            final = 0.6 * sim + 0.3 * rec.confidence + 0.1 * recency
            ranked.append((rec, final))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked


class ContradictionDetector:
    def __init__(self, store: VectorStore, graph: KnowledgeGraph, threshold: float = 0.82):
        self.store = store
        self.graph = graph
        self.threshold = threshold
        self.embedder = get_embedding_model()

    def scan(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        records = [r for r in self.store.all() if (domain is None or r.domain == domain)]
        flagged = []
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                a, b = records[i], records[j]
                if a.domain != b.domain or not a.embedding or not b.embedding:
                    continue
                sim = cosine_similarity(a.embedding, b.embedding)
                if sim >= self.threshold and self._opposing(a, b):
                    flagged.append({"memory_a": a.memory_id, "memory_b": b.memory_id,
                                   "similarity": round(sim, 3), "domain": a.domain,
                                   "reason": "high similarity, opposing conclusions"})
                    self.graph.connect(a.memory_id, b.memory_id, "contradicts", weight=sim)
        return flagged

    def _opposing(self, a: MemoryRecord, b: MemoryRecord) -> bool:
        pos = {"success", "profit", "gain", "up", "bullish", "increase", "win", "true", "works"}
        neg = {"failure", "loss", "down", "bearish", "decrease", "lose", "false", "broken"}
        ta = f"{a.summary} {a.lesson} {a.content}".lower()
        tb = f"{b.summary} {b.lesson} {b.content}".lower()
        return ((any(w in ta for w in pos) and any(w in tb for w in neg)) or
                (any(w in ta for w in neg) and any(w in tb for w in pos)))
