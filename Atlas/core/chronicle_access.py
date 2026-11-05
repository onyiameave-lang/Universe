"""Bounded, process-wide Chronicle search access for Atlas."""
from __future__ import annotations

import concurrent.futures
import logging
import threading
from typing import Any, Optional

log = logging.getLogger("atlas.chronicle_access")
_MAX_ACTIVE_SEARCHES = 2
_SEARCH_SLOTS = threading.BoundedSemaphore(_MAX_ACTIVE_SEARCHES)
_SEARCH_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_ACTIVE_SEARCHES, thread_name_prefix="atlas-chronicle-search"
)


def bounded_chronicle_search(
    client: Any, *, query: str, domain: str, limit: int, requester: str,
    timeout_sec: float = 2.0, record_usage: bool = False,
) -> Optional[list]:
    """Search Chronicle within a deadline and cap stuck calls to two workers.

    Python cannot kill a thread blocked in Chronicle. A timed-out worker keeps
    its slot until it actually exits, preventing repeated searches from growing
    an unbounded collection of stuck threads.
    """
    search = getattr(client, "search", None)
    if not callable(search):
        return None
    try:
        timeout = min(5.0, max(0.05, float(timeout_sec)))
    except (TypeError, ValueError):
        timeout = 2.0
    if not _SEARCH_SLOTS.acquire(blocking=False):
        log.warning("Chronicle search skipped: Atlas search capacity is exhausted")
        return None

    def _run():
        try:
            return search(query=query, domain=domain, limit=limit, requester=requester,
                          record_usage=record_usage)
        finally:
            _SEARCH_SLOTS.release()

    try:
        future = _SEARCH_POOL.submit(_run)
    except RuntimeError as exc:
        _SEARCH_SLOTS.release()
        log.warning("Chronicle search could not be scheduled: %s", type(exc).__name__)
        return None
    try:
        result = future.result(timeout=timeout)
        return result if isinstance(result, list) else None
    except concurrent.futures.TimeoutError:
        if future.cancel():
            # A cancelled task never entered _run(), so its slot is still held.
            _SEARCH_SLOTS.release()
        log.warning("Chronicle search exceeded Atlas budget of %.1fs", timeout)
        return None
    except Exception as exc:
        log.warning("Chronicle search failed: %s", type(exc).__name__)
        return None
