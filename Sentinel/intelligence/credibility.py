"""
Sentinel.intelligence.credibility
=================================
Source credibility, misinformation risk, and cross-source corroboration.
(Book II Part III Ch VI Confidence; Book VI Part II Honesty: rate reliability
truthfully, flag manipulation.)

A news desk must weigh WHO is reporting and WHETHER it is corroborated. This
module computes, from real signals:

  * CREDIBILITY  source prior x content-quality signals (length, specificity,
                 presence of attribution/quotes/numbers) x corroboration.
  * MISINFORMATION RISK  sensational language, absence of attribution,
                 single-source unusual claims, clickbait patterns.
  * CORROBORATION  independent cross-source agreement on the same event.

No verdict is fabricated; every score derives from observable text features.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, List

try:
    from core.collectors import SOURCE_BASE_CREDIBILITY  # type: ignore
except ImportError:
    from Sentinel.core.collectors import SOURCE_BASE_CREDIBILITY  # type: ignore

_WORD = re.compile(r"[a-z0-9]+")
SENSATIONAL = {"shocking", "unbelievable", "secret", "exposed", "destroyed", "slams",
               "you won't believe", "breaking", "bombshell", "insane", "crazy", "miracle"}
ATTRIBUTION = {"said", "according", "reported", "announced", "stated", "confirmed",
               "spokesperson", "official", "data", "report", "study"}


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def content_quality(title: str, body: str) -> Dict[str, float]:
    text = f"{title} {body}".lower()
    toks = _tokens(text)
    length_signal = min(len(toks) / 60.0, 1.0)
    has_numbers = 1.0 if re.search(r"\d", text) else 0.0
    has_quotes = 1.0 if ('"' in (title + body) or "'" in body) else 0.0
    attribution = min(sum(1 for w in ATTRIBUTION if w in text) / 3.0, 1.0)
    sensational = min(sum(1 for w in SENSATIONAL if w in text) / 2.0, 1.0)
    return {"length": round(length_signal, 3), "numbers": has_numbers, "quotes": has_quotes,
            "attribution": round(attribution, 3), "sensational": round(sensational, 3)}


def credibility_score(source: str, title: str, body: str, corroboration: int = 0) -> Dict[str, Any]:
    base = SOURCE_BASE_CREDIBILITY.get(source.lower(), SOURCE_BASE_CREDIBILITY["unknown"])
    q = content_quality(title, body)
    quality = (0.3 * q["length"] + 0.2 * q["numbers"] + 0.2 * q["quotes"] +
              0.3 * q["attribution"])
    corrob = min(corroboration / 3.0, 1.0)
    score = 0.5 * base + 0.3 * quality + 0.2 * corrob
    # sensational language drags credibility down
    score = max(0.0, score - 0.25 * q["sensational"])
    return {"credibility": round(min(score, 0.99), 3), "source_prior": base,
            "quality_signals": q, "corroboration": corroboration}


def misinformation_risk(source: str, title: str, body: str, corroboration: int = 0) -> Dict[str, Any]:
    q = content_quality(title, body)
    base = SOURCE_BASE_CREDIBILITY.get(source.lower(), 0.4)
    risk = 0.0
    reasons = []
    if q["sensational"] > 0.3:
        risk += 0.35; reasons.append("sensational language")
    if q["attribution"] < 0.2:
        risk += 0.25; reasons.append("weak attribution")
    if base < 0.5:
        risk += 0.2; reasons.append("low-credibility source")
    if corroboration == 0 and q["sensational"] > 0:
        risk += 0.2; reasons.append("uncorroborated sensational claim")
    # clickbait title pattern
    if re.search(r"(?i)(you won't believe|this one|number \d+ will|what happened next)", title):
        risk += 0.25; reasons.append("clickbait title")
    return {"misinformation_risk": round(min(risk, 1.0), 3), "reasons": reasons}


def compute_corroboration(articles: List[Dict[str, Any]], threshold: float = 0.4) -> None:
    """Set each article's cross-source corroboration count (independent agreement)."""
    tok_sets = [set(_tokens(a.get("title", "") + " " + a.get("summary", ""))) for a in articles]
    for i, a in enumerate(articles):
        agree = 0
        for j, other in enumerate(articles):
            if i == j or articles[j].get("source") == a.get("source"):
                continue
            union = tok_sets[i] | tok_sets[j]
            inter = tok_sets[i] & tok_sets[j]
            if union and len(inter) / len(union) >= threshold:
                agree += 1
        a["corroboration"] = agree
