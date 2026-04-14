# task_log

```python
from fastapi_taskflow import task_log
```

## Signature

```python
def task_log(message: str, *, level: str = "info", **extra: Any) -> None
```

Emits a structured log entry from within a running task. The entry is appended to the task's `logs` list and becomes immediately visible in the live dashboard under the **Logs** tab. It is also forwarded to any configured `TaskObserver` instances. Works in both sync and async task functions.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | required | The log message. |
| `level` | `str` | `"info"` | Severity: `"debug"`, `"info"`, `"warning"`, or `"error"`. Controls observer filtering via `min_level`. |
| `**extra` | `Any` | | Arbitrary key/value pairs forwarded to `LogEvent.extra` for downstream observers. Not stored in the dashboard log line. |

## Behaviour

Each call prepends a UTC timestamp in `YYYY-MM-DDTHH:MM:SS` format when stored on the task record:

```
2026-04-01T10:00:00 Connecting to SMTP server
```

When called outside a managed task context (direct function call, script, cron job, test), the entry is forwarded to the standard library logger `fastapi_taskflow.task` at the matching log level. Configure that logger via Python's standard `logging` setup to control output.

When a task retries, the executor inserts a `--- Retry N ---` separator into the log list before the next attempt begins. User-emitted entries appear before and after this separator in order.

## Examples

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

With log levels and structured extras:

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

## Storage

Log entries are stored on `TaskRecord.logs` as a `list[str]`. They are persisted by both `SqliteBackend` and `RedisBackend` and are included in the `to_dict()` output and all API responses.

Structured `extra` fields are **not** stored on the task record. They are only forwarded to `TaskObserver.on_log()` for the current session. Use extras for downstream log aggregators, not for data you need to retrieve from the API later.

## See also

- [Task Logging guide](../guide/logging.md)
- [Task Context](../guide/task-context.md) - `get_task_context()` for task metadata
- [Observability](../guide/observability.md) - observer configuration and event reference
- [TaskRecord](models.md#taskrecord)
