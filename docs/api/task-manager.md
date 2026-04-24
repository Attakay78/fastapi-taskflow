# TaskManager

The central object. Holds the task registry and in-memory store, manages the snapshot scheduler, and exposes the decorator and dependency methods.

## Constructor

```python
TaskManager(
    snapshot_db: str | None = None,
    snapshot_backend: SnapshotBackend | None = None,
    snapshot_interval: float = 60.0,
    requeue_pending: bool = False,
    merged_list_ttl: float = 5.0,
    loggers: list[TaskObserver] | None = None,
    log_file: str | None = None,
    log_file_max_bytes: int = 10485760,
    log_file_backup_count: int = 5,
    log_file_mode: str = "rotate",
    log_lifecycle: bool = False,
    encrypt_args_key: bytes | str | None = None,
    max_concurrent_tasks: int | None = None,
    max_sync_threads: int | None = None,
    retention_days: float | None = None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `snapshot_db` | `str` | `None` | Path to a SQLite file. Enables persistence with the default SQLite backend. |
| `snapshot_backend` | `SnapshotBackend` | `None` | Custom backend instance. Takes precedence over `snapshot_db`. |
| `snapshot_interval` | `float` | `60.0` | Seconds between periodic flushes. |
| `requeue_pending` | `bool` | `False` | Save unfinished tasks on shutdown and re-dispatch on startup. |
| `merged_list_ttl` | `float` | `5.0` | How long (in seconds) to cache the merged in-memory and backend task list. Used by the dashboard in multi-instance deployments. |
| `loggers` | `list[TaskObserver]` | `None` | List of observer instances. All observers run independently and receive every `task_log()` call and lifecycle transition. |
| `log_file` | `str` | `None` | Shorthand for adding a single `FileLogger`. Ignored when `loggers` already contains a `FileLogger` for the same path. |
| `log_file_max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating. Only used with `log_file`. |
| `log_file_backup_count` | `int` | `5` | Number of rotated backup files to keep. Only used with `log_file`. |
| `log_file_mode` | `str` | `"rotate"` | `"rotate"` for automatic rotation; `"watched"` for external rotation (e.g. logrotate). Only used with `log_file`. |
| `log_lifecycle` | `bool` | `False` | Write a line on each task status transition. Only used with `log_file`. |
| `encrypt_args_key` | `bytes \| str` | `None` | Fernet key for encrypting task args and kwargs at rest. Requires `pip install "fastapi-taskflow[encryption]"`. |
| `max_concurrent_tasks` | `int` | `None` | Maximum number of async tasks that may run concurrently. When set, an `asyncio.Semaphore` is used to cap concurrency, protecting request handlers from event loop pressure under burst task load. `None` disables the limit (existing behaviour). |
| `max_sync_threads` | `int` | `None` | Size of the dedicated thread pool for sync task functions. When set, sync tasks run in an isolated `ThreadPoolExecutor` instead of the default asyncio pool, preventing task bursts from starving sync request handlers. `None` uses `asyncio.to_thread` (existing behaviour). |
| `retention_days` | `float` | `None` | Automatically delete terminal task records (success, failed, cancelled) older than this many days. Pruning runs approximately every 6 hours during the snapshot loop. `None` disables automatic pruning. Can also be set via `TaskAdmin(retention_days=...)`. |

## Methods

### `task()`

Decorator factory that registers a function with execution configuration.

