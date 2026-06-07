# Named Queues

Named queues give you per-queue concurrency limits, backpressure caps, and task routing. Use them when different types of work need separate concurrency budgets or when you need to protect one workload from another.

## Why named queues

Without named queues, all tasks share the same global concurrency limit set by `max_concurrent_tasks`. A burst of slow report jobs can occupy every slot and delay fast, user-facing tasks even if those tasks are set to a higher priority.

Named queues solve this by giving each class of work its own concurrency pool and pending heap. A `reports` queue running at `concurrency=4` can never block the `email` queue running at `concurrency=30`, regardless of how many reports are queued.

## Defining queues

Pass a `queues` dict to `TaskManager`. Each key is the queue name; each value is a `QueueConfig`:

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.models import QueueConfig

task_manager = TaskManager(
    snapshot_db="tasks.db",
    queues={
        "email":   QueueConfig(concurrency=30, max_size=500),
        "reports": QueueConfig(concurrency=4,  max_size=50),
        "webhooks": QueueConfig(concurrency=10),
    },
)
```

A `"default"` queue is created automatically if you do not define one. Its `concurrency` defaults to `None` (unlimited) and `max_size` defaults to `None` (no backpressure limit).

### QueueConfig fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `concurrency` | `int \| None` | `None` | Maximum number of tasks from this queue running at once. `None` removes the limit. |
| `max_size` | `int \| None` | `None` | Maximum number of tasks allowed to wait in the queue heap. When the heap is full, `add_task()` raises `QueueFullError`. `None` removes the limit. |

## Routing tasks to a queue

### On the decorator

Add `queue=` to `@task_manager.task()` to route every call to that function into the named queue:

```python
@task_manager.task(retries=3, queue="email")
async def send_email(address: str) -> None:
    await mailer.send(address)

@task_manager.task(retries=1, queue="reports")
def generate_report(user_id: int) -> None:
    build_report(user_id)
