# Adding Tasks

Once you have a registered task function and a route wired up with one of the [injection patterns](injection.md), you enqueue work by calling `add_task()` on the injected `ManagedBackgroundTasks` instance.

## Basic usage

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

## Storing the task ID

The returned `task_id` can be stored and used to query task status later via the API:

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

## Idempotency keys

Pass an `idempotency_key` to prevent the same logical operation from running more than once, even if the route is called multiple times:

```python
@app.post("/invoice/{invoice_id}/send")
def send_invoice(invoice_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        email_invoice,
        invoice_id,
        idempotency_key=f"invoice-{invoice_id}",
    )
    return {"task_id": task_id}
```

If a non-failed task with the same key already exists, `add_task()` returns its existing `task_id` without enqueuing the function again. This works within a single process and across multiple instances when a shared backend is configured.

## Tags

Attach key/value labels to a task at enqueue time with `tags=`. Tags are stored on the task record and forwarded to every log and lifecycle event emitted by the task, making it easy to filter logs in downstream systems.

```python
@app.post("/invoice")
def create_invoice(user_id: int, plan: str, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        process_invoice,
        user_id,
        tags={"user_id": str(user_id), "plan": plan, "source": "api"},
    )
    return {"task_id": task_id}
```

Tags are accessible inside the task function via `get_task_context()`:

```python
from fastapi_taskflow import get_task_context, task_log

@task_manager.task(retries=2)
def process_invoice(user_id: int) -> None:
    ctx = get_task_context()
    plan = ctx.tags.get("plan", "free") if ctx else "free"
    task_log("Processing invoice", user_id=user_id, plan=plan)
```

See [Task Context](task-context.md) for the full `get_task_context()` API.

## Adding tasks outside a route

If you need to enqueue a task outside a request context (e.g. from a startup handler or a management script), construct `ManagedBackgroundTasks` directly and schedule it with `asyncio`:

```python
import asyncio
from fastapi_taskflow import ManagedBackgroundTasks
from fastapi_taskflow.executor import make_background_func

async def on_startup():
    tasks = ManagedBackgroundTasks(task_manager)
    task_id = tasks.add_task(sync_database)
    asyncio.create_task(tasks.tasks[-1]())
```

For most cases, the route-based patterns are simpler and preferred. Direct construction is only needed when no request context is available.
