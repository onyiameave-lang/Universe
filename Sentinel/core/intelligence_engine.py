"""
Sentinel.core.intelligence_engine
=================================
The news intelligence pipeline. (Book I Part IV Article VII; Book II Part II.)

Turns raw multi-source articles into institutional intelligence:
  1. COLLECT       parallel multi-source acquisition (collectors).
  2. ENRICH        symbols, event type, market-aware sentiment per article.
  3. CORROBORATE   cross-source agreement counts.
  4. SCORE         credibility + misinformation risk per article.
  5. CLUSTER       group articles into ranked EVENTS.
  6. REPORT        per-symbol sentiment + top events + high-priority alerts.
  7. PRESERVE      important intelligence -> Chronicle.

Every number derives from real gathered text. Offline, it reports the
limitation honestly instead of inventing news.
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

from core.collectors import CollectorRegistry                                        # type: ignore
from intelligence.credibility import (credibility_score, misinformation_risk,        # type: ignore
                                      compute_corroboration)
from intelligence.analysis import (extract_symbols, classify_event, sentiment,       # type: ignore
                                   EventClusterer)


class IntelligenceEngine:
    def __init__(self, chronicle_client=None, llm=None):
        self.chronicle = chronicle_client
        self.llm = llm
        self.collectors = CollectorRegistry()
        self.clusterer = EventClusterer()
        self._articles: List[Dict[str, Any]] = []

    def gather(self, topics=None, sources=None, limit=8) -> Dict[str, Any]:
        started = time.time()
        raw = self.collectors.collect(topics=topics, sources=sources, limit=limit)
        articles = [a.to_dict() for a in raw["articles"]]
        # enrich
        for a in articles:
            a["symbols"] = extract_symbols(a["title"], a.get("summary", ""))
            a["event_type"] = classify_event(a["title"], a.get("summary", ""))
            a["sentiment"] = sentiment(a["title"], a.get("summary", ""), llm=self.llm)
        # corroboration then credibility + misinfo (credibility uses corroboration)
        compute_corroboration(articles)
        for a in articles:
            cred = credibility_score(a["source"], a["title"], a.get("summary", ""),
                                    a.get("corroboration", 0))
            a["credibility"] = cred["credibility"]
            mis = misinformation_risk(a["source"], a["title"], a.get("summary", ""),
                                     a.get("corroboration", 0))
            a["misinformation_risk"] = mis["misinformation_risk"]
            a["misinfo_reasons"] = mis["reasons"]
        self._articles.extend(articles)
        return {"articles": articles, "source_status": raw["source_status"],
               "count": len(articles), "duration_sec": round(time.time() - started, 2)}

    def report(self, topics=None, sources=None) -> Dict[str, Any]:
        gathered = self.gather(topics=topics, sources=sources)
        articles = gathered["articles"]
        if not articles:
            return {"status": "complete", "report": None,
                   "note": "no news gathered; feeds unreachable or no matches",
                   "source_status": gathered["source_status"]}
        events = self.clusterer.cluster(articles)
        # high-priority alerts: strong sentiment, credible, corroborated
        alerts = [{"headline": a["title"], "sentiment": a["sentiment"],
                  "credibility": a["credibility"], "symbols": a["symbols"], "source": a["source"]}
                 for a in sorted(articles, key=lambda x: x["credibility"] * abs(x["sentiment"]),
                                reverse=True)[:5]
                 if abs(a["sentiment"]) > 0.4 and a["credibility"] > 0.6]
        overall = sum(a["sentiment"] for a in articles) / len(articles)
        flagged = [a for a in articles if a["misinformation_risk"] > 0.5]
        report = {"report_id": f"news-{uuid.uuid4().hex[:8]}", "timestamp": time.time(),
                 "article_count": len(articles), "event_count": len(events),
                 "top_events": events[:8], "high_priority_alerts": alerts,
                 "overall_sentiment": round(overall, 3),
                 "flagged_misinformation": len(flagged),
                 "source_status": gathered["source_status"]}
        self._preserve(report)
        return {"status": "complete", "report": report}

    def sentiment_for(self, symbol: str) -> Dict[str, Any]:
        rel = [a for a in self._articles if symbol.upper() in [s.upper() for s in a.get("symbols", [])]]
        if not rel:
            # try a fresh targeted gather
            g = self.gather(topics=[symbol])
            rel = [a for a in g["articles"] if symbol.upper() in [s.upper() for s in a.get("symbols", [])]]
        if not rel:
            return {"symbol": symbol, "sentiment": 0.0, "article_count": 0, "confidence": 0.0}
        # credibility-weighted sentiment
        wsum = sum(a["sentiment"] * a["credibility"] for a in rel)
        cw = sum(a["credibility"] for a in rel) or 1.0
        return {"symbol": symbol, "sentiment": round(wsum / cw, 3), "article_count": len(rel),
               "confidence": round(sum(a["credibility"] for a in rel) / len(rel), 3),
               "cross_source": len({a["source"] for a in rel}) > 1,
               "top_headline": max(rel, key=lambda a: a["credibility"])["title"]}

    def _preserve(self, report):
        if self.chronicle is None:
            return
        try:
            summary = (f"News: {report['article_count']} articles, {report['event_count']} events, "
                      f"overall sentiment {report['overall_sentiment']}, "
                      f"{report['flagged_misinformation']} flagged.")
            self.chronicle.store(content=summary, memory_type="social", domain="news",
                                tags=["sentinel", "intelligence"], source="sentinel")
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        return {"articles_cached": len(self._articles),
               "collectors": {n: getattr(c, "available", False) for n, c in self.collectors.collectors.items()}}
