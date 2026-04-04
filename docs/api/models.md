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
```

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `duration` | `float \| None` | Seconds between `start_time` and `end_time`. `None` if not yet complete. |

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `to_dict()` | `dict` | JSON-serialisable representation used by the API and dashboard. |
