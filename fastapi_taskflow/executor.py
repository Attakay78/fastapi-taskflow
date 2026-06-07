"""Task execution engine.

:func:`execute_task` is the single function responsible for running a task
function through its full lifecycle. Internally it builds an
:class:`~fastapi_taskflow.middleware.ExecContext` and runs it through the
default middleware pipeline:

1. :class:`~fastapi_taskflow.middleware.IdempotencyMiddleware`
2. :class:`~fastapi_taskflow.middleware.LoggingMiddleware`
3. :class:`~fastapi_taskflow.middleware.RetryMiddleware`
4. :func:`_dispatch_endpoint` (executor dispatch)

:func:`make_background_func` wraps ``execute_task`` into a zero-argument
async callable that FastAPI's ``BackgroundTasks`` can call after the response
is sent.

Dispatch is routed through the :class:`~fastapi_taskflow.executors.base.Executor`
protocol so that async, thread, and process execution share a uniform interface.
The concrete executor instance is selected once at enqueue time (in
:class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks`) and carried through
to this module as the ``executor_obj`` parameter.
"""

import asyncio
import contextvars
import pickle
import sys
from typing import TYPE_CHECKING, Any, Callable, Optional

from .executors.base import TaskExecutionContext
from .middleware import (
    ExecContext,
    IdempotencyMiddleware,
    LoggingMiddleware,
    RetryMiddleware,
    build_pipeline,
)
from .models import TaskConfig
from .store import TaskStore
from .task_logging import (
    TaskContext,
    _log_sink,
    _task_context,
)

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend
    from .executors.base import Executor
    from .loggers.base import TaskObserver


def _build_exec_ctx(
    captured_ctx: Optional[contextvars.Context],
    sink_fn: Callable,
    task_ctx: TaskContext,
) -> contextvars.Context:
    """Build an execution context for one task attempt.

    Starts from *captured_ctx* (preserving any trace context from the original
    request) and layers the task-specific ``_log_sink`` and ``_task_context``
    vars on top. When *captured_ctx* is ``None``, starts from a copy of the
    current context instead.

    The returned context is used to run the task function so that:

    * Trace context (OpenTelemetry spans, etc.) flows from the enqueue site
      into the background execution automatically.
    * ``task_log()`` and ``get_task_context()`` work correctly inside the
      task function regardless of whether it is sync or async.

    Args:
        captured_ctx: Context snapshot taken at ``add_task()`` time, or
            ``None`` when no context was captured (e.g. requeue, retry).
        sink_fn: The log sink closure for the current attempt.
        task_ctx: The :class:`~fastapi_taskflow.task_logging.TaskContext` for
            the current attempt.

    Returns:
        A :class:`contextvars.Context` ready to run the task function in.
    """
    result: list[contextvars.Context] = []

    def _setup() -> None:
        _log_sink.set(sink_fn)
        _task_context.set(task_ctx)
        result.append(contextvars.copy_context())

    base = captured_ctx if captured_ctx is not None else contextvars.copy_context()
    base.run(_setup)
    return result[0]


