"""
Nexus.intelligence.domain_classifier
====================================
Learnable domain classification for routing. (Book II Part II Ch IV Task Types;
Book II Part I Ch VII: repositories automatically know which specialist should
receive each task.)

A real weighted classifier, not a keyword lookup:
  * Each domain has a vocabulary of weighted terms.
  * Queries score against every domain via TF weighting.
  * Confidence is a softmax-style margin between the top and runner-up.
  * `reinforce()` updates term weights from real routing feedback, so
    classification genuinely improves over time (Book I Article IX Online
    Learning). Weights persist to disk.

FIX LOG (phase5b-classifier-v1  2026-07-21):
  FIX-DC-07  Intent-first classification: keyword scoring was too biased toward
             financial symbols. "latest news on EURUSD" scored high on "trading"
             because "eurusd" has weight 3.5, overriding "news" (weight 3.5).
             The PRIMARY INTENT ("latest news", "headlines", "breaking") was
             ignored.
             FIX: Added INTENT_SIGNALS dict mapping strong intent phrases to
             domains with override weights. classify() checks INTENT_SIGNALS
             FIRST. If a strong intent phrase is found, its domain score is
             boosted by 10x before softmax, making it dominate keyword scores.
             Examples:
               "latest news on EURUSD"    -> news (Sentinel) ✅
               "is there a trade on EURUSD" -> trading (Oracle) ✅
               "what are people saying"   -> social (Pulse) ✅
               "train a strategy"         -> training (Forge) ✅
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-DC-08  Chronicle memory contamination guard: Chronicle.search() returns
             memories by recency/embedding similarity, not topic relevance.
             "latest news on farmlands" retrieved "what is an animal" memories
             because they were the most recent embeddings.
             FIX: Added relevance_filter(query, memories) method that scores
             each memory against the query using token overlap and rejects
             memories with overlap < 0.1 (less than 10% token match).
             Constitutional law: Book II Principle I Memory First — but only
             relevant memories should be injected.

  FIX-DC-01  DOMAIN_TO_REPO was missing "prediction" -> "oracle" mapping.
             Oracle's actual domain attribute is "prediction" (confirmed from
             Oracle/agents/oracle_agent.py line 108: domain = "prediction").
             The classifier already had "trading" -> "oracle" but queries that
             scored highest on "prediction" would fall through to "nexus".
             FIX: Added "prediction": "oracle" to DOMAIN_TO_REPO.
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-DC-02  SEED_VOCAB "trading" domain lacked financial symbol keywords
             (eurusd, gbpusd, usdjpy, xauusd, btc, eth, open, close, pip,
             spread, leverage, margin, lot, pnl, equity, balance, exposure).
             Compound queries like "is there a trade on EURUSD" scored low on
             "trading" and fell through to "general" -> Atlas.
             FIX: Enriched "trading" vocabulary with 20+ financial terms.
             Constitutional law: Book II Principle I Memory First — correct
             routing is prerequisite to correct agent invocation.

  FIX-DC-03  SEED_VOCAB "news" domain lacked financial news keywords
             (sentiment, financial, market news, wire, gdelt, rss, breaking,
             symbol-specific news terms).
             FIX: Enriched "news" vocabulary.
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-DC-04  SEED_VOCAB "social" domain lacked social sentiment keywords
             (sentiment, opinion, buzz, discussion, forum, influencer, feed,
             mention, reaction, engagement).
             FIX: Enriched "social" vocabulary.
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-DC-05  SEED_VOCAB "research" domain lacked general knowledge keywords
             (what is, explain, define, describe, how does, why, animal,
             biology, physics, history) so "what is an animal" scored 0 on
             all domains and fell through to "general" -> Atlas with low
             confidence.
             FIX: Enriched "research" vocabulary with general knowledge terms.
             Constitutional law: Book II Principle V Graceful Degradation.

  FIX-DC-06  DOMAIN_TO_REPO "general" mapped to "nexus" instead of "atlas".
             "general" queries should go to Atlas (research) as the default
             knowledge agent, not Nexus (coordinator).
             FIX: "general": "atlas" (was "nexus").
             Constitutional law: Book II Principle V Graceful Degradation.
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_WORD = re.compile(r"[a-z0-9]+")

# ---------------------------------------------------------------------------
# FIX-DC-07: Intent signals — strong phrase → domain override.
# These are checked BEFORE keyword scoring. A match boosts the domain score
# by INTENT_BOOST so it dominates even when financial symbols are present.
# Phrases are matched as substrings of the lowercased query.
# ---------------------------------------------------------------------------
INTENT_BOOST = 10.0  # multiplier applied to the intent-matched domain score

INTENT_SIGNALS: Dict[str, str] = {
    # News intent → Sentinel
    "latest news":        "news",
    "recent news":        "news",
    "breaking news":      "news",
    "news on":            "news",
    "news about":         "news",
    "news for":           "news",
    "headlines":          "news",
    "headline":           "news",
    "what is the news":   "news",
    "any news":           "news",
    "market news":        "news",
    "financial news":     "news",
    "news sentiment":     "news",
    "news coverage":      "news",
    "in the news":        "news",
    "news today":         "news",
    # Social intent → Pulse
    "people saying":      "social",
    "what are people":    "social",
    "social media":       "social",
    "on twitter":         "social",
    "on reddit":          "social",
    "on telegram":        "social",
    "on discord":         "social",
    "social sentiment":   "social",
    "community saying":   "social",
    "retail sentiment":   "social",
    "buzz about":         "social",
    "trending on":        "social",
    # Trading intent → Oracle (prediction)
    "is there a trade":   "trading",
    "open trade":         "trading",
    "open position":      "trading",
    "trade signal":       "trading",
    "trade on":           "trading",
    "buy signal":         "trading",
    "sell signal":        "trading",
    "market analysis":    "trading",
    "technical analysis": "trading",
    "price action":       "trading",
    "entry point":        "trading",
    "exit point":         "trading",
    # Research intent → Atlas
    "what is":            "research",
    "what are":           "research",
    "how does":           "research",
    "explain":            "research",
    "define":             "research",
    "definition of":      "research",
    "tell me about":      "research",
    "describe":           "research",
    # Training intent → Forge
    "train a":            "training",
    "train new":          "training",
    "train model":        "training",
    "new strategy":       "training",
    "optimize strategy":  "training",
    "backtest":           "training",
    "evolve strategy":    "training",
    # Creation intent → Genesis
    "create an agent":    "creation",
    "spawn an agent":     "creation",
    "deploy an agent":    "creation",
    "new agent":          "creation",
    "build an agent":     "creation",
}

# ---------------------------------------------------------------------------
# BUG C FIX (Phase 5c): Stop words for relevance_filter().
# The old filter used ALL tokens including "a", "on", "the", "is" etc.
# "train a new strategy on AAPL" shared stop-word tokens with "what is an animal"
# memories → false 50% overlap → 8 "relevant" memories returned → fast path fired.
# Strip stop words before computing overlap; require >=30% meaningful token match
# AND at least 1 meaningful token in common.
# Constitutional law: Book II Principle I Memory First — inject ONLY relevant memories.
# ---------------------------------------------------------------------------
STOP_WORDS: frozenset = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "on", "at", "by", "for", "with", "about",
    "against", "between", "into", "through", "during", "before", "after",
    "above", "below", "from", "up", "down", "out", "off", "over", "under",
    "again", "further", "then", "once", "and", "but", "or", "nor", "so",
    "yet", "both", "either", "neither", "not", "only", "own", "same",
    "than", "too", "very", "just", "because", "as", "until", "while",
    "if", "how", "what", "which", "who", "whom", "this", "that", "these",
    "those", "i", "me", "my", "myself", "we", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "he", "him", "his", "himself",
    "she", "her", "hers", "herself", "it", "its", "itself", "they",
    "them", "their", "theirs", "themselves", "new", "any", "some", "all",
    "each", "every", "no", "more", "most", "other", "such", "own", "s",
    "t", "re", "ve", "ll", "d", "m",
})


def _normalize(word: str) -> str:
    """Truncate to a short prefix so common inflections of the same word
    collide on lookup -- e.g. 'trade', 'trading', 'traded', 'trader' all
    normalize to 'trad'. Cheap alternative to real stemming; short words
    are left as-is since truncating them further would cause unrelated
    words to collide."""
    return word[:5] if len(word) > 5 else word

# FIX-DC-02/03/04/05: Enriched vocabularies for all domains.
# Confirmed agent domains from actual code (phase5 audit):
#   oracle   -> domain="prediction"  (capabilities: market.analyze, trade.signal, trade.execute)
#   sentinel -> domain="news"        (capabilities: news.collect, news.sentiment, news.for_symbol)
#   pulse    -> domain="social"      (capabilities: social.collect, social.sentiment, social.for_symbol)
#   aegis    -> domain="governance"  (capabilities: audit.action, security.scan, compliance)
#   forge    -> domain="training"    (capabilities: training.run, training.evolve)
#   genesis  -> domain="creation"    (capabilities: agent.create, agent.deploy)
#   atlas    -> domain="research"    (capabilities: research.investigate, research.synthesize)
#   chronicle-> domain="memory"      (capabilities: memory.store, memory.retrieve, memory.search)
#   nexus    -> domain="coordination"(capabilities: domain.classify, ecosystem.route)
SEED_VOCAB: Dict[str, Dict[str, float]] = {
    # Oracle: prediction/trading domain — financial markets, instruments, execution
    "trading": {
        # Core trading terms
        "trade": 3.0, "market": 2.5, "forex": 3.0, "stock": 2.5, "price": 2.0,
        "buy": 2.5, "sell": 2.5, "currency": 2.5, "portfolio": 2.5, "position": 3.0,
        "risk": 1.5, "profit": 2.0, "chart": 1.5, "signal": 2.5,
        # Strategy / evolution
        "evolve": 3.0, "strategy": 3.0, "backtest": 3.0, "champion": 2.5,
        "genome": 2.5, "oracle": 2.0, "sharpe": 2.5, "drawdown": 2.5,
        # FIX-DC-02: Financial instrument symbols and execution terms
        "eurusd": 3.5, "gbpusd": 3.5, "usdjpy": 3.5, "xauusd": 3.5,
        "btc": 3.0, "eth": 3.0, "crypto": 2.5, "bitcoin": 3.0,
        "open": 1.5, "close": 1.5, "pip": 3.0, "spread": 2.5,
        "leverage": 3.0, "margin": 2.5, "lot": 2.5, "pnl": 3.0,
        "equity": 2.0, "balance": 1.5, "exposure": 2.5, "hedge": 2.5,
        "long": 2.0, "short": 2.0, "entry": 2.0, "exit": 2.0,
        "stop": 2.0, "limit": 1.5, "order": 2.0, "fill": 1.5,
        "technical": 2.0, "fundamental": 1.5, "indicator": 2.0,
        "rsi": 2.5, "macd": 2.5, "ema": 2.0, "sma": 2.0,
        "volatility": 2.0, "liquidity": 2.0, "volume": 1.5,
        "prediction": 2.0, "forecast": 2.0, "regime": 2.5,
    },
    # Sentinel: news domain — financial news, wires, events
    "news": {
        # Core news terms
        "news": 3.5, "headline": 3.0, "article": 2.0, "press": 2.0, "media": 2.0,
        "report": 1.5, "breaking": 2.5, "story": 1.5, "journalist": 2.0, "coverage": 1.5,
        # FIX-DC-03: Financial news and sentiment terms
        "sentiment": 2.5, "financial": 2.0, "wire": 2.5, "gdelt": 3.0, "rss": 2.5,
        "newsapi": 3.0, "reuters": 2.5, "bloomberg": 2.5, "cnbc": 2.5,
        "announcement": 2.0, "release": 1.5, "event": 1.5, "update": 1.5,
        "credibility": 2.0, "misinformation": 2.0, "cluster": 1.5,
        "symbol": 1.5, "asset": 1.5, "currency": 1.5,
        "impact": 1.5, "reaction": 1.5, "coverage": 1.5,
        "intel": 2.0, "intelligence": 1.5, "desk": 1.5,
    },
    # Pulse: social domain — social media, sentiment, trends
    "social": {
        # Core social terms
        "social": 3.5, "twitter": 3.0, "reddit": 3.0, "sentiment": 3.0, "trending": 2.5,
        "viral": 2.5, "post": 1.5, "community": 2.0, "hype": 2.5, "crowd": 2.0,
        # FIX-DC-04: Social media and engagement terms
        "opinion": 2.5, "buzz": 2.5, "discussion": 2.0, "forum": 2.0,
        "influencer": 2.5, "feed": 2.0, "mention": 2.5, "reaction": 2.0,
        "engagement": 2.5, "follower": 2.0, "like": 1.5, "share": 1.5,
        "telegram": 2.5, "discord": 2.5, "stocktwits": 3.0,
        "retail": 1.5, "trader": 1.5, "fomo": 2.5, "fud": 2.5,
        "manipulation": 2.5, "pump": 2.5, "dump": 2.5,
        "regional": 2.0, "category": 1.5, "trend": 2.0,
    },
    # Atlas: research domain — academic, scientific, general knowledge
    "research": {
        # Core research terms
        "research": 3.0, "paper": 3.0, "study": 2.0, "hypothesis": 3.0, "evidence": 3.0,
        "investigate": 2.0, "experiment": 2.0, "cite": 2.0, "scientific": 2.0, "explain": 1.5,
        # FIX-DC-05: General knowledge and factual query terms
        "what": 2.0, "how": 1.5, "why": 1.5, "define": 2.5, "definition": 2.5,
        "describe": 2.0, "meaning": 2.0, "concept": 2.0, "theory": 2.0,
        "animal": 2.5, "biology": 2.5, "physics": 2.5, "chemistry": 2.5,
        "history": 2.0, "science": 2.5, "technology": 1.5, "math": 2.0,
        "fact": 2.0, "knowledge": 2.0, "learn": 1.5, "understand": 1.5,
        "arxiv": 3.0, "scholar": 2.5, "pubmed": 2.5, "doi": 2.0,
        "corroborate": 2.5, "validate": 2.0, "synthesize": 2.0,
        "source": 1.5, "reference": 1.5, "literature": 2.0,
    },
    # Forge: training domain — ML, model training, optimization
    "training": {
        "train": 3.0, "model": 3.0, "benchmark": 2.0, "optimize": 2.0, "hyperparameter": 3.0,
        "dataset": 2.0, "epoch": 2.0, "accuracy": 1.5, "loss": 1.5, "neural": 2.0,
        "ml": 2.5, "machine": 2.0, "learning": 1.5, "deep": 2.0, "network": 1.5,
        "forge": 2.5, "backend": 2.0, "sklearn": 2.5, "pytorch": 2.5, "tensorflow": 2.5,
        "cross": 1.5, "validation": 2.0, "overfitting": 2.5, "regularize": 2.0,
        "champion": 2.0, "challenger": 2.0, "evolve": 2.0, "discover": 1.5,
    },
    # Chronicle: memory domain — recall, history, stored knowledge
    "memory": {
        "remember": 3.0, "recall": 3.0, "history": 2.0, "knowledge": 2.0, "previous": 2.0,
        "stored": 2.0, "past": 1.5, "memory": 3.0, "retrieve": 2.0,
        "chronicle": 3.0, "episodic": 2.5, "semantic": 2.0, "pillar": 2.0,
        "forget": 2.0, "archive": 2.0, "log": 1.5, "record": 1.5,
        "prior": 2.0, "before": 1.5, "earlier": 1.5, "last": 1.5,
    },
    # Aegis: governance domain — audit, compliance, security, policy
    "governance": {
        "audit": 3.0, "security": 3.0, "compliance": 3.0, "violation": 2.0, "policy": 2.0,
        "govern": 2.0, "constitution": 2.0, "permission": 2.0, "threat": 2.0,
        "aegis": 3.0, "risk": 1.5, "anomaly": 2.5, "monitor": 2.0,
        "breach": 2.5, "escalate": 2.0, "quarantine": 2.5, "heal": 2.0,
        "hash": 2.0, "tamper": 2.5, "verify": 2.0, "integrity": 2.0,
        "rulebook": 2.5, "register": 1.5, "exposure": 1.5,
    },
    # Genesis: creation domain — agent spawning, deployment, lifecycle
    "creation": {
        "create": 2.0, "spawn": 3.0, "agent": 2.0, "build": 1.5, "generate": 2.0,
        "factory": 2.0, "capability": 2.0, "design": 1.5,
        "genesis": 3.0, "deploy": 2.5, "synthesize": 2.0, "certify": 2.5,
        "lifecycle": 2.5, "retire": 2.0, "rollback": 2.0, "gap": 2.0,
        "new": 1.0, "install": 1.5, "register": 1.5,
    },
    # Nexus: coordination domain — routing, orchestration, workflow
    "coordination": {
        "coordinate": 3.0, "route": 2.0, "orchestrate": 3.0, "workflow": 2.0,
        "collaborate": 2.0, "delegate": 2.0, "manage": 1.5,
        "nexus": 3.0, "dispatch": 2.5, "parallel": 2.0, "sla": 2.5,
        "breaker": 2.0, "circuit": 2.0, "session": 1.5,
    },
}

# FIX-DC-01: Added "prediction" -> "oracle" (Oracle's actual domain attr is "prediction").
# FIX-DC-06: Changed "general" -> "atlas" (was "nexus") — general queries go to Atlas.
DOMAIN_TO_REPO = {
    "trading": "oracle",
    "prediction": "oracle",   # FIX-DC-01: Oracle.domain = "prediction" in code
    "news": "sentinel",
    "social": "pulse",
    "research": "atlas",
    "training": "forge",
    "memory": "chronicle",
    "governance": "aegis",
    "creation": "genesis",
    "coordination": "nexus",
    "general": "atlas",        # FIX-DC-06: general knowledge -> Atlas, not Nexus
}


class DomainClassifier:
    def __init__(self, storage_dir: str = "memory"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "classifier_weights.json"
        self._lock = threading.RLock()
        self._vocab: Dict[str, Dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._vocab = {d: self._normalize_vocab(v) for d, v in raw.items()}
                return
            except Exception:
                pass
        self._vocab = {d: self._normalize_vocab(v) for d, v in SEED_VOCAB.items()}

    @staticmethod
    def _normalize_vocab(vocab: Dict[str, float]) -> Dict[str, float]:
        normalized: Dict[str, float] = {}
        for word, weight in vocab.items():
            key = _normalize(word)
            normalized[key] = normalized.get(key, 0.0) + weight
        return normalized

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps(self._vocab), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    def _tokens(self, text: str) -> List[str]:
        return [_normalize(w) for w in _WORD.findall((text or "").lower())]

    def classify(self, query: str) -> Dict[str, Any]:
        tokens = self._tokens(query)
        if not tokens:
            return {"domain": "general", "repository": DOMAIN_TO_REPO.get("general", "nexus"),
                   "confidence": 0.0, "scores": {}}

        # FIX-DC-07: Check INTENT_SIGNALS first — strong intent phrases override keyword scoring.
        q_lower = (query or "").lower()
        intent_domain: Optional[str] = None
        for phrase, domain in INTENT_SIGNALS.items():
            if phrase in q_lower:
                intent_domain = domain
                break  # first (longest) match wins; dict is ordered by specificity

        with self._lock:
            scores = {domain: sum(vocab.get(tok, 0.0) for tok in tokens)
                     for domain, vocab in self._vocab.items()}
            scores = {d: s for d, s in scores.items() if s > 0}

        # FIX-DC-07: Boost intent-matched domain score by INTENT_BOOST so it dominates.
        if intent_domain:
            base = scores.get(intent_domain, 0.5)
            scores[intent_domain] = base * INTENT_BOOST if base > 0 else INTENT_BOOST

        if not scores:
            return {"domain": "general", "repository": DOMAIN_TO_REPO.get("general", "nexus"),
                   "confidence": 0.3, "scores": {}}
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_domain = ranked[0][0]
        exp = {d: math.exp(min(s, 500)) for d, s in ranked}  # cap to avoid overflow
        total = sum(exp.values())
        confidence = exp[top_domain] / total if total else 0.0
        return {"domain": top_domain, "repository": DOMAIN_TO_REPO.get(top_domain, "nexus"),
                "confidence": round(confidence, 4),
                "scores": {d: round(s, 2) for d, s in ranked[:5]},
                "intent_signal": intent_domain}

    def relevance_filter(self, query: str, memories: List[Any]) -> List[Any]:
        """FIX-DC-08 (updated Phase 5c): Filter Chronicle memories by topic relevance.

        BUG C ROOT CAUSE: The old filter used ALL tokens including stop words
        ("a", "on", "the", "is"). "train a new strategy on AAPL" shared stop-word
        tokens with "what is an animal" memories → false 50% overlap → 8 spurious
        "relevant" memories → Chronicle fast path fired for unrelated queries.

        Fix:
          1. Strip STOP_WORDS before computing overlap (meaningful tokens only).
          2. Require >= 30% overlap on meaningful tokens (raised from 10%).
          3. Require at least 1 meaningful token in common (guards against
             empty-after-stop-word-removal edge cases).

        Constitutional law: Book II Principle I Memory First — inject only
        relevant memories, not all recent ones.
        """
        if not memories or not query:
            return memories
        # Strip stop words from query tokens
        all_query_tokens = set(self._tokens(query))
        query_tokens = all_query_tokens - STOP_WORDS
        if not query_tokens:
            # All query words are stop words — fall back to unfiltered (edge case)
            return memories
        filtered = []
        for mem in memories:
            if isinstance(mem, str):
                text = mem
            elif isinstance(mem, dict):
                text = mem.get("content") or mem.get("text") or mem.get("summary") or str(mem)
            else:
                text = str(mem)
            # Strip stop words from memory tokens too
            all_mem_tokens = set(self._tokens(text))
            mem_tokens = all_mem_tokens - STOP_WORDS
            if not mem_tokens:
                continue
            common = query_tokens & mem_tokens
            # Require at least 1 meaningful token AND >= 30% overlap
            if len(common) >= 1 and len(common) / len(query_tokens) >= 0.30:
                filtered.append(mem)
        return filtered

    def reinforce(self, query: str, correct_domain: str, weight: float = 0.5) -> None:
        if correct_domain not in self._vocab:
            self._vocab[correct_domain] = {}
        with self._lock:
            for tok in set(self._tokens(query)):
                if len(tok) >= 2:
                    self._vocab[correct_domain][tok] = self._vocab[correct_domain].get(tok, 0.0) + weight
            self._persist()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"domains": list(self._vocab.keys()),
                   "vocab_sizes": {d: len(v) for d, v in self._vocab.items()}}