# ManagedBackgroundTasks

A `BackgroundTasks` subclass that intercepts `add_task` to inject retries, status tracking, and task IDs.

Because it inherits from `BackgroundTasks`, `isinstance` checks pass and it is a drop-in replacement.

## Constructor

```python
ManagedBackgroundTasks(
    task_manager: TaskManager,
    background_tasks: BackgroundTasks | None = None,
)
```

When constructed via a dependency or `install()`, FastAPI passes its native `BackgroundTasks` instance. The wrapper shares the native task list so Starlette executes the wrapped tasks after the response is sent.

## Methods

### `add_task(func, *args, idempotency_key=None, tags=None, eager=None, priority=None, **kwargs) -> str`

Enqueues a function as a managed background task. Returns the `task_id` UUID string.

This overrides `BackgroundTasks.add_task` which returns `None`. If you are capturing the return value in existing code, this is the only behavioural change.

```python
task_id = background_tasks.add_task(send_email, address=email)

# With priority
task_id = background_tasks.add_task(send_otp, phone, priority=9)

# Eager dispatch (starts immediately, before response is sent)
task_id = background_tasks.add_task(notify, user_id, eager=True)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `func` | `Callable` | required | The task function to run. Must be registered with `@task_manager.task()` to get retries and config applied. |
| `*args` | | | Positional arguments forwarded to `func`. |
| `idempotency_key` | `str \| None` | `None` | Deduplication key. If a non-failed task with the same key already exists, its `task_id` is returned and `func` is not enqueued again. |
| `tags` | `dict[str, str] \| None` | `None` | Key/value labels attached to this task. Forwarded to every `LogEvent` and `LifecycleEvent`. |
| `eager` | `bool \| None` | `None` | When `True`, dispatch via `asyncio.create_task` immediately rather than waiting for the response to be sent. Overrides the decorator-level `eager` setting for this call only. |
| `priority` | `int \| None` | `None` | Route through the priority queue. Higher values run first. Conventional range 1-10. Overrides the decorator-level `priority` for this call only. Mutually exclusive with `eager` — when priority is set, `eager` is ignored. |
| `**kwargs` | | | Keyword arguments forwarded to `func`. |

## Direct construction

You can construct `ManagedBackgroundTasks` directly outside of a request context, for example in tests:

```python
from fastapi_taskflow import ManagedBackgroundTasks, TaskManager

tm = TaskManager()
managed = ManagedBackgroundTasks(tm)
task_id = managed.add_task(my_func, arg)
```

Tasks added this way will not be executed by Starlette automatically. You would need to call them manually or run them inside a request context.
