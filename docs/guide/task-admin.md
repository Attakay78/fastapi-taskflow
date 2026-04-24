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

## Automatic retention

To prune old records automatically, set `retention_days` on `TaskManager` or on `TaskAdmin`:

```python
# Set at construction time
task_manager = TaskManager(snapshot_db="tasks.db", retention_days=30)

# Or override at mount time
TaskAdmin(app, task_manager, retention_days=30)
```

Pruning runs approximately every 6 hours during the snapshot loop. Records with status `success`, `failed`, or `cancelled` whose `end_time` is older than `retention_days` are deleted from both the in-memory store and the backend. Pending and running tasks are never deleted.

## Deleting task history

Completed tasks (success, failed, cancelled, interrupted) can be deleted on demand by time window. Pending and running tasks are never touched.

```bash
# Delete tasks completed more than 6 hours ago
curl -X DELETE "http://localhost:8000/tasks/history?value=6&unit=hour"

# Delete tasks completed more than 7 days ago
curl -X DELETE "http://localhost:8000/tasks/history?value=7&unit=day"
```

The `unit` parameter accepts `min`, `hour`, or `day`. The response shows how many records were removed from each layer:

```json
{"deleted": 42, "store": 30, "backend": 12}
```

The same action is available in the dashboard via the **Clear history** button in the filter panel.

## Cancelling tasks

Both pending and running tasks can be cancelled:

```bash
curl -X POST http://localhost:8000/tasks/{task_id}/cancel
```

For `pending` tasks the status is set to `cancelled` immediately. For `running` async tasks the asyncio task is cancelled and the status transitions to `cancelled` once the executor handles the interruption. Sync tasks running in a thread pool stop waiting from asyncio's perspective but the underlying thread runs to completion.

The cancel action is recorded in the audit log.

## Audit log

Every retry and cancel action is recorded with the timestamp, actor username, and the affected task ID. The last 1000 entries are kept in memory. When auth is configured, the username of the logged-in user is recorded; otherwise the actor is `"anonymous"`.

```bash
curl http://localhost:8000/tasks/audit
```

## Without TaskAdmin

If you do not want the dashboard or API routes, use `init_app()` to register lifecycle hooks without mounting any routes:

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

Both approaches work in v0.6.0+. The lifespan handler remains valid if you prefer explicit control.
