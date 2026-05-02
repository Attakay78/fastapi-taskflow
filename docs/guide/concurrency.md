# Concurrency Controls

This page explains how to stop background tasks from competing with your request handlers under burst load, and how to run CPU-bound tasks in separate OS processes.

## The default behaviour

By default, fastapi-taskflow shares the event loop and the default thread pool with FastAPI itself. For most applications this is perfectly fine. The event loop schedules everything cooperatively, and there is no conflict.

Problems appear when many tasks are enqueued at once.

Imagine 50 tasks arrive at the same time, each making several network calls. All 50 start immediately, flooding the event loop with coroutines. Your request handlers are coroutines on that same loop. They still get scheduled at each `await` boundary, but P99 latency climbs because there are now 50 competitors for every scheduling slot.

Sync tasks make this even more concrete. FastAPI runs sync route handlers in a `ThreadPoolExecutor`. So does asyncio's `to_thread`. If 50 sync tasks fill every thread in that shared pool, the next sync request handler has to wait in line behind them.

Neither of these is a bug. They are the natural result of sharing resources. The settings below let you opt out of that sharing.

!!! note
    `max_concurrent_tasks` and `max_sync_threads` both default to `None`. If you do not set them, nothing changes from the default FastAPI behaviour.

## `max_concurrent_tasks`

This setting caps how many async tasks can run at the same time using an `asyncio.Semaphore`. Tasks that arrive when the cap is reached wait for a slot before starting.

While they wait, they are parked and consume no event loop time. Your request handlers continue to get consistent scheduling access regardless of how many tasks are queued behind the semaphore.

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_concurrent_tasks=10,
)
```

Once a task acquires its slot and starts, it runs at full speed. The semaphore only controls entry, not execution pace.

!!! tip
    Start with a value of 10 for IO-bound tasks. If tasks are queuing up on an otherwise idle system, raise the value. If request latency is still elevated under load, lower it.

## `max_sync_threads`

Sync task functions run in a thread pool via `asyncio.to_thread`. By default that is the same pool asyncio uses for everything else, including sync FastAPI route handlers. Setting `max_sync_threads` creates a dedicated `ThreadPoolExecutor` exclusively for sync tasks.

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_sync_threads=8,
)
```

With a dedicated pool, a burst of sync tasks can fill their own pool entirely without touching the threads available to your sync request handlers. The two workloads no longer interfere with each other.

!!! tip
    For IO-bound sync tasks, `os.cpu_count() + 4` is a reasonable starting point. For CPU-bound sync tasks, stay closer to `os.cpu_count()` to avoid over-scheduling on the available cores.

## Using both together

The two settings are independent and compose naturally.

```python hl_lines="6 7"
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_concurrent_tasks=10,
    max_sync_threads=os.cpu_count() + 4,
)
```

Async tasks are capped at 10 concurrent. Sync tasks run in their own pool and never compete with request handlers for threads. Both limits operate simultaneously without interfering with each other.

## CPU-bound tasks

Thread-based concurrency does not help with CPU-bound work. Python threads share the GIL, so only one thread executes Python bytecode at a time. A task that saturates a CPU core for several seconds will still affect other tasks sharing that core, regardless of how many threads you allocate.

For CPU-bound tasks, use `executor='process'`:

```python
@task_manager.task(executor="process")
def generate_report(report_id: int) -> bytes:
    return render_pdf(report_id)
```

Process tasks run in a separate `ProcessPoolExecutor` using the `spawn` start method. Each worker is an independent Python interpreter, so the GIL is not a factor and workers run in true parallel on separate cores.

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    max_process_workers=os.cpu_count(),
    process_shutdown_timeout=60.0,
)
```

See [Process Executor](process-tasks.md) for the full guide, including constraints and configuration.

!!! note
    There is one remaining edge case for async tasks: a coroutine that never yields will block the event loop for as long as it runs, regardless of the semaphore. Tasks should `await` regularly, or delegate any CPU-heavy work to a thread or process.
