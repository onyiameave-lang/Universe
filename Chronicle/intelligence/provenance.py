"""
Chronicle.intelligence.provenance
================================
Provenance chains and tiered memory lifecycle. (Book II Part III Ch III Every
Memory Has Meaning; Ch VIII Memory Evolution; Book VI Part II Ch VI
Accountability.)

Institutional knowledge bases must answer: WHERE did this belief come from, and
WHY do we still trust it? This module adds two institutional capabilities on top
of the existing store:

  * PROVENANCE CHAIN: for any memory, trace its full lineage through the
    knowledge graph (derived_from / supersedes / contradicts / related edges),
    so every belief is auditable back to its evidence and origins.

  * TIERED LIFECYCLE: memories flow through HOT (active, frequently used),
    WARM (validated, occasionally used), and COLD (archival) tiers based on real
    usage, confidence, and recency. Hot memories are cheap to retrieve; cold ones
    stay preserved but out of the hot path. This is how a real system stays fast
    as knowledge scales to millions of records (Book III Ch VIII Scalability).

Deterministic and auditable. No fabrication.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set


class ProvenanceEngine:
    def __init__(self, store, graph):
        self.store = store
        self.graph = graph

    # ---- provenance chains ----

    def trace(self, memory_id: str, max_depth: int = 4) -> Dict[str, Any]:
        """
        Full lineage of a belief: what it derives from, what it superseded, what
        it contradicts, and its supporting evidence. Auditable back to origins.
        """
        rec = self.store.get(memory_id)
        if not rec:
            return {"error": "memory not found"}

        lineage = {"memory_id": memory_id, "summary": rec.summary,
                  "source_repository": rec.source_repository, "confidence": rec.confidence,
                  "verified": rec.verified, "evidence": rec.evidence,
                  "created_at": rec.created_at, "derived_from": [], "supersedes": [],
                  "contradicts": [], "related": []}

        # walk typed edges outward
        for edge_type, key in (("derived_from", "derived_from"), ("supersedes", "supersedes"),
                              ("contradicts", "contradicts"), ("related", "related")):
            for e in self.graph.neighbors(memory_id, relation=edge_type):
                target = self.store.get(e["to"])
                if target:
                    lineage[key].append({"memory_id": e["to"], "summary": target.summary,
                                       "confidence": target.confidence, "weight": e["weight"]})

        # recursively trace what this was derived from (bounded)
        lineage["origin_chain"] = self._origin_chain(memory_id, max_depth)
        lineage["auditable"] = bool(rec.evidence or lineage["origin_chain"])
        return lineage

    def _origin_chain(self, memory_id: str, max_depth: int) -> List[Dict[str, Any]]:
        chain: List[Dict[str, Any]] = []
        visited: Set[str] = {memory_id}
        current = memory_id
        depth = 0
        while depth < max_depth:
            parents = self.graph.neighbors(current, relation="derived_from")
            if not parents:
                break
            parent = parents[0]["to"]
            if parent in visited:
                break
            visited.add(parent)
            rec = self.store.get(parent)
            if not rec:
                break
            chain.append({"memory_id": parent, "summary": rec.summary,
                        "source": rec.source_repository, "confidence": rec.confidence})
            current = parent
            depth += 1
        return chain

    # ---- tiered lifecycle ----

    def classify_tier(self, rec) -> str:
        """HOT / WARM / COLD from real usage, confidence, recency."""
        age_days = (time.time() - rec.updated_at) / 86400.0
        if rec.total_uses >= 5 and age_days < 14:
            return "hot"
        if (rec.verified or rec.confidence >= 0.6 or rec.total_uses >= 1) and age_days < 120:
            return "warm"
        return "cold"

    def rebalance(self, domain: Optional[str] = None) -> Dict[str, Any]:
        """
        Recompute tiers across the store and archive genuinely cold, unused,
        low-confidence memories (preserved, not deleted).
        """
        tiers = {"hot": 0, "warm": 0, "cold": 0}
        archived = 0
        for rec in self.store.all():
            if domain and rec.domain != domain:
                continue
            tier = self.classify_tier(rec)
            tiers[tier] += 1
            rec.tags = [t for t in rec.tags if not t.startswith("tier:")] + [f"tier:{tier}"]
            if tier == "cold" and rec.total_uses == 0 and rec.confidence < 0.3 and not rec.verified:
                self.store.archive(rec.memory_id)
                archived += 1
            else:
                self.store.update(rec)
        return {"tiers": tiers, "archived_cold": archived}

    def stats(self) -> Dict[str, Any]:
        tiers = {"hot": 0, "warm": 0, "cold": 0}
        for rec in self.store.all():
            tiers[self.classify_tier(rec)] += 1
        return {"tier_distribution": tiers}
