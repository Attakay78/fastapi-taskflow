# Priority Queues

By default, tasks run in the order they are enqueued. Priority queues let you control execution order: higher-priority tasks run before lower-priority ones regardless of arrival order.

## How it works

Tasks with a `priority` value bypass Starlette's background task list and enter a dedicated `asyncio.PriorityQueue`. A single worker coroutine drains the queue and dispatches each task via `asyncio.create_task`. The actual concurrency is then governed by the existing `max_concurrent_tasks` semaphore inside the executor.

The priority queue is started by `TaskManager.startup()` and stopped cleanly by `TaskManager.shutdown()`. Any tasks still in the queue at shutdown remain as `PENDING` in the store and are handled by the existing interrupted-task and requeue mechanisms.

## Setting priority

### Decorator-level default

Set `priority` on `@task_manager.task()` to give all invocations of a function a fixed default:

```python
@task_manager.task(retries=2, priority=9)
async def send_otp(phone: str) -> None:
    await sms_gateway.send(phone)

@task_manager.task(priority=1)
def generate_report(user_id: int) -> None:
    build_report(user_id)
```

Every `add_task(send_otp, ...)` call routes through the priority queue at priority 9. Every `add_task(generate_report, ...)` call routes at priority 1.

### Per-call override

Override the decorator default for a single enqueue:

```python
@app.post("/process")
def process(
    item_id: int,
    rush: bool = False,
    tasks=Depends(task_manager.get_tasks),
):
    task_id = tasks.add_task(process_item, item_id, priority=10 if rush else None)
    return {"task_id": task_id}
```

Per-call `priority` takes precedence over the decorator setting. Passing `priority=None` falls back to the decorator default. If both are `None`, the task routes through the normal Starlette mechanism with no priority.

## Priority values

Any integer is accepted. Higher integers run first. The conventional range is 1 (lowest) to 10 (highest), with 5 as a midpoint:

| Priority | Intended use |
|----------|-------------|
| 9-10 | User-facing, time-sensitive (OTP, payment confirmation) |
| 6-8 | Important background work (webhook delivery, email) |
| 4-5 | Routine background work (data sync, cache warm-up) |
| 1-3 | Deferrable work (report generation, batch jobs) |

Tasks with the same priority execute in arrival order (FIFO).

## Dashboard

Priority is shown in the task table as a color-coded badge:

- **Teal** — high priority (8-10)
- **Amber** — medium priority (5-7)
- **Gray** — low priority (1-4)

The Priority column is sortable. Tasks without a priority value show a dash.

## Normal tasks and priority tasks together

Setting `priority` on only some tasks is fine. Normal tasks (no priority) continue to run via Starlette's background task list after the response is sent. Priority tasks run through the dedicated queue concurrently. There is no ordering relationship between the two dispatch paths.

```python
@task_manager.task(priority=8)
async def send_confirmation(order_id: int) -> None:
    ...

@task_manager.task()
async def update_analytics(order_id: int) -> None:
    ...

@app.post("/order")
def place_order(order_id: int, tasks=Depends(task_manager.get_tasks)):
    conf_id      = tasks.add_task(send_confirmation, order_id)   # priority queue
    analytics_id = tasks.add_task(update_analytics, order_id)    # Starlette list
    return {"confirmation_task": conf_id, "analytics_task": analytics_id}
```

## Interaction with eager dispatch

When `priority` is set, the task is always routed through the priority queue. The `eager` setting is ignored. The priority worker dispatches tasks via `asyncio.create_task`, so they start quickly without waiting for a request to complete.

## Interaction with concurrency controls

The priority queue controls dispatch order, not concurrency. Once dispatched, each task enters the same `execute_task` path and is subject to the `max_concurrent_tasks` semaphore if one is configured. A high-priority task dispatched while the semaphore is full will wait for a slot, just like any other task.

## Example

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager()
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
    report_id  = tasks.add_task(build_monthly_report, "2025-04")
    # If all three tasks are queued simultaneously, the OTP runs first,
    # then the receipt, then the report.
    return {"receipt_task": receipt_id, "report_task": report_id}
```
