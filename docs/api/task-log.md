# task_log

```python
from fastapi_taskflow import task_log
```

## Signature

```python
def task_log(message: str) -> None
```

Emits a timestamped log entry from within a running task. The entry is appended to the task's `logs` list and becomes immediately visible in the live dashboard under the **Logs** tab.

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `message` | `str` | The log message to record. |

## Behaviour

Each call prepends a UTC timestamp in `YYYY-MM-DDTHH:MM:SS` format, producing an entry like:

```
2026-04-07T10:00:00 Connecting to SMTP server
```

Calls made outside a managed task context are silently ignored.

When a task retries, the executor inserts a `--- Retry N ---` separator into the log list before the next attempt begins. User-emitted entries appear before and after this separator in order.

## Example

```python
from fastapi_taskflow import TaskManager, task_log

task_manager = TaskManager()

@task_manager.task(retries=2, delay=1.0, backoff=2.0)
def generate_report(user_id: int) -> None:
    task_log(f"Starting report for user {user_id}")
    data = fetch_data(user_id)
    task_log(f"Fetched {len(data)} records")
    write_output(data)
    task_log("Report complete")
```

## Storage

Log entries are stored on `TaskRecord.logs` as a `list[str]`. They are persisted by both `SqliteBackend` and `RedisBackend` and are included in the `to_dict()` output and all API responses.

## See also

- [Task Logging guide](../guide/logging.md)
- [TaskRecord](models.md#taskrecord)
