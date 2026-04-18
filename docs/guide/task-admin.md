# Setting Up TaskAdmin

`TaskAdmin` mounts the observability routes and dashboard onto your FastAPI app and wires up the startup and shutdown lifecycle automatically. You do not need to keep a reference to it after construction.

## Minimal setup

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager)
```

This mounts the task list, metrics, detail, and dashboard routes under `/tasks`. On app startup and shutdown, `TaskAdmin` calls `task_manager.startup()` and `task_manager.shutdown()` for you.

## Custom mount path

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, path="/admin/tasks")
```

All routes are mounted under the new prefix: `/admin/tasks`, `/admin/tasks/dashboard`, etc.

## With auto_install

Use `auto_install=True` to enable bare `BackgroundTasks` annotation injection without a separate `install()` call:

```python
from fastapi import FastAPI, BackgroundTasks
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auto_install=True)

@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_email, email)
    return {"task_id": task_id}
```

See [Injection Patterns](injection.md) for the full comparison of injection approaches.

## With authentication

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

The dashboard and all API routes require a valid session. A login page is mounted at `/tasks/auth/login`.

For multiple users:

```python
TaskAdmin(app, task_manager, auth=[("alice", "pw1"), ("bob", "pw2")])
```

For persistent sessions across restarts, set an explicit `secret_key`. Without it, a random key is generated at startup and all sessions are invalidated on restart:

```python
import os
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(
    app,
    task_manager,
    auth=("admin", "secret"),
    secret_key=os.environ["SESSION_SECRET"],
    token_expiry=3600,
)
```

For a custom auth backend, see [Authentication](authentication.md).

## Custom title

Replace the default "fastapi-taskflow" label in the dashboard header and login page with your own app name:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, title="My App")
```

## Dashboard poll interval

When SSE is unavailable (proxies that buffer streaming responses, or multi-instance deployments where backend polling is needed), the dashboard falls back to polling. The default is every 30 seconds:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager, poll_interval=10.0)
```

## Full example

```python
import os
from fastapi import FastAPI
from fastapi_taskflow import TaskManager, TaskAdmin

task_manager = TaskManager(
    snapshot_db="tasks.db",
    requeue_pending=True,
    log_file="tasks.log",
)
app = FastAPI()

TaskAdmin(
    app,
    task_manager,
    path="/tasks",
    auto_install=True,
    auth=("admin", os.environ["DASHBOARD_PASSWORD"]),
    secret_key=os.environ["SESSION_SECRET"],
    token_expiry=86400,
    poll_interval=30.0,
    title="My App",
)
```

## Without TaskAdmin

If you do not want the dashboard or API routes, skip `TaskAdmin` and wire the lifecycle manually:

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
task_manager.install(app)
```
