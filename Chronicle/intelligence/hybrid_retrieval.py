"""
Chronicle.intelligence.hybrid_retrieval
========================================
SBERT + BM25 hybrid relevance scoring for Chronicle's retrieval layer.

Replaces pure cosine-similarity ranking with a two-signal hybrid:

  final_score = 0.6 * sbert_cosine_similarity + 0.4 * bm25_score

  * SBERT (sentence-transformers, all-MiniLM-L6-v2): semantic meaning.
    "Apple phone" matches "iPhone"; "who invented telephone" matches
    "Alexander Graham Bell biography". Lazy-loaded on first use; falls back
    to the existing hash-embedding cosine if sentence-transformers is absent.

  * BM25 (stdlib only, no rank_bm25): exact keyword precision.
    "what is BM25" ranks BM25 records highest. Standard Okapi BM25 formula
    with k1=1.5, b=0.75.

  * Recency boost: multiply by 1.0 + 0.1 * recency_factor (decays over 30d).
  * Domain tag boost: multiply by 1.2 when memory domain matches query domain.

Constitutional law: Book II Part III Ch V Retrieval — five stages before
generation; Ch VI Memory Evolution — knowledge refined by verified use.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("chronicle.retrieval")

# ---------------------------------------------------------------------------
# Optional SBERT import — graceful fallback to hash-embedding cosine
# ---------------------------------------------------------------------------
try:
    from sentence_transformers import SentenceTransformer as _ST  # type: ignore
    _SBERT_IMPORTABLE = True
except ImportError:
    _ST = None  # type: ignore
    _SBERT_IMPORTABLE = False

# ---------------------------------------------------------------------------
# BM25 constants (Okapi BM25, standard params)
# ---------------------------------------------------------------------------
_BM25_K1: float = 1.5
_BM25_B: float = 0.75
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Lowercase word-tokenizer — same as embeddings.py for consistency."""
    return _TOKEN_RE.findall((text or "").lower())


# ---------------------------------------------------------------------------
# BM25Index — pure stdlib, built from a corpus of documents
# ---------------------------------------------------------------------------

class BM25Index:
    """
    Okapi BM25 index over a list of text documents.

    score(query, doc_id) = Σ_t  IDF(t) * tf_norm(t, doc_id)

    where:
      IDF(t)         = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
      tf_norm(t, d)  = tf(t,d) * (k1+1) / (tf(t,d) + k1*(1-b + b*dl/avgdl))
    """

    def __init__(self, documents: List[str], doc_ids: List[str]):
        assert len(documents) == len(doc_ids), "documents and doc_ids must be same length"
        self._doc_ids = doc_ids
        self._id_to_idx: Dict[str, int] = {did: i for i, did in enumerate(doc_ids)}
        self._N = len(documents)

        # Tokenize all documents
        tokenized: List[List[str]] = [_tokenize(d) for d in documents]
        self._dl: List[int] = [len(t) for t in tokenized]
        self._avgdl: float = sum(self._dl) / self._N if self._N else 1.0

        # Term frequency per document: {term: {doc_idx: count}}
        self._tf: Dict[str, Dict[int, int]] = {}
        for idx, tokens in enumerate(tokenized):
            for tok in tokens:
                if tok not in self._tf:
                    self._tf[tok] = {}
                self._tf[tok][idx] = self._tf[tok].get(idx, 0) + 1

        # Document frequency: {term: count_of_docs_containing_term}
        self._df: Dict[str, int] = {term: len(docs) for term, docs in self._tf.items()}

        # Precompute IDF for all terms
        self._idf: Dict[str, float] = {}
        for term, df in self._df.items():
            self._idf[term] = math.log((self._N - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query: str, doc_id: str) -> float:
        """BM25 score for a single (query, doc_id) pair. Returns 0.0 if doc not found."""
        idx = self._id_to_idx.get(doc_id)
        if idx is None:
            return 0.0
        dl = self._dl[idx]
        tokens = _tokenize(query)
        total = 0.0
        for tok in tokens:
            if tok not in self._tf:
                continue
            tf = self._tf[tok].get(idx, 0)
            if tf == 0:
                continue
            idf = self._idf.get(tok, 0.0)
            tf_norm = tf * (_BM25_K1 + 1.0) / (
                tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * dl / self._avgdl)
            )
            total += idf * tf_norm
        return total

    def max_possible_score(self, query: str) -> float:
        """
        Upper-bound BM25 score for normalization: score a document that contains
        every query term exactly once, with average document length.
        """
        tokens = _tokenize(query)
        total = 0.0
        for tok in set(tokens):
            idf = self._idf.get(tok, math.log((self._N + 0.5) / 1.5 + 1.0))
            tf = 1
            tf_norm = tf * (_BM25_K1 + 1.0) / (
                tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * 1.0)
            )
            total += idf * tf_norm
        return total if total > 0 else 1.0


# ---------------------------------------------------------------------------
# HybridRetriever — the main class
# ---------------------------------------------------------------------------