async def _dispatch_endpoint(ctx: ExecContext) -> None:
    """Innermost pipeline step: dispatch the task function via the executor.

    Builds the per-attempt execution context, routes through ``ctx.executor_obj``
    (or the legacy implicit dispatch path when it is ``None``), and registers
    the running task handle in ``ctx.running_tasks`` for cancellation support.
    """
    ctx_obj = TaskContext(
        task_id=ctx.task_id,
        func_name=ctx.func_name,
        attempt=ctx.attempt,
        tags=ctx.tags,
    )
    sink = ctx.sink or (lambda *_a, **_kw: None)
    exec_ctx = _build_exec_ctx(ctx.captured_ctx, sink, ctx_obj)
    task_exec_ctx = TaskExecutionContext(
        task_id=ctx.task_id,
        func_name=ctx.func_name,
        attempt=ctx.attempt,
        tags=ctx.tags,
    )

    if ctx.executor_obj is not None:
        dispatch_coro = ctx.executor_obj.dispatch(
            ctx.func,
            ctx.actual_args,
            ctx.actual_kwargs,
            task_exec_ctx,
            exec_ctx,
            sink,
            ctx.loop,
        )
        ctx._inner_task = asyncio.ensure_future(dispatch_coro)
    else:
        # Legacy implicit dispatch: auto-detect from function signature.
        # Preserved for backward compatibility with direct execute_task callers.
        import inspect as _inspect

        if _inspect.iscoroutinefunction(ctx.func):
            if sys.version_info >= (3, 11):
                ctx._inner_task = asyncio.create_task(
                    ctx.func(*ctx.actual_args, **ctx.actual_kwargs), context=exec_ctx
                )
            else:

                async def _run_in_ctx() -> None:
                    t1 = _log_sink.set(ctx.sink)
                    t2 = _task_context.set(ctx_obj)
                    try:
                        await ctx.func(*ctx.actual_args, **ctx.actual_kwargs)
                    finally:
                        _log_sink.reset(t1)
                        _task_context.reset(t2)

                ctx._inner_task = asyncio.create_task(_run_in_ctx())
        else:
            ctx._inner_task = asyncio.ensure_future(
                asyncio.to_thread(
                    exec_ctx.run, ctx.func, *ctx.actual_args, **ctx.actual_kwargs
                )
            )

    if ctx.running_tasks is not None:
        ctx.running_tasks[ctx.task_id] = ctx._inner_task
    await ctx._inner_task
    ctx._inner_task = None


_DEFAULT_MIDDLEWARE = [
    IdempotencyMiddleware(),
    LoggingMiddleware(),
    RetryMiddleware(),
]


async def execute_task(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
    executor_obj: "Optional[Executor]" = None,
    backend: "Optional[SnapshotBackend]" = None,
    on_success: "Optional[Callable]" = None,
    logger: "Optional[TaskObserver]" = None,
    encryptor: Any = None,
    captured_ctx: Optional[contextvars.Context] = None,
    running_tasks: Optional[dict] = None,
) -> None:
    """Run *func* through the full task lifecycle: PENDING -> RUNNING -> SUCCESS | FAILED.

    Builds an :class:`~fastapi_taskflow.middleware.ExecContext` and runs it
    through the default middleware pipeline (idempotency, logging, retry) before
    the executor dispatch endpoint.

    Dispatch is routed through *executor_obj*, which encapsulates whether the
    function runs as a coroutine, in a thread pool, or in a process pool.
    When *executor_obj* is ``None`` the executor is inferred from the function
    signature (async -> event loop, sync -> thread pool), preserving backward
    compatibility for call sites that have not yet been updated.

    Args:
        func: The task function to run (sync or async).
        task_id: ID of the record already created in *store*.
        config: Retry/delay settings from ``@task_manager.task()``.
        store: The in-memory store where status updates are written.
        args: Positional arguments to pass to *func*. Ignored when the record
            carries an ``encrypted_payload`` and *encryptor* is provided.
        kwargs: Keyword arguments to pass to *func*. Ignored when the record
            carries an ``encrypted_payload`` and *encryptor* is provided.
        executor_obj: Concrete executor that handles the actual function
            dispatch. When ``None``, the legacy implicit dispatch path is used
            (auto-detect from ``asyncio.iscoroutinefunction``).
        backend: When provided, used to check cross-instance idempotency keys
            before running and to record the key on success.
        on_success: Async callable invoked with *task_id* right after SUCCESS.
            Used by the snapshot scheduler to flush the record immediately so
            a crash before the next periodic flush does not cause re-execution.
        logger: When provided, :class:`~fastapi_taskflow.loggers.LogEvent` and
            :class:`~fastapi_taskflow.loggers.LifecycleEvent` objects are
            dispatched to it for every log entry and status transition.
        encryptor: A ``cryptography.fernet.Fernet`` instance. When provided and
            the task record has an ``encrypted_payload``, the payload is decrypted
            to recover the original ``(args, kwargs)`` before calling *func*.
        captured_ctx: ``contextvars`` context snapshot taken at ``add_task()``
            time. When provided, the task function runs inside this context so
            trace context (OpenTelemetry spans, etc.) propagates from the
            originating request into the background execution.
        running_tasks: Shared dict mapping ``task_id`` to the asyncio Task or
            Future currently executing that function. Populated when the task
            enters ``RUNNING`` and removed on completion or cancellation. Used
            by ``POST /tasks/{task_id}/cancel`` to cancel running async tasks.
    """
    record = store.get(task_id)
    func_name = func.__name__
    tags: dict[str, str] = record.tags if record is not None else {}

    # Resolve actual args/kwargs. When encryption is active, the store record
    # holds an encrypted_payload and empty args/kwargs; we decrypt here once.
    if (
        record is not None
        and record.encrypted_payload is not None
        and encryptor is not None
    ):
        actual_args, actual_kwargs = pickle.loads(
            encryptor.decrypt(record.encrypted_payload)
        )
    else:
        actual_args, actual_kwargs = args, kwargs

    loop = asyncio.get_running_loop()

    ctx = ExecContext(
        func=func,
        func_name=func_name,
        task_id=task_id,
        config=config,
        store=store,
        actual_args=actual_args,
        actual_kwargs=actual_kwargs,
        record=record,
        tags=tags,
        executor_obj=executor_obj,
        backend=backend,
        on_success=on_success,
        logger=logger,
        captured_ctx=captured_ctx,
        running_tasks=running_tasks,
        loop=loop,
    )

    pipeline = build_pipeline(ctx, _DEFAULT_MIDDLEWARE, _dispatch_endpoint)
    try:
        await pipeline()
    finally:
        if running_tasks is not None:
            running_tasks.pop(task_id, None)


