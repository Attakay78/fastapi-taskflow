import asyncio
import inspect
from datetime import datetime
from typing import Callable

from .models import TaskConfig, TaskStatus
from .store import TaskStore


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
    """
    store.update(task_id, status=TaskStatus.RUNNING, start_time=datetime.utcnow())

    delay = config.delay
    last_error: Exception | None = None

    for attempt in range(config.retries + 1):
        if attempt > 0:
            await asyncio.sleep(delay)
            delay *= config.backoff
            store.update(task_id, retries_used=attempt)

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

    store.update(
        task_id,
        status=TaskStatus.FAILED,
        end_time=datetime.utcnow(),
        error=str(last_error),
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
