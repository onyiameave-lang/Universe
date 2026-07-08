"""
Forge.intelligence.optimizer
==========================
Hyperparameter optimization and Atlas-informed backend selection. (Book I
Part IV Article IX; Book II Principle IV Research Before Assumption; Book II
Part I Ch VII specialists collaborate.)

Two institutional capabilities:

  * BACKEND SELECTION: score each AVAILABLE backend against the task profile
    (type, size, dimensionality, RL environment), boosted by real Atlas research
    into the best technique. Explainable decision; only installed backends win.

  * HYPERPARAMETER OPTIMIZATION: real search (random search over sensible ranges)
    evaluated by cross-validated score, so the chosen config is measured, not
    guessed. Returns the best config plus the full trial history.

No fabricated results: every trial trains and evaluates on real folds.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.backends import available_backends, backend_catalog  # type: ignore


class BackendSelector:
    def __init__(self, atlas_client=None):
        self.atlas = atlas_client

    def select(self, task_type: str, n_samples: int, n_features: int,
               has_environment: bool = False, research: bool = True,
               user_preference: Optional[str] = None) -> Dict[str, Any]:
        available = {b.name: b for b in available_backends()}
        candidates = [b for b in available.values()
                     if (b.kind != "reinforcement" or has_environment)
                     and (task_type in b.supports or (b.kind == "reinforcement" and has_environment))]

        research_notes, rec_terms = (None, [])
        if research and self.atlas is not None:
            research_notes, rec_terms = self._research(task_type, n_samples, n_features, has_environment)

        scored = []
        for b in candidates:
            score, reasons = self._score(b, task_type, n_samples, n_features, has_environment, rec_terms)
            if user_preference and b.name == user_preference:
                score += 5.0; reasons["user_preference"] = True
            scored.append((b.name, score, reasons))
        scored.sort(key=lambda x: x[1], reverse=True)

        chosen = scored[0][0] if scored else "scratch"
        return {"chosen_backend": chosen,
               "candidates": [{"backend": n, "score": round(s, 2), "reasons": r} for n, s, r in scored],
               "catalog": backend_catalog(), "atlas_research": research_notes,
               "recommended_terms": rec_terms}

    def _score(self, b, task_type, n, d, has_env, rec_terms):
        score, reasons = 1.0, {}
        if b.kind == "reinforcement":
            if has_env:
                score += 6.0; reasons["rl_fit"] = "environment present"
            else:
                score -= 10.0; reasons["rl_fit"] = "no environment"
        if b.name == "pytorch_mlp":
            if n >= 500:
                score += 3.0; reasons["data"] = "ample data favors NN"
            elif n < 100:
                score -= 2.0; reasons["data"] = "too little data for NN"
            if d >= 20:
                score += 1.5; reasons["dim"] = "high-dim favors NN"
        if b.name == "sklearn":
            if n < 1000:
                score += 2.5; reasons["data"] = "small/medium tabular favors classical ML"
            score += 1.0; reasons["robust"] = "fast, well-tested baseline"
        if b.name == "scratch":
            score += 0.5; reasons["always"] = "dependency-free"
            if n > 2000:
                score -= 1.5; reasons["scale"] = "scratch GD slow at scale"
        term_map = {"pytorch_mlp": ["neural", "deep", "mlp", "network", "backprop"],
                   "ppo": ["reinforcement", "ppo", "policy", "reward", "rl"],
                   "sklearn": ["random forest", "gradient boosting", "logistic", "ensemble", "svm"],
                   "scratch": ["gradient descent", "logistic regression", "linear"]}
        matched = [t for t in rec_terms if any(k in t for k in term_map.get(b.name, []))]
        if matched:
            score += 2.0 * len(matched); reasons["atlas_recommended"] = matched
        return score, reasons

    def _research(self, task_type, n, d, has_env):
        query = ("best reinforcement learning algorithm for control" if has_env
                else f"best machine learning model for {task_type} with {n} samples {d} features")
        try:
            out = self.atlas.handle({"task": "research.investigate",
                "context": {"query": query, "domain": "training"}, "sender": "forge"})
            rep = out.get("report", {})
            text = (rep.get("summary", "") + " " + " ".join(rep.get("key_terms", []))).lower()
            techs = [p for p in ["neural network", "deep learning", "random forest",
                                "gradient boosting", "logistic regression", "reinforcement",
                                "ppo", "ensemble", "support vector"] if p in text]
            return ({"query": query, "confidence": rep.get("confidence"),
                    "summary": rep.get("summary", "")[:250]}, techs)
        except Exception:
            return None, []


class HyperparameterOptimizer:
    """Random search over sensible ranges, scored by cross-validated performance."""

    SPACES = {
        "sklearn": {"model_type": ["logistic", "random_forest", "gradient_boosting"],
                   "n_estimators": [50, 100, 200], "C": [0.1, 1.0, 10.0], "max_iter": [500, 1000]},
        "pytorch_mlp": {"hidden_size": [32, 64, 128], "epochs": [80, 120, 200],
                       "learning_rate": [0.001, 0.01, 0.05]},
        "scratch": {"learning_rate": [0.05, 0.1, 0.2], "epochs": [200, 300, 500]},
        "ppo": {"learning_rate": [1e-4, 3e-4, 1e-3], "n_steps": [1024, 2048],
               "gamma": [0.95, 0.99], "timesteps": [10000, 20000]},
    }

    def optimize(self, backend_name: str, cv_eval_fn: Callable[[Dict[str, Any]], float],
                 n_trials: int = 8, seed: int = 42) -> Dict[str, Any]:
        """
        cv_eval_fn(hp) -> mean cross-validated score (higher is better).
        Returns the best hyperparameters plus the full trial history.
        """
        space = self.SPACES.get(backend_name, {})
        rng = random.Random(seed)
        trials: List[Dict[str, Any]] = []
        best_hp, best_score = {}, -math.inf
        # always try the default (empty hp) first
        configs = [{}]
        for _ in range(max(0, n_trials - 1)):
            configs.append({k: rng.choice(v) for k, v in space.items()})
        for hp in configs:
            try:
                score = cv_eval_fn(hp)
            except Exception:
                score = -math.inf
            trials.append({"hyperparameters": hp, "cv_score": round(score, 4)
                          if math.isfinite(score) else None})
            if score > best_score:
                best_score, best_hp = score, hp
        return {"best_hyperparameters": best_hp, "best_cv_score": round(best_score, 4)
               if math.isfinite(best_score) else None,
               "trials": trials, "n_trials": len(trials)}
