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

FIX-HYBRID-01: Stage III now uses HybridRetriever (SBERT + BM25) for the
final ranking step, replacing the pure cosine _rank(). The five-stage
pipeline is unchanged; HybridRetriever re-scores the candidates assembled
by stages III-IV and returns them in hybrid-score order.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from core.embeddings import get_embedding_model, cosine_similarity  # type: ignore
from core.memory_record import MemoryRecord, MemoryPillar, SOURCE_CREDIBILITY  # type: ignore
from core.vector_store import VectorStore                             # type: ignore
from core.knowledge_graph import KnowledgeGraph                       # type: ignore

# FIX-HYBRID-01: import the hybrid scorer
try:
    from intelligence.hybrid_retrieval import HybridRetriever  # type: ignore
    _HYBRID_AVAILABLE = True
except ImportError:
    try:
        from hybrid_retrieval import HybridRetriever  # type: ignore
        _HYBRID_AVAILABLE = True
    except ImportError:
        _HYBRID_AVAILABLE = False
        HybridRetriever = None  # type: ignore

log = logging.getLogger("chronicle.retrieval")


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
        self.penalty_fn = None

        # FIX-HYBRID-01: instantiate HybridRetriever over current store records.
        # Converts MemoryRecord objects to plain dicts for the retriever.
        self._hybrid: Optional[HybridRetriever] = None
        if _HYBRID_AVAILABLE and HybridRetriever is not None:
            try:
                self._hybrid = HybridRetriever(
                    self._records_as_dicts(), use_sbert=True
                )
                # FIX-HYBRID-01: register refresh callback with the store so
                # add() / update() / archive() automatically invalidate the
                # SBERT embedding cache without callers needing to know.
                self.store._hybrid_notify = self._refresh_hybrid
                log.info("[chronicle.retrieval] HybridRetriever initialised "
                         "(%d records)", len(self.store.all()))
            except Exception as exc:
                log.warning("[chronicle.retrieval] HybridRetriever init failed: %s — "
                            "falling back to cosine-only ranking", exc)
                self._hybrid = None
        else:
            log.info("[chronicle.retrieval] hybrid_retrieval not available — "
                     "using cosine-only ranking")

    # ------------------------------------------------------------------
    # Helper: convert store records to plain dicts for HybridRetriever
    # ------------------------------------------------------------------

    def _records_as_dicts(self) -> List[Dict[str, Any]]:
        """Return all active MemoryRecords as plain dicts (HybridRetriever input)."""
        out = []
        for rec in self.store.all(include_archived=False):
            d = {
                "memory_id": rec.memory_id,
                "summary": rec.summary or "",
                "content": str(rec.content or "")[:500],
                "tags": list(rec.tags or []),
                "lesson": rec.lesson or "",
                "domain": rec.domain or "",
                "pillar": rec.pillar.value if hasattr(rec.pillar, "value") else str(rec.pillar),
                "confidence": rec.confidence,
                "updated_at": rec.updated_at,
                "embedding": list(rec.embedding or []),
            }
            out.append(d)
        return out

    def _refresh_hybrid(self) -> None:
        """Rebuild HybridRetriever corpus when store records change."""
        if self._hybrid is not None:
            try:
                self._hybrid.update_records(self._records_as_dicts())
            except Exception as exc:
                log.debug("[chronicle.retrieval] hybrid refresh failed: %s", exc)

    # ------------------------------------------------------------------
    # Main retrieve() — five-stage pipeline, unchanged interface
    # ------------------------------------------------------------------

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

        # FIX-CH-FB-02: apply negative-feedback penalty
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

        # FIX-HYBRID-01: re-rank candidates with HybridRetriever (SBERT + BM25)
        # instead of the pure cosine _rank(). Falls back to _rank() if hybrid
        # is unavailable or raises.
        ranked = self._hybrid_rank(candidates, query, domain, top_k)
        trace["stage_hybrid"] = {
            "scorer": "sbert+bm25" if (self._hybrid is not None) else "cosine-only"
        }

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

    # ------------------------------------------------------------------
    # Hybrid ranking (replaces _rank for the final sort)
    # ------------------------------------------------------------------

    def _hybrid_rank(self, candidates: List[Tuple[MemoryRecord, float]],
                     query: str, domain: Optional[str],
                     top_k: int) -> List[Tuple[MemoryRecord, float]]:
        """
        FIX-HYBRID-01: Re-rank candidates using HybridRetriever (SBERT + BM25).

        Strategy:
          1. Refresh the HybridRetriever corpus (cheap if records unchanged).
          2. Build a lookup dict from memory_id -> MemoryRecord for the candidates.
          3. Ask HybridRetriever to score each candidate record dict.
          4. Return sorted (MemoryRecord, hybrid_score) pairs.

        Falls back to cosine _rank() if HybridRetriever is unavailable.
        """
        if self._hybrid is None or not candidates:
            return self._rank(candidates)

        try:
            # Refresh corpus so newly added records are included
            self._refresh_hybrid()

            # Build candidate dict lookup
            cand_map: Dict[str, Tuple[MemoryRecord, float]] = {}
            for rec, cosine_score in candidates:
                cand_map[rec.memory_id] = (rec, cosine_score)

            # Build plain-dict versions of candidates for HybridRetriever
            cand_dicts: List[Dict[str, Any]] = []
            for rec, cosine_score in candidates:
                d = {
                    "memory_id": rec.memory_id,
                    "summary": rec.summary or "",
                    "content": str(rec.content or "")[:500],
                    "tags": list(rec.tags or []),
                    "lesson": rec.lesson or "",
                    "domain": rec.domain or "",
                    "pillar": rec.pillar.value if hasattr(rec.pillar, "value") else str(rec.pillar),
                    "confidence": rec.confidence,
                    "updated_at": rec.updated_at,
                    "embedding": list(rec.embedding or []),
                    "score": cosine_score,  # passed as fallback cosine in BM25-only mode
                }
                cand_dicts.append(d)

            # Score each candidate with the hybrid scorer
            scored_dicts: List[Tuple[Dict[str, Any], float]] = []
            for d in cand_dicts:
                s = self._hybrid.score(query, d, query_domain=domain)
                scored_dicts.append((d, s))

            scored_dicts.sort(key=lambda x: x[1], reverse=True)

            # Reconstruct (MemoryRecord, score) pairs
            result: List[Tuple[MemoryRecord, float]] = []
            for d, s in scored_dicts[:top_k]:
                mid = d["memory_id"]
                if mid in cand_map:
                    result.append((cand_map[mid][0], s))

            log.debug("[chronicle.retrieval] hybrid_rank: %d candidates → top %d "
                      "(scorer=%s)", len(candidates), len(result),
                      self._hybrid.stats().get("mode", "?"))
            return result

        except Exception as exc:
            log.warning("[chronicle.retrieval] hybrid_rank failed (%s) — "
                        "falling back to cosine _rank()", exc)
            return self._rank(candidates)

    # ------------------------------------------------------------------
    # Original cosine _rank (kept as fallback)
    # ------------------------------------------------------------------

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
        """Original cosine-only ranking (fallback when HybridRetriever unavailable)."""
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