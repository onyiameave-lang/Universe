"""Loopback client that forwards Oracle V1 learning events to Forge V2."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("oracle.forge_bridge")
_ROOT = Path(__file__).resolve().parents[1]
_OUTBOX = _ROOT / "Oracle" / "memory" / "forge_bridge_outbox.jsonl"
_DEAD_LETTER = _ROOT / "Oracle" / "memory" / "forge_bridge_dead_letter.jsonl"


class ForgeBridgeClient:
    """Small durable outbox client for the Oracle V1 -> Forge V2 boundary.

    Demo outcomes and Oracle decision observations are written locally before
    delivery. Forge V2 deduplicates them and persists accepted events through
    Chronicle. No trading or strategy-promotion task is exposed by this client.
    """

    _ALLOWED_TASKS = {
        "oracle.strategy.demo_outcome",
        "oracle.improvement.request",
        "oracle.decision.observation",
        "oracle.trader.heartbeat",
    }

    def __init__(self) -> None:
        self.base_url = os.getenv("ORACLE_FORGE_BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.token = os.getenv("ORACLE_FORGE_BRIDGE_TOKEN", "").strip()
        self.timeout = self._timeout_seconds()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        if not self.token:
            log.warning(
                "Forge V2 bridge token is missing; demo feedback will remain in the local outbox"
            )
        self.start()

    @staticmethod
    def _timeout_seconds() -> float:
        try:
            value = float(os.getenv("ORACLE_FORGE_BRIDGE_TIMEOUT_SEC", "3"))
            return min(10.0, max(0.5, value))
        except ValueError:
            return 3.0

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="oracle-forge-bridge", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=1.0)

    def act(self, task: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Forge-compatible act facade; feedback uses the durable outbox."""
        if task not in self._ALLOWED_TASKS:
            return {"status": "blocked", "message": "task is not allowed by the Oracle V1 bridge"}
        context = dict(context or {})
        if task == "oracle.strategy.demo_outcome":
            outcome = context.get("outcome")
            if not isinstance(outcome, dict):
                return {"status": "error", "message": "demo outcome must be an object"}
            if not self._queue_outcome(outcome):
                return {"status": "error", "message": "could not persist Forge outcome to local outbox"}
            self._wake.set()
            return {"status": "queued", "message": "demo outcome saved for Forge V2 delivery"}
        if task == "oracle.decision.observation":
            observation = context.get("observation")
            if not isinstance(observation, dict):
                return {"status": "error", "message": "decision observation must be an object"}
            if not self._queue_observation(observation):
                return {"status": "error", "message": "could not persist decision observation to local outbox"}
            self._wake.set()
            return {"status": "queued", "message": "decision observation saved for Forge V2 delivery"}
        if task == "oracle.trader.heartbeat":
            heartbeat = context.get("heartbeat")
            if not isinstance(heartbeat, dict):
                return {"status": "error", "message": "trader heartbeat must be an object"}
            if not self._queue_observation(heartbeat, task=task, context_key="heartbeat"):
                return {"status": "error", "message": "could not persist trader heartbeat to local outbox"}
            self._wake.set()
            return {"status": "queued", "message": "trader heartbeat saved for Forge V2 delivery"}
        return self._post(task, context)

    def _queue_outcome(self, outcome: Dict[str, Any]) -> bool:
        try:
            encoded = json.dumps(
                {"task": "oracle.strategy.demo_outcome", "context": {"outcome": outcome}},
                sort_keys=True, separators=(",", ":"), allow_nan=False,
            )
            _OUTBOX.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, _OUTBOX.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.error("Forge V2 outcome could not be queued: %s", type(exc).__name__)
            return False

    def _queue_observation(self, observation: Dict[str, Any],
                           task: str = "oracle.decision.observation",
                           context_key: str = "observation") -> bool:
        try:
            encoded = json.dumps(
                {"task": task, "context": {context_key: observation}},
                sort_keys=True, separators=(",", ":"), allow_nan=False,
            )
            if len(encoded.encode("utf-8")) > 64 * 1024:
                log.warning("Forge decision observation exceeds the 64 KiB limit")
                return False
            _OUTBOX.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, _OUTBOX.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.error("Forge decision observation could not be queued: %s", type(exc).__name__)
            return False

    def _post(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.token:
            return {"status": "blocked", "message": "ORACLE_FORGE_BRIDGE_TOKEN is not configured"}
        payload = json.dumps(
            {"task": task, "context": context},
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/internal/forge/act", data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Oracle-Forge-Token": self.token,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            return result if isinstance(result, dict) else {"status": "error", "message": "invalid Forge response"}
        except HTTPError as exc:
            log.warning("Forge V2 bridge returned HTTP %s", exc.code)
            return {"status": "unavailable", "http_status": exc.code}
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            log.warning("Forge V2 bridge unavailable: %s", type(exc).__name__)
            return {"status": "unavailable", "message": type(exc).__name__}

    @staticmethod
    def _is_persisted(response: Dict[str, Any]) -> bool:
        result = response.get("result")
        return isinstance(result, dict) and result.get("status") == "complete"

    @staticmethod
    def _valid_outbox_envelope(envelope: Any) -> bool:
        if not isinstance(envelope, dict):
            return False
        task, context = envelope.get("task"), envelope.get("context")
        if not isinstance(context, dict):
            return False
        if task == "oracle.strategy.demo_outcome":
            payload = context.get("outcome")
            return isinstance(payload, dict) and payload.get("schema") == "oracle.strategy.demo_outcome.v1"
        if task == "oracle.decision.observation":
            payload = context.get("observation")
            return isinstance(payload, dict) and payload.get("schema") == "oracle.decision.observation.v1"
        if task == "oracle.trader.heartbeat":
            payload = context.get("heartbeat")
            return isinstance(payload, dict) and payload.get("schema") == "oracle.trader.heartbeat.v1"
        return False

    def _archive_outbox_row(self, row: str, reason: str,
                            response: Optional[Dict[str, Any]] = None) -> bool:
        """Durably quarantine a permanently invalid row, then advance the FIFO."""
        temp = None
        record = {
            "schema": "oracle.forge_bridge.dead_letter.v1",
            "row_sha256": hashlib.sha256(row.encode("utf-8")).hexdigest(),
            "dead_lettered_at_unix": time.time(),
            "reason": reason[:240], "response": response or {}, "raw_row": row,
        }
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
            _DEAD_LETTER.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with _DEAD_LETTER.open("a", encoding="utf-8") as stream:
                    stream.write(encoded + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                current = _OUTBOX.read_text(encoding="utf-8").splitlines()
                try:
                    current.remove(row)
                except ValueError:
                    return True  # The row was already removed after a prior retry.
                if current:
                    temp = _OUTBOX.with_suffix(".jsonl.tmp")
                    with temp.open("w", encoding="utf-8", newline="\n") as stream:
                        stream.write("\n".join(current) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, _OUTBOX)
                else:
                    _OUTBOX.unlink(missing_ok=True)
            log.error("Quarantined invalid Forge V2 outbox row (%s); later events may proceed", reason)
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.error("Could not durably quarantine Forge V2 outbox row: %s", type(exc).__name__)
            return False
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _flush_outbox(self) -> None:
        if not self.token or not _OUTBOX.exists():
            return
        while not self._stop.is_set():
            try:
                with self._lock:
                    rows = _OUTBOX.read_text(encoding="utf-8").splitlines()
            except OSError:
                return
            if not rows:
                break
            row = rows[0]
            try:
                envelope = json.loads(row)
            except (TypeError, json.JSONDecodeError):
                if not self._archive_outbox_row(row, "invalid_json"):
                    return
                continue
            if not self._valid_outbox_envelope(envelope):
                if not self._archive_outbox_row(row, "unsupported_or_invalid_envelope"):
                    return
                continue
            task = envelope["task"]
            context = envelope["context"]

            # Network waits happen without the queue lock so close processing
            # can append the next outcome immediately.
            response = self._post(task, context)
            if not self._is_persisted(response):
                if response.get("http_status") in {400, 413, 422}:
                    if not self._archive_outbox_row(
                            row, f"permanent_http_{response['http_status']}", response):
                        return
                    continue
                break
            try:
                with self._lock:
                    current = _OUTBOX.read_text(encoding="utf-8").splitlines()
                    if current and current[0] == row:
                        current.pop(0)
                    else:
                        current.remove(row)
                    if current:
                        temp = _OUTBOX.with_suffix(".jsonl.tmp")
                        with temp.open("w", encoding="utf-8", newline="\n") as stream:
                            stream.write("\n".join(current) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp, _OUTBOX)
                    else:
                        _OUTBOX.unlink(missing_ok=True)
                log.info("Delivered Oracle learning event to Forge V2")
            except (OSError, ValueError) as exc:
                log.error("Could not compact Forge V2 outbox: %s", type(exc).__name__)
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            self._flush_outbox()
            self._wake.wait(timeout=15.0)
            self._wake.clear()
