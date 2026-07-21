"""
Forge.agents.training_agent
=========================
Forge (formerly Training Engine): an institutional, SELF-IMPROVING ML platform,
on the constitutional BaseAgent. (Book I Article IX + XIII; Book III Part II
Ch VIII.)

Now Forge does not merely pick among fixed backends: it RESEARCHES newer/better
methods (via Atlas), tries them as challengers against the reigning champion on
real cross-validated data, and ADOPTS a method only when it genuinely wins.
Discovered methods that need an uninstalled library are reported honestly as
recommendations (or routed to Genesis for gated synthesis), never faked.

Pipeline per job: validate -> (preferred/discovered method or) select backend ->
CV-tune -> train -> evaluate -> register (with drift baseline) -> gate promotion.
`training.evolve` runs the discovery + champion/challenger loop.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

from core.dataset import (DatasetLoader, Dataset, DataValidator, train_test_split,  # type: ignore
                          kfold_indices, standardize)
from core.backends import (all_backends, available_backends, backend_catalog,        # type: ignore
                           UnifiedModel, evaluate_supervised)
from core.extra_backends import discovery_backends, predict_with                     # type: ignore
from core.model_registry import ModelRegistry                                        # type: ignore
from intelligence.optimizer import BackendSelector, HyperparameterOptimizer          # type: ignore
from intelligence.method_discovery import MethodDiscovery                            # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "forge")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("forge")


class ForgeAgent(BaseAgent):
    name = "forge"
    repository = "Forge"
    domain = "training"
    description = "Self-improving institutional ML platform: discovers, tests, and adopts better methods."
    capabilities = ["training.run", "training.from_csv", "training.evolve", "data.validate",
                    "method.discover", "benchmark.execute", "model.register", "model.promote",
                    "model.leaderboard", "model.predict", "model.drift",
                    "hyperparameter.optimize", "backends.catalog"]
    channels = ["ecosystem.training", "ecosystem.optimization", "ecosystem.broadcast"]
    memory_namespace = "forge_memory"
    security_level = "elevated"
    mission = {"purpose": "Train with rigor and continually adopt better methods through evidence."}

    def __init__(self, chronicle_client=None, atlas_client=None, genesis_client=None,
                 storage_dir=None, **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                        storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.loader = DatasetLoader()
        self.validator = DataValidator()
        self.registry = ModelRegistry(storage_dir=storage_dir or str(_REPO_ROOT / "models"))
        self.selector = BackendSelector(atlas_client=atlas_client)
        self.optimizer = HyperparameterOptimizer()
        self.discovery = MethodDiscovery(atlas=atlas_client, chronicle=chronicle_client,
                                        genesis=genesis_client, storage_dir=str(_REPO_ROOT / "memory"))
        # core backends + discoverable extra backends, unified
        self._backends = {b.name: b for b in all_backends()}
        self._backends.update(discovery_backends())

    def on_start(self) -> None:
        log.info("Forge self-improving platform online. Backends: %s | Atlas: %s",
                 [n for n, b in self._backends.items() if b.available], self.atlas is not None)

    # ---- cross-validated score for any backend (used by tuning + discovery) ----

    def _cv_score(self, ds: Dataset, backend_name: str, hp: Optional[Dict] = None) -> Optional[float]:
        backend = self._backends.get(backend_name)
        if backend is None or not backend.available or backend.kind == "reinforcement":
            return None
        hp = hp or {}
        folds = kfold_indices(ds, k=min(5, max(2, ds.n_samples // 20)))
        scores = []
        for tr, va in folds:
            Xtr = [ds.X[i] for i in tr]; ytr = [ds.y[i] for i in tr]
            Xv = [ds.X[i] for i in va]; yv = [ds.y[i] for i in va]
            Xtr_s, Xv_s, _ = standardize(Xtr, Xv)
            try:
                raw, info = backend.train({"X_train": Xtr_s, "y_train": ytr,
                                         "task_type": ds.task_type}, hp)
                preds = (predict_with(backend_name, raw, Xv_s)
                        if backend_name in ("xgboost", "lightgbm", "svm")
                        else UnifiedModel(backend_name, raw, ds.task_type,
                                        classes=info.get("classes")).predict(Xv_s))
                scores.append(self._score(preds, yv, ds.task_type))
            except Exception:
                return None
        return sum(scores) / len(scores) if scores else None

    def _score(self, preds, y, task_type) -> float:
        if task_type == "classification":
            return sum(1 for a, b in zip(preds, y) if a == b) / len(y) if y else 0.0
        n = len(y); mean_y = sum(y) / n
        ss_tot = sum((v - mean_y) ** 2 for v in y) or 1.0
        ss_res = sum((a - b) ** 2 for a, b in zip(preds, y))
        return 1 - ss_res / ss_tot

    # ---- the self-improvement loop ----

    def evolve(self, ds: Dataset) -> Dict[str, Any]:
        """Research better methods, A/B them vs the champion on real CV, adopt winners."""
        disc = self.discovery.discover(ds.task_type, ds.n_samples, ds.n_features)
        results = []
        # challengers = currently-runnable recommended methods + strong defaults
        challengers = [m["method"] for m in disc["runnable_now"]]
        for base in ("sklearn", "xgboost", "lightgbm"):
            if base not in challengers and self._backends.get(base) and self._backends[base].available:
                challengers.append(base)
        for method in challengers:
            outcome = self.discovery.challenge(
                ds.task_type, ds.n_samples, ds.n_features, method,
                cv_eval_fn=lambda m: self._cv_score(ds, "sklearn" if m == "svm" else m))
            results.append(outcome)
        champion = self.discovery.preferred_method(ds.task_type, ds.n_samples, ds.n_features)
        return {"status": "complete", "discovery": disc, "challenges": results,
               "adopted_champion": champion,
               "recommended_but_missing": disc["recommended_but_missing"]}

    def train(self, ds: Dataset, user_backend=None, register_as=None, optimize=True,
              use_champion=True) -> Dict[str, Any]:
        validation = self.validator.validate(ds)
        if not validation["ok"]:
            return {"status": "error", "message": "data failed validation", "validation": validation}

        # prefer a previously-adopted champion method for this profile
        backend_name = user_backend
        if backend_name is None and use_champion:
            backend_name = self.discovery.preferred_method(ds.task_type, ds.n_samples, ds.n_features)
        decision = None
        if backend_name is None:
            decision = self.selector.select(ds.task_type, ds.n_samples, ds.n_features,
                                           user_preference=user_backend)
            backend_name = decision["chosen_backend"]

        backend = self._backends.get(backend_name)
        if backend is None or not backend.available:
            return {"status": "error", "message": f"backend {backend_name} unavailable"}

        X_train, X_val, y_train, y_val = train_test_split(ds, 0.2)
        X_train_s, X_val_s, scaler = standardize(X_train, X_val)

        best_hp, opt_result = {}, None
        if optimize and backend.kind != "reinforcement":
            opt_result = self.optimizer.optimize(
                backend_name, lambda hp: self._cv_score(ds, backend_name, hp) or 0.0, n_trials=6)
            best_hp = opt_result["best_hyperparameters"]

        raw, info = backend.train({"X_train": X_train_s, "y_train": y_train,
                                 "task_type": ds.task_type}, best_hp)
        if backend_name in ("xgboost", "lightgbm", "svm"):
            preds = predict_with(backend_name, raw, X_val_s)
            metrics = self._metrics(preds, y_val, ds.task_type)
            model = _ExtraModelWrapper(backend_name, raw, ds.task_type, scaler)
        else:
            model = UnifiedModel(backend_name, raw, ds.task_type, classes=info.get("classes"), scaler=scaler)
            metrics = evaluate_supervised(model, X_val_s, y_val)

        eid = self.registry.record_experiment({"dataset": ds.profile(), "backend": backend_name,
            "hyperparameters": best_hp, "metrics": metrics, "decision": decision,
            "validation": validation, "cv_optimization": opt_result})
        result = {"status": "complete", "experiment_id": eid, "chosen_backend": backend_name,
                 "metrics": metrics, "best_hyperparameters": best_hp,
                 "data_quality": validation["quality_score"], "used_champion": use_champion and decision is None}
        if register_as:
            rec = self.registry.register_model(register_as, "1.0.0", ds.dataset_id, ds.task_type,
                                              metrics, eid, model_obj=model, hyperparams=best_hp,
                                              feature_baseline=scaler)
            result["model_id"] = rec["model_id"]
        self._preserve(register_as or ds.dataset_id, backend_name, metrics)
        return result

    def _metrics(self, preds, y, task_type):
        if task_type == "classification":
            try:
                from sklearn.metrics import accuracy_score, precision_recall_fscore_support
                acc = accuracy_score(y, preds)
                p, r, f1, _ = precision_recall_fscore_support(y, preds, average="weighted", zero_division=0)
                return {"accuracy": round(float(acc), 4), "precision": round(float(p), 4),
                       "recall": round(float(r), 4), "f1": round(float(f1), 4)}
            except Exception:
                return {"accuracy": round(sum(1 for a, b in zip(preds, y) if a == b) / len(y), 4)}
        n = len(y); mean_y = sum(y) / n
        ss_tot = sum((v - mean_y) ** 2 for v in y) or 1.0
        ss_res = sum((a - b) ** 2 for a, b in zip(preds, y))
        return {"r2": round(1 - ss_res / ss_tot, 4),
               "mae": round(sum(abs(a - b) for a, b in zip(preds, y)) / n, 4),
               "rmse": round((ss_res / n) ** 0.5, 4)}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        if task == "backends.catalog":
            return {"status": "complete",
                   "catalog": {n: b.describe() for n, b in self._backends.items()}}
        if task == "method.discover":
            return {"status": "complete", "discovery": self.discovery.discover(
                ctx.get("task_type", "classification"), ctx.get("n_samples", 100),
                ctx.get("n_features", 10))}
        if task == "training.evolve":
            ds = self._ds(ctx)
            if ds is None:
                return {"status": "error", "message": "provide data"}
            return self.evolve(ds)
        if task in ("training.run", "training.from_csv"):
            ds = self._ds(ctx)
            if ds is None:
                return {"status": "error", "message": "provide X+y or path+target_column"}
            return self.train(ds, user_backend=ctx.get("backend"),
                            register_as=ctx.get("register_as"), optimize=ctx.get("optimize", True))
        if task == "data.validate":
            ds = self._ds(ctx)
            return ({"status": "complete", "validation": self.validator.validate(ds)}
                   if ds else {"status": "error", "message": "provide data"})
        if task == "model.leaderboard":
            return {"status": "complete", "leaderboard": self.registry.leaderboard(
                ctx.get("domain", ""), ctx.get("metric", "accuracy"))}
        if task == "model.drift":
            return {"status": "complete", **self.registry.detect_drift(
                ctx.get("model_id", ""), ctx.get("live_features", []))}
        if task == "model.predict":
            model = self.registry.load_model(ctx.get("model_id", ""))
            return ({"status": "complete", "predictions": list(model.predict(ctx.get("X", [])))}
                   if model else {"status": "error", "message": "model not found"})
        if task == "model.promote":
            return {"status": "complete", **self.registry.promote(ctx.get("model_id", ""),
                    ctx.get("to_status", "candidate"), ctx.get("benchmark_passed", False))}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def _ds(self, ctx) -> Optional[Dataset]:
        if ctx.get("path"):
            try:
                return self.loader.load_csv(ctx["path"], ctx.get("target_column", ""),
                                          ctx.get("task_type", "classification"))
            except Exception:
                return None
        if ctx.get("X") and ctx.get("y"):
            return self.loader.load_arrays(ctx["X"], ctx["y"], ctx.get("feature_names"),
                                         ctx.get("task_type", "classification"),
                                         ctx.get("dataset_id", "in_memory"))
        return None

    def _preserve(self, name, backend, metrics):
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(content=f"Forge trained '{name}' via {backend}: {metrics}",
                                pillar="evolutionary", domain="training",
                                tags=["forge", backend], source_repository="forge")
        except Exception:
            log.debug("chronicle persist failed")

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["backends"] = {n: b.describe() for n, b in self._backends.items()}
        base["registry"] = self.registry.stats()
        base["method_discovery"] = self.discovery.stats()
        return base


class _ExtraModelWrapper:
    """Persistable wrapper so xgboost/lightgbm/svm models predict uniformly + scale inputs."""
    def __init__(self, backend, raw, task_type, scaler):
        self.backend = backend; self.raw = raw; self.task_type = task_type; self.scaler = scaler or {}
    def predict(self, X):
        m, s = self.scaler.get("means"), self.scaler.get("stds")
        Xs = ([[(r[j] - m[j]) / s[j] for j in range(len(r))] for r in X] if m and s else X)
        return predict_with(self.backend, self.raw, Xs)
