"""Task execution middleware pipeline.

Each middleware is an async callable with the signature::

    async def __call__(self, ctx: ExecContext, call_next: NextFn) -> None: ...

Middlewares are composed into a chain by :func:`build_pipeline`. The default
stack applied by :func:`~fastapi_taskflow.executor.execute_task` is, from
outermost to innermost:

1. :class:`IdempotencyMiddleware` -- skip duplicate executions, record key on
   success.
2. :class:`LoggingMiddleware` -- RUNNING status, lifecycle events, sink wiring,
   final SUCCESS/FAILED/CANCELLED finalisation.
3. :class:`RetryMiddleware` -- retry loop with delay and exponential backoff.

The innermost callable is the executor dispatch step, wired in by
:func:`~fastapi_taskflow.executor._dispatch_endpoint`.

Custom middleware can be inserted between any of the built-in layers or at
the outermost position. Middleware must propagate :class:`asyncio.CancelledError`
without swallowing it so the shutdown/cancel machinery in
:class:`LoggingMiddleware` can finalise the record correctly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .models import TaskConfig, TaskStatus

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend
    from .executors.base import Executor
    from .loggers.base import TaskObserver
    from .models import TaskRecord
    from .store import TaskStore

from .loggers.base import LifecycleEvent, LogEvent

_log = logging.getLogger(__name__)

# Type alias for the "call next middleware" argument.
NextFn = Callable[[], Awaitable[None]]


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ExecContext:
    """Mutable state bag threaded through the middleware pipeline for one task run.

    Middleware layers read and write fields on this object rather than passing
    values through return values, keeping the ``NextFn`` signature parameter-free.
    """

    # --- task identity ---
    func: Callable
    func_name: str
    task_id: str
    config: TaskConfig
    store: "TaskStore"
    actual_args: tuple
    actual_kwargs: dict
    record: "Optional[TaskRecord]"
    tags: dict

    # --- runtime handles ---
    executor_obj: "Optional[Executor]"
    backend: "Optional[SnapshotBackend]"
    on_success: "Optional[Callable]"
    logger: "Optional[TaskObserver]"
    captured_ctx: Any  # contextvars.Context | None
    running_tasks: "Optional[dict]"
    loop: asyncio.AbstractEventLoop

    # --- mutable state written during execution ---

    attempt: int = 0
    """Current retry attempt index (0 = first run). Updated by RetryMiddleware."""

    task_start: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    """Wall time when the task entered RUNNING. Set by LoggingMiddleware."""

    attempt_start: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    """Wall time at the start of the current attempt. Set by RetryMiddleware."""

    last_error: Optional[Exception] = None
    """Last exception from a dispatch attempt. Non-None after all retries exhaust."""

    last_tb: Optional[str] = None
    """Formatted traceback for last_error, used in the FAILED record."""

    succeeded: bool = False
    """Set to True by LoggingMiddleware on SUCCESS so IdempotencyMiddleware can
    record the idempotency key after the pipeline returns."""

    pending_log: list = dataclasses.field(default_factory=list)
    """Pending observer on_log futures. Drained by RetryMiddleware after each attempt."""

    sink: Optional[Callable] = None
    """Log sink closure wired up by LoggingMiddleware and passed to the executor."""

    _inner_task: Optional[asyncio.Task] = None
    """Handle to the currently dispatched asyncio Task. Held so LoggingMiddleware
    can cancel it on CancelledError."""


# ---------------------------------------------------------------------------
# Pipeline utilities
# ---------------------------------------------------------------------------


def build_pipeline(
    ctx: ExecContext,
    middlewares: list,
    endpoint: Callable[[ExecContext], Awaitable[None]],
) -> Callable[[], Awaitable[None]]:
    """Chain *middlewares* around *endpoint* and return a zero-argument coroutine.

    Middlewares are applied outermost-first: ``middlewares[0]`` is the first to
    run and the last to return.

    Args:
        ctx: Shared execution context passed to every layer.
        middlewares: Ordered list of middleware callables.
        endpoint: Innermost async callable that performs the actual dispatch.

    Returns:
        A zero-argument async callable that runs the full pipeline.
    """

    async def _endpoint() -> None:
        await endpoint(ctx)

    chain: Callable[[], Awaitable[None]] = _endpoint
    for mw in reversed(middlewares):
        _next = chain
        _mw = mw

        async def _step(_next: Callable = _next, _mw: Any = _mw) -> None:
            await _mw(ctx, _next)

        chain = _step

    return chain


# ---------------------------------------------------------------------------
# Scheduling helpers (used by LoggingMiddleware)
# ---------------------------------------------------------------------------


def _schedule_log(
    coro: Any,
    loop: asyncio.AbstractEventLoop,
    pending: list,
) -> None:
    """Schedule a log observer coroutine from any calling context.

    When called from the event loop thread, ``ensure_future`` is used. When
    called from a thread-pool thread (sync tasks via ``asyncio.to_thread``),
    ``run_coroutine_threadsafe`` is used. In both cases the resulting future is
    appended to *pending* for later draining.
    """
    try:
        asyncio.get_running_loop()
        pending.append(asyncio.ensure_future(coro))
    except RuntimeError:
        pending.append(asyncio.run_coroutine_threadsafe(coro, loop))


async def _drain_pending(pending: list) -> None:
    """Await all futures in *pending*, then clear it.

    ``asyncio.Task`` objects are awaited directly. ``concurrent.futures.Future``
    objects from thread-pool log events are wrapped via ``asyncio.wrap_future``.
    """
    if not pending:
        return
    awaitables = [
        f if isinstance(f, asyncio.Task) else asyncio.wrap_future(f) for f in pending
    ]
    pending.clear()
    await asyncio.gather(*awaitables, return_exceptions=True)


# ---------------------------------------------------------------------------
# Built-in middleware
# ---------------------------------------------------------------------------


class IdempotencyMiddleware:
    """Skip execution when another instance already completed this task.

    Checks the backend for an existing idempotency key before the pipeline
    runs. If a match is found for a different task ID, the record is marked
    SUCCESS immediately and the rest of the pipeline is skipped.

    After a successful run (``ctx.succeeded is True``), records the key so
    future instances skip this task.
    """

    async def __call__(self, ctx: ExecContext, call_next: NextFn) -> None:
        if (
            ctx.record is not None
            and ctx.record.idempotency_key
            and ctx.backend is not None
        ):
            try:
                existing_id = await ctx.backend.check_idempotency_key(
                    ctx.record.idempotency_key
                )
            except Exception:
                _log.exception(
                    "fastapi-taskflow: idempotency key check failed for task %s, "
                    "proceeding without deduplication.",
                    ctx.task_id,
                )
                existing_id = None

            if existing_id is not None and existing_id != ctx.task_id:
                now = datetime.now(timezone.utc)
                ctx.store.update(
                    ctx.task_id,
                    status=TaskStatus.SUCCESS,
                    start_time=now,
                    end_time=now,
                )
                return

        await call_next()

        if (
            ctx.succeeded
            and ctx.record is not None
            and ctx.record.idempotency_key
            and ctx.backend is not None
        ):
            try:
                await ctx.backend.record_idempotency_key(
                    ctx.record.idempotency_key, ctx.task_id
                )
            except Exception:
                _log.exception(
                    "fastapi-taskflow: failed to record idempotency key for task %s, "
                    "duplicate execution is possible if this task is retried externally.",
                    ctx.task_id,
                )


class LoggingMiddleware:
    """Manage task status transitions and emit lifecycle/log observer events.

    Sets the record to RUNNING before the inner pipeline runs, then finalises
    it as SUCCESS or FAILED when the retry loop returns. Handles
    :class:`asyncio.CancelledError` to set CANCELLED (user-initiated) or
    PENDING/INTERRUPTED (shutdown-driven) based on store shutdown state and
    ``config.requeue_on_interrupt``.

    Wires the per-attempt log sink onto ``ctx.sink`` so the executor dispatch
    step and task functions can emit :func:`~fastapi_taskflow.task_logging.task_log`
    entries that flow to the store and to any attached observer.
    """

    def _make_sink(self, ctx: ExecContext) -> Callable:
        """Return a log sink closure that reads ctx.attempt for event labelling."""

        def sink(msg: str, level: str, extra: dict) -> None:
            ts = datetime.now(timezone.utc)
            ctx.store.append_log(
                ctx.task_id, f"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
            )
            if ctx.logger is not None:
                event = LogEvent(
                    task_id=ctx.task_id,
                    func_name=ctx.func_name,
                    message=msg,
                    level=level,
                    timestamp=ts,
                    attempt=ctx.attempt,
                    tags=ctx.tags,
                    extra=extra,
                )
                _schedule_log(ctx.logger.on_log(event), ctx.loop, ctx.pending_log)

        return sink

    async def __call__(self, ctx: ExecContext, call_next: NextFn) -> None:
        ctx.task_start = datetime.now(timezone.utc)
        ctx.attempt_start = ctx.task_start
        ctx.store.update(
            ctx.task_id, status=TaskStatus.RUNNING, start_time=ctx.task_start
        )
        ctx.sink = self._make_sink(ctx)

        if ctx.logger is not None:
            await ctx.logger.on_lifecycle(
                LifecycleEvent(
                    task_id=ctx.task_id,
                    func_name=ctx.func_name,
                    status=TaskStatus.RUNNING,
                    timestamp=ctx.task_start,
                    attempt=0,
                    retries_used=0,
                    tags=ctx.tags,
                )
            )

        try:
            await call_next()
        except asyncio.CancelledError:
            if ctx._inner_task is not None and not ctx._inner_task.done():
                ctx._inner_task.cancel()
            await _drain_pending(ctx.pending_log)
            end_time = datetime.now(timezone.utc)
            current = ctx.store.get(ctx.task_id)
            if current is not None and current.status == TaskStatus.RUNNING:
                if ctx.store._shutting_down and ctx.config.requeue_on_interrupt:
                    ctx.store.update(ctx.task_id, status=TaskStatus.PENDING)
                elif ctx.store._shutting_down:
                    pass  # leave RUNNING for flush_pending to classify as INTERRUPTED
                else:
                    ctx.store.update(
                        ctx.task_id, status=TaskStatus.CANCELLED, end_time=end_time
                    )
                    if ctx.logger is not None:
                        duration = (end_time - ctx.attempt_start).total_seconds()
                        await ctx.logger.on_lifecycle(
                            LifecycleEvent(
                                task_id=ctx.task_id,
                                func_name=ctx.func_name,
                                status=TaskStatus.CANCELLED,
                                timestamp=end_time,
                                attempt=ctx.attempt,
                                retries_used=ctx.attempt,
                                duration=duration,
                                tags=ctx.tags,
                            )
                        )
                    if ctx.on_success is not None:
                        await ctx.on_success(ctx.task_id)
            raise

        end_time = datetime.now(timezone.utc)
        if ctx.last_error is None:
            ctx.succeeded = True
            ctx.store.update(ctx.task_id, status=TaskStatus.SUCCESS, end_time=end_time)
            if ctx.logger is not None:
                duration = (end_time - ctx.attempt_start).total_seconds()
                await ctx.logger.on_lifecycle(
                    LifecycleEvent(
                        task_id=ctx.task_id,
                        func_name=ctx.func_name,
                        status=TaskStatus.SUCCESS,
                        timestamp=end_time,
                        attempt=ctx.attempt,
                        retries_used=ctx.attempt,
                        duration=duration,
                        tags=ctx.tags,
                    )
                )
            if ctx.on_success is not None:
                await ctx.on_success(ctx.task_id)
        else:
            # Retry loop exhausted. Guard against shutdown having already finalised
            # the record (e.g. requeue_on_interrupt set it to PENDING).
            current = ctx.store.get(ctx.task_id)
            if current is not None and current.status != TaskStatus.RUNNING:
                return
            ctx.store.update(
                ctx.task_id,
                status=TaskStatus.FAILED,
                end_time=end_time,
                error=str(ctx.last_error),
                stacktrace=ctx.last_tb,
            )
            if ctx.logger is not None:
                duration = (end_time - ctx.attempt_start).total_seconds()
                await ctx.logger.on_lifecycle(
                    LifecycleEvent(
                        task_id=ctx.task_id,
                        func_name=ctx.func_name,
                        status=TaskStatus.FAILED,
                        timestamp=end_time,
                        attempt=ctx.config.retries,
                        retries_used=ctx.config.retries,
                        duration=duration,
                        error=str(ctx.last_error),
                        stacktrace=ctx.last_tb,
                        tags=ctx.tags,
                    )
                )


class RetryMiddleware:
    """Retry loop with configurable delay and exponential backoff.

    Runs the inner pipeline (dispatch endpoint) up to ``config.retries + 1``
    times. On each failure, captures the exception and traceback into
    ``ctx.last_error`` / ``ctx.last_tb`` and continues. On success, drains
    pending log futures and returns immediately.

    :class:`asyncio.CancelledError` and the ``_was_interrupted`` sentinel
    (from process workers killed by SIGINT) short-circuit the loop without
    recording an error.
    """

    async def __call__(self, ctx: ExecContext, call_next: NextFn) -> None:
        delay = ctx.config.delay
        for attempt in range(ctx.config.retries + 1):
            ctx.attempt = attempt

            if attempt > 0:
                await asyncio.sleep(delay)
                delay *= ctx.config.backoff
                ctx.store.update(ctx.task_id, retries_used=attempt)
                ctx.store.append_log(ctx.task_id, f"--- Retry {attempt} ---")

            ctx.attempt_start = datetime.now(timezone.utc)

            try:
                await call_next()
                await _drain_pending(ctx.pending_log)
                # Clear errors from previous attempts so LoggingMiddleware
                # finalises as SUCCESS, not FAILED.
                ctx.last_error = None
                ctx.last_tb = None
                return  # success — stop retrying
            except asyncio.CancelledError:
                await _drain_pending(ctx.pending_log)
                raise
            except Exception as exc:  # noqa: BLE001
                ctx._inner_task = None
                if getattr(exc, "_was_interrupted", False):
                    return
                await _drain_pending(ctx.pending_log)
                ctx.last_error = exc
                ctx.last_tb = (
                    getattr(exc, "_worker_traceback", None) or traceback.format_exc()
                )
        # All retries exhausted. Return normally with ctx.last_error set so
        # LoggingMiddleware can finalise the record as FAILED.
