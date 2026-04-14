"""Pluggable logging layer for fastapi-taskflow.

Built-in loggers:

* :class:`FileLogger`    -- writes to a rotating or watched plain text file
* :class:`StdoutLogger`  -- prints events to stdout (useful during development)
* :class:`InMemoryLogger` -- stores events in memory (useful in tests)

To send task events to any external system (Logfire, Prometheus, Grafana, etc.),
subclass :class:`TaskObserver` and implement ``on_log`` and/or ``on_lifecycle``::

    from fastapi_taskflow.loggers import TaskObserver, LogEvent, LifecycleEvent

    class LogfireLogger(TaskObserver):
        async def on_log(self, event: LogEvent) -> None:
            logfire.info(event.message, task_id=event.task_id, **event.extra)

        async def on_lifecycle(self, event: LifecycleEvent) -> None:
            if event.status.value == "failed":
                logfire.error("Task failed", task_id=event.task_id, error=event.error)

    task_manager = TaskManager(loggers=[FileLogger("tasks.log"), LogfireLogger()])

Multiple loggers run independently -- an error in one never affects the others or
the task itself.
"""

from .base import LifecycleEvent, LogEvent, TaskObserver
from .chain import LoggerChain
from .file import FileLogger
from .memory import InMemoryLogger
from .stdout import StdoutLogger

__all__ = [
    "TaskObserver",
    "LogEvent",
    "LifecycleEvent",
    "LoggerChain",
    "FileLogger",
    "InMemoryLogger",
    "StdoutLogger",
]
