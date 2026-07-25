# One-Off Scheduled Tasks

This page covers running a task exactly once at a future time that your application computes at runtime, including rescheduling, cancellation, and how pending firings survive restarts.

## When to use this instead of `schedule()`

`@task_manager.schedule(every=...)` and `cron=` fix a cadence at import time, and that cadence applies to every firing. That fits work that is not tied to any particular record: flush a cache hourly, run a nightly cleanup, poll an API every five minutes.

It does not fit the other common shape, which is acting on one specific record when its own deadline passes. A transaction enters an acknowledgement window at 14:12 on a Tuesday with a 48-hour buffer. A trial started at an arbitrary moment and expires 14 days later. An auction closes at a time the seller picked. In each case the run time is data, not configuration, and it is different for every record.

The usual workaround is a materialised `next_action_at` column plus a periodic sweep that polls for rows that are due. That works, but every project that needs it rebuilds the same thing: the poll query, the index, the reconciliation job that catches drift, and the care needed to keep several write sites in sync.

`schedule_once()` covers that case directly.

## Basic usage

```python
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager)


@task_manager.task()
async def auto_complete_transaction(transaction_id: str) -> None:
    ...


@app.post("/transactions/{transaction_id}/acknowledge")
async def acknowledge(transaction_id: str):
    await task_manager.schedule_once(
        auto_complete_transaction,
        transaction_id,
        run_at=datetime.now(timezone.utc) + timedelta(hours=48),
        run_key=f"auto-complete:{transaction_id}",
    )
    return {"status": "acknowledged"}
```

`schedule_once()` is a coroutine, unlike `add_task()`. It has to be awaited because it writes the pending firing to the backend before returning, which is what makes the firing survive a restart.

The task function must be registered with `@task_manager.task()` or `@task_manager.schedule()`. When the firing comes due it is resolved by name, possibly in a different process than the one that scheduled it, so an unregistered function cannot be found.

## `run_at`

The time to run. Naive datetimes are treated as UTC, consistent with the rest of the library. A time in the past fires on the next scheduler tick rather than being rejected, so clock skew between your app and your database does not cause silent drops.

```python
run_at=datetime.now(timezone.utc) + timedelta(hours=48)   # explicit UTC
run_at=acknowledged_at + timedelta(hours=buffer_hours)    # computed from your data
```

## `run_key` and rescheduling

`run_key` is the identity of the pending firing. It is what makes a moved deadline tractable.

Calling `schedule_once()` again with a `run_key` that is already pending **replaces** the entry. It does not create a second firing.

```python
# Original deadline.
await task_manager.schedule_once(
    auto_complete_transaction, transaction_id,
    run_at=acknowledged_at + timedelta(hours=48),
    run_key=f"auto-complete:{transaction_id}",
)

# The buyer requested an extension. This moves the deadline.
await task_manager.schedule_once(
    auto_complete_transaction, transaction_id,
    run_at=acknowledged_at + timedelta(hours=96),
    run_key=f"auto-complete:{transaction_id}",
)
```

Only one firing happens, at the later time. Any string works as a key. Prefixing by schedule type, as in `f"auto-complete:{transaction_id}"`, keeps different kinds of schedule for the same record from colliding.

## Cancelling

```python
@app.post("/transactions/{transaction_id}/dispute")
async def raise_dispute(transaction_id: str):
    await task_manager.cancel_scheduled(f"auto-complete:{transaction_id}")
    return {"status": "disputed"}
```

`cancel_scheduled()` returns `True` if a pending entry was removed and `False` if none existed, which covers the cases where the task already fired, was already cancelled, or was never scheduled.

Cancellation removes the entry from the backend first and then clears the in-memory copy. That ordering matters: if it only cleared memory, a cancel racing a restart would let the next startup re-arm the cancelled task from the backend.

## Inspecting what is pending

```python
pending = await task_manager.list_scheduled()

for entry in pending:
    print(entry.run_key, entry.func_name, entry.fire_at)
```

By default this returns everything pending. When the pending set is large, pass a bound:

```python
from datetime import datetime, timedelta, timezone

soon = await task_manager.list_scheduled(
    before=datetime.now(timezone.utc) + timedelta(hours=1)
)
```

## Passing arguments

Positional and keyword arguments are forwarded to the function, the same as `add_task()`:

```python
await task_manager.schedule_once(
    send_reminder,
    patient_id,
    channel="sms",
    run_at=appointment_at - timedelta(hours=24),
    run_key=f"reminder:{appointment_id}",
)
```

