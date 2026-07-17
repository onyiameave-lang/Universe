"""
Pulse.core.collectors  (Universe-oracle social-upgrade v9 — commodity sentiment)
=================================================================================
Changes from v8 (polish release):

  Fix O-5a — Reddit: add Commodity category for XAUUSD/gold/oil topics
    Root cause: _TOPIC_TO_CATEGORY had no entry for "xauusd", "gold", "silver",
    "xagusd", "usoil", "oil", "commodity" etc. So sentiment_for("XAUUSD") called
    gather(topics=["XAUUSD"]) → _topics_to_subs(["XAUUSD"]) → no category match
    → fell back to General subs (worldnews, AskReddit) → 0 gold-related posts.
    Fix: add "Commodity" category with r/Gold, r/wallstreetbets, r/investing,
    r/Economics. Add all commodity keywords to _TOPIC_TO_CATEGORY.

  Fix O-5b — StockTwits: multi-symbol lookup for commodity symbols
    Root cause: _ST_SYMBOL_MAP["XAUUSD"] = "GLD" (single ETF ticker). GLD
    returns generic equity chatter, not gold futures/spot commentary.
    Live test confirmed: XAUUSD (30 msgs), GC_F (30 msgs), GOLD (30 msgs),
    GLD (30 msgs) all return real gold-specific posts.
    Fix: replace _ST_SYMBOL_MAP (str→str) with _SYMBOL_TO_ST_SYMBOLS (str→list)
    for commodity symbols. StockTwitsCollector now fetches all symbols in the
    list and merges results, giving 3× more gold posts per call.

  Fix O-5c — intelligence_engine.sentiment_for() commodity path
    Root cause: sentiment_for("XAUUSD") calls gather(topics=["XAUUSD"]) which
    uses all collectors. Reddit now routes to Commodity subs. StockTwits now
    fetches GLD+XAUUSD+GC_F. But the symbol-matching step
    `if "XAUUSD" in p["symbols"]` still returns 0 because StockTwits posts
    have title="GLD" not "XAUUSD". Fix: add _SYMBOL_ALIASES so "GLD", "GC_F",
    "GOLD", "IAU" all resolve to "XAUUSD" in the symbol-match step.
    This fix lives in intelligence_engine.py (separate file).

// ... existing code ...
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("pulse.collectors")

# Use a real browser UA — Reddit and Nairaland block Python/urllib UA
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 14

PULSE_USER_REGION: str = os.getenv("PULSE_USER_REGION", "NG").upper()

PLATFORM_BASE_TRUST = {
    "reddit":        0.55,
    "hackernews":    0.60,
    "stocktwits":    0.50,
    "cryptopanic":   0.52,
    "googletrends":  0.45,
    "nairaland":     0.50,
    "rss":           0.40,
    "unknown":       0.35,
}


# ─────────────────────────────────────────────────────────────────────────────
# Post dataclass
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Post:
    post_id:      str
    platform:     str
    author:       str       = ""
    title:        str       = ""
    content:      str       = ""
    url:          str       = ""
    score:        int       = 0
    comments:     int       = 0
    created_at:   str       = ""
    category:     str       = "General"
    region:       str       = "Global"
    collected_at: float     = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "post_id":    self.post_id,
            "platform":   self.platform,
            "author":     self.author,
            "title":      self.title,
            "content":    self.content[:400],
            "url":        self.url,
            "score":      self.score,
            "comments":   self.comments,
            "created_at": self.created_at,
            "category":   self.category,
            "region":     self.region,
        }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get(url: str, headers: Optional[Dict] = None,
         timeout: int = _TIMEOUT) -> Optional[str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode(
                r.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return None


def _pid(text: str, platform: str) -> str:
    return "post-" + hashlib.md5(f"{platform}:{text}".encode()).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# Category classifier (keyword-based, zero LLM cost)
# ─────────────────────────────────────────────────────────────────────────────
_CAT_KEYWORDS: Dict[str, List[str]] = {
    "Finance": [
        "stock", "stocks", "market", "markets", "forex", "crypto", "bitcoin",
        "ethereum", "gold", "oil", "crude", "trading", "invest", "investing",
        "investment", "portfolio", "dividend", "earnings", "ipo", "bond",
        "bonds", "inflation", "interest rate", "fed", "central bank", "naira",
        "dollar", "euro", "pound", "yen", "commodity", "commodities",
        "futures", "options", "hedge", "fund", "etf", "defi", "nft",
        "reserves", "tax credit", "barrel", "bonga",
    ],
    "Tech": [
        "ai", "artificial intelligence", "machine learning", "llm", "gpt",
        "openai", "google", "microsoft", "apple", "meta", "amazon", "nvidia",
        "startup", "software", "hardware", "chip", "semiconductor", "cloud",
        "cybersecurity", "hack", "data", "algorithm", "robot", "automation",
        "programming", "developer", "github", "open source", "tech",
    ],
    "Entertainment": [
        "music", "movie", "film", "celebrity", "actor", "actress", "singer",
        "album", "concert", "award", "grammy", "oscar", "netflix", "spotify",
        "youtube", "tiktok", "instagram", "viral", "meme", "fashion",
        "afrobeats", "nollywood", "bollywood", "hollywood",
    ],
    "Sports": [
        "football", "soccer", "basketball", "nba", "nfl", "premier league",
        "champions league", "world cup", "olympics", "tennis", "golf",
        "cricket", "rugby", "athletics", "sport", "sports", "player",
        "team", "match", "goal", "score", "transfer", "super eagles",
        "afcon", "laliga", "bundesliga", "betting", "fc ", " fc",
    ],
    "Politics": [
        "government", "president", "election", "vote", "policy", "law",
        "congress", "senate", "parliament", "minister", "political",
        "democracy", "protest", "war", "conflict", "sanction", "treaty",
        "tinubu", "buhari", "white house", "kremlin", "nato", "un", "imf",
        "world bank", "fg ", "efcc", "nnpc", "fuel price",
    ],
    "Regional": [
        "nigeria", "naija", "lagos", "abuja", "kano", "ibadan", "nairaland",
        "naira", "cbn", "dangote", "gtbank", "zenith", "access bank",
        "mtn nigeria", "airtel nigeria", "jollof", "aso rock",
    ],
}

_CAT_ORDER = ["Regional", "Finance", "Tech", "Sports", "Entertainment", "Politics"]


def _classify(title: str, content: str, region: str = "Global") -> str:
    """Return the best-matching category for a post."""
    text = (title + " " + content).lower()
    scores: Dict[str, int] = {}
    for cat, kws in _CAT_KEYWORDS.items():
        scores[cat] = sum(1 for kw in kws if kw in text)
    if region == "NG":
        scores["Regional"] = scores.get("Regional", 0) + 1
    best_cat = max(scores, key=lambda c: scores[c])
    return best_cat if scores[best_cat] > 0 else "General"


# ─────────────────────────────────────────────────────────────────────────────
# 1. RedditCollector  — old.reddit.com Atom RSS (no OAuth, no 403)
# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: added r/Nigeria, r/NigeriaNews, r/africa, r/naijapolitics, r/lagos
# to the Nigeria category sub list.

_REDDIT_CATEGORY_SUBS: Dict[str, List[str]] = {
    "Finance":       ["wallstreetbets", "stocks", "investing", "forex",
                      "SecurityAnalysis", "StockMarket"],
    "Crypto":        ["CryptoCurrency", "Bitcoin", "ethereum", "CryptoMarkets",
                      "defi"],
    "Tech":          ["technology", "programming", "MachineLearning",
                      "artificial", "netsec"],
    "Entertainment": ["entertainment", "movies", "Music", "television",
                      "popculturechat"],
    "Sports":        ["sports", "soccer", "nba", "nfl", "tennis",
                      "PremierLeague"],
    "Politics":      ["worldnews", "politics", "geopolitics", "news"],
    # FIX 3: expanded Nigeria sub list
    "Nigeria":       ["Nigeria", "NigeriaNews", "naija", "africa",
                      "naijapolitics", "lagos", "AfricanHistory"],
    # FIX O-5a: Commodity category — r/Gold confirmed 200 ✓, r/wallstreetbets ✓
    "Commodity":     ["Gold", "wallstreetbets", "investing", "Economics",
                      "Commodities"],
    "General":       ["worldnews", "AskReddit", "todayilearned", "Futurology"],
}

_TOPIC_TO_CATEGORY: Dict[str, str] = {
    "stock": "Finance", "stocks": "Finance", "market": "Finance",
    "forex": "Finance", "invest": "Finance", "trading": "Finance",
    "etf": "Finance", "bond": "Finance", "inflation": "Finance",
    "fed": "Finance", "sp500": "Finance", "nasdaq": "Finance",
    # FIX O-5a: commodity keywords → Commodity category
    "gold": "Commodity", "xauusd": "Commodity", "xau": "Commodity",
    "silver": "Commodity", "xagusd": "Commodity", "xag": "Commodity",
    "oil": "Commodity", "usoil": "Commodity", "crude": "Commodity",
    "wti": "Commodity", "brent": "Commodity", "opec": "Commodity",
    "commodity": "Commodity", "commodities": "Commodity",
    "bullion": "Commodity", "precious": "Commodity", "metals": "Commodity",
    "copper": "Commodity", "natgas": "Commodity", "gas": "Commodity",
    "crypto": "Crypto", "bitcoin": "Crypto", "btc": "Crypto",
    "ethereum": "Crypto", "eth": "Crypto", "defi": "Crypto", "nft": "Crypto",
    "tech": "Tech", "ai": "Tech", "software": "Tech", "hardware": "Tech",
    "startup": "Tech", "programming": "Tech", "llm": "Tech",
    "entertainment": "Entertainment", "music": "Entertainment",
    "movie": "Entertainment", "film": "Entertainment",
    "celebrity": "Entertainment", "afrobeats": "Entertainment",
    "sport": "Sports", "sports": "Sports", "football": "Sports",
    "soccer": "Sports", "basketball": "Sports", "nba": "Sports",
    "nfl": "Sports", "tennis": "Sports",
    "politics": "Politics", "government": "Politics", "election": "Politics",
    "news": "Politics", "war": "Politics",
    "nigeria": "Nigeria", "naija": "Nigeria", "lagos": "Nigeria",
    "naira": "Nigeria",
}

# Atom namespace used by old.reddit.com RSS feeds
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _topics_to_subs(topics: Optional[List[str]]) -> Tuple[List[str], str]:
    if not topics:
        subs = (
            _REDDIT_CATEGORY_SUBS["Finance"][:3]
            + _REDDIT_CATEGORY_SUBS["General"][:2]
        )
        if PULSE_USER_REGION == "NG":
            subs += _REDDIT_CATEGORY_SUBS["Nigeria"][:3]
        return list(dict.fromkeys(subs)), "General"

    cat_votes: Dict[str, int] = {}
    for t in topics:
        cat = _TOPIC_TO_CATEGORY.get(t.lower())
        if cat:
            cat_votes[cat] = cat_votes.get(cat, 0) + 1

    if cat_votes:
        best_cat = max(cat_votes, key=lambda c: cat_votes[c])
        subs = _REDDIT_CATEGORY_SUBS.get(best_cat, _REDDIT_CATEGORY_SUBS["General"])
        return subs[:5], best_cat

    return (_REDDIT_CATEGORY_SUBS["General"][:3]
            + _REDDIT_CATEGORY_SUBS["Finance"][:2]), "General"


class RedditCollector:
    name      = "reddit"
    available = True

    def collect(self, topics: Optional[List[str]] = None,
                limit: int = 10) -> List[Post]:
        subs, hint_cat = _topics_to_subs(topics)
        out: List[Post] = []

        for sub in subs[:5]:
            url  = f"https://old.reddit.com/r/{sub}/.rss?limit={limit}"
            body = _get(url)
            if not body:
                continue
            try:
                root     = ET.fromstring(body)
                entries  = root.findall("atom:entry", _ATOM_NS)
            except Exception:
                continue

            for entry in entries:
                title_el = entry.find("atom:title", _ATOM_NS)
                link_el  = entry.find("atom:link",  _ATOM_NS)
                auth_el  = entry.find("atom:author/atom:name", _ATOM_NS)
                upd_el   = entry.find("atom:updated", _ATOM_NS)
                cont_el  = entry.find("atom:content", _ATOM_NS)

                title = (title_el.text or "").strip() if title_el is not None else ""
                if not title or len(title) < 5:
                    continue

                url_post = ""
                if link_el is not None:
                    url_post = link_el.get("href", "")

                author   = (auth_el.text or "").strip() if auth_el is not None else ""
                updated  = (upd_el.text  or "")[:10]   if upd_el  is not None else ""
                content  = ""
                if cont_el is not None:
                    raw = cont_el.text or ""
                    content = re.sub(r"<[^>]+>", " ", raw).strip()[:400]

                # FIX 3: mark Nigeria subs as NG region
                is_ng = sub in _REDDIT_CATEGORY_SUBS.get("Nigeria", [])
                cat = _classify(title, content, region="NG" if is_ng else PULSE_USER_REGION)
                out.append(Post(
                    _pid(title, "reddit"), "reddit",
                    author, title, content, url_post,
                    0, 0, updated,
                    category=cat,
                    region="NG" if is_ng else "Global",
                ))

        return out


# ─────────────────────────────────────────────────────────────────────────────
# 2. HNCollector  — Firebase topstories API (no search queries)
# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Replace Algolia search with Firebase topstories API.
# The Algolia search for "trending" matches old meta posts like
# "Find out what is trending", "See what is trending amongst friends".
# The Firebase API returns the ACTUAL current front-page story IDs.
# We fetch the top N IDs then retrieve each item individually.
# This guarantees real, current stories with real titles and URLs.

class HNCollector:
    name      = "hackernews"
    available = True

    # FIX 1: Firebase REST API — returns actual front-page story IDs
    _TOP_URL  = "https://hacker-news.firebaseio.com/v0/topstories.json"
    _ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

    def collect(self, topics: Optional[List[str]] = None,
                limit: int = 10) -> List[Post]:
        # Step 1: get top story IDs
        body = _get(self._TOP_URL)
        if not body:
            return []
        try:
            all_ids: List[int] = json.loads(body)
        except Exception:
            return []

        # Step 2: fetch each item (parallel, up to limit+buffer for filtering)
        fetch_ids = all_ids[: limit * 2]  # fetch extra in case some are jobs/polls

        def _fetch_item(story_id: int) -> Optional[Post]:
            item_body = _get(self._ITEM_URL.format(id=story_id), timeout=8)
            if not item_body:
                return None
            try:
                item = json.loads(item_body)
            except Exception:
                return None

            # Skip jobs, polls, deleted/dead items
            if item.get("type") not in ("story", None):
                return None
            if item.get("deleted") or item.get("dead"):
                return None

            title = (item.get("title") or "").strip()
            if not title or len(title) < 8:
                return None

            url = (
                item.get("url")
                or f"https://news.ycombinator.com/item?id={story_id}"
            )
            cat = _classify(title, "", region=PULSE_USER_REGION)
            return Post(
                _pid(title, "hackernews"), "hackernews",
                item.get("by", ""),
                title[:200],
                "",
                url,
                int(item.get("score", 0) or 0),
                int(item.get("descendants", 0) or 0),
                time.strftime("%Y-%m-%d",
                              time.gmtime(item.get("time", time.time()))),
                category=cat,
                region="Global",
            )

        out: List[Post] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for post in pool.map(_fetch_item, fetch_ids):
                if post is not None:
                    out.append(post)
                    if len(out) >= limit:
                        break

        return out[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# 3. StockTwitsCollector  — confirmed working + CryptoPanic fallback
# ─────────────────────────────────────────────────────────────────────────────
# FIX 4: StockTwits BTC.X confirmed returning 30 messages in live test.
# Added explicit error logging when ST returns non-200 status.
# Added CryptoPanic RSS as a fallback when ST returns 0 results.

# FIX O-5b: _SYMBOL_TO_ST_SYMBOLS — multi-symbol lookup for commodity symbols.
# Live test results (2026-07-17):
#   GLD    → 200, 30 msgs  (ETF, generic equity chatter)
#   XAUUSD → 200, 30 msgs  (spot gold, futures commentary)
#   GC_F   → 200, 30 msgs  (gold futures, "parabolic move above 8000")
#   GOLD   → 200, 30 msgs  (Barrick Gold stock + gold commentary)
#   IAU    → 200, 30 msgs  (iShares gold ETF)
# Using 3 symbols per commodity gives 3× more relevant posts.
_SYMBOL_TO_ST_SYMBOLS: Dict[str, List[str]] = {
    # Gold — spot + futures + ETFs
    "XAUUSD": ["XAUUSD", "GC_F", "GLD"],
    # Silver
    "XAGUSD": ["XAGUSD", "SI_F", "SLV"],
    # Oil
    "USOIL":  ["USOIL", "CL_F", "USO"],
    # Crypto (already multi-symbol via _TOPIC_TO_ST_SYMBOLS)
    "BTCUSD": ["BTC.X"],
    "ETHUSD": ["ETH.X"],
    # Forex
    "EURUSD": ["EUR.USD"],
    "GBPUSD": ["GBP.USD"],
    "USDJPY": ["JPY.USD"],
    # Equity indices
    "SPX":    ["SPY"],
    "NDX":    ["QQQ"],
}

_ST_SYMBOL_MAP: Dict[str, str] = {
    "BTCUSD":  "BTC.X",
    "ETHUSD":  "ETH.X",
    "XRPUSD":  "XRP.X",
    "SOLUSD":  "SOL.X",
    "EURUSD":  "EUR.USD",
    "GBPUSD":  "GBP.USD",
    "USDJPY":  "JPY.USD",
    "XAUUSD":  "GLD",   # kept for backward compat; _SYMBOL_TO_ST_SYMBOLS takes priority
    "USOIL":   "USO",
    "SPX":     "SPY",
}

_TOPIC_TO_ST_SYMBOLS: Dict[str, List[str]] = {
    "crypto":    ["BTC.X", "ETH.X"],
    "bitcoin":   ["BTC.X"],
    "btc":       ["BTC.X"],
    "ethereum":  ["ETH.X"],
    "eth":       ["ETH.X"],
    # FIX O-5b: gold/commodity topics → multi-symbol
    "gold":      ["XAUUSD", "GC_F", "GLD"],
    "xauusd":    ["XAUUSD", "GC_F", "GLD"],
    "xau":       ["XAUUSD", "GC_F"],
    "silver":    ["XAGUSD", "SLV"],
    "xagusd":    ["XAGUSD", "SLV"],
    "oil":       ["USOIL", "CL_F", "USO"],
    "usoil":     ["USOIL", "CL_F", "USO"],
    "crude":     ["USOIL", "CL_F"],
    "commodity": ["XAUUSD", "GC_F", "USOIL"],
    "sp500":     ["SPY"],
    "nasdaq":    ["QQQ"],
    "tech":      ["AAPL", "NVDA", "MSFT"],
    "stock":     ["SPY", "QQQ"],
    "stocks":    ["SPY", "QQQ"],
    "market":    ["SPY", "QQQ"],
    "forex":     ["EUR.USD", "GBP.USD"],
}

_ST_DEFAULT_SYMBOLS = ["SPY", "BTC.X", "ETH.X", "AAPL", "TSLA", "NVDA"]

# CryptoPanic RSS — free, no API key, covers crypto news with sentiment
_CRYPTOPANIC_RSS = "https://cryptopanic.com/news/rss/"


def _to_st_symbol(sym: str) -> str:
    return _ST_SYMBOL_MAP.get(sym.upper(), sym.upper())


def _to_st_symbols(sym: str) -> List[str]:
    """Return the list of StockTwits symbols to query for a given input symbol.
    FIX O-5b: commodity symbols map to multiple ST tickers for better coverage."""
    upper = sym.upper()
    if upper in _SYMBOL_TO_ST_SYMBOLS:
        return _SYMBOL_TO_ST_SYMBOLS[upper]
    # fall back to single-symbol map, then identity
    return [_ST_SYMBOL_MAP.get(upper, upper)]


class StockTwitsCollector:
    name      = "stocktwits"
    available = True
    _API = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"

    def collect(self, topics: Optional[List[str]] = None,
                limit: int = 10) -> List[Post]:
        symbols: List[str] = []

        if topics:
            # FIX O-5b: use _to_st_symbols() for multi-symbol commodity lookup
            raw_syms = [t for t in topics if t.isupper() and len(t) <= 8]
            for s in raw_syms:
                symbols.extend(_to_st_symbols(s))
            if not symbols:
                for t in topics:
                    # also check _SYMBOL_TO_ST_SYMBOLS for lowercase topic names
                    upper_t = t.upper()
                    if upper_t in _SYMBOL_TO_ST_SYMBOLS:
                        symbols.extend(_SYMBOL_TO_ST_SYMBOLS[upper_t])
                    else:
                        mapped = _TOPIC_TO_ST_SYMBOLS.get(t.lower(), [])
                        symbols.extend(mapped)

        if not symbols:
            symbols = _ST_DEFAULT_SYMBOLS

        seen: set = set()
        symbols = [s for s in symbols if not (s in seen or seen.add(s))]  # type: ignore

        out: List[Post] = []
        for sym in symbols[:6]:  # FIX O-5b: allow up to 6 (was 4) for commodity multi-symbol
            encoded = urllib.parse.quote(sym, safe=".")
            url     = self._API.format(sym=encoded)
            body    = _get(url)
            if not body:
                continue
            try:
                data = json.loads(body)
                resp_status = data.get("response", {}).get("status")
                if resp_status not in (None, 200):
                    # FIX 4: log the actual error for diagnosability
                    errors = data.get("errors", data.get("error", "unknown"))
                    log.warning("StockTwits %s returned status %s: %s",
                                sym, resp_status, errors)
                    continue
                messages = data.get("messages", [])
            except Exception as exc:
                log.warning("StockTwits parse error for %s: %s", sym, exc)
                continue

            for m in messages[:limit]:
                text = m.get("body", "")
                if not text:
                    continue
                user = m.get("user") or {}
                cat  = _classify(sym + " " + text, "", region=PULSE_USER_REGION)
                out.append(Post(
                    _pid(text, "stocktwits"), "stocktwits",
                    user.get("username", ""), sym, text, "",
                    int(user.get("followers", 0) or 0), 0,
                    (m.get("created_at") or "")[:10],
                    category=cat,
                    region="Global",
                ))

        # FIX 4: CryptoPanic RSS fallback when ST returns nothing
        if not out:
            out = self._cryptopanic_fallback(limit)

        return out

    def _cryptopanic_fallback(self, limit: int) -> List[Post]:
        """CryptoPanic RSS — free, no API key, covers crypto/finance news."""
        body = _get(_CRYPTOPANIC_RSS, timeout=12)
        if not body:
            return []
        fallback: List[Post] = []
        try:
            root  = ET.fromstring(body)
            items = root.findall(".//item")
            for item in items[:limit]:
                t = item.find("title")
                l = item.find("link")
                if t is None or not t.text:
                    continue
                title = t.text.strip()
                link  = l.text.strip() if l is not None and l.text else ""
                cat   = _classify(title, "", region=PULSE_USER_REGION)
                fallback.append(Post(
                    _pid(title, "cryptopanic"), "cryptopanic",
                    "cryptopanic", title, title, link,
                    0, 0, time.strftime("%Y-%m-%d"),
                    category=cat, region="Global",
                ))
        except Exception:
            pass
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# 4. GoogleTrendsCollector  — per-item article URLs from ht:news_item
# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: The <link> element in each RSS <item> points to the feed URL itself
# for every item — not to an individual trend page. Each <item> contains
# <ht:news_item> sub-elements with real article URLs:
#   <ht:news_item_url>   — URL of a real news article about this trend
#   <ht:news_item_title> — title of that article
# Use the first <ht:news_item_url> as the post URL.

class GoogleTrendsCollector:
    """
    Returns currently trending searches from Google Trends.

    Primary:  /trending/rss?geo=NG  (no library, no API key)
    Fallback: /trending/rss?geo=US  (if NG returns nothing)
    """
    name      = "googletrends"
    available = True

    _RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"
    _HT_NS   = "https://trends.google.com/trending/rss"

    def collect(self, topics: Optional[List[str]] = None,
                limit: int = 15) -> List[Post]:
        geo   = PULSE_USER_REGION
        posts = self._collect_rss(geo, limit)
        if not posts and geo != "US":
            posts = self._collect_rss("US", limit)
        return posts

    def _collect_rss(self, geo: str, limit: int) -> List[Post]:
        url  = self._RSS_URL.format(geo=geo)
        body = _get(url, timeout=15)
        if not body:
            return []

        out: List[Post] = []
        try:
            root  = ET.fromstring(body)
            items = root.findall(".//item")
            for item in items[:limit]:
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                term = title_el.text.strip()
                if not term or len(term) < 4:
                    continue

                traffic_el = item.find(f"{{{self._HT_NS}}}approx_traffic")
                traffic    = traffic_el.text if traffic_el is not None else ""

                # FIX 2: extract per-item article URL from ht:news_item
                # Each trend item has one or more <ht:news_item> children with
                # <ht:news_item_url> pointing to a real news article.
                article_url = ""
                article_title = ""
                news_items = item.findall(f"{{{self._HT_NS}}}news_item")
                if news_items:
                    first_ni = news_items[0]
                    url_el   = first_ni.find(f"{{{self._HT_NS}}}news_item_url")
                    atitle_el = first_ni.find(f"{{{self._HT_NS}}}news_item_title")
                    if url_el is not None and url_el.text:
                        article_url = url_el.text.strip()
                    if atitle_el is not None and atitle_el.text:
                        article_title = atitle_el.text.strip()

                # Fall back to Google Trends explore URL if no news_item
                if not article_url:
                    article_url = (
                        f"https://trends.google.com/trends/explore"
                        f"?q={urllib.parse.quote(term)}&geo={geo}"
                    )

                # Use article title as content if available (more informative)
                content_text = article_title if article_title else (
                    f"Trending: {term}" + (f" (~{traffic} searches)" if traffic else "")
                )

                cat = _classify(term, article_title, region=geo)
                out.append(Post(
                    _pid(term + geo, "googletrends"), "googletrends",
                    "google_trends",
                    term,
                    content_text,
                    article_url,
                    0, 0,
                    time.strftime("%Y-%m-%d"),
                    category=cat,
                    region=geo,
                ))
        except Exception as exc:
            log.warning("GoogleTrends parse error: %s", exc)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# 5. NairalandCollector  — skip sticky/pinned threads (unchanged from v7)
# ─────────────────────────────────────────────────────────────────────────────
class NairalandCollector:
    """
    Scrapes Nairaland.com for recent (non-pinned) threads.
    No API key needed — public HTML scraping.
    """
    name      = "nairaland"
    available = True

    _SECTIONS: Dict[str, str] = {
        "":              "General",
        "business":      "Finance",
        "investment":    "Finance",
        "politics":      "Politics",
        "entertainment": "Entertainment",
        "sports":        "Sports",
        "technology":    "Tech",
        "romance":       "Entertainment",
        "crime":         "Politics",
        "education":     "General",
    }

    _BASE = "https://www.nairaland.com"
    _MIN_THREAD_ID = 1_000_000

    def collect(self, topics: Optional[List[str]] = None,
                limit: int = 15) -> List[Post]:
        sections = self._pick_sections(topics)
        out: List[Post] = []

        for section, cat in sections:
            url  = f"{self._BASE}/{section}" if section else self._BASE
            body = _get(url, timeout=15)
            if not body:
                continue
            posts = self._parse_nairaland(
                body, cat,
                limit_per_section=max(4, limit // len(sections) + 2),
            )
            out.extend(posts)
            if len(out) >= limit:
                break

        return out[:limit]

    def _pick_sections(self, topics: Optional[List[str]]) -> List[Tuple[str, str]]:
        if not topics:
            return [
                ("",              "General"),
                ("business",      "Finance"),
                ("politics",      "Politics"),
                ("entertainment", "Entertainment"),
            ]
        result: List[Tuple[str, str]] = []
        for t in topics:
            tl = t.lower()
            if any(k in tl for k in ["finance", "stock", "invest", "market", "naira"]):
                result.append(("business",   "Finance"))
                result.append(("investment", "Finance"))
            elif any(k in tl for k in ["politic", "government", "election"]):
                result.append(("politics",   "Politics"))
            elif any(k in tl for k in ["sport", "football", "soccer"]):
                result.append(("sports",     "Sports"))
            elif any(k in tl for k in ["tech", "ai", "software"]):
                result.append(("technology", "Tech"))
            elif any(k in tl for k in ["entertainment", "music", "movie"]):
                result.append(("entertainment", "Entertainment"))
        if not result:
            result = [("", "General"), ("business", "Finance")]
        seen: set = set()
        return [item for item in result if not (item[0] in seen or seen.add(item[0]))]  # type: ignore

    def _parse_nairaland(self, html: str, default_cat: str,
                         limit_per_section: int = 10) -> List[Post]:
        out: List[Post] = []
        seen_ids: set   = set()

        chunks = html.split('<td id="top')
        for chunk in chunks[1:]:
            id_m = re.match(r'^(\d+)', chunk)
            if not id_m:
                continue
            thread_id_str = id_m.group(1)

            try:
                thread_id = int(thread_id_str)
                if thread_id < self._MIN_THREAD_ID:
                    continue
            except ValueError:
                continue

            if "sticky.gif" in chunk:
                continue

            link_m = re.search(
                r'href="(/' + thread_id_str + r'/[^"]+)"[^>]*>([^<]{5,200})</a>',
                chunk, re.IGNORECASE,
            )
            if not link_m:
                continue

            path  = link_m.group(1)
            title = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
            title = (title
                     .replace("&#x27;", "'")
                     .replace("&amp;", "&")
                     .replace("&lt;", "<")
                     .replace("&gt;", ">")
                     .replace("&quot;", '"')
                     .replace("&#39;", "'"))

            if not title or len(title) < 8:
                continue

            pid = _pid(title, "nairaland")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            cat = _classify(title, "", region="NG") or default_cat
            out.append(Post(
                pid, "nairaland",
                "nairaland_user", title, title,
                self._BASE + path,
                0, 0,
                time.strftime("%Y-%m-%d"),
                category=cat,
                region="NG",
            ))
            if len(out) >= limit_per_section:
                break

        return out


# ─────────────────────────────────────────────────────────────────────────────
# 6. RSSCollector  — generic RSS fallback (unchanged)
# ─────────────────────────────────────────────────────────────────────────────
class RSSCollector:
    name      = "rss"
    available = True

    def __init__(self, feed_urls: Optional[List[str]] = None):
        self._feeds = feed_urls or []

    def collect(self, topics: Optional[List[str]] = None,
                limit: int = 10) -> List[Post]:
        out: List[Post] = []
        for feed_url in self._feeds:
            body = _get(feed_url, timeout=15)
            if not body:
                continue
            try:
                root  = ET.fromstring(body)
                items = root.findall(".//item")
                for item in items[:limit]:
                    t = item.find("title")
                    l = item.find("link")
                    if t is None or not t.text:
                        continue
                    title = t.text.strip()
                    link  = l.text.strip() if l is not None and l.text else ""
                    cat   = _classify(title, "", region=PULSE_USER_REGION)
                    out.append(Post(
                        _pid(title, "rss"), "rss",
                        "", title, title, link,
                        0, 0, time.strftime("%Y-%m-%d"),
                        category=cat, region=PULSE_USER_REGION,
                    ))
            except Exception:
                continue
        return out[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# CollectorRegistry
# ─────────────────────────────────────────────────────────────────────────────
class CollectorRegistry:
    """
    Manages all collectors and runs them in parallel.

    Default collector set depends on PULSE_USER_REGION:
      - Always:    reddit, hackernews, stocktwits, googletrends
      - NG region: + nairaland
    """

    def __init__(self):
        self._all: Dict[str, Any] = {
            "reddit":       RedditCollector(),
            "hackernews":   HNCollector(),
            "stocktwits":   StockTwitsCollector(),
            "googletrends": GoogleTrendsCollector(),
            "nairaland":    NairalandCollector(),
        }
        self._default = ["reddit", "hackernews", "stocktwits", "googletrends"]
        if PULSE_USER_REGION == "NG":
            self._default.append("nairaland")

    @property
    def collectors(self) -> Dict[str, Any]:
        return self._all

    def collect(self, topics: Optional[List[str]] = None,
                sources: Optional[List[str]] = None,
                limit: int = 10) -> Dict[str, Any]:
        chosen = [s for s in (sources or self._default) if s in self._all]

        posts:  List[Post]     = []
        status: Dict[str, Any] = {}

        def _run(name: str):
            c = self._all[name]
            if not getattr(c, "available", False):
                return name, [], "unavailable"
            try:
                items = c.collect(topics=topics, limit=limit)
                return name, items, "ok" if items else "no_results"
            except Exception as exc:
                return name, [], f"error: {exc}"

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(chosen), 5)) as pool:
            for name, items, st in pool.map(_run, chosen):
                status[name] = {"collected": len(items), "status": st}
                posts.extend(items)

        seen: set = set()
        deduped: List[Post] = []
        for p in posts:
            if p.post_id not in seen:
                seen.add(p.post_id)
                deduped.append(p)

        if not deduped:
            status["_summary"] = (
                "no social posts collected "
                "(platforms unreachable or no matches)"
            )

        return {"posts": deduped, "source_status": status}

    def collect_by_category(
            self,
            topics: Optional[List[str]] = None,
            sources: Optional[List[str]] = None,
            limit: int = 10,
    ) -> Dict[str, Any]:
        """
        Like collect() but also returns posts grouped by category.
        Used by intelligence_engine for multi-category reports.
        """
        result = self.collect(topics=topics, sources=sources, limit=limit)
        posts  = result["posts"]

        by_cat: Dict[str, List[Post]] = {}
        for p in posts:
            by_cat.setdefault(p.category, []).append(p)

        result["by_category"] = {
            cat: [p.to_dict() for p in ps]
            for cat, ps in by_cat.items()
        }
        result["categories_found"] = sorted(by_cat.keys())
        return result