```python
@task_manager.task(
    retries: int = 0,
    delay: float = 0.0,
    backoff: float = 1.0,
    persist: bool = False,
    name: str | None = None,
    requeue_on_interrupt: bool = False,
)
def my_task(...) -> None:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `retries` | `int` | `0` | Additional attempts after the first failure. |
| `delay` | `float` | `0.0` | Seconds before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each subsequent retry. |
| `persist` | `bool` | `False` | Save this task for requeue on restart. |
| `name` | `str` | function name | Override the name stored and shown in the dashboard. |
| `requeue_on_interrupt` | `bool` | `False` | Requeue this task if it was mid-execution at shutdown. Only set for idempotent tasks that are safe to restart from scratch. |

### `schedule()`

Decorator factory that registers a function as a periodic background task. The function is also added to the task registry, so it can be enqueued manually via `add_task()` in addition to running on schedule.

Exactly one of `every` or `cron` must be provided.

```python
@task_manager.schedule(
    every: float | None = None,
    cron: str | None = None,
    retries: int = 0,
    delay: float = 0.0,
    backoff: float = 1.0,
    name: str | None = None,
    run_on_startup: bool = False,
    timezone: str = "UTC",
)
async def my_scheduled_task() -> None:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `every` | `float \| None` | `None` | Interval in seconds between runs. Mutually exclusive with `cron`. |
| `cron` | `str \| None` | `None` | Five-field cron expression (e.g. `"0 * * * *"`). Requires `pip install 'fastapi-taskflow[scheduler]'`. Mutually exclusive with `every`. |
| `retries` | `int` | `0` | Additional attempts after the first failure. |
| `delay` | `float` | `0.0` | Seconds before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each subsequent retry. |
| `name` | `str` | function name | Override the display name in logs and the dashboard. |
| `run_on_startup` | `bool` | `False` | Fire the task immediately on the first scheduler tick rather than waiting for the first interval or cron slot. |
| `timezone` | `str` | `"UTC"` | IANA timezone name used when evaluating `cron` expressions. Ignored when `every` is used. |

Raises `ValueError` if neither or both of `every` and `cron` are provided. Raises `ImportError` if `cron` is used and `croniter` is not installed.

### `get_tasks(background_tasks)`

FastAPI dependency. Returns a `ManagedBackgroundTasks` instance that shares the native request task list.

```python
@app.post("/route")
def route(tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(my_func, arg)
```

### `background_tasks`

Property alias for `get_tasks`. Enables a more descriptive annotation:

```python
@app.post("/route")
def route(background_tasks=Depends(task_manager.background_tasks)):
    task_id = background_tasks.add_task(my_func, arg)
```

### `init_app(app)`

Registers startup and shutdown hooks on `app`. The preferred way to wire lifecycle when not using `TaskAdmin`:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

task_manager.init_app(app)
```

`TaskAdmin` calls this internally. Calling `init_app()` more than once on the same app is safe — hooks are only registered the first time.

### `startup()`

Starts the snapshot scheduler, requeues pending tasks, and initialises the logger chain. Called automatically via `init_app()` (and therefore by `TaskAdmin`).

Call directly only when managing the lifecycle yourself without `init_app()`:

=== "v0.6.0+"

    Prefer `init_app()` over calling `startup()` and `shutdown()` directly — it registers the hooks for you:

    ```python
    from fastapi import FastAPI
    from fastapi_taskflow import TaskManager

    task_manager = TaskManager(snapshot_db="tasks.db")
    app = FastAPI()

    task_manager.init_app(app)
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

### `shutdown()`

Stops the scheduler, flushes completed and pending tasks to the backend, closes the logger chain, and shuts down the sync thread pool. Called automatically via `init_app()` (and therefore by `TaskAdmin`).

### `install(app)`

Patches `fastapi.dependencies.utils.BackgroundTasks` so that bare `BackgroundTasks` annotations in route signatures receive a `ManagedBackgroundTasks` instance. Process-wide.

```python
task_manager.install(app)
```

See [Injection Patterns](../guide/injection.md) for the full comparison.

## Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `registry` | `TaskRegistry` | Registered functions and their configs. |
| `store` | `TaskStore` | In-memory task state. |
| `logger` | `LoggerChain \| None` | Active observer chain. Present when `loggers` was set or `log_file` was configured. |
| `fernet` | `Fernet \| None` | Active Fernet encryptor. Present when `encrypt_args_key` was set. |
| `_scheduler` | `SnapshotScheduler \| None` | Present if a backend was configured. |
| `_audit_log` | `collections.deque` | In-memory ring buffer of the last 1000 `AuditEntry` records. Written on every retry and cancel action. |
| `_running_tasks` | `dict[str, asyncio.Task]` | Maps `task_id` to the asyncio Task or Future currently executing that task. Populated when a task enters `RUNNING` state; removed on completion or cancellation. Used by `POST /tasks/{task_id}/cancel` to cancel running async tasks. |
