# Changelog

## v0.7.0

Adds priority queues, eager dispatch, and Dead Letter Queue bulk replay.

### Priority queues

- Added `priority` parameter to `@task_manager.task()` and `add_task()`. Tasks with a priority value bypass Starlette's background task list and enter a dedicated `asyncio.PriorityQueue` drained by a worker coroutine.
- Higher values run first. Equal-priority tasks execute in arrival (FIFO) order. The conventional range is 1 (lowest) to 10 (highest); any integer is accepted.
- Per-call `priority` on `add_task()` overrides the decorator-level default.
- `TaskRecord.priority` records the priority at enqueue time. Shown in the dashboard task table and detail panel with a color-coded badge.

### Eager dispatch

- Added `eager` parameter to `@task_manager.task()` and `add_task()`. When `True`, the task is dispatched via `asyncio.create_task` the moment `add_task()` is called, before FastAPI sends the response.
- Per-call `eager` on `add_task()` overrides the decorator-level default.

### Dead Letter Queue bulk replay

- Added `POST /tasks/bulk-retry`. Accepts a JSON body `{"task_ids": [...]}` and re-enqueues each listed task that is `failed` or `interrupted`. Tasks not found, not retryable, or whose function is no longer registered are counted in `skipped` without raising an error.
- Added `POST /tasks/retry-failed`. Accepts a `since` query param (`<N>h`, `<N>d`, or `all`) and an optional `func_name` filter. Re-enqueues all matched failed tasks created within that window.
- Both endpoints return `{dispatched, skipped, results}` and record a `bulk_retry` entry in the audit log.
- Dead Letters tab toolbar: a time-window selector (1h, 6h, 24h, 7d, all) and a **Replay window** button. Clicking opens a confirmation modal before anything is dispatched.
- Per-row checkboxes on Dead Letters rows. Checking one or more rows shows a bulk bar with a **Replay selected** button. Selection clears automatically after dispatch.

---

## v0.6.1

Fixed a bug where the distributed schedule lock was issued with a zero-second TTL for sub-second intervals, causing both instances to fire the same entry on every tick. The lock TTL now has a minimum of 1 second.

---

## v0.6.0

Adds a periodic task scheduler with interval and cron triggers, distributed locking for multi-instance deployments, dashboard differentiation between scheduled and manual runs, and `TaskManager.init_app()` for lifecycle registration without `TaskAdmin`.

### Scheduled tasks

- Added `@task_manager.schedule(every=, cron=)` decorator. Registers a function as both a periodic task and a normal task registry entry so it can also be enqueued manually via `add_task()`.
- `every=` accepts a float (seconds). No extra dependencies required.
- `cron=` accepts a standard five-field cron expression. Requires `pip install "fastapi-taskflow[scheduler]"` (`croniter>=1.4`).
- `retries`, `delay`, `backoff`, and `name` work the same as on `@task_manager.task()`.
- `run_on_startup=True` fires the task on the first scheduler tick instead of waiting for the first interval or cron slot.
- `PeriodicScheduler` runs a single `asyncio.Task` loop ticking every second. Started and stopped automatically by `TaskManager.startup()` and `TaskManager.shutdown()`.

### Distributed lock

- Added `acquire_schedule_lock(key, ttl)` to `SnapshotBackend`. Default implementation always returns `True`. `SqliteBackend` and `RedisBackend` implement proper distributed locking so only one instance fires each scheduled entry per interval.

### TaskRecord source

- Added `source` field to `TaskRecord`. Value is `"manual"` for tasks enqueued via `add_task()` (default, no behaviour change) and `"scheduled"` for tasks fired by the scheduler.
- `source` is included in `TaskRecord.to_dict()` and the REST API response.

### Dashboard

- Scheduled task rows show a `scheduled` badge next to the function name.
- New Schedules tab lists all registered entries, their trigger (interval or cron expression), and the next scheduled run time.
- Dashboard tabs restructured: "Tasks" renamed to "View" (task run table); new "Tasks" tab lists all registered functions with their config as inline badges.
- Added `DELETE /tasks/history?value=N&unit=<min|hour|day>` to delete completed tasks older than a given time window from both the in-memory store and the backend.
- Added `TaskStore.delete_completed_before(cutoff)` to remove terminal records from the in-memory store where `end_time < cutoff`.
- Added `cancelled` status with a dedicated badge and metrics card; `POST /tasks/{task_id}/cancel` now works for both pending and running tasks — async tasks are cancelled immediately, sync tasks stop being awaited while the underlying thread completes.
- Added Dead Letters tab (failed tasks only), auth-gated Audit tab, drag-resizable detail panel, P95 duration in function analytics, and cancel button in the detail panel for pending and running tasks.

### Retention

