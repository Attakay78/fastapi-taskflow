# File Logging

This page shows you how to write task logs to a plain text file, filter by severity, and handle log rotation safely when multiple processes share the same host.

## Why write to a file

The dashboard is great for watching tasks in real time, but a plain text log file gives you tools the dashboard does not.

You can run `tail -f tasks.log` to follow output in a terminal, use `grep` to search history, and feed the file to any standard log shipper (Loki, Datadog, Fluentd, CloudWatch) that reads files directly. These workflows are especially useful in production where you want a durable, queryable record of what every task did.

## Basic setup

The quickest way to enable file logging is the `log_file` shorthand on `TaskManager`:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks.log",
)
```

Alternatively, pass a `FileLogger` instance directly to `loggers=`. This gives you access to all options in one place:

```python
from fastapi_taskflow import FileLogger, TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[FileLogger("tasks.log")],
)
```

Both approaches produce identical output. Use `loggers=` when you need more than one observer, for example combining a `FileLogger` with a `StdoutLogger`. See [Observability](observability.md) for the full observer system.

## Log format

Every `task_log()` call produces one line:

```
[task_id] [func_name] 2026-01-01T12:00:00 message text
```

For example:

```
[abc12345] [send_email] 2026-01-01T12:00:00 Connecting to SMTP server
[abc12345] [send_email] 2026-01-01T12:00:00 Sending to user@example.com
[abc12345] [send_email] --- Retry 1 ---
[abc12345] [send_email] 2026-01-01T12:00:02 Connecting to SMTP server
```

## Lifecycle events

By default only `task_log()` messages are written. Set `log_lifecycle=True` to also write a line for each status transition (RUNNING, SUCCESS, FAILED, INTERRUPTED). This makes it easy to calculate task duration or count failures with `grep`.

```python
FileLogger("tasks.log", log_lifecycle=True)
```

Output:

```
[abc12345] [send_email] 2026-01-01T12:00:00 -- RUNNING
[abc12345] [send_email] 2026-01-01T12:00:00 Connecting to SMTP server
[abc12345] [send_email] 2026-01-01T12:00:01 -- SUCCESS
```

## Filtering by log level

Set `min_level=` to drop entries below a certain severity. Entries below the threshold are silently ignored and never written to the file.

```python
FileLogger("tasks.log", min_level="warning")
```

Valid levels from lowest to highest: `"debug"`, `"info"`, `"warning"`, `"error"`. The default is `"info"`.

## FileLogger parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | File path to write to. Created if it does not exist. |
| `max_bytes` | `int` | `10485760` | File size limit (10 MB) before rotating. Ignored in `watched` mode. |
| `backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `mode` | `str` | `"rotate"` | `"rotate"` for automatic rotation, `"watched"` for external rotation. |
| `log_lifecycle` | `bool` | `False` | Write a line on each task status transition. |
| `min_level` | `str` | `"info"` | Minimum log level to write. |

## The multi-process rotation problem

When you run several workers on the same host (for example, Gunicorn with multiple Uvicorn workers), all processes write to the same file. Normal writes are fine, but automatic rotation introduces a race condition.

`"rotate"` mode uses Python's `RotatingFileHandler`. When the file hits `max_bytes`, the handler does this:

1. Close `tasks.log`
2. Rename `tasks.log` to `tasks.log.1`
3. Open a new `tasks.log`
4. Write the line

These are three separate OS calls with no cross-process lock. If two workers hit the size limit at the same moment, both try to rename `tasks.log` to `tasks.log.1`. One rename wins and the other silently overwrites it, dropping the previous backup.

!!! warning
    Do not use `mode="rotate"` when multiple processes on the same host write to the same file path.

## Safe strategies for multiple processes

### Separate file per instance (recommended)

Give each worker its own path. Each file rotates independently with no coordination needed:

```python
# Worker 1
task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[FileLogger("tasks-1.log", log_lifecycle=True)],
)

# Worker 2
task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[FileLogger("tasks-2.log", log_lifecycle=True)],
)
```

In Docker or Kubernetes, drive the path from an environment variable so each container gets a unique file automatically:

```python
import os

worker_id = os.getenv("WORKER_ID", "0")
task_manager = TaskManager(
    loggers=[FileLogger(f"tasks-{worker_id}.log")],
)
```

### External rotation with watched mode

Set `mode="watched"` and let logrotate manage rotation instead. `WatchedFileHandler` does not rotate on its own. On every write it checks whether the file it has open still matches the inode on disk. If logrotate has replaced the file, the handler detects the mismatch and reopens the new file before writing.

```python hl_lines="6"
task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[
        FileLogger(
            "/var/log/myapp/tasks.log",
            mode="watched",
            log_lifecycle=True,
        )
    ],
)
```

All workers write to the same path. Rotation is handled entirely by logrotate, so there is no race condition between Python processes.

## Setting up logrotate

Create `/etc/logrotate.d/myapp`:

```
/var/log/myapp/tasks.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    create 0644 www-data www-data
    postrotate
        pkill -HUP -f "uvicorn myapp"
    endscript
}
```

Key directives:

| Directive | What it does |
|---|---|
| `daily` | Rotate once a day. Also accepts `weekly`, `monthly`, or `size 100M`. |
| `rotate 7` | Keep 7 backup files before deleting the oldest. |
| `compress` | Gzip rotated files to save disk space. |
| `create 0644 www-data www-data` | Create a fresh empty file after rotation with the correct owner. |
| `postrotate` | Signal the app after rotation. Optional with `WatchedFileHandler`. |

!!! tip
    The `postrotate` block is optional when using `mode="watched"`. `WatchedFileHandler` detects the replaced file on the next write regardless of whether the process received a signal.

If your app writes a PID file, you can use `kill -HUP` with it instead of `pkill`:

```
postrotate
    kill -HUP $(cat /var/run/myapp.pid)
endscript
```

To test your config before applying it:

```bash
# Dry run: shows what would happen without touching any files
logrotate --debug /etc/logrotate.d/myapp

# Force a rotation right now
sudo logrotate --force /etc/logrotate.d/myapp
```

## Multiple hosts (Redis backend)

When workers run on separate machines, each host writes its own file. `"rotate"` mode is safe here because no two processes share a file path. Use a log shipper to aggregate files from all hosts:

```python
from fastapi_taskflow import FileLogger, TaskManager
from fastapi_taskflow.backends import RedisBackend

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://your-redis-host:6379/0"),
    loggers=[FileLogger("tasks.log", log_lifecycle=True)],
)
```

## See also

- [Observability](observability.md)
- [Multi-Instance Deployments](multi-instance.md)
