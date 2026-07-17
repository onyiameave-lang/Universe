"""
Pulse.intelligence.sentiment  (Universe-oracle deep-fix v5)
===========================================================
Market-aware social sentiment, symbol extraction, and trend/velocity detection.

Changes in this version:
  * sentiment() now has a 5-layer fallback chain — LLM is layer 4 (last resort),
    called with essential=False so it is SKIPPED INSTANTLY in essential_only mode.
  * Per-text in-memory cache (MD5, 300 s TTL) — repeat posts cost zero.
  * Chronicle memory lookup before LLM — zero API cost if a recent score exists.
  * Richer neutral heuristic (general positive/negative words) so the LLM is
    almost never needed even in "full" mode.
  * USOIL bug fixed — added oil/crude/wti trigger words to SYMBOL_TERMS.
  * All LLM calls pass essential=False — they are advisory, never trade-critical.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

_WORD = re.compile(r"[a-z0-9]+")

# ---------------------------------------------------------------------------
# Symbol → trigger words  (USOIL bug fixed)
# ---------------------------------------------------------------------------
SYMBOL_TERMS = {
    "EURUSD":  ["eurusd", "euro"],
    "GBPUSD":  ["gbpusd", "cable", "pound"],
    "USDJPY":  ["usdjpy", "yen"],
    "XAUUSD":  ["gold", "xau"],
    "BTCUSD":  ["bitcoin", "btc"],
    "ETHUSD":  ["ethereum", "eth"],
    "SPX":     ["spy", "s&p", "spx", "sp500"],
    "NVDA":    ["nvda", "nvidia"],
    "TSLA":    ["tsla", "tesla"],
    "AAPL":    ["aapl", "apple"],
    # ── USOIL fix ──────────────────────────────────────────────────────
    "USOIL":   ["oil", "crude", "wti", "usoil", "brent", "petroleum", "nymex"],
}

BULLISH = {
    "moon", "rocket", "buy", "long", "calls", "breakout", "bullish", "pump",
    "rip", "squeeze", "green", "up", "rally", "hold", "diamond", "surge",
    "soar", "gain", "gains", "upside", "higher", "strong", "strength",
}
BEARISH = {
    "crash", "dump", "sell", "short", "puts", "bearish", "rug", "red", "down",
    "tank", "drilling", "bagholder", "bear", "collapse", "drop", "fall",
    "falling", "lower", "weak", "weakness", "decline", "plunge",
}

# General positive/negative words for the neutral heuristic (layer 3)
_POSITIVE = {
    "good", "great", "positive", "optimistic", "confident", "hope", "hopeful",
    "improve", "improving", "recovery", "recover", "growth", "growing",
}
_NEGATIVE = {
    "bad", "terrible", "negative", "pessimistic", "worried", "worry", "fear",
    "fearful", "worse", "worsen", "decline", "declining", "risk", "risky",
}

# ---------------------------------------------------------------------------
# Per-text sentiment cache (process-wide, 300 s TTL)
# ---------------------------------------------------------------------------
_sent_cache: Dict[str, Dict[str, Any]] = {}
_SENT_CACHE_TTL = 300.0


def _sent_cache_get(key: str) -> Optional[float]:
    entry = _sent_cache.get(key)
    if entry and (time.time() - entry["ts"]) < _SENT_CACHE_TTL:
        return entry["score"]
    return None


def _sent_cache_put(key: str, score: float) -> None:
    _sent_cache[key] = {"score": score, "ts": time.time()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def _text_key(text: str) -> str:
    return hashlib.md5(text[:300].encode("utf-8", errors="replace")).hexdigest()


def extract_symbols(text: str) -> List[str]:
    low = (text or "").lower()
    found = [sym for sym, terms in SYMBOL_TERMS.items() if any(t in low for t in terms)]
    for m in re.findall(r"\$([A-Za-z]{1,5})", text or ""):
        found.append(m.upper())
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# sentiment() — 5-layer fallback chain
# ---------------------------------------------------------------------------
def sentiment(text: str, llm=None, chronicle=None) -> float:
    """
    Score social text from -1.0 (bearish) to +1.0 (bullish).

    Layer 1: in-memory cache (MD5 of text, 300 s TTL)
    Layer 2: BULLISH/BEARISH lexicon  — always available, zero cost
    Layer 3: general positive/negative heuristic — zero cost
    Layer 4: Chronicle memory lookup  — zero LLM cost
    Layer 5: LLM call (essential=False) — skipped in essential_only mode;
             also skipped if circuit breaker is open after a 429

    Returns 0.0 if all layers produce no signal.
    """
    toks = _tokens(text)
    key  = _text_key(text)

    # ── Layer 1: cache ────────────────────────────────────────────────
    cached = _sent_cache_get(key)
    if cached is not None:
        return cached

    # ── Layer 2: BULLISH / BEARISH lexicon ────────────────────────────
    b = sum(1 for t in toks if t in BULLISH)
    r = sum(1 for t in toks if t in BEARISH)
    if (b + r) > 0:
        score = round((b - r) / (b + r), 3)
        _sent_cache_put(key, score)
        return score

    # ── Layer 3: general positive/negative heuristic ──────────────────
    p = sum(1 for t in toks if t in _POSITIVE)
    n = sum(1 for t in toks if t in _NEGATIVE)
    if (p + n) > 0:
        score = round((p - n) / (p + n) * 0.4, 3)   # dampened — less certain
        _sent_cache_put(key, score)
        return score

    # ── Layer 4: Chronicle memory lookup ─────────────────────────────
    if chronicle is not None:
        try:
            results = chronicle.search(
                query=f"sentiment: {text[:120]}",
                memory_type="social", limit=1,
            )
            if results:
                m = re.search(
                    r"sentiment[:\s]+(-?\d*\.?\d+)",
                    results[0].get("content", ""), re.I,
                )
                if m:
                    score = max(-1.0, min(1.0, float(m.group(1))))
                    _sent_cache_put(key, score)
                    return score
        except Exception:
            pass

    # ── Layer 5: LLM (essential=False — skipped in essential_only) ────
    if llm is not None and getattr(llm, "has_any", False) and len(toks) > 3:
        try:
            from shared.llm import system_prompt  # type: ignore
            out = llm.complete(
                system_prompt("pulse"),
                f"Social/trader sentiment of this post, -1 (bearish) to 1 (bullish). "
                f"Reply with only the number.\n\n{text[:300]}",
                temperature=0.1,
                max_tokens=8,
                essential=False,   # ← non-essential: skipped in essential_only mode
            )
            if out.ok:
                m = re.search(r"-?\d*\.?\d+", out.text)
                if m:
                    score = max(-1.0, min(1.0, float(m.group())))
                    _sent_cache_put(key, score)
                    return score
        except Exception:
            pass

    _sent_cache_put(key, 0.0)
    return 0.0


# ---------------------------------------------------------------------------
# TrendDetector — unchanged logic, no LLM calls
# ---------------------------------------------------------------------------
class TrendDetector:
    """Detect trending symbols/topics with mention velocity."""

    def __init__(self):
        self._history: Dict[str, List[float]] = defaultdict(list)

    def observe(self, posts: List[Dict[str, Any]]) -> None:
        now = time.time()
        for p in posts:
            for s in p.get("symbols", []):
                self._history[s].append(now)
        cutoff = now - 86400
        for s in list(self._history):
            self._history[s] = [t for t in self._history[s] if t >= cutoff]
            if not self._history[s]:
                del self._history[s]

    def trends(self, posts: List[Dict[str, Any]],
               window_min: int = 60) -> List[Dict[str, Any]]:
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
            recent = [t for t in self._history.get(sym, [])
                      if now - t <= window_min * 60]
            velocity = round(len(recent) / max(window_min, 1), 3)
            weights  = [p.get("authenticity", 0.5) for p in group]
            wsent    = sum(p.get("sentiment", 0) * w for p, w in zip(group, weights))
            wsum     = sum(weights) or 1.0
            out.append({
                "symbol":          sym,
                "mentions":        len(group),
                "velocity_per_min": velocity,
                "sentiment":       round(wsent / wsum, 3),
                "confidence":      round(min(len(group) / 20.0, 1.0), 3),
            })
        out.sort(key=lambda x: x["mentions"] * (1 + x["velocity_per_min"]), reverse=True)
        return out
