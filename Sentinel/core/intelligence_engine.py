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
import copy
import logging
import os
import socket as _socket
import sys
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from core.collectors import CollectorRegistry, _topic_matches_article             # type: ignore
except ImportError:
    from Sentinel.core.collectors import CollectorRegistry, _topic_matches_article     # type: ignore
try:
    from intelligence.credibility import (credibility_score, misinformation_risk,    # type: ignore
                                          compute_corroboration)
except ImportError:
    from Sentinel.intelligence.credibility import (credibility_score, misinformation_risk,  # type: ignore
                                                    compute_corroboration)
try:
    from intelligence.analysis import (extract_symbols, classify_event, sentiment,   # type: ignore
                                       EventClusterer)
except ImportError:
    from Sentinel.intelligence.analysis import (extract_symbols, classify_event,     # type: ignore
                                                 sentiment, EventClusterer)
try:
    from intelligence.deep_analysis import deep_analyze                             # type: ignore
except ImportError:
    from Sentinel.intelligence.deep_analysis import deep_analyze                    # type: ignore
# Module-level import (not deferred) is deliberate: this runs while
# Sentinel's own directory is still on sys.path (during _load()'s
# exec_module() call when Oracle loads Sentinel as a peer). That caches
# intelligence.term_reliability in sys.modules permanently — every later
# DEFERRED import of it elsewhere (in intelligence.analysis, in
# sentinel_agent.py's task handlers) then resolves instantly from that
# cache, regardless of sys.path state at that later point. Without this,
# those deferred imports fail once _load() pops Sentinel's directory from
# sys.path after construction — a real bug found and fixed this session.
# UPDATE: this alone wasn't sufficient either — a LATER _unload_conflicting_
# modules() call (made before loading the NEXT peer, e.g. Pulse) wipes this
# cache again, and the next peer's own "intelligence" package can then take
# over the bare name permanently. The actual fix is the dual-import fallback
# pattern now applied at every deferred call site (see analysis.py,
# sentinel_agent.py, and this file's own imports above) — this module-level
# import is kept as a harmless first line of defense, not the real fix.
try:
    import intelligence.term_reliability                                            # type: ignore  # noqa: F401
except ImportError:
    import Sentinel.intelligence.term_reliability                                   # type: ignore  # noqa: F401

# How old (seconds) a Chronicle-cached news report can be before we bypass it
# and fetch fresh data.  Default: 15 minutes.
CHRONICLE_CACHE_TTL_SEC = int(900)

