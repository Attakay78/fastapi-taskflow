# Scheduled Tasks

Periodic tasks run automatically at a fixed interval or on a cron expression. They use the same execution path as manually enqueued tasks, so retries, logging, persistence, and the dashboard all work without any extra setup.

## Basic usage

Use `@task_manager.schedule()` with either `every=` (seconds) or `cron=`.

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
    task_log("Starting nightly cleanup")
    ...
```

`TaskAdmin` starts the scheduler on app startup and stops it on shutdown automatically. Without `TaskAdmin`, use `init_app()`:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, task_log

task_manager = TaskManager()
app = FastAPI()

task_manager.init_app(app)

@task_manager.schedule(every=60)
async def ping() -> None:
    task_log("ping")
```

## Triggers

### Interval (`every=`)

Pass the number of seconds between runs as a float. No extra dependencies required.

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(every=60)      # every minute
async def every_minute() -> None: ...

@task_manager.schedule(every=3600)    # every hour
async def every_hour() -> None: ...
```

### Cron expression (`cron=`)

Pass a standard five-field cron expression. Requires `croniter`:

```bash
pip install "fastapi-taskflow[scheduler]"
```

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(cron="0 * * * *")     # top of every hour
async def hourly() -> None: ...

@task_manager.schedule(cron="0 2 * * *")     # 02:00 every day
async def nightly() -> None: ...

@task_manager.schedule(cron="*/5 * * * *")   # every 5 minutes
async def five_min() -> None: ...
```

## Retries

Scheduled tasks support the same retry options as regular tasks:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(every=300, retries=3, delay=10.0, backoff=2.0)
async def critical_sync() -> None:
    ...
```

On failure, the task retries up to 3 times with delays of 10s, 20s, and 40s. The next scheduled firing is not affected by retries on the current run.

## run_on_startup

By default, the first firing waits for the first interval or cron slot. Set `run_on_startup=True` to fire immediately on the first scheduler tick:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(every=3600, run_on_startup=True)
async def warm_cache() -> None:
    ...
```

## Triggering manually

A scheduled function is also registered in the task registry. You can enqueue it manually from a route alongside the automatic schedule:

```python
from fastapi import Depends, FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(every=300)
async def health_check() -> None:
    ...

@app.post("/health/run-now")
async def trigger_now(tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(health_check)
    return {"task_id": task_id}
```

A manually triggered run produces a task record with `source="manual"`. A scheduled run produces `source="scheduled"`. Both are visible in the dashboard, with scheduled runs shown with a badge.

## Dashboard

Scheduled runs appear in the task list like any other task. The `source` field identifies them:

- Tasks fired by the scheduler show a **scheduled** badge next to the function name.
- The **Schedules** tab lists all registered entries, their trigger, and the next scheduled run time.

## Multi-instance deployments

When multiple instances share the same backend (SQLite same-host or Redis), the scheduler uses a distributed lock before each firing. Only one instance fires each scheduled entry per interval. Instances that do not acquire the lock skip that firing silently.

This is handled automatically when a backend is configured:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(every=300)
async def sync_data() -> None:
    ...
```

Without a backend, all instances fire independently. If that is not acceptable, configure a shared backend.

## Timezone support

By default, cron expressions are evaluated in UTC. Pass a `timezone=` argument to evaluate in any IANA timezone:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()
TaskAdmin(app, task_manager)

@task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
async def morning_report() -> None:
    ...
```

`every=` schedules are not affected by `timezone=`. Interval arithmetic is always relative to the current UTC time.

