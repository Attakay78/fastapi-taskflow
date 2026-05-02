# Setting Up TaskAdmin

This page explains what `TaskAdmin` does, how to configure it, and when to use the alternative lifecycle options.

## What TaskAdmin does

`TaskAdmin` is the main entry point for wiring fastapi-taskflow into your FastAPI application. It does three things:

1. Mounts the task API routes (list, detail, metrics, cancel, history) under a configurable path.
2. Mounts the real-time dashboard.
3. Registers startup and shutdown lifecycle hooks so `task_manager.startup()` and `task_manager.shutdown()` are called automatically with your app.

You do not need to keep a reference to `TaskAdmin` after construction.

## Minimal setup

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager)
```

This mounts the task list, metrics, detail, and dashboard routes under `/tasks`. The dashboard is available at `/tasks/dashboard`.

## Custom mount path

Pass `path=` to mount everything under a different prefix:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, path="/admin/tasks")
```

All routes shift to the new prefix: `/admin/tasks`, `/admin/tasks/dashboard`, and so on.

## auto_install

By default, fastapi-taskflow's dependency injection requires you to declare `tasks: TaskContext = Depends(task_manager.get_tasks)` in your route. Setting `auto_install=True` also patches FastAPI's `BackgroundTasks` so you can inject it with the plain `BackgroundTasks` annotation, without the extra `Depends` call:

```python
from fastapi import BackgroundTasks, FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auto_install=True)


@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_welcome_email, email)
    return {"task_id": task_id}
```

!!! note
    `auto_install=True` is a convenience for applications that already use `BackgroundTasks` and want minimal migration effort. See [Injection Patterns](injection.md) for a full comparison of injection approaches.

## Authentication

Pass `auth=` to require a login before accessing the dashboard and API routes. A login page is automatically mounted at `{path}/auth/login`.

Single user:

```python
TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

Multiple users:

```python
TaskAdmin(app, task_manager, auth=[("alice", "pw1"), ("bob", "pw2")])
```

For persistent sessions across restarts, set an explicit `secret_key`. Without it, a random key is generated at startup and all sessions are invalidated when the process restarts:

```python
import os

TaskAdmin(
    app,
    task_manager,
    auth=("admin", os.environ["DASHBOARD_PASSWORD"]),
    secret_key=os.environ["SESSION_SECRET"],
    token_expiry=3600,
)
```

For a custom authentication backend, see [Authentication](authentication.md).

## Custom title

Replace the default "fastapi-taskflow" label in the dashboard header and login page with your own application name:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, title="My App")
```

## Retention days

Set `retention_days` to automatically prune old completed task records. Pruning runs approximately every 6 hours during the snapshot loop. Records with status `success`, `failed`, or `cancelled` whose `end_time` is older than the threshold are deleted from both the in-memory store and the backend. Pending and running tasks are never deleted.

```python
# Set on TaskManager at construction time
task_manager = TaskManager(snapshot_db="tasks.db", retention_days=30)

# Or override when mounting TaskAdmin
TaskAdmin(app, task_manager, retention_days=30)
```

## Poll interval

The dashboard uses SSE to stream live task updates. The `poll_interval` controls how often the stream also refreshes from the shared backend to pick up completed tasks from other instances in a multi-instance deployment.

The default is 30 seconds. Reduce it if you need the history view to update more frequently:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager, poll_interval=10.0)
```

## Full example

```python
import os
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    requeue_pending=True,
    log_file="tasks.log",
    retention_days=30,
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

## Lifecycle alternatives

There are three ways to connect fastapi-taskflow's lifecycle to your app. Choose the one that fits your project structure.

### TaskAdmin (recommended)

`TaskAdmin` registers the startup and shutdown hooks automatically when you mount it. This is the right choice for most applications.

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager)
```

### init_app

Use `init_app()` when you want the lifecycle hooks but not the dashboard or API routes. This is useful for background worker processes that share a codebase with your web app but do not need the observability UI.

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

task_manager.init_app(app)
```

### Manual lifespan

Use a manual lifespan when you need explicit control over startup and shutdown order, for example when fastapi-taskflow must start after your database connection pool is ready:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()          # must run before task_manager.startup()
    await task_manager.startup()
    yield
    await task_manager.shutdown()
    await db.disconnect()


app = FastAPI(lifespan=lifespan)
```

!!! note
    When using a manual lifespan, do not also call `TaskAdmin` or `init_app()`. Both of those register their own lifespan handlers, and calling `startup()` twice will cause errors.

## See also

- [Authentication](authentication.md)
- [Injection Patterns](injection.md)
- [Multi-Instance Deployments](multi-instance.md)
- [TaskAdmin reference](../api/task-admin.md)
