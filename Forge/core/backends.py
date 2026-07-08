"""
Forge.core.backends
==================
Pluggable, real training backends. (Book I Part IV Article IX: many forms of
learning; Book IV Part VII AI Model Standards.)

One contract, many real methods, chosen per job:
  * SklearnBackend   classical ML (logistic/linear/random-forest/gradient-boost)
  * TorchMLPBackend  real PyTorch neural nets (backprop)          [needs torch]
  * PPOBackend       real reinforcement learning via SB3 PPO      [needs sb3+gym]
  * ScratchBackend   from-scratch gradient descent (always available)

Each backend reports `available` (its libraries installed) and `supports`
(task types). A UnifiedModel wraps any backend output behind one predict()
interface so evaluation and the registry are backend-agnostic. Nothing is
faked: an unavailable backend is skipped, never simulated.
"""
from __future__ import annotations

import abc
import math
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:
    _np = None; _HAS_NUMPY = False
try:
    import sklearn  # noqa
    _HAS_SKLEARN = True
except Exception:
    _HAS_SKLEARN = False
try:
    import torch  # noqa
    import torch.nn as _nn  # noqa
    _HAS_TORCH = True
except Exception:
    _HAS_TORCH = False
try:
    import stable_baselines3 as _sb3  # noqa
    _HAS_SB3 = True
except Exception:
    _HAS_SB3 = False


class TrainingBackend(abc.ABC):
    name = "base"; kind = "supervised"; supports: List[str] = []
    @property
    @abc.abstractmethod
    def available(self) -> bool: ...
    @abc.abstractmethod
    def train(self, data: Dict[str, Any], hp: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]: ...
    def describe(self):
        return {"name": self.name, "kind": self.kind, "supports": self.supports, "available": self.available}


# ---------------- from-scratch models (zero deps) ----------------

class _LogRegScratch:
    def __init__(self, lr=0.1, epochs=300):
        self.lr, self.epochs, self.classes, self.W, self.history = lr, epochs, [], [], []
    def fit(self, X, y):
        self.classes = sorted(set(y), key=lambda v: str(v))
        idx = {c: i for i, c in enumerate(self.classes)}
        n, d, k = len(X), len(X[0]), len(self.classes)
        self.W = [[0.0] * (d + 1) for _ in range(k)]
        Xb = [[1.0] + r for r in X]; Y = [idx[v] for v in y]
        for _ in range(self.epochs):
            grads = [[0.0] * (d + 1) for _ in range(k)]; loss = 0.0
            for xi, yi in zip(Xb, Y):
                logits = [sum(self.W[c][j] * xi[j] for j in range(d + 1)) for c in range(k)]
                mx = max(logits); exps = [math.exp(l - mx) for l in logits]; s = sum(exps)
                probs = [e / s for e in exps]; loss += -math.log(max(probs[yi], 1e-12))
                for c in range(k):
                    err = probs[c] - (1.0 if c == yi else 0.0)
                    for j in range(d + 1):
                        grads[c][j] += err * xi[j]
            for c in range(k):
                for j in range(d + 1):
                    self.W[c][j] -= self.lr * grads[c][j] / n
            self.history.append(loss / n)
        return self
    def predict(self, X):
        d = len(self.W[0]) - 1; out = []
        for r in X:
            xi = [1.0] + list(r)
            logits = [sum(self.W[c][j] * xi[j] for j in range(d + 1)) for c in range(len(self.classes))]
            out.append(self.classes[logits.index(max(logits))])
        return out


class _LinRegScratch:
    def __init__(self, lr=0.01, epochs=500):
        self.lr, self.epochs, self.w, self.history = lr, epochs, [], []
    def fit(self, X, y):
        n, d = len(X), len(X[0]); self.w = [0.0] * (d + 1); Xb = [[1.0] + r for r in X]
        for _ in range(self.epochs):
            grad = [0.0] * (d + 1); loss = 0.0
            for xi, yi in zip(Xb, y):
                pred = sum(self.w[j] * xi[j] for j in range(d + 1)); err = pred - yi
                loss += err * err
                for j in range(d + 1):
                    grad[j] += err * xi[j]
            for j in range(d + 1):
                self.w[j] -= self.lr * grad[j] / n
            self.history.append(loss / n)
        return self
    def predict(self, X):
        d = len(self.w) - 1
        return [sum(self.w[j] * ([1.0] + list(r))[j] for j in range(d + 1)) for r in X]


