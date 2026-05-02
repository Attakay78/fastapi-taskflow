"""AsyncExecutor: runs async task functions as coroutines on the event loop.

Equivalent to the implicit async dispatch path that existed before the executor
abstraction was introduced. Gated by an optional concurrency semaphore that
maps to ``max_concurrent_tasks`` on :class:`~fastapi_taskflow.TaskManager`.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
from typing import Any, Callable, Optional

from .base import TaskExecutionContext


class AsyncExecutor:
    """Runs ``async def`` task functions as coroutines on the event loop.

    The semaphore (when provided) caps how many coroutines hold event loop
    time simultaneously. Coroutines waiting for a semaphore slot yield control
    back to the event loop and do not block request handlers.

    This executor rejects sync functions at decoration time. Passing
    ``executor='async'`` on a plain ``def`` function raises :exc:`ValueError`
    immediately so the misconfiguration is caught before any task is enqueued.

    Attributes:
        name: Executor identifier. Always ``"async"``.
    """

    name: str = "async"

    def __init__(self, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        """
        Args:
            semaphore: Optional semaphore limiting concurrent coroutines.
                Acquired before each dispatch and released on completion,
                regardless of success or failure. Corresponds to
                ``max_concurrent_tasks`` on :class:`~fastapi_taskflow.TaskManager`.
                ``None`` means no limit (existing behaviour).
        """
        self._semaphore = semaphore

    async def dispatch(
        self,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
        context: TaskExecutionContext,
        exec_ctx: contextvars.Context,
        sink: Callable[[str, str, dict], None],
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        """Run *func* as a coroutine on the event loop, inside *exec_ctx*.

        Acquires the concurrency semaphore (if set) before creating the inner
        task and releases it in a ``finally`` block so cancellation or
        exceptions never leak the semaphore slot.

        On Python 3.11 and later, the inner task is created with
        ``context=exec_ctx`` so trace context, the log sink, and
        :func:`~fastapi_taskflow.task_logging.get_task_context` propagate
        automatically. On older Python versions a thin wrapper coroutine sets
        the relevant contextvars manually, which provides correct task-log
        behaviour at the cost of best-effort trace propagation.

        Args:
            func: An ``async def`` function to run.
            args: Positional arguments forwarded to *func*.
            kwargs: Keyword arguments forwarded to *func*.
            context: Per-task context for log and contextvar reconstruction.
            exec_ctx: Context snapshot with log sink and task context set.
                Used to create the inner :class:`asyncio.Task`.
            sink: Log sink for the current attempt. Embedded in *exec_ctx*
                for the Python 3.11+ path; used directly in the compat path.
            loop: Running event loop (unused by this executor; kept for
                protocol compatibility).

        Returns:
            Whatever *func* returns.

        Raises:
            Exception: Whatever *func* raises, propagated unchanged.
            asyncio.CancelledError: When the outer task is cancelled.
        """
        if self._semaphore is not None:
            await self._semaphore.acquire()

        _inner: Optional[asyncio.Task] = None
        try:
            if sys.version_info >= (3, 11):
                _inner = asyncio.create_task(func(*args, **kwargs), context=exec_ctx)
            else:
                _inner = asyncio.create_task(
                    self._run_compat(func, args, kwargs, context, sink)
                )
            return await _inner

        except asyncio.CancelledError:
            if _inner is not None and not _inner.done():
                _inner.cancel()
            raise

        finally:
            if self._semaphore is not None:
                self._semaphore.release()

    @staticmethod
    async def _run_compat(
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
        context: TaskExecutionContext,
        sink: Callable[[str, str, dict], None],
    ) -> Any:
        """Compat wrapper for Python < 3.11 where create_task lacks *context*.

        Sets ``_log_sink`` and ``_task_context`` directly on the coroutine's
        contextvar namespace so ``task_log()`` and ``get_task_context()`` work.
        Trace context propagation is best-effort on older Python.

        Args:
            func: The async task function.
            args: Positional arguments.
            kwargs: Keyword arguments.
            context: Per-task context for contextvar reconstruction.
            sink: Log sink for this attempt.

        Returns:
            Whatever *func* returns.
        """
        from fastapi_taskflow.task_logging import (
            TaskContext,
            _log_sink,
            _task_context,
        )

        ctx_obj = TaskContext(
            task_id=context.task_id,
            func_name=context.func_name,
            attempt=context.attempt,
            tags=context.tags,
        )
        t1 = _log_sink.set(sink)
        t2 = _task_context.set(ctx_obj)
        try:
            return await func(*args, **kwargs)
        finally:
            _log_sink.reset(t1)
            _task_context.reset(t2)

    async def shutdown(self, timeout: float | None = None) -> None:
        """No-op. This executor holds no resources of its own.

        Args:
            timeout: Ignored.
        """

    def validate(self, func: Callable[..., Any]) -> None:
        """Reject sync functions at decoration time.

        Args:
            func: The decorated function.

        Raises:
            ValueError: If *func* is not an ``async def`` function.
        """
        import inspect

        if not inspect.iscoroutinefunction(func):
            raise ValueError(
                f"Task '{func.__name__}' uses executor='async' but is a sync function. "
                "Use executor='thread' for sync functions, or make the function async."
            )

    def validate_args(self, args: tuple, kwargs: dict) -> None:
        """No-op. Async tasks have no argument constraints.

        Args:
            args: Positional arguments (ignored).
            kwargs: Keyword arguments (ignored).
        """
