"""
Sentinel.intelligence.analysis
=============================
Event clustering, entity/symbol extraction, and market-aware sentiment.
(Book I Part IV Article VII; Book II Part II Ch VIII.)

A news desk groups the flood of articles into EVENTS, extracts which
instruments/entities each concerns, and scores sentiment in a market-aware way
(a "beats expectations" headline is bullish; "misses" is bearish). All real,
deterministic NLP; the LLM optionally sharpens sentiment when a key is present.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

_WORD = re.compile(r"[a-z0-9]+")

# instrument lexicon (symbol -> trigger terms)
SYMBOL_TERMS = {
    "EURUSD": ["eurusd", "euro", "ecb", "eurozone"], "GBPUSD": ["gbpusd", "pound", "sterling", "boe"],
    "USDJPY": ["usdjpy", "yen", "boj", "japan"], "XAUUSD": ["gold", "xauusd", "bullion"],
    "BTCUSD": ["bitcoin", "btc", "crypto"], "SPX": ["s&p", "sp500", "spx", "wall street"],
    "NASDAQ": ["nasdaq", "tech stocks"], "DXY": ["dollar index", "dxy", "greenback"],
    "USOIL": ["oil", "crude", "wti", "brent", "opec"],
}

BULLISH = {"surge", "rally", "gain", "beats", "beat", "jump", "soar", "record high", "upgrade",
           "strong", "growth", "optimism", "boost", "rebound", "recovery"}
BEARISH = {"plunge", "crash", "fall", "misses", "miss", "slump", "tumble", "downgrade",
           "weak", "recession", "fear", "selloff", "decline", "contraction", "cut"}

EVENT_TYPES = {
    "monetary_policy": ["rate", "fed", "ecb", "boe", "boj", "central bank", "hike", "cut", "inflation"],
    "earnings": ["earnings", "profit", "revenue", "guidance", "quarterly", "results"],
    "geopolitical": ["war", "sanction", "election", "conflict", "tariff", "trade deal"],
    "commodity": ["oil", "gold", "opec", "supply", "crude"],
    "macro": ["gdp", "unemployment", "jobs", "cpi", "ppi", "pmi"],
}


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def extract_symbols(title: str, body: str) -> List[str]:
    text = f"{title} {body}".lower()
    return [sym for sym, terms in SYMBOL_TERMS.items() if any(t in text for t in terms)]


def classify_event(title: str, body: str) -> str:
    text = f"{title} {body}".lower()
    best, best_hits = "general", 0
    for etype, terms in EVENT_TYPES.items():
        hits = sum(1 for t in terms if t in text)
        if hits > best_hits:
            best, best_hits = etype, hits
    return best


def sentiment(title: str, body: str, llm=None) -> float:
    text = f"{title} {body}".lower()
    b = sum(1 for w in BULLISH if w in text)
    r = sum(1 for w in BEARISH if w in text)
    lexical = (b - r) / (b + r) if (b + r) else 0.0
    if llm is not None and getattr(llm, "has_any", False) and (b + r) == 0:
        try:
            from shared.llm import system_prompt
            out = llm.complete(system_prompt("sentinel"),
                f"Market sentiment of this headline on a scale -1 (bearish) to 1 (bullish). "
                f"Reply with only the number.\n\n{title}", temperature=0.1, max_tokens=8)
            if out.ok:
                m = re.search(r"-?\d*\.?\d+", out.text)
                if m:
                    return max(-1.0, min(1.0, float(m.group())))
        except Exception:
            pass
    return round(lexical, 3)


class EventClusterer:
    """Groups articles into events by shared symbols + topic overlap."""

    def cluster(self, articles: List[Dict[str, Any]], threshold: float = 0.35) -> List[Dict[str, Any]]:
        tok_sets = [set(_tokens(a.get("title", "") + " " + a.get("summary", ""))) for a in articles]
        clusters: List[List[int]] = []
        assigned = set()
        for i in range(len(articles)):
            if i in assigned:
                continue
            group = [i]; assigned.add(i)
            for j in range(i + 1, len(articles)):
                if j in assigned:
                    continue
                union = tok_sets[i] | tok_sets[j]
                inter = tok_sets[i] & tok_sets[j]
                same_symbol = bool(set(articles[i].get("symbols", [])) &
                                 set(articles[j].get("symbols", [])))
                if union and (len(inter) / len(union) >= threshold or
                            (same_symbol and len(inter) / len(union) >= threshold * 0.6)):
                    group.append(j); assigned.add(j)
            clusters.append(group)

        events = []
        for group in clusters:
            arts = [articles[i] for i in group]
            symbols = list({s for a in arts for s in a.get("symbols", [])})
            avg_sent = sum(a.get("sentiment", 0) for a in arts) / len(arts)
            avg_cred = sum(a.get("credibility", 0.5) for a in arts) / len(arts)
            sources = list({a.get("source") for a in arts})
            events.append({"event_id": f"evt-{group[0]}", "article_count": len(arts),
                          "headline": arts[0].get("title", ""), "symbols": symbols,
                          "event_type": classify_event(arts[0].get("title", ""),
                                                       arts[0].get("summary", "")),
                          "avg_sentiment": round(avg_sent, 3), "avg_credibility": round(avg_cred, 3),
                          "sources": sources, "cross_source": len(sources) > 1,
                          "importance": round(len(arts) * avg_cred * (1 + len(sources)) / 5.0, 3)})
        events.sort(key=lambda e: e["importance"], reverse=True)
        return events
