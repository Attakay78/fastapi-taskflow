# ManagedBackgroundTasks

`ManagedBackgroundTasks` is a `BackgroundTasks` subclass that intercepts `add_task()` to inject retries, status tracking, priority queuing, and task ID generation.

Because it inherits from Starlette's `BackgroundTasks`, `isinstance` checks pass and it is a drop-in replacement anywhere `BackgroundTasks` is used.

> **Guide:** [Task Manager](../guide/task-manager.md) covers the three injection patterns (dependency, install, and direct construction).

---

## Constructor

```python
from fastapi_taskflow import ManagedBackgroundTasks

ManagedBackgroundTasks(
    task_manager: TaskManager,
    background_tasks: BackgroundTasks | None = None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task_manager` | `TaskManager` | required | The task manager that holds the registry, store, and observer chain. |
| `background_tasks` | `BackgroundTasks \| None` | `None` | The native FastAPI `BackgroundTasks` instance for the current request. When provided, tasks added via `add_task()` are appended to this list so Starlette executes them after the response is sent. When `None`, tasks are dispatched eagerly. |

When constructed via a dependency (`Depends(task_manager.get_tasks)`) or `install()`, FastAPI passes its own native `BackgroundTasks` instance automatically.

---

## Methods

### `add_task(func, *args, **kwargs) -> str`

```python
def add_task(
    func: Callable,
    *args: Any,
    idempotency_key: str | None = None,
    tags: dict[str, str] | None = None,
    eager: bool | None = None,
    priority: int | None = None,
    **kwargs: Any,
) -> str
```

Enqueues a function as a managed background task. Returns the `task_id` UUID string.

This overrides `BackgroundTasks.add_task()`, which returns `None`. If you are already capturing the return value in existing code, this is the only behavioural difference to be aware of.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `func` | `Callable` | required | The task function to run. Must be registered with `@task_manager.task()` for retries and `TaskConfig` settings to apply. Unregistered functions are still executed but without retry logic. |
| `*args` | `Any` | | Positional arguments forwarded to `func`. |
| `idempotency_key` | `str \| None` | `None` | Deduplication key. If a non-failed task with the same key already exists in the store or backend, its `task_id` is returned and `func` is not enqueued again. |
| `tags` | `dict[str, str] \| None` | `None` | Key/value labels attached to this task. Forwarded to every `LogEvent` and `LifecycleEvent` emitted for the task. |
| `eager` | `bool \| None` | `None` | When `True`, dispatch via `asyncio.create_task` immediately rather than waiting for the response to be sent. Overrides the decorator-level `eager` setting for this call only. |
| `priority` | `int \| None` | `None` | Route through the priority queue instead of Starlette's background task list. Higher values run first; the conventional range is 1 (lowest) to 10 (highest). Overrides the decorator-level `priority` for this call only. Mutually exclusive with `eager`: when `priority` is set, `eager` is ignored. |
| `**kwargs` | `Any` | | Keyword arguments forwarded to `func`. |

**Examples:**

```python
# Basic enqueue
task_id = background_tasks.add_task(send_email, address=email)

# With priority (runs before lower-priority tasks)
task_id = background_tasks.add_task(send_otp, phone, priority=9)

# Eager dispatch (starts before the response is sent)
task_id = background_tasks.add_task(notify, user_id, eager=True)

# With deduplication
task_id = background_tasks.add_task(
    generate_invoice,
    order_id,
    idempotency_key=f"invoice:{order_id}",
)

# With tags for observability
task_id = background_tasks.add_task(
    process_payment,
    amount,
    tags={"customer_id": str(customer_id), "plan": "pro"},
)
```

---

## Injection patterns

Three patterns are supported. All three return a `ManagedBackgroundTasks` instance:

**Explicit dependency** (recommended for route-scoped injection):

```python
from fastapi import Depends

@app.post("/signup")
def signup(
    email: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.get_tasks),
):
    task_id = tasks.add_task(send_welcome_email, email)
    return {"task_id": task_id}
```

**Explicit type** (after calling `task_manager.install(app)`):

```python
@app.post("/signup")
def signup(email: str, background_tasks: ManagedBackgroundTasks):
    task_id = background_tasks.add_task(send_welcome_email, email)
    return {"task_id": task_id}
```

**Zero-migration** (after calling `task_manager.install(app)`, bare `BackgroundTasks` receives the managed instance):

```python
from fastapi import BackgroundTasks

@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(send_welcome_email, email)
    # background_tasks is a ManagedBackgroundTasks instance at runtime
```

---

## Direct construction for testing

You can construct `ManagedBackgroundTasks` directly outside of a request context. This is useful in tests:

```python
from fastapi_taskflow import ManagedBackgroundTasks, TaskManager

tm = TaskManager()
managed = ManagedBackgroundTasks(tm)
task_id = managed.add_task(my_func, arg)
```

Tasks added this way are not executed by Starlette automatically because there is no active request. To execute them in a test, either call the function directly or run the task inside an `asyncio` event loop using the executor directly.
