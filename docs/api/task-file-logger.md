# Loggers and Observers

This page documents the built-in observer implementations, the `TaskObserver` ABC, and the event types delivered to observers.

fastapi-taskflow ships three built-in observer implementations. All implement the `TaskObserver` ABC and can be combined in any order via the `loggers=` parameter on `TaskManager`.

> **Guide:** [Observability](../guide/observability.md) covers observer configuration, combining loggers, and writing custom observers.

---

## FileLogger

`FileLogger` writes log and lifecycle events to a plain text file with automatic rotation.

```python
from fastapi_taskflow import FileLogger
```

### Constructor

```python
FileLogger(
    path: str,
    max_bytes: int = 10485760,
    backup_count: int = 5,
    mode: Literal["rotate", "watched"] = "rotate",
    log_lifecycle: bool = False,
    min_level: str = "info",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | `str` | required | File path to write to. The file is created if it does not exist. |
| `max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating to a new file. Has no effect in `"watched"` mode. |
| `backup_count` | `int` | `5` | Number of rotated backup files to keep alongside the active log file. Has no effect in `"watched"` mode. |
| `mode` | `str` | `"rotate"` | `"rotate"` uses Python's `RotatingFileHandler`, suitable for single-process deployments. `"watched"` uses `WatchedFileHandler`, suitable for multi-process deployments where an external tool such as logrotate manages rotation. |
| `log_lifecycle` | `bool` | `False` | When `True`, also write lifecycle transitions (`RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`) to the file in addition to `task_log()` entries. |
| `min_level` | `str` | `"info"` | Minimum log level to write. Entries below this level are silently dropped. Accepts `"debug"`, `"info"`, `"warning"`, or `"error"`. |

### Output format

Log entry (from `task_log()`):

```
[abc12345] [send_email] 2026-04-30T12:00:00 Sending to user@example.com
```

Lifecycle entry (when `log_lifecycle=True`):

```
[abc12345] [send_email] 2026-04-30T12:00:01 -- SUCCESS
```

### Thread safety

A single `FileLogger` instance is safe for concurrent use across multiple threads (sync tasks run in a thread pool) and the asyncio event loop within one process. For multi-process or multi-host deployments, see the [File Logging guide](../guide/file-logging.md).

**Example:**

```python
from fastapi_taskflow import TaskManager, FileLogger

task_manager = TaskManager(
    loggers=[FileLogger("tasks.log", log_lifecycle=True, min_level="debug")],
)
```

---

## StdoutLogger

`StdoutLogger` prints log and lifecycle events to stdout. Useful in containers where stdout is captured by the logging agent.

```python
from fastapi_taskflow import StdoutLogger
```

### Constructor

```python
StdoutLogger(
    log_lifecycle: bool = False,
    min_level: str = "info",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_lifecycle` | `bool` | `False` | When `True`, also print lifecycle transitions in addition to `task_log()` entries. |
| `min_level` | `str` | `"info"` | Minimum log level to print. Entries below this level are silently dropped. |

### Output format

Output matches `FileLogger`:

```
[abc12345] [send_email] 2026-04-30T12:00:00 Sending to user@example.com
```

**Example:**

```python
from fastapi_taskflow import TaskManager, StdoutLogger

task_manager = TaskManager(
    loggers=[StdoutLogger(log_lifecycle=True)],
)
```

---

## InMemoryLogger

`InMemoryLogger` captures events in memory. It is designed for use in tests where you want to assert on log output and lifecycle transitions without writing to disk or stdout.

```python
from fastapi_taskflow import InMemoryLogger
```

### Constructor

```python
InMemoryLogger(
    min_level: str = "debug",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `min_level` | `str` | `"debug"` | Minimum log level to capture. The default captures all levels. |

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `log_events` | `list[LogEvent]` | All captured log events in order, each emitted by a `task_log()` call. |
| `lifecycle_events` | `list[LifecycleEvent]` | All captured lifecycle transition events in order. |

### Methods

#### `clear() -> None`

Empties both `log_events` and `lifecycle_events`. Call this between test cases to reset state.

**Example:**

```python
from fastapi_taskflow import InMemoryLogger, TaskManager, task_log

mem = InMemoryLogger()
task_manager = TaskManager(loggers=[mem])

@task_manager.task()
def my_task() -> None:
    task_log("hello")

# ... run the task in a test ...

assert mem.log_events[0].message == "hello"
assert mem.lifecycle_events[-1].status.value == "success"

