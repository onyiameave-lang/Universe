"""
Pulse.intelligence.sentiment
============================
Market-aware social sentiment, symbol extraction, and trend/velocity detection.
(Book I Part IV Article VII; Book II Part II Ch VIII.)

Social language differs from news: "moon", "puts", "rug". This module scores
sentiment in that dialect, extracts instruments, and detects TRENDS with
VELOCITY (rate of mention growth), so Pulse catches a narrative building before
it peaks. All deterministic; LLM optionally sharpens ambiguous posts.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

_WORD = re.compile(r"[a-z0-9]+")

SYMBOL_TERMS = {
    "EURUSD": ["eurusd", "euro"], "GBPUSD": ["gbpusd", "cable", "pound"],
    "USDJPY": ["usdjpy", "yen"], "XAUUSD": ["gold", "xau"], "BTCUSD": ["bitcoin", "btc"],
    "ETHUSD": ["ethereum", "eth"], "SPX": ["spy", "s&p", "spx", "sp500"],
    "NVDA": ["nvda", "nvidia"], "TSLA": ["tsla", "tesla"], "AAPL": ["aapl", "apple"],
}

BULLISH = {"moon", "rocket", "buy", "long", "calls", "breakout", "bullish", "pump",
           "rip", "squeeze", "green", "up", "rally", "hold", "diamond"}
BEARISH = {"crash", "dump", "sell", "short", "puts", "bearish", "rug", "red", "down",
           "tank", "drilling", "bagholder", "bear", "collapse"}


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def extract_symbols(text: str) -> List[str]:
    low = (text or "").lower()
    found = [sym for sym, terms in SYMBOL_TERMS.items() if any(t in low for t in terms)]
    # cashtags ($TSLA)
    for m in re.findall(r"\$([A-Za-z]{1,5})", text or ""):
        found.append(m.upper())
    return list(dict.fromkeys(found))


def sentiment(text: str, llm=None) -> float:
    toks = _tokens(text)
    b = sum(1 for t in toks if t in BULLISH)
    r = sum(1 for t in toks if t in BEARISH)
    if (b + r) == 0:
        if llm is not None and getattr(llm, "has_any", False) and len(toks) > 3:
            try:
                from shared.llm import system_prompt
                out = llm.complete(system_prompt("pulse"),
                    f"Social/trader sentiment of this post, -1 (bearish) to 1 (bullish). "
                    f"Reply with only the number.\n\n{text[:300]}", temperature=0.1, max_tokens=8)
                if out.ok:
                    m = re.search(r"-?\d*\.?\d+", out.text)
                    if m:
                        return max(-1.0, min(1.0, float(m.group())))
            except Exception:
                pass
        return 0.0
    return round((b - r) / (b + r), 3)


class TrendDetector:
    """Detect trending symbols/topics with mention velocity."""

    def __init__(self):
        # rolling history: symbol -> list of timestamps
        self._history: Dict[str, List[float]] = defaultdict(list)

    def observe(self, posts: List[Dict[str, Any]]) -> None:
        now = time.time()
        for p in posts:
            for s in p.get("symbols", []):
                self._history[s].append(now)
        # trim to last 24h
        cutoff = now - 86400
        for s in list(self._history):
            self._history[s] = [t for t in self._history[s] if t >= cutoff]
            if not self._history[s]:
                del self._history[s]

    def trends(self, posts: List[Dict[str, Any]], window_min: int = 60) -> List[Dict[str, Any]]:
        self.observe(posts)
        now = time.time()
        by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for p in posts:
            for s in p.get("symbols", []):
                by_symbol[s].append(p)
        out = []
        for sym, group in by_symbol.items():
            if len(group) < 2:
                continue
            recent = [t for t in self._history.get(sym, []) if now - t <= window_min * 60]
            velocity = round(len(recent) / max(window_min, 1), 3)  # mentions/min
            # authenticity-weighted sentiment for the trend
            weights = [p.get("authenticity", 0.5) for p in group]
            wsent = sum(p.get("sentiment", 0) * w for p, w in zip(group, weights))
            wsum = sum(weights) or 1.0
            out.append({"symbol": sym, "mentions": len(group), "velocity_per_min": velocity,
                       "sentiment": round(wsent / wsum, 3),
                       "confidence": round(min(len(group) / 20.0, 1.0), 3)})
        out.sort(key=lambda x: x["mentions"] * (1 + x["velocity_per_min"]), reverse=True)
        return out
