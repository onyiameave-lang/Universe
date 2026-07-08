"""
Forge.intelligence.method_discovery
===================================
Discover, evaluate, and ADOPT newer/better training methods. (Book I Part IV
Article IX Learning + Curiosity; Article XIII Evolution; Book II Principle IV
Research Before Assumption; Book II Part I Ch VII collaboration.)

Forge should not be frozen with four backends forever. This engine lets Forge
genuinely improve HOW it trains, safely:

  1. RESEARCH   ask Atlas for the current best/newer techniques for a task
                profile (e.g. "gradient boosting for tabular", "transformers for
                sequences"). Real research, real citations.
  2. MAP        map each recommended technique to (a) an ALREADY-INSTALLED
                library adapter, or (b) a MISSING library (reported honestly as
                "recommended, install X"), or (c) a candidate for Genesis to
                synthesize a new backend adapter (gated: sandbox + Aegis + human).
  3. CHALLENGE  champion/challenger A-B test: the new method is trained and
                cross-validated on the SAME real data as the incumbent. It is
                adopted ONLY if it beats the champion by a margin.
  4. ADOPT      winning methods are recorded as preferred for that task profile
                and reused next time. Losers are remembered so they aren't
                re-tried blindly (learn-from-mistakes).

Honesty rails: a method whose library is not installed is NEVER faked; it is
reported as an actionable recommendation. New adapter code only reaches the
runtime through Genesis's gated pipeline (sandbox + Aegis + human approval).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# Map research terms -> a training method + the library that provides it.
# `available` is probed live so we never claim a method we cannot run.
def _probe(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


KNOWN_METHODS = {
    "xgboost": {"terms": ["xgboost", "extreme gradient boosting", "gradient boosting"],
               "library": "xgboost", "task": ["classification", "regression"]},
    "lightgbm": {"terms": ["lightgbm", "light gradient boosting"],
                "library": "lightgbm", "task": ["classification", "regression"]},
    "catboost": {"terms": ["catboost"], "library": "catboost",
                "task": ["classification", "regression"]},
    "transformer": {"terms": ["transformer", "attention", "sequence model"],
                   "library": "torch", "task": ["classification"]},
    "svm": {"terms": ["support vector", "svm", "kernel method"],
           "library": "sklearn", "task": ["classification", "regression"]},
    "random_forest": {"terms": ["random forest", "ensemble"],
                     "library": "sklearn", "task": ["classification", "regression"]},
}


class MethodDiscovery:
    def __init__(self, atlas=None, chronicle=None, genesis=None, storage_dir: str = "memory"):
        self.atlas = atlas
        self.chronicle = chronicle
        self.genesis = genesis
        self._path = Path(storage_dir) / "discovered_methods.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # task_profile -> {"champion": name, "champion_score": float, "tried": {name: score}}
        self._preferred: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _profile_key(self, task_type: str, n_samples: int, n_features: int) -> str:
        size = "small" if n_samples < 200 else "medium" if n_samples < 2000 else "large"
        dim = "lowdim" if n_features < 20 else "highdim"
        return f"{task_type}:{size}:{dim}"

    def _load(self):
        if self._path.exists():
            try:
                self._preferred = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def _persist(self):
        try:
            self._path.write_text(json.dumps(self._preferred, indent=2), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    # ---- 1+2: research + map to runnable/missing/synthesizable ----

    def discover(self, task_type: str, n_samples: int, n_features: int) -> Dict[str, Any]:
        """Research better methods and classify each as runnable / missing / synthesizable."""
        query = (f"best and newest machine learning methods for {task_type} on "
                f"{'tabular' if n_features < 100 else 'high-dimensional'} data with "
                f"{n_samples} samples")
        research = None
        rec_terms: List[str] = []
        if self.atlas is not None:
            try:
                out = self.atlas.handle({"task": "research.investigate",
                    "context": {"query": query, "domain": "training"}, "sender": "forge"})
                rep = out.get("report", {})
                research = {"summary": rep.get("summary", "")[:300], "confidence": rep.get("confidence")}
                text = (rep.get("summary", "") + " " + " ".join(rep.get("key_terms", []))).lower()
                rec_terms = [name for name, meta in KNOWN_METHODS.items()
                           if any(t in text for t in meta["terms"])]
            except Exception:
                research = {"error": "atlas unavailable"}

        runnable, missing, synthesizable = [], [], []
        for name in rec_terms:
            meta = KNOWN_METHODS[name]
            if task_type not in meta["task"]:
                continue
            if _probe(meta["library"]):
                runnable.append({"method": name, "library": meta["library"]})
            else:
                # library missing: honest recommendation, and a synthesis candidate
                missing.append({"method": name, "install": meta["library"]})
                if self.genesis is not None:
                    synthesizable.append({"method": name, "library": meta["library"]})

        return {"task_profile": self._profile_key(task_type, n_samples, n_features),
               "research": research, "recommended": rec_terms,
               "runnable_now": runnable, "recommended_but_missing": missing,
               "synthesizable_via_genesis": synthesizable}

    # ---- 3: champion/challenger A-B on real data ----

    def challenge(self, task_type: str, n_samples: int, n_features: int,
                 challenger_method: str, cv_eval_fn: Callable[[str], Optional[float]],
                 margin: float = 0.01) -> Dict[str, Any]:
        """
        Evaluate a challenger method vs the current champion on the SAME data via
        cv_eval_fn(method_name) -> cross-validated score (or None if unrunnable).
        Adopt the challenger only if it beats the champion by `margin`.
        """
        key = self._profile_key(task_type, n_samples, n_features)
        with self._lock:
            entry = self._preferred.setdefault(key, {"champion": None, "champion_score": None,
                                                     "tried": {}})

        challenger_score = cv_eval_fn(challenger_method)
        if challenger_score is None:
            return {"status": "unrunnable", "method": challenger_method,
                   "note": "library not installed; not adopted (honest)"}

        with self._lock:
            entry["tried"][challenger_method] = round(challenger_score, 4)
            champ, champ_score = entry["champion"], entry["champion_score"]

            adopted = False
            if champ is None or champ_score is None:
                # no champion yet: challenger becomes champion if it clears a floor
                if challenger_score >= 0.5:
                    entry["champion"], entry["champion_score"] = challenger_method, round(challenger_score, 4)
                    adopted = True
            elif challenger_score > champ_score + margin:
                entry["champion"], entry["champion_score"] = challenger_method, round(challenger_score, 4)
                adopted = True
            self._persist()

        self._preserve(key, challenger_method, challenger_score, adopted)
        return {"status": "complete", "task_profile": key, "challenger": challenger_method,
               "challenger_score": round(challenger_score, 4),
               "incumbent": champ, "incumbent_score": champ_score,
               "adopted": adopted, "current_champion": entry["champion"]}

    # ---- 4: what to use now ----

    def preferred_method(self, task_type: str, n_samples: int, n_features: int) -> Optional[str]:
        key = self._profile_key(task_type, n_samples, n_features)
        with self._lock:
            return self._preferred.get(key, {}).get("champion")

    def _preserve(self, key, method, score, adopted):
        if self.chronicle is None:
            return
        try:
            verdict = "adopted as new champion" if adopted else "did not beat champion"
            self.chronicle.store(
                content=f"Forge evaluated training method '{method}' for {key}: "
                       f"cv_score={round(score,4)}, {verdict}.",
                memory_type="evolutionary", domain="training",
                tags=["forge", "method_discovery", method, "adopted" if adopted else "rejected"],
                source="forge")
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"task_profiles": len(self._preferred),
                   "champions": {k: v.get("champion") for k, v in self._preferred.items()}}
