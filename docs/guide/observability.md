# Observability

fastapi-taskflow includes a pluggable observer system. Observers receive structured events for every `task_log()` call and every task lifecycle transition (RUNNING, SUCCESS, FAILED, INTERRUPTED). Multiple observers can run simultaneously with full error isolation: a failing observer never affects the task or other observers.

## Built-in observers

### FileLogger

Writes log and lifecycle events to a rotating plain text file.

```python
from fastapi_taskflow import FileLogger, TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[FileLogger("tasks.log", log_lifecycle=True)],
)
```

Each log entry has the format:

```
[abc12345] [send_email] 2026-01-01T12:00:00 Sending to user@example.com
```

Lifecycle entries (when `log_lifecycle=True`):

```
[abc12345] [send_email] 2026-01-01T12:00:00 -- RUNNING
[abc12345] [send_email] 2026-01-01T12:00:01 -- SUCCESS
```

**FileLogger parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | `str` | required | File path to write to. Created if it does not exist. |
| `max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating. Ignored in `watched` mode. |
| `backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `mode` | `str` | `"rotate"` | `"rotate"` for automatic rotation; `"watched"` for external rotation via logrotate. |
| `log_lifecycle` | `bool` | `False` | Also write lifecycle transitions. |
| `min_level` | `str` | `"info"` | Minimum log level to write. Entries below this level are silently dropped. |

### StdoutLogger

Prints log and lifecycle events to stdout. Useful in containers where stdout is captured by the logging agent (Fluentd, Logstash, CloudWatch Logs).

```python
from fastapi_taskflow import StdoutLogger, TaskManager

task_manager = TaskManager(
    loggers=[StdoutLogger(log_lifecycle=True)],
)
```

Output format matches `FileLogger`:

```
[abc12345] [send_email] 2026-01-01T12:00:00 Sending to user@example.com
```

**StdoutLogger parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_lifecycle` | `bool` | `False` | Also print lifecycle transitions. |
| `min_level` | `str` | `"info"` | Minimum log level to print. |

### InMemoryLogger

Captures events in memory. Designed for tests: assert exactly what log entries and lifecycle events were emitted.

```python
from fastapi_taskflow import InMemoryLogger, TaskManager

mem_logger = InMemoryLogger()
task_manager = TaskManager(loggers=[mem_logger])
```

Access captured events after the task runs:

```python
# LogEvent and LifecycleEvent objects
for event in mem_logger.log_events:
    print(event.message, event.level, event.extra)

for event in mem_logger.lifecycle_events:
    print(event.func_name, event.status.value, event.duration)

# Clear for the next test
mem_logger.clear()
```

**InMemoryLogger parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `min_level` | `str` | `"debug"` | Minimum log level to capture. Default captures all levels. |

## Multiple observers

Pass a list to `loggers=` to fan out to multiple observers simultaneously. All run independently: an error in one never affects the others.

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.loggers import FileLogger, StdoutLogger

task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[
        FileLogger("tasks.log", log_lifecycle=True),
        StdoutLogger(log_lifecycle=True, min_level="warning"),
    ],
)
```

## Tags

Attach key/value labels to a task at enqueue time with `tags=`. Tags flow through to every `LogEvent` and `LifecycleEvent` emitted for that task, so downstream systems can filter logs by label without parsing message strings.

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager()
app = FastAPI()

@app.post("/invoice")
def create_invoice(user_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        process_invoice,
        user_id,
        tags={"user_id": str(user_id), "source": "invoice_api"},
    )
    return {"task_id": task_id}
```

Tags are available inside the task via `get_task_context()`:

```python
from fastapi_taskflow import get_task_context

@task_manager.task(retries=2)
def process_invoice(user_id: int) -> None:
    ctx = get_task_context()
    source = ctx.tags.get("source", "unknown") if ctx else "unknown"
    task_log("Processing invoice", user_id=user_id, source=source)
```

## The log_file shorthand

If you only need a single `FileLogger`, use the `log_file` shorthand on `TaskManager` instead of constructing `FileLogger` manually:

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.loggers import FileLogger

# shorthand
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks.log",
    log_file_max_bytes=5 * 1024 * 1024,
    log_file_backup_count=3,
    log_file_mode="rotate",
    log_lifecycle=True,
)

# equivalent
task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[
        FileLogger(
            "tasks.log",
            max_bytes=5 * 1024 * 1024,
            backup_count=3,
            mode="rotate",
            log_lifecycle=True,
        )
    ],
)
```

The shorthand is kept for backwards compatibility. Use `loggers=` directly when you need multiple observers or more control.

## Custom observers

Implement the `TaskObserver` ABC to send events anywhere:

```python
from fastapi_taskflow import TaskObserver
from fastapi_taskflow.loggers.base import LifecycleEvent, LogEvent


class MyObserver(TaskObserver):
    async def on_log(self, event: LogEvent) -> None:
        # event.task_id, event.func_name, event.message,
        # event.level, event.timestamp, event.attempt,
        # event.tags, event.extra
        await my_log_service.send(event.message, tags=event.tags)

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        # event.status, event.duration, event.error, event.stacktrace
        if event.status.value == "failed":
            await alert_service.notify(event.func_name, event.error)

    async def startup(self) -> None:
        await my_log_service.connect()

    async def close(self) -> None:
        await my_log_service.disconnect()


from fastapi_taskflow import TaskManager

task_manager = TaskManager(loggers=[MyObserver()])
```

`startup()` is called when `TaskAdmin` mounts the app. `close()` is called on shutdown. Both default to no-ops in the base class.

## Event reference

### LogEvent

Emitted for every `task_log()` call.

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the task. |
| `func_name` | `str` | Task function name. |
| `message` | `str` | The log message. |
| `level` | `str` | Log level string (`"debug"`, `"info"`, `"warning"`, `"error"`). |
| `timestamp` | `datetime` | UTC time the entry was created. |
| `attempt` | `int` | Zero-based retry index. |
| `tags` | `dict[str, str]` | Tags attached at enqueue time. |
| `extra` | `dict` | Arbitrary extra fields from `task_log(**extra)`. |

### LifecycleEvent

Emitted on each status transition.

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the task. |
| `func_name` | `str` | Task function name. |
| `status` | `TaskStatus` | New status (`RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`). |
| `timestamp` | `datetime` | UTC time of the transition. |
| `attempt` | `int` | Zero-based retry index. |
| `retries_used` | `int` | Total retries consumed. |
| `duration` | `float \| None` | Seconds from start to end. Present on `SUCCESS` and `FAILED`. |
| `error` | `str \| None` | Error message string. Present on `FAILED`. |
| `stacktrace` | `str \| None` | Formatted traceback. Present on `FAILED`. |
| `tags` | `dict[str, str]` | Tags attached at enqueue time. |

## See also

- [Task Logging](logging.md)
- [Task Context](task-context.md)
- [File Logging](file-logging.md) - rotation options and multi-process deployment
