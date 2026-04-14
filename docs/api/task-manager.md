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

### `startup()`

Starts the snapshot scheduler, requeues pending tasks, and initialises the logger chain. Called automatically by `TaskAdmin` on app startup.

Use this directly if you are managing the lifecycle yourself via a lifespan handler:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await task_manager.startup()
    yield
    await task_manager.shutdown()

app = FastAPI(lifespan=lifespan)
```

### `shutdown()`

Stops the scheduler, flushes completed and pending tasks to the backend, closes the logger chain, and shuts down the sync thread pool. Called automatically by `TaskAdmin` on app shutdown.

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
