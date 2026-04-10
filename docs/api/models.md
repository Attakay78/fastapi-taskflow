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
    task_id:          str
    func_name:        str
    status:           TaskStatus
    args:             tuple
    kwargs:           dict
    created_at:       datetime
    start_time:       datetime | None
    end_time:         datetime | None
    retries_used:     int
    error:            str | None
    logs:             list[str]
    stacktrace:       str | None
    idempotency_key:  str | None
```

`logs` holds timestamped entries written via `task_log()`. Each entry is a string in the format `YYYY-MM-DDTHH:MM:SS message`. Retry separators (`--- Retry N ---`) are inserted automatically by the executor.

`stacktrace` holds the formatted Python traceback from the final failed attempt, or `None` if the task succeeded.

`idempotency_key` is an optional caller-supplied string. If set and a non-failed task with the same key already exists, `add_task()` returns the existing `task_id` without enqueuing a new task.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `duration` | `float \| None` | Seconds between `start_time` and `end_time`. `None` if not yet complete. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `to_dict()` | `dict` | JSON-serialisable representation used by the API and dashboard. Includes `logs` and `stacktrace`. |
