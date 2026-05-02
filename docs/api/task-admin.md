# TaskAdmin

`TaskAdmin` mounts task observability routes onto a FastAPI app and manages the snapshot scheduler lifecycle.

> **Guide:** [Task Admin](../guide/task-admin.md) covers authentication patterns, retention configuration, and dashboard setup. This page documents every parameter, the routes mounted, and the SSE event format.

---

## Constructor

```python
from fastapi_taskflow import TaskAdmin

TaskAdmin(app, task_manager)
```

```python
TaskAdmin(
    app: FastAPI,
    task_manager: TaskManager,
    path: str = "/tasks",
    display_func_args: bool = False,
    auto_install: bool = False,
    auth: tuple | list | TaskAuthBackend | None = None,
    token_expiry: int = 86400,
    secret_key: str | None = None,
    poll_interval: float = 30.0,
    title: str = "fastapi-taskflow",
    retention_days: float | None = None,
)
```

All setup happens in the constructor. No reference to the `TaskAdmin` instance needs to be kept after construction.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `app` | `FastAPI` | required | The FastAPI application to mount routes onto. |
| `task_manager` | `TaskManager` | required | The task manager instance whose tasks will be exposed. |
| `path` | `str` | `"/tasks"` | URL prefix for all mounted routes. |
| `display_func_args` | `bool` | `False` | Include task arguments in the dashboard task list. Disable this if arguments may contain sensitive data. |
| `auto_install` | `bool` | `False` | Call `task_manager.install(app)` automatically, enabling bare `BackgroundTasks` injection across all routes. |
| `auth` | `tuple \| list \| TaskAuthBackend \| None` | `None` | Credentials for dashboard and API authentication. `None` disables auth entirely. Accepts a `(username, password)` tuple, a list of such tuples, or a custom `TaskAuthBackend` instance. |
| `token_expiry` | `int` | `86400` | Seconds before an issued session token expires. |
| `secret_key` | `str \| None` | `None` | HMAC signing key for session tokens. Auto-generated if `None` and auth is configured. Set an explicit value for sessions that survive restarts. |
| `poll_interval` | `float` | `30.0` | Seconds between dashboard poll requests when SSE is unavailable. |
| `title` | `str` | `"fastapi-taskflow"` | Display name shown in the dashboard header and on the login page. |
| `retention_days` | `float \| None` | `None` | Override the retention policy on `task_manager`. Terminal records older than this many days are pruned approximately every 6 hours. `None` leaves the manager's existing setting unchanged. |

---

## Authentication

Pass an `auth` argument to require login before accessing the dashboard or any task API endpoint.

**Single user:**

```python
TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

**Multiple users:**

```python
TaskAdmin(app, task_manager, auth=[("alice", "pw1"), ("bob", "pw2")])
```

**Custom backend:**

```python
from fastapi_taskflow import TaskAuthBackend

class DBAuthBackend(TaskAuthBackend):
    def authenticate_user(self, username: str, password: str) -> bool:
        return verify_against_database(username, password)

