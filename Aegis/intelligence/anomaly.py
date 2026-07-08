"""
Aegis.intelligence.anomaly
=========================
Statistical behavioral anomaly detection. (Book I Part IV Article XII
Self-Evaluation; institutional surveillance: catch UNKNOWN bad behavior, not
just known rule violations.)

Rule checks catch what you already forbade. Anomaly detection catches what you
did not anticipate: an agent suddenly calling 10x more, latency spiking, error
rate climbing, activity at odd intervals. This engine maintains a rolling
behavioral baseline per agent (mean + standard deviation via Welford's online
algorithm) and flags observations beyond a z-score threshold.

No fabrication: an anomaly is a real statistical deviation from the agent's own
established baseline, reported with the z-score so the judgment is auditable.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional


class _RunningStat:
    """Welford's online mean/variance. Real streaming statistics."""
    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / self.n) if self.n > 1 else 0.0

    def z(self, x: float) -> float:
        if self.n < 5 or self.std == 0:
            return 0.0
        return (x - self.mean) / self.std


class AnomalyDetector:
    def __init__(self, z_threshold: float = 3.0):
        self.z_threshold = z_threshold
        self._lock = threading.RLock()
        # metric key -> per-agent running stats
        self._call_rate: Dict[str, _RunningStat] = defaultdict(_RunningStat)
        self._latency: Dict[str, _RunningStat] = defaultdict(_RunningStat)
        self._error_rate: Dict[str, _RunningStat] = defaultdict(_RunningStat)
        # sliding window of call timestamps for rate computation
        self._recent_calls: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self._anomalies: List[Dict[str, Any]] = []

    def observe(self, agent: str, latency_ms: float, success: bool) -> Optional[Dict[str, Any]]:
        """Record one observed call; return an anomaly record if it deviates."""
        with self._lock:
            now = time.time()
            self._recent_calls[agent].append(now)
            # instantaneous rate: calls in the last 60s
            recent = [t for t in self._recent_calls[agent] if now - t <= 60]
            rate = len(recent)
            err = 0.0 if success else 1.0

            anomalies = []
            for name, stat, value in (("call_rate", self._call_rate[agent], rate),
                                     ("latency", self._latency[agent], latency_ms),
                                     ("error_rate", self._error_rate[agent], err)):
                z = stat.z(value)
                if abs(z) >= self.z_threshold and value > stat.mean:  # only flag WORSE-than-normal
                    anomalies.append({"metric": name, "value": round(value, 2),
                                    "baseline_mean": round(stat.mean, 2),
                                    "baseline_std": round(stat.std, 2), "z_score": round(z, 2)})
                stat.update(value)

            if anomalies:
                record = {"agent": agent, "timestamp": now, "anomalies": anomalies}
                self._anomalies.append(record)
                if len(self._anomalies) > 500:
                    self._anomalies = self._anomalies[-500:]
                return record
            return None

    def recent_anomalies(self, agent: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            a = self._anomalies if agent is None else [x for x in self._anomalies if x["agent"] == agent]
            return a[-limit:]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"agents_baselined": len(self._call_rate),
                   "anomalies_detected": len(self._anomalies),
                   "z_threshold": self.z_threshold}
