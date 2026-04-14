# Concurrency Controls

By default, fastapi-taskflow shares the event loop and thread pool with FastAPI request handlers. For most use cases this works fine. Under burst task load it can affect request latency.

Two opt-in parameters let you control this. Neither changes existing behaviour when not set.

## The default behaviour

**Async tasks** run as coroutines on the main event loop alongside request handlers. When many async tasks are active simultaneously, they compete with request handlers for scheduling time. Each task yields at every `await`, so requests still get scheduled, but P99 latency can climb under high task concurrency.

**Sync tasks** are offloaded via `asyncio.to_thread`, which submits them to the default `ThreadPoolExecutor`. That is the same pool FastAPI uses for sync route handlers. A burst of sync tasks can exhaust available threads, causing sync request handlers to queue behind them.

## `max_concurrent_tasks`

Caps how many async tasks run concurrently on the event loop using an `asyncio.Semaphore`.

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_concurrent_tasks=10,
)
```

When 10 tasks are already running, any new task waits for a slot before starting. Waiting tasks are parked without consuming event loop time, so request handlers always have consistent scheduling access regardless of task volume.

Tasks do not slow down once they start. They only wait before starting if the limit is reached.

**When to set this:** if you see elevated request latency during periods of high task activity. A value of 10 is a reasonable starting point for IO-bound tasks. Raise it if tasks queue up unnecessarily on idle systems.

## `max_sync_threads`

Runs sync task functions in a dedicated `ThreadPoolExecutor` instead of the default asyncio pool.

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_sync_threads=8,
)
```

Tasks and request handlers now have separate thread pools. A burst of sync tasks can exhaust the task pool entirely without affecting the threads available to sync request handlers.

**When to set this:** if you run sync task functions and see request latency increase when many tasks are active. Set it to `os.cpu_count() + 4` for IO-bound sync tasks, or closer to `os.cpu_count()` for CPU-bound ones.

## Using both together

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_concurrent_tasks=10,
    max_sync_threads=os.cpu_count() + 4,
)
```

Async tasks are capped at 10 concurrent. Sync tasks run in their own pool. Request handlers are unaffected by task load in both cases.

## What these controls do not solve

- **CPU-bound tasks** that saturate a core will still affect other work sharing that core. A dedicated thread pool helps parallelise across cores but does not remove the CPU contention. If your tasks are CPU-bound and high-volume, a separate worker process is the right answer.
- **Very long-running async tasks** that never yield will block the event loop regardless of the semaphore. Tasks should `await` regularly or delegate CPU work to a thread.
