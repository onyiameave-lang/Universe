"""
Oracle.intelligence.adaptive_fusion
==================================
Signal fusion whose weights and thresholds LEARN from real trade outcomes.
(Book I Article IX Online Learning; Article XIII Evolution.)

The institutional version used fixed weights (technical 0.45, news 0.22, ...).
This version starts from those priors but ADAPTS them: after each closed trade,
the streams that pointed the RIGHT way gain weight; the ones that misled lose
weight. Entry thresholds also adapt to the symbol's realized hit-rate. Over time
Oracle learns, per symbol, which evidence streams actually predict its moves,
which no fixed institutional model does.

Weights persist per symbol. All updates are bounded and normalized so no single
stream can dominate or vanish (stability).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_WEIGHTS = {"technical": 0.45, "news": 0.22, "social": 0.18, "memory": 0.15}
MIN_W, MAX_W = 0.05, 0.75
LR = 0.08  # learning rate for weight updates


class AdaptiveFusion:
    def __init__(self, storage_dir: str = "memory"):
        self._path = Path(storage_dir) / "fusion_weights.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # per-symbol weights + entry threshold + per-stream hit stats
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._state = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}

    def _persist(self):
        try:
            self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    def _sym(self, symbol: str) -> Dict[str, Any]:
        with self._lock:
            if symbol not in self._state:
                self._state[symbol] = {"weights": dict(DEFAULT_WEIGHTS),
                                     "entry_threshold": 0.15,
                                     "stream_hits": {k: {"correct": 0, "total": 0} for k in DEFAULT_WEIGHTS},
                                     "trades": 0, "wins": 0}
            return self._state[symbol]

    def weights(self, symbol: str) -> Dict[str, float]:
        return dict(self._sym(symbol)["weights"])

    def entry_threshold(self, symbol: str) -> float:
        return self._sym(symbol)["entry_threshold"]

    def fuse(self, symbol: str, streams: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Confidence-weighted fusion using this symbol's LEARNED weights."""
        s = self._sym(symbol)
        w = s["weights"]
        num, denom = 0.0, 0.0
        for name, sig in streams.items():
            weight = w.get(name, 0.1) * sig.get("confidence", 0.0)
            num += weight * sig.get("direction", 0.0)
            denom += weight
        direction = num / denom if denom else 0.0
        dirs = [sig["direction"] for sig in streams.values()
               if sig.get("confidence", 0) > 0.1 and abs(sig["direction"]) > 0.05]
        agreement = 0.5
        if len(dirs) >= 2:
            pos = sum(1 for d in dirs if d > 0); neg = sum(1 for d in dirs if d < 0)
            agreement = max(pos, neg) / len(dirs)
        base_conf = denom / sum(w.values()) if w else 0.0
        confidence = round(min(base_conf * (0.5 + 0.5 * agreement), 0.95), 3)
        thr = s["entry_threshold"]
        call = "buy" if direction > thr else "sell" if direction < -thr else "hold"
        return {"symbol": symbol, "call": call, "direction": round(direction, 3),
               "confidence": confidence, "agreement": round(agreement, 3),
               "learned_weights": dict(w), "entry_threshold": thr, "streams": streams,
               "manipulation_warning": streams.get("social", {}).get("manipulation_warning", False)}

    def learn_from_outcome(self, symbol: str, streams_at_entry: Dict[str, Dict[str, Any]],
                          realized_direction: int) -> Dict[str, Any]:
        """
        realized_direction: +1 if price moved up after entry, -1 if down.
        Reward streams that agreed with the realized move, penalize those that didn't.
        """
        with self._lock:
            s = self._sym(symbol)
            w = s["weights"]
            for name, sig in streams_at_entry.items():
                d = sig.get("direction", 0.0)
                if abs(d) < 0.05 or sig.get("confidence", 0) < 0.1:
                    continue
                stream_dir = 1 if d > 0 else -1
                correct = (stream_dir == realized_direction)
                stat = s["stream_hits"].setdefault(name, {"correct": 0, "total": 0})
                stat["total"] += 1
                if correct:
                    stat["correct"] += 1
                    w[name] = min(MAX_W, w[name] + LR * sig.get("confidence", 0.5))
                else:
                    w[name] = max(MIN_W, w[name] - LR * sig.get("confidence", 0.5))
            # normalize weights to sum 1
            total = sum(w.values()) or 1.0
            for k in w:
                w[k] = round(w[k] / total, 4)
            # adapt entry threshold to realized win rate
            s["trades"] += 1
            if realized_direction != 0:
                s["wins"] += 1 if realized_direction > 0 else 0
            win_rate = s["wins"] / s["trades"] if s["trades"] else 0.5
            # low win rate -> demand a stronger signal (raise threshold); high -> relax
            s["entry_threshold"] = round(min(0.4, max(0.08, 0.15 + (0.5 - win_rate) * 0.3)), 3)
            self._persist()
            return {"symbol": symbol, "updated_weights": dict(w),
                   "entry_threshold": s["entry_threshold"],
                   "stream_accuracy": {n: round(st["correct"] / st["total"], 3)
                                     for n, st in s["stream_hits"].items() if st["total"]}}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {sym: {"weights": st["weights"], "entry_threshold": st["entry_threshold"],
                        "trades": st["trades"]} for sym, st in self._state.items()}
