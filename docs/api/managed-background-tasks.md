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

### `add_task(func, *args, **kwargs) -> str`

Enqueues a function as a managed background task. Returns the `task_id` UUID string.

This overrides `BackgroundTasks.add_task` which returns `None`. If you are capturing the return value in existing code, this is the only behavioural change.

```python
task_id = background_tasks.add_task(send_email, address=email)
```

## Direct construction

You can construct `ManagedBackgroundTasks` directly outside of a request context, for example in tests:

```python
from fastapi_taskflow import ManagedBackgroundTasks, TaskManager

tm = TaskManager()
managed = ManagedBackgroundTasks(tm)
task_id = managed.add_task(my_func, arg)
```

Tasks added this way will not be executed by Starlette automatically. You would need to call them manually or run them inside a request context.
