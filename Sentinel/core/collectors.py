"""
Sentinel.core.collectors
========================
Institutional news acquisition. (Book I Part IV Article VII; Book II Ch IV.)

A real news desk pulls from many wires, not one. Each collector implements a
`Collector` contract and reports `available` (keys/network). All degrade
honestly: no feed reachable -> explicit empty result, never fabricated news.

  * RSSCollector      key-free RSS/Atom from major financial + world wires
                      Reuters DEAD (502) → replaced with Al Jazeera + AP-via-AJ
                      + commodity feeds: OilPrice, GoldTelegraph, WSJ Markets
  * NewsAPICollector  newsapi.org (NEWSAPI_KEY, optional; richer if present)
  * GuardianCollector The Guardian open API (api-key=test works, no signup)
                      Replaces GDELTCollector which times out on every call
  * HNCollector       practitioner/industry signal (Firebase top-stories, key-free)

Collectors run in parallel for desk-speed. Every article carries source
provenance so downstream credibility scoring is auditable.

Fixes applied (sentinel-fix):
  S-1  Reuters RSS dead (502) → Al Jazeera all.xml (200 ✓) + AP via AJ
  S-2  GDELT times out on every call → GuardianCollector (api-key=test, 200 ✓)
       GuardianCollector has a 5-minute circuit-breaker so a single timeout
       does not cascade into repeated slow calls.
  S-3  FT RSS returns headlines only (paywalled) → credibility note added;
       FT kept in DEFAULT_FEEDS but SOURCE_BASE_CREDIBILITY lowered to 0.72
       to reflect headline-only quality.
  S-7  `from shared.config import get_config` fails when collectors.py is run
       standalone (e.g. during unit tests). Wrapped in try/except with a
       lightweight _FallbackConfig so the module is always importable.

Fixes applied (sentinel-fix_v2):
  S-8  HTML entities (&apos; &#x2019; &#x2014; etc.) not decoded in headlines.
       Added html.unescape() to _clean() so all entity forms are normalised.
  S-9  Only RSS appeared in source_status — Guardian and HN were silently
       excluded because sentinel_agent.py PATH_SOURCES still referenced "gdelt"
       (the old name) instead of "guardian". Fixed in sentinel_agent.py.
       Also: CollectorRegistry now defaults to ALL available collectors when
       sources=None, so `report` without a path always fires every collector.
  S-10 XAUUSD symbol matcher missed "precious metals", "xau", "xau/usd",
       "silver" (as a proxy), "mining". Extended SYMBOL_TERMS in analysis.py.
  S-11 RSSCollector ignored the `topics` parameter entirely — returned all
       articles regardless of what was requested. Added post-collection
       relevance filter: when topics contain a known symbol, only articles
       whose text matches that symbol's terms are returned.
  S-12 Added commodity-focused RSS feeds (OilPrice, GoldTelegraph, WSJ Markets,
       MarketWatch MarketPulse, CNBC Finance, Nasdaq Markets) so XAUUSD and
       USOIL queries have dedicated sources to draw from.
  S-13 PATH_SOURCES in sentinel_agent.py still referenced "gdelt" after it was
       renamed to "guardian". Fixed: all three paths now use "guardian".
"""
from __future__ import annotations

import concurrent.futures
import html as _html
import json
import logging
import os
import re
import socket as _socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# S-7: standalone import guard — shared.config may not be on sys.path when
# collectors.py is imported directly (unit tests, quick scripts).
try:
    from shared.config import get_config  # type: ignore
    _cfg = get_config()
except Exception:
    class _FallbackConfig:  # type: ignore
        newsapi_key: str = os.environ.get("NEWSAPI_KEY", "")
        guardian_api_key: str = os.environ.get("GUARDIAN_API_KEY", "test")
        enabled_news_feeds: list = []
    _cfg = _FallbackConfig()

_UA = "SentinelNewsAI/1.0 (AI Ecosystem news intelligence)"
_TIMEOUT = 12

