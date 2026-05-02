"""Executor protocol and cross-boundary task execution context.

The :class:`Executor` protocol defines the interface every dispatch path
implements. :class:`TaskExecutionContext` is the per-task state carrier that
can be serialized across the process boundary for process executor tasks.

All three built-in executors satisfy the protocol structurally -- they do not
inherit from it, which keeps the inheritance graph flat and avoids import
cycles.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class TaskExecutionContext:
    """Per-task state passed into every executor dispatch call.

    Carries the fields needed to populate :func:`~fastapi_taskflow.task_logging.task_log`
    and :func:`~fastapi_taskflow.task_logging.get_task_context` correctly during
    execution. For the process executor this object is serialized into the
    dispatch payload so workers can reconstruct the same context on the other
    side of the process boundary.

    Attributes:
        task_id: UUID of the task being executed.
        func_name: Registered display name of the task function.
        attempt: Zero-based retry attempt index.
        tags: Key/value labels attached at enqueue time.
    """

    task_id: str
    func_name: str
    attempt: int
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for cross-process transfer.

        Returns:
            A JSON-safe dict with only basic Python types so it survives
            ``pickle`` and ``multiprocessing`` transport without issue.
        """
        return {
            "task_id": self.task_id,
            "func_name": self.func_name,
            "attempt": self.attempt,
            "tags": dict(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskExecutionContext":
        """Reconstruct from a dict produced by :meth:`to_dict`.

        Args:
            data: Dict as returned by :meth:`to_dict`.

        Returns:
            A new :class:`TaskExecutionContext` instance.
        """
        return cls(
            task_id=data["task_id"],
            func_name=data["func_name"],
            attempt=data["attempt"],
            tags=data.get("tags", {}),
        )


class Executor(Protocol):
    """Interface all dispatch paths implement.

    Each concrete executor wraps one underlying execution mechanism: an asyncio
    coroutine, a :class:`concurrent.futures.ThreadPoolExecutor` thread, or a
    :class:`concurrent.futures.ProcessPoolExecutor` worker. The protocol is
    satisfied structurally -- concrete classes do not need to inherit from it.

    All dispatch paths run inside the retry loop in
    :func:`~fastapi_taskflow.executor.execute_task`. The executor is
    responsible only for running the function and returning its result, or
    raising the exception the function raised. Status transitions, log capture,
    and retry logic remain in the caller.

    Executor instances are created once by :class:`~fastapi_taskflow.TaskManager`
    at startup and reused across all tasks of the matching type. Thread safety
    requirements vary by executor -- see each implementation for details.
    """

    name: str

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
        """Execute ``func(*args, **kwargs)`` and return its result.

        Must propagate exceptions from ``func`` unchanged so the retry loop
        can count attempts correctly. Must support cooperative cancellation
        where the underlying mechanism allows it. Must arrange for
        :func:`~fastapi_taskflow.task_logging.task_log` and
        :func:`~fastapi_taskflow.task_logging.get_task_context` to work
        correctly inside ``func``.

        Args:
            func: The task function to run.
            args: Positional arguments.
            kwargs: Keyword arguments.
            context: Per-task execution context for log and context round-trip.
                In-process executors set this on the appropriate contextvar.
                The process executor serializes it into the worker payload.
            exec_ctx: A :class:`contextvars.Context` snapshot that includes
                the log sink and task context set for this attempt. Used by
                in-process executors to propagate trace context from the
                originating request. Ignored by the process executor because
                contextvars do not cross the process boundary.
            sink: The log sink closure for the current attempt. Called by
                :func:`~fastapi_taskflow.task_logging.task_log` entries
                emitted inside the task. In-process executors embed this in
                ``exec_ctx``; the process executor uses it directly when
                routing log records from the worker queue back to the parent.
            loop: The running event loop. Used for thread-safe coroutine
                scheduling when log sink calls originate from a non-event-loop
                thread (thread executor, process executor drain thread).

        Returns:
            Whatever ``func`` returns on success.

        Raises:
            Exception: Whatever ``func`` raises, propagated unchanged.
            asyncio.CancelledError: When the dispatch was cancelled before
                ``func`` completed.
        """
        ...

    async def shutdown(self, timeout: float | None = None) -> None:
        """Drain in-flight work and release resources. Idempotent.

        Called by :meth:`~fastapi_taskflow.TaskManager.shutdown` for every
        executor in the registry. Executors that hold no resources (async,
        thread with default pool) implement this as a no-op.

        Args:
            timeout: Seconds to wait for in-flight work before forcing
                shutdown. ``None`` means wait indefinitely.
        """
        ...

    def validate(self, func: Callable[..., Any]) -> None:
        """Static checks at decoration time. Raise :exc:`ValueError` on misuse.

        Called once when the ``@task`` decorator runs with an explicit
        ``executor=`` argument. Mismatches that are configuration errors (e.g.
        ``executor='async'`` on a sync function) are caught here rather than
        at runtime.

        Args:
            func: The decorated function.

        Raises:
            ValueError: If ``func`` is incompatible with this executor.
        """
        ...

    def validate_args(self, args: tuple, kwargs: dict) -> None:
        """Per-enqueue checks. Raise :exc:`TaskArgumentError` on misuse.

        Called at ``add_task()`` time before the task is persisted to the
        store. Allows executors to reject arguments that would cause a runtime
        failure inside the executor (e.g. non-picklable args for process
        tasks), converting the error from a cryptic worker crash into a clear
        API error at the call site.

        Args:
            args: Positional arguments to be passed to the task function.
            kwargs: Keyword arguments to be passed to the task function.

        Raises:
            TaskArgumentError: If any argument cannot be used by this executor.
        """
        ...
