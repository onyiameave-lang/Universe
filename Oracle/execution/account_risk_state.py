"""Small durable account-level guard state shared by Oracle trader runners."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict


_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR = _ROOT / "Oracle" / "memory" / "account_risk"


class AccountRiskState:
    """Persist each broker account's UTC-day equity baseline and kill latch.

    Restarting a trader process must not silently reset the daily loss baseline.
    Files are account-keyed and atomically replaced; corrupt state is surfaced so
    callers can fail closed instead of inventing a fresh baseline.
    """

    def __init__(self, identity: Dict[str, Any]):
        if not str(identity.get("login") or "").strip():
            raise ValueError("broker account login is required for a durable risk key")
        if not str(identity.get("broker") or "").strip():
            raise ValueError("broker name is required for a durable risk key")
        canonical = "|".join(str(identity.get(k) or "").strip().lower()
                             for k in ("broker", "login", "server", "account_type", "currency"))
        self.account_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.path = _STATE_DIR / f"{self.account_key}.json"

    @staticmethod
    def utc_day(now: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))

    def load_or_initialize(self, equity: float, *, now: float | None = None) -> Dict[str, Any]:
        value = float(equity)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("broker equity must be finite and positive")
        today = self.utc_day(now)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if (not isinstance(saved, dict)
                    or saved.get("schema") != "oracle.account_risk_state.v1"
                    or saved.get("account_key") != self.account_key):
                raise ValueError("saved broker risk state is invalid or belongs to another account")
            if saved.get("utc_day") == today:
                baseline = float(saved.get("day_start_equity"))
                if not math.isfinite(baseline) or baseline <= 0:
                    raise ValueError("saved daily equity baseline is invalid")
                return {
                    "utc_day": today,
                    "day_start_equity": baseline,
                    "kill_switch_latched": bool(saved.get("kill_switch_latched", False)),
                }
        state = {"utc_day": today, "day_start_equity": value, "kill_switch_latched": False}
        self.save(state)
        return state

    def save(self, state: Dict[str, Any]) -> bool:
        baseline = float(state["day_start_equity"])
        if not math.isfinite(baseline) or baseline <= 0:
            raise ValueError("daily equity baseline must be finite and positive")
        latched = bool(state.get("kill_switch_latched", False))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            # Never clear a same-day latch if another local runner already persisted it.
            if self.path.exists():
                try:
                    with self.path.open("r", encoding="utf-8") as handle:
                        previous = json.load(handle)
                    if (isinstance(previous, dict) and previous.get("account_key") == self.account_key
                            and previous.get("utc_day") == state["utc_day"]):
                        latched = latched or bool(previous.get("kill_switch_latched", False))
                        prior_baseline = float(previous.get("day_start_equity"))
                        if math.isfinite(prior_baseline) and prior_baseline > 0:
                            baseline = prior_baseline
                except (OSError, ValueError, TypeError) as exc:
                    raise ValueError("saved broker risk state became unreadable") from exc
            payload = {
                "schema": "oracle.account_risk_state.v1",
                "account_key": self.account_key,
                "utc_day": str(state["utc_day"]),
                "day_start_equity": baseline,
                "kill_switch_latched": latched,
                "updated_at_unix": time.time(),
            }
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
            os.replace(temporary, self.path)
        return latched

    @contextmanager
    def _exclusive_lock(self):
        """Serialize same-account state writes from parallel trader processes."""
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.tell() == 0 and lock_path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
