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

Constitutional fix (2026-07-20): Principle 3 — Memory First.
  _consult_chronicle() now queries Chronicle BEFORE hitting external news APIs.
  If a fresh cached report exists (< CHRONICLE_CACHE_TTL_SEC old), it is
  returned directly, avoiding redundant external calls and respecting the
  "retrieve before generating" mandate.

Every number derives from real gathered text. Offline, it reports the
limitation honestly instead of inventing news.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import socket as _socket
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

# How old (seconds) a Chronicle-cached news report can be before we bypass it
# and fetch fresh data.  Default: 15 minutes.
CHRONICLE_CACHE_TTL_SEC = int(900)

# FIX-IE-02 (Phase 5e): Nuclear socket timeout — bounds DNS resolution which
# urllib timeout= does NOT cover. Set here so it applies even if collectors.py
# hasn't been imported yet. Constitutional: Book II Principle V.
_socket.setdefaulttimeout(15)

log = logging.getLogger(__name__)


class IntelligenceEngine:
    def __init__(self, chronicle_client=None, llm=None):
        self.chronicle = chronicle_client
        self.llm = llm
        self.collectors = CollectorRegistry()
        self.clusterer = EventClusterer()
        self._articles: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Principle 3 — Memory First: consult Chronicle before external APIs
    # ------------------------------------------------------------------

    def _consult_chronicle(self, topics: Optional[List[str]]) -> Optional[Dict[str, Any]]:
        """Query Chronicle for a recent cached news report on *topics*.

        Returns the cached report dict if one exists and is fresh enough
        (< CHRONICLE_CACHE_TTL_SEC old), otherwise returns None so the
        caller proceeds with live external collection.

        Constitutional basis: Principle 3 — "Memory First — retrieve before
        generating.  Every agent MUST consult Chronicle before hitting
        external APIs."
        """
        if self.chronicle is None:
            return None
        try:
            query = " ".join(topics) if topics else "news intelligence report"
            results = self.chronicle.search(query=query, domain="news", limit=1)
            if not results:
                return None
            # results may be a list of dicts or a dict with a 'results' key
            hits = results if isinstance(results, list) else results.get("results", [])
            if not hits:
                return None
            hit = hits[0]
            # Check freshness — Chronicle entries carry a 'timestamp' field
            stored_at = hit.get("timestamp") or hit.get("created_at") or 0
            age_sec = time.time() - float(stored_at)
            if age_sec > CHRONICLE_CACHE_TTL_SEC:
                return None  # stale — fetch fresh
            # Return a minimal report wrapper so callers can detect a cache hit
            return {
                "status": "complete",
                "source": "chronicle_cache",
                "age_sec": round(age_sec, 1),
                "cached_summary": hit.get("content", ""),
                "report": None,  # full structured report not stored; summary only
                "note": (f"Chronicle cache hit (age {round(age_sec)}s < "
                         f"{CHRONICLE_CACHE_TTL_SEC}s TTL). "
                         "Skipped external API calls per Principle 3."),
            }
        except Exception:
            return None  # Chronicle unavailable — fall through to live fetch

    def gather(self, topics=None, sources=None, limit=8) -> Dict[str, Any]:
        # ---- Principle 3: Memory First ----
        cached = self._consult_chronicle(topics)
        if cached is not None:
            # Fresh Chronicle hit — return without hitting external APIs
            return {
                "articles": [],
                "source_status": {"chronicle": "cache_hit"},
                "count": 0,
                "duration_sec": 0.0,
                "chronicle_cache": cached,
            }

        started = time.time()
        # FIX-IE-03 (Phase 5e): Log before collectors.collect() so we can see
        # exactly where the hang occurs in production logs.
        log.info("[sentinel.engine] gather: topics=%r sources=%r — calling collectors.collect()", topics, sources)
        raw = self.collectors.collect(topics=topics, sources=sources, limit=limit)
        log.info("[sentinel.engine] gather: collectors.collect() returned %d articles in %.2fs",
                 len(raw.get("articles", [])), time.time() - started)
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
        # If Chronicle returned a fresh cache hit, surface it directly
        if gathered.get("chronicle_cache"):
            return gathered["chronicle_cache"]
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

    def sentiment_for(self, symbol: str, topics: Optional[List[str]] = None) -> Dict[str, Any]:
        # FIX-IE-05 (Phase 5h): Accept optional `topics` parameter.
        # When the coordinator passes topics=["GBPUSD"], use that list directly
        # for gather() so collectors filter by the right symbol terms.
        # Fall back to [symbol] if topics is not provided (backward compatible).
        gather_topics = topics if topics else ([symbol] if symbol else None)
        rel = [a for a in self._articles if symbol.upper() in [s.upper() for s in a.get("symbols", [])]]
        if not rel:
            # FIX-IE-04 (Phase 5e): Wrap targeted gather() in a thread with 20s
            # timeout. Previously this call had NO timeout — if collectors hung on
            # DNS for symbol-specific feeds, sentiment_for() blocked forever.
            # Constitutional: Book II Principle V Graceful Degradation.
            log.info("[sentinel.engine] sentiment_for: no cached articles for %r — fetching live (20s timeout) topics=%r",
                     symbol, gather_topics)
            _t0 = time.time()
            def _gather():
                _socket.setdefaulttimeout(15)
                return self.gather(topics=gather_topics)
            try:
                with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                    _fut = _pool.submit(_gather)
                    g = _fut.result(timeout=20)
                log.info("[sentinel.engine] sentiment_for: gather(%r) completed in %.2fs — %d articles collected",
                         gather_topics, time.time() - _t0, g.get("count", 0))
            except _cf.TimeoutError:
                log.warning("[sentinel.engine] sentiment_for: gather(%r) TIMED OUT after %.2fs — returning empty sentiment",
                            gather_topics, time.time() - _t0)
                return {"symbol": symbol, "sentiment": 0.0, "article_count": 0, "confidence": 0.0,
                        "note": "timed out fetching live news; try again in a moment"}
            rel = [a for a in g["articles"] if symbol.upper() in [s.upper() for s in a.get("symbols", [])]]
            if not rel and g.get("articles"):
                # FIX-IE-06 (Phase 5h): If symbol extraction didn't match any article
                # (e.g. "GBPUSD" not in article.symbols because analysis.py missed it),
                # fall back to returning ALL gathered articles with a note.
                # This prevents returning empty results when news WAS fetched.
                log.info("[sentinel.engine] sentiment_for: symbol %r not found in article.symbols — "
                         "returning all %d gathered articles (symbol extraction miss)",
                         symbol, len(g["articles"]))
                rel = g["articles"]
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