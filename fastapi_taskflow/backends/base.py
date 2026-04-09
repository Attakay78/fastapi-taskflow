"""Abstract base class for snapshot backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import TaskRecord


class SnapshotBackend(ABC):
    """
    Contract that every snapshot backend must fulfil.

    Implementations are responsible for their own connection / resource
    management.  ``close()`` is called by :class:`SnapshotScheduler` on
    application shutdown so backends can clean up connections, flush
    write-buffers, etc.

    Two storage concerns are deliberately separated:

    * **History** (``save`` / ``load``) — completed tasks (success/failed)
      kept for observability and analytics.
    * **Requeue** (``save_pending`` / ``load_pending`` / ``clear_pending``) —
      tasks that had not finished when the app shut down and should be
      re-executed on the next startup.  These are stored in a separate
      namespace so they never contaminate the history log.
    """

    @abstractmethod
    async def save(self, records: "list[TaskRecord]") -> int:
        """
        Persist *records* (completed tasks) to the history store.

        Implementations should upsert so repeated calls are idempotent.

        Returns:
            Number of records written/updated.
        """

    @abstractmethod
    async def load(self) -> "list[TaskRecord]":
        """
        Return all previously persisted completed :class:`TaskRecord` objects.

        The scheduler restores them into the in-memory store on startup.
        """

    @abstractmethod
    async def save_pending(self, records: "list[TaskRecord]") -> int:
        """
        Persist *records* whose status is ``pending`` (tasks that had not
        started before shutdown) so they can be requeued on next startup.

        Replaces any previously saved pending snapshot wholesale — call once
        on shutdown.

        Returns:
            Number of records written.
        """

    @abstractmethod
    async def load_pending(self) -> "list[TaskRecord]":
        """
        Return all tasks saved via :meth:`save_pending`.

        Called once on startup before :meth:`clear_pending`.
        """

    @abstractmethod
    async def clear_pending(self) -> None:
        """
        Delete all records stored by :meth:`save_pending`.

        Called after the pending tasks have been successfully requeued so
        they are not re-dispatched on subsequent restarts.
        """

    async def claim_pending(self, task_id: str) -> bool:
        """
        Atomically claim a single pending task for execution.

        Deletes the task from the pending store and returns ``True`` if this
        caller is the one that deleted it, ``False`` if another instance
        already claimed it (i.e. the record was already gone).

        The default implementation always returns ``True`` — custom backends
        that do not override this method retain the original behaviour where
        every instance that loaded the pending list will dispatch all tasks.
        Override this in backends that share state across instances (SQLite
        same-host, Redis) to prevent duplicate execution on restart.
        """
        return True

    async def check_idempotency_key(self, key: str) -> "str | None":
        """
        Return the ``task_id`` previously recorded for *key*, or ``None``.

        Called before executing a task that carries an idempotency key.
        The default no-op means idempotency key cross-instance dedup is
        only active when a backend overrides this method.
        """
        return None

    async def record_idempotency_key(self, key: str, task_id: str) -> None:
        """
        Persist *key* → *task_id* after a task completes successfully.

        The default is a no-op.  Override in backends to enable cross-instance
        idempotency key dedup.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any held resources (connections, file handles, …)."""
