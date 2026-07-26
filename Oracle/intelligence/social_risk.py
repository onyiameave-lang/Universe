"""
Oracle Social Risk (Pulse manipulation-detection integration)
==============================================================
Pulse already feeds aggregate social sentiment into Oracle's signal fusion
(see intelligence/signal_fusion.py -- self.pulse.sentiment_for()). What it
does NOT do yet is anything with Pulse's other real capability:
`social.manipulation`, which flags coordinated pump-and-dump-style activity
(many near-duplicate posts, same direction, same symbol, low-authenticity
accounts, in a short window -- see Pulse/intelligence/authenticity.py's
detect_manipulation()).

That's a genuinely different risk signal from ordinary bullish/bearish
sentiment: it's not "the crowd feels positive", it's "this looks like an
artificial push". A fresh entry riding a detected pump is exactly the kind
of trade Oracle should be suspicious of, win or lose -- the crowd's
conviction here isn't information, it's noise-with-intent.

This module mirrors intelligence/news_impact.py's shape on purpose (same
Assessment/Config/classify pattern, same confidence_multiplier /
size_multiplier / pause mechanism) so the two risk channels compose the
same way in trade.propose and in the Continuous Trade Manager.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SocialRiskAssessment:
    level: str                       # "none" | "medium" | "high"
    confidence_multiplier: float
    size_multiplier: float
    pause_new_entries: bool
    reason: str
    flags: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SocialRiskConfig:
    high_threshold: float = 0.7     # manipulation_score at/above this = high
    medium_threshold: float = 0.5   # detect_manipulation() only emits flags > 0.5 already


class SocialRiskClassifier:
    def __init__(self, config: Optional[SocialRiskConfig] = None):
        self.config = config or SocialRiskConfig()

    def assess(self, flags: List[Dict[str, Any]], symbol: str) -> SocialRiskAssessment:
        relevant = [f for f in flags if f.get("symbol", "").upper() == symbol.upper()]
        if not relevant:
            return SocialRiskAssessment(
                level="none", confidence_multiplier=1.0, size_multiplier=1.0,
                pause_new_entries=False, reason="no coordinated social activity detected")

        top = max(relevant, key=lambda f: f.get("manipulation_score", 0.0))
        score = top.get("manipulation_score", 0.0)

        if score >= self.config.high_threshold:
            return SocialRiskAssessment(
                level="high", confidence_multiplier=0.0, size_multiplier=0.25,
                pause_new_entries=True,
                reason=(f"coordinated {top.get('direction', '')} social activity detected "
                        f"(score={score:.2f}, {top.get('posts', 0)} posts, "
                        f"{top.get('low_authenticity_share', 0):.0%} low-authenticity accounts) "
                        f"-- likely pump/dump, not organic sentiment"),
                flags=relevant)

        # detect_manipulation() only ever returns flags with score > 0.5, so
        # anything that reaches us at all is at least "medium" by construction.
        return SocialRiskAssessment(
            level="medium", confidence_multiplier=0.7, size_multiplier=0.5,
            pause_new_entries=False,
            reason=(f"possible coordinated {top.get('direction', '')} social activity "
                    f"(score={score:.2f}) -- treating sentiment with suspicion"),
            flags=relevant)


def assess_social_risk(pulse_client, symbol: str,
                        classifier: Optional[SocialRiskClassifier] = None) -> SocialRiskAssessment:
    """
    Fetch manipulation flags for `symbol` via the Pulse agent and classify.
    Fails open on the ASSESSMENT (not on risk -- callers still apply their
    own risk gate) if Pulse is unavailable or errors, since a missing social
    feed shouldn't itself halt all trading in a degraded environment.
    """
    classifier = classifier or SocialRiskClassifier()
    if pulse_client is None:
        return SocialRiskAssessment(
            level="none", confidence_multiplier=1.0, size_multiplier=1.0,
            pause_new_entries=False, reason="no Pulse client available — social check skipped")
    try:
        result = pulse_client.act("social.manipulation", {"symbol": symbol, "topics": [symbol],
                                                            "_sender": "social_risk"})
        flags = result.get("manipulation_flags", []) if isinstance(result, dict) else []
    except Exception as exc:
        return SocialRiskAssessment(
            level="none", confidence_multiplier=1.0, size_multiplier=1.0,
            pause_new_entries=False, reason=f"social check failed ({exc}) — skipped")
    return classifier.assess(flags, symbol)