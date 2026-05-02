# TaskManager

`TaskManager` is the central object in fastapi-taskflow: it holds the task registry and in-memory store, manages the snapshot scheduler and observer chain, and exposes the `@task()` and `@schedule()` decorators along with FastAPI dependency helpers.

> **Guide:** [Task Manager](../guide/task-manager.md) covers setup patterns and injection strategies. This page documents every parameter and method signature.

---

## Constructor

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    retries=3,
    # ...
)
```

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
    max_process_workers: int | None = None,
    process_shutdown_timeout: float = 30.0,
    retention_days: float | None = None,
)
```

### Persistence parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `snapshot_db` | `str \| None` | `None` | Path to a SQLite file. Creates a `SqliteBackend` automatically. Ignored when `snapshot_backend` is also provided. |
| `snapshot_backend` | `SnapshotBackend \| None` | `None` | A pre-constructed backend instance (for example `RedisBackend` or `PostgresBackend`). Takes precedence over `snapshot_db`. |
| `snapshot_interval` | `float` | `60.0` | Seconds between periodic flushes of completed tasks to the backend. |
| `requeue_pending` | `bool` | `False` | When `True`, unfinished tasks are saved at shutdown and re-dispatched on the next startup. |
| `merged_list_ttl` | `float` | `5.0` | How long, in seconds, to cache the backend task list used by `merged_list()`. Only the backend portion is cached; in-memory tasks are always current. Useful in multi-instance deployments. |
| `retention_days` | `float \| None` | `None` | Automatically delete terminal task records (success, failed, cancelled) older than this many days. Pruning runs approximately every 6 hours inside the snapshot loop. `None` disables automatic pruning. Can also be set via `TaskAdmin(retention_days=...)`. |

### Logging parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `loggers` | `list[TaskObserver] \| None` | `None` | Observer instances that receive every `task_log()` call and lifecycle transition. All observers run independently; an error in one never affects the others or the task. |
| `log_file` | `str \| None` | `None` | Shorthand for adding a single `FileLogger`. Equivalent to `loggers=[FileLogger(log_file)]`. Ignored when `loggers` already contains a `FileLogger` for the same path. |
| `log_file_max_bytes` | `int` | `10485760` | Maximum file size before rotation (10 MB). Only used with `log_file`. Has no effect in `"watched"` mode. |
| `log_file_backup_count` | `int` | `5` | Number of rotated backup files to keep. Only used with `log_file`. |
| `log_file_mode` | `str` | `"rotate"` | `"rotate"` uses `RotatingFileHandler` for single-process setups. `"watched"` uses `WatchedFileHandler` for multi-process deployments with external rotation (for example, logrotate). Only used with `log_file`. |
| `log_lifecycle` | `bool` | `False` | Also write task lifecycle transitions (running, success, failed) when using the `log_file` shorthand. |

### Concurrency parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_concurrent_tasks` | `int \| None` | `None` | Maximum number of async tasks running concurrently on the event loop. When set, an `asyncio.Semaphore` is acquired before each execution and released on completion. `None` removes the limit. |
| `max_sync_threads` | `int \| None` | `None` | Size of the dedicated thread pool for sync task functions. When set, sync tasks run in an isolated `ThreadPoolExecutor` instead of the default asyncio pool, preventing task bursts from starving sync request handlers. `None` uses `asyncio.to_thread`. |
| `max_process_workers` | `int \| None` | `None` | Number of worker processes in the `ProcessPoolExecutor` used by `executor='process'` tasks. `None` uses `os.cpu_count()`. Has no effect unless at least one task is registered with `executor='process'`. |
| `process_shutdown_timeout` | `float` | `30.0` | Seconds to wait for in-flight process tasks to finish at shutdown before terminating workers forcefully. Only applies when `executor='process'` tasks are registered. |

### Encryption parameter

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `encrypt_args_key` | `bytes \| str \| None` | `None` | A Fernet key for encrypting task args and kwargs at rest. When set, arguments are encrypted at enqueue time and decrypted only when the executor is about to call the function. Accepts a URL-safe base64 string or raw bytes from `Fernet.generate_key()`. Requires `pip install "fastapi-taskflow[encryption]"`. |

---

## Decorators

### `task()`

Registers a function as a managed background task. The decorated function is returned unchanged and can still be called directly.

```python
@task_manager.task(
    retries=3,
    delay=1.0,
    backoff=2.0,
    persist=False,
    name=None,
    requeue_on_interrupt=False,
    eager=False,
    priority=None,
    executor=None,
)
def my_task(user_id: int) -> None:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `retries` | `int` | `0` | Additional attempts after the first failure. A value of `3` means up to 4 total attempts. |
| `delay` | `float` | `0.0` | Seconds to wait before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each subsequent retry. Use `2.0` for exponential backoff (1 s, 2 s, 4 s, ...). |
| `persist` | `bool` | `False` | Save this task record to the backend for requeue on restart. Scoped to this function only. |
| `name` | `str \| None` | function name | Override the display name in logs and the dashboard. |
| `requeue_on_interrupt` | `bool` | `False` | When `True` and `requeue_pending=True` on the manager, a task interrupted at shutdown is reset to `PENDING` and re-dispatched on next startup. Only set this for idempotent functions that are safe to restart from scratch. |
| `eager` | `bool` | `False` | Dispatch via `asyncio.create_task` immediately when `add_task()` is called, before FastAPI sends the response. Per-call `eager` on `add_task()` overrides this value. |
| `priority` | `int \| None` | `None` | Route through the priority queue instead of Starlette's background task list. Higher values run first. The conventional range is 1 (lowest) to 10 (highest). Per-call `priority` on `add_task()` overrides this value. |
| `executor` | `"async" \| "thread" \| "process" \| None` | `None` | Force a specific executor. `"async"` requires a coroutine. `"thread"` requires a plain function. `"process"` routes to a `ProcessPoolExecutor` and requires a module-level function with picklable arguments. `None` auto-detects from the function signature. |

Raises `ValueError` at decoration time if the function is incompatible with the requested executor (for example, `executor='async'` on a sync function, or `executor='process'` on a lambda or nested function).

**Example:**

```python
@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_email(address: str) -> None:
    ...

