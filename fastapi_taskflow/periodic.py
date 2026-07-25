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
    from .models import ScheduledOnce, TaskConfig

logger = logging.getLogger(__name__)

#: How far ahead one-off entries are pulled into memory. Entries due beyond
#: this stay in the backend until a later refill, so the heap tracks the
#: near-term set rather than every pending schedule.
DEFAULT_HORIZON = 300.0

#: How often the horizon is refilled. Must stay below DEFAULT_HORIZON so no
#: entry can become due in the gap between two refills.
DEFAULT_REFILL_INTERVAL = 150.0


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
        once: When set, this entry is a **one-off** pulled from the backend by
            :meth:`~PeriodicScheduler._refill` rather than a recurring
            registration. It fires a single time and is dropped from the heap
            instead of being re-pushed with a recomputed *next_run*.
        cancelled: Tombstone flag. ``heapq`` cannot remove an arbitrary
            element, so cancelling or replacing a one-off marks the heap entry
            dead and it is skipped when it surfaces.
    """

    func: Callable
    config: "TaskConfig"
    every: float | None
    cron: str | None
    run_on_startup: bool
    timezone: str = "UTC"
    next_run: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    once: "ScheduledOnce | None" = None
    cancelled: bool = False

    @property
    def recurring(self) -> bool:
        """``True`` for interval/cron entries, ``False`` for one-off etas."""
        return self.once is None

    def compute_next(self, from_time: datetime) -> datetime:
        """Return the next run time after *from_time*.

        Args:
            from_time: Reference time to advance from.

        Returns:
            UTC datetime of the next scheduled run.

        Raises:
            ValueError: If called on a one-off entry, which by definition has
                no next run.
        """
        if self.once is not None:
            raise ValueError("compute_next() is not meaningful for a one-off entry.")
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
        horizon: float = DEFAULT_HORIZON,
        refill_interval: float = DEFAULT_REFILL_INTERVAL,
    ) -> None:
        self._task_manager = task_manager
        self._entries = entries
        self._backend = backend
        self._bg_task: asyncio.Task | None = None
        self._wake: asyncio.Event = asyncio.Event()
        self._heap: list[ScheduledEntry] = []
        self._horizon = horizon
        self._refill_interval = refill_interval
        # run_key -> live heap entry, for replace and cancel by key.
        self._one_offs: dict[str, ScheduledEntry] = {}
        self._next_refill: datetime = datetime.now(timezone.utc)
        self._supports_one_off = bool(
            backend is not None and backend.supports_scheduled_once
        )

    @property
    def entries(self) -> list[ScheduledEntry]:
        """Read-only snapshot of the registered *recurring* schedule entries.

        One-off entries are deliberately excluded: they live only in the heap
        and ``_one_offs``, never in ``_entries``. The pending one-off set is
        unbounded (one per open deadline in the calling application), and this
        property is serialised per client on every dashboard SSE tick.
        """
        return list(self._entries)

    @property
    def pending_one_off_count(self) -> int:
        """Number of one-off entries currently loaded in the horizon window.

        This is the in-memory count, not the total pending in the backend.
        """
        return len(self._one_offs)

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

    def add_one_off(self, once: "ScheduledOnce") -> bool:
        """Push a one-off entry straight into the live heap.

        Called by :meth:`~fastapi_taskflow.manager.TaskManager.schedule_once`
        when *once* falls inside the current horizon, so an eta due sooner
        than the next refill still fires on time. Entries beyond the horizon
        are left for a later refill to pick up.

        Returns:
            ``True`` if the entry was added to the heap, ``False`` if its
            function is not registered in this process.
        """
        result = self._task_manager.registry.get_by_name(once.func_name)
        if result is None:
            logger.warning(
                "fastapi-taskflow: cannot arm one-off %s — function %r is not "
                "registered in this process. It stays in the backend and will "
                "be retried on the next refill.",
                once.run_key,
                once.func_name,
            )
            return False

        # Replacing an existing key: tombstone the old heap entry rather than
        # trying to remove it, since heapq has no delete.
        existing = self._one_offs.pop(once.run_key, None)
        if existing is not None:
            existing.cancelled = True

        func, config = result
        entry = ScheduledEntry(
            func=func,
            config=config,
            every=None,
            cron=None,
            run_on_startup=False,
            next_run=once.fire_at,
            once=once,
        )
        self._one_offs[once.run_key] = entry
        heapq.heappush(self._heap, entry)
        self._wake.set()
        return True

    def cancel_one_off(self, run_key: str) -> None:
        """Tombstone the in-memory heap entry for *run_key*, if armed.

        The backend row is deleted separately by
        :meth:`~fastapi_taskflow.manager.TaskManager.cancel_scheduled`; this
        only clears the in-process copy.
        """
        entry = self._one_offs.pop(run_key, None)
        if entry is not None:
            entry.cancelled = True

    async def _refill(self, now: datetime) -> None:
        """Pull the next horizon window of one-off entries into the heap.

        Entries already armed under the same ``run_key`` are skipped unless
        their ``fire_at`` changed, which happens when another instance
        rescheduled them.
        """
        if self._backend is None:
            return
        horizon_end = now + timedelta(seconds=self._horizon)
        try:
            due = await self._backend.load_due(horizon_end)
        except Exception:
            logger.exception(
                "fastapi-taskflow: one-off refill failed — will retry in %.0fs.",
                self._refill_interval,
            )
            return

        for once in due:
            existing = self._one_offs.get(once.run_key)
            if existing is not None and existing.next_run == once.fire_at:
                continue  # already armed at this time
            self.add_one_off(once)

    async def _run(self) -> None:
        """Main scheduling loop. Runs for the lifetime of the application.

        Pops entries from the min-heap whose ``next_run`` has passed and fires
        each one. Recurring entries are pushed back with an updated
        ``next_run``; one-offs are dropped. Sleeps until the nearest upcoming
        deadline, capped by the next horizon refill, or until woken by a new
        registration.
        """
        while True:
            now = datetime.now(timezone.utc)

            if self._supports_one_off and now >= self._next_refill:
                await self._refill(now)
                self._next_refill = now + timedelta(seconds=self._refill_interval)

            # Fire all entries that are due.
            while self._heap and self._heap[0].next_run <= now:
                entry = heapq.heappop(self._heap)
                if entry.cancelled:
                    continue  # tombstone from a cancel or reschedule
                if entry.recurring:
                    await self._fire(entry, now)
                    heapq.heappush(self._heap, entry)
                else:
                    assert entry.once is not None
                    self._one_offs.pop(entry.once.run_key, None)
                    await self._fire_once(entry.once)

            # Sleep until the nearest deadline or the next refill, whichever
            # comes first, unless woken early by a new registration.
            now = datetime.now(timezone.utc)
            deadlines: list[datetime] = []
            if self._heap:
                deadlines.append(self._heap[0].next_run)
            if self._supports_one_off:
                deadlines.append(self._next_refill)
            sleep_for = (
                max(0.001, (min(deadlines) - now).total_seconds()) if deadlines else 1.0
            )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=sleep_for)
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
        executor_obj = self._task_manager._resolve_executor(entry.func, entry.config)
        run_queue = entry.config.queue or "default"

        self._task_manager.store.create(
            task_id,
            entry.func.__name__,
            (),
            {},
            source="scheduled",
            executor=executor_obj.name,
            queue=run_queue,
        )
        self._dispatch(
            entry.func,
            entry.config,
            task_id,
            (),
            {},
            executor_obj,
            run_queue,
            entry.config.priority,
        )

        logger.info(
            "fastapi-taskflow: fired scheduled task %s (%s)",
            task_id,
            entry.func.__name__,
        )

    async def _fire_once(self, once: "ScheduledOnce") -> None:
        """Fire a one-off entry, claiming it first so it runs exactly once.

        Unlike :meth:`_fire`, the claim is an atomic delete rather than a TTL
        lock: a one-off has no interval from which to derive a lock lifetime,
        and "exactly once" is stronger than "at most once per TTL".
        """
        if self._backend is not None:
            try:
                claimed = await self._backend.claim_scheduled(once.run_key)
            except Exception:
                logger.exception(
                    "fastapi-taskflow: failed to claim one-off %s — skipping this "
                    "firing. It stays in the backend for a later refill.",
                    once.run_key,
                )
                return
            if not claimed:
                logger.debug(
                    "fastapi-taskflow: one-off %s already claimed by another "
                    "instance, skipping.",
                    once.run_key,
                )
                return

        result = self._task_manager.registry.get_by_name(once.func_name)
        if result is None:
            logger.warning(
                "fastapi-taskflow: cannot fire one-off %s — function %r is no "
                "longer registered. The entry has been consumed and will not "
                "fire again.",
                once.run_key,
                once.func_name,
            )
            return

        func, config = result
        task_id = str(uuid.uuid4())
        executor_obj = self._task_manager._resolve_executor(func, config)
        run_queue = once.queue or config.queue or "default"
        priority = once.priority if once.priority is not None else config.priority

        self._task_manager.store.create(
            task_id,
            func.__name__,
            once.args,
            once.kwargs,
            source="scheduled",
            executor=executor_obj.name,
            queue=run_queue,
            idempotency_key=once.idempotency_key,
            tags=once.tags,
            encrypted_payload=once.encrypted_payload,
            priority=priority,
        )
        self._dispatch(
            func,
            config,
            task_id,
            once.args,
            once.kwargs,
            executor_obj,
            run_queue,
            priority,
        )

        logger.info(
            "fastapi-taskflow: fired one-off task %s (%s) for run_key %s",
            task_id,
            func.__name__,
            once.run_key,
        )

    def _dispatch(
        self,
        func: Callable,
        config: "TaskConfig",
        task_id: str,
        args: tuple,
        kwargs: dict,
        executor_obj,
        run_queue: str,
        priority: "int | None",
    ) -> None:
        """Route an already-stored task record into the queue or event loop.

        Shared by :meth:`_fire` and :meth:`_fire_once` so both firing paths
        produce identical execution semantics.
        """
        from .executor import execute_task, make_background_func

        scheduler = self._task_manager._scheduler
        backend = scheduler._backend if scheduler is not None else None
        on_success = scheduler.flush_one if scheduler is not None else None

        if self._task_manager._queues:
            wrapped = make_background_func(
                func,
                task_id,
                config,
                self._task_manager.store,
                args,
                kwargs,
                backend=backend,
                on_success=on_success,
                logger=self._task_manager.logger,
                encryptor=self._task_manager.fernet,
                executor_obj=executor_obj,
                running_tasks=self._task_manager._running_tasks,
            )
            self._task_manager._get_queue(run_queue).enqueue(task_id, priority, wrapped)
        else:
            asyncio.create_task(
                execute_task(
                    func,
                    task_id,
                    config,
                    self._task_manager.store,
                    args,
                    kwargs,
                    executor_obj=executor_obj,
                    backend=backend,
                    on_success=on_success,
                    logger=self._task_manager.logger,
                    encryptor=self._task_manager.fernet,
                    running_tasks=self._task_manager._running_tasks,
                )
            )
