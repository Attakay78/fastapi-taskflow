"""Periodic task scheduler for fastapi-taskflow.

Drives the schedule loop that fires registered tasks at fixed intervals or
on cron expressions. Each fired task runs through the same execute_task path
as a manually enqueued task, producing a normal TaskRecord visible in the
dashboard and API.

When a shared backend is configured, a distributed lock is acquired before
each fire so only one instance fires the task in a multi-instance deployment.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend
    from .manager import TaskManager
    from .models import TaskConfig

logger = logging.getLogger(__name__)


def _next_every(every: float, from_time: datetime) -> datetime:
    """Return the next run time for an interval-based schedule.

    Args:
        every: Interval in seconds.
        from_time: The reference time to advance from.

    Returns:
        UTC datetime of the next run.
    """
    return from_time + timedelta(seconds=every)


def _next_cron(cron: str, from_time: datetime, timezone_name: str = "UTC") -> datetime:
    """Return the next run time for a cron expression after *from_time*.

    Args:
        cron: Five-field cron expression (e.g. ``"0 * * * *"``).
        from_time: The reference time to start from.
        timezone_name: IANA timezone name (e.g. ``"America/New_York"``).
            Defaults to ``"UTC"``.

    Returns:
        UTC datetime of the next matching cron slot.

    Raises:
        ImportError: If ``croniter`` is not installed.
    """
    try:
        from croniter import croniter  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "Cron-based schedules require the 'croniter' package. "
            "Install it with: pip install 'fastapi-taskflow[scheduler]'"
        ) from exc

    if timezone_name != "UTC":
        try:
            import zoneinfo
        except ImportError:
            from backports import zoneinfo  # type: ignore[no-redef, import-untyped]
        tz = zoneinfo.ZoneInfo(timezone_name)
        from_local = from_time.astimezone(tz)
        next_local = croniter(cron, from_local).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        return next_local.astimezone(timezone.utc)

    return croniter(cron, from_time).get_next(datetime).replace(tzinfo=timezone.utc)


@dataclass
class ScheduledEntry:
    """One registered periodic task.

    You never instantiate this directly. It is created by
    ``@task_manager.schedule()`` and stored in the
    :class:`~fastapi_taskflow.periodic.PeriodicScheduler`.

    Attributes:
        func: The task function. Already registered in the
            :class:`~fastapi_taskflow.registry.TaskRegistry`.
        config: Execution settings (retries, delay, backoff).
        every: Interval in seconds between runs. Mutually exclusive
            with *cron*.
        cron: Five-field cron expression. Mutually exclusive with *every*.
        run_on_startup: When ``True``, fire on the first scheduler tick
            instead of waiting for the first interval or cron slot.
        timezone: IANA timezone name used when evaluating *cron* expressions.
            Ignored when *every* is used. Defaults to ``"UTC"``.
        next_run: UTC datetime of the next scheduled execution. Set by
            :meth:`~PeriodicScheduler.start`.
    """

    func: Callable
    config: "TaskConfig"
    every: float | None
    cron: str | None
    run_on_startup: bool
    timezone: str = "UTC"
    next_run: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def compute_next(self, from_time: datetime) -> datetime:
        """Return the next run time after *from_time*.

        Args:
            from_time: Reference time to advance from.

        Returns:
            UTC datetime of the next scheduled run.
        """
        if self.every is not None:
            return _next_every(self.every, from_time)
        assert self.cron is not None
        return _next_cron(self.cron, from_time, self.timezone)

    def __lt__(self, other: "ScheduledEntry") -> bool:
        """Heap ordering by next_run."""
        return self.next_run < other.next_run


class PeriodicScheduler:
    """Fires registered schedules at their configured intervals.

    Uses a min-heap ordered by ``next_run`` so the loop always sleeps
    until the nearest deadline across all entries. Wakes exactly when
    the next task is due rather than polling every second.

    When a shared backend is configured, a distributed lock is acquired
    before each fire to ensure only one instance fires each entry in a
    multi-instance deployment.

    Args:
        task_manager: The :class:`~fastapi_taskflow.manager.TaskManager`
            that holds the registry, store, logger, and optional backend.
        entries: The :class:`ScheduledEntry` objects to drive.
        backend: Optional backend used for distributed locking. When
            ``None``, all instances fire each entry independently.
    """

    def __init__(
        self,
        task_manager: "TaskManager",
        entries: list[ScheduledEntry],
        backend: "Optional[SnapshotBackend]" = None,
    ) -> None:
        self._task_manager = task_manager
        self._entries = entries
        self._backend = backend
        self._bg_task: asyncio.Task | None = None
        self._wake: asyncio.Event = asyncio.Event()
        self._heap: list[ScheduledEntry] = []

    @property
    def entries(self) -> list[ScheduledEntry]:
        """Read-only snapshot of the registered schedule entries."""
        return list(self._entries)

    def start(self) -> None:
        """Start the periodic scheduling loop.

        Initialises ``next_run`` for each entry, builds the min-heap,
        then launches the loop as a background ``asyncio.Task``. Called
        by :meth:`~fastapi_taskflow.manager.TaskManager.startup`.
        """
        now = datetime.now(timezone.utc)
        for entry in self._entries:
            if entry.run_on_startup:
                entry.next_run = now
            else:
                entry.next_run = entry.compute_next(now)
        self._heap = list(self._entries)
        heapq.heapify(self._heap)
        self._bg_task = asyncio.create_task(self._run())

    def stop(self) -> None:
        """Cancel the scheduling loop.

        Called by :meth:`~fastapi_taskflow.manager.TaskManager.shutdown`.
        """
        if self._bg_task is not None:
            self._bg_task.cancel()
            self._bg_task = None

    def _add_entry(self, entry: ScheduledEntry) -> None:
        """Add a new entry to the heap at runtime and wake the loop.

        Used when ``@task_manager.schedule()`` is called after the
        scheduler has already started.
        """
        self._entries.append(entry)
        heapq.heappush(self._heap, entry)
        self._wake.set()

    async def _run(self) -> None:
        while True:
            now = datetime.now(timezone.utc)

            # Fire all entries that are due.
            while self._heap and self._heap[0].next_run <= now:
                entry = heapq.heappop(self._heap)
                await self._fire(entry, now)
                heapq.heappush(self._heap, entry)

            # Sleep until the next due entry or until woken by a new registration.
            if self._heap:
                sleep_for = max(
                    0.001,
                    (
                        self._heap[0].next_run - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=sleep_for)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass

    async def _fire(self, entry: ScheduledEntry, now: datetime) -> None:
        """Fire one scheduled entry if the distributed lock is acquired.

        Advances ``next_run`` immediately before the lock attempt so that
        a slow acquisition never causes back-to-back firings.

        Args:
            entry: The schedule entry to fire.
            now: The current UTC time used to compute the next run.
        """
        entry.next_run = entry.compute_next(now)

        lock_key = f"schedule:{entry.func.__name__}"
        # Hold the lock for one full interval so a second instance cannot
        # fire the same entry while the first is still running.
        # max(1, ...) prevents sub-second intervals from producing a 0s TTL.
        lock_ttl = max(1, int(entry.every if entry.every is not None else 60))

        if self._backend is not None:
            acquired = await self._backend.acquire_schedule_lock(lock_key, lock_ttl)
            if not acquired:
                logger.debug(
                    "fastapi-taskflow: schedule lock not acquired for %s, skipping.",
                    entry.func.__name__,
                )
                return

        task_id = str(uuid.uuid4())

        scheduler = self._task_manager._scheduler
        backend = scheduler._backend if scheduler is not None else None
        on_success = scheduler.flush_one if scheduler is not None else None

        self._task_manager.store.create(
            task_id,
            entry.func.__name__,
            (),
            {},
            source="scheduled",
        )

        from .executor import execute_task

        asyncio.create_task(
            execute_task(
                entry.func,
                task_id,
                entry.config,
                self._task_manager.store,
                (),
                {},
                backend=backend,
                on_success=on_success,
                logger=self._task_manager.logger,
                encryptor=self._task_manager.fernet,
                semaphore=self._task_manager._task_semaphore,
                sync_executor=self._task_manager._sync_executor,
            )
        )

        logger.info(
            "fastapi-taskflow: fired scheduled task %s (%s)",
            task_id,
            entry.func.__name__,
        )
