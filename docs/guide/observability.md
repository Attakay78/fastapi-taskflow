# Observability

This page explains the observer system: what it does, which built-in observers are available, how to combine them, and how to write your own.

## What the observer system does

Every `task_log()` call and every task status transition (RUNNING, SUCCESS, FAILED, INTERRUPTED) emits a structured event. Observers receive those events and can forward them anywhere: a file, stdout, a logging service, a metrics backend.

Multiple observers run in parallel. If one raises an exception it is caught and silenced so the task and the remaining observers continue unaffected.

!!! note
    The dashboard already shows task output in real time. Observers are for cases where you need those events somewhere else too: a file for `tail -f`, stdout for a container log driver, or an in-memory buffer for test assertions.

## FileLogger

`FileLogger` writes events to a plain text file. It supports automatic rotation and external rotation via logrotate.

```python
from fastapi_taskflow import FileLogger, TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[FileLogger("tasks.log", log_lifecycle=True)],
)
```

Each line written by a `task_log()` call looks like this:

```
[abc12345] [send_email] 2026-01-01T12:00:00 Sending to user@example.com
```

When `log_lifecycle=True`, status transitions get their own line:

```
[abc12345] [send_email] 2026-01-01T12:00:00 -- RUNNING
[abc12345] [send_email] 2026-01-01T12:00:00 Connecting to SMTP server
[abc12345] [send_email] 2026-01-01T12:00:01 -- SUCCESS
```

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | File path to write to. Created if it does not exist. |
| `max_bytes` | `int` | `10485760` | File size limit (10 MB) before rotating. Ignored in `watched` mode. |
| `backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `mode` | `str` | `"rotate"` | `"rotate"` for automatic rotation, `"watched"` for external rotation via logrotate. |
| `log_lifecycle` | `bool` | `False` | Write a line on each task status transition. |
| `min_level` | `str` | `"info"` | Minimum log level to write. Entries below this level are silently dropped. |

## StdoutLogger

`StdoutLogger` prints events to stdout. This is the right choice for containers where a log driver (Fluentd, Logstash, CloudWatch Logs) captures stdout automatically. There is no file to manage and no rotation to configure.

```python
from fastapi_taskflow import StdoutLogger, TaskManager

task_manager = TaskManager(
    loggers=[StdoutLogger(log_lifecycle=True)],
)
```

Output format is identical to `FileLogger`.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `log_lifecycle` | `bool` | `False` | Print a line on each task status transition. |
| `min_level` | `str` | `"info"` | Minimum log level to print. |

## InMemoryLogger

`InMemoryLogger` captures events in an in-memory list instead of writing anywhere. It is designed for tests where you want to assert exactly what was logged without touching the filesystem.

```python
from fastapi_taskflow import InMemoryLogger, TaskManager

mem_logger = InMemoryLogger()
task_manager = TaskManager(loggers=[mem_logger])
```

After the task runs, inspect the captured events:

```python
def test_charge_payment_logs_amount():
    mem_logger = InMemoryLogger()
    task_manager = TaskManager(loggers=[mem_logger])

    # run the task synchronously in your test setup ...

    messages = [e.message for e in mem_logger.log_events]
    assert any("amount=99.0" in m for m in messages)

    statuses = [e.status.value for e in mem_logger.lifecycle_events]
    assert statuses == ["running", "success"]

    # clear between tests
    mem_logger.clear()
```

`log_events` is a list of `LogEvent` objects. `lifecycle_events` is a list of `LifecycleEvent` objects. The [Event reference](#event-reference) section below documents every field.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `min_level` | `str` | `"debug"` | Minimum log level to capture. The default captures everything, including debug entries. |

## Running multiple observers

Pass a list to `loggers=` to send events to several observers at once:

```python hl_lines="6 7"
from fastapi_taskflow import FileLogger, StdoutLogger, TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[
        FileLogger("tasks.log", log_lifecycle=True),
        StdoutLogger(log_lifecycle=True, min_level="warning"),
    ],
)
```

In this example every event goes to the file, but only warnings and above go to stdout. Each observer runs independently: a failure in one does not prevent the others from receiving the event.

## The log_file shorthand

If you only need a single `FileLogger`, you can use the `log_file` parameter on `TaskManager` directly instead of constructing a `FileLogger` by hand:

```python
from fastapi_taskflow import TaskManager

# shorthand
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks.log",
    log_file_max_bytes=5 * 1024 * 1024,
    log_file_backup_count=3,
    log_file_mode="rotate",
    log_lifecycle=True,
)
```

This is equivalent to:

```python
from fastapi_taskflow import FileLogger, TaskManager

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

!!! tip
    The `log_file` shorthand exists for backwards compatibility and quick setups. When you need more than one observer, or you want to combine a `FileLogger` with a `StdoutLogger`, use `loggers=` directly.

## Custom observers

Implement `TaskObserver` to send events anywhere you like. You only need to override the methods you care about.

There are two event-handling methods:

- `on_log(event: LogEvent)` is called for every `task_log()` call.
- `on_lifecycle(event: LifecycleEvent)` is called on each status transition.

Both are `async` and optional. The base class provides no-op defaults.

```python
from fastapi_taskflow import TaskManager, TaskObserver
from fastapi_taskflow.loggers.base import LifecycleEvent, LogEvent


class SlackAlerter(TaskObserver):
    async def on_log(self, event: LogEvent) -> None:
        # Available fields: task_id, func_name, message, level,
        # timestamp, attempt, tags, extra
        if event.level == "error":
            await slack.post(f"[{event.func_name}] {event.message}")

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        # Available fields: task_id, func_name, status, timestamp,
        # attempt, retries_used, duration, error, stacktrace, tags
        if event.status.value == "failed":
            await slack.post(
                f"Task {event.func_name} failed after "
                f"{event.retries_used} retries: {event.error}"
            )

    async def startup(self) -> None:
        # Called when TaskAdmin mounts the app.
        await slack.connect()

    async def close(self) -> None:
        # Called on shutdown.
        await slack.disconnect()


task_manager = TaskManager(loggers=[SlackAlerter()])
```

`startup()` and `close()` are optional lifecycle hooks. Override them when you need to open or close a connection on startup and shutdown.

## Event reference

### LogEvent

Emitted for every `task_log()` call.

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | UUID of the task. |
| `func_name` | `str` | Task function name. |
| `message` | `str` | The log message. |
| `level` | `str` | Log level: `"debug"`, `"info"`, `"warning"`, or `"error"`. |
| `timestamp` | `datetime` | UTC time the entry was created. |
| `attempt` | `int` | Zero-based retry index. |
| `tags` | `dict[str, str]` | Tags attached at enqueue time. |
| `extra` | `dict` | Arbitrary keyword arguments passed to `task_log()`. |

### LifecycleEvent

Emitted on each status transition.

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | UUID of the task. |
| `func_name` | `str` | Task function name. |
| `status` | `TaskStatus` | New status: `RUNNING`, `SUCCESS`, `FAILED`, or `INTERRUPTED`. |
| `timestamp` | `datetime` | UTC time of the transition. |
| `attempt` | `int` | Zero-based retry index. |
| `retries_used` | `int` | Total retries consumed so far. |
| `duration` | `float \| None` | Seconds from start to end. Present on `SUCCESS` and `FAILED`. |
| `error` | `str \| None` | Error message string. Present on `FAILED`. |
| `stacktrace` | `str \| None` | Formatted traceback. Present on `FAILED`. |
| `tags` | `dict[str, str]` | Tags attached at enqueue time. |

## See also

- [Task Logging](logging.md)
- [Task Context](task-context.md)
- [File Logging](file-logging.md)
