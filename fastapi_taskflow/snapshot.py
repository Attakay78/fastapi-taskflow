"""
Snapshot scheduler for fastapi-taskflow.

Periodically persists completed tasks via a :class:`SnapshotBackend` so that
history survives across restarts.  When ``requeue_pending=True``, tasks that
had not finished when the app shut down are re-executed automatically on the
next startup.

Typical usage via :class:`~fastapi_taskflow.TaskManager`::

    # SQLite — history only (default)
    task_manager = TaskManager(snapshot_db="tasks.db")

    # SQLite + requeue unfinished tasks on startup
    task_manager = TaskManager(snapshot_db="tasks.db", requeue_pending=True)

    # Redis + requeue
    from fastapi_taskflow.backends import RedisBackend
    task_manager = TaskManager(
        snapshot_backend=RedisBackend("redis://localhost:6379/0"),
        requeue_pending=True,
    )
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .loggers.base import LifecycleEvent
from .models import TaskStatus

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend
    from .manager import TaskManager

logger = logging.getLogger(__name__)


class SnapshotScheduler:
    """Drives the periodic flush loop, startup restore, and optional requeue.

    Args:
        task_manager: The :class:`~fastapi_taskflow.manager.TaskManager` whose
            store will be snapshotted.
        backend: Any :class:`~fastapi_taskflow.backends.SnapshotBackend`
            implementation (SQLite, Redis, or custom).
        interval: How often (seconds) to flush completed tasks to the backend.
            Default is 60 seconds.
        requeue_pending: When ``True``:

            * On shutdown -- tasks still ``pending`` or ``running`` are saved
              to the backend's pending store. Running tasks are treated as
              pending since they did not complete.
            * On startup -- those tasks are loaded and re-dispatched. Tasks
              whose function is no longer registered are skipped with a warning.
              The pending store is cleared after all tasks are dispatched.
    """

    def __init__(
        self,
        task_manager: "TaskManager",
        backend: "SnapshotBackend | None" = None,
        interval: float = 60.0,
        requeue_pending: bool = False,
        retention_days: float | None = None,
        # Legacy keyword kept for backwards-compatibility.
        db_path: str | None = None,
    ) -> None:
        if backend is None:
            if db_path is None:
                raise ValueError("Provide either 'backend' or 'db_path'.")
            from .backends.sqlite import SqliteBackend

            backend = SqliteBackend(db_path)

        self._task_manager = task_manager
        self._backend = backend
        self._interval = interval
        self._requeue_pending = requeue_pending
        self._retention_days = retention_days
        self._bg_task: asyncio.Task | None = None
        self._retention_counter: int = 0
        # Run retention every ~6 hours regardless of snapshot interval.
        self._retention_every: int = max(1, round(21600 / max(interval, 1)))

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def load(self) -> int:
        """
        Restore persisted completed records into the in-memory store.

        Live tasks already in the store are never overwritten.

        Returns:
            Number of records restored.
        """
        records = await self._backend.load()
        for record in records:
            self._task_manager.store.restore(record)
        return len(records)

    async def requeue(self) -> int:
        """
        Re-dispatch tasks that were pending at the previous shutdown.

        Each task is matched back to its registered function by name.
        Unrecognised function names are skipped with a ``WARNING`` log.
        The pending store is cleared after all tasks have been dispatched.

        Returns:
            Number of tasks re-dispatched.
        """
        records = await self._backend.load_pending()
        if not records:
            return 0

        from .executor import execute_task

        # Check only the pending task IDs against history — avoids loading
        # the full history just to find a handful of already-completed tasks.
        pending_ids = [r.task_id for r in records]
        completed_ids = await self._backend.completed_ids(pending_ids)

        dispatched = 0
        for record in records:
            # Skip tasks that completed successfully before the crash —
            # they were flushed to history but not yet removed from pending.
            if record.task_id in completed_ids:
                logger.info(
                    "fastapi-taskflow: skipping requeue of task %s (%s) — "
                    "already completed successfully.",
                    record.task_id,
                    record.func_name,
                )
                continue

            # Atomically claim the task so only one instance dispatches it.
            # Backends that share state (SQLite same-host, Redis) delete the
            # pending record here; the default no-op returns True for custom
            # backends that don't implement atomic claiming.
            claimed = await self._backend.claim_pending(record.task_id)
            if not claimed:
                logger.debug(
                    "fastapi-taskflow: task %s already claimed by another instance, skipping.",
                    record.task_id,
                )
                continue

            result = self._task_manager.registry.get_by_name(record.func_name)
            if result is None:
                logger.warning(
                    "fastapi-taskflow: cannot requeue task %s — "
                    "function %r is not registered in the current process. "
                    "Skipping.",
                    record.task_id,
                    record.func_name,
                )
                continue

            func, config = result
            # Ensure the task record is in the store so the dashboard shows it.
            self._task_manager.store.restore(record)

            asyncio.create_task(
                execute_task(
                    func,
                    record.task_id,
                    config,
                    self._task_manager.store,
                    record.args,
                    record.kwargs,
                    backend=self._backend,
                    logger=self._task_manager.logger,
                    encryptor=self._task_manager.fernet,
                    semaphore=self._task_manager._task_semaphore,
                    sync_executor=self._task_manager._sync_executor,
                )
            )
            dispatched += 1
            logger.info(
                "fastapi-taskflow: requeued task %s (%s)",
                record.task_id,
                record.func_name,
            )

        # clear_pending is now a safety net — backends with atomic claim_pending
        # already deleted each record individually above. For custom backends
        # using the default no-op claim_pending this clears the full list.
        await self._backend.clear_pending()
        return dispatched

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def flush(self) -> int:
        """
        Immediately persist all completed tasks to the backend.

        Returns:
            Number of records written.
        """
        completed = [
            t
            for t in self._task_manager.store.list()
            if t.status.value in ("success", "failed", "cancelled")
        ]
        if not completed:
            return 0
        return await self._backend.save(completed)

    async def flush_one(self, task_id: str) -> None:
        """
        Immediately persist a single completed task to the backend.

        Called by the executor right after a task transitions to SUCCESS so
        that a crash between SUCCESS and the next periodic flush does not
        cause the task to be re-executed on restart.
        """
        record = self._task_manager.store.get(task_id)
        if record is not None:
            await self._backend.save([record])

    async def flush_pending(self) -> int:
        """
        Persist all unfinished tasks at shutdown so they can be handled on
        next startup.  Only called when ``requeue_pending=True``.

        ``PENDING`` tasks (never started) are always saved for requeue.

        ``RUNNING`` tasks (mid-execution at shutdown) are treated based on
        their registered ``requeue_on_interrupt`` flag:

        - ``requeue_on_interrupt=True`` — saved as ``PENDING`` for requeue.
          Only set this on idempotent tasks that are safe to restart from
          scratch even if they partially executed.
        - ``requeue_on_interrupt=False`` (default) — saved to the **history**
          backend as ``INTERRUPTED`` so they are visible in the dashboard but
          are **not** re-executed automatically.

        Returns:
            Number of records saved to the pending store.
        """
        unfinished = [
            t
            for t in self._task_manager.store.list()
            if t.status.value in ("pending", "running")
        ]
        if not unfinished:
            return 0

        to_requeue = []
        to_interrupt = []

        for t in unfinished:
            if t.status == TaskStatus.PENDING:
                to_requeue.append(t)
            else:
                # RUNNING — check whether the task opted in to requeue on interrupt
                result = self._task_manager.registry.get_by_name(t.func_name)
                requeue_safe = result[1].requeue_on_interrupt if result else False

                if requeue_safe:
                    self._task_manager.store.update(
                        t.task_id, status=TaskStatus.PENDING
                    )
                    to_requeue.append(t)
                else:
                    # Mark as INTERRUPTED in the store and flush to history so
                    # it's visible in the dashboard but will not be re-executed.
                    end_time = datetime.now(timezone.utc)
                    self._task_manager.store.update(
                        t.task_id,
                        status=TaskStatus.INTERRUPTED,
                        end_time=end_time,
                    )
                    if self._task_manager.logger is not None:
                        await self._task_manager.logger.on_lifecycle(
                            LifecycleEvent(
                                task_id=t.task_id,
                                func_name=t.func_name,
                                status=TaskStatus.INTERRUPTED,
                                timestamp=end_time,
                                attempt=t.retries_used,
                                retries_used=t.retries_used,
                                tags=t.tags,
                            )
                        )
                    to_interrupt.append(t)

        if to_interrupt:
            await self._backend.save(to_interrupt)

        if not to_requeue:
            return 0

        return await self._backend.save_pending(to_requeue)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background periodic flush loop."""
        self._bg_task = asyncio.create_task(self._run())

    def stop(self) -> None:
        """Cancel the background loop."""
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None

    async def _run(self) -> None:
        """Periodic flush loop. Runs for the lifetime of the application.

        Sleeps for *interval* seconds, then flushes all completed tasks to the
        backend. Every *_retention_every* flushes, also prunes old records when
        *retention_days* is configured.
        """
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()
            if self._retention_days is not None:
                self._retention_counter += 1
                if self._retention_counter >= self._retention_every:
                    self._retention_counter = 0
                    await self._prune_old_records()

    async def _prune_old_records(self) -> None:
        """Delete terminal records older than *retention_days* from the store and backend.

        Removes records whose ``end_time`` is before the computed cutoff from
        both the in-memory store and the backend. Invalidates the merged-list
        cache so the dashboard reflects the deletion immediately.
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)  # type: ignore[arg-type]
        store_deleted = self._task_manager.store.delete_completed_before(cutoff)
        backend_deleted = await self._backend.delete_before(cutoff)
        if store_deleted or backend_deleted:
            self._task_manager._invalidate_backend_cache()
            logger.info(
                "fastapi-taskflow: retention pruned %d store + %d backend records older than %.1f days",
                store_deleted,
                backend_deleted,
                self._retention_days,
            )

    # ------------------------------------------------------------------
    # SQLite query passthrough
    # ------------------------------------------------------------------

    def query(
        self,
        status: str | None = None,
        func_name: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Query historical records.  Only available when the backend is
        :class:`~fastapi_taskflow.backends.SqliteBackend`.

        Raises:
            AttributeError: If the configured backend does not expose ``query``.
        """
        if not hasattr(self._backend, "query"):
            raise AttributeError(
                f"{type(self._backend).__name__} does not support query(). "
                "This method is only available on SqliteBackend."
            )
        return self._backend.query(status=status, func_name=func_name, limit=limit)  # type: ignore[union-attr]