# ---------------- backends ----------------

class SklearnBackend(TrainingBackend):
    name = "sklearn"; kind = "supervised"; supports = ["classification", "regression"]
    @property
    def available(self): return _HAS_SKLEARN
    def train(self, data, hp):
        from sklearn.linear_model import LogisticRegression, LinearRegression
        from sklearn.ensemble import (RandomForestClassifier, GradientBoostingClassifier,
                                       RandomForestRegressor)
        X, y, task = data["X_train"], data["y_train"], data["task_type"]
        mt = hp.get("model_type", "auto")
        if task == "classification":
            m = (RandomForestClassifier(n_estimators=hp.get("n_estimators", 100), random_state=42)
                 if mt == "random_forest" else
                 GradientBoostingClassifier(random_state=42) if mt == "gradient_boosting" else
                 LogisticRegression(max_iter=hp.get("max_iter", 1000), C=hp.get("C", 1.0)))
        else:
            m = (RandomForestRegressor(n_estimators=hp.get("n_estimators", 100), random_state=42)
                 if mt == "random_forest" else LinearRegression())
        t = time.time(); m.fit(X, y)
        return m, {"backend": self.name, "seconds": round(time.time() - t, 3),
                  "classes": list(getattr(m, "classes_", [])) or None}


class TorchMLPBackend(TrainingBackend):
    name = "pytorch_mlp"; kind = "supervised"; supports = ["classification", "regression"]
    @property
    def available(self): return _HAS_TORCH and _HAS_NUMPY
    def train(self, data, hp):
        import torch, torch.nn as nn
        X = torch.tensor(data["X_train"], dtype=torch.float32); task = data["task_type"]
        nf = X.shape[1]; hidden = hp.get("hidden_size", 64); epochs = hp.get("epochs", 120)
        if task == "classification":
            classes = sorted(set(data["y_train"]), key=lambda v: str(v))
            idx = {c: i for i, c in enumerate(classes)}
            y = torch.tensor([idx[v] for v in data["y_train"]], dtype=torch.long)
            net = nn.Sequential(nn.Linear(nf, hidden), nn.ReLU(), nn.Linear(hidden, hidden),
                              nn.ReLU(), nn.Linear(hidden, len(classes)))
            loss_fn = nn.CrossEntropyLoss()
        else:
            classes = []; y = torch.tensor(data["y_train"], dtype=torch.float32).view(-1, 1)
            net = nn.Sequential(nn.Linear(nf, hidden), nn.ReLU(), nn.Linear(hidden, hidden),
                              nn.ReLU(), nn.Linear(hidden, 1))
            loss_fn = nn.MSELoss()
        opt = torch.optim.Adam(net.parameters(), lr=hp.get("learning_rate", 0.01))
        t = time.time(); hist = []
        net.train()
        for _ in range(epochs):
            opt.zero_grad(); out = net(X); loss = loss_fn(out, y); loss.backward(); opt.step()
            hist.append(float(loss.item()))
        return ({"net": net, "classes": classes, "task": task},
                {"backend": self.name, "seconds": round(time.time() - t, 3),
                 "final_loss": round(hist[-1], 5), "classes": classes or None,
                 "architecture": f"MLP[{nf}->{hidden}->{hidden}->out]"})


class PPOBackend(TrainingBackend):
    name = "ppo"; kind = "reinforcement"; supports = ["reinforcement", "control", "trading"]
    @property
    def available(self): return _HAS_SB3
    def train(self, data, hp):
        from stable_baselines3 import PPO
        env_fn, env_id = data.get("env_fn"), data.get("env_id")
        if env_fn is None and env_id is None:
            raise ValueError("PPO requires env_fn or env_id (a real environment)")
        try:
            import gymnasium as gym
        except Exception:
            import gym  # type: ignore
        env = env_fn() if env_fn else gym.make(env_id)
        t = time.time()
        model = PPO("MlpPolicy", env, learning_rate=hp.get("learning_rate", 3e-4),
                    n_steps=hp.get("n_steps", 2048), gamma=hp.get("gamma", 0.99), verbose=0)
        model.learn(total_timesteps=hp.get("timesteps", 10000))
        return ({"policy": model, "env": env},
                {"backend": self.name, "seconds": round(time.time() - t, 3),
                 "timesteps": hp.get("timesteps", 10000), "kind": "reinforcement"})


