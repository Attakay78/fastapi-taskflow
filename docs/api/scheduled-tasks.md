# Scheduled Tasks API

## `@task_manager.schedule()`

Decorator that registers a function as a periodic task.

```python
@task_manager.schedule(
    every: float | None = None,
    cron: str | None = None,
    retries: int = 0,
    delay: float = 0.0,
    backoff: float = 1.0,
    name: str | None = None,
    run_on_startup: bool = False,
    timezone: str = "UTC",
)
def my_task() -> None:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `every` | `float` | `None` | Interval in seconds between runs. Mutually exclusive with `cron`. |
| `cron` | `str` | `None` | Five-field cron expression (e.g. `"0 * * * *"`). Requires `pip install "fastapi-taskflow[scheduler]"`. Mutually exclusive with `every`. |
| `retries` | `int` | `0` | Number of additional attempts after the first failure. |
| `delay` | `float` | `0.0` | Seconds to wait before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each retry. |
| `name` | `str` | function name | Override the display name in logs and the dashboard. |
| `run_on_startup` | `bool` | `False` | When `True`, fire on the first scheduler tick instead of waiting for the first interval or cron slot. |
| `timezone` | `str` | `"UTC"` | IANA timezone name for evaluating `cron` expressions (e.g. `"America/New_York"`). Ignored when `every` is used. |

Raises `ValueError` if neither or both of `every` and `cron` are provided.

The decorated function is also registered in the task registry, so it can be enqueued manually via `add_task()` in addition to running on schedule.

## `ScheduledEntry`

One registered periodic task. Created by `@task_manager.schedule()` and held by `PeriodicScheduler`.

```python
@dataclass
class ScheduledEntry:
    func: Callable
    config: TaskConfig
    every: float | None
    cron: str | None
    run_on_startup: bool
    timezone: str
    next_run: datetime
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `func` | `Callable` | The task function. |
| `config` | `TaskConfig` | Execution settings (retries, delay, backoff). |
| `every` | `float \| None` | Interval in seconds. `None` when `cron` is used. |
| `cron` | `str \| None` | Cron expression. `None` when `every` is used. |
| `run_on_startup` | `bool` | Whether to fire on the first tick after startup. |
| `timezone` | `str` | IANA timezone name used to evaluate the cron expression. `"UTC"` by default. |
| `next_run` | `datetime` | UTC time of the next scheduled execution. Updated after each firing. |

## `PeriodicScheduler`

Drives the scheduling loop. Held at `task_manager._periodic_scheduler`. Created automatically when `@task_manager.schedule()` is first used.

```python
class PeriodicScheduler:
    entries: list[ScheduledEntry]   # read-only snapshot

    def start(self) -> None: ...
    def stop(self) -> None: ...
```

`start()` and `stop()` are called by `TaskManager.startup()` and `TaskManager.shutdown()`. You do not need to call them directly.

## `TaskRecord.source`

Every task record carries a `source` field indicating how the task was created.

| Value | Meaning |
|-------|---------|
| `"manual"` | Enqueued via `add_task()` from a route or other code. Default. |
| `"scheduled"` | Fired by `PeriodicScheduler`. |

The `source` field is included in `TaskRecord.to_dict()` and returned by the REST API.

## Backend lock method

Backends used with scheduled tasks must support `acquire_schedule_lock`:

```python
async def acquire_schedule_lock(self, key: str, ttl: int) -> bool:
    ...
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | `str` | Lock identifier, typically `"schedule:{func_name}"`. |
| `ttl` | `int` | Lock lifetime in seconds. |

Returns `True` if the lock was acquired (should fire). Returns `False` if another instance holds it (should skip).

The default implementation in `SnapshotBackend` always returns `True`. `SqliteBackend` and `RedisBackend` implement proper distributed locking.

## Optional dependency

Cron expressions require `croniter`:

```bash
pip install "fastapi-taskflow[scheduler]"
```

`every=`-based schedules have no extra dependencies.
