<p align="center">
  <img src="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/banner.svg" alt="fastapi-taskflow" width="380" />
</p>
<p align="center">
  <strong>Turn FastAPI BackgroundTasks into a production-ready task system.</strong><br/>
  Retries, control, resiliency and visibility without workers or brokers.
</p>

---

FastAPI's `BackgroundTasks` handles simple fire-and-forget work well. But in real applications you quickly hit the same gaps: tasks fail silently, you have no visibility into what ran, and nothing survives a restart.

fastapi-taskflow is a thin layer on top of what you already have. It does not compete with Celery, ARQ, Taskiq, or Dramatiq. It is built for teams who are already using FastAPI's native background tasks and want retries, resilience, status tracking, and a live dashboard without adding infrastructure.

<p align="center">
  <a href="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/dashboard.png" target="_blank">
    <img src="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/dashboard.png" alt="Task dashboard overview" width="100%" />
  </a>
</p>
<p align="center">
  <a href="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/logs.png" target="_blank">
    <img src="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/logs.png" alt="Task logs panel" width="49%" />
  </a>
  <a href="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/error.png" target="_blank">
    <img src="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/error.png" alt="Task error and stack trace panel" width="49%" />
  </a>
</p>

## Features

- Automatic retries with configurable delay and exponential backoff
- Task IDs and full lifecycle tracking: `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`
- Live admin dashboard over SSE at `/tasks/dashboard`
- SQLite persistence out of the box, Redis as an optional extra
- Pending task requeue: unfinished tasks at shutdown are re-dispatched on startup
- `requeue_on_interrupt`: opt-in requeue for idempotent tasks interrupted mid-execution
- Idempotency keys: prevent duplicate execution of the same logical operation
- Multi-instance support: atomic requeue claiming, shared task history across instances
- `task_log(message, level=, **extra)`: structured log entries with level filtering and arbitrary extra fields
- `get_task_context()`: access task metadata (task_id, attempt, tags) from any code path inside a running task
- Tags: attach key/value labels at enqueue time, forwarded to every log and lifecycle event
- Pluggable observers: `FileLogger`, `StdoutLogger`, `InMemoryLogger`, and custom `TaskObserver` implementations
- Argument encryption: Fernet-based at-rest encryption for task args and kwargs
- Trace context propagation: OpenTelemetry spans flow from the request into background execution (Python 3.11+)
- Concurrency controls: opt-in semaphore for async tasks and dedicated thread pool for sync tasks
- Priority queues: `priority=` on `@task_manager.task()` or `add_task()` — higher-priority tasks run first, equal-priority tasks are FIFO
- Eager dispatch: `eager=True` starts a task immediately via `asyncio.create_task` before the HTTP response is sent
- Scheduled tasks: `@task_manager.schedule(every=)` and `cron=` with distributed lock for multi-instance
- Zero-migration injection: keep your existing `BackgroundTasks` annotations
- Both sync and async task functions supported

## Installation

```bash
pip install fastapi-taskflow
```

With all optional dependencies:

```bash
pip install "fastapi-taskflow[all]"
```

Or install only what you need:

| Extra | Installs | Required for |
|-------|----------|--------------|
| `redis` | `redis[asyncio]` | Redis persistence backend |
| `scheduler` | `croniter` | Cron-based scheduled tasks |
| `encryption` | `cryptography` | Argument encryption at rest |

```bash
pip install "fastapi-taskflow[redis]"
pip install "fastapi-taskflow[scheduler]"
pip install "fastapi-taskflow[encryption]"
```

## Quick start

```python
from fastapi import BackgroundTasks, FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db", snapshot_interval=30.0)
app = FastAPI()

# auto_install=True patches FastAPI's BackgroundTasks injection so existing
# route signatures work without any changes.
TaskAdmin(app, task_manager, auto_install=True)


@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_email(address: str) -> None:
    ...


@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_email, address=email)
    return {"task_id": task_id}
```

```bash
uvicorn examples.basic_app:app --reload

curl -X POST "http://localhost:8000/signup?email=user@example.com"
curl "http://localhost:8000/tasks"
curl "http://localhost:8000/tasks/metrics"
open "http://localhost:8000/tasks/dashboard"
```

## Injection patterns

Three ways to get a `ManagedBackgroundTasks` instance into your routes:

```python
# Pattern 1: keep the native annotation (requires auto_install=True)
def route(background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(my_func, arg)

# Pattern 2: explicit managed type (also requires auto_install=True)
from fastapi_taskflow import ManagedBackgroundTasks

def route(background_tasks: ManagedBackgroundTasks):
    task_id = background_tasks.add_task(my_func, arg)

# Pattern 3: explicit Depends — no install() required
from fastapi import Depends

def route(tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(my_func, arg)
```

