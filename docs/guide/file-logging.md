# File Logging

In addition to the dashboard, log entries can be written to a plain text file. This makes it possible to use standard tools like `tail -f`, `grep`, and log shippers (Loki, Datadog, Fluentd, CloudWatch) alongside the dashboard.

## Basic setup

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks.log",
)
```

Every `task_log()` call and retry separator is written to the file as well as stored on the task record.

Each line has the format:

```
[task_id] [func_name] 2024-01-01T12:00:00 message text
```

For example:

```
[abc12345] [send_email] 2024-01-01T12:00:00 Connecting to SMTP server
[abc12345] [send_email] 2024-01-01T12:00:00 Sending to user@example.com
[abc12345] [send_email] --- Retry 1 ---
[abc12345] [send_email] 2024-01-01T12:00:02 Connecting to SMTP server
```

## Lifecycle events

Set `log_lifecycle=True` to also write a line for each task status transition (`RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`):

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks.log",
    log_lifecycle=True,
)
```

Output:

```
[abc12345] [send_email] 2024-01-01T12:00:00 -- RUNNING
[abc12345] [send_email] 2024-01-01T12:00:00 Connecting to SMTP server
[abc12345] [send_email] 2024-01-01T12:00:01 -- SUCCESS
```

## Rotation options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_file` | `str` | `None` | Path to the log file. File logging is disabled when not set. |
| `log_file_max_bytes` | `int` | `10485760` | Maximum file size (10 MB) before rotating. Ignored in `watched` mode. |
| `log_file_backup_count` | `int` | `5` | Number of rotated backup files to keep. Ignored in `watched` mode. |
| `log_file_mode` | `str` | `"rotate"` | `"rotate"` for automatic rotation; `"watched"` for external rotation. |
| `log_lifecycle` | `bool` | `False` | Write a line on each task status transition. |

## Multi-instance deployments

### Why `"rotate"` mode is not safe with multiple processes

`"rotate"` mode uses Python's `RotatingFileHandler`. When the file reaches `max_bytes`, the handler does this sequence:

1. Close `tasks.log`
2. Rename `tasks.log` to `tasks.log.1`
3. Open a new `tasks.log`
4. Write the line

These are three separate OS calls with no lock across processes. If two processes hit the size limit at the same time, they both try to rename `tasks.log` to `tasks.log.1`. One rename wins silently and the other process's backup is overwritten. Additionally, after the rename the losing process still holds an open file handle pointing to the old inode, so its writes go to `tasks.log.1` instead of the new `tasks.log` until it closes and reopens.

### Safe strategies

**Separate file per instance (recommended)**

Give each instance its own path. Each file rotates independently with no coordination needed:

```python
# Instance 1
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks-1.log",
    log_lifecycle=True,
)

# Instance 2
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks-2.log",
    log_lifecycle=True,
)
```

**External rotation with `"watched"` mode**

Set `log_file_mode="watched"` and let an external tool such as `logrotate` manage rotation. `WatchedFileHandler` does not rotate at all. On every write it checks whether the file it has open still matches the inode and device of the path on disk. If `logrotate` has replaced the file, the handler detects the mismatch and reopens the path before writing.

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="/var/log/myapp/tasks.log",
    log_file_mode="watched",
    log_lifecycle=True,
)
```

The rotation is handled entirely by logrotate as a single atomic `rename()` + `create`. Your app has no knowledge of logrotate — it just detects the replaced file on the next write and reopens it.

### Setting up logrotate

logrotate is a system daemon separate from your app. It reads drop-in config files from `/etc/logrotate.d/`. Create `/etc/logrotate.d/myapp`:

```
/var/log/myapp/tasks.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    create 0644 www-data www-data
    postrotate
        kill -HUP $(cat /var/run/myapp.pid)
    endscript
}
```

Key directives:

| Directive | What it does |
|---|---|
| `daily` | Rotate once a day (also: `weekly`, `monthly`, `size 100M`) |
| `rotate 7` | Keep 7 backup files |
| `compress` | Gzip old files |
| `create 0644 www-data www-data` | Create a new empty file after rotation with the right owner |
| `postrotate` | Signal your app after rotation (optional with `WatchedFileHandler`) |

The `postrotate` block is optional. `WatchedFileHandler` detects the replaced file on the next write regardless. Sending `SIGHUP` just makes the transition immediate rather than waiting for the next log line.

If you do not have a PID file, use `pkill`:

```
postrotate
    pkill -HUP -f "uvicorn myapp"
endscript
```

**Testing the config:**

```bash
# Dry run — shows what logrotate would do without doing it
logrotate --debug /etc/logrotate.d/myapp

# Force a rotation now (ignores schedule)
sudo logrotate --force /etc/logrotate.d/myapp

# Verify both files have different inodes (confirms clean rotation)
ls -li /var/log/myapp/tasks.log*
```

### Multiple hosts (Redis)

Each host writes its own file. `"rotate"` mode is safe here because no two processes share a file path. Use a log shipper to aggregate them:

```python
# Instance 1
task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://localhost:6379/0"),
    log_file="tasks.log",
    log_lifecycle=True,
)
```

### Docker / no logrotate

If you are running in Docker without logrotate, use separate files per instance and let your log driver handle rotation. Set a unique path per worker using an environment variable:

```python
import os

worker_id = os.getenv("WORKER_ID", "0")
task_manager = TaskManager(
    log_file=f"tasks-{worker_id}.log",
)
```
