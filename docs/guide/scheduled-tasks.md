# Scheduled Tasks

This page covers how to run tasks automatically on a fixed interval or a cron schedule, and how scheduled tasks integrate with retries, the dashboard, and multi-instance deployments.

## Why use scheduled tasks

Some work does not belong to any individual HTTP request. You might need to flush a cache every hour, run a nightly cleanup, send a daily digest email, or check an external API every five minutes.

Scheduled tasks let you define that work alongside your regular tasks and have it run automatically, without managing a separate cron daemon or scheduler process.

Scheduled tasks use exactly the same execution path as manually enqueued tasks. Retries, logging, persistence, and the dashboard all work without any extra setup.

## Basic setup

Decorate a function with `@task_manager.schedule()` and pass either `every=` (an interval in seconds) or `cron=` (a cron expression).

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager, task_log

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.schedule(every=300)
async def health_check() -> None:
    task_log("Running health check")
    ...


@task_manager.schedule(cron="0 2 * * *")
def nightly_cleanup() -> None:
    task_log("Starting nightly cleanup")
    ...
```

`TaskAdmin` starts the scheduler automatically on app startup and stops it cleanly on shutdown. If you prefer not to use `TaskAdmin`, call `task_manager.init_app(app)` instead.

## Interval-based scheduling with `every=`

Pass the number of seconds between runs as a float. No extra dependencies are required.

```python
@task_manager.schedule(every=60)    # every minute
async def every_minute() -> None:
    ...

@task_manager.schedule(every=3600)  # every hour
async def every_hour() -> None:
    ...
```

The next run is scheduled relative to the current UTC time after each firing. Timezone settings do not affect interval-based tasks.

## Cron-based scheduling with `cron=`

Pass a standard five-field cron expression. This requires the `croniter` package, included in the `scheduler` extra.

```bash
pip install "fastapi-taskflow[scheduler]"
```

```python
@task_manager.schedule(cron="0 * * * *")    # top of every hour
async def hourly() -> None:
    ...

@task_manager.schedule(cron="0 2 * * *")    # 02:00 every day
async def nightly() -> None:
    ...

@task_manager.schedule(cron="*/5 * * * *")  # every 5 minutes
async def five_min() -> None:
    ...
```

!!! tip
    [crontab.guru](https://crontab.guru) is a quick way to verify that your cron expression does what you expect.

## Timezone support

Cron expressions are evaluated in UTC by default. Pass `timezone=` with an IANA timezone name to evaluate in a different timezone.

```python hl_lines="1"
@task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
async def morning_report() -> None:
    ...
```

This fires at 09:00 New York time, accounting for daylight saving automatically. The `timezone=` argument has no effect on `every=` schedules.

## Run immediately on startup

By default, the first firing waits for the first interval or cron slot to arrive. Set `run_on_startup=True` to fire once immediately when the app starts, then continue on the normal schedule.

```python
@task_manager.schedule(every=3600, run_on_startup=True)
async def warm_cache() -> None:
    ...
```

This is useful for tasks that populate a cache or sync state that your app needs immediately on boot.

## Retries on scheduled tasks

Scheduled tasks support the same retry options as regular tasks: `retries=`, `delay=`, and `backoff=`.

```python
@task_manager.schedule(every=300, retries=3, delay=10.0, backoff=2.0)
async def critical_sync() -> None:
    ...
```

On failure, the task retries up to three times with delays of 10 s, 20 s, and 40 s. Retries on the current run do not affect when the next scheduled firing happens. The schedule continues independently.

## Dashboard integration

Scheduled runs appear in the task list alongside manually enqueued tasks. You can tell them apart by the `source` field.

- Tasks fired by the scheduler show a **scheduled** badge next to the function name in the task list.
- The **Schedules** tab lists all registered scheduled tasks, their trigger configuration, and the next scheduled run time.

Manually triggered runs of the same function show `source=manual` instead.

## Triggering a scheduled task manually

A scheduled function is also registered in the task registry. You can enqueue it from a route at any time, independently of the automatic schedule.

```python
@task_manager.schedule(every=300)
async def health_check() -> None:
    ...


@app.post("/health/run-now")
async def trigger_now(tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(health_check)
    return {"task_id": task_id}
```

This is useful for admin endpoints, testing, or letting users request a refresh on demand.

## Multi-instance deployments

When multiple instances of your app share the same backend (for example, several containers pointing at the same Redis or SQLite database), you would normally expect each instance to fire the same scheduled task at the same time. fastapi-taskflow prevents this automatically.

Before each firing, the scheduler acquires a distributed lock. Only one instance acquires the lock per interval. All other instances see that the lock is taken and skip that firing silently.

```python
task_manager = TaskManager(snapshot_db="tasks.db")  # shared backend
```

No extra configuration is needed. The locking is handled automatically whenever a backend is configured.

!!! warning
    Without a shared backend, every instance fires independently. If that is not acceptable for your use case, make sure all instances point at the same backend.

## If a scheduled task takes longer than its interval

If a task run is still executing when the next scheduled firing is due, the scheduler will attempt to enqueue the next run as a new task. The two runs can overlap if the first has not finished.

!!! warning
    If you need to prevent overlapping runs, design your task to be idempotent or use an external lock within the task body. The scheduler does not check whether a previous run of the same task is still active before firing the next one.
