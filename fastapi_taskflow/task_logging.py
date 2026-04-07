"""
User-facing task logging API.

Call :func:`task_log` from inside any managed task to emit a timestamped
log entry that is stored on the task record and shown in the live dashboard::

    from fastapi_taskflow import task_log

    @task_manager.task(retries=3)
    def send_email(address: str) -> None:
        task_log("Connecting to SMTP server")
        task_log(f"Sending to {address}")

Calls outside a task context (e.g. at import time) are silently ignored.
"""

from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from typing import Callable, Optional

# Holds a sink callable set by executor.py before each task invocation.
# None when called outside a task context.
_log_sink: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("_task_log_sink", default=None)
)


def task_log(message: str) -> None:
    """
    Emit a timestamped log entry from within a running task.

    The entry is appended to the task's log list and becomes immediately
    visible in the live dashboard under that task's detail panel.

    No-op when called outside a managed task context.
    """
    sink = _log_sink.get()
    if sink is None:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    sink(f"{ts} {message}")


def _set_sink(sink: Optional[Callable[[str], None]]) -> contextvars.Token:
    """Internal: activate a log sink for the current execution context."""
    return _log_sink.set(sink)


def _reset_sink(token: "contextvars.Token[Optional[Callable[[str], None]]]") -> None:
    """Internal: restore the previous sink using the token from _set_sink."""
    _log_sink.reset(token)