def make_background_func(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
    executor_obj: "Optional[Executor]" = None,
    backend: "Optional[SnapshotBackend]" = None,
    on_success: "Optional[Callable]" = None,
    logger: "Optional[TaskObserver]" = None,
    encryptor: Any = None,
    captured_ctx: Optional[contextvars.Context] = None,
    running_tasks: Optional[dict] = None,
) -> Callable:
    """Wrap *func* into a zero-argument async callable for ``BackgroundTasks.add_task``.

    FastAPI's ``BackgroundTasks`` requires callables that take no arguments.
    This closes over all the parameters needed by :func:`execute_task` so
    the wrapper can be handed directly to Starlette.

    Args:
        func: The task function to run.
        task_id: ID of the record created in *store*.
        config: Retry/delay settings.
        store: The in-memory task store.
        args: Positional arguments for *func* (empty when encryption is on).
        kwargs: Keyword arguments for *func* (empty when encryption is on).
        executor_obj: Concrete executor that handles the actual function
            dispatch. Passed through to :func:`execute_task`. ``None``
            triggers the legacy implicit dispatch path.
        backend: Optional snapshot backend for idempotency.
        on_success: Optional callback invoked after SUCCESS.
        logger: Optional observer chain for structured event delivery.
        encryptor: Optional ``Fernet`` instance for decrypting args at run time.
        captured_ctx: Optional ``contextvars`` context snapshot from enqueue time
            for trace context propagation.
        running_tasks: Shared dict forwarded to :func:`execute_task` so the
            executor can register and deregister the inner task handle for
            cancellation support.

    Returns:
        A zero-argument async callable named ``_bg_{func_name}_{task_id[:8]}``.
    """

    async def _wrapped() -> None:
        await execute_task(
            func,
            task_id,
            config,
            store,
            args,
            kwargs,
            executor_obj=executor_obj,
            backend=backend,
            on_success=on_success,
            logger=logger,
            encryptor=encryptor,
            captured_ctx=captured_ctx,
            running_tasks=running_tasks,
        )

    _wrapped.__name__ = f"_bg_{func.__name__}_{task_id[:8]}"
    return _wrapped
