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
    log_file: str | None = None,
    log_file_max_bytes: int = 10485760,
    log_file_backup_count: int = 5,
    log_file_mode: str = "rotate",
    log_lifecycle: bool = False,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `snapshot_db` | `str` | `None` | Path to a SQLite file. Enables persistence with the default SQLite backend. |
| `snapshot_backend` | `SnapshotBackend` | `None` | Custom backend instance. Takes precedence over `snapshot_db`. |
| `snapshot_interval` | `float` | `60.0` | Seconds between periodic flushes. |
| `requeue_pending` | `bool` | `False` | Save unfinished tasks on shutdown and re-dispatch on startup. |
| `merged_list_ttl` | `float` | `5.0` | How long (in seconds) to cache the merged in-memory and backend task list. Used by the dashboard in multi-instance deployments. |
| `log_file` | `str` | `None` | Path to a plain text log file. File logging is disabled when not set. |
| `log_file_max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating. Ignored in `watched` mode. |
| `log_file_backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `log_file_mode` | `str` | `"rotate"` | `"rotate"` for automatic rotation; `"watched"` for external rotation (e.g. logrotate). |
| `log_lifecycle` | `bool` | `False` | Write a line on each task status transition (`RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`). |

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
| `_scheduler` | `SnapshotScheduler \| None` | Present if a backend was configured. |
| `file_logger` | `TaskFileLogger \| None` | Present if `log_file` was set. Writes task log entries and lifecycle events to a plain text file. |
