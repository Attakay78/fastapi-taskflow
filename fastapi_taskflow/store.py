import asyncio
import threading
from typing import Optional

from .models import TaskRecord, TaskStatus


class TaskStore:
    """Thread-safe in-memory store for live task state."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

        # SSE subscriber queues — one per connected dashboard client.
        # maxsize=1 coalesces rapid bursts: if a notification is already
        # waiting, further put_nowait calls are silently dropped.
        self._queues: set[asyncio.Queue] = set()
        self._queues_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE subscriber management
    # ------------------------------------------------------------------

    def add_subscriber(self) -> "asyncio.Queue[str]":
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        with self._queues_lock:
            self._queues.add(q)
        return q

    def remove_subscriber(self, q: "asyncio.Queue[str]") -> None:
        with self._queues_lock:
            self._queues.discard(q)

    def _notify_change(self) -> None:
        """Non-blocking fan-out to all SSE subscriber queues.

        Only fires when called from within a running event loop (i.e. from
        async task execution). Calls originating from thread-pool threads
        (e.g. ``asyncio.to_thread`` during snapshot load) are silently
        skipped — there are no SSE clients connected at that point anyway.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # not in event loop thread — skip

        with self._queues_lock:
            queues = list(self._queues)

        for q in queues:
            try:
                q.put_nowait("change")
            except asyncio.QueueFull:
                pass  # notification already pending — coalesced

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
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=task_id,
            func_name=func_name,
            status=TaskStatus.PENDING,
            args=args,
            kwargs=kwargs,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            self._tasks[task_id] = record
        self._notify_change()
        return record

    def find_by_idempotency_key(self, key: str) -> Optional[TaskRecord]:
        """Return the first active (non-failed, non-interrupted) task with the given idempotency key, or None."""
        with self._lock:
            for record in self._tasks.values():
                if record.idempotency_key == key and record.status not in (
                    TaskStatus.FAILED,
                    TaskStatus.INTERRUPTED,
                ):
                    return record
        return None

    def update(self, task_id: str, **fields) -> Optional[TaskRecord]:
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
        """Insert a pre-built record (e.g. loaded from a snapshot) without overwriting."""
        with self._lock:
            if record.task_id not in self._tasks:
                self._tasks[record.task_id] = record
        # No notify — restore runs at startup before any SSE client connects.

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[TaskRecord]:
        with self._lock:
            return list(self._tasks.values())

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()
