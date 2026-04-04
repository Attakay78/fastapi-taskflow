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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend
    from .manager import TaskManager

logger = logging.getLogger(__name__)


class SnapshotScheduler:
    """
    Drives the periodic flush loop, startup restore, and optional requeue.

    Args:
        task_manager: The :class:`~fastapi_taskflow.TaskManager` whose
            store will be snapshotted.
        backend: Any :class:`~fastapi_taskflow.backends.SnapshotBackend`
            implementation.
        interval: How often (in seconds) to flush completed tasks to the
            backend.
        requeue_pending: When ``True``:

            * On **shutdown** — tasks still in ``pending`` or ``running``
              state are saved to the backend's pending store (``running``
              tasks are treated as ``pending`` since they did not complete).
            * On **startup** — those tasks are loaded and re-dispatched via
              :func:`asyncio.ensure_future` for each function that is still
              registered in the task registry.  Tasks whose function is no
              longer registered are skipped with a warning.  The pending
              store is cleared after successful requeue.
    """

    def __init__(
        self,
        task_manager: "TaskManager",
        backend: "SnapshotBackend | None" = None,
        interval: float = 60.0,
        requeue_pending: bool = False,
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
        self._bg_task: asyncio.Task | None = None

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

        dispatched = 0
        for record in records:
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

            asyncio.ensure_future(
                execute_task(
                    func,
                    record.task_id,
                    config,
                    self._task_manager.store,
                    record.args,
                    record.kwargs,
                )
            )
            dispatched += 1
            logger.info(
                "fastapi-taskflow: requeued task %s (%s)",
                record.task_id,
                record.func_name,
            )

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
            if t.status.value in ("success", "failed")
        ]
        if not completed:
            return 0
        return await self._backend.save(completed)

    async def flush_pending(self) -> int:
        """
        Persist all unfinished tasks (``pending`` and ``running``) so they
        can be requeued on next startup.  Tasks in ``running`` state are
        saved as ``pending`` because they did not complete cleanly.

        Only called when ``requeue_pending=True``.

        Returns:
            Number of records saved.
        """
        from .models import TaskStatus

        unfinished = [
            t
            for t in self._task_manager.store.list()
            if t.status.value in ("pending", "running")
        ]
        if not unfinished:
            return 0

        # Normalise running → pending so they are retried from scratch.
        for t in unfinished:
            if t.status == TaskStatus.RUNNING:
                self._task_manager.store.update(t.task_id, status=TaskStatus.PENDING)

        return await self._backend.save_pending(unfinished)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background periodic flush loop."""
        self._bg_task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        """Cancel the background loop."""
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()

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