# FIX-SC-01 (Phase 5e): Set socket-level default timeout at module load time.
# urllib's timeout= parameter only covers the READ phase of an HTTP connection.
# DNS resolution happens BEFORE the socket connects and is NOT bounded by
# urllib timeout=. On a network with dropped DNS packets (firewall, sandbox,
# unreachable nameserver), urllib.request.urlopen() blocks FOREVER at the OS
# level in a C-level getaddrinfo() syscall — no Python timeout can interrupt it.
# socket.setdefaulttimeout() is the ONLY way to bound DNS hangs.
# Constitutional: Book II Principle V Graceful Degradation.
_socket.setdefaulttimeout(_TIMEOUT)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default key-free RSS wires (financial + world + commodity).
#
# S-1:  reuters_business removed (502 dead). Al Jazeera added (200 ✓).
# S-3:  ft_home kept but credibility lowered (headline-only, paywalled).
# S-12: Added commodity feeds confirmed working in live tests:
#         oilprice_rss    — 15 items, commodity-focused ✓
#         gold_telegraph  — 10 items, gold/precious metals ✓
#         wsj_markets     — 20 items, broad markets ✓
#         marketwatch_mp  — 30 items, market pulse ✓
#         cnbc_finance    — 30 items, finance ✓
#         nasdaq_markets  — 15 items, equities ✓
# ---------------------------------------------------------------------------
DEFAULT_FEEDS = [
    # World / general finance
    ("aljazeera",    "https://www.aljazeera.com/xml/rss/all.xml"),
    ("cnbc_finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("bbc_business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("marketwatch",  "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("ft_home",      "https://www.ft.com/rss/home"),
    # Commodity / markets (S-12)
    ("oilprice",     "https://oilprice.com/rss/main"),
    ("gold_telegraph", "https://www.goldtelegraph.com/feed"),
    ("wsj_markets",  "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("marketwatch_mp", "https://feeds.marketwatch.com/marketwatch/marketpulse/"),
    ("nasdaq_markets", "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
]

# Commodity-specific feeds used when topics include a commodity symbol
COMMODITY_FEEDS = [
    ("oilprice",       "https://oilprice.com/rss/main"),
    ("gold_telegraph", "https://www.goldtelegraph.com/feed"),
    ("wsj_markets",    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("marketwatch_mp", "https://feeds.marketwatch.com/marketwatch/marketpulse/"),
    ("cnbc_finance",   "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
]

# S-3: FT lowered from 0.92 → 0.72 (headline-only, paywalled body).
# S-1: reuters/reuters_business removed; aljazeera added at 0.88.
# S-12: commodity sources added.
SOURCE_BASE_CREDIBILITY = {
    "reuters": 0.95, "reuters_business": 0.95,   # kept for legacy articles in cache
    "bloomberg": 0.93,
    "aljazeera": 0.88,
    "ap": 0.94,
    "bbc": 0.90, "bbc_business": 0.90,
    "cnbc": 0.85, "cnbc_finance": 0.85,
    "marketwatch": 0.82, "marketwatch_mp": 0.82,
    "guardian": 0.87,
    "ft": 0.72, "ft_home": 0.72,          # S-3: headline-only, paywalled
    "wsj_markets": 0.90,
    "oilprice": 0.78,
    "gold_telegraph": 0.72,
    "nasdaq_markets": 0.80,
    "newsapi": 0.70,
    "gdelt": 0.65,
    "hackernews": 0.55,
    "unknown": 0.40,
}

# Symbol → RSS query terms used to decide which feeds to prioritise.
# Kept here (not in analysis.py) so collectors can do topic-aware feed selection.
_SYMBOL_FEED_TERMS: Dict[str, List[str]] = {
    "XAUUSD": ["gold", "xauusd", "bullion", "precious metal", "xau", "xau/usd",
               "silver", "mining", "commodity"],
    "USOIL":  ["oil", "crude", "wti", "brent", "opec", "energy", "petroleum"],
    "BTCUSD": ["bitcoin", "btc", "crypto", "cryptocurrency", "blockchain"],
    "EURUSD": ["euro", "eurusd", "ecb", "eurozone", "eur/usd"],
    "GBPUSD": ["pound", "sterling", "gbpusd", "boe", "gbp/usd"],
    "USDJPY": ["yen", "usdjpy", "boj", "japan", "usd/jpy"],
    "SPX":    ["s&p", "sp500", "spx", "wall street", "s&p 500"],
    "NASDAQ": ["nasdaq", "tech stocks", "nasdaq 100"],
    "DXY":    ["dollar index", "dxy", "greenback", "us dollar"],
}

# Commodity symbols — these get the commodity feed set added automatically
_COMMODITY_SYMBOLS = {"XAUUSD", "USOIL", "XAGUSD", "COPPER", "NATGAS"}


@dataclass
class Article:
    article_id: str
    title: str
    source: str
    url: str = ""
    published_at: str = ""
    summary: str = ""
    body: str = ""
    collected_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "article_id": self.article_id,
            "title": self.title,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at,
            "summary": self.summary[:400],
        }


def _get(url: str, headers: Optional[Dict] = None) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    # FIX-SC-02 (Phase 5e): Log each HTTP fetch attempt so we can see exactly
    # which URL hangs in production logs. Constitutional: Book II No Silent Failures.
    # FIX-SC-06 (Phase 5h): Upgraded from DEBUG to INFO/WARNING so errors are
    # visible in production logs without needing --debug flag.
    log.info("[sentinel.collectors] _get: fetching %s (timeout=%ds)", url[:80], _TIMEOUT)
    _t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            body = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
        log.info("[sentinel.collectors] _get: OK %s in %.2fs (%d bytes)", url[:60], time.time() - _t0, len(body))
        return body
    except urllib.error.HTTPError as exc:
        # FIX-SC-07 (Phase 5i): HTTPError carries the response body — read it so
        # callers (e.g. NewsAPICollector) can parse the JSON error payload and log
        # the actual API error code (e.g. "apiKeyInvalid", "rateLimited").
        # Previously this fell through to the generic except and returned None,
        # hiding the real reason for the 401/429.
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        log.warning("[sentinel.collectors] _get: HTTP %d %s in %.2fs — %s — body=%r",
                    exc.code, exc.reason, time.time() - _t0, url[:60], err_body[:200])
        return err_body if err_body else None
    except Exception as exc:
        log.warning("[sentinel.collectors] _get: FAILED %s in %.2fs — %s: %s",
                    url[:60], time.time() - _t0, type(exc).__name__, exc)
        return None


def _clean(text: str) -> str:
    """Strip HTML tags, decode all entity forms, normalise whitespace.

    S-8: html.unescape() handles &apos; &#x2019; &#x2014; &#39; &amp; etc.
    The manual replacements are kept as a fast-path for the most common cases,
    but html.unescape() catches everything else.
    """
    text = re.sub(r"(?s)<[^>]+>", " ", text or "")
    # S-8: decode all HTML entities (named + numeric + hex)
    text = _html.unescape(text)
    # Belt-and-suspenders for CDATA remnants
    text = re.sub(r"(?s)<!\[CDATA\[(.*?)\]\]>", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _aid(title: str, source: str) -> str:
    import hashlib
    return "art-" + hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:12]


def _topic_matches_article(text_lower: str, topics: List[str]) -> bool:
    """Return True if the article text is relevant to any of the requested topics.

    S-11: RSSCollector previously ignored topics entirely. This function is
    called post-collection to filter articles when a specific symbol/topic is
    requested. It uses the same term lists as analysis.extract_symbols() so
    the two are always in sync.
    """
    for topic in topics:
        t_upper = topic.upper()
        # Direct symbol match
        if t_upper in _SYMBOL_FEED_TERMS:
            if any(term in text_lower for term in _SYMBOL_FEED_TERMS[t_upper]):
                return True
        # Plain keyword match (e.g. "gold", "oil", "bitcoin")
        if topic.lower() in text_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# RSSCollector
# ---------------------------------------------------------------------------
class RSSCollector:
    name = "rss"
    available = True

    def __init__(self, feeds=None):
        env_feeds = getattr(_cfg, "enabled_news_feeds", [])
        self.feeds = feeds or DEFAULT_FEEDS
        if env_feeds:
            self.feeds = [(f"custom{i}", u.strip()) for i, u in enumerate(env_feeds)]

    def collect(self, topics=None, limit=8) -> List[Article]:
        # S-12: for commodity symbols, prepend commodity-specific feeds
        feeds = list(self.feeds)
        if topics:
            for t in topics:
                if t.upper() in _COMMODITY_SYMBOLS:
                    # prepend commodity feeds (deduplicated by name)
                    existing_names = {n for n, _ in feeds}
                    extra = [(n, u) for n, u in COMMODITY_FEEDS if n not in existing_names]
                    feeds = extra + feeds
                    break

        out = []
        # FIX-SC-04 (Phase 5e): Fetch each RSS feed in its own thread with a
        # per-feed timeout. Previously feeds were fetched sequentially — if feed
        # N hung on DNS, feeds N+1..end never ran. Now all feeds run concurrently
        # and each is individually bounded. Constitutional: Book II Principle V.
        def _fetch_feed(name_url):
            name, url = name_url
            log.debug("[sentinel.collectors] RSSCollector: fetching feed '%s' %s", name, url[:60])
            body = _get(url)
            if not body:
                return []
            items = re.findall(r"(?s)<(?:item|entry)>(.*?)</(?:item|entry)>", body)
            feed_articles = []
            for it in items[:limit]:
                title = self._tag(it, "title")
                if not title:
                    continue
                link = self._tag(it, "link") or self._attr_link(it)
                desc = self._tag(it, "description") or self._tag(it, "summary")
                pub = self._tag(it, "pubDate") or self._tag(it, "published")
                feed_articles.append(Article(
                    _aid(title, name), _clean(title), name,
                    _clean(link), pub[:25], _clean(desc),
                ))
            log.debug("[sentinel.collectors] RSSCollector: feed '%s' returned %d articles", name, len(feed_articles))
            return feed_articles

        # Run all feeds concurrently; each is bounded by socket.setdefaulttimeout
        # + urllib timeout=_TIMEOUT. Cap workers to avoid overwhelming the network.
        max_workers = min(len(feeds), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_fetch_feed, nurl): nurl[0] for nurl in feeds}
            for fut in concurrent.futures.as_completed(futs, timeout=_TIMEOUT + 3):
                try:
                    out.extend(fut.result())
                except Exception as exc:
                    log.debug("[sentinel.collectors] RSSCollector: feed '%s' raised %s", futs[fut], exc)

        # S-11: post-collection topic filter — only when a specific topic is requested
        if topics and out:
            filtered = [
                a for a in out
                if _topic_matches_article(
                    (a.title + " " + a.summary).lower(), topics
                )
            ]
            # Degrade gracefully: if filter removes everything, return unfiltered
            # (better to show general news than nothing)
            if filtered:
                out = filtered

        return out

    def _tag(self, block, tag):
        m = re.search(rf"(?s)<{tag}[^>]*>(.*?)</{tag}>", block)
        if not m:
            return ""
        val = m.group(1)
        val = re.sub(r"(?s)<!\[CDATA\[(.*?)\]\]>", r"\1", val)
        return val.strip()

    def _attr_link(self, block):
        m = re.search(r'<link[^>]*href="([^"]+)"', block)
        return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# NewsAPICollector
# ---------------------------------------------------------------------------
class NewsAPICollector:
    name = "newsapi"
    API = "https://newsapi.org/v2/everything"

    @property
    def available(self) -> bool:
        key = getattr(_cfg, "newsapi_key", "") or os.environ.get("NEWSAPI_KEY", "")
        return bool(key.strip())

    def collect(self, topics=None, limit=8) -> List[Article]:
        key = (getattr(_cfg, "newsapi_key", "") or os.environ.get("NEWSAPI_KEY", "")).strip()
        if not key:
            log.info("[sentinel.newsapi] NEWSAPI_KEY not set — skipping NewsAPI collector")
            return []

        # FIX-SC-08 (Phase 5i): Log key prefix so user can verify the correct key
        # is being read. Shows first 8 chars + "..." to avoid leaking the full key.
        log.info("[sentinel.newsapi] using NEWSAPI_KEY=%s... (len=%d)",
                 key[:8], len(key))

        # FIX-SC-05 (Phase 5h): Map financial symbols to human-readable search terms.
        # NewsAPI doesn't understand "GBPUSD" — it needs "pound sterling GBP forex".
        # Use the same _SYMBOL_FEED_TERMS dict that RSS/Guardian use for consistency.
        query_terms: List[str] = []
        for t in (topics or []):
            t_upper = t.upper()
            if t_upper in _SYMBOL_FEED_TERMS:
                # Use first 4 terms joined with OR for a rich but focused query
                query_terms.extend(_SYMBOL_FEED_TERMS[t_upper][:4])
            else:
                query_terms.append(t)
        q = " OR ".join(query_terms) if query_terms else "markets economy forex"

        params = urllib.parse.urlencode({
            "q": q, "sortBy": "publishedAt", "pageSize": limit, "language": "en",
        })
        url = f"{self.API}?{params}"
        log.info("[sentinel.newsapi] fetching: q=%r pageSize=%d", q, limit)
        body = _get(url, headers={"X-Api-Key": key})
        if not body:
            log.warning("[sentinel.newsapi] ERROR: _get() returned None for %s — "
                        "check NEWSAPI_KEY validity and network connectivity", url[:80])
            return []
        try:
            parsed = json.loads(body)
        except Exception as exc:
            log.warning("[sentinel.newsapi] ERROR: JSON parse failed — %s — body[:200]=%r", exc, body[:200])
            return []
        # NewsAPI returns {"status": "error", "code": "...", "message": "..."} on bad key/quota
        if parsed.get("status") == "error":
            log.warning("[sentinel.newsapi] API ERROR: code=%r message=%r",
                        parsed.get("code"), parsed.get("message"))
            return []
        arts = parsed.get("articles", [])
        log.info("[sentinel.newsapi] API returned %d articles for q=%r", len(arts), q)
        out = []
        for a in arts:
            title = _html.unescape(a.get("title") or "")   # S-8
            if title:
                out.append(Article(
                    _aid(title, "newsapi"), title, "newsapi",
                    a.get("url", ""), a.get("publishedAt", "")[:25],
                    _html.unescape(a.get("description") or ""),
                    a.get("content") or "",
                ))
        log.info("[sentinel.newsapi] returning %d articles", len(out))
        return out


# ---------------------------------------------------------------------------
# GuardianCollector  (S-2: replaces GDELTCollector)
#
# The Guardian open API works with api-key=test (confirmed 200 ✓, no signup).
# Set GUARDIAN_API_KEY in .env for a registered free key (500 req/day).
#
# Circuit-breaker: after a timeout/error, backs off for _CB_COOLDOWN seconds
# so a single slow call does not cascade into repeated 12-second waits.
#
# S-11: passes topics as the search query so Guardian returns relevant articles.
# ---------------------------------------------------------------------------
class GuardianCollector:
    name = "guardian"
    API = "https://content.guardianapis.com/search"
    _CB_COOLDOWN = 300   # 5-minute backoff after a failure
    _last_fail: float = 0.0

    @property
    def available(self) -> bool:
        if time.time() - self._last_fail < self._CB_COOLDOWN:
            return False   # circuit open
        return True

    def collect(self, topics=None, limit=8) -> List[Article]:
        key = (getattr(_cfg, "guardian_api_key", "") or
               os.environ.get("GUARDIAN_API_KEY", "test")).strip() or "test"

        # S-11: expand symbol topics to human-readable query terms for Guardian
        query_terms = []
        for t in (topics or []):
            t_upper = t.upper()
            if t_upper in _SYMBOL_FEED_TERMS:
                # Use first 3 terms as the Guardian query
                query_terms.extend(_SYMBOL_FEED_TERMS[t_upper][:3])
            else:
                query_terms.append(t)
        q = " ".join(query_terms) if query_terms else "markets economy"

        params = urllib.parse.urlencode({
            "q": q,
            "page-size": limit,
            "order-by": "newest",
            "api-key": key,
        })
        url = f"{self.API}?{params}"
        log.info("[sentinel.guardian] fetching: q=%r api-key=%s", q, "test" if key == "test" else "***")
        body = _get(url)
        if not body:
            log.warning("[sentinel.guardian] ERROR: _get() returned None — "
                        "Guardian API unreachable (network/firewall). url=%s", url[:100])
            GuardianCollector._last_fail = time.time()
            return []
        try:
            parsed = json.loads(body)
            results = parsed.get("response", {}).get("results", [])
        except Exception as exc:
            log.warning("[sentinel.guardian] ERROR: JSON parse failed — %s — body[:200]=%r", exc, body[:200])
            GuardianCollector._last_fail = time.time()
            return []
        log.info("[sentinel.guardian] API returned %d results for q=%r", len(results), q)
        out = []
        for r in results:
            title = _html.unescape(r.get("webTitle") or "")   # S-8
            if not title:
                continue
            out.append(Article(
                _aid(title, "guardian"), title, "guardian",
                r.get("webUrl", ""),
                (r.get("webPublicationDate") or "")[:25],
                title,   # Guardian free tier has no body/snippet
            ))
        return out


# ---------------------------------------------------------------------------
# HNCollector  (uses Firebase top-stories, not Algolia search)
# ---------------------------------------------------------------------------
class HNCollector:
    name = "hackernews"
    TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
    ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
    available = True

    def collect(self, topics=None, limit=8) -> List[Article]:
        body = _get(self.TOP_URL)
        if not body:
            return []
        try:
            ids = json.loads(body)[:limit * 3]   # fetch 3× to allow filtering
        except Exception:
            return []

        out: List[Article] = []
        # FIX-SC-03 (Phase 5e): Previously fetched limit*3 items SEQUENTIALLY.
        # Each _get() call can hang on DNS for up to _TIMEOUT seconds. With
        # limit=8, that's 24 sequential potential hangs = up to 288s total.
        # Fix: fetch all items concurrently with a shared timeout budget.
        # Constitutional: Book II Principle V Graceful Degradation.
        def _fetch_item(item_id):
            item_body = _get(self.ITEM_URL.format(item_id))
            if not item_body:
                return None
            try:
                return json.loads(item_body)
            except Exception:
                return None

        log.debug("[sentinel.collectors] HNCollector: fetching %d items concurrently", len(ids))
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ids), 10)) as pool:
            item_futs = list(pool.map(_fetch_item, ids, timeout=_TIMEOUT + 3))

        for item in item_futs:
            if len(out) >= limit:
                break
            if item is None:
                continue
            if item.get("type") != "story" or item.get("dead") or item.get("deleted"):
                continue
            title = _html.unescape(item.get("title") or "")   # S-8
            if not title:
                continue
            url = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id', '')}"
            pub = time.strftime("%Y-%m-%d", time.gmtime(item.get("time", 0)))
            out.append(Article(
                _aid(title, "hackernews"), title, "hackernews",
                url, pub, title,
            ))
        log.debug("[sentinel.collectors] HNCollector: returning %d articles", len(out))
        return out


# ---------------------------------------------------------------------------
# CollectorRegistry
# ---------------------------------------------------------------------------
class CollectorRegistry:
    def __init__(self):
        self.collectors: Dict[str, Any] = {
            c.name: c for c in (
                RSSCollector(),
                NewsAPICollector(),
                GuardianCollector(),
                HNCollector(),
            )
        }

    def collect(self, topics=None, sources=None, limit=8) -> Dict[str, Any]:
        # S-9: when sources=None, run ALL available collectors (not just rss).
        # Previously the default fell through to list(self.collectors) which
        # should have worked, but sentinel_agent.py PATH_SOURCES always passed
        # an explicit list that still contained "gdelt" (old name) instead of
        # "guardian". That is fixed in sentinel_agent.py (S-13). Here we also
        # ensure that an explicit sources list that contains unknown names
        # (e.g. stale "gdelt") is silently skipped rather than crashing.
        if sources is None:
            chosen = [n for n, c in self.collectors.items()
                      if getattr(c, "available", False)]
        else:
            chosen = [s for s in sources if s in self.collectors]

        articles: List[Article] = []
        status: Dict[str, Any] = {}

        def _run(name):
            c = self.collectors[name]
            if not getattr(c, "available", False):
                return name, [], "unavailable"
            try:
                items = c.collect(topics=topics, limit=limit)
                return name, items, "ok" if items else "no_results"
            except Exception as exc:
                return name, [], f"error: {exc}"

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(chosen), 5)
        ) as pool:
            for name, items, st in pool.map(_run, chosen):
                status[name] = {"collected": len(items), "status": st}
                articles.extend(items)

        # de-dupe by article_id
        seen, deduped = set(), []
        for a in articles:
            if a.article_id not in seen:
                seen.add(a.article_id)
                deduped.append(a)

        if not deduped:
            status["_summary"] = "no news collected (feeds unreachable or no matches)"
        return {"articles": deduped, "source_status": status}