- Added `retention_days` parameter to `TaskManager` and `TaskAdmin`. When set, terminal records (`success`, `failed`, `cancelled`) whose `end_time` is older than the configured number of days are pruned automatically every ~6 hours during the snapshot loop. `TaskAdmin(retention_days=...)` overrides the manager's setting at mount time. Pending and running tasks are never deleted.

### Lifecycle

- Added `TaskManager.init_app(app)`. Registers startup and shutdown hooks on a FastAPI app without requiring `TaskAdmin`. `TaskAdmin` calls this internally. Calling it more than once on the same app is safe.

---

## v0.5.1

Dashboard login screen updated to match the teal theme. `TaskAdmin` gains a `title` parameter to customise the display name shown in the dashboard header and login page.

---

## v0.5.0

Adds pluggable observability, task context, tags, argument encryption, trace propagation, and opt-in concurrency controls for async and sync tasks.

### Concurrency controls

- Added `max_concurrent_tasks` on `TaskManager`. When set, an `asyncio.Semaphore` caps how many async tasks hold event loop time simultaneously. Tasks waiting for a slot are parked without blocking the event loop or delaying request handlers. Defaults to `None` (no limit, existing behaviour unchanged).
- Added `max_sync_threads` on `TaskManager`. When set, sync task functions run in a dedicated `ThreadPoolExecutor` instead of the default asyncio thread pool, preventing sync task bursts from exhausting threads needed by sync request handlers. Defaults to `None` (existing behaviour unchanged).
- Both parameters are opt-in. Neither changes any existing behaviour when not set.

### Pluggable observability

- Added `TaskObserver` ABC. Implement it to send task log and lifecycle events to any destination.
- Added `FileLogger`, `StdoutLogger`, and `InMemoryLogger` built-in implementations.
- `TaskManager` accepts a `loggers: list[TaskObserver]` parameter. All observers run independently via `LoggerChain`; an error in one never affects the others.
- `task_log()` gains `level=` (`"debug"`, `"info"`, `"warning"`, `"error"`) and `**extra` keyword fields. Extra fields are forwarded to `LogEvent.extra` for structured log consumers. The old `task_log(message)` signature is unchanged.
- `FileLogger` and `StdoutLogger` support `min_level=` filtering so low-severity entries can be suppressed without changing the task code.
- `InMemoryLogger` captures `log_events` and `lifecycle_events` lists for test assertions.
- The `log_file` shorthand on `TaskManager` remains fully supported.

### Task context

- Added `get_task_context()`. Returns a `TaskContext` dataclass (`task_id`, `func_name`, `attempt`, `tags`) from inside any code path invoked during task execution, including helper functions.
- `TaskContext` is available in both sync and async tasks.

### Tags

- `add_task()` accepts an optional `tags: dict[str, str]` parameter. Tags are attached to the `TaskRecord` and forwarded to every `LogEvent` and `LifecycleEvent`.

### Argument encryption

- Added `encrypt_args_key` parameter to `TaskManager`. When set, task `args` and `kwargs` are encrypted with Fernet (AES-128-CBC + HMAC) at `add_task()` time and decrypted only inside the executor just before the function is called. They are never stored in plain text.
- Requires `pip install "fastapi-taskflow[encryption]"`.
- `SqliteBackend` and `RedisBackend` store the encrypted payload in a new `encrypted_payload` column/field with automatic migration.

### task_log outside managed context

- `task_log()` no longer silently drops calls made outside a managed task. It now forwards to the stdlib logger `fastapi_taskflow.task` at the matching level. Task functions work correctly in all call contexts (routes, scripts, cron jobs, direct test calls) without code changes.

### Trace context propagation

- `add_task()` captures the caller's `contextvars` context snapshot. On Python 3.11+, the task function runs inside this context so OpenTelemetry spans and other trace state flow from the enqueue site into background execution transparently. On Python 3.10, `task_log()` and `get_task_context()` still work correctly; full trace propagation requires Python 3.11+.

---

## v0.4.1

- Added docstrings with `Args:` and `Returns:` descriptions to all public classes, methods, and functions across the codebase.
- Fixed `TaskStore._notify_change` to use `call_soon_threadsafe` so `task_log()` entries from sync tasks trigger live dashboard updates correctly.

---

## v0.4.0

### File logging

- Added `log_file` parameter to `TaskManager`. When set, every `task_log()` call and retry separator is written to a plain text file in addition to being stored on the task record.
- Each line has the format `[task_id] [func_name] 2024-01-01T12:00:00 message`.
- `log_lifecycle=True` writes an additional line for each status transition (`RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`).
- Automatic rotation via `RotatingFileHandler` (default). External rotation via `WatchedFileHandler` with `log_file_mode="watched"`.
- For same-host multi-process deployments, use separate files per instance or `log_file_mode="watched"` with logrotate. For multi-host Redis deployments, each host writes its own file.

