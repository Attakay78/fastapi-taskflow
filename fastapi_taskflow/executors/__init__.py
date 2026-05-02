"""Executor abstractions for fastapi-taskflow.

All task dispatch paths implement the :class:`~fastapi_taskflow.executors.base.Executor`
protocol so that async, thread, and process execution share a common interface.

Built-in executors:

* :class:`~fastapi_taskflow.executors.async_executor.AsyncExecutor`  -- coroutines on
  the event loop (default for ``async def`` tasks).
* :class:`~fastapi_taskflow.executors.thread_executor.ThreadExecutor` -- sync functions
  in a thread pool (default for plain ``def`` tasks).
* :class:`~fastapi_taskflow.executors.process_executor.ProcessExecutor` -- sync or
  async functions in a ``ProcessPoolExecutor`` worker (opt-in via
  ``executor='process'`` on the ``@task`` decorator).

Select an executor per-task on the decorator::

    @task_manager.task(executor="process", retries=2)
    def render_pdf(data: dict) -> bytes:
        ...

See :mod:`fastapi_taskflow.executors.base` for the protocol and
:mod:`fastapi_taskflow.executors.process_executor` for process-specific
constraints and configuration.
"""

from .async_executor import AsyncExecutor
from .base import Executor, TaskExecutionContext
from .process_executor import LazyProcessExecutor, ProcessExecutor, TaskArgumentError
from .thread_executor import ThreadExecutor

__all__ = [
    "Executor",
    "TaskExecutionContext",
    "AsyncExecutor",
    "ThreadExecutor",
    "ProcessExecutor",
    "LazyProcessExecutor",
    "TaskArgumentError",
]