@task_manager.task(executor="process")
def generate_report(report_id: int) -> bytes:
    return render_pdf(report_id)
```

> **See also:** [Process Executor](../guide/process-tasks.md) for constraints and configuration specific to `executor='process'`.

---

### `schedule()`

Registers a function as a periodic background task. Exactly one of `every` or `cron` must be provided.

The decorated function is also added to the task registry so it can be enqueued manually via `add_task()` in addition to running on schedule.

```python
@task_manager.schedule(
    every=300,
)
async def cleanup_expired_sessions() -> None:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `every` | `float \| None` | `None` | Interval in seconds between runs (for example `300` for every 5 minutes). Mutually exclusive with `cron`. |
| `cron` | `str \| None` | `None` | Five-field cron expression (for example `"0 * * * *"` for every hour). Requires `pip install "fastapi-taskflow[scheduler]"`. Mutually exclusive with `every`. |
| `retries` | `int` | `0` | Additional attempts after the first failure. |
| `delay` | `float` | `0.0` | Seconds to wait before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each retry. |
| `name` | `str \| None` | function name | Override the display name in logs and the dashboard. |
| `run_on_startup` | `bool` | `False` | When `True`, fire the task on the first scheduler tick immediately after startup, rather than waiting for the first interval or cron slot. |
| `timezone` | `str` | `"UTC"` | IANA timezone name used when evaluating `cron` expressions (for example `"America/New_York"`). Ignored when `every` is used. |
| `executor` | `"async" \| "thread" \| "process" \| None` | `None` | Force a specific executor for each firing. Same constraints as on `@task()`. `None` auto-detects from the function signature. |

Raises `ValueError` if neither or both of `every` and `cron` are provided, or if `executor` is incompatible with the function. Raises `ImportError` if `cron` is used and `croniter` is not installed.

**Examples:**

```python
@task_manager.schedule(every=300, retries=1)
async def cleanup_expired_sessions() -> None:
    ...

@task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
async def morning_report() -> None:
    ...

@task_manager.schedule(every=3600, executor="process")
def rebuild_search_index() -> None:
    ...
```

> **See also:** [Scheduled Tasks API](scheduled-tasks.md) for `ScheduledEntry` fields and the `TaskRecord.source` value.

---

## Methods

### `get_tasks(background_tasks)`

FastAPI dependency that returns a `ManagedBackgroundTasks` instance sharing the native request task list.

```python
@app.post("/signup")
def signup(email: str, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(send_email, email)
    return {"task_id": task_id}
```

### `background_tasks`

Property alias for `get_tasks`. Reads more naturally when naming the dependency parameter:

```python
@app.post("/signup")
def signup(
    email: str,
    background_tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_id = background_tasks.add_task(send_email, email)
    return {"task_id": task_id}
```

### `init_app(app)`

Registers startup and shutdown hooks on `app`. Use this when you want lifecycle management without mounting the dashboard or any other routes.

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

task_manager.init_app(app)
```

Calling `init_app()` more than once on the same app is safe. Hooks are only registered the first time.

`TaskAdmin` calls `init_app()` internally, so you do not need to call it yourself when using `TaskAdmin`.

### `startup()`

Starts the snapshot scheduler, requeues pending tasks, and initialises the observer chain. Called automatically by `init_app()` (and therefore by `TaskAdmin`).

Call this directly only when managing the lifecycle yourself without `init_app()`:

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

Prefer `init_app()` when possible. It registers both hooks for you and is safe to call multiple times.

### `shutdown()`

Stops the scheduler, flushes completed and pending tasks to the backend, closes the observer chain, shuts down the sync thread pool, and waits for in-flight process tasks up to `process_shutdown_timeout`. Called automatically by `init_app()` (and therefore by `TaskAdmin`).

### `install(app)`

Patches `fastapi.dependencies.utils.BackgroundTasks` so that bare `BackgroundTasks` annotations in route signatures receive a `ManagedBackgroundTasks` instance. The patch is process-wide.

```python
task_manager.install(app)

@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(send_email, email)
    # background_tasks is now ManagedBackgroundTasks
```

After this call, all three injection patterns return a `ManagedBackgroundTasks` instance:

1. `background_tasks: BackgroundTasks` (zero migration)
2. `background_tasks: ManagedBackgroundTasks` (explicit type)
3. `tasks = Depends(task_manager.get_tasks)` (explicit dependency)

`install()` is called automatically when `TaskAdmin(auto_install=True)` is set or when `init_app()` is called. For route-scoped injection, use `Depends(task_manager.get_tasks)` instead.

---

## Public attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `registry` | `TaskRegistry` | The registered functions and their `TaskConfig` objects. |
| `store` | `TaskStore` | In-memory task state for the current process. |
| `logger` | `LoggerChain \| None` | Active observer chain. Present when `loggers` was set or `log_file` was configured. `None` otherwise. |
| `fernet` | `Fernet \| None` | Active Fernet encryptor. Present when `encrypt_args_key` was set. `None` otherwise. |
