"""
Atlas.intelligence.analysis
==========================
Institutional text analysis: relevance, extractive summarization, claim
extraction, and CALIBRATED, corroboration-aware confidence. (Book I Part IV
Article X Decision Making; Book II Ch VI Confidence.)

An institutional desk does not trust a single source. Confidence here rewards
INDEPENDENT CORROBORATION across sources and PENALIZES thin or one-sided
evidence. Every number is computed from the actual gathered text.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

_WORD = re.compile(r"[a-z0-9]+")
_SENT = re.compile(r"(?<=[.!?])\s+")

STOPWORDS = {"the","a","an","and","or","but","of","to","in","on","for","with","is","are","was",
             "were","be","been","being","as","at","by","it","this","that","these","those","from",
             "which","who","whom","whose","what","when","where","why","how","all","any","both",
             "each","few","more","most","other","some","such","no","nor","not","only","own","same",
             "so","than","too","very","can","will","just","should","now","into","about","over",
             "then","there","here","we","they","he","she","its","their","them","has","have","had",
             "do","does","did","would"}


def tokenize(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def term_frequencies(text: str) -> Counter:
    return Counter(tokenize(text))


def relevance(query: str, document: str) -> float:
    q_tf, d_tf = term_frequencies(query), term_frequencies(document)
    if not q_tf or not d_tf:
        return 0.0
    shared = set(q_tf) & set(d_tf)
    if not shared:
        return 0.0
    dot = sum(q_tf[t] * d_tf[t] for t in shared)
    qn = math.sqrt(sum(v * v for v in q_tf.values()))
    dn = math.sqrt(sum(v * v for v in d_tf.values()))
    return round(dot / (qn * dn), 4) if qn and dn else 0.0


def keywords(text: str, top_n: int = 10) -> List[Tuple[str, int]]:
    return term_frequencies(text).most_common(top_n)


def extract_claims(text: str, max_claims: int = 6) -> List[str]:
    """Pull declarative, information-bearing sentences (candidate claims)."""
    sentences = [s.strip() for s in _SENT.split(text or "") if 30 < len(s.strip()) < 320]
    scored = []
    for s in sentences:
        toks = tokenize(s)
        if len(toks) < 5:
            continue
        # information density: unique salient terms + presence of numbers/comparatives
        density = len(set(toks)) / max(len(toks), 1)
        signal = 1.0 + (0.3 if re.search(r"\d", s) else 0) + (
            0.2 if re.search(r"\b(more|less|higher|lower|increase|decrease|because|therefore)\b", s.lower()) else 0)
        scored.append((density * signal, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:max_claims]]


def summarize(text: str, query: str = "", max_sentences: int = 4) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    sentences = [s.strip() for s in _SENT.split(text) if len(s.strip()) > 20]
    if len(sentences) <= max_sentences:
        return " ".join(sentences)
    doc_tf = term_frequencies(text)
    q_terms = set(tokenize(query)) if query else set()
    scored = []
    for idx, sent in enumerate(sentences):
        st = tokenize(sent)
        if not st:
            continue
        salience = sum(doc_tf.get(t, 0) for t in st) / len(st)
        boost = 1.0 + (len(set(st) & q_terms) * 0.5 if q_terms else 0.0)
        penalty = 1.0 / (1.0 + max(len(st) - 30, 0) / 30.0)
        scored.append((idx, salience * boost * penalty, sent))
    top = sorted(scored, key=lambda x: x[1], reverse=True)[:max_sentences]
    return " ".join(s for _, _, s in sorted(top, key=lambda x: x[0]))


def compute_corroboration(evidence: List[Any], sim_threshold: float = 0.35) -> None:
    """
    Mark how many OTHER sources corroborate each piece of evidence (independent
    agreement). Mutates evidence in place, setting `.corroboration`.
    """
    texts = [set(tokenize(f"{e.title} {e.text}")) for e in evidence]
    for i, e in enumerate(evidence):
        agree = 0
        for j, other in enumerate(evidence):
            if i == j or evidence[j].source == e.source:
                continue  # only count CROSS-source agreement (independence)
            union = texts[i] | texts[j]
            inter = texts[i] & texts[j]
            if union and len(inter) / len(union) >= sim_threshold:
                agree += 1
        e.corroboration = agree


def compute_confidence(evidence: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """
    Calibrated confidence. Beyond quantity/credibility/relevance, it rewards
    INDEPENDENT CORROBORATION and source DIVERSITY, and penalizes monoculture.
    """
    if not evidence:
        return {"confidence": 0.0, "factors": {"reason": "no evidence gathered"}}
    n = len(evidence)
    quantity = min(n / 6.0, 1.0)
    avg_cred = sum(e.get("credibility", 0.0) for e in evidence) / n
    avg_rel = sum(e.get("relevance", 0.0) for e in evidence) / n
    # source diversity: distinct sources / items (independence proxy)
    distinct = len({e.get("source") for e in evidence})
    diversity = min(distinct / 3.0, 1.0)
    # corroboration: mean cross-source agreements, saturating
    avg_corrob = sum(e.get("corroboration", 0) for e in evidence) / n
    corroboration = min(avg_corrob / 2.0, 1.0)
    # citation impact where available (peer-reviewed weight)
    cited = [e for e in evidence if e.get("citations", 0)]
    impact = min((sum(e["citations"] for e in cited) / len(cited)) / 100.0, 1.0) if cited else 0.0

    confidence = (0.15 * quantity + 0.22 * avg_cred + 0.22 * avg_rel +
                 0.16 * diversity + 0.18 * corroboration + 0.07 * impact)
    return {"confidence": round(min(max(confidence, 0.0), 0.98), 4),
            "factors": {"quantity": round(quantity, 3), "avg_credibility": round(avg_cred, 3),
                       "avg_relevance": round(avg_rel, 3), "source_diversity": round(diversity, 3),
                       "corroboration": round(corroboration, 3), "citation_impact": round(impact, 3),
                       "evidence_count": n, "distinct_sources": distinct}}
