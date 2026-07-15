# Production Configuration

The defaults are safe for development but leave several resources unbounded in production. This page covers what to set before going live and why each setting matters.

## Unbounded defaults

**`max_concurrent_tasks` is `None`**

All requests feed the same shared `TaskManager`. A burst of requests each submitting tasks means all those tasks start immediately and compete with your request handlers on the same event loop. Without a cap, 100 tasks arriving at once all run concurrently and your request handlers queue behind all of them.

**`max_sync_threads` is `None`**

Without a dedicated pool, sync tasks and sync FastAPI route handlers share the same default asyncio thread pool. A burst of sync tasks can fill every slot in that pool. The next `def` route handler waits behind them.

**Queue `max_size` is `None`**

Tasks that cannot execute immediately sit in an in-memory heap. Without a size limit that heap grows without bound. Under sustained load this becomes a memory leak.

## Baseline

```python
import os
from fastapi_taskflow import TaskManager
from fastapi_taskflow.models import QueueConfig

task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_concurrent_tasks=20,               # cap async tasks on the event loop
    max_sync_threads=os.cpu_count() + 4,   # isolate sync tasks from request handlers
    max_process_workers=os.cpu_count(),    # one worker per core for CPU-bound tasks
    queues={
        "default": QueueConfig(
            concurrency=20,
            max_size=500,                  # reject at 500 pending tasks instead of growing forever
        ),
    },
)
```

When a queue is full, `add_task()` raises `QueueFullError`. Return a `429` to the caller rather than letting it propagate as a 500.

```python
from fastapi import HTTPException
from fastapi_taskflow.exceptions import QueueFullError

try:
    await task_manager.add_task(my_task, arg1, arg2)
except QueueFullError:
    raise HTTPException(status_code=429, detail="Task queue is full, try again later.")
```

## Workload isolation with named queues

Because all requests share one `TaskManager`, a burst of slow tasks from one route can occupy every concurrency slot and delay fast, user-facing tasks submitted by other routes. Named queues solve this by giving each class of work its own concurrency pool and pending heap.

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_sync_threads=os.cpu_count() + 4,
    queues={
        "emails":   QueueConfig(concurrency=20, max_size=500),
        "reports":  QueueConfig(concurrency=3,  max_size=50),
        "webhooks": QueueConfig(concurrency=10, max_size=200),
    },
)
```

A `reports` queue running at `concurrency=3` can never block the `emails` queue running at `concurrency=20`, regardless of how many reports are pending. Each queue drains independently.

Route handlers that submit slow background work and route handlers that submit fast user-facing work no longer compete for the same slots.

```python
@app.post("/reports/generate")
async def generate_report(background_tasks: BackgroundTasks):
    await task_manager.add_task(build_report, queue="reports")

@app.post("/users/welcome")
async def send_welcome(background_tasks: BackgroundTasks):
    await task_manager.add_task(send_email, queue="emails")
```

Set `max_size` on every queue. Without it the heap for that queue grows without bound when tasks arrive faster than they can execute.

See [Named Queues](named-queues.md) for the full guide including live config updates and queue stats.

