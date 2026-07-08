"""
Chronicle.core.knowledge_graph
==============================
Typed, weighted knowledge graph connecting memories. (Book II Part III Ch VII
Knowledge Relationships; Page 52.)

Real directed multigraph with BFS relationship traversal for relationship-based
retrieval. Persists to disk. Constitutional relationship vocabulary.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

RELATION_TYPES = {
    "cause_effect", "problem_solution", "strategy_performance", "research_evidence",
    "failure_improvement", "agent_contribution", "repository_architecture",
    "dataset_model", "event_outcome", "feedback_optimization", "supersedes",
    "related", "contradicts", "derived_from",
}


class KnowledgeGraph:
    def __init__(self, storage_dir: str = "memory_store"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "knowledge_graph.json"
        self._lock = threading.RLock()
        self._edges: Dict[str, List[Dict[str, Any]]] = {}
        self._reverse: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._edges = data.get("edges", {})
            self._reverse = data.get("reverse", {})
        except Exception:
            self._edges, self._reverse = {}, {}

    def _persist(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"edges": self._edges, "reverse": self._reverse}), encoding="utf-8")
        tmp.replace(self._path)

    def connect(self, from_id: str, to_id: str, relation: str = "related",
                weight: float = 1.0) -> Dict[str, Any]:
        if relation not in RELATION_TYPES:
            relation = "related"
        edge = {"to": to_id, "type": relation, "weight": float(weight)}
        with self._lock:
            self._edges.setdefault(from_id, [])
            if not any(e["to"] == to_id and e["type"] == relation for e in self._edges[from_id]):
                self._edges[from_id].append(edge)
            self._reverse.setdefault(to_id, [])
            if not any(e["to"] == from_id and e["type"] == relation for e in self._reverse[to_id]):
                self._reverse[to_id].append({"to": from_id, "type": relation, "weight": float(weight)})
            self._persist()
        return edge

    def neighbors(self, memory_id: str, relation: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            edges = self._edges.get(memory_id, [])
            return [e for e in edges if e["type"] == relation] if relation else list(edges)

    def related(self, memory_id: str, max_depth: int = 2, limit: int = 20) -> List[Tuple[str, float, int]]:
        with self._lock:
            visited: Set[str] = {memory_id}
            results: List[Tuple[str, float, int]] = []
            queue: deque = deque([(memory_id, 1.0, 0)])
            while queue:
                node, acc, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for edge in self._edges.get(node, []):
                    nxt = edge["to"]
                    w = acc * edge["weight"]
                    if nxt not in visited:
                        visited.add(nxt)
                        results.append((nxt, round(w, 4), depth + 1))
                        queue.append((nxt, w, depth + 1))
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]

    def find_contradictions(self) -> List[Tuple[str, str]]:
        with self._lock:
            return [(src, e["to"]) for src, edges in self._edges.items()
                   for e in edges if e["type"] == "contradicts"]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            edge_count = sum(len(v) for v in self._edges.values())
            by_type: Dict[str, int] = {}
            for edges in self._edges.values():
                for e in edges:
                    by_type[e["type"]] = by_type.get(e["type"], 0) + 1
            return {"nodes": len(set(self._edges) | set(self._reverse)),
                   "edges": edge_count, "by_relation": by_type}
