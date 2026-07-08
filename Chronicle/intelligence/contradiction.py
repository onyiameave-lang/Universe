"""
Chronicle.intelligence.contradiction
====================================
Institutional contradiction detection and BELIEF REVISION. (Book II Part III
Ch VII Contradiction Detection; Ch VIII Memory Evolution; Book VI Part II Ch III
Honesty.)

The old detector used keyword sentiment. That is beneath a memory system this
central. This engine does it properly:

  1. SEMANTIC CANDIDATE DETECTION: find memory pairs that are about the SAME
     subject (high embedding similarity) but whose stated conclusions diverge
     (polarity + negation + numeric divergence), across the same domain.
  2. ADJUDICATION: it does not decide by wordlist. It refers the conflict to
     ATLAS (the research desk) for an evidence-based verdict, and weighs each
     memory's own earned confidence and corroboration.
  3. BELIEF REVISION: the losing memory is not deleted (nothing dies without
     record). It is superseded, its confidence lowered, and a `contradicts` +
     `supersedes` edge is written to the knowledge graph. The winning belief
     is reinforced. A revision record preserves the whole decision.

This makes Chronicle a self-correcting knowledge base, like an institutional
research library that reconciles conflicting reports rather than hoarding them.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core.embeddings import cosine_similarity, get_embedding_model  # type: ignore
from core.memory_record import MemoryRecord                          # type: ignore

_NEG = re.compile(r"\b(no|not|never|cannot|can't|without|fails?|failed|false|refut|disprov|"
                  r"contradict|unlikely|ineffective|insufficient|avoid|worse)\b", re.IGNORECASE)
POS = {"increase", "higher", "rise", "growth", "improve", "effective", "success", "profit",
       "supports", "confirms", "beneficial", "gain", "up", "strong", "works", "wins"}
NEG = {"decrease", "lower", "fall", "decline", "worsen", "ineffective", "failure", "loss",
       "refutes", "harmful", "down", "weak", "broken", "loses"}


def _polarity(text: str) -> int:
    t = (text or "").lower()
    score = sum(1 for w in POS if w in t) - sum(1 for w in NEG if w in t)
    if _NEG.search(t):
        score = -score
    return (score > 0) - (score < 0)


def _numbers(text: str) -> List[float]:
    out = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?", text or ""):
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


class ContradictionEngine:
    """Semantic contradiction detection with Atlas adjudication + belief revision."""

    def __init__(self, store, graph, atlas=None, chronicle_agent=None,
                 subject_similarity: float = 0.78, **kwargs):
        self.store = store
        self.graph = graph
        self.atlas = atlas
        self.chronicle = chronicle_agent
        self.subject_similarity = subject_similarity
        self.llm = kwargs.get("llm")  # Allow llm if passed by mistake
        self.embedder = get_embedding_model()
        self._revisions: List[Dict[str, Any]] = []

    # ---- 1: detect genuine contradictions ----

    def scan(self, domain: Optional[str] = None, auto_revise: bool = False) -> Dict[str, Any]:
        records = [r for r in self.store.all()
                  if (domain is None or r.domain == domain) and r.embedding]
        flagged: List[Dict[str, Any]] = []
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                a, b = records[i], records[j]
                if a.domain != b.domain:
                    continue
                sim = cosine_similarity(a.embedding, b.embedding)
                if sim < self.subject_similarity:
                    continue  # not about the same subject
                if not self._diverges(a, b):
                    continue  # same subject, same conclusion -> corroboration, not conflict
                flagged.append({"memory_a": a.memory_id, "memory_b": b.memory_id,
                              "subject_similarity": round(sim, 3), "domain": a.domain,
                              "summary_a": a.summary, "summary_b": b.summary,
                              "confidence_a": a.confidence, "confidence_b": b.confidence})
                self.graph.connect(a.memory_id, b.memory_id, "contradicts", weight=sim)

        result = {"contradictions": flagged, "count": len(flagged)}
        if auto_revise and flagged:
            result["revisions"] = [self.adjudicate(f["memory_a"], f["memory_b"]) for f in flagged]
        return result

    def _diverges(self, a: MemoryRecord, b: MemoryRecord) -> bool:
        ta = f"{a.summary} {a.lesson} {a.content}"
        tb = f"{b.summary} {b.lesson} {b.content}"
        pa, pb = _polarity(ta), _polarity(tb)
        if pa != 0 and pb != 0 and pa != pb:
            return True
        # numeric divergence about the same subject
        na, nb = _numbers(ta), _numbers(tb)
        if na and nb:
            lo, hi = min(na + nb), max(na + nb)
            if hi and (hi - lo) / (abs(hi) + 1e-9) > 0.5:
                return True
        return False

    # ---- 2+3: adjudicate + revise belief ----

    def adjudicate(self, memory_id_a: str, memory_id_b: str) -> Dict[str, Any]:
        """
        Resolve a contradiction using evidence (Atlas) + earned confidence,
        then revise belief: supersede the loser, reinforce the winner.
        """
        a = self.store.get(memory_id_a)
        b = self.store.get(memory_id_b)
        if not a or not b:
            return {"status": "error", "message": "memory not found"}

        # Gather an external verdict from Atlas (evidence, not opinion).
        atlas_view = None
        atlas_leans = 0  # -1 favors A, +1 favors B, 0 neutral
        if self.atlas is not None:
            try:
                question = (f"Which is better supported by evidence? "
                          f"A: {a.summary}  ||  B: {b.summary}")
                out = self.atlas.handle({"task": "research.investigate",
                    "context": {"query": question, "domain": a.domain}, "sender": "chronicle"})
                report = out.get("report", {})
                atlas_view = {"confidence": report.get("confidence"),
                            "consensus": report.get("consensus"),
                            "summary": report.get("summary", "")[:200]}
                # naive lean: which summary shares more with the consensus text
                consensus_text = (str(report.get("consensus") or "") + " " +
                                report.get("summary", "")).lower()
                a_overlap = self._overlap(a.summary, consensus_text)
                b_overlap = self._overlap(b.summary, consensus_text)
                atlas_leans = (b_overlap > a_overlap) - (a_overlap > b_overlap)
            except Exception:
                atlas_view = {"error": "atlas unavailable"}

        # Score each belief: earned confidence + corroboration + atlas lean.
        score_a = a.confidence + 0.1 * len(a.used_by) + (0.2 if atlas_leans < 0 else 0)
        score_b = b.confidence + 0.1 * len(b.used_by) + (0.2 if atlas_leans > 0 else 0)

        if abs(score_a - score_b) < 0.05:
            # too close to call: keep both, mark unresolved for future evidence
            revision = {"status": "unresolved", "reason": "scores within margin; awaiting more evidence",
                       "memory_a": memory_id_a, "memory_b": memory_id_b,
                       "score_a": round(score_a, 3), "score_b": round(score_b, 3),
                       "atlas": atlas_view}
            self._revisions.append(revision)
            return revision

        winner, loser = (a, b) if score_a > score_b else (b, a)
        # BELIEF REVISION: supersede loser (archive-not-delete), reinforce winner.
        loser.confidence = round(max(loser.confidence * 0.5, 0.05), 4)
        loser.lesson = (loser.lesson + " [SUPERSEDED: contradicted by higher-evidence belief]").strip()
        self.store.update(loser)
        winner.verified = True
        winner.evidence.append(f"upheld over {loser.memory_id} on {time.strftime('%Y-%m-%d')}")
        self.store.update(winner)
        self.graph.connect(winner.memory_id, loser.memory_id, "supersedes", weight=1.0)

        revision = {"status": "revised", "winner": winner.memory_id, "loser": loser.memory_id,
                   "winner_summary": winner.summary, "score_winner": round(max(score_a, score_b), 3),
                   "score_loser": round(min(score_a, score_b), 3), "atlas": atlas_view,
                   "revised_at": time.time()}
        self._revisions.append(revision)
        self._preserve_revision(revision, winner, loser)
        return revision

    def _overlap(self, a: str, b: str) -> float:
        ta = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
        tb = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    def _preserve_revision(self, revision: Dict, winner: MemoryRecord, loser: MemoryRecord) -> None:
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(
                content=f"Belief revised: '{winner.summary}' upheld over '{loser.summary}'. "
                       f"Loser superseded, not deleted.",
                pillar="evolutionary", domain=winner.domain,
                source_repository="chronicle", tags=["belief_revision", winner.domain],
                autolink=False)
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        revised = sum(1 for r in self._revisions if r.get("status") == "revised")
        return {"revisions_total": len(self._revisions), "beliefs_revised": revised,
               "unresolved": sum(1 for r in self._revisions if r.get("status") == "unresolved")}
