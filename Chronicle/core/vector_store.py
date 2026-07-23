"""
Chronicle.core.vector_store
===========================
Persistent vector store for memory records. (Book II Part III; Book IV Ch VII:
infrastructure is replaceable.)

Real implementation: records + embeddings persist to disk as JSON (atomic
writes), semantic search via numpy when available else correct pure-Python,
constitutional filters (domain, pillar, confidence), and archive-not-delete
(nothing dies without record, Ch VIII). Swap for Chroma/FAISS/pgvector later
without touching callers.

FIX-VS-V8-01: Windows-safe atomic write (os.replace with shutil.move fallback).
FIX-HYBRID-01: hybrid_retriever observer — notified on add/update so SBERT
               cache stays consistent with the live record corpus.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.embeddings import cosine_similarity  # type: ignore
from core.memory_record import MemoryRecord      # type: ignore

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:
    _np = None
    _HAS_NUMPY = False


class VectorStore:
    def __init__(self, storage_dir: str = "memory_store"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "records.json"
        self._lock = threading.RLock()
        self._records: Dict[str, MemoryRecord] = {}
        self._matrix = None
        self._matrix_ids: List[str] = []
        self._dirty = True
        # FIX-HYBRID-01: optional callback invoked after add/update so
        # RetrievalEngine can refresh the HybridRetriever corpus.
        # Set by RetrievalEngine after construction; never called in __init__.
        self._hybrid_notify: Optional[Callable[[], None]] = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw:
                rec = MemoryRecord.from_dict(item)
                self._records[rec.memory_id] = rec
            self._dirty = True
        except Exception:
            if self._path.exists():
                self._path.rename(self.storage_dir / "records.corrupt.json")

    def _persist(self) -> None:
        """FIX-VS-V8-01: Windows-safe atomic write with retry fallback."""
        data = [r.to_dict(include_embedding=True) for r in self._records.values()]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        if sys.platform == "win32":
            import shutil
            for attempt in range(3):
                try:
                    os.replace(str(tmp), str(self._path))
                    return
                except (PermissionError, OSError):
                    if attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
                    else:
                        try:
                            shutil.move(str(tmp), str(self._path))
                        except Exception:
                            try:
                                tmp.unlink(missing_ok=True)
                            except Exception:
                                pass
        else:
            tmp.replace(self._path)

    def _notify_hybrid(self) -> None:
        """FIX-HYBRID-01: Notify HybridRetriever that records changed."""
        if self._hybrid_notify is not None:
            try:
                self._hybrid_notify()
            except Exception:
                pass

    def add(self, record: MemoryRecord) -> MemoryRecord:
        with self._lock:
            record.compute_confidence()
            self._records[record.memory_id] = record
            self._dirty = True
            self._persist()
        self._notify_hybrid()  # FIX-HYBRID-01: outside lock to avoid deadlock
        return record

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            return self._records.get(memory_id)

    def update(self, record: MemoryRecord) -> None:
        with self._lock:
            record.version += 1
            record.compute_confidence()
            self._records[record.memory_id] = record
            self._dirty = True
            self._persist()
        self._notify_hybrid()  # FIX-HYBRID-01: outside lock to avoid deadlock

    def archive(self, memory_id: str) -> bool:
        with self._lock:
            rec = self._records.get(memory_id)
            if not rec:
                return False
            rec.archived = True
            rec.updated_at = time.time()
            self._dirty = True
            self._persist()
        self._notify_hybrid()  # FIX-HYBRID-01
        return True

    def all(self, include_archived: bool = False) -> List[MemoryRecord]:
        with self._lock:
            return [r for r in self._records.values() if include_archived or not r.archived]

    def _rebuild_matrix(self) -> None:
        active = [(mid, r) for mid, r in self._records.items() if not r.archived and r.embedding]
        self._matrix_ids = [mid for mid, _ in active]
        self._matrix = _np.array([r.embedding for _, r in active], dtype=float) if (_HAS_NUMPY and active) else None
        self._dirty = False

    def search(self, query_embedding: List[float], top_k: int = 5,
               domain: Optional[str] = None, pillar: Optional[str] = None,
               min_confidence: float = 0.0) -> List[Tuple[MemoryRecord, float]]:
        with self._lock:
            if self._dirty:
                self._rebuild_matrix()
            candidates = [(mid, self._records[mid]) for mid in self._matrix_ids
                         if (domain is None or self._records[mid].domain == domain)
                         and (pillar is None or self._records[mid].pillar.value == pillar)
                         and self._records[mid].confidence >= min_confidence]
            if not candidates:
                return []
            scored: List[Tuple[MemoryRecord, float]] = []
            if _HAS_NUMPY and self._matrix is not None:
                q = _np.array(query_embedding, dtype=float)
                qn = q / (_np.linalg.norm(q) or 1.0)
                id_to_row = {mid: i for i, mid in enumerate(self._matrix_ids)}
                for mid, rec in candidates:
                    row = self._matrix[id_to_row[mid]]
                    rn = row / (_np.linalg.norm(row) or 1.0)
                    scored.append((rec, float(qn.dot(rn))))
            else:
                for _, rec in candidates:
                    scored.append((rec, cosine_similarity(query_embedding, rec.embedding)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def keyword_search(self, terms: List[str], top_k: int = 5) -> List[MemoryRecord]:
        terms_low = [t.lower() for t in terms]
        results = []
        for rec in self.all():
            text = f"{rec.summary} {rec.content} {' '.join(rec.tags)}".lower()
            score = sum(text.count(t) for t in terms_low)
            if score > 0:
                results.append((rec, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return [r for r, _ in results[:top_k]]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_pillar: Dict[str, int] = {}
            by_domain: Dict[str, int] = {}
            for r in self._records.values():
                if r.archived:
                    continue
                by_pillar[r.pillar.value] = by_pillar.get(r.pillar.value, 0) + 1
                by_domain[r.domain] = by_domain.get(r.domain, 0) + 1
            active = [r for r in self._records.values() if not r.archived]
            avg_conf = sum(r.confidence for r in active) / len(active) if active else 0.0
            return {"total_records": len(self._records), "active": len(active),
                   "archived": sum(1 for r in self._records.values() if r.archived),
                   "by_pillar": by_pillar, "by_domain": by_domain,
                   "avg_confidence": round(avg_conf, 3),
                   "backend": "numpy" if _HAS_NUMPY else "pure-python"}