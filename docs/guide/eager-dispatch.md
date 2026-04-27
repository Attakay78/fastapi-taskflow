# Eager Dispatch

By default, tasks added via `add_task()` are appended to Starlette's background task list and run after FastAPI sends the HTTP response. Eager dispatch bypasses this and starts the task immediately via `asyncio.create_task`, while the route handler is still executing.

## When to use it

The standard deferred mechanism is the right default for most tasks. Use eager dispatch when:

- The task result or side effect must be in progress before the client reconnects (e.g. a push notification that the client polls immediately after receiving the response)
- You want the task to overlap with response serialisation rather than start after it
- The task is fire-and-forget and you do not care about Starlette's task ordering guarantees

Do not use eager dispatch if:

- You need the task to run strictly after the response is sent (e.g. tasks that close database sessions used by the request handler)
- You are using priority queues — tasks with a `priority` value are always dispatched through the priority worker and ignore the `eager` setting

## Decorator-level default

Set `eager=True` on `@task_manager.task()` to make every invocation of that function dispatch eagerly:

```python
@task_manager.task(eager=True)
async def notify_user(user_id: int, message: str) -> None:
    await push_notification(user_id, message)
```

Every call to `add_task(notify_user, ...)` will start the task via `asyncio.create_task` at the moment `add_task()` returns, regardless of where the request is in its lifecycle.

## Per-call override

Override the decorator default for a specific enqueue call using the `eager` argument on `add_task()`:

```python
@app.post("/send")
def send(user_id: int, urgent: bool, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(notify_user, user_id, "you have mail", eager=urgent)
    return {"task_id": task_id}
```

Per-call `eager` takes precedence over the decorator setting. You can also force a normally-eager task to defer:

```python
task_id = tasks.add_task(notify_user, user_id, message, eager=False)
```

## Behaviour

When `eager=True` is resolved:

1. The task record is created in the store with status `PENDING` (same as normal)
2. `asyncio.create_task(wrapped())` is called immediately inside `add_task()`
3. `add_task()` returns the `task_id`
4. The route handler continues and eventually returns the response
5. The task runs concurrently with response serialisation and any remaining route code

The task is tracked, retried, and visible in the dashboard exactly the same as a deferred task. The only difference is when it starts.

## Sync tasks and eager dispatch

Eager dispatch works with sync task functions. The task is still wrapped in `asyncio.to_thread` (or submitted to the dedicated sync thread pool when `max_sync_threads` is set), so it does not block the event loop. The coroutine wrapper is what gets dispatched via `asyncio.create_task`.

## Example

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task(eager=True)
async def send_push(user_id: int, body: str) -> None:
    # Starts immediately when add_task() is called, overlapping with the response.
    await push_service.send(user_id, body)


@task_manager.task()
async def log_event(event: str) -> None:
    # Deferred — runs after the response is sent.
    await analytics.record(event)


@app.post("/action")
async def action(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    push_id = tasks.add_task(send_push, user_id, "Your request is processing")
    log_id  = tasks.add_task(log_event, "action.started")

    # send_push is already running here.
    # log_event will run after the response is sent.

    return {"push_task_id": push_id, "log_task_id": log_id}
```
