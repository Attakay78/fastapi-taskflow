# Task Logging

`task_log()` lets you emit structured log entries from inside a running task. Logs are stored on the task record and shown in the dashboard detail panel under the **Logs** tab. They are also forwarded to any configured observer (see [Observability](observability.md)).

## Basic usage

```python
from fastapi_taskflow import task_log

@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_email(address: str) -> None:
    task_log("Connecting to SMTP server")
    task_log(f"Sending to {address}")
```

Call `task_log()` anywhere inside a managed task function. No setup or configuration is needed.

## Log levels

Pass a `level=` keyword argument to classify the entry. The default is `"info"`.

```python
task_log("Starting sync", level="debug")
task_log("Sync complete")                          # level="info" by default
task_log("Rate limit reached, retrying", level="warning")
task_log("Payment gateway error", level="error")
```

Valid levels: `"debug"`, `"info"`, `"warning"`, `"error"`.

Observers such as `FileLogger` and `StdoutLogger` can filter by `min_level` so low-severity entries are suppressed in production without removing them from the task record.

## Structured extras

Pass arbitrary keyword arguments to attach structured fields to the log entry. The fields are forwarded to every configured observer as `LogEvent.extra` and do not appear in the dashboard log line.

```python
task_log("Payment processed", order_id="ord_123", amount=49.99, currency="USD")
task_log("Retry failed", level="warning", attempt=2, error_code=503)
```

Extras are useful for filtering in downstream log aggregators (Loki, Datadog, CloudWatch) using structured queries without parsing the message string.

## Retries

When a task retries, a `--- Retry N ---` separator is inserted automatically between attempts. This makes it easy to see what each attempt did in the Logs tab.

```
2026-04-01T10:00:00 Connecting to SMTP server
2026-04-01T10:00:00 Sending to user@example.com
--- Retry 1 ---
2026-04-01T10:00:01 Connecting to SMTP server
2026-04-01T10:00:01 Sending to user@example.com
```

## Sync and async tasks

`task_log()` works the same in both sync and async tasks. Sync tasks run in a thread pool and log entries are safely handed back to the event loop.

```python
@task_manager.task(retries=1)
def generate_report(user_id: int) -> None:
    task_log(f"Starting report for user {user_id}", user_id=user_id)
    data = fetch_data(user_id)
    task_log(f"Fetched {len(data)} records", count=len(data))
    task_log("Report complete")

@task_manager.task(retries=1)
async def process_webhook(payload: dict) -> None:
    task_log(f"Received event: {payload['type']}", event_type=payload["type"])
    await send_to_service(payload)
    task_log("Forwarded successfully")
```

## Outside task context

When called outside a managed task (direct function call, script, cron job, test), `task_log()` forwards to the standard library logger `fastapi_taskflow.task` at the matching log level instead of dropping the call.

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Now task_log() output is visible even when calling the function directly
send_email("user@example.com")
```

Configure that logger via Python's standard logging setup to control where the output goes:

```python
import logging

# Suppress fallback output entirely
logging.getLogger("fastapi_taskflow.task").setLevel(logging.CRITICAL)

# Route to a specific handler
handler = logging.FileHandler("fallback.log")
logging.getLogger("fastapi_taskflow.task").addHandler(handler)
```

This means task functions work correctly in all call contexts without any code changes: through `add_task()` in a route, from a management script, from a cron job, or in a unit test calling the function directly.

## Error visibility

When a task fails, the full Python traceback is captured automatically alongside the error message. Both are shown in the dashboard detail panel under the **Error** tab.

## Screenshots

**Logs tab** - timestamped entries per attempt, separated by retry markers:

[![Task logs panel](../assets/images/logs.png){ .screenshot }](../assets/images/logs.png){ target="_blank" }

**Error tab** - error message and collapsible stack trace:

[![Task error and stack trace panel](../assets/images/error.png){ .screenshot }](../assets/images/error.png){ target="_blank" }

## See also

- [Task Context](task-context.md) - access task metadata (task_id, attempt, tags) from inside any task
- [Observability](observability.md) - send structured log events to files, stdout, or custom observers
