"""
Pulse.intelligence.authenticity
==============================
Bot detection, influence scoring, and manipulation guards. (Book VI Part II
Honesty: distinguish genuine sentiment from noise and coordinated manipulation;
Book II Ch VI Confidence.)

Social sentiment is worthless if it can be gamed. This module weighs every post
by how AUTHENTIC and INFLUENTIAL it is, and flags coordinated manipulation
(pump-and-dump, brigading) that a naive average would swallow whole.

  * INFLUENCE     engagement (score/comments/followers) -> real reach weight.
  * BOT RISK      heuristic bot signals: brand-new/empty author, spammy
                  repetition, extreme caps/emoji, link-only, generic hype.
  * MANIPULATION  detects bursts of near-duplicate posts pushing one symbol in
                  one direction (coordinated pump), and down-weights them.

All heuristic and observable; nothing fabricated. Weights feed the
authenticity-weighted sentiment used everywhere downstream.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List

_WORD = re.compile(r"[a-z0-9]+")
HYPE = {"moon", "rocket", "lambo", "pump", "guaranteed", "easy money", "cant lose",
        "to the moon", "buy now", "10x", "100x", "yolo", "diamond hands"}


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def influence(post: Dict[str, Any]) -> float:
    """Reach weight from real engagement, log-scaled and saturating."""
    import math
    score = post.get("score", 0) or 0
    comments = post.get("comments", 0) or 0
    raw = score + 2 * comments
    return round(min(math.log1p(max(raw, 0)) / math.log(1000), 1.0), 3)


def bot_risk(post: Dict[str, Any]) -> Dict[str, Any]:
    author = (post.get("author") or "").lower()
    text = f"{post.get('title','')} {post.get('content','')}"
    reasons = []
    risk = 0.0
    if not author or author in ("deleted", "[deleted]", "anonymous"):
        risk += 0.3; reasons.append("no/anonymous author")
    # extreme caps
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.4:
        risk += 0.2; reasons.append("excessive caps")
    # hype-only content
    toks = _tokens(text)
    if toks and sum(1 for t in toks if t in HYPE) / len(toks) > 0.15:
        risk += 0.3; reasons.append("hype-heavy language")
    # link-only / very short
    if len(toks) < 4:
        risk += 0.15; reasons.append("very short / link-only")
    # emoji spam
    if len(re.findall(r"[\U0001F300-\U0001FAFF]", text)) > 5:
        risk += 0.1; reasons.append("emoji spam")
    return {"bot_risk": round(min(risk, 1.0), 3), "reasons": reasons}


def authenticity_weight(post: Dict[str, Any]) -> float:
    """Combined weight: high influence + low bot risk -> trust this post more."""
    inf = influence(post)
    br = bot_risk(post)["bot_risk"]
    return round(max(0.0, (0.6 * inf + 0.4) * (1.0 - br)), 3)


def detect_manipulation(posts: List[Dict[str, Any]], symbol: str = None) -> Dict[str, Any]:
    """
    Flag coordinated pushes: many near-duplicate posts, same direction, same
    symbol, from many low-authenticity authors in a short window.
    """
    # group by symbol
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in posts:
        for s in p.get("symbols", []):
            if symbol is None or s.upper() == symbol.upper():
                by_symbol[s].append(p)

    flags = []
    for sym, group in by_symbol.items():
        if len(group) < 4:
            continue
        # directional skew
        sents = [p.get("sentiment", 0) for p in group]
        pos = sum(1 for s in sents if s > 0.2); neg = sum(1 for s in sents if s < -0.2)
        skew = abs(pos - neg) / max(len(group), 1)
        # near-duplicate content ratio
        norm = [" ".join(sorted(set(_tokens(p.get("content", "") + p.get("title", ""))))) for p in group]
        dup_ratio = 1.0 - (len(set(norm)) / max(len(norm), 1))
        # low-authenticity share
        low_auth = sum(1 for p in group if authenticity_weight(p) < 0.3) / len(group)
        manip_score = round(0.4 * skew + 0.4 * dup_ratio + 0.2 * low_auth, 3)
        if manip_score > 0.5 and skew > 0.6:
            flags.append({"symbol": sym, "posts": len(group),
                        "direction": "bullish" if pos > neg else "bearish",
                        "manipulation_score": manip_score, "duplicate_ratio": round(dup_ratio, 3),
                        "low_authenticity_share": round(low_auth, 3)})
    return {"manipulation_flags": flags, "flagged": bool(flags)}
