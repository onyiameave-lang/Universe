"""
Aegis.core.risk_register
========================
A living risk register with time-decayed, per-agent and per-repository exposure.
(Book VI Part II Accountability; institutional risk-management practice: a
control function tracks risk EXPOSURE over time, not just discrete violations.)

Every violation contributes risk = policy.base_risk scaled by observed severity.
Exposure DECAYS over time (a clean agent recovers), so the register reflects
CURRENT risk posture, not permanent punishment. Trends are computed so Aegis can
say "this repo's risk is rising" before it becomes an incident.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional


class RiskRegister:
    def __init__(self, storage_dir: str = "security", half_life_hours: float = 24.0):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "risk_register.json"
        self._lock = threading.RLock()
        self.half_life = half_life_hours * 3600.0
        # entity -> list of (timestamp, risk_contribution, policy_id)
        self._events: Dict[str, List[List[float]]] = defaultdict(list)
        # short history of exposure snapshots for trend detection
        self._history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for k, v in data.get("events", {}).items():
                    self._events[k] = v
            except Exception:
                pass

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps({"events": dict(self._events)}), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    def add_risk(self, entity: str, risk: float, policy_id: str = "") -> None:
        with self._lock:
            self._events[entity].append([time.time(), float(risk), policy_id])
            # keep last 200 events per entity
            if len(self._events[entity]) > 200:
                self._events[entity] = self._events[entity][-200:]
            self._persist()

    def exposure(self, entity: str) -> float:
        """Current time-decayed risk exposure for an entity (0..~unbounded)."""
        with self._lock:
            now = time.time()
            total = 0.0
            for ts, risk, _ in self._events.get(entity, []):
                age = now - ts
                decay = math.exp(-math.log(2) * age / self.half_life)
                total += risk * decay
            return round(total, 4)

    def snapshot_trend(self, entity: str) -> Dict[str, Any]:
        """Record current exposure and report whether risk is rising/falling."""
        with self._lock:
            exp = self.exposure(entity)
            hist = self._history[entity]
            hist.append(exp)
            if len(hist) < 3:
                trend = "insufficient_data"
            else:
                recent = sum(list(hist)[-3:]) / 3
                older = sum(list(hist)[:-3]) / max(len(hist) - 3, 1)
                if recent > older * 1.2:
                    trend = "rising"
                elif recent < older * 0.8:
                    trend = "falling"
                else:
                    trend = "stable"
            return {"entity": entity, "exposure": exp, "trend": trend}

    def top_risks(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            scored = [(e, self.exposure(e)) for e in self._events.keys()]
            scored.sort(key=lambda x: x[1], reverse=True)
            return [{"entity": e, "exposure": r} for e, r in scored[:limit] if r > 0]

    def register(self) -> Dict[str, Any]:
        with self._lock:
            entities = {e: self.exposure(e) for e in self._events.keys()}
            elevated = {e: r for e, r in entities.items() if r >= 1.0}
            return {"tracked_entities": len(entities), "elevated_risk": elevated,
                   "top_risks": self.top_risks(5),
                   "total_exposure": round(sum(entities.values()), 3)}
