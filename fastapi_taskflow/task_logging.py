"""User-facing task logging API.

Call :func:`task_log` from inside any managed task to emit a structured log
entry that is stored on the task record and shown in the live dashboard::

    from fastapi_taskflow import task_log

    @task_manager.task(retries=3)
    def send_email(address: str) -> None:
        task_log("Connecting to SMTP server")
        task_log("Sending to %s", level="info", recipient=address)

When called outside a managed task context (direct function call, script,
cron job, test), the entry is forwarded to the standard library logger
``fastapi_taskflow.task`` at the matching log level instead of being dropped.
Configure that logger via Python's standard logging setup to control output.
"""

from __future__ import annotations

import contextvars
import logging as _logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_fallback_logger = _logging.getLogger("fastapi_taskflow.task")

_LEVEL_MAP: dict[str, int] = {
    "debug": _logging.DEBUG,
    "info": _logging.INFO,
    "warning": _logging.WARNING,
    "error": _logging.ERROR,
}


# Holds a sink callable set by executor.py before each task invocation.
# None when called outside a task context.
_log_sink: contextvars.ContextVar[Optional[Callable[[str, str, dict], None]]] = (
    contextvars.ContextVar("_task_log_sink", default=None)
)

_task_context: contextvars.ContextVar[Optional["TaskContext"]] = contextvars.ContextVar(
    "_task_context", default=None
)


@dataclass
class TaskContext:
    """Execution context accessible from inside a running task.

    Retrieve it with :func:`get_task_context` from any code path invoked
    during task execution -- including helper functions called by the task.

    Attributes:
        task_id: UUID of the currently running task.
        func_name: Name of the task function.
        attempt: Zero-based retry attempt index. ``0`` means the first run.
        tags: Key/value labels attached to the task at enqueue time.
    """

    task_id: str
    func_name: str
    attempt: int
    tags: dict[str, str] = field(default_factory=dict)


def get_task_context() -> Optional[TaskContext]:
    """Return the :class:`TaskContext` for the currently running task.

    Returns ``None`` when called outside a managed task.

    Example::

        def send_email(address: str) -> None:
            ctx = get_task_context()
            if ctx:
                task_log(f"attempt {ctx.attempt}")
    """
    return _task_context.get()


def task_log(message: str, *, level: str = "info", **extra: Any) -> None:
    """Emit a structured log entry from within a running task.

    Inside a managed task the entry is appended to the task's log list,
    shown in the live dashboard, and forwarded to any configured
    :class:`~fastapi_taskflow.loggers.TaskObserver` instances.

    Outside a managed task (direct function call, script, cron job, test),
    the entry is forwarded to the standard library logger
    ``fastapi_taskflow.task`` at the matching log level.  Configure that
    logger via Python's standard ``logging`` setup to control output.

    Args:
        message: The log message.
        level: Severity string: ``"debug"``, ``"info"``, ``"warning"``, or
            ``"error"``. Default is ``"info"``.
        **extra: Arbitrary key/value pairs forwarded to the structured
            :class:`~fastapi_taskflow.loggers.LogEvent` for downstream observers.
            Not forwarded to the stdlib fallback.

    Example::

        task_log("Connecting to SMTP server")
        task_log("Retry failed", level="warning", attempt=2)
        task_log("Payment processed", order_id="ord_123", amount=49.99)
    """
    sink = _log_sink.get()
    if sink is None:
        _fallback_logger.log(
            _LEVEL_MAP.get(level.lower(), _logging.INFO),
            message,
        )
        return
    sink(message, level, extra)


def _set_sink(
    sink: Optional[Callable[[str, str, dict], None]],
) -> "contextvars.Token[Optional[Callable[[str, str, dict], None]]]":
    """Internal: activate a log sink for the current execution context."""
    return _log_sink.set(sink)


def _reset_sink(
    token: "contextvars.Token[Optional[Callable[[str, str, dict], None]]]",
) -> None:
    """Internal: restore the previous sink using the token from :func:`_set_sink`."""
    _log_sink.reset(token)


def _set_context(
    ctx: Optional[TaskContext],
) -> "contextvars.Token[Optional[TaskContext]]":
    """Internal: set the task context for the current execution context."""
    return _task_context.set(ctx)


def _reset_context(token: "contextvars.Token[Optional[TaskContext]]") -> None:
    """Internal: restore the previous task context."""
    _task_context.reset(token)
