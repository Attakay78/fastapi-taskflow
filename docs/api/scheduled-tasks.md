# Scheduled Tasks API

This page documents the data types involved in periodic task scheduling. For the decorator that creates scheduled tasks, see [`schedule()` in the TaskManager API](task-manager.md#schedule).

> **Guide:** [Scheduled Tasks](../guide/scheduled-tasks.md) covers interval vs. cron scheduling, timezone configuration, and multi-instance deployments.

---

## `@task_manager.schedule()`

Decorator that registers a function as a periodic background task. Exactly one of `every` or `cron` must be provided.

```python
@task_manager.schedule(
    every=300,
    retries=1,
)
async def cleanup_expired_sessions() -> None:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `every` | `float \| None` | `None` | Interval in seconds between runs (for example `300` for every 5 minutes). Mutually exclusive with `cron`. |
| `cron` | `str \| None` | `None` | Five-field cron expression (for example `"0 * * * *"` for every hour). Requires `pip install "fastapi-taskflow[scheduler]"`. Mutually exclusive with `every`. |
| `retries` | `int` | `0` | Additional attempts after the first failure. |
| `delay` | `float` | `0.0` | Seconds to wait before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each retry. |
| `name` | `str \| None` | function name | Override the display name in logs and the dashboard. |
| `run_on_startup` | `bool` | `False` | When `True`, fire on the first scheduler tick immediately after startup, rather than waiting for the first interval or cron slot. |
| `timezone` | `str` | `"UTC"` | IANA timezone name for evaluating `cron` expressions (for example `"America/New_York"`). Ignored when `every` is used. |

Raises `ValueError` if neither or both of `every` and `cron` are provided. Raises `ImportError` if `cron` is used and `croniter` is not installed.

The decorated function is also registered in the task registry, so it can be enqueued manually via `add_task()` in addition to running on schedule.

**Examples:**

```python
@task_manager.schedule(every=300, retries=1)
async def cleanup_expired_sessions() -> None:
    ...

@task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
async def morning_report() -> None:
    ...
```

---

## `ScheduledEntry`

`ScheduledEntry` represents one registered periodic task. Created by `@task_manager.schedule()` and stored in `PeriodicScheduler`. You never instantiate this directly.

```python
from fastapi_taskflow.periodic import ScheduledEntry
```

```python
@dataclass
class ScheduledEntry:
    func:          Callable
    config:        TaskConfig
    every:         float | None
    cron:          str | None
    run_on_startup: bool
    timezone:      str
    next_run:      datetime
```

| Field | Type | Description |
|-------|------|-------------|
| `func` | `Callable` | The task function, already registered in the task registry. |
| `config` | `TaskConfig` | Execution settings (retries, delay, backoff). |
| `every` | `float \| None` | Interval in seconds between runs. `None` when `cron` is used. |
| `cron` | `str \| None` | Five-field cron expression. `None` when `every` is used. |
| `run_on_startup` | `bool` | Whether to fire on the first tick immediately after startup. |
| `timezone` | `str` | IANA timezone name used when evaluating the cron expression. `"UTC"` by default. |
| `next_run` | `datetime` | UTC time of the next scheduled execution. Updated after each firing. |

---

## `TaskRecord.source`

Every task record carries a `source` field indicating how the task was created.

| Value | Meaning |
|-------|---------|
| `"manual"` | Enqueued via `add_task()` from a route or other code. This is the default. |
| `"scheduled"` | Fired automatically by `PeriodicScheduler`. |

The `source` field is included in `TaskRecord.to_dict()` and returned in all REST API responses.

---

## Backend schedule locking

Backends used with scheduled tasks can implement `acquire_schedule_lock` to prevent duplicate firings in multi-instance deployments. The default implementation in `SnapshotBackend` always returns `True` (no locking). `SqliteBackend` and `RedisBackend` both provide proper distributed locking.

```python
async def acquire_schedule_lock(self, key: str, ttl: int) -> bool
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | `str` | Lock identifier. The scheduler uses `"schedule:{func_name}"`. |
| `ttl` | `int` | Lock lifetime in seconds. The lock releases automatically after this period, so a crashed instance does not block future firings. |

Returns `True` if the lock was acquired (this instance should fire). Returns `False` if another instance already holds the lock (this instance should skip).

---

## Optional dependency

Cron expressions require `croniter`:

```bash
pip install "fastapi-taskflow[scheduler]"
```

`every`-based schedules have no extra dependencies.
