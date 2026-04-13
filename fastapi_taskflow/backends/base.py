"""Abstract base class for snapshot backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import TaskRecord


class SnapshotBackend(ABC):
    """Contract that every snapshot backend must implement.

    Backends handle two separate storage concerns:

    * **History** (``save`` / ``load``) -- completed tasks kept for
      observability and the dashboard. Written periodically by the scheduler
      and read back on startup.
    * **Requeue** (``save_pending`` / ``load_pending`` / ``clear_pending``) --
      tasks that had not finished when the app shut down. Stored separately
      so they can be re-dispatched on the next startup without polluting the
      history log.

    Implementations manage their own connections. ``close()`` is called by
    :class:`~fastapi_taskflow.snapshot.SnapshotScheduler` on shutdown.
    """

    @abstractmethod
    async def save(self, records: "list[TaskRecord]") -> int:
        """Persist *records* (completed tasks) to the history store.

        Should upsert so repeated calls are idempotent.

        Returns:
            Number of records written or updated.
        """

    @abstractmethod
    async def load(self) -> "list[TaskRecord]":
        """Return all previously persisted completed task records.

        Called at startup to repopulate the in-memory store.
        """

    @abstractmethod
    async def save_pending(self, records: "list[TaskRecord]") -> int:
        """Persist unfinished tasks at shutdown so they can be requeued.

        Replaces any previously saved pending snapshot wholesale. Called
        once on shutdown when ``requeue_pending=True``.

        Returns:
            Number of records written.
        """

    @abstractmethod
    async def load_pending(self) -> "list[TaskRecord]":
        """Return all tasks saved by :meth:`save_pending`.

        Called once on startup, before :meth:`clear_pending`.
        """

    @abstractmethod
    async def clear_pending(self) -> None:
        """Delete all records saved by :meth:`save_pending`.

        Called after pending tasks have been re-dispatched so they are not
        executed again on subsequent restarts.
        """

    async def claim_pending(self, task_id: str) -> bool:
        """Atomically claim a pending task so only one instance dispatches it.

        Returns ``True`` if this caller claimed the task (deleted it from the
        pending store), or ``False`` if another instance already claimed it.

        The default always returns ``True``. Override in backends that share
        state across instances (SQLite same-host, Redis) to prevent duplicate
        execution on restart.
        """
        return True

    async def check_idempotency_key(self, key: str) -> "str | None":
        """Return the ``task_id`` recorded for *key*, or ``None``.

        Called by the executor before running a task that has an idempotency
        key. The default no-op disables cross-instance deduplication unless
        the backend overrides this method.
        """
        return None

    async def record_idempotency_key(self, key: str, task_id: str) -> None:
        """Persist *key* -> *task_id* after a task completes successfully.

        The default is a no-op. Override to enable cross-instance idempotency
        key deduplication.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any held resources (connections, file handles, etc.)."""
