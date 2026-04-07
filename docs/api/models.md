# Models

## TaskStatus

```python
class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED  = "failed"
```

## TaskConfig

Execution configuration stored alongside a registered function.

```python
@dataclass
class TaskConfig:
    retries: int = 0
    delay:   float = 0.0
    backoff: float = 1.0
    persist: bool = False
    name:    str | None = None
```

## TaskRecord

Live and persisted task state.

```python
@dataclass
class TaskRecord:
    task_id:      str
    func_name:    str
    status:       TaskStatus
    args:         tuple
    kwargs:       dict
    created_at:   datetime
    start_time:   datetime | None
    end_time:     datetime | None
    retries_used: int
    error:        str | None
    logs:         list[str]
    stacktrace:   str | None
```

`logs` holds timestamped entries written via `task_log()`. Each entry is a string in the format `YYYY-MM-DDTHH:MM:SS message`. Retry separators (`--- Retry N ---`) are inserted automatically by the executor.

`stacktrace` holds the formatted Python traceback from the final failed attempt, or `None` if the task succeeded.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `duration` | `float \| None` | Seconds between `start_time` and `end_time`. `None` if not yet complete. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `to_dict()` | `dict` | JSON-serialisable representation used by the API and dashboard. Includes `logs` and `stacktrace`. |
