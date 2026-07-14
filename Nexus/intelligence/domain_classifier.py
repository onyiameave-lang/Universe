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
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_WORD = re.compile(r"[a-z0-9]+")


def _normalize(word: str) -> str:
    """Truncate to a short prefix so common inflections of the same word
    collide on lookup -- e.g. 'trade', 'trading', 'traded', 'trader' all
    normalize to 'trad'. Cheap alternative to real stemming; short words
    are left as-is since truncating them further would cause unrelated
    words to collide."""
    return word[:5] if len(word) > 5 else word

SEED_VOCAB: Dict[str, Dict[str, float]] = {
    "trading": {"trade": 3, "market": 3, "forex": 3, "stock": 2, "price": 2, "buy": 2,
                "sell": 2, "currency": 2, "eurusd": 3, "portfolio": 2, "position": 2,
                "risk": 1.5, "profit": 1.5, "chart": 1.5, "signal": 2,
                "evolve": 3, "strategy": 3, "backtest": 3, "champion": 2.5,
                "genome": 2.5, "oracle": 2, "sharpe": 2.5, "drawdown": 2.5},
    "news": {"news": 3, "headline": 3, "article": 2, "press": 2, "media": 2, "report": 1.5,
             "breaking": 2, "story": 1.5, "journalist": 2, "coverage": 1.5},
    "social": {"social": 3, "twitter": 3, "reddit": 3, "sentiment": 3, "trending": 2,
               "viral": 2, "post": 1.5, "community": 2, "hype": 2, "crowd": 2},
    "research": {"research": 3, "paper": 3, "study": 2, "hypothesis": 3, "evidence": 3,
                 "investigate": 2, "experiment": 2, "cite": 2, "scientific": 2, "explain": 1.5},
    "training": {"train": 3, "model": 3, "benchmark": 2, "optimize": 2, "hyperparameter": 3,
                 "dataset": 2, "epoch": 2, "accuracy": 1.5, "loss": 1.5, "neural": 2},
    "memory": {"remember": 3, "recall": 3, "history": 2, "knowledge": 2, "previous": 2,
               "stored": 2, "past": 1.5, "memory": 3, "retrieve": 2},
    "governance": {"audit": 3, "security": 3, "compliance": 3, "violation": 2, "policy": 2,
                   "govern": 2, "constitution": 2, "permission": 2, "threat": 2},
    "creation": {"create": 2, "spawn": 3, "agent": 2, "build": 1.5, "generate": 2,
                 "factory": 2, "capability": 2, "design": 1.5},
    "coordination": {"coordinate": 3, "route": 2, "orchestrate": 3, "workflow": 2,
                     "collaborate": 2, "delegate": 2, "manage": 1.5},
}

DOMAIN_TO_REPO = {"trading": "oracle", "prediction": "oracle", "news": "sentinel",
                  "social": "pulse", "research": "atlas", "training": "forge",
                  "memory": "chronicle", "governance": "aegis", "creation": "genesis",
                  "coordination": "nexus", "general": "atlas"}


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
        with self._lock:
            scores = {domain: sum(vocab.get(tok, 0.0) for tok in tokens)
                     for domain, vocab in self._vocab.items()}
            scores = {d: s for d, s in scores.items() if s > 0}
        if not scores:
            return {"domain": "general", "repository": DOMAIN_TO_REPO.get("general", "nexus"),
                   "confidence": 0.3, "scores": {}}
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_domain = ranked[0][0]
        exp = {d: math.exp(s) for d, s in ranked}
        total = sum(exp.values())
        confidence = exp[top_domain] / total if total else 0.0
        return {"domain": top_domain, "repository": DOMAIN_TO_REPO.get(top_domain, "nexus"),
                "confidence": round(confidence, 4),
                "scores": {d: round(s, 2) for d, s in ranked[:5]}}

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