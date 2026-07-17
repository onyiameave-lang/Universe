"""
Pulse.core.intelligence_engine  (Universe-oracle deep-fix v5)
=============================================================
Social intelligence pipeline.

Changes in this version:
  * gather() passes chronicle to sentiment() so Layer 4 (Chronicle lookup)
    is available before any LLM call is attempted.
  * report() checks Chronicle for a recent cached report FIRST — if one exists
    within PULSE_CHRONICLE_CACHE_TTL_SEC (default 300 s), returns it immediately
    with zero API calls and fallback_mode="chronicle_cache".
  * All results carry "fallback_mode" so callers can observe which path was used:
      "chronicle_cache"  — served from Chronicle memory, zero API calls
      "rule_based"       — all sentiment scored by lexicon/heuristic, zero LLM
      "llm_reasoning"    — LLM was used for at least one post
  * sentiment_for() also passes chronicle to sentiment().
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.collectors import CollectorRegistry                                    # type: ignore
from intelligence.authenticity import (authenticity_weight, bot_risk,            # type: ignore
                                       detect_manipulation)
from intelligence.sentiment import extract_symbols, sentiment, TrendDetector     # type: ignore

MOOD = [
    (0.6,  "euphoric"),
    (0.2,  "bullish"),
    (-0.2, "neutral"),
    (-0.6, "bearish"),
    (-2.0, "fearful"),
]

_CHRONICLE_CACHE_TTL = float(
    os.getenv("PULSE_CHRONICLE_CACHE_TTL_SEC", "300")
)

# Regex to extract a cached sentiment value from a Chronicle memory string
_SENT_RE = re.compile(r"sentiment\s+([-\d.]+)", re.I)
_MOOD_RE  = re.compile(r"mood\s+(\w+)", re.I)


class IntelligenceEngine:
    def __init__(self, chronicle_client=None, llm=None):
        self.chronicle = chronicle_client
        self.llm       = llm
        self.collectors = CollectorRegistry()
        self.trends     = TrendDetector()
        self._posts: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
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
            # Chronicle entries carry a timestamp in their metadata
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
                        "fallback_mode":     "chronicle_cache",
                    }
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    def gather(self, topics=None, sources=None, limit=10) -> Dict[str, Any]:
        started = time.time()
        raw   = self.collectors.collect(topics=topics, sources=sources, limit=limit)
        posts = [p.to_dict() for p in raw["posts"]]
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
            "posts":        posts,
            "source_status": raw["source_status"],
            "count":        len(posts),
            "duration_sec": round(time.time() - started, 2),
            "llm_used":     llm_used,
        }

    # ------------------------------------------------------------------
    def report(self, topics=None, sources=None) -> Dict[str, Any]:
        # ── Chronicle cache first ─────────────────────────────────────
        cached = self._chronicle_recent_report()
        if cached is not None:
            return {
                "status":        "complete",
                "report":        cached,
                "fallback_mode": "chronicle_cache",
            }

        gathered = self.gather(topics=topics, sources=sources)
        posts    = gathered["posts"]
        if not posts:
            return {
                "status":        "complete",
                "report":        None,
                "fallback_mode": "rule_based",
                "note":          "no social posts gathered; platforms unreachable or no matches",
                "source_status": gathered["source_status"],
            }

        manipulation = detect_manipulation(posts)
        trends       = self.trends.trends(posts)
        weights      = [p["authenticity"] for p in posts]
        wsent        = sum(p["sentiment"] * w for p, w in zip(posts, weights))
        overall      = wsent / (sum(weights) or 1.0)
        mood         = next(m for thr, m in MOOD if overall >= thr)

        fallback_mode = "llm_reasoning" if gathered.get("llm_used") else "rule_based"

        report = {
            "report_id":        f"social-{uuid.uuid4().hex[:8]}",
            "timestamp":        time.time(),
            "post_count":       len(posts),
            "overall_sentiment": round(overall, 3),
            "market_mood":      mood,
            "trending":         trends[:8],
            "manipulation":     manipulation,
            "platforms":        list({p["platform"] for p in posts}),
            "avg_authenticity": round(sum(weights) / len(posts), 3),
            "source_status":    gathered["source_status"],
            "fallback_mode":    fallback_mode,
        }
        self._preserve(report)
        return {"status": "complete", "report": report, "fallback_mode": fallback_mode}

    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    def _preserve(self, report: Dict[str, Any]) -> None:
        if self.chronicle is None:
            return
        try:
            self.chronicle.store(
                content=(
                    f"Social: {report['post_count']} posts, "
                    f"mood {report['market_mood']} "
                    f"(sentiment {report['overall_sentiment']}), "
                    f"{len(report['manipulation']['manipulation_flags'])} manipulation flags."
                ),
                memory_type="social", domain="social",
                tags=["pulse", "intelligence", report["market_mood"]],
                source="pulse",
            )
        except Exception:
            pass  # aegis:allow-silent

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {
            "posts_cached": len(self._posts),
            "collectors":   {
                n: getattr(c, "available", False)
                for n, c in self.collectors.collectors.items()
            },
        }


# ---------------------------------------------------------------------------
# Internal helper — returns (score, llm_was_used)
# ---------------------------------------------------------------------------
def _score_sentiment(text: str, llm=None, chronicle=None):
    """
    Thin wrapper around sentiment() that also reports whether the LLM was used.
    We detect LLM usage by checking the LLM's call counter before and after.
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
            llm_used = calls_after > calls_before
        except Exception:
            pass

    return score, llm_used
