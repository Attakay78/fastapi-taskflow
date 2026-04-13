from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """Lifecycle states a task moves through from creation to completion.

    Transitions:
        PENDING -> RUNNING -> SUCCESS
        PENDING -> RUNNING -> FAILED  (after all retries exhausted)
        PENDING/RUNNING -> INTERRUPTED  (app shut down mid-execution)
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class TaskConfig:
    """Execution settings attached to a function via ``@task_manager.task()``.

    You never instantiate this directly. It is created by the decorator and
    stored in the :class:`~fastapi_taskflow.registry.TaskRegistry`.

    Attributes:
        retries: Number of additional attempts after the first failure.
            A value of ``3`` means up to 4 total attempts.
        delay: Seconds to wait before the first retry.
        backoff: Multiplier applied to ``delay`` on each subsequent retry.
            Use ``2.0`` for exponential backoff (1s, 2s, 4s, ...).
        persist: Reserved for future use.
        name: Display name used in logs and the dashboard. Defaults to the
            decorated function's ``__name__``.
        requeue_on_interrupt: When ``True``, a task that was mid-execution
            at shutdown is saved as PENDING and re-dispatched on the next
            startup. Only set this on functions that are safe to run from
            scratch even if they partially completed (idempotent tasks).
    """

    retries: int = 0
    delay: float = 0.0
    backoff: float = 1.0
    persist: bool = False
    name: str | None = None
    requeue_on_interrupt: bool = False


@dataclass
class TaskRecord:
    """Runtime state for one task invocation.

    Created when ``add_task()`` is called and updated as the task progresses.
    Stored in the :class:`~fastapi_taskflow.store.TaskStore` and persisted to
    the backend when completed.

    Attributes:
        task_id: UUID assigned when the task is enqueued.
        func_name: Name of the function registered with ``@task_manager.task()``.
        status: Current lifecycle state.
        args: Positional arguments the task was called with.
        kwargs: Keyword arguments the task was called with.
        created_at: When ``add_task()`` was called (UTC).
        start_time: When the executor started running the function (UTC).
        end_time: When the task reached a terminal state (UTC).
        retries_used: Number of retry attempts that have run so far.
        error: String form of the last exception, if the task failed.
        logs: Entries emitted by :func:`~fastapi_taskflow.task_logging.task_log`
            during execution, in order.
        stacktrace: Full traceback of the last failure, if the task failed.
        idempotency_key: Caller-provided key used to deduplicate tasks.
            See :meth:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks.add_task`.
    """

    task_id: str
    func_name: str
    status: TaskStatus
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    start_time: datetime | None = None
    end_time: datetime | None = None
    retries_used: int = 0
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    stacktrace: str | None = None
    idempotency_key: str | None = None

    @property
    def duration(self) -> float | None:
        """Elapsed seconds between ``start_time`` and ``end_time``, or ``None`` if
        the task has not finished yet."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this record to a JSON-safe dict for the REST API."""
        return {
            "task_id": self.task_id,
            "func_name": self.func_name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": self.duration,
            "retries_used": self.retries_used,
            "error": self.error,
            "logs": list(self.logs),
            "stacktrace": self.stacktrace,
        }
