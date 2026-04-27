# TaskAdmin

Mounts the observability routes onto a FastAPI app and manages the snapshot scheduler lifecycle.

## Constructor

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

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `app` | `FastAPI` | required | The FastAPI application to mount routes onto. |
| `task_manager` | `TaskManager` | required | The task manager instance. |
| `path` | `str` | `"/tasks"` | URL prefix for all mounted routes. |
| `display_func_args` | `bool` | `False` | Store and display task arguments in the dashboard. |
| `auto_install` | `bool` | `False` | Call `task_manager.install(app)` automatically. Enables bare `BackgroundTasks` injection. |
| `auth` | `tuple \| list \| TaskAuthBackend \| None` | `None` | Credentials for dashboard and API authentication. `None` disables auth entirely. |
| `token_expiry` | `int` | `86400` | Seconds before an issued session token expires. |
| `secret_key` | `str \| None` | `None` | HMAC signing key for tokens. Auto-generated if `None` and auth is configured. |
| `poll_interval` | `float` | `30.0` | Seconds between dashboard poll requests when SSE is unavailable. |
| `title` | `str` | `"fastapi-taskflow"` | Display name shown in the dashboard header badge and on the login page. |
| `retention_days` | `float \| None` | `None` | Override the retention policy on `task_manager`. Terminal records older than this many days are pruned every ~6 hours. `None` leaves the manager's setting unchanged. |

## Usage

```python
task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager)
```

No reference to the `TaskAdmin` instance needs to be kept. All work is done in the constructor.

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
    token_expiry=3600,  # 1 hour
)
```

!!! note
    If `secret_key` is not provided and `auth` is configured, a random key is generated at startup. This means sessions are invalidated on every restart. For persistent sessions across restarts, always set an explicit `secret_key`.

## Lifecycle

`TaskAdmin` always registers FastAPI startup and shutdown hooks that delegate to `TaskManager.startup()` and `TaskManager.shutdown()`. You do not need a separate lifespan handler.

**Startup** (in order):

1. Loads persisted task history from the backend (if configured)
2. Requeues pending tasks if `requeue_pending=True`
3. Starts the periodic snapshot flush loop
4. Initialises the logger chain

**Shutdown** (in order):

1. Stops the flush loop
2. Flushes completed tasks to the backend
3. Saves unfinished tasks if `requeue_pending=True`
4. Closes the logger chain
5. Shuts down the sync thread pool (if `max_sync_threads` was set)

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

Steps that are not applicable (e.g. no backend configured, no loggers) are skipped automatically.

## Cancelling tasks

`POST /tasks/{task_id}/cancel` accepts both `pending` and `running` tasks.

**Pending tasks** are cancelled immediately: the status is set to `cancelled` in the store and flushed to the backend if one is configured.

**Running async tasks** receive a cancellation signal via `asyncio.Task.cancel()`. The executor catches the resulting `CancelledError`, sets the status to `cancelled`, and flushes the record. The status transition happens asynchronously — the API response may still show `running` immediately after the call returns.

**Running sync tasks** (functions dispatched to a thread pool) have their asyncio awaitable cancelled, which stops the event loop from waiting for the result. The underlying OS thread cannot be interrupted and runs to completion. The task is marked `cancelled` once the executor handles the `CancelledError` raised at the await point.

Returns `400` if the task is in any other terminal or non-running state.

## Routes mounted

Relative to `path` (default `/tasks`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks |
| `GET` | `/tasks/metrics` | Aggregated statistics |
| `GET` | `/tasks/audit` | Audit log of retry and cancel actions |
| `GET` | `/tasks/{task_id}` | Single task detail |
| `POST` | `/tasks/{task_id}/retry` | Retry a failed or interrupted task |
| `POST` | `/tasks/{task_id}/cancel` | Cancel a pending or running task |
| `POST` | `/tasks/bulk-retry` | Retry a specific list of tasks by ID |
| `POST` | `/tasks/retry-failed` | Retry all failed tasks within a time window |
| `DELETE` | `/tasks/history` | Delete completed task history older than a time window |
| `GET` | `/tasks/dashboard` | HTML dashboard |
| `GET` | `/tasks/dashboard/stream` | SSE stream (unauthenticated) |
| `GET` | `/tasks/auth/login` | Login page *(auth only)* |
| `POST` | `/tasks/auth/login` | Submit credentials *(auth only)* |
| `GET` | `/tasks/auth/logout` | Clear session and redirect to login *(auth only)* |
