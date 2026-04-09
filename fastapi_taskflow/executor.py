import asyncio
import inspect
import traceback
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from .models import TaskConfig, TaskStatus
from .store import TaskStore
from .task_logging import _reset_sink, _set_sink

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend


async def execute_task(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
    backend: "Optional[SnapshotBackend]" = None,
    on_success: "Optional[Callable]" = None,
) -> None:
    """
    Run *func* inside the execution lifecycle:
      PENDING -> RUNNING -> SUCCESS | FAILED

    Applies retry logic and records all state transitions into *store*.
    Log entries emitted via :func:`task_log` are captured per attempt and
    appended to the task record.  The full stack trace of the final failure
    is stored as ``stacktrace``.

    Args:
        backend: Optional backend used for idempotency key checks and
            immediate history flush on success.
        on_success: Optional async callable invoked with ``task_id`` after
            the task transitions to SUCCESS.  Used by the snapshot scheduler
            to persist the record immediately so a post-success crash does
            not cause re-execution on restart.
    """
    record = store.get(task_id)

    # Cross-instance idempotency key check: if this task carries an
    # idempotency key, ask the shared backend whether another instance
    # already completed the same logical operation.
    if record is not None and record.idempotency_key and backend is not None:
        existing_id = await backend.check_idempotency_key(record.idempotency_key)
        if existing_id is not None and existing_id != task_id:
            # Another instance completed this operation — mark ours as SUCCESS
            # without running the function so the caller's task_id is still valid.
            store.update(
                task_id,
                status=TaskStatus.SUCCESS,
                start_time=datetime.utcnow(),
                end_time=datetime.utcnow(),
            )
            return

    store.update(task_id, status=TaskStatus.RUNNING, start_time=datetime.utcnow())

    # Closure bound once — executor decides what to do with each message.
    sink = lambda msg: store.append_log(task_id, msg)  # noqa: E731

    delay = config.delay
    last_error: Exception | None = None
    last_tb: str | None = None

    for attempt in range(config.retries + 1):
        if attempt > 0:
            await asyncio.sleep(delay)
            delay *= config.backoff
            store.update(task_id, retries_used=attempt)
            store.append_log(task_id, f"--- Retry {attempt} ---")

        token = _set_sink(sink)
        try:
            if inspect.iscoroutinefunction(func):
                await func(*args, **kwargs)
            else:
                await asyncio.to_thread(func, *args, **kwargs)

            store.update(
                task_id,
                status=TaskStatus.SUCCESS,
                end_time=datetime.utcnow(),
            )
            # Immediately persist to backend so a crash between now and the
            # next periodic flush does not cause this task to be re-executed.
            if on_success is not None:
                await on_success(task_id)
            # Record the idempotency key so other instances can detect this
            # completed operation via check_idempotency_key.
            if record is not None and record.idempotency_key and backend is not None:
                await backend.record_idempotency_key(record.idempotency_key, task_id)
            return

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            last_tb = traceback.format_exc()

        finally:
            # Always clear the sink before moving to the next attempt so
            # task_log() calls after the except block don't bleed across.
            _reset_sink(token)

    store.update(
        task_id,
        status=TaskStatus.FAILED,
        end_time=datetime.utcnow(),
        error=str(last_error),
        stacktrace=last_tb,
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
) -> Callable:
    """Return a zero-argument async callable suitable for BackgroundTasks.add_task."""

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
        )

    _wrapped.__name__ = f"_bg_{func.__name__}_{task_id[:8]}"
    return _wrapped
