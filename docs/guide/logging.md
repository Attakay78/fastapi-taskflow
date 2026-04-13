# Task Logging

`task_log()` lets you emit timestamped log entries from inside a running task. Logs are stored on the task record and shown in the dashboard detail panel under the **Logs** tab.

## Basic usage

```python
from fastapi_taskflow import task_log

@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_email(address: str) -> None:
    task_log("Connecting to SMTP server")
    task_log(f"Sending to {address}")
```

Import `task_log` and call it anywhere inside a managed task function. No setup or configuration is needed.

## Retries

When a task retries, a `--- Retry N ---` separator is inserted automatically between attempts. This makes it easy to see what each attempt did in the Logs tab.

```
2026-04-07T10:00:00 Connecting to SMTP server
2026-04-07T10:00:00 Sending to user@example.com
--- Retry 1 ---
2026-04-07T10:00:01 Connecting to SMTP server
2026-04-07T10:00:01 Sending to user@example.com
```

## Sync and async tasks

`task_log()` works the same in both sync and async tasks. Sync tasks run in a
thread pool, and log entries are safely handed back to the event loop so the
dashboard updates in real time.

```python
@task_manager.task(retries=1)
def generate_report(user_id: int) -> None:
    task_log(f"Starting report for user {user_id}")
    data = fetch_data(user_id)
    task_log("Report complete")

@task_manager.task(retries=1)
async def process_webhook(payload: dict) -> None:
    task_log(f"Received event: {payload['type']}")
    await send_to_service(payload)
    task_log("Forwarded successfully")
```

## Outside task context

Calls made outside a running task (at import time, in helper functions, etc.) are silently ignored. You can safely use `task_log()` in shared code without needing to check whether you are inside a task.

## Error visibility

When a task fails, the full Python traceback is captured automatically alongside the error message. Both are shown in the dashboard detail panel under the **Error** tab. No configuration needed.

Failed tasks with logs will show both the **Logs** tab and the **Error** tab in the detail panel.

## Screenshots

**Logs tab** — timestamped entries per attempt, separated by retry markers:

[![Task logs panel](../assets/images/logs.png){ .screenshot }](../assets/images/logs.png){ target="_blank" }

**Error tab** — error message and collapsible stack trace:

[![Task error and stack trace panel](../assets/images/error.png){ .screenshot }](../assets/images/error.png){ target="_blank" }

See [File Logging](file-logging.md) for writing log entries to a plain text file for use with `tail -f`, `grep`, and log shippers.
