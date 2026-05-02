# Eager Dispatch

This page explains what eager dispatch does, when it actually makes a difference, and when you do not need it at all.

## The default: tasks start after the response

When you call `add_task()` inside a route handler, the task is appended to Starlette's background task list. Starlette runs that list after it has sent the HTTP response to the client.

This is intentional. The task starts only when the request lifecycle is fully complete, which makes it safe to close database sessions, flush buffers, or do any other cleanup in the same request handler without worrying about a task reading shared state mid-teardown.

For separate HTTP requests, this default is exactly what you want. Each request runs its own route handler, and tasks from one request start naturally after that response is sent while tasks from the next request are already queuing up. Concurrency between requests is handled by FastAPI's async model without any extra configuration.

## What eager dispatch changes

Setting `eager=True` on a task makes `add_task()` call `asyncio.create_task()` immediately, right at the moment `add_task()` returns, while the route handler is still executing.

The task starts running concurrently with the rest of the route handler and with response serialisation. It does not wait for the response to be sent.

!!! note
    The task is still tracked, persisted, retried, and visible in the dashboard exactly the same as a deferred task. The only thing eager dispatch changes is *when* the task starts.

## When eager dispatch matters

The one scenario where eager dispatch makes a real difference is a batch endpoint: a single route handler that calls `add_task()` multiple times in a loop.

Starlette's background task list runs tasks sequentially, one after another, after the response is sent. If you add 20 tasks in one request handler, they run one at a time even if each one is a short async network call that could all be in flight simultaneously.

```python hl_lines="7"
@app.post("/batch-notify")
async def batch_notify(
    user_ids: list[int],
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    for user_id in user_ids:
        tasks.add_task(notify_user, user_id)
    return {"queued": len(user_ids)}
```

Without eager dispatch, all 20 notifications run sequentially. With `eager=True`, each `add_task()` call creates an independent asyncio task immediately, so all 20 notifications run concurrently.

!!! info
    If your tasks come from different HTTP requests rather than a single handler loop, you do not need eager dispatch. Each request's tasks start after that response is sent, and multiple requests are handled concurrently by FastAPI naturally.

## Setting eager on the decorator

Add `eager=True` to `@task_manager.task()` to make every call to that function dispatch eagerly by default.

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task(eager=True)
async def notify_user(user_id: int, message: str) -> None:
    await push_notification(user_id, message)
```

Every call to `add_task(notify_user, ...)` will start the task via `asyncio.create_task` at the moment `add_task()` returns.

## Overriding per call

Pass `eager=` directly to `add_task()` to override the decorator setting for a single enqueue.

```python
@app.post("/send")
async def send(
    user_id: int,
    urgent: bool,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_id = tasks.add_task(notify_user, user_id, "you have mail", eager=urgent)
    return {"task_id": task_id}
```

You can also force a normally-eager task to defer by passing `eager=False`.

```python
task_id = tasks.add_task(notify_user, user_id, message, eager=False)
```

The per-call value always takes precedence over the decorator setting.

## What happens step by step

When `eager=True` is resolved for a given `add_task()` call:

1. The task record is created in the store with status `PENDING`, same as normal.
2. `asyncio.create_task(wrapped_task())` is called immediately inside `add_task()`.
3. `add_task()` returns the `task_id`.
4. The route handler continues executing and eventually returns the response.
5. The task runs concurrently alongside response serialisation and any remaining route code.

## Sync tasks and eager dispatch

Eager dispatch works with sync functions too. The function is still wrapped in `asyncio.to_thread` (or submitted to the dedicated sync thread pool when `max_sync_threads` is configured), so it does not block the event loop. The coroutine wrapper is what gets dispatched via `asyncio.create_task`.

## Interaction with priority

When a task has a `priority` value set, it is always routed through the priority queue regardless of the `eager` setting. The priority worker dispatches via `asyncio.create_task` itself, so the practical outcome is similar, but `eager` is ignored when `priority` is present.

!!! warning
    Do not use eager dispatch for tasks that depend on request teardown completing first, such as tasks that close database sessions used by the same request handler. In those cases, keep `eager=False` (the default) so the task starts only after the response lifecycle is fully complete.

## Full example

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task(eager=True)
async def send_push(user_id: int, body: str) -> None:
    # Starts immediately when add_task() is called,
    # overlapping with the rest of the route handler.
    await push_service.send(user_id, body)


@task_manager.task()
async def log_event(event: str) -> None:
    # Deferred — starts after the response is sent.
    await analytics.record(event)


@app.post("/batch-notify")
async def batch_notify(
    user_ids: list[int],
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_ids = []
    for user_id in user_ids:
        # Each add_task() call here creates an independent asyncio task immediately.
        # All notifications run concurrently instead of sequentially.
        task_id = tasks.add_task(send_push, user_id, "You have a new message")
        task_ids.append(task_id)

    tasks.add_task(log_event, "batch_notify.queued")
    # log_event will start after the response is sent.

    return {"task_ids": task_ids}
```