mem.clear()
assert len(mem.log_events) == 0
```

---

## TaskObserver ABC

`TaskObserver` is the base class for all custom observers. Subclass it and implement `on_log()` and/or `on_lifecycle()` to send task events to any destination, such as a file, a metrics system, or a tracing backend.

```python
from fastapi_taskflow import TaskObserver
from fastapi_taskflow.loggers.base import LogEvent, LifecycleEvent
```

```python
class TaskObserver(ABC):
    def __init__(self, min_level: str = "info") -> None: ...

    async def on_log(self, event: LogEvent) -> None: ...
    async def on_lifecycle(self, event: LifecycleEvent) -> None: ...
    async def startup(self) -> None: ...
    async def close(self) -> None: ...
```

### Methods to implement

#### `on_log(event: LogEvent) -> None`

Called for every `task_log()` entry emitted by a running task. The base class implementation is a no-op; override it to process log entries.

#### `on_lifecycle(event: LifecycleEvent) -> None`

Called on every task status transition: `RUNNING`, `SUCCESS`, `FAILED`, and `INTERRUPTED`. The base class implementation is a no-op; override it to process lifecycle events.

Both methods are `async`, so implementations can `await` network calls, database writes, or any other async I/O. Sync-only destinations can use `asyncio.to_thread` inside the method body.

Errors raised inside either method are caught by `LoggerChain` and logged to stderr. They never propagate to the task or affect its outcome.

### Optional methods

#### `startup() -> None`

Called at app startup before any tasks run. Use this to open connections or initialise exporters. Defaults to a no-op; override only if needed.

#### `close() -> None`

Called at app shutdown. Use this to flush buffered events and release held resources. Defaults to a no-op; override only if needed.

**Example custom observer:**

```python
from fastapi_taskflow import TaskObserver
from fastapi_taskflow.loggers.base import LifecycleEvent

class PrometheusObserver(TaskObserver):
    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        TASK_COUNTER.labels(
            func=event.func_name,
            status=event.status.value,
        ).inc()
```

---

## LogEvent

`LogEvent` carries a single structured log entry from `task_log()` to each observer.

```python
from fastapi_taskflow.loggers.base import LogEvent
```

```python
@dataclass
class LogEvent:
    task_id:   str
    func_name: str
    message:   str
    level:     str
    timestamp: datetime
    attempt:   int
    tags:      dict[str, str]
    extra:     dict[str, Any]
```

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the task that emitted this entry. |
| `func_name` | `str` | Name of the task function. |
| `message` | `str` | The log message, prefixed with a UTC timestamp in `YYYY-MM-DDTHH:MM:SS` format. |
| `level` | `str` | Severity: `"debug"`, `"info"`, `"warning"`, or `"error"`. |
| `timestamp` | `datetime` | UTC datetime when the entry was created. |
| `attempt` | `int` | Zero-based retry index. `0` means the first run. |
| `tags` | `dict[str, str]` | Key/value labels attached to the task at enqueue time. |
| `extra` | `dict[str, Any]` | Arbitrary structured data passed as keyword arguments to `task_log()`. |

---

## LifecycleEvent

`LifecycleEvent` carries a task status transition to each observer. Emitted on every state change: `RUNNING`, `SUCCESS`, `FAILED`, and `INTERRUPTED`.

```python
from fastapi_taskflow.loggers.base import LifecycleEvent
```

```python
@dataclass
class LifecycleEvent:
    task_id:      str
    func_name:    str
    status:       TaskStatus
    timestamp:    datetime
    attempt:      int
    retries_used: int
    duration:     float | None
    error:        str | None
    stacktrace:   str | None
    tags:         dict[str, str]
```

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the task. |
| `func_name` | `str` | Name of the task function. |
| `status` | `TaskStatus` | The new status value after this transition. |
| `timestamp` | `datetime` | UTC datetime when the transition occurred. |
| `attempt` | `int` | Zero-based retry index at the time of the transition. |
| `retries_used` | `int` | Total retry attempts consumed so far. |
| `duration` | `float \| None` | Elapsed seconds from `start_time` to this transition. `None` on the `RUNNING` transition. |
| `error` | `str \| None` | String form of the last exception. Set only on `FAILED`; `None` otherwise. |
| `stacktrace` | `str \| None` | Full traceback of the last exception. Set only on `FAILED`; `None` otherwise. |
| `tags` | `dict[str, str]` | Key/value labels attached to the task at enqueue time. |

---

## See also

- [task_log](task-log.md) for the function that produces `LogEvent` instances
- [Observability guide](../guide/observability.md) for combining observers and filtering by level
- [File Logging guide](../guide/file-logging.md) for multi-process rotation patterns
