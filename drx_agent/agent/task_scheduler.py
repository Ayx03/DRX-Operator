"""Priority queue with global, per-target, and rate-limited task admission."""

import asyncio
import math
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class TaskPriority(IntEnum):
    EXPLOIT = 0
    RECON = 1
    LATERAL = 2
    PERSIST = 3
    REPORT = 4


@dataclass(order=True)
class ScheduledTask:
    priority: int
    task_id: str = field(compare=False)
    agent_type: str = field(compare=False)
    target: str = field(compare=False)
    description: str = field(compare=False)
    ttl: int = field(compare=False)


class TaskScheduler:
    """Single-event-loop scheduler with bounded, cancellation-safe admission."""

    def __init__(
        self,
        max_concurrent_per_target: int = 3,
        global_qps: Optional[float] = None,
        *,
        max_concurrent: int = 16,
    ):
        for name, value in (
            ("max_concurrent", max_concurrent),
            ("max_concurrent_per_target", max_concurrent_per_target),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if global_qps is not None:
            if isinstance(global_qps, bool) or not isinstance(global_qps, (int, float)):
                raise ValueError("global_qps must be finite and positive")
            try:
                global_qps = float(global_qps)
            except OverflowError as exc:
                raise ValueError("global_qps must be finite and positive") from exc
            if (
                not math.isfinite(global_qps)
                or global_qps <= 0
                or not math.isfinite(1.0 / global_qps)
            ):
                raise ValueError("global_qps must be finite and positive")
        self._queue: list[ScheduledTask] = []
        self._target_concurrency: dict[str, int] = {}
        self._active = 0
        self.max_concurrent = max_concurrent
        self.max_concurrent_per_target = max_concurrent_per_target
        self.global_qps = global_qps
        self._last_slot_ts: Optional[float] = None
        self._waiters: list[tuple[asyncio.Future[None], Optional[str]]] = []
        self._wake_timer: Optional[asyncio.TimerHandle] = None

    def _has_capacity(self, target: str) -> bool:
        return (
            self._active < self.max_concurrent
            and self.concurrency_for(target) < self.max_concurrent_per_target
        )

    def _take_slot(self, target: str) -> None:
        self._target_concurrency[target] = self.concurrency_for(target) + 1
        self._active += 1

    def _drain_waiters(self) -> None:
        if self._wake_timer is not None:
            self._wake_timer.cancel()
            self._wake_timer = None
        self._waiters = [(f, t) for f, t in self._waiters if not f.done()]
        while self._waiters:
            # A saturated target must not block unrelated targets behind it.
            selected = next(
                (
                    i for i, (_, target) in enumerate(self._waiters)
                    if target is None or self._has_capacity(target)
                ),
                None,
            )
            if selected is None:
                return
            if self.global_qps is not None and self._last_slot_ts is not None:
                delay = self._last_slot_ts + 1.0 / self.global_qps - time.monotonic()
                if delay > 0:
                    self._wake_timer = asyncio.get_running_loop().call_later(
                        delay, self._drain_waiters
                    )
                    return
            future, target = self._waiters.pop(selected)
            if target is not None:
                self._take_slot(target)
            # Timestamp grants, never reservations made before sleeping.
            self._last_slot_ts = time.monotonic()
            future.set_result(None)

    async def _admit(self, target: Optional[str]) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((future, target))
        self._drain_waiters()
        try:
            await future
        except asyncio.CancelledError:
            # Cancellation can race with a grant before the caller resumes.
            if future.done() and not future.cancelled() and target is not None:
                self.task_completed(target)
            else:
                future.cancel()
                self._drain_waiters()
            raise

    async def acquire(self, target: str) -> None:
        """Wait for capacity and QPS, then own one slot until task_completed.

        Queued and rate-limited callers hold no slots. Cancellation before
        this method returns releases any raced grant automatically.
        """
        await self._admit(target)

    async def wait_for_slot(self) -> None:
        """Wait for a serialized QPS grant without acquiring concurrency."""
        if self.global_qps is not None:
            await self._admit(None)

    def try_acquire(self, target: str) -> bool:
        """Acquire capacity without waiting or rate limiting (legacy API)."""
        if not self._has_capacity(target):
            return False
        self._take_slot(target)
        return True

    def concurrency_for(self, target: str) -> int:
        """In-flight count for *target* (0 if none)."""
        return self._target_concurrency.get(target, 0)

    def enqueue(self, task: ScheduledTask) -> None:
        """Add a task to the legacy priority queue."""
        self._queue.append(task)
        self._queue.sort(key=lambda t: t.priority)

    def dequeue(self) -> Optional[ScheduledTask]:
        """Pop the highest-priority task fitting both caps, without QPS wait."""
        for i, task in enumerate(self._queue):
            if self.try_acquire(task.target):
                return self._queue.pop(i)
        return None

    def task_completed(self, target: str) -> None:
        """Release one owned slot, ignoring unmatched completions."""
        current = self.concurrency_for(target)
        if current == 0:
            return
        if current == 1:
            del self._target_concurrency[target]
        else:
            self._target_concurrency[target] = current - 1
        self._active -= 1
        self._drain_waiters()

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def pending(self) -> int:
        """Number of tasks in the legacy queue, excluding async waiters."""
        return len(self._queue)

    def status(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "max_concurrent_per_target": self.max_concurrent_per_target,
            "active": self.active_count,
            "pending": self.pending,
            "waiting": sum(not future.done() for future, _ in self._waiters),
            "by_target": dict(self._target_concurrency),
            "global_qps": self.global_qps,
        }

    def cancel_target(self, target: str) -> int:
        """Remove legacy queued tasks; async callers cancel their own tasks."""
        before = len(self._queue)
        self._queue = [t for t in self._queue if t.target != target]
        return before - len(self._queue)

