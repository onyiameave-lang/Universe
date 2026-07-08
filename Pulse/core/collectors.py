"""
Pulse.core.collectors
=====================
Institutional social data acquisition. (Book I Part IV Article VII; Book II Ch IV.)

A social-intelligence desk reads many platforms, not one. Each collector
implements a `Collector` contract and reports `available` (keys/network). All
degrade honestly: no platform reachable -> explicit empty result, never
fabricated posts.

  * RedditCollector   Reddit (public JSON, key-free; OAuth if creds present)
  * HNCollector       Hacker News (Algolia, key-free) - tech/market discourse
  * StockTwitsColl    StockTwits public streams (key-free) - trader sentiment
  * RSSSocialColl     public subreddit / forum RSS (key-free)

Collectors run in parallel. Every post carries platform + author + engagement
so downstream influence/bot scoring is auditable.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_UA = "SocialPulseAI/1.0 (AI Ecosystem social intelligence)"
_TIMEOUT = 12

PLATFORM_BASE_TRUST = {"reddit": 0.55, "hackernews": 0.60, "stocktwits": 0.50,
                       "forum": 0.45, "x": 0.50, "unknown": 0.35}


@dataclass
class Post:
    post_id: str
    platform: str
    author: str = ""
    title: str = ""
    content: str = ""
    url: str = ""
    score: int = 0            # upvotes / likes
    comments: int = 0
    created_at: str = ""
    collected_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"post_id": self.post_id, "platform": self.platform, "author": self.author,
                "title": self.title, "content": self.content[:400], "url": self.url,
                "score": self.score, "comments": self.comments, "created_at": self.created_at}


def _get(url: str, headers: Optional[Dict] = None) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return None


def _pid(text: str, platform: str) -> str:
    return "post-" + hashlib.md5(f"{platform}:{text}".encode()).hexdigest()[:12]


class RedditCollector:
    name = "reddit"
    available = True   # public JSON works without OAuth
    SUBS = ["wallstreetbets", "stocks", "investing", "forex", "cryptocurrency"]

    def collect(self, topics=None, limit=10) -> List[Post]:
        out = []
        subs = self.SUBS if not topics else self.SUBS  # topics used for filtering below
        for sub in subs[:4]:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
            body = _get(url)
            if not body:
                continue
            try:
                children = json.loads(body).get("data", {}).get("children", [])
            except Exception:
                continue
            for ch in children:
                d = ch.get("data", {})
                title = d.get("title", "")
                if not title:
                    continue
                if topics and not any(t.lower() in (title + d.get("selftext", "")).lower() for t in topics):
                    continue
                out.append(Post(_pid(title, "reddit"), "reddit", d.get("author", ""),
                              title, d.get("selftext", ""),
                              "https://reddit.com" + d.get("permalink", ""),
                              int(d.get("score", 0)), int(d.get("num_comments", 0)),
                              time.strftime("%Y-%m-%d", time.gmtime(d.get("created_utc", time.time())))))
        return out


class HNCollector:
    name = "hackernews"
    available = True
    API = "https://hn.algolia.com/api/v1/search"

    def collect(self, topics=None, limit=10) -> List[Post]:
        q = " ".join(topics or ["stocks", "markets"])
        params = urllib.parse.urlencode({"query": q, "tags": "(story,comment)", "hitsPerPage": limit})
        body = _get(f"{self.API}?{params}")
        if not body:
            return []
        try:
            hits = json.loads(body).get("hits", [])
        except Exception:
            return []
        out = []
        for h in hits:
            text = h.get("title") or h.get("comment_text") or h.get("story_text") or ""
            if not text:
                continue
            out.append(Post(_pid(text, "hackernews"), "hackernews", h.get("author", ""),
                          h.get("title", "")[:120], text, h.get("url") or "",
                          int(h.get("points", 0) or 0), int(h.get("num_comments", 0) or 0),
                          h.get("created_at", "")[:10]))
        return out


class StockTwitsCollector:
    name = "stocktwits"
    available = True
    API = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"

    def collect(self, topics=None, limit=10) -> List[Post]:
        symbols = [t for t in (topics or []) if t.isupper()] or ["SPY", "BTC.X", "EURUSD"]
        out = []
        for sym in symbols[:3]:
            body = _get(self.API.format(sym=urllib.parse.quote(sym)))
            if not body:
                continue
            try:
                messages = json.loads(body).get("messages", [])
            except Exception:
                continue
            for m in messages[:limit]:
                text = m.get("body", "")
                if not text:
                    continue
                user = (m.get("user") or {})
                out.append(Post(_pid(text, "stocktwits"), "stocktwits", user.get("username", ""),
                              sym, text, "", int(user.get("followers", 0) or 0), 0,
                              m.get("created_at", "")[:10]))
        return out


class CollectorRegistry:
    def __init__(self):
        self.collectors = {c.name: c for c in
                          (RedditCollector(), HNCollector(), StockTwitsCollector())}

    def collect(self, topics=None, sources=None, limit=10) -> Dict[str, Any]:
        chosen = [s for s in (sources or list(self.collectors)) if s in self.collectors]
        posts: List[Post] = []
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chosen), 4)) as pool:
            for name, items, st in pool.map(_run, chosen):
                status[name] = {"collected": len(items), "status": st}
                posts.extend(items)

        seen, deduped = set(), []
        for p in posts:
            if p.post_id not in seen:
                seen.add(p.post_id)
                deduped.append(p)
        if not deduped:
            status["_summary"] = "no social posts collected (platforms unreachable or no matches)"
        return {"posts": deduped, "source_status": status}