Arguments are serialised to the backend, so they must be JSON-serialisable, or picklable when argument encryption is enabled. When `encrypt_args_key` is configured on the `TaskManager`, arguments are encrypted at rest in the schedule table exactly as they are for normal tasks.

!!! warning
    `run_at`, `run_key`, `idempotency_key`, `tags`, `priority`, and `queue` are reserved keyword arguments. A task function with a parameter of one of those names cannot receive it through `schedule_once()`. Pass it positionally, or wrap the function with `functools.partial`.

## `run_key` is not `idempotency_key`

These solve different problems and live in separate namespaces.

| | `run_key` | `idempotency_key` |
|---|---|---|
| Identifies | A pending schedule | An execution |
| On collision | Replaces the pending entry | Skips, returns the existing task |
| Lifetime | Until it fires or is cancelled | Permanent once recorded |
| Purpose | Move or cancel a deadline | Prevent duplicate work |

Both can be supplied at once. The `idempotency_key` is carried on the entry and applied to the task record when it fires:

```python
await task_manager.schedule_once(
    auto_complete_transaction, transaction_id,
    run_at=deadline,
    run_key=f"auto-complete:{transaction_id}",
    idempotency_key=f"completed:{transaction_id}",
)
```

## Backend requirement

A snapshot backend is required. The point of the feature is that a firing scheduled today still happens after tomorrow's deploy, and that is only possible if it is written somewhere durable.

```python
task_manager = TaskManager(snapshot_db="tasks.db")
```

`SqliteBackend`, `PostgresBackend`, `MySQLBackend`, and `RedisBackend` all support one-off schedules. Calling `schedule_once()` with no backend, or with a custom backend that has not implemented the storage methods, raises `RuntimeError` immediately at the call site. It never accepts a firing it cannot durably store.

Custom backends opt in by setting `supports_scheduled_once = True` and implementing `save_scheduled`, `load_due`, `delete_scheduled`, and `claim_scheduled`. See [Custom Backends](backends.md).

## Surviving restarts

Pending firings live in the backend, not in memory. On startup the scheduler loads the ones that are coming due and arms them. A firing scheduled 48 hours out is unaffected by deploys in between.

The one thing to know: while the app is down, nothing fires. A firing whose time passed during a deploy runs shortly after the new instance starts, rather than being skipped.

## Multi-instance deployments

When several instances share a backend, all of them load the same pending entries. Before running one, an instance claims it with a single atomic delete. Exactly one instance wins that claim and runs the task. The others see the entry is gone and move on.

This is a stronger guarantee than the TTL-based lock used for recurring schedules. A recurring lock gives at most one firing per TTL window, which is the right model when there is an interval to derive a TTL from. A one-off has no interval, and the claim gives exactly-once directly.

No configuration is needed. Point every instance at the same backend.

## How pending entries are held in memory

Entries are not all loaded at once. The scheduler keeps a horizon window, by default five minutes, and refills it every two and a half minutes. Only entries coming due inside that window are held in memory. Everything further out stays in the backend.

This keeps memory proportional to how many firings are imminent rather than to how many are pending in total. An application with 100,000 open deadlines spread over the next month holds only the handful due in the next few minutes.

Firing accuracy is unaffected. Once an entry is in the window the scheduler sleeps until its exact time, and an entry scheduled for sooner than the next refill is armed immediately rather than waiting.

The defaults are module-level constants in `fastapi_taskflow.periodic`:

```python
from fastapi_taskflow.periodic import DEFAULT_HORIZON, DEFAULT_REFILL_INTERVAL
```

If you change them, keep the refill interval below the horizon. Otherwise an entry can come due in the gap between two refills.

## Dashboard

One-off firings produce ordinary task records when they run, with `source="scheduled"`, so they appear in the task list with the same badge as recurring scheduled runs.

They are deliberately **not** listed individually in the Schedules tab. That tab enumerates every entry on each dashboard update, and the pending one-off set is unbounded, so listing them would make the dashboard cost scale with your open deadline count. The Schedules tab shows recurring entries plus a count of armed one-offs.

## Migrating from a polling column

If you currently maintain a `next_action_at` column and sweep it periodically, the mapping is direct. Every place that writes `next_action_at` becomes a `schedule_once()` call with the same `run_key`. Every place that clears it becomes `cancel_scheduled()`. The sweep task and its reconciliation job can be deleted.

Run both in parallel first if the deadlines are financially significant. Keep the sweep in place, have it log rather than act, and compare against what the scheduler fires until you are satisfied they agree.
