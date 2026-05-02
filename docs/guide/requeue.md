# Pending Requeue

This page explains what happens to unfinished tasks when your application stops, how they are saved, and how to safely re-dispatch them on the next startup.

## Why this matters

When an application stops, some tasks may not have finished yet. Without requeue, those tasks are silently dropped. With requeue enabled, fastapi-taskflow saves them at shutdown and dispatches them again on the next startup, so no queued work is lost.

## Enabling requeue

Requeue requires both a persistence backend and the `requeue_pending` flag:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    requeue_pending=True,
)
```

## What happens at shutdown

At shutdown, every task in either `PENDING` or `RUNNING` state is written to the pending store. What happens next depends on which state the task was in.

**PENDING tasks** (queued but not yet started) are saved as-is and will be re-dispatched on the next startup with no extra configuration needed.

**RUNNING tasks** (mid-execution at shutdown) require more thought. The right behavior depends on whether running the task function from scratch a second time is safe.

## RUNNING tasks and the interrupt problem

A task that was executing when the app shut down may have already done something that cannot be undone, such as sending an email, charging a card, or calling an external API. Re-running it from scratch could cause duplicates or double charges.

Because of this, the default behavior is conservative: `RUNNING` tasks are recorded in history with status `INTERRUPTED`. They appear in the dashboard but are not re-dispatched automatically.

!!! warning
    Only opt in to requeue for interrupted tasks if the task function is **idempotent**, meaning it produces the same outcome whether it runs once or multiple times from scratch. If you are not sure, leave the default and handle `INTERRUPTED` tasks manually via the dashboard or the retry API.

## `requeue_on_interrupt`: opting in per task

If a task is safe to run from scratch even after partial execution, add `requeue_on_interrupt=True` to its decorator:

```python hl_lines="6"
@task_manager.task(retries=2, requeue_on_interrupt=True)
def sync_inventory(product_id: int) -> None:
    # Uses upsert semantics throughout; safe to run twice.
    db.execute(
        "INSERT INTO inventory (id, stock) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET stock=excluded.stock",
        (product_id, fetch_stock(product_id)),
    )
```

This function is idempotent because repeated calls produce the same database state. Running it twice does not create duplicate rows or leave data inconsistent.

Contrast that with a task that is not idempotent:

```python
@task_manager.task(retries=3)
def send_welcome_email(user_id: int) -> None:
    # Sends an email; not safe to run twice.
    email_client.send(get_user_email(user_id), template="welcome")
```

This task should not use `requeue_on_interrupt=True`. If it was interrupted mid-execution, re-running it would send the same email a second time.

## Task statuses at a glance

| Status | Meaning |
|---|---|
| `PENDING` | Queued, not yet started |
| `RUNNING` | Currently executing |
| `SUCCESS` | Completed without error |
| `FAILED` | All retries exhausted |
| `INTERRUPTED` | Was running at shutdown, not requeued |

## How re-dispatch works on startup

On startup, the scheduler reads the pending store and re-dispatches each saved task. The process is:

1. Each task is matched back to its registered function by name.
2. If the function is still registered, the task is dispatched.
3. If the function is no longer registered (for example, it was renamed between deploys), the task is skipped with a warning log.

In a multi-instance deployment, pending tasks are claimed atomically before dispatch. Only one instance picks up each task, even if several instances start at the same time.

```mermaid
sequenceDiagram
    participant Scheduler
    participant Backend
    participant Registry
    participant Executor

    Note over Scheduler: startup
    Scheduler->>Backend: load_pending()
    Backend-->>Scheduler: pending records
    Scheduler->>Backend: load()
    Backend-->>Scheduler: history records
    Note over Scheduler: build set of already-completed task_ids

    loop for each pending task
        alt task_id in completed history
            Scheduler->>Scheduler: skip (already succeeded before crash)
        else
            Scheduler->>Backend: claim_pending(task_id)
            alt claimed
                Scheduler->>Registry: get_by_name(func_name)
                alt function found
                    Registry-->>Scheduler: func, config
                    Scheduler->>Executor: create_task(execute_task)
                else function not found
                    Scheduler->>Scheduler: log warning, skip
                end
            else already claimed by another instance
                Scheduler->>Scheduler: skip
            end
        end
    end

    Scheduler->>Backend: clear_pending()
```

!!! note
    Requeued tasks start from scratch. There is no partial execution state. The task function receives the same arguments it was originally called with and runs from the beginning.

## The hard crash limitation

Requeue works on a **clean shutdown**: SIGTERM, Uvicorn graceful stop, or any other path that allows the shutdown hook to run. At shutdown, fastapi-taskflow writes the pending store before the process exits.

On a **hard crash** (SIGKILL, OOM kill, or power loss), the shutdown hook never runs and the pending store is never written. Tasks that were running or queued at the time of the crash are lost.

!!! warning
    There is no recovery from a hard crash at the framework level. If your application must not lose tasks under any circumstance, you need an external coordination mechanism, such as a durable message queue, outside this library.

## Best practices

- Only set `requeue_on_interrupt=True` on functions whose entire body is idempotent, not just the final write.
- Check what shutdown signal your deployment sends. Kubernetes sends SIGTERM before SIGKILL, which gives the application time to run the shutdown hook. Make sure your `terminationGracePeriodSeconds` is long enough for in-flight tasks to be written.
- Monitor `INTERRUPTED` tasks in the dashboard. If they appear frequently, it may mean your shutdown timeout is too short for the work your tasks are doing.
- The pending store is separate from the history store. Requeued tasks appear as new records in history once they complete.
