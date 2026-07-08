"""
Chronicle.intelligence.anticipation
===================================
The anticipation engine: Chronicle predicts what knowledge each repository
will need next and pre-stages it. (Book II Part III Ch V Stage I Identity
Search "who needs this?"; Ch IX Repository-Specific Intelligence: Chronicle
dynamically adapts to the repository requesting knowledge.)

This is what made the original "AI Memory System" smart: it does not wait to
be asked. It learns each repository's access patterns from real request
history, then predicts and warms the most likely-needed memories so retrieval
is faster and more relevant.

Real mechanism (no fabrication):
  * Every retrieval is logged per requester with its query terms and the
    memory ids returned (an access trace).
  * Chronicle builds, per repository, a frequency model of the terms/domains
    it asks about and the memories it actually uses.
  * `predict(repository)` returns the memories that repository is most likely
    to need next, ranked by that repository's own historical usage + recency.
  * `warm(repository)` pre-ranks them so the next real request is instant.

Everything persists so anticipation improves the longer the ecosystem runs.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class AnticipationEngine:
    """Learns per-repository knowledge needs and predicts them ahead of time."""

    def __init__(self, store, storage_dir: str = "memory_store"):
        self.store = store
        self._lock = threading.RLock()
        self._path = Path(storage_dir) / "access_patterns.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # per-repo: term frequencies, domain frequencies, memory-use counts
        self._term_freq: Dict[str, Counter] = defaultdict(Counter)
        self._domain_freq: Dict[str, Counter] = defaultdict(Counter)
        self._mem_use: Dict[str, Counter] = defaultdict(Counter)
        self._last_seen: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for repo, terms in data.get("term_freq", {}).items():
                self._term_freq[repo] = Counter(terms)
            for repo, doms in data.get("domain_freq", {}).items():
                self._domain_freq[repo] = Counter(doms)
            for repo, mems in data.get("mem_use", {}).items():
                self._mem_use[repo] = Counter(mems)
            self._last_seen = data.get("last_seen", {})
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps({
                "term_freq": {r: dict(c) for r, c in self._term_freq.items()},
                "domain_freq": {r: dict(c) for r, c in self._domain_freq.items()},
                "mem_use": {r: dict(c) for r, c in self._mem_use.items()},
                "last_seen": self._last_seen,
            }), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    # ---- learning from real access ----

    def observe(self, requester: str, query: str, domain: Optional[str],
                returned_memory_ids: List[str]) -> None:
        """Record a real retrieval so Chronicle learns this repo's needs."""
        if not requester or requester == "unknown":
            return
        with self._lock:
            for term in _terms(query):
                self._term_freq[requester][term] += 1
            if domain:
                self._domain_freq[requester][domain] += 1
            for mid in returned_memory_ids:
                self._mem_use[requester][mid] += 1
            self._last_seen[requester] = time.time()
            self._persist()

    # ---- prediction ----

    def predict(self, repository: str, top_k: int = 5) -> Dict[str, Any]:
        """
        Predict what this repository will most likely need next, from its own
        historical term/domain/memory usage. Returns ranked memories + the
        profile that produced them (explainable).
        """
        with self._lock:
            term_model = self._term_freq.get(repository, Counter())
            domain_model = self._domain_freq.get(repository, Counter())
            mem_model = self._mem_use.get(repository, Counter())

        if not term_model and not mem_model:
            return {"repository": repository, "predictions": [],
                   "note": "no access history yet; nothing to anticipate"}

        top_terms = [t for t, _ in term_model.most_common(8)]
        top_domain = domain_model.most_common(1)[0][0] if domain_model else None

        # 1) memories this repo has used most (habitual knowledge)
        habitual = []
        for mid, count in mem_model.most_common(top_k):
            rec = self.store.get(mid)
            if rec and not rec.archived:
                habitual.append((rec, count))

        # 2) memories matching this repo's top terms it has NOT used yet (new relevant)
        candidate_new: List[Tuple[Any, float]] = []
        if top_terms:
            for rec in self.store.all():
                if rec.memory_id in mem_model:
                    continue
                text = f"{rec.summary} {' '.join(rec.tags)}".lower()
                overlap = sum(1 for t in top_terms if t in text)
                if overlap > 0 and (top_domain is None or rec.domain == top_domain):
                    candidate_new.append((rec, overlap))
            candidate_new.sort(key=lambda x: x[1], reverse=True)

        predictions = []
        for rec, weight in habitual:
            predictions.append({"memory_id": rec.memory_id, "summary": rec.summary,
                               "reason": "habitual", "weight": weight,
                               "confidence": rec.confidence})
        for rec, overlap in candidate_new[:top_k]:
            predictions.append({"memory_id": rec.memory_id, "summary": rec.summary,
                               "reason": "matches interest profile", "weight": overlap,
                               "confidence": rec.confidence})

        return {"repository": repository,
                "profile": {"top_terms": top_terms, "top_domain": top_domain},
                "predictions": predictions[:top_k * 2]}

    def warm(self, repository: str) -> Dict[str, Any]:
        """Pre-stage predicted memories (touch them so ranking is fresh)."""
        pred = self.predict(repository)
        for p in pred.get("predictions", []):
            rec = self.store.get(p["memory_id"])
            if rec:
                rec.record_use(repository, successful=True)
                self.store.update(rec)
        return {"repository": repository, "warmed": len(pred.get("predictions", []))}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"tracked_repositories": list(self._term_freq.keys()),
                   "total_requests": sum(sum(c.values()) for c in self._term_freq.values())}


def _terms(text: str) -> List[str]:
    import re
    stop = {"the", "a", "an", "of", "to", "in", "for", "and", "or", "is", "are", "what", "how", "why"}
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in stop and len(w) > 2]
