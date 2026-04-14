# Task Context

`get_task_context()` returns a `TaskContext` object from inside any code path invoked during task execution. It gives you access to the current task's metadata without passing parameters through every function in the call stack.

## Basic usage

```python
from fastapi_taskflow import get_task_context, task_log

@task_manager.task(retries=3, delay=1.0)
def send_email(address: str) -> None:
    ctx = get_task_context()
    task_log(
        f"Sending to {address}",
        attempt=ctx.attempt if ctx else 0,
    )
```

`get_task_context()` returns `None` when called outside a managed task, so guard with `if ctx` when the same code might run outside a task.

## TaskContext fields

```python
@dataclass
class TaskContext:
    task_id:   str
    func_name: str
    attempt:   int
    tags:      dict[str, str]
```

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the currently running task. |
| `func_name` | `str` | Name of the task function. |
| `attempt` | `int` | Zero-based retry index. `0` means the first run. |
| `tags` | `dict[str, str]` | Key/value labels attached at enqueue time. |

## Works in helper functions

The context is set on the execution context, not just the task function body. It is accessible from any function called during task execution:

```python
def _validate_payload(data: dict) -> None:
    ctx = get_task_context()
    task_log(
        "Validating payload",
        attempt=ctx.attempt if ctx else 0,
        keys=list(data.keys()),
    )
    if not data.get("id"):
        raise ValueError("Missing required field: id")


@task_manager.task(retries=2)
async def process_event(data: dict) -> None:
    _validate_payload(data)   # get_task_context() works here too
    ...
```

## Works in sync and async tasks

`TaskContext` is propagated via Python's `contextvars`. It is set before the task function body executes and is available in any code path, whether the task is async or sync.

```python
@task_manager.task(retries=1)
def sync_task(user_id: int) -> None:
    ctx = get_task_context()
    if ctx and ctx.attempt > 0:
        task_log("This is a retry", level="warning", attempt=ctx.attempt)
    ...

@task_manager.task(retries=1)
async def async_task(user_id: int) -> None:
    ctx = get_task_context()
    if ctx and ctx.attempt > 0:
        task_log("This is a retry", level="warning", attempt=ctx.attempt)
    ...
```

## Reading tags

Tags attached at enqueue time are available on `ctx.tags`:

```python
@app.post("/report")
def queue_report(user_id: int, plan: str, tasks=Depends(task_manager.get_tasks)):
    tasks.add_task(
        generate_report,
        user_id,
        tags={"plan": plan, "source": "api"},
    )


@task_manager.task(retries=2)
def generate_report(user_id: int) -> None:
    ctx = get_task_context()
    plan = ctx.tags.get("plan", "free") if ctx else "free"
    task_log("Generating report", user_id=user_id, plan=plan)
```

## Outside a task context

`get_task_context()` always returns `None` when called from outside a running task. This makes it safe to use in shared utility code that may be called both inside and outside tasks:

```python
def log_step(message: str, **extra) -> None:
    ctx = get_task_context()
    if ctx:
        task_log(message, **extra)
    else:
        print(message, extra)   # fallback for non-task call sites
```

## See also

- [Task Logging](logging.md)
- [Adding Tasks](adding-tasks.md) - how to attach tags at enqueue time
- [`task_log` API reference](../api/task-log.md)