---

## v0.3.0

Adds resilience and multi-instance support.

### Resilience

- Added `INTERRUPTED` status for tasks that were mid-execution at shutdown. They are saved to history and visible in the dashboard but not re-executed automatically.
- Added `requeue_on_interrupt=True` on `@task_manager.task()` for idempotent tasks that are safe to restart from scratch.
- Completed tasks are flushed to the backend immediately on `SUCCESS`, closing the crash recovery gap between completion and the next periodic flush.

### Idempotency

- `add_task()` accepts an optional `idempotency_key`. If a non-failed task with the same key exists, the original `task_id` is returned and the task is not run again. Works in-process and cross-instance via the shared backend.

### Multi-instance

- Requeue claiming is now atomic per task (SQLite `DELETE` rowcount, Redis `HDEL`). Only one instance dispatches each pending task on startup.
- `SqliteBackend` enables WAL mode for safer concurrent multi-process access.
- Dashboard merges completed task history from all instances via the shared backend, with a cached backend read (default 5s TTL) to avoid excess I/O.
- Added `poll_interval` on `TaskAdmin` (default 30s) to control how often the SSE stream refreshes from the backend for cross-instance updates.

### Task retry

- Added `POST /tasks/{task_id}/retry` endpoint. Re-enqueues a `failed` or `interrupted` task using the original function, args, and kwargs. Returns a new task record with a fresh task ID. The original record is left unchanged in history.
- The dashboard detail panel shows a retry button for failed and interrupted tasks. Interrupted tasks include a warning that the function may have already partially executed.
- Returns `400` if the task status does not allow retry, `404` if not found, and `409` if the function is no longer registered in the process.

### Dashboard

- Added time-range filter (last 1h, 6h, 24h, 7d). Filter and sort preferences are persisted in `localStorage`.
- Added pause/resume button. While paused, incoming SSE events are buffered and a badge shows how many new tasks arrived. Resuming applies the latest state immediately. Pause state survives page reloads.
- Added bulk retry. Checkboxes appear on failed and interrupted rows. A toolbar shows the selection count and a single Retry button dispatches all selected tasks in parallel.
- `interrupted` tasks now have a dedicated amber status badge and metrics card.

---

## v0.2.0

### Task logging

- Added `task_log(message)` function. Call it inside any task to emit timestamped log entries that are stored on the task record and shown in the dashboard.
- Retry attempts are separated automatically with a `--- Retry N ---` marker so per-attempt logs are easy to read.

### Error visibility

- Failed tasks now capture the full Python traceback in a `stacktrace` field on `TaskRecord`.
- The dashboard detail panel now has three tabs: **Details**, **Logs**, and **Error**. The Logs and Error tabs are only shown when the task has data for them.

### Storage

- `TaskRecord` gains two new fields: `logs: list[str]` and `stacktrace: str | None`.
- `SqliteBackend` stores both fields as new columns with automatic migration for existing databases.
- `RedisBackend` stores both fields as hash entries. Old records without these fields load cleanly with empty defaults.

---

## v0.1.0

First public release.

### Core

- `@task_manager.task()` decorator with `retries`, `delay`, `backoff`, `persist`, and `name` options
- `ManagedBackgroundTasks` subclass of FastAPI's `BackgroundTasks` with UUID-based task tracking
- In-memory `TaskStore` with full task lifecycle: `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`
- Both sync and async task functions supported

### Injection

- `task_manager.get_tasks` FastAPI dependency
- `task_manager.background_tasks` property alias
- `task_manager.install(app)` patches FastAPI's internal injection so bare `BackgroundTasks` annotations receive a managed instance
- `TaskAdmin(auto_install=True)` convenience flag

### Observability

- `GET /tasks` — list all in-memory tasks
- `GET /tasks/{task_id}` — single task detail
- `GET /tasks/metrics` — success rate, total, duration averages
- `GET /tasks/dashboard` — live admin dashboard over SSE
- `display_func_args` option to store and show task arguments in the dashboard

### Persistence

- Pluggable `SnapshotBackend` ABC
- `SqliteBackend` — default, zero extra dependencies, includes `query()` for history lookups
- `RedisBackend` — available via `pip install "fastapi-taskflow[redis]"`
- Periodic snapshot scheduler managed automatically by `TaskAdmin`
- Final flush on shutdown

### Requeue

- `requeue_pending=True` on `TaskManager` saves unfinished tasks on shutdown
- Tasks are matched by function name and re-dispatched on next startup
- Unrecognised functions are skipped with a warning log
- Pending store is cleared after successful requeue

### Authentication

- `TaskAdmin` now accepts `auth`, `secret_key`, and `token_expiry` to protect the dashboard and task API with cookie-based authentication; supports a single credential tuple, a list of tuples, or a custom `TaskAuthBackend` instance.
