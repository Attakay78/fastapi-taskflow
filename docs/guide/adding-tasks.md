# Adding Tasks

This page explains how to enqueue tasks, what `add_task()` returns, and the options available at call time.

## How add_task works

Once you have a task function and a route that injects `ManagedBackgroundTasks`, enqueue work by calling `add_task()`:

```python
@app.post("/signup")
def signup(email: str, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(send_email, email)
    return {"task_id": task_id}
```

`add_task()` returns a `task_id` string immediately. The task itself runs after the response is sent.

## Positional and keyword arguments

Pass arguments the same way you would call the function directly:

```python
tasks.add_task(send_email, "user@example.com")                 # positional
tasks.add_task(send_email, address="user@example.com")         # keyword
tasks.add_task(resize_image, image_id, width=800, height=600)  # mixed
```

## Using the task_id to track status

The returned `task_id` can be stored in your database and used later to query task status via the API:

```python
@app.post("/report")
def request_report(user_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(generate_report, user_id)
    return {"task_id": task_id}

@app.get("/report/{task_id}")
def report_status(task_id: str):
    record = task_manager.store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404)
    return record.to_dict()
```

You can also query via the built-in API route at `GET /tasks/{task_id}` when `TaskAdmin` is mounted.

## Idempotency keys

Some operations should only run once, even if a client retries the request. Pass an `idempotency_key` to prevent duplicate execution:

```python hl_lines="6"
@app.post("/invoice/{invoice_id}/send")
def send_invoice(invoice_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        email_invoice,
        invoice_id,
        idempotency_key=f"invoice-{invoice_id}",
    )
    return {"task_id": task_id}
```

If a non-failed task with the same key already exists, `add_task()` returns its existing `task_id` without enqueuing the function again. No duplicate task is created.

!!! note
    Idempotency checks are performed in-process first (fast, no I/O). When a shared backend is configured and multiple instances are running, the check also covers tasks from other instances.

!!! tip
    A failed task does not block a re-run. If a task fails and you want to retry it, the existing key will not prevent a new attempt.

## Tags

Tags are key/value labels you attach to a task at enqueue time. They travel with the task through its entire lifecycle and are forwarded to every log and lifecycle event, making it straightforward to filter or aggregate by label in downstream systems.

```python hl_lines="6"
@app.post("/invoice")
def create_invoice(user_id: int, plan: str, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        process_invoice,
        user_id,
        tags={"user_id": str(user_id), "plan": plan, "source": "api"},
    )
    return {"task_id": task_id}
```

Inside the task function, tags are accessible via `get_task_context()`:

```python
from fastapi_taskflow import get_task_context, task_log

@task_manager.task(retries=2)
def process_invoice(user_id: int) -> None:
    ctx = get_task_context()
    plan = ctx.tags.get("plan", "free") if ctx else "free"
    task_log("Processing invoice", user_id=user_id, plan=plan)
```

See [Task Context](task-context.md) for the full `get_task_context()` API.

## Overriding eager and priority per call

The `eager` and `priority` values set on the decorator apply to every call. You can override them for a specific enqueue without changing the decorator:

```python
# Normally this task runs after the response.
# For this one call, dispatch it immediately.
task_id = tasks.add_task(send_email, email, eager=True)

# Route this call through the priority queue at level 9.
task_id = tasks.add_task(send_alert, message, priority=9)
```

**`eager=True`**: The task is dispatched via `asyncio.create_task` immediately when `add_task()` is called, before FastAPI sends the response. Use this when you need the task to start before the response goes out.

**`priority`**: Routes the task through the priority queue instead of the standard background task list. Higher integers run first. The conventional range is 1 (lowest) to 10 (highest), but any integer is accepted. Tasks with the same priority execute in arrival order (FIFO).

!!! info
    When `priority` is set, the `eager` flag is ignored. The priority queue provides its own non-blocking dispatch path.

## Adding tasks outside a route

If you need to enqueue a task from a startup handler, a management script, or anywhere else without a request context, construct `ManagedBackgroundTasks` directly and dispatch with `asyncio.create_task`:

```python
import asyncio
from fastapi_taskflow import ManagedBackgroundTasks

async def on_startup():
    tasks = ManagedBackgroundTasks(task_manager)
    task_id = tasks.add_task(sync_database, eager=True)
```

!!! note
    Pass `eager=True` (or `priority=...`) when adding tasks outside a route. Without a request lifecycle to trigger Starlette's background runner, the task would sit in the queue and never be dispatched.

For most situations, the route-based injection patterns are simpler and preferred. Direct construction is only necessary when no request context is available.
