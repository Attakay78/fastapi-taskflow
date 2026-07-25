from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional


def _iso_utc(dt: "datetime | None") -> "str | None":
    """Serialize a datetime as an unambiguous UTC ISO 8601 string.

    Naive datetimes are treated as UTC (the convention used everywhere in
    this codebase) rather than left offset-less, since an offset-less ISO
    string is parsed as local time by JS ``Date`` and most other clients.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@dataclass
class QueueConfig:
    """Configuration for a named task queue.

    Pass a dict of these to ``TaskManager(queues={...})`` to define named
    queues with independent concurrency limits and backpressure caps.

    Args:
        concurrency: Maximum number of tasks from this queue that may be
            running simultaneously. ``None`` means no limit. For async tasks
            this caps in-flight coroutines; for thread tasks it caps threads
            drawn from the shared pool.
        max_size: Maximum number of tasks allowed to wait in this queue at
            any one time. When the queue is full, ``add_task()`` raises
            :exc:`~fastapi_taskflow.manager.QueueFullError` so callers can
            return a 429 rather than silently growing memory without bound.
            ``None`` means no limit.

    Example::

        task_manager = TaskManager(
            max_sync_threads=10,
            queues={
                "email":   QueueConfig(concurrency=30, max_size=500),
                "reports": QueueConfig(concurrency=4,  max_size=50),
                "default": QueueConfig(concurrency=20),
            },
        )
    """

    concurrency: Optional[int] = None
    max_size: Optional[int] = None


class TaskStatus(str, Enum):
    """Lifecycle states a task moves through from creation to completion.

    Transitions:
        PENDING -> RUNNING -> SUCCESS
        PENDING -> RUNNING -> FAILED  (after all retries exhausted)
        PENDING/RUNNING -> INTERRUPTED  (app shut down mid-execution)
        PENDING -> CANCELLED            (cancelled before a worker picked it up)
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


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
        persist: Activates the requeue machinery for this function without
            setting ``requeue_pending=True`` on the manager. Tasks that were
            never started at shutdown will be re-dispatched on the next
            startup. Tasks that were mid-execution are only re-dispatched if
            ``requeue_on_interrupt`` is also ``True``.
        name: Display name used in logs and the dashboard. Defaults to the
            decorated function's ``__name__``.
        requeue_on_interrupt: Re-dispatch this task on startup if it was
            mid-execution when the server shut down. Requires ``persist=True``
            or ``requeue_pending=True`` on the manager, otherwise the requeue
            step never runs. Only set this on functions that are safe to run
            from scratch even if they partially completed.
        eager: When ``True``, the task is dispatched via ``asyncio.create_task``
            immediately when ``add_task()`` is called rather than waiting for
            FastAPI to send the response. Per-call ``eager`` on ``add_task()``
            overrides this value.
        priority: Execution priority for the dedicated priority queue. ``None``
            routes through the standard Starlette background task mechanism.
            Any integer routes through the priority queue; higher values run
            first. The conventional range is 1 (lowest) to 10 (highest) with
            5 as the midpoint, but any integer is accepted. Per-call
            ``priority`` on ``add_task()`` overrides this value.
        executor: Explicit executor selection. One of ``"async"``, ``"thread"``,
            or ``"process"``. When ``None`` (the default), the executor is
            chosen automatically: ``"async"`` for ``async def`` functions and
            ``"thread"`` for plain ``def`` functions. Setting this explicitly
            to ``"process"`` routes the task through a
            :class:`concurrent.futures.ProcessPoolExecutor` worker, which is
            appropriate for CPU-bound work that would otherwise block the event
            loop. See :mod:`fastapi_taskflow.executors.process_executor` for
            constraints and configuration.
    """

    retries: int = 0
    delay: float = 0.0
    backoff: float = 1.0
    persist: bool = False
    name: Optional[str] = None
    requeue_on_interrupt: bool = False
    eager: bool = False
    priority: Optional[int] = None
    executor: Optional[Literal["async", "thread", "process"]] = None
    queue: Optional[str] = None


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
        tags: Key/value labels attached at enqueue time. Forwarded to every
            :class:`~fastapi_taskflow.loggers.LogEvent` and
            :class:`~fastapi_taskflow.loggers.LifecycleEvent` so observers can
            use them as metric labels or structured fields.
        encrypted_payload: Fernet-encrypted blob of ``(args, kwargs)`` when
            ``encrypt_args_key`` is set on the ``TaskManager``. When present,
            ``args`` and ``kwargs`` are stored empty and the executor decrypts
            this field before calling the function.
        source: Where this task came from. ``"manual"`` for tasks enqueued
            via ``add_task()``. ``"scheduled"`` for tasks fired by the
            periodic scheduler.
        priority: Priority level assigned at enqueue time. ``None`` when the
            task was routed through the standard Starlette mechanism (no
            explicit priority). Any integer when routed through the priority
            queue; higher values ran first.
        executor: The executor that ran (or will run) this task. One of
            ``"async"``, ``"thread"``, or ``"process"``. Reflects the
            effective executor after auto-detection, not the raw config value.
    """

    task_id: str
    func_name: str
    status: TaskStatus
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    start_time: datetime | None = None
    end_time: datetime | None = None
    retries_used: int = 0
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    stacktrace: str | None = None
    idempotency_key: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    encrypted_payload: bytes | None = field(default=None)
    source: str = "manual"
    priority: int | None = None
    executor: Optional[Literal["async", "thread", "process"]] = None
    queue: str = "default"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        """Reconstruct a TaskRecord from a :meth:`to_dict` payload.

        Fields not present in ``to_dict()`` output (``args``, ``kwargs``,
        ``encrypted_payload``, ``idempotency_key``) default to their zero
        values. This is the correct behaviour for peer fan-out records, which
        are only read for display and never re-executed locally.
        """

        def _dt(v: Any) -> "datetime | None":
            return datetime.fromisoformat(v) if v else None

        return cls(
            task_id=data["task_id"],
            func_name=data["func_name"],
            status=TaskStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            start_time=_dt(data.get("start_time")),
            end_time=_dt(data.get("end_time")),
            retries_used=data.get("retries_used", 0),
            error=data.get("error"),
            logs=list(data.get("logs", [])),
            stacktrace=data.get("stacktrace"),
            tags=dict(data.get("tags", {})),
            source=data.get("source", "manual"),
            priority=data.get("priority"),
            executor=data.get("executor"),
            queue=data.get("queue", "default"),
        )

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
            "created_at": _iso_utc(self.created_at),
            "start_time": _iso_utc(self.start_time),
            "end_time": _iso_utc(self.end_time),
            "duration": self.duration,
            "retries_used": self.retries_used,
            "error": self.error,
            "logs": list(self.logs),
            "stacktrace": self.stacktrace,
            "tags": dict(self.tags),
            "source": self.source,
            "priority": self.priority,
            "executor": self.executor,
            "queue": self.queue,
        }


