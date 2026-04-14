"""Task execution engine.

:func:`execute_task` is the single function responsible for running a task
function through its full lifecycle: status transitions, retry loop,
log capture, and optional persistence on completion.

:func:`make_background_func` wraps ``execute_task`` into a zero-argument
async callable that FastAPI's ``BackgroundTasks`` can call after the response
is sent.
"""

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from .models import TaskConfig, TaskStatus
from .store import TaskStore
from .task_logging import (
    TaskContext,
    _log_sink,
    _task_context,
)

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend
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


def _schedule_log(
    coro: "Coroutine[Any, Any, Any]",
    loop: asyncio.AbstractEventLoop,
    pending: list,
) -> None:
    """Schedule *coro* and append the resulting future to *pending* for later draining.

    When called from the event loop thread, ``ensure_future`` is used and an
    ``asyncio.Task`` is appended. When called from a thread-pool thread (sync tasks
    run via ``asyncio.to_thread``), ``run_coroutine_threadsafe`` is used and a
    ``concurrent.futures.Future`` is appended. In both cases the caller drains
    *pending* with :func:`_drain_pending` after the task function returns.
    """
    try:
        asyncio.get_running_loop()
        pending.append(asyncio.ensure_future(coro))
    except RuntimeError:
        pending.append(asyncio.run_coroutine_threadsafe(coro, loop))


async def _drain_pending(pending: list) -> None:
    """Await all futures collected by :func:`_schedule_log`, then clear the list.

    ``asyncio.Task`` objects are awaited directly. ``concurrent.futures.Future``
    objects (from thread-pool log events) are wrapped via ``asyncio.wrap_future``
    before awaiting.
    """
    if not pending:
        return
    awaitables = [
        f if isinstance(f, asyncio.Task) else asyncio.wrap_future(f) for f in pending
    ]
    pending.clear()
    await asyncio.gather(*awaitables, return_exceptions=True)


async def execute_task(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
    backend: "Optional[SnapshotBackend]" = None,
    on_success: "Optional[Callable]" = None,
    logger: "Optional[TaskObserver]" = None,
    encryptor: Any = None,
    captured_ctx: Optional[contextvars.Context] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    sync_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None,
) -> None:
    """Run *func* through the full task lifecycle: PENDING -> RUNNING -> SUCCESS | FAILED.

    Handles the retry loop, records every status transition in *store*, and
    captures :func:`~fastapi_taskflow.task_logging.task_log` entries per attempt.
    The full traceback of the final failure is stored on the task record.

    Both sync and async functions are supported. Sync functions are offloaded
    to a thread pool so they do not block the event loop. When *sync_executor*
    is provided, tasks run in that dedicated pool instead of the default
    ``asyncio`` thread pool, isolating sync task execution from sync request
    handlers.

    Args:
        func: The task function to run (sync or async).
        task_id: ID of the record already created in *store*.
        config: Retry/delay settings from ``@task_manager.task()``.
        store: The in-memory store where status updates are written.
        args: Positional arguments to pass to *func*. Ignored when the record
            carries an ``encrypted_payload`` and *encryptor* is provided.
        kwargs: Keyword arguments to pass to *func*. Ignored when the record
            carries an ``encrypted_payload`` and *encryptor* is provided.
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
        semaphore: Optional ``asyncio.Semaphore`` that caps how many async tasks
            hold event loop time simultaneously. When set, the semaphore is
            acquired before execution begins and released on completion,
            regardless of success or failure. Tasks waiting for a slot do not
            block the event loop. Corresponds to ``max_concurrent_tasks`` on
            :class:`~fastapi_taskflow.TaskManager`.
        sync_executor: Optional dedicated ``ThreadPoolExecutor`` for sync task
            functions. When set, sync functions run in this pool instead of the
            default ``asyncio`` thread pool, preventing task bursts from
            exhausting threads needed by sync request handlers. Corresponds to
            ``max_sync_threads`` on :class:`~fastapi_taskflow.TaskManager`.
    """
    if semaphore is not None:
        await semaphore.acquire()

    try:
        await _execute_task_inner(
            func=func,
            task_id=task_id,
            config=config,
            store=store,
            args=args,
            kwargs=kwargs,
            backend=backend,
            on_success=on_success,
            logger=logger,
            encryptor=encryptor,
            captured_ctx=captured_ctx,
            sync_executor=sync_executor,
        )
    finally:
        if semaphore is not None:
            semaphore.release()