# Deep Analysis runs an LLM call PER ARTICLE (see the enrichment loop
# below), each bounded by OLLAMA_TIMEOUT (default 25s) or the equivalent
# cloud-provider timeout. Without a cap, up to `limit` articles (default 8)
# could each hit that ceiling -- worst case 8 x 25s = 200s, which blows
# straight through a caller's own per-symbol timeout budget (e.g. Oracle's
# trade.propose is commonly wrapped in a 60s watchdog). This was a real bug
# found this session -- symbols were silently getting skipped whenever
# enough slow articles came back for that symbol's news search, especially
# noticeable with a larger/slower local Ollama model. Capping to the first
# few articles (already the most relevant/recent, per collectors' own
# ordering) bounds worst-case latency to a fraction of the full article
# count while still getting Deep Analysis's value on what matters most.
MAX_DEEP_ANALYSIS_ARTICLES = int(os.getenv("SENTINEL_MAX_DEEP_ANALYSIS_ARTICLES", "3"))

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
_SENTIMENT_REFRESH_SEC = float(os.getenv("SENTINEL_SENTIMENT_REFRESH_SEC", "1800"))
_SENTIMENT_MAX_AGE_SEC = float(os.getenv("SENTINEL_SENTIMENT_MAX_AGE_SEC", "10800"))
_GATHER_CACHE_TTL_SEC = float(os.getenv("SENTINEL_GATHER_CACHE_TTL_SEC", "300"))

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
        self._sentiment_snapshot: List[Dict[str, Any]] = []
        self._sentiment_snapshot_at = 0.0
        self._sentiment_snapshot_status: Dict[str, Any] = {}
        self._sentiment_refresh_lock = __import__("threading").Lock()
        self._gather_cache: Dict[Any, Any] = {}
        self._gather_cache_lock = __import__("threading").Lock()

    @staticmethod
    def _published_at(article: Dict[str, Any]) -> Optional[float]:
        value = article.get("published_at")
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @classmethod
    def _is_fresh_for_trading(cls, article: Dict[str, Any], now: float) -> bool:
        published = cls._published_at(article)
        return published is not None and 0 <= now - published <= _SENTIMENT_MAX_AGE_SEC

    @classmethod
    def _matching_fresh_articles(cls, articles: List[Dict[str, Any]],
                                 symbol: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = time.time() if now is None else now
        symbol_upper = symbol.upper()
        return [article for article in articles
                if symbol_upper in [item.upper() for item in article.get("symbols", [])]
                and cls._is_fresh_for_trading(article, now)]

    def _refresh_sentiment_snapshot(self) -> None:
        if time.time() - self._sentiment_snapshot_at < _SENTIMENT_REFRESH_SEC:
            return
        with self._sentiment_refresh_lock:
            if time.time() - self._sentiment_snapshot_at < _SENTIMENT_REFRESH_SEC:
                return
            _socket.setdefaulttimeout(15)
            gathered = self.gather(
                topics=None, limit=8, consult_chronicle=False,
                include_deep_analysis=False)
            self._sentiment_snapshot = gathered.get("articles", [])
            self._sentiment_snapshot_status = gathered.get("source_status", {})
            self._sentiment_snapshot_at = time.time()

    def articles_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """Return fresh, symbol-matched items from the shared polling snapshot."""
        self._refresh_sentiment_snapshot()
        return {
            "articles": self._matching_fresh_articles(self._sentiment_snapshot, symbol),
            "source_status": self._sentiment_snapshot_status,
            "snapshot_age_sec": round(time.time() - self._sentiment_snapshot_at, 1),
        }

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

            def _refresh_sentiment_snapshot(self) -> None:
                if time.time() - self._sentiment_snapshot_at < _SENTIMENT_REFRESH_SEC:
                    return
                with self._sentiment_refresh_lock:
                    if time.time() - self._sentiment_snapshot_at < _SENTIMENT_REFRESH_SEC:
                        return
                    _socket.setdefaulttimeout(15)
                    gathered = self.gather(
                        topics=None, limit=8, consult_chronicle=False,
                        include_deep_analysis=False)
                    self._sentiment_snapshot = gathered.get("articles", [])
                    self._sentiment_snapshot_status = gathered.get("source_status", {})
                    self._sentiment_snapshot_at = time.time()

            def articles_for_symbol(self, symbol: str) -> Dict[str, Any]:
                """Return only fresh, symbol-matched news from the shared polling snapshot."""
                self._refresh_sentiment_snapshot()
                return {
                    "articles": self._matching_fresh_articles(self._sentiment_snapshot, symbol),
                    "source_status": self._sentiment_snapshot_status,
                    "snapshot_age_sec": round(time.time() - self._sentiment_snapshot_at, 1),
                }
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

    def gather(self, topics=None, sources=None, limit=8, *,
               consult_chronicle=True, include_deep_analysis=True) -> Dict[str, Any]:
        cache_key = (
            tuple(sorted(set(topics or []))), tuple(sorted(set(sources or []))),
            int(limit), bool(consult_chronicle), bool(include_deep_analysis),
        )
        now = time.time()
        with self._gather_cache_lock:
            cached = self._gather_cache.get(cache_key)
            if cached and cached[0] > now:
                result = copy.deepcopy(cached[1])
                result["cache_hit"] = True
                return result
            self._gather_cache.pop(cache_key, None)

        # ---- Principle 3: Memory First ----
        cached = self._consult_chronicle(topics) if consult_chronicle else None
        if cached is not None:
            # Fresh Chronicle hit — return without hitting external APIs
            result = {
                "articles": [],
                "source_status": {"chronicle": "cache_hit"},
                "count": 0,
                "duration_sec": 0.0,
                "chronicle_cache": cached,
            }
            with self._gather_cache_lock:
                self._gather_cache[cache_key] = (time.time() + _GATHER_CACHE_TTL_SEC, result)
            return copy.deepcopy(result)

        started = time.time()
        log.info("[sentinel.engine] gather: topics=%r sources=%r — calling collectors.collect()",
                 topics, sources)
        raw = self.collectors.collect(topics=topics, sources=sources, limit=limit)
        log.info("[sentinel.engine] gather: collectors.collect() returned %d articles in %.2fs",
                 len(raw.get("articles", [])), time.time() - started)
        articles = [a.to_dict() for a in raw["articles"]]
        # enrich
        for i, a in enumerate(articles):
            a["symbols"] = extract_symbols(a["title"], a.get("summary", ""))
            # Lexical event_type/sentiment stay as the fast, always-available
            # fallback (this codebase's own graceful-degradation principle).
            a["event_type"] = classify_event(a["title"], a.get("summary", ""))
            a["sentiment"] = sentiment(a["title"], a.get("summary", ""), llm=self.llm)
            # Deep Analysis (structured event understanding, not word-counting):
            # only activates when a real LLM client is available. When it
            # succeeds, its event_type/economic sentiment REPLACE the lexical
            # ones above for downstream consumers (it understands negation,
            # word order, and event context that keyword matching can't) —
            # but the raw lexical values are preserved under distinct keys so
            # nothing is silently lost, and everything still degrades
            # gracefully to the lexical path when no LLM is available.
            #
            # Capped to the first MAX_DEEP_ANALYSIS_ARTICLES articles (a real
            # bug found this session: an LLM call per article, each bounded
            # by OLLAMA_TIMEOUT, with no cap could blow past a caller's own
            # per-symbol timeout budget once enough articles came back —
            # symbols were silently getting skipped). Articles beyond the
            # cap just keep their lexical event_type/sentiment above.
            deep = deep_analyze(a["title"], a.get("summary", ""), self.llm) \
                if include_deep_analysis and i < MAX_DEEP_ANALYSIS_ARTICLES else None
            a["deep_analysis"] = deep
            if deep is not None:
                a["lexical_event_type"] = a["event_type"]
                a["lexical_sentiment"] = a["sentiment"]
                # LLM-as-teacher vocabulary discovery (Tier 0 mechanism 2):
                # if the lexical path missed this entirely ("general") but
                # Deep Analysis confidently classifies it as something
                # real, queue candidate keywords for human review — never
                # auto-added to the live classify_event() vocabulary.
                if (a["lexical_event_type"] == "general" and deep["event_type"] != "general"
                        and deep["confidence"] >= 0.6):
                    try:
                        from intelligence.term_reliability import (          # type: ignore
                            candidate_keywords_from_miss, get_tracker,
                        )
                    except ImportError:
                        from Sentinel.intelligence.term_reliability import (  # type: ignore
                            candidate_keywords_from_miss, get_tracker,
                        )
                    for cand in candidate_keywords_from_miss(a["title"], deep["event_type"]):
                        get_tracker().suggest_term(cand, deep["event_type"], a["title"])
                a["event_type"] = deep["event_type"]
                a["sentiment"] = deep["sentiment"]["economic"]
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

        # Trade evidence must never be broadened to unrelated headlines.
        # When a specific topic/symbol is requested, only keep articles whose
        # title or summary contains at least one of the topic's search terms.
        # An empty match is honest absence of evidence, not permission to use
        # unrelated market headlines.
        if topics and articles:
            filtered = [
                a for a in articles
                if _topic_matches_article((a["title"] + " " + a.get("summary", "")).lower(), topics)
            ]
            if filtered:
                log.info("[sentinel.engine] gather: relevance filter kept %d/%d articles for topics=%r",
                         len(filtered), len(articles), topics)
            else:
                log.info("[sentinel.engine] gather: no relevant articles among %d for topics=%r",
                         len(articles), topics)
            articles = filtered

        self._articles.extend(articles)
        if len(self._articles) > 2000:
            self._articles = self._articles[-2000:]
        result = {"articles": articles, "source_status": raw["source_status"],
                  "count": len(articles), "duration_sec": round(time.time() - started, 2)}
        with self._gather_cache_lock:
            self._gather_cache[cache_key] = (time.time() + _GATHER_CACHE_TTL_SEC, result)
            if len(self._gather_cache) > 256:
                expired = [key for key, value in self._gather_cache.items()
                           if value[0] <= time.time()]
                for key in expired:
                    self._gather_cache.pop(key, None)
        return copy.deepcopy(result)

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
        snapshot = self.articles_for_symbol(symbol)
        snapshot_age = snapshot["snapshot_age_sec"]
        rel = snapshot["articles"]
        if not rel:
            return {"symbol": symbol, "sentiment": 0.0, "article_count": 0,
                    "confidence": 0.0, "evidence_state": "degraded",
                    "source_status": snapshot["source_status"],
                    "snapshot_age_sec": round(snapshot_age, 1),
                    "note": "no fresh, symbol-matched news evidence"}
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
             "evidence_state": "fresh",
               "source_status": snapshot["source_status"],
               "snapshot_age_sec": snapshot_age,
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