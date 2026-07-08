"""
Chronicle.core.embeddings
=========================
Real text embedding for semantic memory. (Book II Part III Ch V: Retrieval.)

Two real backends, chosen automatically, never faked:
  1. sentence-transformers (all-MiniLM-L6-v2, 384-d) if installed -> true
     semantic embeddings.
  2. Otherwise a real, deterministic feature-hashing vectorizer (token +
     word-bigram + char-trigram, signed hashing, L2-normalized). Meaningful
     cosine similarity, zero dependencies, works offline.

Same interface either way, so the rest of Chronicle never cares which is active.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import List, Optional

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingModel:
    """Fixed-dimension, L2-normalized embeddings from text."""

    def __init__(self, dim: int = 384, prefer_transformer: Optional[bool] = None):
        self.dim = dim
        self._backend = "hashing"
        self._st_model = None
        if prefer_transformer is None:
            prefer_transformer = os.getenv("MEMORY_USE_REAL_EMBEDDINGS", "false").lower() in ("1", "true", "yes")
        if prefer_transformer:
            self._try_load_transformer()

    def _try_load_transformer(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            self._st_model = SentenceTransformer("all-MiniLM-L6-v2")
            self.dim = self._st_model.get_sentence_embedding_dimension()
            self._backend = "sentence-transformers"
        except Exception:
            self._st_model = None
            self._backend = "hashing"

    @property
    def backend(self) -> str:
        return self._backend

    def encode(self, text: str) -> List[float]:
        if self._st_model is not None:
            vec = self._st_model.encode(text, normalize_embeddings=True)
            return [float(x) for x in vec]
        return self._hash_encode(text)

    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        if self._st_model is not None:
            vecs = self._st_model.encode(texts, normalize_embeddings=True)
            return [[float(x) for x in v] for v in vecs]
        return [self._hash_encode(t) for t in texts]

    def _tokens(self, text: str) -> List[str]:
        text = (text or "").lower()
        words = _TOKEN_RE.findall(text)
        feats: List[str] = list(words)
        for i in range(len(words) - 1):
            feats.append(f"{words[i]}_{words[i + 1]}")
        joined = " ".join(words)
        for i in range(len(joined) - 2):
            tri = joined[i:i + 3]
            if tri.strip():
                feats.append(f"#{tri}")
        return feats

    def _hash_encode(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for feat in self._tokens(text):
            h = hashlib.md5(feat.encode("utf-8")).digest()
            bucket = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if (h[4] & 1) == 0 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_model: Optional[EmbeddingModel] = None


def get_embedding_model(dim: int = 384) -> EmbeddingModel:
    global _model
    if _model is None:
        _model = EmbeddingModel(dim=dim)
    return _model
