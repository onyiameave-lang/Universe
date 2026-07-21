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

# FIX-IE-07 (Phase 5i): Maximum time (seconds) allowed for the enrichment loop
# (extract_symbols + classify_event + sentiment × N articles). With 17 articles
# and an LLM that has a 120s rate-limiter acquire(), this loop can block for
# 17 × 120s = 2040s. We cap it at 12s — enough for pure-lexical scoring of
# 100 articles, but short enough to not block the coordinator's 30s window.
_ENRICH_TIMEOUT_SEC = 12

log = logging.getLogger(__name__)

# FIX-IE-10 (Phase 5i): Alias NEWS_API_KEY → NEWSAPI_KEY so users who set
# either name in their .env get NewsAPI working without code changes.
import os as _os
_newsapi_alias = _os.environ.get("NEWS_API_KEY", "")
if _newsapi_alias and not _os.environ.get("NEWSAPI_KEY", ""):
    _os.environ["NEWSAPI_KEY"] = _newsapi_alias
    log.info("[sentinel.engine] FIX-IE-10: aliased NEWS_API_KEY → NEWSAPI_KEY")


def _enrich_articles(articles: List[Dict[str, Any]], llm=None) -> None:
    """Enrich articles in-place: symbols, event_type, sentiment, credibility, misinfo.

    FIX-IE-07 (Phase 5i): This function is called inside a ThreadPoolExecutor
    with a hard timeout so it can never block the coordinator indefinitely.
    The LLM sentiment call (analysis.sentiment()) can block for up to 120s per
    article when the rate-limiter is saturated — with 17 articles that's 34 min.
    We run the whole loop in a thread and abandon it after _ENRICH_TIMEOUT_SEC.
    """
    _socket.setdefaulttimeout(15)  # nuclear DNS bound inside worker thread
    log.info("[sentinel.engine] enrich: starting enrichment of %d articles (llm=%s)",
             len(articles), "yes" if llm is not None else "no")
    t0 = time.time()
    for i, a in enumerate(articles):
        a["symbols"]    = extract_symbols(a["title"], a.get("summary", ""))
        a["event_type"] = classify_event(a["title"], a.get("summary", ""))
        # FIX-IE-08 (Phase 5i): Pass llm=None here regardless of what the engine
        # has. The LLM sentiment call is non-essential (essential=False in analysis.py)
        # but the rate-limiter acquire() still blocks for up to 120s when the bucket
        # is empty. Pure lexical scoring is instant and honest: "no signal" = 0.0.
        # If the user wants LLM-assisted sentiment they can set SENTINEL_LLM_MODE=full
        # and the analysis.py S-5 gate will handle it without blocking here.
        a["sentiment"]  = sentiment(a["title"], a.get("summary", ""), llm=None)
        log.debug("[sentinel.engine] enrich: article %d/%d done in %.2fs — symbols=%r sentiment=%.3f",
                  i + 1, len(articles), time.time() - t0, a["symbols"], a["sentiment"])
    compute_corroboration(articles)
    for a in articles:
        cred = credibility_score(a["source"], a["title"], a.get("summary", ""),
                                 a.get("corroboration", 0))
        a["credibility"] = cred["credibility"]
        mis = misinformation_risk(a["source"], a["title"], a.get("summary", ""),
                                  a.get("corroboration", 0))
        a["misinformation_risk"] = mis["misinformation_risk"]
        a["misinfo_reasons"]     = mis["reasons"]
    log.info("[sentinel.engine] enrich: completed %d articles in %.2fs",
             len(articles), time.time() - t0)


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
        log.info("[sentinel.engine] gather: topics=%r sources=%r — calling collectors.collect()",
                 topics, sources)
        raw = self.collectors.collect(topics=topics, sources=sources, limit=limit)
        log.info("[sentinel.engine] gather: collectors.collect() returned %d articles in %.2fs",
                 len(raw.get("articles", [])), time.time() - started)
        articles = [a.to_dict() for a in raw["articles"]]
        # enrich
        for a in articles:
            a["symbols"] = extract_symbols(a["title"], a.get("summary", ""))
            a["event_type"] = classify_event(a["title"], a.get("summary", ""))
            a["sentiment"] = sentiment(a["title"], a.get("summary", ""), llm=self.llm)
        # corroboration then credibility + misinfo
        compute_corroboration(articles)
        for a in articles:
            cred = credibility_score(a["source"], a["title"], a.get("summary", ""),
                                    a.get("corroboration", 0))
            a["credibility"] = cred["credibility"]
            mis = misinformation_risk(a["source"], a["title"], a.get("summary", ""),
                                     a.get("corroboration", 0))
            a["misinformation_risk"] = mis["misinformation_risk"]
            a["misinfo_reasons"] = mis["reasons"]

        # FIX-IE-11 (Phase 5i): Post-collection relevance filter.
        # When a specific topic/symbol is requested, only keep articles whose
        # title or summary contains at least one of the topic's search terms.
        # This prevents returning unrelated articles when NewsAPI/RSS returns
        # broad results. Degrades gracefully: if filter removes everything,
        # return all articles (better than empty).
        if topics and articles:
            from core.collectors import _topic_matches_article  # type: ignore
            filtered = [
                a for a in articles
                if _topic_matches_article((a["title"] + " " + a.get("summary", "")).lower(), topics)
            ]
            if filtered:
                log.info("[sentinel.engine] gather: relevance filter kept %d/%d articles for topics=%r",
                         len(filtered), len(articles), topics)
                articles = filtered
            else:
                log.info("[sentinel.engine] gather: relevance filter removed all articles — returning unfiltered %d",
                         len(articles))

        self._articles.extend(articles)
        return {"articles": articles, "source_status": raw["source_status"],
               "count": len(articles), "duration_sec": round(time.time() - started, 2)}

    # FIX-IE-07 (Phase 5i): LLM synthesis with hard 20s timeout.
    # Previously any LLM call in the engine had no timeout — if the LLM HTTP
    # endpoint was slow or unreachable, the entire pipeline blocked forever.
    # This helper wraps self.llm.think() (or equivalent) in a thread with a
    # hard deadline. On timeout, returns None so callers fall back to
    # extractive (non-LLM) summaries.
    def _synthesise_with_llm(self, prompt: str, timeout_sec: int = 20) -> Optional[str]:
        """Call LLM for synthesis with a hard timeout. Returns None on timeout/error."""
        if self.llm is None:
            return None
        think_fn = getattr(self.llm, "think", None) or getattr(self.llm, "generate", None)
        if think_fn is None:
            return None
        log.info("[sentinel.engine] _synthesise_with_llm: calling LLM (timeout=%ds)", timeout_sec)
        _t0 = time.time()
        def _call():
            _socket.setdefaulttimeout(timeout_sec - 2)
            return think_fn(prompt)
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_call)
                result = fut.result(timeout=timeout_sec)
            log.info("[sentinel.engine] _synthesise_with_llm: LLM returned in %.2fs", time.time() - _t0)
            return result.strip() if isinstance(result, str) else None
        except _cf.TimeoutError:
            log.warning("[sentinel.engine] _synthesise_with_llm: LLM TIMED OUT after %.2fs — using extractive fallback",
                        time.time() - _t0)
            return None
        except Exception as exc:
            log.warning("[sentinel.engine] _synthesise_with_llm: LLM error %s — using extractive fallback", exc)
            return None

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

        # FIX-IE-08 (Phase 5i): Build a plain-text 'summary' field so the
        # coordinator's _format_result() and main.py's _extract_summary() can
        # surface a human-readable answer without parsing nested dicts.
        # Try LLM synthesis first (20s timeout); fall back to extractive.
        top_headlines = [a["title"] for a in sorted(articles, key=lambda x: x["credibility"], reverse=True)[:5]]
        sentiment_label = (
            "Bullish 🟢" if overall > 0.15 else
            "Slightly Bullish 🟡" if overall > 0.05 else
            "Slightly Bearish 🔴" if overall > -0.15 else
            "Bearish 🔴" if overall <= -0.15 else "Neutral ⚪"
        )
        topic_str = ", ".join(topics) if topics else "markets"
        llm_prompt = (
            f"Summarise the following {len(articles)} news headlines about {topic_str} "
            f"in 2-3 sentences for a financial professional. Overall sentiment: {sentiment_label}.\n"
            + "\n".join(f"- {h}" for h in top_headlines)
        )
        llm_summary = self._synthesise_with_llm(llm_prompt, timeout_sec=20)
        if llm_summary:
            plain_summary = llm_summary
        else:
            # Extractive fallback: list top headlines
            headline_lines = "\n".join(f"  {i+1}. \"{h}\"" for i, h in enumerate(top_headlines))
            plain_summary = (
                f"{len(articles)} articles collected on {topic_str}. "
                f"Overall sentiment: {sentiment_label} ({round(overall, 3)}).\n"
                f"Top headlines:\n{headline_lines}"
            )

        report = {"report_id": f"news-{uuid.uuid4().hex[:8]}", "timestamp": time.time(),
                 "article_count": len(articles), "event_count": len(events),
                 "top_events": events[:8], "high_priority_alerts": alerts,
                 "overall_sentiment": round(overall, 3),
                 "flagged_misinformation": len(flagged),
                 "source_status": gathered["source_status"],
                 "summary": plain_summary,          # FIX-IE-08: plain-text for formatter
                 "top_headlines": top_headlines,    # FIX-IE-08: list for formatter
                 "sentiment_label": sentiment_label}  # FIX-IE-08: label for formatter
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
        top = max(rel, key=lambda a: a["credibility"])
        sentiment_val = round(wsum / cw, 3)
        confidence_val = round(sum(a["credibility"] for a in rel) / len(rel), 3)
        sentiment_label = (
            "Bullish 🟢" if sentiment_val > 0.15 else
            "Slightly Bullish 🟡" if sentiment_val > 0.05 else
            "Slightly Bearish 🔴" if sentiment_val > -0.15 else
            "Bearish 🔴" if sentiment_val <= -0.15 else "Neutral ⚪"
        )
        # FIX-IE-09 (Phase 5i): Add plain-text 'summary' field so coordinator
        # _format_result() can surface a human-readable answer directly.
        top_headlines = [a["title"] for a in sorted(rel, key=lambda a: a["credibility"], reverse=True)[:5]]
        plain_summary = (
            f"{len(rel)} articles on {symbol}. Sentiment: {sentiment_label} ({sentiment_val}). "
            f"Confidence: {round(confidence_val * 100)}%.\n"
            + "\n".join(f"  {i+1}. \"{h}\"" for i, h in enumerate(top_headlines))
        )
        return {"symbol": symbol, "sentiment": sentiment_val, "article_count": len(rel),
               "confidence": confidence_val,
               "cross_source": len({a["source"] for a in rel}) > 1,
               "top_headline": top["title"],
               "top_headlines": top_headlines,
               "sentiment_label": sentiment_label,
               "summary": plain_summary}  # FIX-IE-09: plain-text for formatter

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