async def _execute_task_inner(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
    backend: "Optional[SnapshotBackend]" = None,
    on_success: "Optional[Callable]" = None,
    logger: "Optional[TaskObserver]" = None,
    encryptor: Any = None,
    captured_ctx: Optional[contextvars.Context] = None,
    sync_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None,
) -> None:
    """Inner execution body. Called by :func:`execute_task` after the optional
    semaphore is acquired. Not intended for direct use."""
    from .loggers.base import LifecycleEvent, LogEvent

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
        import pickle

        actual_args, actual_kwargs = pickle.loads(
            encryptor.decrypt(record.encrypted_payload)
        )
    else:
        actual_args, actual_kwargs = args, kwargs

    # Capture the running event loop now. The sink closure may be called from
    # a thread-pool thread (sync tasks via asyncio.to_thread), so we hold the
    # loop reference to schedule coroutines safely via run_coroutine_threadsafe.
    loop = asyncio.get_running_loop()

    # Cross-instance idempotency check.
    if record is not None and record.idempotency_key and backend is not None:
        existing_id = await backend.check_idempotency_key(record.idempotency_key)
        if existing_id is not None and existing_id != task_id:
            store.update(
                task_id,
                status=TaskStatus.SUCCESS,
                start_time=datetime.now(timezone.utc),
                end_time=datetime.now(timezone.utc),
            )
            return

    start_time = datetime.now(timezone.utc)
    store.update(task_id, status=TaskStatus.RUNNING, start_time=start_time)

    if logger is not None:
        await logger.on_lifecycle(
            LifecycleEvent(
                task_id=task_id,
                func_name=func_name,
                status=TaskStatus.RUNNING,
                timestamp=start_time,
                attempt=0,
                retries_used=0,
                tags=tags,
            )
        )

    # _state tracks the current attempt index so the sink closure always
    # attaches the right number to LogEvent objects. A dict avoids Python
    # cell-variable rebinding issues.
    _state: dict[str, int] = {"attempt": 0}

    # Pending log futures: collected by the sink, drained after each attempt
    # so all on_log() calls complete before the lifecycle event fires.
    _pending_log: list = []

    def sink(msg: str, level: str, extra: dict) -> None:
        ts = datetime.now(timezone.utc)
        store.append_log(task_id, f"{ts.strftime('%Y-%m-%dT%H:%M:%S')} {msg}")
        if logger is not None:
            event = LogEvent(
                task_id=task_id,
                func_name=func_name,
                message=msg,
                level=level,
                timestamp=ts,
                attempt=_state["attempt"],
                tags=tags,
                extra=extra,
            )
            _schedule_log(logger.on_log(event), loop, _pending_log)

    delay = config.delay
    last_error: Exception | None = None
    last_tb: str | None = None

    for attempt in range(config.retries + 1):
        _state["attempt"] = attempt

        if attempt > 0:
            await asyncio.sleep(delay)
            delay *= config.backoff
            store.update(task_id, retries_used=attempt)
            store.append_log(task_id, f"--- Retry {attempt} ---")

        ctx_obj = TaskContext(
            task_id=task_id,
            func_name=func_name,
            attempt=attempt,
            tags=tags,
        )

        # Build an execution context that merges trace context (from captured_ctx)
        # with the task-specific log sink and TaskContext vars.
        exec_ctx = _build_exec_ctx(captured_ctx, sink, ctx_obj)

        try:
            if inspect.iscoroutinefunction(func):
                # asyncio.create_task supports context= only on Python 3.11+.
                # On older versions fall back to a wrapper coroutine that sets
                # the task-specific vars directly; trace context propagation
                # is best-effort and requires Python 3.11+.
                import sys

                if sys.version_info >= (3, 11):
                    task = asyncio.create_task(
                        func(*actual_args, **actual_kwargs), context=exec_ctx
                    )
                    await task
                else:

                    async def _run_in_ctx() -> None:
                        t1 = _log_sink.set(sink)
                        t2 = _task_context.set(ctx_obj)
                        try:
                            await func(*actual_args, **actual_kwargs)
                        finally:
                            _log_sink.reset(t1)
                            _task_context.reset(t2)

                    await _run_in_ctx()
            else:
                # Run the sync function in a thread, inside exec_ctx so both
                # trace vars and task_log() work correctly.
                # When a dedicated executor is configured, use it to isolate
                # sync task threads from the default asyncio thread pool used
                # by sync request handlers.
                _loop = asyncio.get_running_loop()
                if sync_executor is not None:
                    # run_in_executor does not support kwargs directly.
                    # Use functools.partial to bind both args and kwargs before
                    # passing to exec_ctx.run so keyword arguments are preserved.
                    await _loop.run_in_executor(
                        sync_executor,
                        functools.partial(
                            exec_ctx.run, func, *actual_args, **actual_kwargs
                        ),
                    )
                else:
                    await asyncio.to_thread(
                        exec_ctx.run, func, *actual_args, **actual_kwargs
                    )

            await _drain_pending(_pending_log)

            end_time = datetime.now(timezone.utc)
            store.update(task_id, status=TaskStatus.SUCCESS, end_time=end_time)

            if logger is not None:
                duration = (end_time - start_time).total_seconds()
                await logger.on_lifecycle(
                    LifecycleEvent(
                        task_id=task_id,
                        func_name=func_name,
                        status=TaskStatus.SUCCESS,
                        timestamp=end_time,
                        attempt=attempt,
                        retries_used=attempt,
                        duration=duration,
                        tags=tags,
                    )
                )

            if on_success is not None:
                await on_success(task_id)
            if record is not None and record.idempotency_key and backend is not None:
                await backend.record_idempotency_key(record.idempotency_key, task_id)
            return

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            last_tb = traceback.format_exc()

    await _drain_pending(_pending_log)

    end_time = datetime.now(timezone.utc)
    store.update(
        task_id,
        status=TaskStatus.FAILED,
        end_time=end_time,
        error=str(last_error),
        stacktrace=last_tb,
    )

    if logger is not None:
        duration = (end_time - start_time).total_seconds()
        await logger.on_lifecycle(
            LifecycleEvent(
                task_id=task_id,
                func_name=func_name,
                status=TaskStatus.FAILED,
                timestamp=end_time,
                attempt=config.retries,
                retries_used=config.retries,
                duration=duration,
                error=str(last_error),
                stacktrace=last_tb,
                tags=tags,
            )
        )


def make_background_func(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
    backend: "Optional[SnapshotBackend]" = None,
    on_success: "Optional[Callable]" = None,
    logger: "Optional[TaskObserver]" = None,
    encryptor: Any = None,
    captured_ctx: Optional[contextvars.Context] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    sync_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None,
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
        backend: Optional snapshot backend for idempotency.
        on_success: Optional callback invoked after SUCCESS.
        logger: Optional observer chain for structured event delivery.
        encryptor: Optional ``Fernet`` instance for decrypting args at run time.
        captured_ctx: Optional ``contextvars`` context snapshot from enqueue time
            for trace context propagation.
        semaphore: Optional semaphore forwarded to :func:`execute_task` to cap
            concurrent async task execution.
        sync_executor: Optional thread pool forwarded to :func:`execute_task`
            to isolate sync task threads from the default asyncio pool.

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
            backend=backend,
            on_success=on_success,
            logger=logger,
            encryptor=encryptor,
            captured_ctx=captured_ctx,
            semaphore=semaphore,
            sync_executor=sync_executor,
        )

    _wrapped.__name__ = f"_bg_{func.__name__}_{task_id[:8]}"
    return _wrapped
