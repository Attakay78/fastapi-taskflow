"""Abstract base class for snapshot backends."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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

    async def completed_ids(self, task_ids: "list[str]") -> "set[str]":
        """Return the subset of *task_ids* that exist in the history store with
        a ``success`` status.

        Used by :meth:`~fastapi_taskflow.snapshot.SnapshotScheduler.requeue`
        to skip tasks that already completed successfully before the crash,
        without loading the full history.

        The default implementation loads all history and filters in Python.
        Override in backends that share state (SQLite, Redis) for an efficient
        targeted query.

        Args:
            task_ids: Task IDs to check.

        Returns:
            The subset of *task_ids* found in history with ``success`` status.
        """
        if not task_ids:
            return set()
        id_set = set(task_ids)
        all_records = await self.load()
        return {
            r.task_id
            for r in all_records
            if r.task_id in id_set and r.status.value == "success"
        }

    async def delete_records(self, task_ids: "list[str]") -> int:
        """Delete specific task records from the history store by their IDs.

        Called when ``retry_replaces_original=True`` to remove the original
        record after a retry is dispatched, so only the new run is visible.

        The default is a no-op that returns 0. Override in backends that
        support targeted deletion (SQLite, Redis, Postgres, MySQL).

        Args:
            task_ids: Task IDs to delete from the history store.

        Returns:
            Number of records deleted.
        """
        return 0

    async def delete_before(self, cutoff: datetime) -> int:
        """Delete terminal task records whose ``end_time`` is before *cutoff*.

        Terminal statuses are success, failed, and interrupted. Pending and
        running records are never deleted.

        The default is a no-op that returns 0. Override in backends that
        support targeted deletion (SQLite, Redis).

        Args:
            cutoff: Delete records with ``end_time`` strictly before this timestamp.

        Returns:
            Number of records deleted.
        """
        return 0

    async def acquire_schedule_lock(self, key: str, ttl: int) -> bool:
        """Acquire a distributed lock for a scheduled task firing.

        Called by :class:`~fastapi_taskflow.periodic.PeriodicScheduler`
        before firing a scheduled entry to ensure only one instance fires
        it in a multi-instance deployment.

        Args:
            key: Unique lock identifier, typically ``"schedule:{func_name}"``.
            ttl: Lock lifetime in seconds. The lock is released automatically
                after this period so a crashed instance does not block future
                firings.

        Returns:
            ``True`` if this caller acquired the lock (should proceed to fire).
            ``False`` if another instance holds the lock (should skip this
            firing).

        The default always returns ``True``. Override in backends that share
        state across instances (SQLite same-host, Redis) to prevent duplicate
        scheduled firings.
        """
        return True

    async def save_metadata(self, key: str, value: str) -> None:
        """Persist a metadata string under *key*.

        Used to store operator-edited queue config overrides so they survive
        restarts. The default is a no-op; backends that share state across
        instances (SQLite, Redis) override this to write to their store.

        Args:
            key: Identifier for the metadata entry (e.g. ``"queue_configs"``).
            value: The value to store (arbitrary UTF-8 string, typically JSON).
        """

    async def load_metadata(self, key: str) -> "str | None":
        """Return the value stored under *key*, or ``None`` if not found.

        The default returns ``None`` (no-op); override in backends that
        persist metadata across restarts.

        Args:
            key: Identifier for the metadata entry.

        Returns:
            The stored string, or ``None``.
        """
        return None

    @abstractmethod
    async def close(self) -> None:
        """Release any held resources (connections, file handles, etc.)."""


class ThreadedSnapshotBackend(SnapshotBackend):
    """Base for backends that offload sync DB operations to a dedicated thread pool.

    Subclasses implement the ``_*_sync`` methods.  This class owns the
    executor, ``_run``, and all async interface methods so they are not
    duplicated across backends.
    """

    def __init__(self, max_workers: int, thread_name_prefix: str) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    # ------------------------------------------------------------------
    # Sync contract — subclasses implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def _save_sync(self, records: "list[TaskRecord]") -> int: ...

    @abstractmethod
    def _load_sync(self) -> "list[TaskRecord]": ...

    @abstractmethod
    def _save_pending_sync(self, records: "list[TaskRecord]") -> int: ...

    @abstractmethod
    def _load_pending_sync(self) -> "list[TaskRecord]": ...

    @abstractmethod
    def _clear_pending_sync(self) -> None: ...

    @abstractmethod
    def _claim_pending_sync(self, task_id: str) -> bool: ...

    @abstractmethod
    def _check_idempotency_key_sync(self, key: str) -> "str | None": ...

    @abstractmethod
    def _record_idempotency_key_sync(self, key: str, task_id: str) -> None: ...

    @abstractmethod
    def _delete_before_sync(self, cutoff: str) -> int: ...

    @abstractmethod
    def _delete_records_sync(self, task_ids: list[str]) -> int: ...

    @abstractmethod
    def _completed_ids_sync(self, task_ids: list[str]) -> "set[str]": ...

    @abstractmethod
    def _acquire_schedule_lock_sync(self, key: str, ttl: int) -> bool: ...

    # ------------------------------------------------------------------
    # Async interface — single implementation for all threaded backends
    # ------------------------------------------------------------------

    async def save(self, records: "list[TaskRecord]") -> int:
        return await self._run(self._save_sync, records)

    async def load(self) -> "list[TaskRecord]":
        return await self._run(self._load_sync)

    async def save_pending(self, records: "list[TaskRecord]") -> int:
        return await self._run(self._save_pending_sync, records)

    async def load_pending(self) -> "list[TaskRecord]":
        return await self._run(self._load_pending_sync)

    async def clear_pending(self) -> None:
        await self._run(self._clear_pending_sync)

    async def claim_pending(self, task_id: str) -> bool:
        return await self._run(self._claim_pending_sync, task_id)

    async def check_idempotency_key(self, key: str) -> "str | None":
        return await self._run(self._check_idempotency_key_sync, key)

    async def record_idempotency_key(self, key: str, task_id: str) -> None:
        await self._run(self._record_idempotency_key_sync, key, task_id)

    async def delete_before(self, cutoff: datetime) -> int:
        return await self._run(self._delete_before_sync, cutoff.isoformat())

    async def delete_records(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        return await self._run(self._delete_records_sync, task_ids)

    async def completed_ids(self, task_ids: list[str]) -> "set[str]":
        if not task_ids:
            return set()
        return await self._run(self._completed_ids_sync, task_ids)

    async def acquire_schedule_lock(self, key: str, ttl: int) -> bool:
        return await self._run(self._acquire_schedule_lock_sync, key, ttl)
