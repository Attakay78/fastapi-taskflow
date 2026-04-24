import asyncio
import threading
from datetime import datetime
from typing import Optional

from .models import TaskRecord, TaskStatus


class TaskStore:
    """Thread-safe in-memory store for live task state.

    Holds every :class:`~fastapi_taskflow.models.TaskRecord` created since the
    app started. Records are never deleted from here -- the dashboard and API
    read from this store directly for the lowest-latency view.

    Also manages Server-Sent Events (SSE) subscriber queues so the dashboard
    receives a push notification whenever any task state changes.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

        # One queue per connected dashboard client. maxsize=1 coalesces rapid
        # back-to-back updates: if a notification is already waiting in the
        # queue, put_nowait silently drops the duplicate.
        self._queues: set[asyncio.Queue] = set()
        self._queues_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE subscriber management
    # ------------------------------------------------------------------

    def add_subscriber(self) -> "asyncio.Queue[str]":
        """Register a new SSE client and return its notification queue.

        The dashboard calls this when a browser connects to the ``/stream``
        endpoint and calls :meth:`remove_subscriber` when the connection closes.
        """
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        with self._queues_lock:
            self._queues.add(q)
        return q

    def remove_subscriber(self, q: "asyncio.Queue[str]") -> None:
        """Unregister an SSE client queue."""
        with self._queues_lock:
            self._queues.discard(q)

    def _fan_out(self) -> None:
        """Push a ``"change"`` message to every connected SSE client.

        Must be called from within the event loop thread. Uses ``put_nowait``
        with ``maxsize=1`` to coalesce bursts: if a notification is already
        waiting in a client's queue, the duplicate is silently dropped.
        """
        with self._queues_lock:
            queues = list(self._queues)

        for q in queues:
            try:
                q.put_nowait("change")
            except asyncio.QueueFull:
                pass  # a notification is already pending; drop the duplicate

    def _notify_change(self) -> None:
        """Schedule ``_fan_out`` onto the event loop from any calling context.

        Safe to call from both the event loop thread (e.g. status transitions
        in ``execute_task``) and from thread-pool threads (e.g. sync tasks run
        via ``asyncio.to_thread``). This ensures ``task_log()`` entries emitted
        inside sync tasks still trigger live dashboard updates.
        """
        try:
            loop = asyncio.get_running_loop()
            # Already in the event loop thread — schedule directly.
            loop.call_soon(self._fan_out)
        except RuntimeError:
            # Called from a thread-pool thread — hand off safely.
            try:
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(self._fan_out)
            except RuntimeError:
                return  # no event loop exists (e.g. during unit tests)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create(
        self,
        task_id: str,
        func_name: str,
        args: tuple,
        kwargs: dict,
        idempotency_key: Optional[str] = None,
        tags: Optional[dict] = None,
        encrypted_payload: Optional[bytes] = None,
        source: str = "manual",
    ) -> TaskRecord:
        """Create a new ``PENDING`` record and add it to the store.

        Called by :meth:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks.add_task`
        immediately after a task is enqueued.

        Args:
            task_id: UUID string for the new task.
            func_name: Name of the registered function.
            args: Positional arguments passed to the function.
            kwargs: Keyword arguments passed to the function.
            idempotency_key: Optional deduplication key.
            tags: Optional key/value labels.
            encrypted_payload: Optional Fernet-encrypted args blob.
            source: Origin of the task. ``"manual"`` for route-enqueued
                tasks, ``"scheduled"`` for periodic scheduler fires.
        """
        record = TaskRecord(
            task_id=task_id,
            func_name=func_name,
            status=TaskStatus.PENDING,
            args=args,
            kwargs=kwargs,
            idempotency_key=idempotency_key,
            tags=tags or {},
            encrypted_payload=encrypted_payload,
            source=source,
        )
        with self._lock:
            self._tasks[task_id] = record
        self._notify_change()
        return record

    def find_by_idempotency_key(self, key: str) -> Optional[TaskRecord]:
        """Return the first non-failed task with the given idempotency key, or ``None``.

        Used by :meth:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks.add_task`
        to skip re-enqueuing a task that is already pending or running.
        """
        with self._lock:
            for record in self._tasks.values():
                if record.idempotency_key == key and record.status not in (
                    TaskStatus.FAILED,
                    TaskStatus.INTERRUPTED,
                ):
                    return record
        return None

    def update(self, task_id: str, **fields) -> Optional[TaskRecord]:
        """Update arbitrary fields on a task record in-place.

        Returns the updated record, or ``None`` if *task_id* is not found.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            for key, value in fields.items():
                setattr(record, key, value)
        self._notify_change()
        return record

    def append_log(self, task_id: str, message: str) -> None:
        """Append a log entry to the task record and notify SSE subscribers."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.logs.append(message)
        self._notify_change()

    def restore(self, record: TaskRecord) -> None:
        """Insert a pre-built record from a snapshot without overwriting live state.

        Called at startup by the :class:`~fastapi_taskflow.snapshot.SnapshotScheduler`
        to repopulate the store with previously completed tasks. If the same
        ``task_id`` already exists (e.g. from a duplicate load), it is skipped.
        """
        with self._lock:
            if record.task_id not in self._tasks:
                self._tasks[record.task_id] = record
        # No SSE notify -- restore runs at startup before any client connects.

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """Return the record for *task_id*, or ``None``."""
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[TaskRecord]:
        """Return a snapshot of all records currently in the store."""
        with self._lock:
            return list(self._tasks.values())

    def delete_completed_before(self, cutoff: datetime) -> int:
        """Remove terminal records from the store whose end_time is before *cutoff*.

        Only removes success, failed, and interrupted records. Pending and
        running records are never removed.

        Returns:
            Number of records removed.
        """
        _terminal = {
            TaskStatus.SUCCESS,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }
        to_remove = []
        with self._lock:
            for task_id, record in self._tasks.items():
                if (
                    record.status in _terminal
                    and record.end_time is not None
                    and record.end_time < cutoff
                ):
                    to_remove.append(task_id)
            for task_id in to_remove:
                del self._tasks[task_id]
        return len(to_remove)

    def clear(self) -> None:
        """Remove all records. Primarily used in tests."""
        with self._lock:
            self._tasks.clear()
