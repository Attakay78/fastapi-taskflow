import asyncio
import inspect
import traceback
from datetime import datetime
from typing import Callable

from .models import TaskConfig, TaskStatus
from .store import TaskStore
from .task_logging import _reset_sink, _set_sink


async def execute_task(
    func: Callable,
    task_id: str,
    config: TaskConfig,
    store: TaskStore,
    args: tuple,
    kwargs: dict,
) -> None:
    """
    Run *func* inside the execution lifecycle:
      PENDING -> RUNNING -> SUCCESS | FAILED

    Applies retry logic and records all state transitions into *store*.
    Log entries emitted via :func:`task_log` are captured per attempt and
    appended to the task record.  The full stack trace of the final failure
    is stored as ``stacktrace``.
    """
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
) -> Callable:
    """Return a zero-argument async callable suitable for BackgroundTasks.add_task."""

    async def _wrapped() -> None:
        await execute_task(func, task_id, config, store, args, kwargs)

    _wrapped.__name__ = f"_bg_{func.__name__}_{task_id[:8]}"
    return _wrapped
