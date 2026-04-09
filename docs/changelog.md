# Changelog

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
