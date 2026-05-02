# Persistence

This page explains why fastapi-taskflow starts without a database, when to add one, how to configure each supported backend, and how the flush cycle keeps your data current.

## Default behaviour: in-memory

By default, fastapi-taskflow holds all task state in memory. There is no database to configure, no schema to create, and no extra dependency to install. You can build and test a complete application before deciding how you want to store data.

The trade-off is straightforward: if the process restarts, the task history is gone. Pending tasks that had not yet run are gone too.

This is the right starting point for local development, for short-lived jobs where history does not matter, and for testing. Add a backend when you need more.

## When to add persistence

Add a persistence backend when any of the following apply:

- **Crash recovery**: tasks that were queued or running when the process stopped should be re-dispatched on the next startup.
- **History across restarts**: completed task records should remain visible in the dashboard after a redeploy.
- **Multi-instance deployments**: several application instances sharing the same task history and coordinating requeue.

## SQLite: the simplest option

SQLite requires no extra install and no external service. Pass a file path to `snapshot_db` and everything else is handled for you:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
```

Tables are created automatically on first startup. `TaskAdmin` registers the startup and shutdown lifecycle hooks, so you do not need to write a lifespan function.

!!! tip
    SQLite enables WAL journal mode automatically, so multiple processes on the same host can write concurrently without conflicts. It is a solid choice for single-host deployments.

## Redis

Install the Redis extra, then pass a `RedisBackend` to `snapshot_backend`:

```bash
pip install "fastapi-taskflow[redis]"
```

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends import RedisBackend

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://localhost:6379/0"),
    snapshot_interval=30.0,
)
```

Task records are stored as Redis hashes. If your Redis instance is not configured with AOF or RDB persistence, history will not survive a Redis restart.

## PostgreSQL

Install the Postgres extra, then pass a connection string:

```bash
pip install "fastapi-taskflow[postgres]"
```

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends import PostgresBackend

task_manager = TaskManager(
    snapshot_backend=PostgresBackend("postgresql://user:pass@localhost/mydb"),
    snapshot_interval=30.0,
)
```

Tables are created automatically on first startup. All database calls wrap `psycopg2` in `asyncio.to_thread` to keep the async interface non-blocking.

## MySQL / MariaDB

Install the MySQL extra, then pass connection parameters:

```bash
pip install "fastapi-taskflow[mysql]"
```

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends import MySQLBackend

task_manager = TaskManager(
    snapshot_backend=MySQLBackend("mysql://root:secret@localhost/mydb"),
    snapshot_interval=30.0,
)
```

Tables are created automatically on first startup. The backend is compatible with MariaDB. All operations use `PyMySQL` wrapped in `asyncio.to_thread`.

## Which backend to choose

| Scenario | Recommended backend |
|---|---|
| Single host, no external services | SQLite |
| Single host, Redis already running | Redis |
| Multiple hosts or Kubernetes | Redis, PostgreSQL, or MySQL |

!!! note
    Redis, PostgreSQL, and MySQL all coordinate across any number of application instances on separate hosts. Each instance reads and writes to the same shared store. SQLite is file-local and cannot be shared across machines.

See the [multi-instance guide](multi-instance.md) for deployment setup and load balancer configuration.

## Controlling flush frequency with `snapshot_interval`

`snapshot_interval` sets how often, in seconds, completed tasks are flushed to the backend. The default is `60.0`.

A lower value keeps the backend more current but adds more write traffic. For most applications the default is fine.

```python
task_manager = TaskManager(
    snapshot_db="tasks.db",
    snapshot_interval=30.0,  # flush every 30 seconds
)
```

On a clean shutdown, a final flush always runs regardless of the interval. No completed records are lost during a graceful stop.

!!! info
    The `snapshot_interval` controls periodic background writes. The immediate flush on `SUCCESS` (described below) is independent of this setting.

## Immediate flush on SUCCESS

When a task reaches `SUCCESS` status, the executor triggers an immediate flush for that record without waiting for the next scheduled interval.

This closes the gap between task completion and backend visibility. A task that finished one second ago will already appear in the dashboard and in history queries, even if the next periodic flush is still 59 seconds away.

The flush happens at the moment the executor marks the task as `SUCCESS`. It does not delay task execution or the application response.

```mermaid
sequenceDiagram
    participant App
    participant SnapshotScheduler
    participant Executor
    participant Backend

    Note over App: startup
    App->>SnapshotScheduler: load()
    SnapshotScheduler->>Backend: load()
    Backend-->>SnapshotScheduler: stored records
    SnapshotScheduler->>App: restore into TaskStore

    loop every snapshot_interval seconds
        SnapshotScheduler->>Backend: save(completed tasks)
    end

    Note over Executor: task reaches SUCCESS or FAILED
    Executor->>SnapshotScheduler: on_success(task_id)
    SnapshotScheduler->>Backend: flush_one(task_id)
    Note over SnapshotScheduler: immediate flush, no wait for<br/>next periodic interval

    Note over App: shutdown
    App->>SnapshotScheduler: flush()
    SnapshotScheduler->>Backend: save(all completed tasks)
```

## Querying history

The SQLite backend includes a `query()` method for filtering completed task records:

```python
# All failed tasks
records = task_manager._scheduler.query(status="failed")

# Failed tasks for a specific function, newest first
records = task_manager._scheduler.query(
    status="failed",
    func_name="send_email",
    limit=50,
)
```

!!! note
    `query()` is only available on `SqliteBackend`. It is not part of the `SnapshotBackend` ABC and is not available on the Redis, PostgreSQL, or MySQL backends.

## Showing arguments in the dashboard

Pass `display_func_args=True` to `TaskAdmin` to store and display the arguments each task was called with:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()

TaskAdmin(app, task_manager, display_func_args=True)
```

When enabled, the arguments are stored alongside the task record and shown in the task detail panel. This is useful for debugging without digging through logs.
