"""ThreadExecutor: runs sync task functions in a thread pool.

Equivalent to the implicit sync dispatch path that existed before the executor
abstraction was introduced. Uses either a dedicated :class:`concurrent.futures.ThreadPoolExecutor`
(when ``max_sync_threads`` is configured) or the default asyncio thread pool.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
from typing import Any, Callable, Optional

from .base import TaskExecutionContext


class ThreadExecutor:
    """Runs plain ``def`` task functions in a thread pool.

    Offloads sync work to a thread so the event loop is not blocked while the
    function runs. When a dedicated pool is provided it isolates task threads
    from the default asyncio thread pool used by sync request handlers,
    preventing a burst of CPU-bound sync tasks from exhausting threads needed
    elsewhere.

    This executor rejects async functions at decoration time. Passing
    ``executor='thread'`` on an ``async def`` function raises :exc:`ValueError`
    immediately.

    Attributes:
        name: Executor identifier. Always ``"thread"``.
    """

    name: str = "thread"

    def __init__(
        self,
        pool: Optional[concurrent.futures.ThreadPoolExecutor] = None,
    ) -> None:
        """
        Args:
            pool: Optional dedicated thread pool for sync task functions.
                When ``None``, tasks run in the default asyncio thread pool via
                :func:`asyncio.to_thread`. Corresponds to ``max_sync_threads``
                on :class:`~fastapi_taskflow.TaskManager`.
        """
        self._pool = pool

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
        """Run *func* in a thread pool, inside *exec_ctx* for contextvar propagation.

        Uses :meth:`exec_ctx.run` as the thread entrypoint so that
        :func:`~fastapi_taskflow.task_logging.task_log` and
        :func:`~fastapi_taskflow.task_logging.get_task_context` work correctly
        from inside the thread. Keyword arguments are bound via
        :func:`functools.partial` before passing to ``exec_ctx.run`` because
        :meth:`~asyncio.loop.run_in_executor` does not accept keyword arguments
        directly.

        When a dedicated pool is configured (``max_sync_threads`` set on
        :class:`~fastapi_taskflow.TaskManager`), tasks run in that isolated
        pool. Otherwise they run in the default asyncio thread pool.

        Cancellation stops the ``await`` but the underlying thread runs to
        completion in the background because threads cannot be interrupted
        mid-execution. This matches the behaviour of the original sync dispatch
        path.

        Args:
            func: A plain ``def`` function to run in a thread.
            args: Positional arguments forwarded to *func*.
            kwargs: Keyword arguments forwarded to *func*.
            context: Per-task context (not used directly; already embedded in
                *exec_ctx* via ``_task_context`` contextvar).
            exec_ctx: Context snapshot with log sink and task context set.
                Passed as the entrypoint to the thread so contextvars
                propagate correctly across the thread boundary.
            sink: Log sink for the current attempt (embedded in *exec_ctx*;
                kept in the signature for protocol compatibility).
            loop: Running event loop (unused; kept for protocol compatibility).

        Returns:
            Whatever *func* returns.

        Raises:
            Exception: Whatever *func* raises, propagated unchanged.
            asyncio.CancelledError: When the outer task is cancelled.
        """
        event_loop = asyncio.get_running_loop()
        bound = functools.partial(exec_ctx.run, func, *args, **kwargs)

        if self._pool is not None:
            fut = asyncio.ensure_future(event_loop.run_in_executor(self._pool, bound))
        else:
            fut = asyncio.ensure_future(asyncio.to_thread(bound))

        return await fut

    async def shutdown(self, timeout: float | None = None) -> None:
        """No-op. The dedicated pool is managed by TaskManager.shutdown().

        The thread pool lifetime is tied to the TaskManager. TaskManager calls
        ``pool.shutdown(wait=True)`` directly in its own shutdown sequence,
        after this method returns. Nothing to do here.

        Args:
            timeout: Ignored.
        """

    def validate(self, func: Callable[..., Any]) -> None:
        """Reject async functions at decoration time.

        Args:
            func: The decorated function.

        Raises:
            ValueError: If *func* is an ``async def`` function.
        """
        import inspect

        if inspect.iscoroutinefunction(func):
            raise ValueError(
                f"Task '{func.__name__}' uses executor='thread' but is an async function. "
                "Use executor='async' for async functions, or make the function a plain def."
            )

    def validate_args(self, args: tuple, kwargs: dict) -> None:
        """No-op. Thread tasks have no argument constraints.

        Args:
            args: Positional arguments (ignored).
            kwargs: Keyword arguments (ignored).
        """
