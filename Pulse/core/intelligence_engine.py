"""
Pulse.core.intelligence_engine  (Universe-oracle social-upgrade v6)
====================================================================
Multi-category, region-aware social intelligence pipeline.

Changes vs deep-fix v5
----------------------
  1. Multi-category report() — returns `categories` dict keyed by
     Finance / Tech / Entertainment / Sports / Politics / Regional / General,
     each with its own post_count, sentiment, mood, and top posts.
  2. Region-aware — PULSE_USER_REGION drives which collectors are active
     and which category gets a "regional" label in the report.
  3. `report()` now accepts `category_filter` to return only one category
     (e.g. `report finance` → only Finance posts).
  4. `trends()` returns trends broken down by category.
  5. All LLM fallback logic from deep-fix v5 is preserved exactly:
       - Chronicle cache check first (zero API cost)
       - Rule-based sentiment scoring (zero LLM cost)
       - LLM calls marked essential=False (skipped in essential_only mode)
       - Circuit breaker respected via llm.complete(essential=False)
  6. `_score_sentiment()` helper preserved from deep-fix v5 (tracks llm_used).
  7. `stats()` now includes per-category post counts and region.

Env vars
--------
    PULSE_USER_REGION           ISO country code (default "NG")
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
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from core.collectors import CollectorRegistry                                # type: ignore
except ImportError:
    from Pulse.core.collectors import CollectorRegistry                          # type: ignore
try:
    from intelligence.authenticity import (authenticity_weight, bot_risk,        # type: ignore
                                           detect_manipulation)
    from intelligence.sentiment import extract_symbols, sentiment, TrendDetector # type: ignore
except ImportError:
    from Pulse.intelligence.authenticity import (authenticity_weight, bot_risk,  # type: ignore
                                                 detect_manipulation)
    from Pulse.intelligence.sentiment import extract_symbols, sentiment, TrendDetector  # type: ignore

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

# Ordered category list for display
_CATEGORIES = [
    "Regional", "Finance", "Tech", "Sports",
    "Entertainment", "Politics", "General",
]

# Regex helpers for Chronicle cache parsing
_SENT_RE = re.compile(r"sentiment\s+([-\d.]+)", re.I)
_MOOD_RE  = re.compile(r"mood\s+(\w+)", re.I)


# ---------------------------------------------------------------------------
# FIX O-5c: Symbol alias resolution for sentiment_for()
# ---------------------------------------------------------------------------
# StockTwits posts for XAUUSD have title="GLD", "GC_F", "GOLD", "IAU" etc.
# The symbol-match step `if "XAUUSD" in p["symbols"]` returns 0 because
# extract_symbols() sees "GLD" in the title, not "XAUUSD".
# Fix: resolve all aliases to the canonical symbol before matching.
_SYMBOL_ALIASES: Dict[str, str] = {
    # Gold
    "GLD":    "XAUUSD",
    "GC_F":   "XAUUSD",
    "GOLD":   "XAUUSD",
    "IAU":    "XAUUSD",
    "XAUUSD": "XAUUSD",
    # Silver
    "SLV":    "XAGUSD",
    "SI_F":   "XAGUSD",
    "XAGUSD": "XAGUSD",
    # Oil
    "USO":    "USOIL",
    "CL_F":   "USOIL",
    "USOIL":  "USOIL",
    # Crypto
    "BTC.X":  "BTCUSD",
    "ETH.X":  "ETHUSD",
    # Indices
    "SPY":    "SPX",
    "QQQ":    "NDX",
}


def _resolve_symbol(sym: str) -> str:
    """Resolve a StockTwits/ETF ticker to its canonical Oracle symbol."""
    return _SYMBOL_ALIASES.get(sym.upper(), sym.upper())


# ─────────────────────────────────────────────────────────────────────────────
# Sentiment helper (preserved from deep-fix v5)
# ─────────────────────────────────────────────────────────────────────────────
def _score_sentiment(text: str, llm=None,
                     chronicle=None):
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
            # Top 3 posts by score
            top = sorted(group, key=lambda p: p.get("score", 0), reverse=True)[:3]
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
                             (e.g. "Finance", "Regional", "Sports")

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
                  "Regional":      { ... },   ← Nigerian content
                  "Entertainment": { ... },
                  "Sports":        { ... },
                  "Politics":      { ... },
                },
                "trending": [...],
                "manipulation": {...},
                "fallback_mode": "rule_based",
                ...
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
            cf = category_filter.strip().title()
            posts = [p for p in posts if p.get("category", "General") == cf]
            if not posts:
                return {
                    "status":        "complete",
                    "report":        None,
                    "fallback_mode": "rule_based",
                    "note":          f"no posts found for category '{cf}'",
                    "source_status": gathered["source_status"],
                }

        # ── 4. Build report (rule-based, zero LLM) ────────────────────────────
        manipulation     = detect_manipulation(posts)
        trends           = self.trends.trends(posts)
        weights          = [p["authenticity"] for p in posts]
        wsent            = sum(p["sentiment"] * w for p, w in zip(posts, weights))
        overall          = wsent / (sum(weights) or 1.0)
        mood             = next(m for thr, m in MOOD if overall >= thr)
        category_summary = self._build_category_summary(posts)
        fallback_mode    = "llm_reasoning" if gathered.get("llm_used") else "rule_based"

        report = {
            "report_id":         f"social-{uuid.uuid4().hex[:8]}",
            "timestamp":         time.time(),
            "post_count":        len(posts),
            "overall_sentiment": round(overall, 3),
            "market_mood":       mood,
            "region":            _PULSE_USER_REGION,
            "categories":        category_summary,
            "categories_found":  sorted(category_summary.keys()),
            "trending":          trends[:8],
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
        sym_upper = symbol.upper()

        def _matches(p: Dict[str, Any]) -> bool:
            """True if post p is relevant to the requested symbol.
            FIX O-5c: resolve aliases so GLD/GC_F posts count for XAUUSD."""
            for s in p.get("symbols", []):
                if _resolve_symbol(s) == sym_upper or s.upper() == sym_upper:
                    return True
            return False

        rel = [p for p in self._posts if _matches(p)]
        if not rel:
            g   = self.gather(topics=[symbol])
            rel = [p for p in g["posts"] if _matches(p)]
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
            self.chronicle.store_memory(
                content=(
                    f"Social: {report['post_count']} posts, "
                    f"mood {report['market_mood']} "
                    f"(sentiment {report['overall_sentiment']}), "
                    f"region {report.get('region', 'Global')}, "
                    f"categories [{cats}], "
                    f"{len(report['manipulation']['manipulation_flags'])} "
                    f"manipulation flags."
                ),
                pillar="social", domain="social",
                tags=["pulse", "intelligence", report["market_mood"],
                      report.get("region", "Global").lower()],
                source_repository="pulse",
            )
        except Exception:
            pass  # aegis:allow-silent

    # ── stats ─────────────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        # Per-category post counts from cached posts
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
