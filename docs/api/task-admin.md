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

If you are not using `TaskAdmin`, call `startup()` and `shutdown()` yourself inside a FastAPI lifespan handler:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await task_manager.startup()
    yield
    await task_manager.shutdown()

app = FastAPI(lifespan=lifespan)
```

Steps that are not applicable (e.g. no backend configured, no loggers) are skipped automatically.

## Routes mounted

Relative to `path` (default `/tasks`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks |
| `GET` | `/tasks/metrics` | Aggregated statistics |
| `GET` | `/tasks/{task_id}` | Single task detail |
| `GET` | `/tasks/dashboard` | HTML dashboard |
| `GET` | `/tasks/dashboard/stream` | SSE stream (unauthenticated) |
| `GET` | `/tasks/auth/login` | Login page *(auth only)* |
| `POST` | `/tasks/auth/login` | Submit credentials *(auth only)* |
| `GET` | `/tasks/auth/logout` | Clear session and redirect to login *(auth only)* |
