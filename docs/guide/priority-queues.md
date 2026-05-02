# Priority Queues

This page shows you how to give time-sensitive tasks a head start so that a slow batch job never delays an OTP or payment confirmation.

## Why priority matters

Without priority, tasks run in arrival order. That is usually fine. It becomes a problem when a long batch job happens to be enqueued just before a user-facing task. The user waits for the batch job to start before their OTP goes out, even though the OTP takes a fraction of a second and the batch job takes minutes.

Priority queues fix this by letting you express importance directly on the task. Higher-priority tasks always start before lower-priority ones, regardless of the order they arrived.

## How it works

Tasks with a `priority` value skip Starlette's background task list and enter a dedicated `asyncio.PriorityQueue` instead. A single worker coroutine drains that queue and dispatches each task via `asyncio.create_task`. Higher integers run first. Tasks with the same priority number run in arrival order (FIFO).

The priority worker starts with `TaskManager.startup()` and shuts down cleanly with `TaskManager.shutdown()`. Any tasks still in the queue at shutdown remain as `PENDING` in the store and are picked up by the existing requeue mechanism.

!!! note
    Tasks without any `priority` value are not affected. They continue to run through the normal Starlette background task path, exactly as before.

## Setting a priority on the decorator

Add `priority=` to `@task_manager.task()` to give every call to that function a fixed default priority.

```python hl_lines="9 14"
from fastapi import Depends, FastAPI
from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task(retries=2, priority=9)
async def send_otp(phone: str) -> None:
    await sms_gateway.send(phone)


@task_manager.task(priority=1)
def generate_report(user_id: int) -> None:
    build_report(user_id)
```

Every `add_task(send_otp, ...)` call routes through the priority queue at priority 9. Every `add_task(generate_report, ...)` call routes at priority 1.

## Overriding priority per call

Pass `priority=` directly to `add_task()` to override the decorator default for a single enqueue.

```python
@app.post("/process")
async def process(
    item_id: int,
    rush: bool = False,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_id = tasks.add_task(process_item, item_id, priority=10 if rush else None)
    return {"task_id": task_id}
```

The per-call value takes precedence over the decorator setting. Passing `priority=None` falls back to whatever the decorator specifies. If both are `None`, the task uses the normal Starlette dispatch path with no priority.

## Choosing priority values

Any integer works. The conventional range is 1 to 10, with 5 as a sensible midpoint.

| Priority | When to use it |
|----------|----------------|
| 9-10 | User-facing, time-sensitive tasks: OTP codes, payment confirmations |
| 6-8 | Important background work: webhook delivery, transactional email |
| 4-5 | Routine background work: data sync, cache warming |
| 1-3 | Deferrable work: report generation, bulk exports, batch jobs |

!!! tip
    You do not need to use the full range. Picking three tiers (for example 9, 5, and 1) covers most applications clearly and makes it easy to reason about which tasks run first.

## Mixing priority and normal tasks

You can apply priority to some tasks while leaving others as normal background tasks. Both dispatch paths run concurrently and independently.

```python
@task_manager.task(priority=8)
async def send_confirmation(order_id: int) -> None:
    await email_service.send_confirmation(order_id)


@task_manager.task()
async def update_analytics(order_id: int) -> None:
    await analytics.record(order_id)


@app.post("/order")
async def place_order(
    order_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    conf_id = tasks.add_task(send_confirmation, order_id)    # priority queue
    analytics_id = tasks.add_task(update_analytics, order_id)  # Starlette list
    return {"confirmation_task": conf_id, "analytics_task": analytics_id}
```

There is no ordering guarantee between the two dispatch paths. Priority only controls ordering within the priority queue itself.

## Interaction with eager dispatch

When `priority` is set, the task always routes through the priority queue. The `eager` flag is ignored. The priority worker dispatches via `asyncio.create_task`, so tasks start quickly without needing to wait for the response to be sent.

## Interaction with concurrency controls

The priority queue controls *when* a task is dispatched, not how many run at once. Once dispatched, each task enters the normal execution path and is still subject to the `max_concurrent_tasks` semaphore if one is configured.

A high-priority task dispatched while the semaphore is full will wait for a slot, the same as any other task.

!!! info
    Think of priority and concurrency controls as two separate layers: priority decides the order tasks leave the queue, and `max_concurrent_tasks` decides how many can be executing at any given moment.

## Dashboard

Priority appears in the task table as a color-coded badge next to the function name.

- **Teal** for high priority (8-10)
- **Amber** for medium priority (5-7)
- **Gray** for low priority (1-4)

The Priority column is sortable. Tasks without a priority value show a dash.

## Full example

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task(retries=3, priority=9)
async def send_otp(phone: str) -> None:
    await sms_gateway.send(phone)


@task_manager.task(priority=5)
async def send_receipt(order_id: int) -> None:
    await email_service.send_receipt(order_id)


@task_manager.task(priority=1)
def build_monthly_report(month: str) -> None:
    report_builder.run(month)


@app.post("/otp")
async def otp(
    phone: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_id = tasks.add_task(send_otp, phone)
    return {"task_id": task_id}


@app.post("/checkout")
async def checkout(
    order_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    receipt_id = tasks.add_task(send_receipt, order_id)
    report_id = tasks.add_task(build_monthly_report, "2025-04")
    # If both tasks are queued at once, the receipt (priority 5) runs
    # before the report (priority 1).
    return {"receipt_task": receipt_id, "report_task": report_id}
```
