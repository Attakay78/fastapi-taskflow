# TaskFileLogger

Writes task log entries and lifecycle events to a plain text file. Normally created automatically by `TaskManager` when `log_file` is set. You can also construct one directly and pass it to lower-level APIs.

## Constructor

```python
TaskFileLogger(
    path: str,
    max_bytes: int = 10485760,
    backup_count: int = 5,
    mode: Literal["rotate", "watched"] = "rotate",
    log_lifecycle: bool = False,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | `str` | required | File path to write to. Created if it does not exist. |
| `max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating. Ignored in `watched` mode. |
| `backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `mode` | `str` | `"rotate"` | `"rotate"` uses `RotatingFileHandler` (thread-safe, single process). `"watched"` uses `WatchedFileHandler` (safe for multi-process same-host deployments with external rotation). |
| `log_lifecycle` | `bool` | `False` | When `True`, also write a line for each task status transition. |

## Methods

### `write(task_id, func_name, message)`

Write one log entry line to the file.

```
[abc12345] [send_email] 2024-01-01T12:00:00 message text
```

`task_id` is truncated to the first 8 characters.

### `lifecycle(task_id, func_name, event)`

Write a lifecycle event line when `log_lifecycle` is enabled. `event` is one of `RUNNING`, `SUCCESS`, `FAILED`, or `INTERRUPTED`.

```
[abc12345] [send_email] 2024-01-01T12:00:01 -- SUCCESS
```

Called automatically by the executor. No need to call this directly.

### `close()`

Flush and close all file handlers. Called automatically by `TaskAdmin` on app shutdown when `log_file` is configured.

## Thread safety

A single `TaskFileLogger` instance is safe for concurrent use across multiple threads (sync tasks run in a thread pool) and the asyncio event loop within one process.

For multi-process or multi-host deployments see [File Logging](../guide/file-logging.md).
