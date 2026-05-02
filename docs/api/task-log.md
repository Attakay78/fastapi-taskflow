# task_log

`task_log()` emits a structured log entry from inside a running task, appending it to the task record and forwarding it to all configured observers.

> **Guide:** [Task Logging](../guide/logging.md) covers log levels, structured extras, and observer configuration.

---

## Signature

```python
from fastapi_taskflow import task_log

def task_log(message: str, *, level: str = "info", **extra: Any) -> None
```

Works in both sync and async task functions.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | required | The log message to record. |
| `level` | `str` | `"info"` | Severity of the entry: `"debug"`, `"info"`, `"warning"`, or `"error"`. Controls observer filtering via `min_level` on each `TaskObserver`. |
| `**extra` | `Any` | | Arbitrary key/value pairs forwarded to `LogEvent.extra` for downstream observers. Not stored on the task record. |

---

## Behaviour

Each call appends a timestamped entry to `TaskRecord.logs`. The stored format is:

```
YYYY-MM-DDTHH:MM:SS message
```

For example:

```
2026-04-30T10:00:00 Connecting to SMTP server
```

When a task retries, the executor inserts a `--- Retry N ---` separator into the log list before the next attempt begins. User-emitted entries appear in order before and after this separator.

The entry is also forwarded immediately to all configured `TaskObserver` instances via `on_log()`. Observers that set a `min_level` higher than the entry's level will silently drop it.

### Behaviour outside a task context

When `task_log()` is called outside a managed task (for example in a direct function call, a script, or a test), the entry is forwarded to the standard library logger named `fastapi_taskflow.task` at the matching log level. Configure that logger via Python's standard `logging` setup to control where output goes.

---

## Where output appears

| Destination | Notes |
|-------------|-------|
| `TaskRecord.logs` | Stored on the record, persisted to the backend, returned by the REST API. |
| Dashboard **Logs** tab | Visible in real time via the SSE stream. |
| `TaskObserver.on_log()` | Forwarded to every observer in the chain (for example `FileLogger`, `StdoutLogger`, `InMemoryLogger`). |

Structured `extra` fields are forwarded to `TaskObserver.on_log()` only. They are not stored on the task record and are not returned by the REST API.

---

## Examples

Basic usage:

```python
from fastapi_taskflow import TaskManager, task_log

task_manager = TaskManager()

@task_manager.task(retries=2, delay=1.0, backoff=2.0)
def generate_report(user_id: int) -> None:
    task_log(f"Starting report for user {user_id}")
    data = fetch_data(user_id)
    task_log(f"Fetched {len(data)} records", count=len(data))
    write_output(data)
    task_log("Report complete")
```

Using log levels and structured extras:

```python
@task_manager.task(retries=3)
def charge_payment(user_id: int, amount: float) -> None:
    task_log("Initiating payment", level="debug", user_id=user_id, amount=amount)
    result = gateway.charge(amount)
    if result.status == "declined":
        task_log("Card declined", level="warning", user_id=user_id, code=result.code)
        raise RuntimeError("Payment declined")
    task_log("Payment accepted", user_id=user_id, transaction_id=result.id)
```

---

## Storage details

Log entries are stored on `TaskRecord.logs` as a `list[str]`. Both `SqliteBackend` and `RedisBackend` persist the full log list. Entries are included in `TaskRecord.to_dict()` and all REST API responses.

Structured `extra` fields are not stored on the task record. They are available to `TaskObserver.on_log()` for the current session only. Use extras for downstream log aggregators or metrics; do not use them for data you need to retrieve from the API later.

---

## See also

- [TaskRecord](models.md#taskrecord) for the `logs` field
- [task-file-logger](task-file-logger.md) for observer types that consume log events
- [Task Context](../guide/task-context.md) for `get_task_context()` metadata inside tasks
- [Observability guide](../guide/observability.md) for observer configuration
