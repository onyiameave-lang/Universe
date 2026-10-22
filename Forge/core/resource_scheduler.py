"""
Forge - Resource Scheduler (§26.6)
===================================
Prevents training workloads from overwhelming specialist agents or live
operations.  Provides per-agent quotas, global budgets, priority classes,
concurrency limits, queue backpressure, cancellation, deadline handling,
and starvation prevention.

This module is internal to Forge.  Specialist agents interact through the
TrainingRequest interface, not this scheduler directly.
"""

import logging
import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from Forge.core.training_request import (
    RequestPriority,
    RequestStatus,
    TrainingRequest,
)

log = logging.getLogger("forge.scheduler")


# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

class AgentQuota(BaseModel):
    """Per-agent resource limits."""
    agent_name: str
    max_concurrent: int = 2
    max_queued: int = 10
    max_daily_requests: int = 50
    max_compute_units_per_day: float = 1000.0
    priority_ceiling: RequestPriority = RequestPriority.URGENT


class SchedulerConfig(BaseModel):
    """Global scheduler configuration."""
    global_max_concurrent: int = 4
    global_max_queued: int = 50
    default_max_concurrent_per_agent: int = 2
    default_max_queued_per_agent: int = 10
    default_max_daily_per_agent: int = 50
    starvation_promotion_seconds: float = 300.0  # 5 minutes
    default_timeout_seconds: float = 3600.0      # 1 hour
    reserved_operational_capacity: float = 0.25   # Reserve 25% for live ops
    agent_quotas: Dict[str, AgentQuota] = Field(default_factory=dict)


# ==============================================================================
# 2. QUEUE ENTRY
# ==============================================================================

class QueueEntry(BaseModel):
    """Internal representation of a queued training request."""
    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str
    request_digest: str = ""
    agent_name: str
    priority: RequestPriority
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deadline: Optional[datetime] = None
    timeout_seconds: Optional[float] = None
    started_at: Optional[datetime] = None
    status: str = "queued"  # queued, running, terminal, timed_out, cancellation_requested
    compute_units_used: float = 0.0
    timeout_reported: bool = False


# ==============================================================================
# 3. SCHEDULING DECISIONS
# ==============================================================================

class SchedulingDecision(BaseModel):
    """Result of a scheduling attempt."""
    allowed: bool
    reason: str = ""
    queue_position: Optional[int] = None
    estimated_wait_seconds: Optional[float] = None


class SchedulerSnapshot(BaseModel):
    """Observable state of the scheduler."""
    global_running: int = 0
    global_queued: int = 0
    global_max_concurrent: int = 0
    per_agent: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    priority_distribution: Dict[str, int] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ==============================================================================
# 4. PRIORITY ORDERING
# ==============================================================================

_PRIORITY_ORDER = {
    RequestPriority.BACKGROUND: 0,
    RequestPriority.NORMAL: 1,
    RequestPriority.HIGH: 2,
    RequestPriority.URGENT: 3,
}


def _priority_key(entry: QueueEntry) -> tuple:
    """Sort key: higher priority first, then FIFO within same priority.

    Starvation prevention: entries waiting longer than the configured
    threshold get promoted one priority level (handled in dequeue).
    """
    return (-_PRIORITY_ORDER.get(entry.priority, 1), entry.submitted_at.timestamp())


# ==============================================================================
# 5. RESOURCE SCHEDULER
# ==============================================================================