## Decorator options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `retries` | `int` | `0` | Additional attempts after the first failure |
| `delay` | `float` | `0.0` | Seconds before the first retry |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each retry |
| `persist` | `bool` | `False` | Save this task for requeue on restart |
| `name` | `str` | function name | Override the name shown in the dashboard |
| `requeue_on_interrupt` | `bool` | `False` | Requeue this task if it was mid-execution at shutdown. Only set for idempotent tasks. |
| `eager` | `bool` | `False` | Start the task via `asyncio.create_task` immediately when `add_task()` is called, before the response is sent. Per-call `eager` on `add_task()` overrides this. |
| `priority` | `int \| None` | `None` | Route through the priority queue. Higher integers run first. Conventional range 1 (lowest) to 10 (highest). Per-call `priority` on `add_task()` overrides this. |

## Idempotency keys

Pass an `idempotency_key` to `add_task()` to prevent the same logical operation from running twice. If a non-failed task with the same key already exists, the original `task_id` is returned and the task is not enqueued again.

```python
task_id = tasks.add_task(send_notification, order_id, idempotency_key="order-42-notified")
```

Useful for handling retried HTTP requests, duplicate webhook deliveries, or double-clicks.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks |
| `GET` | `/tasks/{task_id}` | Single task detail |
| `GET` | `/tasks/metrics` | Aggregated stats |
| `GET` | `/tasks/dashboard` | Live HTML dashboard |
| `POST` | `/tasks/{task_id}/retry` | Retry a failed or interrupted task |

## Multi-instance deployments

fastapi-taskflow supports running multiple instances behind a load balancer when a shared backend is configured.

**Same host, multiple processes** -- use SQLite. All instances share the same file. Requeue claiming is atomic so only one instance picks up each task on restart.

**Multiple hosts** -- use Redis. All instances share the same Redis instance. Idempotency keys, requeue claiming, and completed task history all work across hosts.

```python
from fastapi_taskflow.backends import RedisBackend

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://localhost:6379/0"),
    requeue_pending=True,
)
```

**Dashboard in multi-instance deployments** -- the dashboard shows live tasks for the instance it is connected to. Completed tasks from all instances are visible via the shared backend (with a short cache window). For accurate live task visibility, route dashboard traffic to a single instance using sticky sessions at the load balancer. See the [multi-instance guide](docs/guide/multi-instance.md) for examples.

**Known caveats:**
- Live `PENDING` and `RUNNING` tasks from other instances are not visible in real time. Each instance only holds its own in-memory state.
- SQLite multi-instance only works when all processes share the same file path on the same host. It does not work across separate machines.
- Tasks in `RUNNING` state at the time of a hard crash (SIGKILL, OOM) cannot be recovered. Only clean shutdowns trigger the pending store write.

## Structured task logging

`task_log()` accepts an optional `level=` and arbitrary `**extra` keyword fields. Extras are forwarded to observers as structured fields rather than embedded in the message string.

```python
from fastapi_taskflow import get_task_context, task_log

@task_manager.task(retries=3)
def process_order(order_id: int) -> None:
    ctx = get_task_context()
    task_log("Processing order", order_id=order_id, attempt=ctx.attempt if ctx else 0)
    task_log("Payment gateway error", level="warning", order_id=order_id, code=503)
```

`get_task_context()` returns a `TaskContext` with `task_id`, `func_name`, `attempt`, and `tags` from any code path inside a running task.

## Observability

Pass one or more observers to `loggers=` to receive structured `LogEvent` and `LifecycleEvent` objects for every `task_log()` call and status transition:

```python
from fastapi_taskflow import FileLogger, StdoutLogger, TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    loggers=[
        FileLogger("tasks.log", log_lifecycle=True),
        StdoutLogger(log_lifecycle=True, min_level="warning"),
    ],
)
```

Built-in observers:

| Observer | Description |
|----------|-------------|
| `FileLogger` | Rotating plain text file. Works with `tail -f`, `grep`, and log shippers. |
| `StdoutLogger` | Prints to stdout. Suitable for containers with a log agent. |
| `InMemoryLogger` | Captures events in memory for test assertions. |

The `log_file` shorthand on `TaskManager` remains fully supported and creates a `FileLogger` internally.

## Tags

Attach key/value labels to a task at enqueue time. Tags flow through to every log and lifecycle event.

