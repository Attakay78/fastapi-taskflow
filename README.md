<p align="center">
  <img src="https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/banner.svg" alt="fastapi-taskflow" width="380" />
</p>
<p align="center">
  <strong>Turn FastAPI BackgroundTasks into a production-ready task system.</strong><br/>
  Retries, control, and visibility without workers or brokers.
</p>

---

FastAPI's `BackgroundTasks` handles simple fire-and-forget work well. But in real applications you quickly hit the same gaps: tasks fail silently, you have no visibility into what ran, and nothing survives a restart.

fastapi-taskflow is a thin layer on top of what you already have. It does not compete with Celery, ARQ, Taskiq, or Dramatiq. It is built for teams who are already using FastAPI's native background tasks and want retries, status tracking, and a live dashboard without adding infrastructure.

![fastapi-taskflow dashboard](https://raw.githubusercontent.com/Attakay78/fastapi-taskflow/main/docs/assets/images/dashboard.png)

## Features

- Automatic retries with configurable delay and exponential backoff
- Task IDs and full lifecycle tracking: `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`
- Live admin dashboard over SSE at `/tasks/dashboard`
- SQLite persistence out of the box, Redis as an optional extra
- Pending task requeue: unfinished tasks at shutdown are re-dispatched on startup
- Zero-migration injection: keep your existing `BackgroundTasks` annotations
- Both sync and async task functions supported

## Installation

```bash
pip install fastapi-taskflow
```

With Redis backend:

```bash
pip install "fastapi-taskflow[redis]"
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

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks |
| `GET` | `/tasks/{task_id}` | Single task detail |
| `GET` | `/tasks/metrics` | Aggregated stats |
| `GET` | `/tasks/dashboard` | Live HTML dashboard |

## What this is not

This is not a distributed task queue. If you need tasks to run on separate workers, survive across multiple app instances, or integrate with a message broker, use Celery, ARQ, Taskiq, or a similar tool. This library is for in-process background work that needs more control than the bare `BackgroundTasks` provides.

## License

MIT
