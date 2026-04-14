# Loggers

fastapi-taskflow ships three built-in observer implementations. All implement the `TaskObserver` ABC and can be combined in any order via the `loggers=` parameter on `TaskManager`.

## FileLogger

Writes log and lifecycle events to a rotating plain text file.

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
| `path` | `str` | required | File path to write to. Created if it does not exist. |
| `max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating. Ignored in `watched` mode. |
| `backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `mode` | `str` | `"rotate"` | `"rotate"` uses `RotatingFileHandler` (single process). `"watched"` uses `WatchedFileHandler` (multi-process with external rotation). |
| `log_lifecycle` | `bool` | `False` | Also write lifecycle transitions (`RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`). |
| `min_level` | `str` | `"info"` | Minimum log level to write. Entries below this level are dropped. |

### Output format

Log entry:
```
[abc12345] [send_email] 2026-01-01T12:00:00 Sending to user@example.com
```

Lifecycle entry (when `log_lifecycle=True`):
```
[abc12345] [send_email] 2026-01-01T12:00:01 -- SUCCESS
```

### Thread safety

A single `FileLogger` instance is safe for concurrent use across multiple threads (sync tasks run in a thread pool) and the asyncio event loop within one process.

For multi-process or multi-host deployments see [File Logging](../guide/file-logging.md).

---

## StdoutLogger

Prints log and lifecycle events to stdout. Useful in containers where stdout is captured by the logging agent.

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
| `log_lifecycle` | `bool` | `False` | Also print lifecycle transitions. |
| `min_level` | `str` | `"info"` | Minimum log level to print. |

### Output format

Matches `FileLogger`:
```
[abc12345] [send_email] 2026-01-01T12:00:00 Sending to user@example.com
```

---

## InMemoryLogger

Captures events in memory. Designed for tests.

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
| `min_level` | `str` | `"debug"` | Minimum log level to capture. Default captures all levels. |

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `log_events` | `list[LogEvent]` | All captured log events in order. |
| `lifecycle_events` | `list[LifecycleEvent]` | All captured lifecycle events in order. |

### Methods

| Method | Description |
|--------|-------------|
| `clear()` | Empty both event lists. |

### Example

```python
from fastapi_taskflow import InMemoryLogger, TaskManager
from fastapi_taskflow.executor import execute_task
from fastapi_taskflow.models import TaskConfig
from fastapi_taskflow.store import TaskStore

mem = InMemoryLogger()
tm = TaskManager(loggers=[mem])

@tm.task()
def my_task() -> None:
    task_log("hello")

store = TaskStore()
store.create("t1", "my_task", (), {})

import asyncio
asyncio.run(execute_task(my_task, "t1", TaskConfig(), store, (), {}, logger=tm.logger))

assert mem.log_events[0].message == "hello"
assert mem.lifecycle_events[-1].status.value == "success"
```

---

## TaskObserver ABC

Implement this to create a custom observer.

```python
from fastapi_taskflow import TaskObserver
from fastapi_taskflow.loggers.base import LifecycleEvent, LogEvent


class MyObserver(TaskObserver):
    async def on_log(self, event: LogEvent) -> None:
        ...

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        ...

    async def startup(self) -> None:
        ...  # called when TaskAdmin mounts the app

    async def close(self) -> None:
        ...  # called on app shutdown
```

`startup()` and `close()` default to no-ops in the base class. Override only what you need.

---

## LoggerChain

`LoggerChain` fans out to multiple observers. It is created internally by `TaskManager` when `loggers=` is set. You do not normally need to instantiate it directly.

```python
from fastapi_taskflow import LoggerChain, FileLogger, StdoutLogger

chain = LoggerChain([
    FileLogger("tasks.log"),
    StdoutLogger(),
])
```

Observers run sequentially. An exception in one observer is caught and logged at `ERROR` level; it does not affect subsequent observers or the task itself.

---

## See also

- [Observability guide](../guide/observability.md)
- [File Logging](../guide/file-logging.md)
- [task_log](task-log.md)
