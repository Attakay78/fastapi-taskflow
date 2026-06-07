# Setting Up TaskManager

This page explains what `TaskManager` is, why it exists, and how to configure it for your application.

## What is TaskManager?

`TaskManager` is the central object in fastapi-taskflow. It holds the task registry, the in-memory task store, and every optional subsystem you enable: persistence, logging, encryption, concurrency controls, and process executor configuration.

You create one instance, wire it into your FastAPI app, and everything else flows from it.

## Minimal setup

All parameters are optional. The simplest possible setup is:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager()
```

Tasks are tracked in memory, with no persistence, no retries beyond what you configure per task, and no file logging. This is a perfectly valid starting point.

## Adding persistence

By default tasks are lost when the process restarts. Pass `snapshot_db` to write task history to a SQLite file:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
```

On startup, completed tasks are loaded back into memory. On shutdown, any remaining tasks are flushed to the file.

!!! tip
    Set `requeue_pending=True` alongside `snapshot_db` if you want tasks that were still waiting at shutdown to be automatically dispatched again on the next startup.

See [Backends](backends.md) for the Redis backend and guidance on writing custom backends.

## Adding logging

Pass `log_file` to write structured task events to a file. Add `log_lifecycle=True` to also record status transitions:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    log_file="tasks.log",
    log_lifecycle=True,
)
```

For more control, pass a list of observer objects directly. Multiple observers run independently, so an error in one never affects the others or the task itself:

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.loggers import StdoutLogger, FileLogger

task_manager = TaskManager(
    loggers=[
        StdoutLogger(min_level="info"),
        FileLogger("tasks.log", log_lifecycle=True),
    ]
)
```

See [Observability](observability.md) for the full observer API.

## Adding concurrency controls

By default, fastapi-taskflow imposes no limits on how many tasks run at once. This is fine for low-traffic applications, but under burst load you may want to protect the event loop:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    max_concurrent_tasks=50,
    max_sync_threads=10,
)
```

`max_concurrent_tasks` caps how many async tasks run concurrently on the event loop. When the limit is reached, new tasks wait for a slot without blocking request handlers.

`max_sync_threads` gives sync tasks their own thread pool, separate from the one FastAPI uses for sync route handlers. This prevents a burst of sync tasks from starving request handling.

!!! info
    Both default to `None`, which preserves existing FastAPI behaviour: no concurrency cap for async tasks, and sync tasks run in `asyncio.to_thread`.

See [Concurrency Controls](concurrency.md) for tuning guidance.

## Running CPU-bound tasks in separate processes

For tasks that saturate CPU cores (PDF rendering, image processing, data compression, numerical computation), use `executor='process'` to route them through a dedicated `ProcessPoolExecutor`:

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    max_process_workers=os.cpu_count(),
    process_shutdown_timeout=60.0,
)

@task_manager.task(executor="process")
def generate_report(report_id: int) -> bytes:
    return render_pdf(report_id)
```

`max_process_workers` sets the number of worker processes (`None` uses `os.cpu_count()`). `process_shutdown_timeout` is the number of seconds to wait for in-flight workers at shutdown before terminating them.

See [Process Executor](process-tasks.md) for constraints, `TaskArgumentError`, and the full configuration reference.

## Controlling retry history

When a failed, interrupted, or rejected task is retried via the API or dashboard, `retry_replaces_original` controls what happens to the original record:

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    retry_replaces_original=True,  # default
)
```

- `True` (default): The original record is deleted from the in-memory store and backend after the new task is dispatched. Only the new run appears in the dashboard and history.
- `False`: Both records are kept. The original stays in its terminal state and the new task appears alongside it as a separate entry.

This applies to all retry paths: single task retry, bulk retry, and timed bulk retry. It is independent of whether named queues are active.

## Adding named queues

Named queues give different classes of work their own concurrency budgets and backpressure caps. Without them, all tasks share the same global limit.

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.models import QueueConfig

task_manager = TaskManager(
    snapshot_db="tasks.db",
    queues={
        "email":   QueueConfig(concurrency=30, max_size=500),
        "reports": QueueConfig(concurrency=4,  max_size=50),
    },
)
```

With named queues active, tasks are routed by setting `queue=` on the decorator or per call. A full queue raises `QueueFullError` when `max_size` is configured. The dashboard shows a Queues tab with live stats per queue.

See [Named Queues](named-queues.md) for the full guide, including `QueueFullError` handling and live config updates.

## Adding argument encryption

If your tasks receive sensitive data (tokens, PII, credentials), you can encrypt arguments at enqueue time. They are decrypted only inside the executor, just before the function runs:

```python
from fastapi_taskflow import TaskManager
from cryptography.fernet import Fernet

key = Fernet.generate_key()

task_manager = TaskManager(encrypt_args_key=key)
```

!!! warning
    This requires the `cryptography` package: `pip install "fastapi-taskflow[encryption]"`. Generate the key once and store it in an environment variable or secrets manager, not in source code.

See [Argument Encryption](encryption.md) for key rotation and storage guidance.

## Full example

Here is a configuration that combines all the common options:

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    requeue_pending=True,
    log_file="tasks.log",
    log_lifecycle=True,
    max_concurrent_tasks=50,
    max_sync_threads=10,
    max_process_workers=os.cpu_count(),
    process_shutdown_timeout=60.0,
)
```

## Wiring up the lifecycle

`TaskManager` needs to run startup and shutdown logic: loading persisted tasks, starting background workers, and flushing state on exit. There are three ways to set this up.

### Option 1: Use TaskAdmin (recommended)

`TaskAdmin` is the most convenient path. It mounts the dashboard and API routes, and calls `task_manager.init_app(app)` internally, which registers startup and shutdown hooks automatically:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager)
```

Use this when you want the dashboard and task API included. You do not need to call anything else.

### Option 2: Use init_app (no dashboard)

If you want lifecycle management but not the dashboard or extra routes, call `init_app()` directly:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

task_manager.init_app(app)
```

`init_app()` is idempotent. Calling it more than once on the same app is safe.

### Option 3: Manual lifespan handler

If your application already has a lifespan handler and you want full control, call `startup()` and `shutdown()` yourself:

```python hl_lines="9 11"
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")

@asynccontextmanager
async def lifespan(app):
    await task_manager.startup()
    yield
    await task_manager.shutdown()

app = FastAPI(lifespan=lifespan)
```

`startup()` and `shutdown()` skip any steps that are not applicable based on your configuration, so they are safe to call even on a bare `TaskManager()` with no subsystems enabled.

!!! note
    You must use at least one of these three approaches. If you skip lifecycle wiring entirely, persistence will not load on startup and tasks will not be flushed on shutdown.
