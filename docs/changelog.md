# Changelog

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
