# TaskManager

The central object. Holds the task registry and in-memory store, manages the snapshot scheduler, and exposes the decorator and dependency methods.

## Constructor

```python
TaskManager(
    snapshot_db: str | None = None,
    snapshot_backend: SnapshotBackend | None = None,
    snapshot_interval: float = 60.0,
    requeue_pending: bool = False,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `snapshot_db` | `str` | `None` | Path to a SQLite file. Enables persistence with the default SQLite backend. |
| `snapshot_backend` | `SnapshotBackend` | `None` | Custom backend instance. Takes precedence over `snapshot_db`. |
| `snapshot_interval` | `float` | `60.0` | Seconds between periodic flushes. |
| `requeue_pending` | `bool` | `False` | Save unfinished tasks on shutdown and re-dispatch on startup. |

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
)
def my_task(...) -> None:
    ...
```

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
