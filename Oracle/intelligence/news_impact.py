"""
Oracle News Intelligence
========================
Roadmap Phase 1, Item 2: "This is essential before real trading."

Responsibilities (from the roadmap):
  If high-impact news appears:
    - Pause new entries
    - Recalculate confidence
    - Reduce position size
    - Tighten stops
    - Exit if risk becomes unacceptable
  Oracle should never ignore major news.

What this module is
--------------------
A pure, offline-testable classifier: given a list of news articles (as
returned by Sentinel's engine -- each already enriched with event_type,
sentiment, credibility, misinformation_risk, published_at), it decides
whether a HIGH or MEDIUM impact event is currently active for a symbol,
and translates that into concrete multipliers/actions the rest of Oracle
can apply mechanically:

  - `confidence_multiplier` -- multiply the fused signal confidence by
    this before risk-gating a NEW trade. A high-impact event drives this
    to 0.0, which makes the existing risk gate reject the trade outright
    (reusing risk.py's confidence floor instead of adding a parallel
    "paused" special case).
  - `size_multiplier` -- available for anywhere position size is computed
    directly from confidence/severity rather than through the risk gate.
  - `pause_new_entries` -- explicit flag, for call sites that want to
    short-circuit before even building a signal.
  - feeds into execution.trade_manager.MarketSnapshot.news_impact so the
    Continuous Trade Manager can tighten stops / exit open positions too.

No network calls happen here. Callers are responsible for fetching
articles (typically via `sentinel_agent.act("news.credibility", ...)`)
and handing them to `NewsImpactClassifier.assess()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional


# Event categories (from Sentinel's intelligence.analysis.EVENT_TYPES) that
# are structurally capable of moving an entire asset class, not just one
# name -- these are the ones that justify pausing new entries outright.
HIGH_IMPACT_EVENT_TYPES = {"monetary_policy", "geopolitical", "macro"}

# Categories that matter but are usually narrower in blast radius (a single
# commodity, a specific trade lane) -- these dampen confidence/size but
# don't halt trading on their own.
MEDIUM_IMPACT_EVENT_TYPES = {"commodity", "trade", "corporate"}

# Words that signal an event is a surprise/shock rather than a scheduled,
# already-priced-in release. Each hit nudges severity up.
_SHOCK_TERMS = (
    "unexpected", "surprise", "surprised", "shock", "shocking", "emergency",
    "crisis", "sudden", "unprecedented", "plunge", "plunges", "surge",
    "surges", "crash", "crashes", "spike", "spikes", "collapse", "collapses",
    "halt", "halted", "panic",
)

# How long a news article stays "live" for impact purposes. Older than
# this and it's assumed already priced in / no longer news.
DEFAULT_RECENCY_WINDOW = timedelta(hours=3)


@dataclass
class NewsImpactAssessment:
    level: str                       # "none" | "medium" | "high"
    confidence_multiplier: float
    size_multiplier: float
    pause_new_entries: bool
    reason: str
    driving_articles: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class NewsImpactConfig:
    recency_window: timedelta = DEFAULT_RECENCY_WINDOW
    # Severity score thresholds (see _score_article) that separate the
    # three levels. Tune these against real article volume once you have
    # live data; they're deliberately conservative defaults.
    high_threshold: float = 3.0
    medium_threshold: float = 1.5
    min_credibility: float = 0.35    # articles below this barely count at all


class NewsImpactClassifier:
    def __init__(self, config: Optional[NewsImpactConfig] = None):
        self.config = config or NewsImpactConfig()

    # -- public API ------------------------------------------------------

    def assess(self, articles: List[Dict[str, Any]], symbol: str) -> NewsImpactAssessment:
        now = datetime.now(timezone.utc)
        scored = []
        for a in articles:
            age = self._age(a.get("published_at", ""), now)
            if age is not None and age > self.config.recency_window:
                continue   # stale, no longer "current" news
            score = self._score_article(a)
            if score > 0:
                scored.append((score, a))

        if not scored:
            return NewsImpactAssessment(
                level="none", confidence_multiplier=1.0, size_multiplier=1.0,
                pause_new_entries=False, reason="no current high/medium-impact news found")

        scored.sort(key=lambda t: t[0], reverse=True)
        top_score, top_article = scored[0]
        driving = [
            {"title": a.get("title", ""), "event_type": a.get("event_type", ""),
             "score": round(s, 2)}
            for s, a in scored[:5]
        ]

        if top_score >= self.config.high_threshold:
            return NewsImpactAssessment(
                level="high", confidence_multiplier=0.0, size_multiplier=0.25,
                pause_new_entries=True,
                reason=(f"high-impact {top_article.get('event_type', 'news')} event: "
                        f"\"{top_article.get('title', '')[:80]}\""),
                driving_articles=driving)

        if top_score >= self.config.medium_threshold:
            return NewsImpactAssessment(
                level="medium", confidence_multiplier=0.7, size_multiplier=0.5,
                pause_new_entries=False,
                reason=(f"medium-impact {top_article.get('event_type', 'news')} event: "
                        f"\"{top_article.get('title', '')[:80]}\""),
                driving_articles=driving)

        return NewsImpactAssessment(
            level="none", confidence_multiplier=1.0, size_multiplier=1.0,
            pause_new_entries=False, reason="news present but below impact threshold",
            driving_articles=driving)

    # -- helpers -----------------------------------------------------------

    def _score_article(self, a: Dict[str, Any]) -> float:
        event_type = a.get("event_type", "")
        credibility = float(a.get("credibility", 0.5) or 0.5)
        misinfo = float(a.get("misinformation_risk", 0.0) or 0.0)
        sentiment_val = abs(float(a.get("sentiment", 0.0) or 0.0))
        title = (a.get("title", "") or "").lower()
        summary = (a.get("summary", "") or "").lower()

        if credibility < self.config.min_credibility:
            return 0.0   # don't let unreliable sources drive trading decisions

        base = 0.0
        if event_type in HIGH_IMPACT_EVENT_TYPES:
            base = 3.0
        elif event_type in MEDIUM_IMPACT_EVENT_TYPES:
            base = 1.0

        if base == 0.0:
            return 0.0   # not a market-moving category at all

        shock_hits = sum(1 for term in _SHOCK_TERMS if term in title or term in summary)
        shock_bonus = min(shock_hits, 3) * 0.5

        sentiment_bonus = sentiment_val * 1.0   # 0..1 range typically

        # Credibility scales the whole thing down smoothly (a 0.9-credibility
        # source counts almost fully; a 0.4-credibility source barely counts).
        # Misinformation risk discounts further.
        score = (base + shock_bonus + sentiment_bonus) * credibility * (1.0 - misinfo)
        return score

    @staticmethod
    def _age(published_at: str, now: datetime) -> Optional[timedelta]:
        if not published_at:
            return None
        dt = None
        try:
            dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
        if dt is None:
            try:
                dt = parsedate_to_datetime(published_at)
            except (ValueError, TypeError, IndexError):
                return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return now - dt


# ---------------------------------------------------------------------------
# Convenience wrapper for call sites that have a Sentinel client but not a
# pre-fetched article list (e.g. oracle_agent.py, live_trader.py).
# ---------------------------------------------------------------------------

def assess_news_impact(sentinel_client, symbol: str,
                        classifier: Optional[NewsImpactClassifier] = None) -> NewsImpactAssessment:
    """
    Fetch articles for `symbol` via the Sentinel agent and classify impact.
    Returns a "none" assessment (fail open on the ASSESSMENT, not on risk --
    callers still apply their own risk gate) if Sentinel is unavailable or
    errors, since a missing news feed shouldn't itself halt all trading in a
    degraded environment, but IS logged so it's visible.
    """
    classifier = classifier or NewsImpactClassifier()
    if sentinel_client is None:
        return NewsImpactAssessment(
            level="none", confidence_multiplier=1.0, size_multiplier=1.0,
            pause_new_entries=False, reason="no Sentinel client available — news check skipped")
    try:
        result = sentinel_client.act("news.credibility", {"topics": [symbol], "_sender": "news_impact"})
        articles = result.get("articles", []) if isinstance(result, dict) else []
    except Exception as exc:
        return NewsImpactAssessment(
            level="none", confidence_multiplier=1.0, size_multiplier=1.0,
            pause_new_entries=False, reason=f"news fetch failed ({exc}) — news check skipped")
    return classifier.assess(articles, symbol)