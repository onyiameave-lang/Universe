"""
Chronicle.intelligence.consolidation
====================================
Memory consolidation: Chronicle optimizes itself down to key points.
(Book II Part III Ch VIII Memory Evolution: updating, merging, splitting,
refining, archiving, re-ranking; "Chronicle continuously refines its
understanding.")

A smart memory does not hoard raw records. It periodically CONSOLIDATES:
  * MERGE near-duplicate memories in the same domain into a stronger single
    record (evidence and usage combine; the weaker duplicate is archived, not
    destroyed, per "nothing dies without record").
  * DISTILL clusters of related memories into a higher-level "key point" memory
    (a semantic summary that captures the theme), so retrieval returns crisp
    insight instead of scattered fragments.
  * PRUNE (archive) stale, low-confidence, never-used memories so the store
    stays sharp.

This runs on real data using the existing embeddings + similarity. The LLM,
if present, only phrases the distilled key point; the clustering and decisions
are deterministic.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.embeddings import cosine_similarity, get_embedding_model  # type: ignore
from core.memory_record import MemoryRecord, MemoryPillar           # type: ignore


class ConsolidationEngine:
    def __init__(self, store, graph, llm=None,
                 merge_threshold: float = 0.93, cluster_threshold: float = 0.75):
        self.store = store
        self.graph = graph
        self.llm = llm
        self.embedder = get_embedding_model()
        self.merge_threshold = merge_threshold
        self.cluster_threshold = cluster_threshold

    def consolidate(self, domain: Optional[str] = None,
                   distill: bool = True, prune: bool = True) -> Dict[str, Any]:
        """Run a full consolidation pass. Returns what changed."""
        records = [r for r in self.store.all() if (domain is None or r.domain == domain)]
        report = {"merged": 0, "distilled": 0, "pruned": 0, "key_points": []}

        # 1) MERGE near-duplicates.
        report["merged"] = self._merge_duplicates(records)

        # 2) DISTILL clusters into key points.
        if distill:
            refreshed = [r for r in self.store.all() if (domain is None or r.domain == domain)]
            key_points = self._distill_clusters(refreshed, domain)
            report["distilled"] = len(key_points)
            report["key_points"] = key_points

        # 3) PRUNE stale, unused, low-confidence memories (archive, never delete).
        if prune:
            report["pruned"] = self._prune(domain)

        return report

    def _merge_duplicates(self, records: List[MemoryRecord]) -> int:
        merged = 0
        used = set()
        for i in range(len(records)):
            a = records[i]
            if a.memory_id in used or a.archived or not a.embedding:
                continue
            for j in range(i + 1, len(records)):
                b = records[j]
                if b.memory_id in used or b.archived or not b.embedding:
                    continue
                if a.domain != b.domain:
                    continue
                sim = cosine_similarity(a.embedding, b.embedding)
                if sim >= self.merge_threshold:
                    # combine into the stronger record
                    strong, weak = (a, b) if a.confidence >= b.confidence else (b, a)
                    strong.evidence = list(dict.fromkeys(strong.evidence + weak.evidence))
                    strong.total_uses += weak.total_uses
                    strong.successful_uses += weak.successful_uses
                    for u in weak.used_by:
                        if u not in strong.used_by:
                            strong.used_by.append(u)
                    strong.tags = list(dict.fromkeys(strong.tags + weak.tags))
                    self.store.update(strong)
                    self.store.archive(weak.memory_id)
                    self.graph.connect(strong.memory_id, weak.memory_id, "supersedes", weight=sim)
                    used.add(weak.memory_id)
                    merged += 1
        return merged

    def _distill_clusters(self, records: List[MemoryRecord],
                         domain: Optional[str]) -> List[Dict[str, Any]]:
        """Group related memories and create a higher-level key-point memory."""
        active = [r for r in records if not r.archived and r.embedding]
        clusters: List[List[MemoryRecord]] = []
        assigned = set()
        for r in active:
            if r.memory_id in assigned:
                continue
            cluster = [r]
            assigned.add(r.memory_id)
            for other in active:
                if other.memory_id in assigned:
                    continue
                if cosine_similarity(r.embedding, other.embedding) >= self.cluster_threshold:
                    cluster.append(other)
                    assigned.add(other.memory_id)
            if len(cluster) >= 3:  # only distill meaningful clusters
                clusters.append(cluster)

        key_points = []
        for cluster in clusters:
            summaries = [c.summary for c in cluster]
            key_text = self._phrase_key_point(summaries, domain)
            embedding = self.embedder.encode(key_text)
            kp = MemoryRecord(
                pillar=MemoryPillar.SEMANTIC,
                domain=cluster[0].domain,
                content=key_text,
                summary=f"KEY POINT: {key_text[:140]}",
                embedding=embedding,
                source_repository="chronicle",
                source_agent="consolidation",
                evidence=[c.memory_id for c in cluster],
                tags=["key_point", "distilled", cluster[0].domain],
                verified=True)
            kp.compute_confidence()
            self.store.add(kp)
            for c in cluster:
                self.graph.connect(kp.memory_id, c.memory_id, "derived_from", weight=1.0)
            key_points.append({"key_point_id": kp.memory_id, "summary": kp.summary,
                             "distilled_from": len(cluster)})
        return key_points

    def _phrase_key_point(self, summaries: List[str], domain: Optional[str]) -> str:
        joined = " ".join(summaries)
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                r = self.llm.complete(
                    system_prompt("chronicle"),
                    f"These related memories are about {domain or 'a topic'}:\n"
                    + "\n".join(f"- {s}" for s in summaries[:10])
                    + "\n\nState the single most important KEY POINT they collectively "
                      "establish, in one sentence.",
                    temperature=0.2, max_tokens=90)
                if r.ok and r.text.strip():
                    return r.text.strip()
            except Exception:
                pass
        # deterministic fallback: most frequent salient terms into a phrase
        import re
        from collections import Counter
        words = [w for w in re.findall(r"[a-z0-9]+", joined.lower()) if len(w) > 3]
        top = [w for w, _ in Counter(words).most_common(6)]
        return f"Recurring theme in {domain or 'memory'}: " + ", ".join(top)

    def _prune(self, domain: Optional[str]) -> int:
        pruned = 0
        now = time.time()
        for r in self.store.all():
            if domain and r.domain != domain:
                continue
            age_days = (now - r.updated_at) / 86400.0
            if r.total_uses == 0 and r.confidence < 0.35 and age_days > 30 and not r.verified:
                self.store.archive(r.memory_id)
                pruned += 1
        return pruned
