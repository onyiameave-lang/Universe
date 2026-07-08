"""
Aegis.core.monitor
==================
Continuous monitoring: scheduled, proactive governance sweeps. (Book III Part II
Ch XIII Repository Health; Book I Article XII Self-Evaluation; institutional
control functions run CONTINUOUSLY, not only on request.)

A background monitor thread periodically:
  * snapshots each tracked entity's risk trend (rising/falling),
  * runs compliance + security sweeps of registered repositories,
  * emits alerts when exposure crosses thresholds or trends rise,
  * records everything to the audit log.

Runs on a daemon thread; safe start/stop. The monitor never blocks enforcement.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("aegis.monitor")


class ContinuousMonitor:
    def __init__(self, risk_register, audit_log, sweep_fn: Callable[[], Dict[str, Any]],
                 interval_sec: float = 30.0, exposure_alert: float = 2.0):
        self.risk = risk_register
        self.audit = audit_log
        self.sweep_fn = sweep_fn            # callback that performs a compliance/security sweep
        self.interval = interval_sec
        self.exposure_alert = exposure_alert
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._alerts: List[Dict[str, Any]] = []
        self._sweeps = 0
        self._watched_entities: List[str] = []

    def watch(self, entities: List[str]) -> None:
        self._watched_entities = list(entities)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="aegis-monitor")
        self._thread.start()
        log.info("Continuous monitor started (interval=%.0fs)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as exc:
                log.debug("monitor tick error: %s", exc)

    def _tick(self) -> None:
        self._sweeps += 1
        # 1. risk trends for watched entities
        for entity in self._watched_entities:
            trend = self.risk.snapshot_trend(entity)
            if trend["exposure"] >= self.exposure_alert or trend["trend"] == "rising":
                self._alert("risk_trend", entity,
                          f"exposure={trend['exposure']} trend={trend['trend']}")
        # 2. periodic compliance/security sweep (throttled: every 4th tick)
        if self._sweeps % 4 == 0 and self.sweep_fn:
            try:
                result = self.sweep_fn()
                self.audit.append("ecosystem", "aegis", "scheduled_sweep", severity="info",
                                 detail=f"avg_compliance={result.get('avg_compliance')}")
            except Exception as exc:
                log.debug("sweep failed: %s", exc)

    def _alert(self, kind: str, entity: str, detail: str) -> None:
        alert = {"kind": kind, "entity": entity, "detail": detail, "ts": time.time()}
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]
        self.audit.append(entity, "aegis", f"monitor.{kind}", severity="warning", detail=detail)

    def recent_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._alerts[-limit:]

    def stats(self) -> Dict[str, Any]:
        return {"running": self._thread.is_alive() if self._thread else False,
               "sweeps": self._sweeps, "alerts": len(self._alerts),
               "watched": self._watched_entities, "interval_sec": self.interval}
