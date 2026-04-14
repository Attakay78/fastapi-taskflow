"""Base types for the fastapi-taskflow logging layer.

:class:`TaskObserver` is the interface every logger must implement.
:class:`LogEvent` and :class:`LifecycleEvent` carry the structured data
delivered to each observer.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import TaskStatus

# Numeric weights used for min_level filtering.
_LEVELS: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}

# Natural severity for each task lifecycle status.
_STATUS_LEVEL: dict[str, str] = {
    "pending": "info",
    "running": "info",
    "success": "info",
    "failed": "error",
    "interrupted": "warning",
}


@dataclass
class LogEvent:
    """A single log entry emitted by :func:`~fastapi_taskflow.task_logging.task_log`.

    Attributes:
        task_id: UUID of the task that emitted this entry.
        func_name: Name of the task function.
        message: The log message, prefixed with a UTC timestamp.
        level: Severity string: ``"debug"``, ``"info"``, ``"warning"``, or ``"error"``.
        timestamp: UTC datetime when the entry was created.
        attempt: Zero-based retry attempt index (0 = first run, 1 = first retry, ...).
        tags: Key/value labels attached to the task at enqueue time.
        extra: Arbitrary structured data passed as keyword arguments to ``task_log()``.
    """

    task_id: str
    func_name: str
    message: str
    level: str
    timestamp: datetime
    attempt: int
    tags: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LifecycleEvent:
    """A task status transition fired by the executor.

    Emitted on every state change: RUNNING, SUCCESS, FAILED, and INTERRUPTED.

    Attributes:
        task_id: UUID of the task.
        func_name: Name of the task function.
        status: The new :class:`~fastapi_taskflow.models.TaskStatus` value.
        timestamp: UTC datetime when the transition occurred.
        attempt: Zero-based retry attempt index at the time of the transition.
        retries_used: Total retry attempts consumed so far.
        duration: Elapsed seconds from start to this transition. ``None`` on RUNNING.
        error: String form of the last exception. Set only on FAILED.
        stacktrace: Full traceback of the last exception. Set only on FAILED.
        tags: Key/value labels attached to the task at enqueue time.
    """

    task_id: str
    func_name: str
    status: "TaskStatus"
    timestamp: datetime
    attempt: int
    retries_used: int
    duration: float | None = None
    error: str | None = None
    stacktrace: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


class TaskObserver(abc.ABC):
    """Base class for all task loggers.

    Subclass this and implement :meth:`on_log` and/or :meth:`on_lifecycle` to
    send task events to any destination -- a file, a metrics system, a tracing
    backend, or anything else.

    Both methods are ``async`` so implementations can ``await`` network calls,
    database writes, or any other async I/O. Sync-only destinations can use
    ``asyncio.to_thread`` inside their implementation.

    Errors raised inside either method are caught by :class:`LoggerChain` and
    logged to stderr. They never propagate to the task or affect its outcome.

    Args:
        min_level: Minimum severity to process. Events with a lower level are
            silently skipped. Accepts ``"debug"``, ``"info"``, ``"warning"``,
            or ``"error"``. Default is ``"info"``.

    Example::

        class PrometheusLogger(TaskObserver):
            async def on_lifecycle(self, event: LifecycleEvent) -> None:
                TASK_COUNTER.labels(
                    func=event.func_name,
                    status=event.status.value,
                ).inc()
    """

    def __init__(self, min_level: str = "info") -> None:
        self._min_level = _LEVELS.get(min_level, 1)

    def _should_log(self, level: str) -> bool:
        """Return ``True`` if *level* meets or exceeds this logger's ``min_level``."""
        return _LEVELS.get(level, 1) >= self._min_level

    def _should_log_lifecycle(self, event: LifecycleEvent) -> bool:
        """Return ``True`` if the natural severity of *event*'s status meets ``min_level``."""
        level = _STATUS_LEVEL.get(event.status.value, "info")
        return self._should_log(level)

    async def on_log(self, event: LogEvent) -> None:
        """Process a log entry emitted by ``task_log()`` inside a running task.

        Args:
            event: Structured log entry including message, level, attempt, and tags.
        """

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        """Process a task status transition.

        Called on every transition: RUNNING, SUCCESS, FAILED, INTERRUPTED.

        Args:
            event: Structured lifecycle event including status, duration, and tags.
        """

    async def startup(self) -> None:
        """Called at app startup before any tasks run.

        Use this to open connections, initialise exporters, or warm up buffers.
        """

    async def close(self) -> None:
        """Called at app shutdown.

        Flush any buffered events and release held resources (connections,
        file handles, exporters, etc.).
        """