class ScratchBackend(TrainingBackend):
    name = "scratch"; kind = "supervised"; supports = ["classification", "regression"]
    @property
    def available(self): return True
    def train(self, data, hp):
        task = data["task_type"]; t = time.time()
        if task == "classification":
            m = _LogRegScratch(hp.get("learning_rate", 0.1), hp.get("epochs", 300)).fit(
                data["X_train"], data["y_train"])
            return m, {"backend": self.name, "seconds": round(time.time() - t, 3),
                      "classes": m.classes, "final_loss": m.history[-1] if m.history else None}
        m = _LinRegScratch(hp.get("learning_rate", 0.01), hp.get("epochs", 500)).fit(
            data["X_train"], data["y_train"])
        return m, {"backend": self.name, "seconds": round(time.time() - t, 3),
                  "final_loss": m.history[-1] if m.history else None}


def all_backends() -> List[TrainingBackend]:
    return [SklearnBackend(), TorchMLPBackend(), PPOBackend(), ScratchBackend()]

def available_backends() -> List[TrainingBackend]:
    return [b for b in all_backends() if b.available]

def backend_catalog() -> Dict[str, Any]:
    return {b.name: b.describe() for b in all_backends()}


class UnifiedModel:
    """Backend-agnostic predict() wrapper."""
    def __init__(self, backend, raw, task_type, classes=None, scaler=None):
        self.backend = backend; self.raw = raw; self.task_type = task_type
        self.classes = classes or []; self.scaler = scaler or {}
    def _scale(self, X):
        if not self.scaler:
            return X
        m, s = self.scaler.get("means"), self.scaler.get("stds")
        if not m or not s:
            return X
        return [[(r[j] - m[j]) / s[j] for j in range(len(r))] for r in X]
    def predict(self, X):
        Xs = self._scale(X)
        if self.backend in ("sklearn", "scratch"):
            return list(self.raw.predict(Xs))
        if self.backend == "pytorch_mlp":
            import torch
            net = self.raw["net"]; classes = self.raw["classes"]; task = self.raw["task"]
            net.eval()
            with torch.no_grad():
                out = net(torch.tensor(Xs, dtype=torch.float32))
                if task == "classification":
                    return [classes[i] for i in torch.argmax(out, dim=1).tolist()]
                return out.view(-1).tolist()
        if self.backend == "ppo":
            policy = self.raw["policy"]; acts = []
            for obs in X:
                a, _ = policy.predict(obs, deterministic=True)
                acts.append(a.tolist() if hasattr(a, "tolist") else a)
            return acts
        raise ValueError(f"unknown backend {self.backend}")


def evaluate_supervised(model: UnifiedModel, X_val, y_val) -> Dict[str, float]:
    preds = model.predict(X_val)
    if model.task_type == "classification":
        try:
            from sklearn.metrics import accuracy_score, precision_recall_fscore_support
            acc = accuracy_score(y_val, preds)
            p, r, f1, _ = precision_recall_fscore_support(y_val, preds, average="weighted", zero_division=0)
            return {"accuracy": round(float(acc), 4), "precision": round(float(p), 4),
                   "recall": round(float(r), 4), "f1": round(float(f1), 4)}
        except Exception:
            correct = sum(1 for a, b in zip(preds, y_val) if a == b)
            return {"accuracy": round(correct / len(y_val), 4) if y_val else 0.0}
    n = len(y_val); mean_y = sum(y_val) / n
    ss_tot = sum((v - mean_y) ** 2 for v in y_val) or 1.0
    ss_res = sum((a - b) ** 2 for a, b in zip(preds, y_val))
    return {"r2": round(1 - ss_res / ss_tot, 4),
           "mae": round(sum(abs(a - b) for a, b in zip(preds, y_val)) / n, 4),
           "rmse": round((ss_res / n) ** 0.5, 4)}
