# Scheduled Tasks API

This page documents the data types involved in task scheduling, both recurring and one-off. For the decorator that creates recurring scheduled tasks, see [`schedule()` in the TaskManager API](task-manager.md#schedule).

> **Guide:** [Scheduled Tasks](../guide/scheduled-tasks.md) covers interval vs. cron scheduling, timezone configuration, and multi-instance deployments. [One-Off Scheduled Tasks](../guide/one-off-tasks.md) covers running a task once at a runtime-computed time.

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

## `task_manager.schedule_once()`

Coroutine that schedules a single run of `func` at an exact future time. Unlike `@schedule()`, which fixes a cadence at import time, this takes a timestamp computed at runtime.

```python
await task_manager.schedule_once(
    settle_auction,
    listing_id,
    run_at=closes_at,
    run_key=f"auction-close:{listing_id}",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `func` | `Callable` | required | The task function. Must be registered with `@task_manager.task()` or `@task_manager.schedule()` so it can be resolved by name when the entry fires. |
| `*args` | `Any` | | Positional arguments forwarded to `func`. |
| `run_at` | `datetime` | required | When to run. Naive datetimes are treated as UTC. A past time fires on the next scheduler tick. |
| `run_key` | `str` | required | Identity of this pending firing. Scheduling again with the same key replaces the entry. |
| `idempotency_key` | `str \| None` | `None` | Forwarded onto the task record when the entry fires. Guards duplicate execution, independent of `run_key`. |
| `tags` | `dict[str, str] \| None` | `None` | Key/value labels attached to the task when it fires. |
| `priority` | `int \| None` | `None` | Execution priority for the firing. |
| `queue` | `str \| None` | `None` | Named queue to route the firing into. |
| `**kwargs` | `Any` | | Keyword arguments forwarded to `func`. |

Raises `RuntimeError` if no snapshot backend is configured, or if the configured backend does not support one-off schedules. Raises `TaskArgumentError` if `func` uses `executor='process'` and any argument is not picklable.

Note that `run_at`, `run_key`, `idempotency_key`, `tags`, `priority`, and `queue` are reserved names. A task function parameter with one of those names must be passed positionally or bound with `functools.partial`.

---

## `task_manager.cancel_scheduled()`

Coroutine that removes a pending one-off firing.

```python
removed = await task_manager.cancel_scheduled(f"auction-close:{listing_id}")
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `run_key` | `str` | The key passed to `schedule_once()`. |

Returns `True` if a pending entry was removed, `False` if none existed (already fired, already cancelled, or never scheduled).

The backend row is deleted before the in-memory copy is cleared, so a cancel racing a restart cannot re-arm the cancelled task.

---

## `task_manager.list_scheduled()`

Coroutine returning pending one-off entries.

```python
pending = await task_manager.list_scheduled()
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `before` | `datetime \| None` | one year out | Upper bound on `fire_at`. Pass a nearer bound when the pending set is large. |

Returns a `list[ScheduledOnce]`.

---

## `ScheduledOnce`

Represents one pending one-off firing. Created by `schedule_once()` and persisted to the backend. This is not a task invocation: no `task_id` exists until the entry fires, at which point a normal `TaskRecord` is created and the entry is deleted.

```python
from fastapi_taskflow import ScheduledOnce
```

```python
@dataclass
class ScheduledOnce:
    run_key:           str
    func_name:         str
    fire_at:           datetime
    args:              tuple
    kwargs:            dict
    encrypted_payload: bytes | None
    queue:             str
    priority:          int | None
    idempotency_key:   str | None
    tags:              dict[str, str]
    created_at:        datetime
```

| Field | Type | Description |
|-------|------|-------------|
| `run_key` | `str` | Identity of the pending firing. Primary key in the backend. |
| `func_name` | `str` | Name of the registered function to run. |
| `fire_at` | `datetime` | UTC time at which the task should run. |
| `args` | `tuple` | Positional arguments. Empty when `encrypted_payload` is set. |
| `kwargs` | `dict` | Keyword arguments. Empty when `encrypted_payload` is set. |
| `encrypted_payload` | `bytes \| None` | Fernet-encrypted `(args, kwargs)` when `encrypt_args_key` is configured. |
| `queue` | `str` | Named queue the firing is routed into. |
| `priority` | `int \| None` | Priority to enqueue the firing at. |
| `idempotency_key` | `str \| None` | Forwarded onto the `TaskRecord` at fire time. |
| `tags` | `dict[str, str]` | Labels forwarded onto the `TaskRecord` at fire time. |
| `created_at` | `datetime` | When the entry was scheduled. |

---

## `ScheduledEntry`

`ScheduledEntry` represents one entry in the scheduler's heap. Created by `@task_manager.schedule()` for recurring tasks, or built from a `ScheduledOnce` when a one-off is armed. You never instantiate this directly.

```python
from fastapi_taskflow.periodic import ScheduledEntry
```

```python
@dataclass
class ScheduledEntry:
    func:           Callable
    config:         TaskConfig
    every:          float | None
    cron:           str | None
    run_on_startup: bool
    timezone:       str
    next_run:       datetime
    once:           ScheduledOnce | None
    cancelled:      bool
```

| Field | Type | Description |
|-------|------|-------------|
| `func` | `Callable` | The task function, already registered in the task registry. |
| `config` | `TaskConfig` | Execution settings (retries, delay, backoff). |
| `every` | `float \| None` | Interval in seconds between runs. `None` when `cron` is used or for one-offs. |
| `cron` | `str \| None` | Five-field cron expression. `None` when `every` is used or for one-offs. |
| `run_on_startup` | `bool` | Whether to fire on the first tick immediately after startup. |
| `timezone` | `str` | IANA timezone name used when evaluating the cron expression. `"UTC"` by default. |
| `next_run` | `datetime` | UTC time of the next scheduled execution. Updated after each firing. |
| `once` | `ScheduledOnce \| None` | Set for one-off entries. `None` for recurring entries. |
| `cancelled` | `bool` | Tombstone flag. Cancelling or replacing a one-off marks the heap entry dead, since `heapq` cannot remove an arbitrary element. |

The `recurring` property returns `True` when `once` is `None`. Calling `compute_next()` on a one-off entry raises `ValueError`, since a one-off has no next run.

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
