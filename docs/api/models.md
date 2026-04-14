# Models

## TaskStatus

```python
class TaskStatus(str, Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    SUCCESS     = "success"
    FAILED      = "failed"
    INTERRUPTED = "interrupted"
```

`INTERRUPTED` is set when a task was mid-execution at shutdown and `requeue_on_interrupt` was not enabled. The task is saved to history and visible in the dashboard but is not re-executed automatically. It can be retried manually via `POST /tasks/{task_id}/retry`.

## TaskConfig

Execution configuration stored alongside a registered function.

```python
@dataclass
class TaskConfig:
    retries:              int = 0
    delay:                float = 0.0
    backoff:              float = 1.0
    persist:              bool = False
    name:                 str | None = None
    requeue_on_interrupt: bool = False
```

`requeue_on_interrupt` controls what happens to a `RUNNING` task at shutdown. When `False` (default), the task is marked `INTERRUPTED` and saved to history. When `True`, it is reset to `PENDING` and re-dispatched on next startup. Only set this for tasks that are safe to restart from scratch.

## TaskRecord

Live and persisted task state.

```python
@dataclass
class TaskRecord:
    task_id:           str
    func_name:         str
    status:            TaskStatus
    args:              tuple
    kwargs:            dict
    created_at:        datetime
    start_time:        datetime | None
    end_time:          datetime | None
    retries_used:      int
    error:             str | None
    logs:              list[str]
    stacktrace:        str | None
    idempotency_key:   str | None
    tags:              dict[str, str]
    encrypted_payload: bytes | None
```

`logs` holds timestamped entries written via `task_log()`. Each entry is a string in the format `YYYY-MM-DDTHH:MM:SS message`. Retry separators (`--- Retry N ---`) are inserted automatically by the executor.

`stacktrace` holds the formatted Python traceback from the final failed attempt, or `None` if the task succeeded.

`idempotency_key` is an optional caller-supplied string. If set and a non-failed task with the same key already exists, `add_task()` returns the existing `task_id` without enqueuing a new task.

`tags` holds key/value labels attached at enqueue time via `add_task(tags=...)`. Tags are forwarded to every `LogEvent` and `LifecycleEvent` emitted for the task. They appear on `TaskRecord` but are not included in persistence backends as a separate column; they are stored as part of the snapshot payload.

`encrypted_payload` holds the Fernet-encrypted `(args, kwargs)` when `encrypt_args_key` is configured on `TaskManager`. When present, `args` and `kwargs` will be empty. Not included in `to_dict()` output or API responses.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `duration` | `float \| None` | Seconds between `start_time` and `end_time`. `None` if not yet complete. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `to_dict()` | `dict` | JSON-serialisable representation used by the API and dashboard. Includes `logs`, `stacktrace`, and `tags`. Does not include `encrypted_payload`. |

## TaskContext

Execution context accessible from inside a running task via `get_task_context()`.

```python
@dataclass
class TaskContext:
    task_id:   str
    func_name: str
    attempt:   int
    tags:      dict[str, str]
```

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the currently running task. |
| `func_name` | `str` | Name of the task function. |
| `attempt` | `int` | Zero-based retry index. `0` means the first run. |
| `tags` | `dict[str, str]` | Key/value labels attached at enqueue time. |

See [Task Context guide](../guide/task-context.md) for usage examples.
