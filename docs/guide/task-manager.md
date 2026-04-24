# Setting Up TaskManager

`TaskManager` is the core object. It holds the task registry, the in-memory task store, and all optional subsystems — persistence, logging, encryption, and concurrency controls. You create one instance and share it across your application.

## Minimal setup

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager()
```

No arguments required. By default tasks are tracked in memory only, with no retries, no persistence, and no logging beyond what is stored on each task record.

## With persistence

Pass a SQLite file path to enable task history that survives restarts:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
```

On startup, completed tasks are loaded back into memory. On shutdown, any remaining tasks are flushed to the file.

See [Persistence](persistence.md) for full details including the Redis backend and custom backends.

## With file logging

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    log_file="tasks.log",
    log_lifecycle=True,
)
```

Every `task_log()` call and status transition is written to the file. See [File Logging](file-logging.md) for rotation modes and external logrotate integration.

## With structured observers

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

Observers receive every `task_log()` call and lifecycle event as structured objects. Multiple observers run independently. See [Observability](observability.md).

## With concurrency controls

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    max_concurrent_tasks=50,
    max_sync_threads=10,
)
```

`max_concurrent_tasks` caps how many async tasks run concurrently on the event loop. `max_sync_threads` gives sync tasks a dedicated thread pool separate from the one FastAPI uses for sync request handlers. Both default to `None` (no limit, existing FastAPI behaviour). See [Concurrency Controls](concurrency.md).

## With argument encryption

```python
from fastapi_taskflow import TaskManager
from cryptography.fernet import Fernet

key = Fernet.generate_key()

task_manager = TaskManager(encrypt_args_key=key)
```

Task arguments are encrypted at enqueue time and decrypted only inside the executor just before the function runs. Requires `pip install "fastapi-taskflow[encryption]"`. See [Argument Encryption](encryption.md).

## Full example

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    requeue_pending=True,
    log_file="tasks.log",
    log_lifecycle=True,
    max_concurrent_tasks=50,
    max_sync_threads=10,
)
```

## Lifecycle

`TaskManager` exposes three ways to wire up its lifecycle depending on how much you want to set up.

### With `TaskAdmin`

The simplest path. `TaskAdmin` calls `task_manager.init_app(app)` internally which registers startup and shutdown hooks automatically:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager)
```

### With `init_app()` (no dashboard)

Use `init_app()` when you want lifecycle management but not the dashboard or API routes:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

task_manager.init_app(app)
```

`init_app()` is idempotent. Calling it more than once on the same app is safe.

### With a lifespan handler

=== "v0.6.0+"

    ```python
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi_taskflow import TaskManager

    task_manager = TaskManager(snapshot_db="tasks.db")
    app = FastAPI()

    task_manager.init_app(app)

    # Or call startup/shutdown directly in the lifespan:
    @asynccontextmanager
    async def lifespan(app):
        await task_manager.startup()
        yield
        await task_manager.shutdown()

    app = FastAPI(lifespan=lifespan)
    ```

=== "Before v0.6.0"

    ```python
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

`startup()` and `shutdown()` are safe to call even when no subsystems are configured. Steps that are not applicable are skipped automatically.
