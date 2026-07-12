"""
Sentinel.core.collectors
========================
Institutional news acquisition. (Book I Part IV Article VII; Book II Ch IV.)

A real news desk pulls from many wires, not one. Each collector implements a
`Collector` contract and reports `available` (keys/network). All degrade
honestly: no feed reachable -> explicit empty result, never fabricated news.

  * RSSCollector      key-free RSS/Atom from major financial + world wires
  * NewsAPICollector  newsapi.org (NEWSAPI_KEY, optional; richer if present)
  * GDELTCollector    global coverage volume + tone (key-free)
  * HNCollector       practitioner/industry signal (Algolia, key-free)

Collectors run in parallel for desk-speed. Every article carries source
provenance so downstream credibility scoring is auditable.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.config import get_config
_cfg = get_config()

_UA = "SentinelNewsAI/1.0 (AI Ecosystem news intelligence)"
_TIMEOUT = 12

# Default key-free RSS wires (financial + world). Extend via ENABLED_NEWS_FEEDS.
DEFAULT_FEEDS = [
    ("reuters_business", "https://feeds.reuters.com/reuters/businessNews"),
    ("cnbc_finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("bbc_business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("marketwatch", "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("ft_home", "https://www.ft.com/rss/home"),
]

SOURCE_BASE_CREDIBILITY = {
    "reuters": 0.95, "reuters_business": 0.95, "bloomberg": 0.93, "ft": 0.92, "ft_home": 0.92,
    "wall_street_journal": 0.92, "ap": 0.94, "bbc": 0.90, "bbc_business": 0.90,
    "cnbc": 0.85, "cnbc_finance": 0.85, "marketwatch": 0.82, "newsapi": 0.70,
    "gdelt": 0.65, "hackernews": 0.55, "unknown": 0.40,
}


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
        return {"article_id": self.article_id, "title": self.title, "source": self.source,
                "url": self.url, "published_at": self.published_at,
                "summary": self.summary[:400]}


def _get(url: str, headers: Optional[Dict] = None) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return None


def _clean(text: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", text or "")
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(a, b)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _aid(title: str, source: str) -> str:
    import hashlib
    return "art-" + hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:12]


class RSSCollector:
    name = "rss"
    available = True

    def __init__(self, feeds=None):
        env_feeds = _cfg.enabled_news_feeds
        self.feeds = feeds or DEFAULT_FEEDS
        if env_feeds:
            self.feeds = [(f"custom{i}", u.strip()) for i, u in enumerate(env_feeds)]

    def collect(self, topics=None, limit=8) -> List[Article]:
        out = []
        for name, url in self.feeds:
            body = _get(url)
            if not body:
                continue
            items = re.findall(r"(?s)<(?:item|entry)>(.*?)</(?:item|entry)>", body)
            for it in items[:limit]:
                title = self._tag(it, "title")
                if not title:
                    continue
                link = self._tag(it, "link") or self._attr_link(it)
                desc = self._tag(it, "description") or self._tag(it, "summary")
                pub = self._tag(it, "pubDate") or self._tag(it, "published")
                out.append(Article(_aid(title, name), _clean(title), name, _clean(link),
                                 pub[:25], _clean(desc)))
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


class NewsAPICollector:
    name = "newsapi"
    API = "https://newsapi.org/v2/everything"

    @property
    def available(self) -> bool:
        return bool(_cfg.newsapi_key.strip())

    def collect(self, topics=None, limit=8) -> List[Article]:
        key = _cfg.newsapi_key.strip()
        if not key:
            return []
        q = " OR ".join(topics or ["markets", "economy", "forex"])
        params = urllib.parse.urlencode({"q": q, "sortBy": "publishedAt",
                                        "pageSize": limit, "language": "en"})
        body = _get(f"{self.API}?{params}", headers={"X-Api-Key": key})
        if not body:
            return []
        try:
            arts = json.loads(body).get("articles", [])
        except Exception:
            return []
        out = []
        for a in arts:
            title = a.get("title") or ""
            if title:
                src = (a.get("source") or {}).get("name", "newsapi")
                out.append(Article(_aid(title, "newsapi"), title, "newsapi",
                                 a.get("url", ""), a.get("publishedAt", "")[:25],
                                 a.get("description") or "", a.get("content") or ""))
        return out


class GDELTCollector:
    name = "gdelt"
    API = "https://api.gdeltproject.org/api/v2/doc/doc"
    available = True

    def collect(self, topics=None, limit=8) -> List[Article]:
        q = " ".join(topics or ["markets"])
        params = urllib.parse.urlencode({"query": q, "mode": "artlist",
                                        "maxrecords": limit, "format": "json"})
        body = _get(f"{self.API}?{params}")
        if not body:
            return []
        try:
            arts = json.loads(body).get("articles", [])
        except Exception:
            return []
        return [Article(_aid(a.get("title", ""), "gdelt"), a.get("title", ""), "gdelt",
                       a.get("url", ""), a.get("seendate", "")[:8], a.get("title", ""))
                for a in arts if a.get("title")]


class HNCollector:
    name = "hackernews"
    API = "https://hn.algolia.com/api/v1/search_by_date"
    available = True

    def collect(self, topics=None, limit=8) -> List[Article]:
        q = " ".join(topics or ["economy"])
        params = urllib.parse.urlencode({"query": q, "tags": "story", "hitsPerPage": limit})
        body = _get(f"{self.API}?{params}")
        if not body:
            return []
        try:
            hits = json.loads(body).get("hits", [])
        except Exception:
            return []
        out = []
        for h in hits:
            title = h.get("title") or h.get("story_title") or ""
            if title:
                out.append(Article(_aid(title, "hackernews"), title, "hackernews",
                                 h.get("url") or "", h.get("created_at", "")[:10], title))
        return out


class CollectorRegistry:
    def __init__(self):
        self.collectors = {c.name: c for c in
                          (RSSCollector(), NewsAPICollector(), GDELTCollector(), HNCollector())}

    def collect(self, topics=None, sources=None, limit=8) -> Dict[str, Any]:
        chosen = [s for s in (sources or list(self.collectors)) if s in self.collectors]
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chosen), 5)) as pool:
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