class HybridRetriever:
    """
    Hybrid SBERT + BM25 retriever over a list of MemoryRecord-like dicts.

    Usage:
        retriever = HybridRetriever(records, use_sbert=True)
        top = retriever.retrieve(query="who was aristotle", top_k=5)
        # top is a list of (record_dict, score) tuples, sorted descending

    Thread-safe: a single RLock guards SBERT model loading and cache updates.
    """

    _SBERT_MODEL_NAME = "all-MiniLM-L6-v2"

    def __init__(self, records: List[Dict[str, Any]], use_sbert: bool = True):
        self._lock = threading.RLock()
        self._records: List[Dict[str, Any]] = []
        self._bm25: Optional[BM25Index] = None
        self._sbert_model = None          # lazy-loaded
        self._sbert_cache: Dict[str, List[float]] = {}   # memory_id -> embedding
        self._use_sbert = use_sbert and _SBERT_IMPORTABLE
        self._sbert_loaded = False
        self._sbert_load_attempted = False

        # Log which mode we're starting in
        if self._use_sbert:
            log.info("[chronicle.retrieval] HybridRetriever: SBERT available — "
                     "will lazy-load %s on first query", self._SBERT_MODEL_NAME)
        else:
            if use_sbert and not _SBERT_IMPORTABLE:
                log.warning("[chronicle.retrieval] HybridRetriever: sentence-transformers "
                            "not installed — using BM25-only mode. "
                            "Install with: pip install sentence-transformers>=2.2.0")
            else:
                log.info("[chronicle.retrieval] HybridRetriever: BM25-only mode (SBERT disabled)")

        self.update_records(records)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_records(self, records: List[Dict[str, Any]]) -> None:
        """
        Replace the record corpus and rebuild BM25 index.
        Invalidates the SBERT embedding cache for records that changed.
        Call this whenever new memories are added to the store.
        """
        with self._lock:
            self._records = list(records)
            self._rebuild_bm25()
            # Invalidate SBERT cache for any record_id not in new corpus
            new_ids = {r.get("memory_id", "") for r in records}
            stale = [mid for mid in list(self._sbert_cache) if mid not in new_ids]
            for mid in stale:
                del self._sbert_cache[mid]

    def score(self, query: str, record: Dict[str, Any],
              query_domain: Optional[str] = None) -> float:
        """
        Compute hybrid relevance score for a single (query, record) pair.
        Returns a float in approximately [0, 1].
        """
        with self._lock:
            return self._score_one(query, record, query_domain)

    def retrieve(self, query: str, top_k: int = 5,
                 query_domain: Optional[str] = None) -> List[Tuple[Dict[str, Any], float]]:
        """
        Score all records against the query and return the top_k most relevant,
        sorted by score descending.

        Returns: list of (record_dict, score) tuples.
        """
        with self._lock:
            # Lazy-load SBERT on first real query
            if self._use_sbert and not self._sbert_load_attempted:
                self._load_sbert()

            if not self._records:
                return []

            # Compute SBERT query embedding once (reused for all records)
            q_sbert_emb: Optional[List[float]] = None
            if self._sbert_loaded and self._sbert_model is not None:
                try:
                    q_sbert_emb = self._encode_sbert(query)
                except Exception as exc:
                    log.warning("[chronicle.retrieval] SBERT query encode failed: %s", exc)

            # BM25 normalization factor for this query
            bm25_max = self._bm25.max_possible_score(query) if self._bm25 else 1.0

            scored: List[Tuple[Dict[str, Any], float]] = []
            for rec in self._records:
                s = self._score_one(query, rec, query_domain,
                                    q_sbert_emb=q_sbert_emb, bm25_max=bm25_max)
                scored.append((rec, s))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    # ------------------------------------------------------------------
    # Internal scoring
    # ------------------------------------------------------------------

    def _score_one(self, query: str, record: Dict[str, Any],
                   query_domain: Optional[str] = None,
                   q_sbert_emb: Optional[List[float]] = None,
                   bm25_max: Optional[float] = None) -> float:
        """
        Compute the hybrid score for one record. Caller must hold self._lock.

        hybrid = 0.6 * sbert_sim + 0.4 * bm25_norm
        final  = hybrid * recency_boost * domain_boost
        """
        mid = record.get("memory_id", "")

        # ---- SBERT component ----
        sbert_sim = 0.0
        if self._sbert_loaded and self._sbert_model is not None:
            try:
                if q_sbert_emb is None:
                    q_sbert_emb = self._encode_sbert(query)
                rec_emb = self._get_record_sbert_emb(record)
                if rec_emb and q_sbert_emb:
                    sbert_sim = _cosine(q_sbert_emb, rec_emb)
                    sbert_sim = max(0.0, sbert_sim)   # clamp negatives to 0
            except Exception as exc:
                log.debug("[chronicle.retrieval] SBERT score error for %s: %s", mid, exc)
        else:
            # Fallback: use the stored hash-embedding cosine from the record
            stored_emb = record.get("embedding") or []
            if stored_emb:
                # We need a query embedding too — use the stored one from the
                # record's own embedder (already computed by Chronicle on store)
                # We can't re-encode here without the embedder, so sbert_sim stays 0
                # and BM25 carries the full weight in fallback mode.
                pass

        # ---- BM25 component ----
        bm25_raw = 0.0
        if self._bm25 is not None and mid:
            bm25_raw = self._bm25.score(query, mid)
        if bm25_max is None:
            bm25_max = self._bm25.max_possible_score(query) if self._bm25 else 1.0
        bm25_norm = min(bm25_raw / bm25_max, 1.0) if bm25_max > 0 else 0.0

        # ---- Hybrid combination ----
        if self._sbert_loaded and self._sbert_model is not None:
            hybrid = 0.6 * sbert_sim + 0.4 * bm25_norm
        else:
            # SBERT unavailable: BM25 carries 60%, stored cosine carries 40%
            stored_emb = record.get("embedding") or []
            stored_cos = 0.0
            # We can't encode the query without the embedder here, so we rely
            # on the score field already computed by RetrievalEngine (passed in
            # as record["score"] when called from the wired retrieval.py).
            stored_cos = float(record.get("score", 0.0))
            hybrid = 0.6 * stored_cos + 0.4 * bm25_norm

        # ---- Recency boost: 1.0 + 0.1 * recency_factor ----
        # recency_factor decays from 1.0 (just stored) to ~0 over 30 days
        updated_at = float(record.get("updated_at", 0.0))
        if updated_at > 0:
            age_days = max((time.time() - updated_at) / 86400.0, 0.0)
            recency_factor = 1.0 / (1.0 + age_days / 30.0)
        else:
            recency_factor = 0.5
        recency_boost = 1.0 + 0.1 * recency_factor

        # ---- Domain boost: 1.2 if memory domain matches query domain ----
        domain_boost = 1.0
        if query_domain and record.get("domain") == query_domain:
            domain_boost = 1.2

        final = hybrid * recency_boost * domain_boost
        return round(final, 6)

    # ------------------------------------------------------------------
    # BM25 index construction
    # ------------------------------------------------------------------

    def _rebuild_bm25(self) -> None:
        """Build BM25 index from current records. Caller must hold self._lock."""
        if not self._records:
            self._bm25 = None
            return
        doc_ids: List[str] = []
        documents: List[str] = []
        for rec in self._records:
            mid = rec.get("memory_id", "")
            if not mid:
                continue
            # Concatenate all text fields for BM25 indexing
            text = " ".join(filter(None, [
                str(rec.get("summary", "") or ""),
                str(rec.get("content", "") or ""),
                " ".join(rec.get("tags", []) or []),
                str(rec.get("lesson", "") or ""),
                str(rec.get("domain", "") or ""),
            ]))
            doc_ids.append(mid)
            documents.append(text)
        self._bm25 = BM25Index(documents, doc_ids) if doc_ids else None

    # ------------------------------------------------------------------
    # SBERT lazy loading and encoding
    # ------------------------------------------------------------------

    def _load_sbert(self) -> None:
        """Lazy-load the SBERT model. Caller must hold self._lock."""
        self._sbert_load_attempted = True
        if not _SBERT_IMPORTABLE or _ST is None:
            self._sbert_loaded = False
            log.warning("[chronicle.retrieval] Using BM25-only scorer "
                        "(sentence-transformers not installed)")
            return
        try:
            t0 = time.monotonic()
            self._sbert_model = _ST(self._SBERT_MODEL_NAME)
            elapsed = time.monotonic() - t0
            self._sbert_loaded = True
            log.info("[chronicle.retrieval] Using hybrid SBERT+BM25 scorer "
                     "(model=%s loaded in %.1fs)", self._SBERT_MODEL_NAME, elapsed)
        except Exception as exc:
            self._sbert_loaded = False
            self._sbert_model = None
            log.warning("[chronicle.retrieval] SBERT load failed (%s) — "
                        "falling back to BM25-only scorer", exc)

    def _encode_sbert(self, text: str) -> List[float]:
        """Encode text with SBERT. Caller must hold self._lock."""
        vec = self._sbert_model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]

    def _get_record_sbert_emb(self, record: Dict[str, Any]) -> Optional[List[float]]:
        """
        Get (or compute and cache) the SBERT embedding for a record.
        Uses summary + content as the text to embed.
        Caller must hold self._lock.
        """
        mid = record.get("memory_id", "")
        if mid and mid in self._sbert_cache:
            return self._sbert_cache[mid]
        text = " ".join(filter(None, [
            str(record.get("summary", "") or ""),
            str(record.get("content", "") or "")[:500],  # cap at 500 chars
        ]))
        if not text.strip():
            return None
        try:
            emb = self._encode_sbert(text)
            if mid:
                self._sbert_cache[mid] = emb
            return emb
        except Exception as exc:
            log.debug("[chronicle.retrieval] SBERT record encode failed for %s: %s", mid, exc)
            return None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": "sbert+bm25" if self._sbert_loaded else "bm25-only",
                "sbert_model": self._SBERT_MODEL_NAME if self._sbert_loaded else None,
                "sbert_cache_size": len(self._sbert_cache),
                "bm25_docs": len(self._records),
                "bm25_vocab": len(self._bm25._df) if self._bm25 else 0,
            }


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python, no numpy dependency)
# ---------------------------------------------------------------------------

def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
