"""
Sentinel.intelligence.analysis
=============================
Event clustering, entity/symbol extraction, and market-aware sentiment.
(Book I Part IV Article VII; Book II Part II Ch VIII.)

A news desk groups the flood of articles into EVENTS, extracts which
instruments/entities each concerns, and scores sentiment in a market-aware way
(a "beats expectations" headline is bullish; "misses" is bearish). All real,
deterministic NLP; the LLM optionally sharpens sentiment when a key is present.

Fix applied (sentinel-fix):
  S-5  LLM call in sentiment() had no essential=False gate. With 30 articles
       per report and ~50% having no lexical signal, this fired ~15 LLM calls
       per report — causing 429 storms. Fixed: pass essential=False so the
       LLM call is skipped when SENTINEL_LLM_MODE=essential_only (or any
       non-essential gate is active). The lexical fallback (0.0) is used
       instead, which is honest: "no signal" is the correct answer.

Fixes applied (sentinel-fix_v2):
  S-10 SYMBOL_TERMS for XAUUSD was too narrow — missed "precious metals",
       "xau", "xau/usd", "silver" (as a proxy for gold market), "mining",
       "bullion market", "safe haven". Extended all commodity symbols.
       Also added XAGUSD, COPPER, NATGAS as new symbols.
  S-5b EVENT_TYPES classifier had wrong keyword priorities — "cut" in
       monetary_policy matched "production cuts" (OPEC), "rate" matched
       "streaming rate". Fixed by:
         1. Using PRIORITY_EVENT_TYPES: a ranked list checked in order.
            The first type with ≥1 hit wins (no more "most hits" race).
         2. Commodity type checked BEFORE monetary_policy so "gold" and
            "oil" are not overridden by "cut" or "rate".
         3. Added 8 new event types: tech, corporate, streaming_media,
            crypto, real_estate, healthcare, energy, trade.
  S-6  EventClusterer threshold was too strict (0.35 Jaccard on full token
       sets). General news headlines share few tokens even when covering the
       same story. Improved with:
         1. Lowered base threshold to 0.20.
         2. Named-entity overlap: extracts capitalised words (≥4 chars) and
            checks if ≥2 entities are shared — a strong corroboration signal.
         3. Symbol overlap bonus: same symbol → threshold drops to 0.12.
         4. Bigram overlap: checks 2-word phrases for partial title matches.
       Result: cross_source events now cluster correctly across RSS + Guardian
       + HN when they cover the same story.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set

_WORD = re.compile(r"[a-z0-9]+")
_ENTITY = re.compile(r"\b([A-Z][A-Za-z]{3,}(?:\s+[A-Z][A-Za-z]{2,})*)\b")

# ---------------------------------------------------------------------------
# Symbol → trigger terms
# S-10: extended XAUUSD, USOIL; added XAGUSD, COPPER, NATGAS
# ---------------------------------------------------------------------------
SYMBOL_TERMS: Dict[str, List[str]] = {
    "EURUSD": ["eurusd", "euro", "ecb", "eurozone", "eur/usd", "european central bank"],
    "GBPUSD": ["gbpusd", "pound", "sterling", "boe", "gbp/usd", "bank of england"],
    "USDJPY": ["usdjpy", "yen", "boj", "japan", "usd/jpy", "bank of japan"],
    "XAUUSD": [
        "gold", "xauusd", "bullion", "precious metal", "xau", "xau/usd",
        "silver", "mining stock", "gold price", "gold market", "safe haven",
        "gold etf", "gold futures", "gold reserve", "bullion market",
        "precious metals", "gold demand", "gold supply",
    ],
    "XAGUSD": ["silver", "xagusd", "xag/usd", "silver price", "silver market"],
    "BTCUSD": ["bitcoin", "btc", "crypto", "cryptocurrency", "blockchain",
               "btc/usd", "digital asset", "defi", "ethereum", "altcoin"],
    "SPX":    ["s&p", "sp500", "spx", "wall street", "s&p 500", "s&p500",
               "dow jones", "dow", "djia", "us stocks", "us equities"],
    "NASDAQ": ["nasdaq", "tech stocks", "nasdaq 100", "nasdaq composite",
               "tech index", "faang", "magnificent seven"],
    "DXY":    ["dollar index", "dxy", "greenback", "us dollar", "usd index",
               "dollar strength", "dollar weakness"],
    "USOIL":  ["oil", "crude", "wti", "brent", "opec", "petroleum",
               "energy price", "oil price", "crude oil", "oil market",
               "oil supply", "oil demand", "barrel", "opec+"],
    "COPPER": ["copper", "copper price", "copper market", "base metal"],
    "NATGAS": ["natural gas", "lng", "gas price", "gas market", "natgas"],
}

BULLISH = {
    "surge", "rally", "gain", "beats", "beat", "jump", "soar", "record high",
    "upgrade", "strong", "growth", "optimism", "boost", "rebound", "recovery",
    "outperform", "exceed", "above expectations", "bullish", "breakout",
    "all-time high", "new high", "inflows", "demand rises",
}
BEARISH = {
    "plunge", "crash", "fall", "misses", "miss", "slump", "tumble", "downgrade",
    "weak", "recession", "fear", "selloff", "decline", "contraction", "cut",
    "below expectations", "bearish", "breakdown", "outflows", "demand falls",
    "layoff", "bankruptcy", "default", "crisis", "collapse",
}

# ---------------------------------------------------------------------------
# Event type classifier — PRIORITY ORDER matters.
# S-5b: checked in order; first type with ≥1 hit wins.
# Commodity is checked BEFORE monetary_policy to prevent "cut" (OPEC cuts)
# from being classified as monetary_policy.
# ---------------------------------------------------------------------------
_PRIORITY_EVENT_TYPES: List[tuple] = [
    # (type_name, [trigger_terms])
    ("commodity", [
        "oil", "gold", "opec", "crude", "precious metal", "bullion",
        "silver", "copper", "iron ore", "natural gas", "lng", "commodity",
        "mining", "xau", "brent", "wti", "energy price", "barrel",
        "gold price", "oil price", "opec+",
    ]),
    ("crypto", [
        "bitcoin", "btc", "ethereum", "crypto", "cryptocurrency", "blockchain",
        "defi", "nft", "digital asset", "altcoin", "stablecoin",
    ]),
    ("earnings", [
        "earnings", "profit", "revenue", "guidance", "quarterly", "results",
        "eps", "beats expectations", "misses expectations", "outlook",
        "dividend", "buyback", "ipo", "listing", "valuation",
    ]),
    ("monetary_policy", [
        "interest rate", "fed rate", "rate hike", "rate cut", "central bank",
        "federal reserve", "ecb rate", "boe rate", "boj rate", "quantitative",
        "taper", "hawkish", "dovish", "basis points", "yield curve",
        "bond yield", "inflation target", "rate decision",
    ]),
    ("geopolitical", [
        "war", "sanction", "election", "conflict", "tariff", "trade deal",
        "military", "troops", "ceasefire", "treaty", "diplomatic", "nato",
        "invasion", "missile", "nuclear", "coup", "protest", "riot",
        "geopolitical", "tension", "embargo",
    ]),
    ("macro", [
        "gdp", "unemployment", "jobless claims", "nonfarm payroll", "cpi",
        "ppi", "pmi", "retail sales", "trade balance", "current account",
        "deficit", "surplus", "economic growth", "recession", "contraction",
        "inflation rate", "consumer price",
    ]),
    ("tech", [
        "artificial intelligence", " ai ", "software", "hardware", "chip",
        "semiconductor", "cloud computing", "cybersecurity", "data breach",
        "hack", "startup", "venture capital", "tech", "algorithm",
        "machine learning", "llm", "openai", "google", "microsoft", "apple",
        "spacex", "tesla",
    ]),
    ("streaming_media", [
        "netflix", "streaming", "subscriber", "viewership", "content",
        "disney", "hbo", "prime video", "spotify", "podcast", "box office",
        "entertainment", "media",
    ]),
    ("corporate", [
        "merger", "acquisition", "takeover", "bankruptcy", "restructuring",
        "layoff", "ceo", "executive", "board", "lawsuit", "settlement",
        "antitrust", "regulation", "fine", "penalty", "short seller",
    ]),
    ("real_estate", [
        "housing", "mortgage", "real estate", "property", "home price",
        "rent", "commercial property", "reit",
    ]),
    ("healthcare", [
        "drug", "vaccine", "fda", "clinical trial", "pharma", "biotech",
        "health", "medical", "hospital", "insurance",
    ]),
    ("trade", [
        "export", "import", "trade war", "tariff", "customs", "supply chain",
        "logistics", "shipping", "freight",
    ]),
]

# Flat dict for backward compatibility (used by credibility.py etc.)
EVENT_TYPES: Dict[str, List[str]] = {name: terms for name, terms in _PRIORITY_EVENT_TYPES}


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def _entities(text: str) -> Set[str]:
    """Extract capitalised named entities (≥4 chars) from text."""
    return {m.group(1) for m in _ENTITY.finditer(text or "")}


def _bigrams(tokens: List[str]) -> Set[str]:
    """Return set of adjacent token pairs."""
    return {f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1)}


def extract_symbols(title: str, body: str) -> List[str]:
    text = f"{title} {body}".lower()
    return [sym for sym, terms in SYMBOL_TERMS.items() if any(t in text for t in terms)]


def classify_event(title: str, body: str) -> str:
    """Classify article into an event type.

    S-5b: uses priority-ordered list. First type with ≥1 hit wins.
    This prevents "cut" (OPEC production cuts) from matching monetary_policy
    before commodity gets a chance.
    """
    text = f"{title} {body}".lower()
    for etype, terms in _PRIORITY_EVENT_TYPES:
        if any(t in text for t in terms):
            return etype
    return "general"


def sentiment(title: str, body: str, llm=None) -> float:
    """
    Market-aware sentiment score in [-1, 1].

    Lexical scoring is always attempted first. The LLM is called ONLY when:
      1. There is no lexical signal (b + r == 0), AND
      2. An LLM client is provided, AND
      3. The LLM client has at least one key (has_any), AND
      4. The call is classified as non-essential (essential=False) — meaning
         it will be skipped when SENTINEL_LLM_MODE=essential_only.

    S-5 fix: added `essential=False` to llm.complete() so this call is gated
    by the same circuit-breaker that prevents 429 storms on report commands.
    """
    text = f"{title} {body}".lower()
    b = sum(1 for w in BULLISH if w in text)
    r = sum(1 for w in BEARISH if w in text)
    lexical = (b - r) / (b + r) if (b + r) else 0.0

    if llm is not None and getattr(llm, "has_any", False) and (b + r) == 0:
        try:
            from shared.llm import system_prompt  # type: ignore
            # S-5: essential=False — skipped when LLM mode is essential_only.
            out = llm.complete(
                system_prompt("sentinel"),
                (
                    "Market sentiment of this headline on a scale -1 (bearish) to 1 (bullish). "
                    "Reply with only the number.\n\n" + title
                ),
                temperature=0.1,
                max_tokens=8,
                essential=False,   # ← S-5 fix
            )
            if out.ok:
                m = re.search(r"-?\d*\.?\d+", out.text)
                if m:
                    return max(-1.0, min(1.0, float(m.group())))
        except Exception:
            pass
    return round(lexical, 3)


class EventClusterer:
    """Groups articles into events by shared symbols + topic overlap.

    S-6: improved clustering with three complementary signals:
      1. Jaccard similarity on token sets (lowered threshold 0.35 → 0.20)
      2. Named-entity overlap: ≥2 shared capitalised entities → cluster
      3. Symbol overlap: same symbol → threshold drops to 0.12
      4. Bigram overlap: ≥1 shared 2-word phrase → cluster

    These together catch cross-source corroboration that the old single-signal
    Jaccard missed (e.g. "Gold hits record" vs "Gold prices surge" share the
    entity "Gold" and the bigram "gold prices" but have low Jaccard).
    """

    def cluster(
        self, articles: List[Dict[str, Any]], threshold: float = 0.20
    ) -> List[Dict[str, Any]]:
        tok_sets = [
            set(_tokens(a.get("title", "") + " " + a.get("summary", "")))
            for a in articles
        ]
        bigram_sets = [
            _bigrams(_tokens(a.get("title", "") + " " + a.get("summary", "")))
            for a in articles
        ]
        entity_sets = [
            _entities(a.get("title", "") + " " + a.get("summary", ""))
            for a in articles
        ]

        clusters: List[List[int]] = []
        assigned: set = set()

        for i in range(len(articles)):
            if i in assigned:
                continue
            group = [i]
            assigned.add(i)
            syms_i = set(articles[i].get("symbols", []))

            for j in range(i + 1, len(articles)):
                if j in assigned:
                    continue

                syms_j = set(articles[j].get("symbols", []))
                same_symbol = bool(syms_i & syms_j)

                # Effective threshold: lower when symbols match
                eff_threshold = threshold * 0.6 if same_symbol else threshold

                union = tok_sets[i] | tok_sets[j]
                inter = tok_sets[i] & tok_sets[j]
                jaccard = len(inter) / len(union) if union else 0.0

                # Named-entity overlap: ≥2 shared entities is strong signal
                # (≥1 is enough when same symbol — they're covering the same instrument)
                shared_entities = entity_sets[i] & entity_sets[j]
                entity_match = (
                    len(shared_entities) >= 2
                    or (same_symbol and len(shared_entities) >= 1)
                )

                # Bigram overlap: ≥1 shared 2-word phrase
                bigram_match = bool(bigram_sets[i] & bigram_sets[j])

                # Symbol-only path: same symbol + at least one shared token
                # catches "Gold prices surge" vs "Gold market rallies" (share "gold")
                symbol_token_match = same_symbol and jaccard >= 0.06

                if (
                    jaccard >= eff_threshold
                    or entity_match
                    or (same_symbol and bigram_match)
                    or symbol_token_match
                ):
                    group.append(j)
                    assigned.add(j)

            clusters.append(group)

        events = []
        for group in clusters:
            arts = [articles[i] for i in group]
            symbols = list({s for a in arts for s in a.get("symbols", [])})
            avg_sent = sum(a.get("sentiment", 0) for a in arts) / len(arts)
            avg_cred = sum(a.get("credibility", 0.5) for a in arts) / len(arts)
            sources = list({a.get("source") for a in arts})
            events.append({
                "event_id":        f"evt-{group[0]}",
                "article_count":   len(arts),
                "headline":        arts[0].get("title", ""),
                "symbols":         symbols,
                "event_type":      classify_event(
                    arts[0].get("title", ""), arts[0].get("summary", "")
                ),
                "avg_sentiment":   round(avg_sent, 3),
                "avg_credibility": round(avg_cred, 3),
                "sources":         sources,
                "cross_source":    len(sources) > 1,
                "importance":      round(
                    len(arts) * avg_cred * (1 + len(sources)) / 5.0, 3
                ),
            })
        events.sort(key=lambda e: e["importance"], reverse=True)
        return events