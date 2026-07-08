"""
Nexus.intelligence.collaboration_graph
======================================
The learnable collaboration graph: Nexus discovers, from real outcomes, which
specialists should feed which, and in what order, rather than relying on a
fixed table. (Book I Part IV Article IX Trial and Error / Continual Learning;
Article XII Self-Evaluation; Book II Part II Ch XIII Collaboration Sessions.)

Why this makes Nexus the "smartest": every other agent reasons about its OWN
problem. Nexus reasons about HOW THE WHOLE CIVILIZATION SHOULD COOPERATE. It
learns:
  * DEPENDENCIES  which domain's output improves which other domain's result
                  (e.g. does news-before-trading actually help?), scored by the
                  quality of the multi-agent answers that used that ordering.
  * AFFINITIES    which specialists tend to be needed together for a kind of
                  query, so decomposition can pre-suggest collaborators.
  * ORDER QUALITY the Wilson-lower-bound success rate of each ordering, so a
                  proven pipeline is preferred but new orderings still get
                  explored (exploit + explore, like shared.reasoning).

Seed hints bootstrap it; real outcomes then refine it. Persists to disk.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


# Seed dependency hints (dep -> dependents). Learning refines these; they are
# NOT hard rules, just a reasonable prior so day-one behavior is sensible.
SEED_DEPENDENCIES = {
    "news": ["trading", "prediction", "social"],
    "social": ["trading", "prediction"],
    "research": ["trading", "prediction", "training", "creation"],
    "memory": ["trading", "prediction", "governance", "creation"],
    "trading": [],
    "governance": [],
}


class CollaborationGraph:
    """Learns dependencies, affinities, and ordering quality from real outcomes."""

    def __init__(self, storage_dir: str = "memory", exploration: float = 0.3):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "collaboration_graph.json"
        self._lock = threading.RLock()
        self.exploration = exploration
        # dependency edge stats: (dep, dependent) -> {"help": int, "total": int}
        self._dep_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"help": 0, "total": 0})
        # affinity: frozenset(domains) -> count of times used together with success
        self._affinity: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        # ordering quality: tuple(order) -> {"success": int, "total": int}
        self._order_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        self._load()

    def _key(self, dep: str, dependent: str) -> str:
        return f"{dep}->{dependent}"

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for k, v in data.get("dep_stats", {}).items():
                    self._dep_stats[k] = v
                for k, v in data.get("affinity", {}).items():
                    self._affinity[k] = v
                for k, v in data.get("order_stats", {}).items():
                    self._order_stats[k] = v
                return
            except Exception:
                pass
        # bootstrap dependency stats from seed hints (weak prior: 1 helpful obs)
        for dep, dependents in SEED_DEPENDENCIES.items():
            for d in dependents:
                self._dep_stats[self._key(dep, d)] = {"help": 1, "total": 1}

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps({
                "dep_stats": dict(self._dep_stats),
                "affinity": dict(self._affinity),
                "order_stats": dict(self._order_stats),
            }), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    # ---- querying the graph (used during planning) ----

    def dependencies_for(self, domain: str, present: List[str]) -> List[str]:
        """
        Which present domains should run BEFORE `domain`, per learned evidence.
        A dependency is honored if its helpfulness lower-bound beats a threshold,
        or (exploration) occasionally even when unproven, so new links surface.
        """
        deps = []
        with self._lock:
            for other in present:
                if other == domain:
                    continue
                stats = self._dep_stats.get(self._key(other, domain))
                if not stats:
                    continue
                lb = wilson_lower_bound(stats["help"], stats["total"])
                explore = self.exploration * math.sqrt(
                    math.log(stats["total"] + 2) / (stats["total"] + 1))
                if lb + explore >= 0.35:
                    deps.append((other, lb + explore))
        deps.sort(key=lambda x: x[1], reverse=True)
        return [d for d, _ in deps]

    def suggest_collaborators(self, domains: List[str]) -> List[str]:
        """Given some detected domains, suggest others historically needed with them."""
        suggestions: Dict[str, float] = defaultdict(float)
        with self._lock:
            for key, stats in self._affinity.items():
                members = set(key.split("+"))
                if members & set(domains) and stats["total"] > 0:
                    rate = stats["success"] / stats["total"]
                    for m in members - set(domains):
                        suggestions[m] += rate
        return [d for d, _ in sorted(suggestions.items(), key=lambda x: x[1], reverse=True)[:2]]

    def order_quality(self, ordering: List[str]) -> float:
        key = ">".join(ordering)
        with self._lock:
            s = self._order_stats.get(key, {"success": 0, "total": 0})
            return wilson_lower_bound(s["success"], s["total"])

    # ---- learning from real multi-agent sessions ----

    def record_session(self, ordering: List[str], per_agent_success: Dict[str, bool],
                       overall_success: bool) -> None:
        """
        Learn from a completed multi-agent session:
          * ordering quality,
          * affinity of the domain set,
          * dependency helpfulness (did running X before Y coincide with Y
            succeeding?).
        """
        with self._lock:
            # ordering quality
            okey = ">".join(ordering)
            self._order_stats[okey]["total"] += 1
            if overall_success:
                self._order_stats[okey]["success"] += 1

            # affinity of the whole set
            akey = "+".join(sorted(ordering))
            self._affinity[akey]["total"] += 1
            if overall_success:
                self._affinity[akey]["success"] += 1

            # dependency helpfulness: for each pair where dep ran before dependent,
            # count it as "helpful" if the dependent succeeded.
            for i, dep in enumerate(ordering):
                for dependent in ordering[i + 1:]:
                    k = self._key(dep, dependent)
                    self._dep_stats[k]["total"] += 1
                    if per_agent_success.get(dependent, False):
                        self._dep_stats[k]["help"] += 1
            self._persist()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            top_deps = sorted(
                ({"edge": k, "helpfulness": round(wilson_lower_bound(v["help"], v["total"]), 3),
                  "observations": v["total"]} for k, v in self._dep_stats.items()),
                key=lambda x: x["helpfulness"], reverse=True)[:8]
            top_orders = sorted(
                ({"order": k, "quality": round(wilson_lower_bound(v["success"], v["total"]), 3),
                  "runs": v["total"]} for k, v in self._order_stats.items() if v["total"] > 0),
                key=lambda x: x["quality"], reverse=True)[:5]
            return {"learned_dependencies": top_deps, "best_orderings": top_orders,
                   "affinity_sets": len(self._affinity)}