class ResourceScheduler:
    """
    Manages resource allocation for Forge training workloads.

    Thread-safe.  All mutations are protected by a single lock to prevent
    races between concurrent requests from different agents.

    Does NOT assume physical infrastructure supports true isolation --
    enforcement is logical (concurrency limits and queue management).
    """

    def __init__(self, config: Optional[SchedulerConfig] = None):
        self._config = config or SchedulerConfig()
        self._lock = threading.Lock()

        # Active state
        self._queue: List[QueueEntry] = []
        self._running: Dict[str, QueueEntry] = {}   # request_id -> entry
        self._completed: List[QueueEntry] = []

        # Per-agent daily counters (reset daily)
        self._daily_counts: Dict[str, int] = defaultdict(int)
        self._daily_compute: Dict[str, float] = defaultdict(float)
        self._daily_reset_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Cancelled request IDs (prevent duplicate jobs on timeout/failure)
        self._cancelled: Set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self, request: TrainingRequest, *, count_daily: bool = True
    ) -> SchedulingDecision:
        """Attempt to enqueue a training request.

        Returns a SchedulingDecision indicating whether the request was
        accepted and its queue position.
        """
        with self._lock:
            self._rotate_daily_counters()

            agent = request.requestor.agent_name
            quota = self._get_quota(agent)
            priority = request.budget.priority

            # Check priority ceiling
            if _PRIORITY_ORDER.get(priority, 1) > _PRIORITY_ORDER.get(quota.priority_ceiling, 3):
                return SchedulingDecision(
                    allowed=False,
                    reason=f"Agent '{agent}' priority ceiling is {quota.priority_ceiling.value}; "
                           f"requested {priority.value}",
                )

            # Check daily request limit
            if count_daily and self._daily_counts[agent] >= quota.max_daily_requests:
                return SchedulingDecision(
                    allowed=False,
                    reason=f"Agent '{agent}' exceeded daily request limit ({quota.max_daily_requests})",
                )

            # Check queue depth
            agent_queued = sum(1 for e in self._queue if e.agent_name == agent)
            if agent_queued >= quota.max_queued:
                return SchedulingDecision(
                    allowed=False,
                    reason=f"Agent '{agent}' queue full ({quota.max_queued} queued)",
                )

            # Check global queue depth
            if len(self._queue) >= self._config.global_max_queued:
                return SchedulingDecision(
                    allowed=False,
                    reason=f"Global queue full ({self._config.global_max_queued} queued)",
                )

            # Prevent duplicate work while an equivalent request is active.
            request_digest = request.content_digest()
            for existing in list(self._queue) + list(self._running.values()):
                if existing.request_id == request.request_id:
                    return SchedulingDecision(
                        allowed=False,
                        reason=f"Request {request.request_id} already submitted",
                    )
                if existing.request_digest and existing.request_digest == request_digest:
                    return SchedulingDecision(
                        allowed=False,
                        reason=(
                            f"Equivalent request {request.request_id} is already active as "
                            f"{existing.request_id}"
                        ),
                    )

            # Check deadline
            deadline = request.budget.deadline
            if deadline and deadline <= datetime.now(timezone.utc):
                return SchedulingDecision(
                    allowed=False,
                    reason="Request deadline has already passed",
                )

            # Accept the request
            entry = QueueEntry(
                request_id=request.request_id,
                request_digest=request_digest,
                agent_name=agent,
                priority=priority,
                deadline=deadline,
                timeout_seconds=(
                    request.budget.max_duration_seconds
                    or self._config.default_timeout_seconds
                ),
            )
            self._queue.append(entry)
            self._queue.sort(key=_priority_key)
            if count_daily:
                self._daily_counts[agent] += 1

            position = next(
                i for i, e in enumerate(self._queue) if e.request_id == request.request_id
            )

            log.info(
                "Request %s from '%s' queued at position %d (priority: %s)",
                request.request_id, agent, position, priority.value,
            )

            return SchedulingDecision(
                allowed=True,
                queue_position=position,
                estimated_wait_seconds=position * 60.0,  # Rough estimate
            )

    @property
    def global_max_concurrent(self) -> int:
        return self._config.global_max_concurrent

    def max_concurrent_for(self, agent_name: str) -> int:
        return self._get_quota(agent_name).max_concurrent

    def max_daily_for(self, agent_name: str) -> int:
        return self._get_quota(agent_name).max_daily_requests

    def max_queued_for(self, agent_name: str) -> int:
        return self._get_quota(agent_name).max_queued

    @property
    def global_max_queued(self) -> int:
        return self._config.global_max_queued

    @property
    def policy_fingerprint(self) -> str:
        payload = json.dumps(
            self._config.model_dump(mode="json"),
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def timeout_for(self, request: TrainingRequest) -> float:
        return float(request.budget.max_duration_seconds or self._config.default_timeout_seconds)

    def restore_queued(
        self, request: TrainingRequest
    ) -> tuple[bool, Optional[str]]:
        """Restore a durable queue entry and explain why it cannot be restored."""
        with self._lock:
            request_digest = request.content_digest()
            if any(entry.request_id == request.request_id
                   for entry in self._queue + list(self._running.values())):
                return False, "request_id_already_active"
            if any(
                entry.request_digest and entry.request_digest == request_digest
                for entry in self._queue + list(self._running.values())
            ):
                return False, "equivalent_request_already_active"
            deadline = request.budget.deadline
            if deadline and deadline <= datetime.now(timezone.utc):
                return False, "deadline_expired"
            entry = QueueEntry(
                request_id=request.request_id,
                request_digest=request_digest,
                agent_name=request.requestor.agent_name,
                priority=request.budget.priority,
                submitted_at=request.submitted_at,
                deadline=deadline,
                timeout_seconds=(
                    request.budget.max_duration_seconds
                    or self._config.default_timeout_seconds
                ),
            )
            self._queue.append(entry)
            self._queue.sort(key=_priority_key)
            return True, None

    def return_to_queue(self, entry: QueueEntry) -> None:
        """Return a locally dequeued item when the durable cross-process gate is full."""
        with self._lock:
            current = self._running.pop(entry.request_id, None)
            if current is None:
                return
            if current.request_id in self._cancelled:
                current.status = "cancelled"
                self._cancelled.discard(current.request_id)
                self._completed.append(current)
                return
            current.status = "queued"
            current.started_at = None
            if not any(item.request_id == current.request_id for item in self._queue):
                self._queue.append(current)
                self._queue.sort(key=_priority_key)

    def dequeue_next(self) -> Optional[QueueEntry]:
        """Pop the highest-priority eligible request for execution.

        Returns None if no request can run (global or per-agent concurrency
        limits reached).  Applies starvation prevention by promoting long-
        waiting entries.
        """
        with self._lock:
            if len(self._running) >= self._config.global_max_concurrent:
                return None

            self._apply_starvation_prevention()
            self._expire_deadlines()
            self._queue.sort(key=_priority_key)
            retained = []
            for entry in self._queue:
                if entry.request_id in self._cancelled:
                    entry.status = "cancelled"
                    self._cancelled.discard(entry.request_id)
                    self._completed.append(entry)
                else:
                    retained.append(entry)
            self._queue = retained

            for i, entry in enumerate(self._queue):
                quota = self._get_quota(entry.agent_name)
                agent_running = sum(
                    1 for e in self._running.values() if e.agent_name == entry.agent_name
                )
                if agent_running >= quota.max_concurrent:
                    continue

                # Found an eligible entry
                self._queue.pop(i)
                entry.status = "running"
                entry.started_at = datetime.now(timezone.utc)
                self._running[entry.request_id] = entry
                log.info(
                    "Dispatching request %s for agent '%s' (running: %d/%d global)",
                    entry.request_id, entry.agent_name,
                    len(self._running), self._config.global_max_concurrent,
                )
                return entry

            return None

    def complete(self, request_id: str, compute_units: float = 0.0) -> None:
        """Mark a running request as completed."""
        with self._lock:
            entry = self._running.pop(request_id, None)
            if entry is None:
                log.warning("Attempted to complete unknown request %s", request_id)
                return
            entry.status = "completed"
            entry.compute_units_used = compute_units
            self._cancelled.discard(request_id)
            self._daily_compute[entry.agent_name] += compute_units
            self._completed.append(entry)
            log.info("Request %s completed (%.1f compute units)", request_id, compute_units)

    def fail(self, request_id: str, compute_units: float = 0.0) -> None:
        """Mark a running request as failed (does NOT trigger a retry)."""
        with self._lock:
            entry = self._running.pop(request_id, None)
            if entry is None:
                log.warning("Attempted to fail unknown request %s", request_id)
                return
            entry.status = "failed"
            entry.compute_units_used = compute_units
            self._cancelled.discard(request_id)
            self._daily_compute[entry.agent_name] += compute_units
            self._completed.append(entry)
            log.info("Request %s failed (%.1f compute units)", request_id, compute_units)

    def cancel(self, request_id: str) -> bool:
        """Cancel queued work or request cancellation for a running worker.

        Running adapter code cannot be forcibly stopped. Keep it in the running
        set so its capacity remains reserved until it exits or an operator has
        independently verified worker termination and calls
        ``release_terminated``.
        """
        with self._lock:
            # Remove from queue
            for i, entry in enumerate(self._queue):
                if entry.request_id == request_id:
                    entry.status = "cancelled"
                    self._queue.pop(i)
                    self._cancelled.discard(request_id)
                    self._completed.append(entry)
                    log.info("Request %s cancelled (was queued)", request_id)
                    return True
            # A cancellation request is observable but does not release capacity.
            if request_id in self._running:
                self._cancelled.add(request_id)
                entry = self._running[request_id]
                entry.status = "cancellation_requested"
                log.info("Cancellation requested for running request %s", request_id)
                return True
            return False

    def release_terminated(
        self, request_id: str, compute_units: Optional[float] = None
    ) -> bool:
        """Release a slot after the caller has verified the worker stopped.

        This is intentionally separate from ``cancel``: a timeout or cancel
        request alone is not evidence that adapter code has stopped running.
        When omitted, compute usage is conservatively recorded as elapsed
        wall time, matching the scheduler's normal completion accounting.
        """
        with self._lock:
            entry = self._running.pop(request_id, None)
            if entry is None:
                return False
            if compute_units is None:
                compute_units = (
                    max(0.0, (datetime.now(timezone.utc) - entry.started_at).total_seconds())
                    if entry.started_at else 0.0
                )
            if (
                isinstance(compute_units, bool)
                or not isinstance(compute_units, (int, float))
                or not 0 <= compute_units < float("inf")
            ):
                self._running[request_id] = entry
                raise ValueError("compute_units must be a finite non-negative number")
            entry.status = "aborted"
            entry.compute_units_used = float(compute_units)
            self._cancelled.discard(request_id)
            self._daily_compute[entry.agent_name] += entry.compute_units_used
            self._completed.append(entry)
            log.info(
                "Released request %s after worker termination was verified (%.1f compute units)",
                request_id, entry.compute_units_used,
            )
            return True

    def check_timeouts(self) -> List[str]:
        """Report newly observed timeouts without releasing worker capacity.

        Python cannot safely terminate blocked adapter code. A timeout is a
        signal for operators; the request remains in ``_running`` until its
        worker returns or termination is verified and reconciled.
        """
        timed_out = []
        with self._lock:
            now = datetime.now(timezone.utc)
            for request_id, entry in list(self._running.items()):
                if entry.started_at and entry.timeout_seconds and not entry.timeout_reported:
                    elapsed = (now - entry.started_at).total_seconds()
                    if elapsed > entry.timeout_seconds:
                        entry.status = "timed_out"
                        entry.timeout_reported = True
                        timed_out.append(request_id)
                        log.warning(
                            "Request %s timed out after %.0fs (limit: %.0fs)",
                            request_id, elapsed, entry.timeout_seconds,
                        )
        return timed_out

    def snapshot(self) -> SchedulerSnapshot:
        """Return an observable snapshot of the scheduler state."""
        with self._lock:
            per_agent: Dict[str, Dict[str, Any]] = {}
            all_agents = set()
            for entry in self._queue:
                all_agents.add(entry.agent_name)
            for entry in self._running.values():
                all_agents.add(entry.agent_name)

            for agent in all_agents:
                quota = self._get_quota(agent)
                per_agent[agent] = {
                    "running": sum(1 for e in self._running.values() if e.agent_name == agent),
                    "queued": sum(1 for e in self._queue if e.agent_name == agent),
                    "max_concurrent": quota.max_concurrent,
                    "daily_requests": self._daily_counts.get(agent, 0),
                    "daily_compute_units": self._daily_compute.get(agent, 0.0),
                }

            priority_dist = defaultdict(int)
            for entry in self._queue:
                priority_dist[entry.priority.value] += 1

            return SchedulerSnapshot(
                global_running=len(self._running),
                global_queued=len(self._queue),
                global_max_concurrent=self._config.global_max_concurrent,
                per_agent=per_agent,
                priority_distribution=dict(priority_dist),
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_quota(self, agent_name: str) -> AgentQuota:
        """Resolve the quota for an agent, falling back to defaults."""
        if agent_name in self._config.agent_quotas:
            return self._config.agent_quotas[agent_name]
        return AgentQuota(
            agent_name=agent_name,
            max_concurrent=self._config.default_max_concurrent_per_agent,
            max_queued=self._config.default_max_queued_per_agent,
            max_daily_requests=self._config.default_max_daily_per_agent,
        )

    def _rotate_daily_counters(self) -> None:
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_counts.clear()
            self._daily_compute.clear()
            self._daily_reset_date = today

    def _apply_starvation_prevention(self) -> None:
        """Promote entries waiting longer than the starvation threshold."""
        now = datetime.now(timezone.utc)
        threshold = self._config.starvation_promotion_seconds
        for entry in self._queue:
            wait = (now - entry.submitted_at).total_seconds()
            if wait > threshold and entry.priority != RequestPriority.URGENT:
                current = _PRIORITY_ORDER.get(entry.priority, 1)
                promoted = min(current + 1, _PRIORITY_ORDER[RequestPriority.HIGH])
                for p, v in _PRIORITY_ORDER.items():
                    if v == promoted:
                        if p != entry.priority:
                            log.info(
                                "Promoting request %s from %s to %s (waited %.0fs)",
                                entry.request_id, entry.priority.value, p.value, wait,
                            )
                            entry.priority = p
                        break

    def _expire_deadlines(self) -> None:
        """Remove entries whose deadlines have passed."""
        now = datetime.now(timezone.utc)
        expired = []
        for i, entry in enumerate(self._queue):
            if entry.deadline and entry.deadline <= now:
                expired.append(i)
                log.warning("Request %s expired (deadline passed)", entry.request_id)
        for i in reversed(expired):
            entry = self._queue.pop(i)
            entry.status = "timed_out"
            self._cancelled.discard(entry.request_id)
            self._completed.append(entry)