```

### Per call

Pass `queue=` to `add_task()` to override the decorator setting for a single enqueue:

```python
@app.post("/export")
async def export(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_id = tasks.add_task(generate_report, user_id, queue="reports")
    return {"task_id": task_id}
```

The per-call value takes precedence over the decorator setting. If neither is set, the task goes to `"default"`.

## Backpressure and QueueFullError

When a queue defines `max_size`, calling `add_task()` after the heap reaches that limit raises `QueueFullError`. Catch it and return an appropriate HTTP response:

```python
from fastapi import HTTPException
from fastapi_taskflow import QueueFullError

@app.post("/report")
async def request_report(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    try:
        task_id = tasks.add_task(generate_report, user_id, queue="reports")
    except QueueFullError:
        raise HTTPException(status_code=429, detail="Report queue is full, try again later")
    return {"task_id": task_id}
```

`QueueFullError` is also importable from the top-level package:

```python
from fastapi_taskflow import QueueFullError
```

### REJECTED status

When a task is dispatched to a queue that is at capacity, the task record is created with status `REJECTED` before `QueueFullError` is raised. This means the rejection is visible in the dashboard and queryable via the API, giving you a record of every rejected enqueue attempt.

The `REJECTED` count is also tracked per queue and shown in the dashboard Queues tab.

## Priority within named queues

When the named queue system is active, the `priority` parameter on `add_task()` or `@task_manager.task()` controls ordering within each queue's heap. Higher priority tasks in the same queue are dispatched first:

```python
@task_manager.task(queue="email", priority=9)
async def send_otp(phone: str) -> None:
    await sms_gateway.send(phone)

@task_manager.task(queue="email", priority=1)
async def send_newsletter(address: str) -> None:
    await mailer.send_bulk(address)
```

Both tasks go into the `email` queue. OTP tasks at priority 9 drain before newsletter tasks at priority 1, while the `email` queue's `concurrency` limit applies to all tasks in it.

!!! note
    Priority only controls the order tasks are dispatched from the heap. It does not affect which queue the task enters. A high-priority task in `"reports"` does not jump ahead of tasks in `"email"`.

## Scheduling tasks into queues

Add `queue=` to `@task_manager.schedule()` to route each periodic firing into a named queue:

```python
@task_manager.schedule(every=3600, queue="reports")
def hourly_summary() -> None:
    build_summary()
```

Each firing is subject to the target queue's concurrency limit and backpressure. If the queue is full at fire time, the scheduled task is rejected and a new attempt will be made at the next interval.

## Queue stats

`GET /tasks/queues` returns the current state of all named queues:

```json
[
  {
    "name": "email",
    "concurrency": 30,
    "max_size": 500,
    "pending": 12,
    "running": 8,
    "finished": 4201,
    "rejected": 3
  },
  {
    "name": "reports",
    "concurrency": 4,
    "max_size": 50,
    "pending": 2,
    "running": 4,
    "finished": 87,
    "rejected": 0
  }
]
```

| Field | Description |
|-------|-------------|
| `pending` | Tasks waiting in the heap, not yet dispatched. |
| `running` | Tasks currently executing. |
| `finished` | Terminal tasks (success, failed, interrupted, cancelled). |
| `rejected` | Tasks rejected due to the queue being full. |

## Live config updates

Change a queue's concurrency or max_size at runtime without restarting the server:

```
PATCH /tasks/queues/{queue_name}
Content-Type: application/json

{"concurrency": 8, "max_size": 100}
```

The change takes effect immediately for new tasks entering the queue. In-flight tasks that already acquired a concurrency slot are not affected. Configuration changes are persisted to the backend and restored on the next startup.

## Dashboard

The Queues tab in the dashboard shows a live card for each named queue with its concurrency limit, max size, and current counts for pending, running, finished, and rejected tasks. You can edit concurrency and max_size directly from the card.

The task table includes a Queue column. Use the Queue filter dropdown to narrow the table to a single queue.

## Global max_size shorthand

If you only need backpressure on the default queue without defining any other queues, pass `max_size` directly:

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_size=1000,
)
```

This activates the named queue system with a single `"default"` queue capped at 1000 pending tasks.

## Interaction with concurrency controls

Named queues and the global `max_concurrent_tasks` semaphore are independent. If both are set, a task must:

1. Be popped from the named queue heap (respecting per-queue `concurrency`)
2. Acquire the global semaphore (respecting `max_concurrent_tasks`)

For most applications, you should use one or the other, not both. Named queues give finer per-queue control. The global semaphore is a blunt cap across all queues.

## Interaction with priority queues

The legacy priority queue (enabled by setting `priority=` on the decorator without any `queues=` argument) still works when the named queue system is not active.

When the named queue system is active (any `queues=` or `max_size=` argument is passed to `TaskManager`), `priority=` controls task ordering within the named queue heap rather than routing through a separate priority queue. There is no separate priority worker in named queue mode.

## Full example

```python
from fastapi import Depends, FastAPI, HTTPException
from fastapi_taskflow import ManagedBackgroundTasks, QueueFullError, TaskAdmin, TaskManager
from fastapi_taskflow.models import QueueConfig

task_manager = TaskManager(
    snapshot_db="tasks.db",
    queues={
        "email":   QueueConfig(concurrency=30, max_size=500),
        "reports": QueueConfig(concurrency=4,  max_size=50),
    },
)
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task(retries=3, queue="email", priority=9)
async def send_otp(phone: str) -> None:
    await sms_gateway.send(phone)


@task_manager.task(retries=1, queue="reports")
def generate_report(user_id: int) -> None:
    build_report(user_id)


@app.post("/otp")
async def otp(
    phone: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    task_id = tasks.add_task(send_otp, phone)
    return {"task_id": task_id}


@app.post("/report")
async def request_report(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    try:
        task_id = tasks.add_task(generate_report, user_id)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="Report queue is full")
    return {"task_id": task_id}
```
