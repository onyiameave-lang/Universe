"""
Atlas.intelligence.contradiction
================================
Cross-source contradiction and corroboration analysis. (Book II Part III Ch VII
Contradiction Detection; Book VI Part II Ch III Honesty: surface disagreement,
never paper over it.)

An institutional research desk explicitly reports where sources DISAGREE. This
engine, operating on gathered evidence and their extracted claims:

  * CLUSTERS claims by semantic overlap (which claims are "about the same thing").
  * Within a cluster, detects OPPOSITION via negation + antonym/polarity cues
    and divergent numeric magnitudes.
  * Produces a CONSENSUS view (what most credible, corroborated sources say) and
    a DISSENT view (credible minority positions), each with supporting sources.

This is real, deterministic NLP (no fabricated verdicts). The LLM, if present,
is used only to phrase the consensus/dissent narrative, not to decide it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from intelligence.analysis import tokenize  # type: ignore

_NEG = re.compile(r"\b(no|not|never|cannot|can't|without|fails?|failed|false|refut|disprov|"
                  r"contradict|unlikely|ineffective|insufficient)\b", re.IGNORECASE)
POLARITY_POS = {"increase", "higher", "rise", "growth", "improve", "effective", "success",
                "supports", "confirms", "beneficial", "positive", "gain", "up", "strong"}
POLARITY_NEG = {"decrease", "lower", "fall", "decline", "worsen", "ineffective", "failure",
                "refutes", "harmful", "negative", "loss", "down", "weak"}


def _polarity(text: str) -> int:
    t = text.lower()
    score = sum(1 for w in POLARITY_POS if w in t) - sum(1 for w in POLARITY_NEG if w in t)
    if _NEG.search(t):
        score = -score  # negation flips polarity
    return (score > 0) - (score < 0)   # -1, 0, +1


def _numbers(text: str) -> List[float]:
    out = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?", text):
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _overlap(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class ContradictionEngine:
    def __init__(self, cluster_threshold: float = 0.28, llm=None):
        self.cluster_threshold = cluster_threshold
        self.llm = llm

    def analyze(self, evidence: List[Any], claims_by_source: List[Tuple[str, str, float]]
               ) -> Dict[str, Any]:
        """
        claims_by_source: list of (claim_text, source_name, credibility).
        Returns consensus, dissent, and flagged contradiction pairs.
        """
        if len(claims_by_source) < 2:
            return {"consensus": None, "dissent": [], "contradictions": [],
                   "note": "insufficient claims to assess agreement"}

        # 1. cluster claims by semantic overlap
        clusters: List[List[Tuple[str, str, float]]] = []
        for claim in claims_by_source:
            placed = False
            for cluster in clusters:
                if _overlap(claim[0], cluster[0][0]) >= self.cluster_threshold:
                    cluster.append(claim)
                    placed = True
                    break
            if not placed:
                clusters.append([claim])

        contradictions = []
        consensus_candidates = []
        dissent = []

        for cluster in clusters:
            if len(cluster) < 2:
                # lone claim: still a consensus candidate weighted by credibility
                consensus_candidates.append((cluster[0], cluster[0][2], 1))
                continue
            # polarity split within the cluster
            pos = [c for c in cluster if _polarity(c[0]) > 0]
            neg = [c for c in cluster if _polarity(c[0]) < 0]
            # numeric divergence check
            numeric_conflict = self._numeric_conflict(cluster)
            if (pos and neg) or numeric_conflict:
                # a genuine contradiction within this topic cluster
                stronger = max(cluster, key=lambda c: c[2])
                weaker_side = neg if stronger in pos else pos
                contradictions.append({
                    "topic": stronger[0][:120],
                    "position_a": {"claim": (pos[0][0] if pos else stronger[0])[:200],
                                  "sources": [c[1] for c in (pos or [stronger])]},
                    "position_b": {"claim": (neg[0][0] if neg else "")[:200],
                                  "sources": [c[1] for c in neg]},
                    "numeric_conflict": numeric_conflict,
                    "resolution_hint": f"higher-credibility source: {stronger[1]}"})
                # majority/credibility side is consensus; minority is dissent
                majority = pos if len(pos) >= len(neg) else neg
                minority = neg if majority is pos else pos
                if majority:
                    best = max(majority, key=lambda c: c[2])
                    consensus_candidates.append((best, best[2], len(majority)))
                for c in minority:
                    dissent.append({"claim": c[0][:200], "source": c[1], "credibility": c[2]})
            else:
                # agreement cluster: corroborated consensus
                best = max(cluster, key=lambda c: c[2])
                consensus_candidates.append((best, best[2], len(cluster)))

        # rank consensus by (credibility * corroboration size)
        consensus_candidates.sort(key=lambda x: x[1] * x[2], reverse=True)
        consensus = None
        if consensus_candidates:
            top = consensus_candidates[0]
            consensus = {"claim": top[0][0][:280], "source": top[0][1],
                        "credibility": round(top[1], 3), "corroborating_claims": top[2]}

        narrative = self._narrate(consensus, dissent, contradictions)
        return {"consensus": consensus, "dissent": dissent[:5],
               "contradictions": contradictions[:5], "narrative": narrative,
               "clusters_found": len(clusters)}

    def _numeric_conflict(self, cluster: List[Tuple[str, str, float]]) -> bool:
        nums = []
        for claim, _, _ in cluster:
            nums.extend(_numbers(claim))
        if len(nums) < 2:
            return False
        lo, hi = min(nums), max(nums)
        # conflict if the spread is large relative to magnitude
        if hi == 0:
            return False
        return (hi - lo) / (abs(hi) + 1e-9) > 0.5

    def _narrate(self, consensus, dissent, contradictions) -> str:
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                r = self.llm.complete(system_prompt("atlas"),
                    f"Consensus: {consensus}\nDissent: {dissent[:3]}\n"
                    f"Contradictions: {contradictions[:3]}\n\n"
                    f"Write 2-3 sentences stating what the evidence agrees on and where it "
                    f"genuinely disagrees. Be honest about uncertainty.",
                    temperature=0.2, max_tokens=200)
                if r.ok and r.text.strip():
                    return r.text.strip()
            except Exception:
                pass
        if not consensus:
            return "No clear consensus; evidence is sparse or scattered."
        base = f"Consensus ({consensus['source']}): {consensus['claim']}"
        if contradictions:
            base += f" However, {len(contradictions)} genuine disagreement(s) were found across sources."
        elif dissent:
            base += f" A minority of {len(dissent)} source(s) dissent."
        return base