TaskAdmin(app, task_manager, auth=DBAuthBackend())
```

**Custom token lifetime and signing key:**

```python
TaskAdmin(
    app,
    task_manager,
    auth=("admin", "secret"),
    secret_key="your-secret-key",
    token_expiry=3600,
)
```

!!! note
    When `secret_key` is not provided and `auth` is configured, a random key is generated at startup. Sessions are therefore invalidated on every restart. Set an explicit `secret_key` if you need sessions to persist across restarts.

---

## Lifecycle

`TaskAdmin` always registers FastAPI startup and shutdown hooks that delegate to `TaskManager.startup()` and `TaskManager.shutdown()`. You do not need a separate lifespan handler.

**Startup** (in order):

1. Loads persisted task history from the backend (if configured).
2. Requeues pending tasks if `requeue_pending=True`.
3. Starts the periodic snapshot flush loop.
4. Initialises the observer chain.

**Shutdown** (in order):

1. Stops the flush loop.
2. Flushes completed tasks to the backend.
3. Saves unfinished tasks if `requeue_pending=True`.
4. Closes the observer chain.
5. Shuts down the sync thread pool (if `max_sync_threads` was set).

Steps that do not apply (for example, no backend configured or no loggers) are skipped automatically.

### Without TaskAdmin

If you are not using `TaskAdmin`, use `init_app()` to register lifecycle hooks without mounting any routes:

=== "v0.6.0+"

    ```python
    from fastapi import FastAPI
    from fastapi_taskflow import TaskManager

    task_manager = TaskManager(snapshot_db="tasks.db")
    app = FastAPI()

    task_manager.init_app(app)
    ```

=== "Before v0.6.0"

    ```python
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi_taskflow import TaskManager

    task_manager = TaskManager(snapshot_db="tasks.db")

    @asynccontextmanager
    async def lifespan(app):
        await task_manager.startup()
        yield
        await task_manager.shutdown()

    app = FastAPI(lifespan=lifespan)
    ```

---

## Routes mounted

All paths are relative to `path` (default `/tasks`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks. |
| `GET` | `/tasks/metrics` | Aggregated statistics across all tasks. |
| `GET` | `/tasks/audit` | Audit log of retry and cancel actions. |
| `GET` | `/tasks/{task_id}` | Full detail for a single task record. |
| `POST` | `/tasks/{task_id}/retry` | Retry a failed or interrupted task. |
| `POST` | `/tasks/{task_id}/cancel` | Cancel a pending or running task. |
| `POST` | `/tasks/bulk-retry` | Retry a specific list of task IDs. |
| `POST` | `/tasks/retry-failed` | Retry all failed tasks within a time window. |
| `DELETE` | `/tasks/history` | Delete completed task history older than a given time window. |
| `GET` | `/tasks/dashboard` | Live HTML dashboard. |
| `GET` | `/tasks/dashboard/stream` | SSE stream (unauthenticated). |
| `GET` | `/tasks/auth/login` | Login page *(auth only)*. |
| `POST` | `/tasks/auth/login` | Submit credentials *(auth only)*. |
| `GET` | `/tasks/auth/logout` | Clear session and redirect to login *(auth only)*. |

---

## Cancelling tasks

`POST /tasks/{task_id}/cancel` accepts tasks in `pending` or `running` states.

**Pending tasks** are cancelled immediately: the status is set to `cancelled` in the store and flushed to the backend if one is configured.

**Running async tasks** receive a cancellation signal via `asyncio.Task.cancel()`. The executor catches the resulting `CancelledError`, sets the status to `cancelled`, and flushes the record. The status transition happens asynchronously, so the API response may still show `running` immediately after the call returns.

**Running sync tasks** (functions dispatched to a thread pool) have their asyncio awaitable cancelled, which stops the event loop from waiting for the result. The underlying OS thread cannot be interrupted and runs to completion. The task is marked `cancelled` once the executor handles the `CancelledError` raised at the await point.

Returns `400` if the task is already in any other terminal or non-running state.

---

## SSE event format

The dashboard stream at `GET /tasks/dashboard/stream` sends Server-Sent Events. Each event is a JSON object serialised from `TaskRecord.to_dict()`.

Example event payload:

```json
{
  "task_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "func_name": "send_email",
  "status": "success",
  "created_at": "2026-04-30T10:00:00",
  "start_time": "2026-04-30T10:00:01",
  "end_time": "2026-04-30T10:00:02",
  "duration": 1.02,
  "retries_used": 0,
  "error": null,
  "logs": ["2026-04-30T10:00:01 Sending to user@example.com"],
  "stacktrace": null,
  "tags": {},
  "source": "manual",
  "priority": null
}
```

Events are emitted whenever a task changes state. The stream endpoint does not require authentication even when `auth` is configured, so the dashboard page itself can subscribe from the browser after the page load is authenticated.
