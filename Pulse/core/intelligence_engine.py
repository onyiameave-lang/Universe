"""
Pulse.core.intelligence_engine  (Universe-oracle social-upgrade v7 — bug-fix)
==============================================================================
Bug fix release on top of v6 (social-upgrade).

Changes vs v6
-------------
  Bug 6 — trending[] always empty
    Root cause: `TrendDetector.trends()` only tracks posts that have a
    `symbols` field. Posts from Nairaland, Google Trends, and general Reddit
    subs have no symbols → TrendDetector finds nothing → trending=[].
    Fix: build trending from TWO sources merged together:
      a) Symbol-based trending (TrendDetector — Finance/Crypto posts with
         explicit ticker symbols like BTC, SPY, XAUUSD)
      b) Topic-based trending: top posts by engagement (score + comments)
         across ALL categories, one representative per category, deduplicated
         by title similarity (Jaccard on word sets).
    The merged list is sorted by a combined velocity+engagement score and
    capped at 12 items.

All other changes from v6 (multi-category, region-aware, Chronicle cache,
LLM essential=False fallback chain) are preserved exactly.

Env vars
--------
    PULSE_USER_REGION              ISO country code (default "NG")
    PULSE_CHRONICLE_CACHE_TTL_SEC  max age of cached Chronicle report (default 300 s)
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.collectors import CollectorRegistry                                    # type: ignore
from intelligence.authenticity import (authenticity_weight, bot_risk,            # type: ignore
                                       detect_manipulation)
from intelligence.sentiment import extract_symbols, sentiment, TrendDetector     # type: ignore

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
MOOD = [
    (0.6,  "euphoric"),
    (0.2,  "bullish"),
    (-0.2, "neutral"),
    (-0.6, "bearish"),
    (-2.0, "fearful"),
]

_CHRONICLE_CACHE_TTL = float(os.getenv("PULSE_CHRONICLE_CACHE_TTL_SEC", "300"))
_PULSE_USER_REGION   = os.getenv("PULSE_USER_REGION", "NG").upper()

_CATEGORIES = [
    "Regional", "Finance", "Tech", "Sports",
    "Entertainment", "Politics", "General",
]

_SENT_RE = re.compile(r"sentiment\s+([-\d.]+)", re.I)
_MOOD_RE  = re.compile(r"mood\s+(\w+)", re.I)


# ─────────────────────────────────────────────────────────────────────────────
# Sentiment helper (preserved from deep-fix v5)
# ─────────────────────────────────────────────────────────────────────────────
def _score_sentiment(text: str, llm=None, chronicle=None):
    """
    Thin wrapper around sentiment() that also reports whether the LLM was used.
    Returns (score: float, llm_was_used: bool).
    """
    calls_before = 0
    if llm is not None:
        try:
            calls_before = llm.stats().get("total_calls", 0)
        except Exception:
            pass

    score = sentiment(text, llm=llm, chronicle=chronicle)

    llm_used = False
    if llm is not None:
        try:
            calls_after = llm.stats().get("total_calls", 0)
            llm_used    = calls_after > calls_before
        except Exception:
            pass

    return score, llm_used


# ─────────────────────────────────────────────────────────────────────────────
# BUG 6 FIX — Topic-based trending builder
# ─────────────────────────────────────────────────────────────────────────────
def _word_set(text: str) -> Set[str]:
    """Return a set of lowercase words (≥3 chars) from text."""
    return {w for w in re.findall(r"\b[a-z]{3,}\b", text.lower())}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _build_trending(
        posts: List[Dict[str, Any]],
        trend_detector: TrendDetector,
        max_items: int = 12,
) -> List[Dict[str, Any]]:
    """
    BUG 6 FIX: Build a trending list from two sources:

    Source A — Symbol-based (TrendDetector):
        Works for Finance/Crypto posts that have explicit ticker symbols.
        Returns velocity-ranked symbols with mention counts.

    Source B — Topic-based (engagement-ranked):
        For every category, pick the top post by (score + comments).
        This covers Nairaland, Google Trends, and general Reddit posts
        that have no symbols field.

    Merge A + B, deduplicate by title similarity (Jaccard ≥ 0.5),
    sort by combined score, cap at max_items.
    """
    trending: List[Dict[str, Any]] = []

    # ── Source A: symbol-based trending ──────────────────────────────────────
    try:
        sym_trends = trend_detector.trends(posts)
        for t in sym_trends:
            trending.append({
                "type":     "symbol",
                "symbol":   t.get("symbol", ""),
                "title":    t.get("symbol", ""),
                "category": "Finance",
                "score":    t.get("velocity", 0) * 10,  # normalise to ~engagement scale
                "platform": t.get("platforms", ["unknown"])[0] if t.get("platforms") else "unknown",
                "url":      "",
                "region":   "Global",
                "velocity": t.get("velocity", 0),
                "mentions": t.get("mentions", 0),
            })
    except Exception:
        pass

    # ── Source B: topic-based trending (per-category top post) ───────────────
    by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in posts:
        by_cat[p.get("category", "General")].append(p)

    for cat, group in by_cat.items():
        if not group:
            continue
        # Sort by engagement: score + comments (both may be 0 for Nairaland/Trends)
        # Secondary sort: authenticity (higher = more trustworthy)
        top = sorted(
            group,
            key=lambda p: (
                p.get("score", 0) + p.get("comments", 0),
                p.get("authenticity", 0.5),
            ),
            reverse=True,
        )
        for p in top[:2]:  # up to 2 per category
            title = p.get("title", "").strip()
            if not title:
                continue
            trending.append({
                "type":     "topic",
                "symbol":   "",
                "title":    title[:120],
                "category": cat,
                "score":    p.get("score", 0) + p.get("comments", 0),
                "platform": p.get("platform", "unknown"),
                "url":      p.get("url", ""),
                "region":   p.get("region", "Global"),
                "velocity": 0,
                "mentions": 1,
            })

    # ── Deduplicate by title similarity ──────────────────────────────────────
    deduped: List[Dict[str, Any]] = []
    seen_word_sets: List[Set[str]] = []

    for item in trending:
        ws = _word_set(item["title"])
        if not ws:
            continue
        # Check if too similar to an already-accepted item
        duplicate = any(_jaccard(ws, s) >= 0.5 for s in seen_word_sets)
        if not duplicate:
            deduped.append(item)
            seen_word_sets.append(ws)

    # ── Sort: symbols first (velocity), then topics (engagement) ─────────────
    deduped.sort(
        key=lambda t: (
            1 if t["type"] == "symbol" else 0,  # symbols first
            t["score"],
        ),
        reverse=True,
    )

    return deduped[:max_items]


# ─────────────────────────────────────────────────────────────────────────────
# IntelligenceEngine
# ─────────────────────────────────────────────────────────────────────────────
class IntelligenceEngine:
    def __init__(self, chronicle_client=None, llm=None):
        self.chronicle  = chronicle_client
        self.llm        = llm
        self.collectors = CollectorRegistry()
        self.trends     = TrendDetector()
        self._posts: List[Dict[str, Any]] = []

    # ── Chronicle cache helpers ───────────────────────────────────────────────
    def _chronicle_recent_report(self) -> Optional[Dict[str, Any]]:
        """Return a cached report from Chronicle if one exists within TTL."""
        if self.chronicle is None:
            return None
        try:
            results = self.chronicle.search(
                query="Social: posts mood sentiment manipulation",
                memory_type="social", limit=1,
            )
            if not results:
                return None
            entry = results[0]
            ts = entry.get("timestamp") or entry.get("created_at") or 0
            if ts and (time.time() - float(ts)) < _CHRONICLE_CACHE_TTL:
                content = entry.get("content", "")
                sm = _SENT_RE.search(content)
                mm = _MOOD_RE.search(content)
                if sm and mm:
                    return {
                        "report_id":         f"social-cached-{uuid.uuid4().hex[:8]}",
                        "timestamp":         float(ts),
                        "post_count":        0,
                        "overall_sentiment": float(sm.group(1)),
                        "market_mood":       mm.group(1),
                        "trending":          [],
                        "manipulation":      {"flagged": False, "manipulation_flags": []},
                        "platforms":         [],
                        "avg_authenticity":  0.0,
                        "source_status":     {},
                        "categories":        {},
                        "region":            _PULSE_USER_REGION,
                        "fallback_mode":     "chronicle_cache",
                    }
        except Exception:
            pass
        return None

    # ── gather ────────────────────────────────────────────────────────────────
    def gather(self, topics=None, sources=None, limit=10) -> Dict[str, Any]:
        started  = time.time()
        raw      = self.collectors.collect_by_category(
            topics=topics, sources=sources, limit=limit)
        posts    = [p.to_dict() for p in raw["posts"]]
        llm_used = False

        for p in posts:
            p["symbols"]      = extract_symbols(p["title"] + " " + p["content"])
            score, used_llm   = _score_sentiment(
                p["title"] + " " + p["content"],
                llm=self.llm,
                chronicle=self.chronicle,
            )
            p["sentiment"]    = score
            p["authenticity"] = authenticity_weight(p)
            p["bot_risk"]     = bot_risk(p)["bot_risk"]
            if used_llm:
                llm_used = True

        self._posts.extend(posts)
        return {
            "posts":            posts,
            "source_status":    raw["source_status"],
            "by_category":      raw.get("by_category", {}),
            "categories_found": raw.get("categories_found", []),
            "count":            len(posts),
            "duration_sec":     round(time.time() - started, 2),
            "llm_used":         llm_used,
        }

    # ── _build_category_summary ───────────────────────────────────────────────
    def _build_category_summary(
            self, posts: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Group posts by category and compute per-category sentiment + mood.
        Returns a dict keyed by category name.
        """
        by_cat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for p in posts:
            cat = p.get("category", "General")
            by_cat[cat].append(p)

        summary: Dict[str, Any] = {}
        for cat in _CATEGORIES:
            group = by_cat.get(cat, [])
            if not group:
                continue
            weights = [p.get("authenticity", 0.5) for p in group]
            wsent   = sum(p.get("sentiment", 0) * w
                          for p, w in zip(group, weights))
            overall = wsent / (sum(weights) or 1.0)
            mood    = next(m for thr, m in MOOD if overall >= thr)
            top     = sorted(group, key=lambda p: p.get("score", 0), reverse=True)[:3]
            summary[cat] = {
                "post_count":        len(group),
                "overall_sentiment": round(overall, 3),
                "mood":              mood,
                "platforms":         list({p["platform"] for p in group}),
                "top_posts": [
                    {
                        "title":    p["title"][:120],
                        "url":      p.get("url", ""),
                        "platform": p["platform"],
                        "score":    p.get("score", 0),
                        "region":   p.get("region", "Global"),
                    }
                    for p in top
                ],
            }
        return summary

    # ── report ────────────────────────────────────────────────────────────────
    def report(self, topics=None, sources=None,
               category_filter: Optional[str] = None) -> Dict[str, Any]:
        """
        Full multi-category social intelligence report.

        Args:
            topics:          list of topic strings (e.g. ["crypto", "nigeria"])
            sources:         list of collector names to use
            category_filter: if set, return only posts from this category

        Returns:
            {
              "status": "complete",
              "report": {
                "report_id": ...,
                "post_count": N,
                "overall_sentiment": 0.041,
                "market_mood": "neutral",
                "region": "NG",
                "categories": {
                  "Finance":       { post_count, sentiment, mood, top_posts },
                  "Tech":          { ... },
                  "Regional":      { ... },
                  "Entertainment": { ... },
                  "Sports":        { ... },
                  "Politics":      { ... },
                },
                "trending": [
                  { "type": "symbol", "symbol": "BTC", "title": "BTC", ... },
                  { "type": "topic",  "title": "Nigeria fuel price...", ... },
                  ...
                ],
                "manipulation": {...},
                "fallback_mode": "rule_based",
              }
            }
        """
        # ── 1. Chronicle cache (zero API cost) ────────────────────────────────
        cached = self._chronicle_recent_report()
        if cached is not None:
            return {
                "status":        "complete",
                "report":        cached,
                "fallback_mode": "chronicle_cache",
            }

        # ── 2. Collect raw posts ──────────────────────────────────────────────
        gathered = self.gather(topics=topics, sources=sources)
        posts    = gathered["posts"]

        if not posts:
            return {
                "status":        "complete",
                "report":        None,
                "fallback_mode": "rule_based",
                "note":          "no social posts gathered; "
                                 "platforms unreachable or no matches",
                "source_status": gathered["source_status"],
            }

        # ── 3. Apply category filter if requested ─────────────────────────────
        if category_filter:
            cf    = category_filter.strip().title()
            posts = [p for p in posts if p.get("category", "General") == cf]
            if not posts:
                return {
                    "status":        "complete",
                    "report":        None,
                    "fallback_mode": "rule_based",
                    "note":          f"no posts found for category '{cf}'",
                    "source_status": gathered["source_status"],
                }

        # ── 4. Build report ───────────────────────────────────────────────────
        manipulation     = detect_manipulation(posts)
        weights          = [p["authenticity"] for p in posts]
        wsent            = sum(p["sentiment"] * w for p, w in zip(posts, weights))
        overall          = wsent / (sum(weights) or 1.0)
        mood             = next(m for thr, m in MOOD if overall >= thr)
        category_summary = self._build_category_summary(posts)
        fallback_mode    = "llm_reasoning" if gathered.get("llm_used") else "rule_based"

        # BUG 6 FIX: use _build_trending() which covers both symbol-based
        # and topic-based trending (Nairaland, Google Trends, general Reddit)
        trending_list = _build_trending(posts, self.trends, max_items=12)

        report = {
            "report_id":         f"social-{uuid.uuid4().hex[:8]}",
            "timestamp":         time.time(),
            "post_count":        len(posts),
            "overall_sentiment": round(overall, 3),
            "market_mood":       mood,
            "region":            _PULSE_USER_REGION,
            "categories":        category_summary,
            "categories_found":  sorted(category_summary.keys()),
            "trending":          trending_list,
            "manipulation":      manipulation,
            "platforms":         list({p["platform"] for p in posts}),
            "avg_authenticity":  round(sum(weights) / len(posts), 3),
            "source_status":     gathered["source_status"],
            "fallback_mode":     fallback_mode,
        }

        self._preserve(report)
        return {"status": "complete", "report": report,
                "fallback_mode": fallback_mode}

    # ── sentiment_for ─────────────────────────────────────────────────────────
    def sentiment_for(self, symbol: str) -> Dict[str, Any]:
        rel = [p for p in self._posts
               if symbol.upper() in [s.upper() for s in p.get("symbols", [])]]
        if not rel:
            g   = self.gather(topics=[symbol])
            rel = [p for p in g["posts"]
                   if symbol.upper() in [s.upper() for s in p.get("symbols", [])]]
        if not rel:
            return {"symbol": symbol, "sentiment": 0.0,
                    "post_count": 0, "confidence": 0.0}
        weights = [p["authenticity"] for p in rel]
        wsent   = sum(p["sentiment"] * w for p, w in zip(rel, weights))
        manip   = detect_manipulation(rel, symbol=symbol)
        return {
            "symbol":               symbol,
            "sentiment":            round(wsent / (sum(weights) or 1.0), 3),
            "post_count":           len(rel),
            "confidence":           round(min(len(rel) / 20.0, 1.0), 3),
            "avg_authenticity":     round(sum(weights) / len(rel), 3),
            "platforms":            list({p["platform"] for p in rel}),
            "manipulation_warning": manip["flagged"],
        }

    # ── _preserve ─────────────────────────────────────────────────────────────
    def _preserve(self, report: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            cats = ", ".join(report.get("categories_found", []))
            self.chronicle.store(
                content=(
                    f"Social: {report['post_count']} posts, "
                    f"mood {report['market_mood']} "
                    f"(sentiment {report['overall_sentiment']}), "
                    f"region {report.get('region', 'Global')}, "
                    f"categories [{cats}], "
                    f"{len(report['manipulation']['manipulation_flags'])} "
                    f"manipulation flags."
                ),
                memory_type="social", domain="social",
                tags=["pulse", "intelligence", report["market_mood"],
                      report.get("region", "Global").lower()],
                source="pulse",
            )
        except Exception:
            pass  # aegis:allow-silent

    # ── stats ─────────────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        cat_counts: Dict[str, int] = defaultdict(int)
        for p in self._posts:
            cat_counts[p.get("category", "General")] += 1

        return {
            "posts_cached":    len(self._posts),
            "region":          _PULSE_USER_REGION,
            "collectors":      {
                n: getattr(c, "available", False)
                for n, c in self.collectors.collectors.items()
            },
            "category_counts": dict(cat_counts),
        }