@dataclass
class ScheduledOnce:
    """One pending one-off task, scheduled to fire at an exact future time.

    Created by :meth:`~fastapi_taskflow.manager.TaskManager.schedule_once` and
    persisted to the configured snapshot backend so the firing survives a
    restart. Unlike a :class:`TaskRecord`, this is *not* a task invocation —
    no ``task_id`` exists until the entry actually fires, at which point a
    normal ``TaskRecord`` is created and this row is deleted.

    Attributes:
        run_key: Caller-supplied identity for this pending firing. Primary key.
            Scheduling again with the same ``run_key`` replaces the entry
            rather than creating a second one. This is a separate namespace
            from ``TaskRecord.idempotency_key``, which deduplicates
            *executions*; ``run_key`` identifies a *pending schedule* and is
            deliberately replaceable.
        func_name: Name of the registered function to run.
        fire_at: UTC datetime at which the task should run.
        args: Positional arguments to call the function with.
        kwargs: Keyword arguments to call the function with.
        encrypted_payload: Fernet-encrypted blob of ``(args, kwargs)`` when
            ``encrypt_args_key`` is set on the ``TaskManager``. When present,
            ``args`` and ``kwargs`` are stored empty.
        queue: Named queue the firing should be routed into.
        priority: Priority to enqueue the firing at.
        idempotency_key: Optional key forwarded onto the ``TaskRecord`` when
            this entry fires, guarding duplicate *execution*. Independent of
            ``run_key``.
        tags: Key/value labels forwarded onto the ``TaskRecord`` at fire time.
        created_at: When the entry was scheduled (UTC).
    """

    run_key: str
    func_name: str
    fire_at: datetime
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    encrypted_payload: bytes | None = None
    queue: str = "default"
    priority: int | None = None
    idempotency_key: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the REST API and dashboard."""
        return {
            "run_key": self.run_key,
            "func_name": self.func_name,
            "fire_at": _iso_utc(self.fire_at),
            "queue": self.queue,
            "priority": self.priority,
            "tags": dict(self.tags),
            "created_at": _iso_utc(self.created_at),
        }


@dataclass
class AuditEntry:
    """A single audit log entry recording a user action on a task.

    Attributes:
        entry_id: UUID for this audit entry.
        action: The action taken. Currently ``"retry"`` or ``"cancel"``.
        task_id: The task that was acted on.
        actor: Username of the authenticated user, or ``"anonymous"``.
        timestamp: When the action occurred (UTC).
        detail: Action-specific extra data (e.g. ``new_task_id`` for retries).
    """

    entry_id: str
    action: str
    task_id: str
    actor: str
    timestamp: datetime
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "action": self.action,
            "task_id": self.task_id,
            "actor": self.actor,
            "timestamp": _iso_utc(self.timestamp),
            "detail": self.detail,
        }
