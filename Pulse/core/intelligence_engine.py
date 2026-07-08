"""
Pulse.core.intelligence_engine
=============================
The social intelligence pipeline. (Book I Part IV Article VII; Book II Part II.)

Turns raw multi-platform posts into institutional social intelligence:
  1. COLLECT        parallel multi-platform acquisition (collectors).
  2. ENRICH         symbols, market-aware sentiment, authenticity weight per post.
  3. MANIPULATION   flag coordinated pump/brigading and down-weight it.
  4. TREND          detect trending symbols with mention velocity.
  5. REPORT         authenticity-weighted per-symbol sentiment, mood, trends,
                    manipulation flags.
  6. PRESERVE       important intelligence -> Chronicle.

Authenticity-weighted throughout so bots and hype don't distort the read.
Offline, it reports the limitation honestly instead of inventing posts.
"""
from __future__ import annotations

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

MOOD = [(0.6, "euphoric"), (0.2, "bullish"), (-0.2, "neutral"), (-0.6, "bearish"), (-2, "fearful")]


class IntelligenceEngine:
    def __init__(self, chronicle_client=None, llm=None):
        self.chronicle = chronicle_client
        self.llm = llm
        self.collectors = CollectorRegistry()
        self.trends = TrendDetector()
        self._posts: List[Dict[str, Any]] = []

    def gather(self, topics=None, sources=None, limit=10) -> Dict[str, Any]:
        started = time.time()
        raw = self.collectors.collect(topics=topics, sources=sources, limit=limit)
        posts = [p.to_dict() for p in raw["posts"]]
        for p in posts:
            p["symbols"] = extract_symbols(p["title"] + " " + p["content"])
            p["sentiment"] = sentiment(p["title"] + " " + p["content"], llm=self.llm)
            p["authenticity"] = authenticity_weight(p)
            p["bot_risk"] = bot_risk(p)["bot_risk"]
        self._posts.extend(posts)
        return {"posts": posts, "source_status": raw["source_status"],
               "count": len(posts), "duration_sec": round(time.time() - started, 2)}

    def report(self, topics=None, sources=None) -> Dict[str, Any]:
        gathered = self.gather(topics=topics, sources=sources)
        posts = gathered["posts"]
        if not posts:
            return {"status": "complete", "report": None,
                   "note": "no social posts gathered; platforms unreachable or no matches",
                   "source_status": gathered["source_status"]}
        manipulation = detect_manipulation(posts)
        trends = self.trends.trends(posts)
        # authenticity-weighted overall mood
        weights = [p["authenticity"] for p in posts]
        wsent = sum(p["sentiment"] * w for p, w in zip(posts, weights))
        overall = wsent / (sum(weights) or 1.0)
        mood = next(m for thr, m in MOOD if overall >= thr)
        report = {"report_id": f"social-{uuid.uuid4().hex[:8]}", "timestamp": time.time(),
                 "post_count": len(posts), "overall_sentiment": round(overall, 3),
                 "market_mood": mood, "trending": trends[:8],
                 "manipulation": manipulation, "platforms": list({p["platform"] for p in posts}),
                 "avg_authenticity": round(sum(weights) / len(posts), 3),
                 "source_status": gathered["source_status"]}
        self._preserve(report)
        return {"status": "complete", "report": report}

    def sentiment_for(self, symbol: str) -> Dict[str, Any]:
        rel = [p for p in self._posts if symbol.upper() in [s.upper() for s in p.get("symbols", [])]]
        if not rel:
            g = self.gather(topics=[symbol])
            rel = [p for p in g["posts"] if symbol.upper() in [s.upper() for s in p.get("symbols", [])]]
        if not rel:
            return {"symbol": symbol, "sentiment": 0.0, "post_count": 0, "confidence": 0.0}
        weights = [p["authenticity"] for p in rel]
        wsent = sum(p["sentiment"] * w for p, w in zip(rel, weights))
        manip = detect_manipulation(rel, symbol=symbol)
        return {"symbol": symbol, "sentiment": round(wsent / (sum(weights) or 1.0), 3),
               "post_count": len(rel), "confidence": round(min(len(rel) / 20.0, 1.0), 3),
               "avg_authenticity": round(sum(weights) / len(rel), 3),
               "platforms": list({p["platform"] for p in rel}),
               "manipulation_warning": manip["flagged"]}

    def _preserve(self, report):
        if self.chronicle is None:
            return
        try:
            self.chronicle.store(
                content=f"Social: {report['post_count']} posts, mood {report['market_mood']} "
                       f"(sentiment {report['overall_sentiment']}), "
                       f"{len(report['manipulation']['manipulation_flags'])} manipulation flags.",
                memory_type="social", domain="social",
                tags=["pulse", "intelligence", report["market_mood"]], source="pulse")
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        return {"posts_cached": len(self._posts),
               "collectors": {n: getattr(c, "available", False)
                            for n, c in self.collectors.collectors.items()}}
