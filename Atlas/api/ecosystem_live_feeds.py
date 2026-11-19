"""
Atlas.api.ecosystem_live_feeds
==============================
Collects real-time social media posts for Pulse (with @handle / display name attribution
from Mastodon #markets/#finance/#trading/#bitcoin, Bluesky Social, HackerNews, and Dev.to)
and real-time breaking world & market news for Sentinel from public RSS feeds,
and persists the snapshot into Chronicle (`Chronicle/memory/research/ecosystem_live_feeds.json`).
"""
from __future__ import annotations

import concurrent.futures
import datetime
import html
import json
import re
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

_ECO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_PATH = _ECO_ROOT / "Chronicle" / "memory" / "research" / "ecosystem_live_feeds.json"

_UA = "UniverseEcosystem/2.0 (+https://universe.local)"

_BULL_WORDS = {
    "bull", "bullish", "long", "buy", "rally", "surge", "breakout", "gain", "gains",
    "soar", "soars", "up", "higher", "record", "ath", "support", "upgrade", "profit",
    "boom", "recovery", "strong", "optimism", "positive", "calls", "moon", "green",
    "growth", "launch", "win", "breakthrough", "success",
}
_BEAR_WORDS = {
    "bear", "bearish", "short", "sell", "crash", "drop", "plunge", "dump", "down",
    "lower", "resistance", "downgrade", "loss", "recession", "inflation", "tariff",
    "war", "crisis", "risk", "fear", "panic", "red", "decline", "slump", "default",
    "hack", "warning", "debt", "deficit", "cut", "cuts",
}


