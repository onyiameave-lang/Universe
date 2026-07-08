"""
Forge.core.extra_backends
========================
Additional real training backends for methods Forge can DISCOVER and adopt.
(Book I Article XIII Evolution; Book IV Part VII Model Standards.)

These are genuine adapters to widely-used libraries. Each reports `available`
by live import probe, so a method is only ever RUN if its library is installed.
When absent, Forge reports it as an actionable recommendation, never fakes it.

  * XGBoostBackend   gradient boosting (xgboost)      [needs xgboost]
  * LightGBMBackend  gradient boosting (lightgbm)     [needs lightgbm]
  * SVMBackend       kernel SVM (scikit-learn)        [needs sklearn]

Registered alongside the core backends so the selector and method-discovery
engine can choose them when research recommends and they are installed.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from core.backends import TrainingBackend  # type: ignore


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


class XGBoostBackend(TrainingBackend):
    name = "xgboost"; kind = "supervised"; supports = ["classification", "regression"]
    @property
    def available(self): return _has("xgboost")
    def train(self, data, hp):
        import xgboost as xgb
        X, y, task = data["X_train"], data["y_train"], data["task_type"]
        t = time.time()
        if task == "classification":
            classes = sorted(set(y), key=lambda v: str(v))
            idx = {c: i for i, c in enumerate(classes)}
            model = xgb.XGBClassifier(n_estimators=hp.get("n_estimators", 200),
                                     max_depth=hp.get("max_depth", 6),
                                     learning_rate=hp.get("learning_rate", 0.1),
                                     use_label_encoder=False, eval_metric="mlogloss")
            model.fit(X, [idx[v] for v in y])
            model._forge_classes = classes  # type: ignore
        else:
            model = xgb.XGBRegressor(n_estimators=hp.get("n_estimators", 200),
                                    max_depth=hp.get("max_depth", 6),
                                    learning_rate=hp.get("learning_rate", 0.1))
            model.fit(X, y)
            model._forge_classes = None  # type: ignore
        return model, {"backend": self.name, "seconds": round(time.time() - t, 3),
                      "classes": getattr(model, "_forge_classes", None)}


class LightGBMBackend(TrainingBackend):
    name = "lightgbm"; kind = "supervised"; supports = ["classification", "regression"]
    @property
    def available(self): return _has("lightgbm")
    def train(self, data, hp):
        import lightgbm as lgb
        X, y, task = data["X_train"], data["y_train"], data["task_type"]
        t = time.time()
        if task == "classification":
            classes = sorted(set(y), key=lambda v: str(v))
            idx = {c: i for i, c in enumerate(classes)}
            model = lgb.LGBMClassifier(n_estimators=hp.get("n_estimators", 200),
                                      learning_rate=hp.get("learning_rate", 0.1))
            model.fit(X, [idx[v] for v in y])
            model._forge_classes = classes  # type: ignore
        else:
            model = lgb.LGBMRegressor(n_estimators=hp.get("n_estimators", 200),
                                     learning_rate=hp.get("learning_rate", 0.1))
            model.fit(X, y)
            model._forge_classes = None  # type: ignore
        return model, {"backend": self.name, "seconds": round(time.time() - t, 3),
                      "classes": getattr(model, "_forge_classes", None)}


class SVMBackend(TrainingBackend):
    name = "svm"; kind = "supervised"; supports = ["classification", "regression"]
    @property
    def available(self): return _has("sklearn")
    def train(self, data, hp):
        from sklearn.svm import SVC, SVR
        X, y, task = data["X_train"], data["y_train"], data["task_type"]
        t = time.time()
        if task == "classification":
            model = SVC(C=hp.get("C", 1.0), kernel=hp.get("kernel", "rbf"), probability=False)
            model.fit(X, y)
            classes = list(model.classes_)
        else:
            model = SVR(C=hp.get("C", 1.0), kernel=hp.get("kernel", "rbf"))
            model.fit(X, y)
            classes = None
        return model, {"backend": self.name, "seconds": round(time.time() - t, 3), "classes": classes}


# Method name (from KNOWN_METHODS) -> backend instance
def discovery_backends() -> Dict[str, TrainingBackend]:
    return {"xgboost": XGBoostBackend(), "lightgbm": LightGBMBackend(), "svm": SVMBackend()}


def predict_with(backend_name: str, raw_model, X: List[List[float]]) -> List[Any]:
    """Unified prediction for the extra sklearn-style backends (xgboost maps ints->classes)."""
    classes = getattr(raw_model, "_forge_classes", None)
    preds = raw_model.predict(X)
    preds = list(preds)
    if backend_name == "xgboost" and classes is not None:
        return [classes[int(p)] for p in preds]
    return preds