```python
task_id = tasks.add_task(
    send_email,
    address=email,
    tags={"user_id": str(user_id), "source": "signup"},
)
```

## Argument encryption

When tasks carry sensitive data, encrypt args and kwargs at rest with Fernet:

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    encrypt_args_key=os.environ["TASK_ENCRYPTION_KEY"],
)
```

Generate a key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Args and kwargs are encrypted at `add_task()` time and decrypted only inside the executor. They are never stored in plain text in the task store, the database, or any log file. Requires `pip install "fastapi-taskflow[encryption]"`.

## Concurrency controls

By default, tasks share the event loop and thread pool with request handlers. Under burst task load this can increase request latency. Both controls are opt-in and do not change existing behaviour when not set.

**`max_concurrent_tasks`** — caps how many async tasks hold event loop time simultaneously via an `asyncio.Semaphore`. Tasks waiting for a slot are parked without blocking the loop.

**`max_sync_threads`** — runs sync task functions in a dedicated `ThreadPoolExecutor`, isolated from the default pool used by sync request handlers.

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    max_concurrent_tasks=10,
    max_sync_threads=8,
)
```

Both default to `None`. When not set, execution is identical to previous versions.

## Priority queues

Pass `priority=` to control execution order. Higher-priority tasks run before lower-priority ones regardless of arrival order. Equal-priority tasks execute in arrival (FIFO) order.

```python
@task_manager.task(retries=2, priority=9)
async def send_otp(phone: str) -> None:
    ...

@task_manager.task(priority=1)
def generate_report(user_id: int) -> None:
    ...

@app.post("/otp")
async def otp(phone: str, tasks=Depends(task_manager.get_tasks)):
    # Runs before the report even if the report was queued first.
    task_id = tasks.add_task(send_otp, phone)
    return {"task_id": task_id}
```

Set `priority=` at the decorator level as a default, then override it per call:

```python
task_id = tasks.add_task(process_item, item_id, priority=10)  # rush job
task_id = tasks.add_task(process_item, item_id, priority=1)   # defer
task_id = tasks.add_task(process_item, item_id)               # use decorator default
```

Tasks with no priority route through Starlette's normal background task list unchanged.

## Eager dispatch

Set `eager=True` to start a task via `asyncio.create_task` the moment `add_task()` is called, before FastAPI sends the response. The task is tracked and retried exactly like a normal task.

```python
@task_manager.task(eager=True)
async def notify_user(user_id: int, body: str) -> None:
    await push_service.send(user_id, body)

@app.post("/action")
async def action(user_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(notify_user, user_id, "Your request is processing")
    # notify_user is already running here, before the response goes out.
    return {"task_id": task_id}
```

Override per call with `tasks.add_task(func, arg, eager=True)`. When `priority=` is also set, the priority queue governs dispatch and `eager` is ignored.

## Scheduled tasks

Run functions automatically at a fixed interval or on a cron expression. Scheduled tasks go through the same execution path as manually enqueued tasks, so retries, logging, persistence, and the dashboard all work without any extra setup.

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager, task_log

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.schedule(every=300, retries=1)
async def health_check() -> None:
    task_log("Running health check")
    ...


@task_manager.schedule(cron="0 2 * * *")
def nightly_cleanup() -> None:
    task_log("Starting cleanup")
    ...


@task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
async def morning_report() -> None:
    ...
```

Cron expressions require `pip install "fastapi-taskflow[scheduler]"`. Interval-based schedules have no extra dependencies. Cron expressions default to UTC. Pass any IANA timezone name with `timezone=` to evaluate in local time.

Scheduled tasks also appear in the task registry, so they can be enqueued manually from a route alongside their automatic schedule. In multi-instance deployments, a distributed lock ensures only one instance fires each entry per interval.

## Custom dashboard title

Replace the "fastapi-taskflow" label in the dashboard header and login page with your own app name:

```python
TaskAdmin(app, task_manager, title="My App")
```

## File logging

In addition to the observer system, a plain text log file can be configured directly on `TaskManager`:

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    log_file="tasks.log",
    log_lifecycle=True,
)
```

Each line has the format `[task_id] [func_name] 2026-01-01T12:00:00 message`. For multi-process or multi-host deployments see the [file logging guide](docs/guide/file-logging.md).

## What this is not

fastapi-taskflow does not compete with Celery, ARQ, Taskiq, or Dramatiq. Those tools are built for distributed workers, message brokers, and high-throughput task routing. This library is for teams using FastAPI's native `BackgroundTasks` who want retries, visibility, and resilience without adding worker infrastructure.

## License

MIT