def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_get(url: str, timeout: float = 4.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean_html(raw: str) -> str:
    txt = html.unescape(raw or "")
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _score_sentiment(text: str) -> Dict[str, Any]:
    tokens = re.findall(r"[a-z]+", text.lower())
    bull = sum(1 for t in tokens if t in _BULL_WORDS)
    bear = sum(1 for t in tokens if t in _BEAR_WORDS)
    if bull > bear:
        label = "BULLISH"
        score = min(0.92, 0.58 + 0.08 * (bull - bear))
    elif bear > bull:
        label = "BEARISH"
        score = max(0.12, 0.42 - 0.08 * (bear - bull))
    else:
        label = "NEUTRAL"
        score = 0.54
    return {"label": label, "score": round(score, 2)}


_NON_FINANCIAL_NOISE = re.compile(
    r"\b(?:mercados|culinary|flamenco|farmers\s+market|flea\s+market|recipe|cookbook|gardening)\b",
    re.IGNORECASE,
)


def _fetch_mastodon_tag(tag: str, limit: int = 3) -> List[Dict[str, Any]]:
    url = f"https://mastodon.social/api/v1/timelines/tag/{tag}?limit={limit + 4}"
    out: List[Dict[str, Any]] = []
    try:
        raw = _http_get(url, timeout=4.0)
        data = json.loads(raw)
        for item in data:
            acct_obj = item.get("account") or {}
            acct = str(acct_obj.get("acct") or acct_obj.get("username") or "market_poster").strip()
            display = str(acct_obj.get("display_name") or acct).strip()
            avatar = str(acct_obj.get("avatar_static") or acct_obj.get("avatar") or "").strip()
            content = _clean_html(str(item.get("content") or ""))
            if len(content) < 20 or _NON_FINANCIAL_NOISE.search(content):
                continue
            media = item.get("media_attachments") or []
            img_url = ""
            if media and isinstance(media[0], dict):
                img_url = str(media[0].get("preview_url") or media[0].get("url") or "").strip()
            short_content = content[:195] + ("..." if len(content) > 195 else "")
            sent = _score_sentiment(content)
            favs = int(item.get("favourites_count") or 0)
            reblogs = int(item.get("reblogs_count") or 0)
            replies = int(item.get("replies_count") or 0)
            created = str(item.get("created_at") or "")[:16].replace("T", " ") + " UTC"
            out.append({
                "platform": f"Mastodon #{tag}",
                "author": f"@{acct}",
                "display_name": display,
                "avatar_url": avatar,
                "image_url": img_url,
                "headline": short_content,
                "sentiment": sent["label"],
                "sentiment_score": sent["score"],
                "upvotes": favs + reblogs,
                "comments": replies,
                "time": created,
                "title": f"[{sent['label']}] @{acct} ({display}) posted on #{tag}: \"{short_content}\" ({favs} likes · {reblogs} boosts · {replies} replies)",
                "source": f"Mastodon #{tag} · Account: @{acct}",
            })
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _fetch_bluesky_feed(limit: int = 4) -> List[Dict[str, Any]]:
    url = (
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed"
        "?feed=at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
        f"&limit={limit + 4}"
    )
    out: List[Dict[str, Any]] = []
    try:
        raw = _http_get(url, timeout=4.0)
        data = json.loads(raw)
        for item in data.get("feed") or []:
            post = item.get("post") or {}
            author_obj = post.get("author") or {}
            handle = str(author_obj.get("handle") or "user.bsky.social").strip()
            display = str(author_obj.get("displayName") or handle).strip()
            avatar = str(author_obj.get("avatar") or "").strip()
            rec = post.get("record") or {}
            text = _clean_html(str(rec.get("text") or ""))
            if len(text) < 20 or _NON_FINANCIAL_NOISE.search(text):
                continue
            embed = post.get("embed") or {}
            images = embed.get("images") or (embed.get("media") or {}).get("images") or []
            img_url = ""
            if images and isinstance(images[0], dict):
                img_url = str(images[0].get("thumb") or images[0].get("fullsize") or "").strip()
            short_text = text[:195] + ("..." if len(text) > 195 else "")
            sent = _score_sentiment(text)
            likes = int(post.get("likeCount") or 0)
            reposts = int(post.get("repostCount") or 0)
            replies = int(post.get("replyCount") or 0)
            created = str(rec.get("createdAt") or "")[:16].replace("T", " ") + " UTC"
            out.append({
                "platform": "Bluesky Social",
                "author": f"@{handle}",
                "display_name": display,
                "avatar_url": avatar,
                "image_url": img_url,
                "headline": short_text,
                "sentiment": sent["label"],
                "sentiment_score": sent["score"],
                "upvotes": likes,
                "comments": replies,
                "time": created,
                "title": f"[{sent['label']}] @{handle} ({display}) posted on Bluesky: \"{short_text}\" ({likes} likes · {reposts} reposts · {replies} replies)",
                "source": f"Bluesky Social · Account: @{handle}",
            })
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _fetch_hackernews_posts(limit: int = 4) -> List[Dict[str, Any]]:
    url = f"https://hn.algolia.com/api/v1/search_by_date?tags=story&numericFilters=points>15&hitsPerPage={limit}"
    out: List[Dict[str, Any]] = []
    try:
        raw = _http_get(url, timeout=4.0)
        data = json.loads(raw)
        for h in (data.get("hits") or [])[:limit]:
            author = str(h.get("author") or "hn_user").strip()
            title = _clean_html(str(h.get("title") or ""))
            if not title:
                continue
            points = int(h.get("points") or 0)
            comments = int(h.get("num_comments") or 0)
            sent = _score_sentiment(title)
            created = str(h.get("created_at") or "")[:16].replace("T", " ") + " UTC"
            out.append({
                "platform": "HackerNews",
                "author": f"@{author}",
                "display_name": author,
                "headline": title,
                "sentiment": sent["label"],
                "sentiment_score": sent["score"],
                "upvotes": points,
                "comments": comments,
                "time": created,
                "title": f"[{sent['label']}] @{author} posted on HackerNews: \"{title}\" ({points} pts · {comments} comments)",
                "source": f"HackerNews · Account: @{author}",
            })
    except Exception:
        pass
    return out


def _fetch_rss_feed(name: str, url: str, category: str, limit: int = 4) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        raw = _http_get(url, timeout=4.0)
        root = ET.fromstring(raw)
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for it in items[:limit]:
            title_el = it.find("title")
            if title_el is None:
                title_el = it.find("{http://www.w3.org/2005/Atom}title")
            desc_el = it.find("description")
            if desc_el is None:
                desc_el = it.find("{http://www.w3.org/2005/Atom}summary")
            pub_el = it.find("pubDate")
            if pub_el is None:
                pub_el = it.find("{http://www.w3.org/2005/Atom}updated")

            title = _clean_html(title_el.text if title_el is not None and title_el.text else "")
            if not title:
                continue
            clean_desc = _clean_html(desc_el.text if desc_el is not None and desc_el.text else "")[:175]
            pub_str = (pub_el.text if pub_el is not None and pub_el.text else "").strip()
            short_time = pub_str[:22] if pub_str else "Today"
            sent = _score_sentiment(f"{title} {clean_desc}")
            out.append({
                "outlet": name,
                "category": category,
                "headline": title,
                "summary": clean_desc,
                "sentiment": sent["label"],
                "time": short_time,
                "title": f"[{name} · {category}] {title}" + (f" — {clean_desc}" if clean_desc and clean_desc.lower() != title.lower() else ""),
                "source": f"{name} ({sent['label']})",
            })
    except Exception:
        pass
    return out


def refresh_live_feeds() -> Dict[str, Any]:
    mastodon_tags = ["markets", "finance", "trading", "bitcoin", "economy"]
    rss_sources = [
        ("Al Jazeera World", "https://www.aljazeera.com/xml/rss/all.xml", "Geopolitics & World"),
        ("BBC Business & World", "https://feeds.bbci.co.uk/news/business/rss.xml", "Global Macro"),
        ("CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "Markets"),
        ("MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "Wall Street"),
        ("WSJ Global Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "Institutional Markets"),
        ("OilPrice Energy Wire", "https://oilprice.com/rss/main", "Commodities & Energy"),
    ]

    social_posts: List[Dict[str, Any]] = []
    world_news: List[Dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        m_futs = [pool.submit(_fetch_mastodon_tag, t, 2) for t in mastodon_tags]
        bsky_fut = pool.submit(_fetch_bluesky_feed, 4)
        hn_fut = pool.submit(_fetch_hackernews_posts, 4)
        rss_futs = [pool.submit(_fetch_rss_feed, name, url, cat, 3) for name, url, cat in rss_sources]

        for f in m_futs:
            try:
                social_posts.extend(f.result(timeout=5.0))
            except Exception:
                pass
        for f in (bsky_fut, hn_fut):
            try:
                social_posts.extend(f.result(timeout=5.0))
            except Exception:
                pass
        for f in rss_futs:
            try:
                world_news.extend(f.result(timeout=5.0))
            except Exception:
                pass

    # Deduplicate social posts by headline prefix
    seen_headlines = set()
    unique_social: List[Dict[str, Any]] = []
    for p in social_posts:
        key = str(p.get("headline", ""))[:50].lower()
        if key and key not in seen_headlines:
            seen_headlines.add(key)
            unique_social.append(p)

    bull_count = sum(1 for p in unique_social if p.get("sentiment") == "BULLISH")
    bear_count = sum(1 for p in unique_social if p.get("sentiment") == "BEARISH")
    total_posts = len(unique_social) or 1
    avg_sent_score = round(
        sum(float(p.get("sentiment_score", 0.54)) for p in unique_social) / total_posts, 2
    ) if unique_social else 0.58
    overall_sentiment = "BULLISH" if bull_count >= bear_count else "CAUTIOUS / MIXED"

    unique_authors = len({p.get("author") for p in unique_social if p.get("author")})

    payload = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pulse": {
            "overall_sentiment": overall_sentiment,
            "sentiment_score": avg_sent_score,
            "posts_analyzed": len(unique_social),
            "influencers_tracked": unique_authors,
            "bullish_count": bull_count,
            "bearish_count": bear_count,
            "social_posts": unique_social[:18],
        },
        "sentinel": {
            "headlines_count": len(world_news),
            "sources_monitored": len(rss_sources),
            "world_news": world_news[:18],
        },
    }

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass

    return payload


if __name__ == "__main__":
    res = refresh_live_feeds()
    sys.stdout.write(json.dumps(res))
