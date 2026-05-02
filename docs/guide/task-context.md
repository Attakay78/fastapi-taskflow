# Task Context

This page explains how to access the current task's metadata from anywhere in your code, without threading parameters through every function call.

## The problem it solves

When a task runs, useful metadata is available: the task's UUID, which attempt number this is, the function name, and any tags attached at enqueue time. That information lives on the task record, but getting it into your helper functions is awkward. You would normally have to pass it as arguments through every layer of your call stack.

`get_task_context()` gives you a cleaner way. It returns a `TaskContext` object from inside any function that runs during task execution, whether that is the task function itself or a helper buried several calls deep.

## The `TaskContext` fields

```python
@dataclass
class TaskContext:
    task_id:   str
    func_name: str
    attempt:   int
    tags:      dict[str, str]
```

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | UUID of the currently running task. |
| `func_name` | `str` | Name of the task function. |
| `attempt` | `int` | Zero-based retry index. `0` means the first run, `1` means the first retry, and so on. |
| `tags` | `dict[str, str]` | Key/value labels attached to this task at enqueue time. |

The context is propagated via Python's `contextvars` module. It is set before the task function body executes and is available in any synchronous or asynchronous code path invoked during that execution.

## Basic usage

```python
from fastapi_taskflow import get_task_context, task_log

@task_manager.task(retries=3, delay=1.0)
def send_email(address: str) -> None:
    ctx = get_task_context()
    task_log(
        f"Sending to {address}",
        task_id=ctx.task_id if ctx else None,
        attempt=ctx.attempt if ctx else 0,
    )
    # ... send the email
```

## Using it inside a helper function

The context is not limited to the task function body. Any helper called during task execution can access it directly, without being passed any extra arguments.

```python
from fastapi_taskflow import get_task_context, task_log


def _validate_payload(data: dict) -> None:
    ctx = get_task_context()
    task_log(
        "Validating payload",
        attempt=ctx.attempt if ctx else 0,
        fields=list(data.keys()),
    )
    if not data.get("id"):
        raise ValueError("Missing required field: id")


def _enrich_payload(data: dict) -> dict:
    ctx = get_task_context()
    if ctx:
        data["_task_id"] = ctx.task_id
    return data


@task_manager.task(retries=2)
async def process_event(data: dict) -> None:
    _validate_payload(data)       # get_task_context() works here
    enriched = _enrich_payload(data)  # and here
    await send_to_downstream(enriched)
```

This pattern keeps your helper functions free of taskflow-specific parameters while still giving them access to task metadata when they need it.

## Detecting first run vs retry

The `attempt` field starts at `0` for the first execution and increments by one for each retry. Use it to change behaviour on retries without duplicating work.

```python hl_lines="5 9"
@task_manager.task(retries=3, delay=5.0, backoff=2.0)
def sync_inventory(store_id: str) -> None:
    ctx = get_task_context()

    if ctx and ctx.attempt == 0:
        # First attempt: do a full sync
        task_log("Running full inventory sync", store_id=store_id)
        data = fetch_full_inventory(store_id)
    elif ctx:
        # Retry: only sync what changed since the last attempt
        task_log(
            "Running incremental sync on retry",
            store_id=store_id,
            attempt=ctx.attempt,
            level="warning",
        )
        data = fetch_incremental_inventory(store_id)

    save_inventory(store_id, data)
```

!!! tip
    `attempt == 0` is a reliable check for the first run. If your task needs to skip an expensive setup step on retries, or send a notification only once regardless of how many retries follow, this is the right field to use.

## Reading tags

Tags are key/value strings you attach when enqueuing a task. They are available on `ctx.tags` and let you pass routing or configuration data to the task without adding function parameters.

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import TaskManager, get_task_context, task_log

task_manager = TaskManager()
app = FastAPI()


@app.post("/report")
def queue_report(user_id: int, plan: str, tasks=Depends(task_manager.get_tasks)):
    tasks.add_task(
        generate_report,
        user_id,
        tags={"plan": plan, "source": "api"},
    )
    return {"queued": True}


@task_manager.task(retries=2)
def generate_report(user_id: int) -> None:
    ctx = get_task_context()
    plan = ctx.tags.get("plan", "free") if ctx else "free"
    task_log("Generating report", user_id=user_id, plan=plan)

    if plan == "enterprise":
        generate_full_report(user_id)
    else:
        generate_summary_report(user_id)
```

## Calling `get_task_context()` outside a task

When called from outside a running task, `get_task_context()` returns `None`. This makes it safe to use in shared utility code that runs in both task and non-task contexts.

```python
def log_step(message: str, **extra) -> None:
    ctx = get_task_context()
    if ctx:
        task_log(message, task_id=ctx.task_id, **extra)
    else:
        # Fallback for scripts, tests, or direct function calls
        import logging
        logging.getLogger(__name__).info(message, extra=extra)
```

!!! warning "Always guard against `None`"
    `get_task_context()` returns `None` outside a task. If you access fields on the result without checking first, you will get an `AttributeError`. A simple `if ctx:` guard is all that is needed.

```python
# This will raise AttributeError if called outside a task
attempt = get_task_context().attempt   # do not do this

# This is safe anywhere
ctx = get_task_context()
attempt = ctx.attempt if ctx else 0
```

## See also

- [Task Logging](logging.md) — emit structured log entries from inside a task
- [Adding Tasks](adding-tasks.md) — how to attach tags at enqueue time
- [`task_log` API reference](../api/task-log.md)
