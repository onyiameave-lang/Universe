"""Conservative per-scan accounting for account-wide open positions."""
from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from pathlib import Path


_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def account_position_lock():
    """Serialize Oracle processes while they check the account cap and submit.

    This closes the race where two trader processes both see the same free
    slot and submit orders before either process can observe the other's fill.
    The lock file contains no account credentials; its suffix is a hash of the
    configured account identifier.
    """
    identity = (os.getenv("CTRADER_ACCOUNT_ID") or os.getenv("MT5_LOGIN") or "default").strip()
    account_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    lock_path = Path(__file__).resolve().parents[1] / "memory" / f"open_position_limit_{account_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _THREAD_LOCK:
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
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


class OpenPositionBudget:
    """Track broker-observed positions plus fills during one scan cycle.

    The count is monotonic within a scan: a position disappearing from a later
    snapshot does not create a fresh slot until the next cycle. This avoids
    over-entry while the broker and local manager are reconciling a close.
    """

    def __init__(self, max_positions: int, observed_open_positions: int = 0) -> None:
        self.max_positions = max(0, int(max_positions))
        self._open_count = max(0, int(observed_open_positions))
        self._lock = threading.RLock()

    @property
    def open_count(self) -> int:
        with self._lock:
            return self._open_count

    @property
    def full(self) -> bool:
        with self._lock:
            return self._open_count >= self.max_positions

    def reconcile(self, observed_open_positions: int) -> int:
        with self._lock:
            self._open_count = max(self._open_count, max(0, int(observed_open_positions)))
            return self._open_count

    def reserve(self) -> bool:
        """Reserve one slot before submitting an order to the broker."""
        with self._lock:
            if self._open_count >= self.max_positions:
                return False
            self._open_count += 1
            return True

    def release_rejected_order(self) -> int:
        """Release a reservation only after a definite non-execution result."""
        with self._lock:
            self._open_count = max(0, self._open_count - 1)
            return self._open_count
