# Backends

This page explains the `SnapshotBackend` contract, covers each built-in backend with its install command and caveats, and shows you how to write your own backend for any storage system.

## Why backends are pluggable

fastapi-taskflow does not hard-code a storage system. All persistence goes through a `SnapshotBackend` abstract base class. You can swap SQLite for Redis or PostgreSQL without changing any other part of your code, and you can write a custom backend for any datastore your infrastructure already uses.

## The `SnapshotBackend` ABC

The ABC lives in `fastapi_taskflow.backends.base`. Every backend, including the ones built into this library, implements exactly this contract:

```python
from fastapi_taskflow.backends.base import SnapshotBackend
from fastapi_taskflow.models import TaskRecord

class MyBackend(SnapshotBackend):

    async def save(self, records: list[TaskRecord]) -> int:
        # Persist completed tasks (SUCCESS / FAILED) to the history store.
        # Must upsert; repeated calls must be idempotent.
        # Returns the number of records written or updated.
        ...

    async def load(self) -> list[TaskRecord]:
        # Return all persisted completed task records.
        # Called at startup to repopulate the in-memory store.
        ...

    async def save_pending(self, records: list[TaskRecord]) -> int:
        # Persist unfinished tasks at shutdown for requeue on next startup.
        # Replaces any previous pending snapshot wholesale.
        # Returns the number of records written.
        ...

    async def load_pending(self) -> list[TaskRecord]:
        # Return all tasks saved by save_pending.
        # Called once on startup, before clear_pending.
        ...

    async def clear_pending(self) -> None:
        # Delete all records saved by save_pending.
        # Called after pending tasks have been re-dispatched.
        ...

    async def close(self) -> None:
        # Release connections, close file handles, flush buffers.
        # Called once on app shutdown.
        ...
```

Two additional methods have sensible defaults that you can override for better performance:

```python
    async def delete_before(self, cutoff: datetime) -> int:
        # Delete terminal records (SUCCESS / FAILED / INTERRUPTED) with
        # end_time before cutoff. Never deletes PENDING or RUNNING records.
        # Returns the number of records deleted.
        # Default is a no-op returning 0.
        ...

    async def completed_ids(self, task_ids: list[str]) -> set[str]:
        # Given a list of task IDs, return the subset already recorded as
        # SUCCESS in the backend.
        # Used at startup to skip re-dispatching tasks that already
        # succeeded on another instance before the crash.
        # Default loads all history and filters in Python; override this
        # for a targeted query when your backend supports it.
        ...
```

!!! tip
    See the [API reference](../api/task-admin.md) for the full method signatures, including `claim_pending` and `acquire_schedule_lock`, which are relevant for multi-instance deployments.

## Supporting one-off schedules

[One-off scheduled tasks](one-off-tasks.md) need four more methods. They are optional: a backend that does not implement them simply cannot be used with `schedule_once()`, and the error surfaces at the call site rather than silently dropping the firing.

To opt in, set `supports_scheduled_once = True` and implement all four:

```python
from fastapi_taskflow.models import ScheduledOnce


class MyBackend(SnapshotBackend):
    supports_scheduled_once = True

    async def save_scheduled(self, entry: ScheduledOnce) -> None:
        # Upsert on entry.run_key. Scheduling again with an existing key must
        # replace the entry, not create a second firing.
        ...

    async def load_due(self, before: datetime) -> list[ScheduledOnce]:
        # Return pending entries with fire_at at or before `before`.
        # Index fire_at: this runs on every refill tick.
        ...

    async def delete_scheduled(self, run_key: str) -> bool:
        # Remove the entry. Returns True if one was removed.
        ...

    async def claim_scheduled(self, run_key: str) -> bool:
        # Atomically remove the entry and report whether THIS caller removed
        # it. Must be a single atomic operation, since it is what guarantees
        # exactly one instance fires the task.
        ...
```

`claim_scheduled` is the important one. In every built-in backend it is a single delete that reports whether it removed a row, which is what makes the firing exactly-once across instances. Implementing it as a read followed by a separate delete opens a window where two instances both see the entry and both fire it.

Deleting a `run_key` that does not exist is not an error. Both `delete_scheduled` and `claim_scheduled` return `False` in that case.

## Storage separation

The ABC deliberately separates three concerns:

- **History** (`save` / `load`): completed tasks kept for observability and the dashboard.
- **Requeue** (`save_pending` / `load_pending` / `clear_pending`): unfinished tasks saved at shutdown for re-execution on the next startup.
- **One-off schedules** (`save_scheduled` / `load_due` / `delete_scheduled` / `claim_scheduled`): firings scheduled for a future time, which have not run yet and may never run if cancelled.

Keep these in separate tables or key namespaces so they never mix. In particular, do not store one-off schedules in the requeue namespace: requeue is loaded and dispatched immediately on startup, which would fire every pending schedule at once.

## Built-in backends

| Backend | Import path | Extra install |
|---|---|---|
| SQLite | `fastapi_taskflow.backends.sqlite.SqliteBackend` | None |
| Redis | `fastapi_taskflow.backends.redis.RedisBackend` | `fastapi-taskflow[redis]` |
| PostgreSQL | `fastapi_taskflow.backends.postgres.PostgresBackend` | `fastapi-taskflow[postgres]` |
| MySQL / MariaDB | `fastapi_taskflow.backends.mysql.MySQLBackend` | `fastapi-taskflow[mysql]` |

### SQLite

No extra install needed. The quickest way is to pass a file path via the `snapshot_db` shorthand on `TaskManager`:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
```

Or construct the backend explicitly if you want to pass it alongside other options:

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends.sqlite import SqliteBackend

task_manager = TaskManager(snapshot_backend=SqliteBackend("tasks.db"))
```

**When to use it:** Single-host deployments where you want persistence without running an external service.

**Caveats:** SQLite is file-local. It works for multiple processes on the same host (WAL mode is enabled automatically), but it cannot be shared across separate machines.

**Thread pool:** `SqliteBackend` uses a dedicated thread pool with one persistent connection per thread. The default `max_workers=4` is fine for most workloads. Raise it only if you see backend operations queuing behind each other under heavy snapshot load.

**Extras:** `SqliteBackend` includes a `query()` method for filtering history by status and function name. This method is not part of the `SnapshotBackend` ABC and is not available on the other backends.

### Redis

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

**When to use it:** Multi-host deployments where Redis is already part of your stack, or when you want a shared backend that is straightforward to operate.

**Caveats:** Task records are stored as Redis hashes. If your Redis instance is not configured with persistence (AOF or RDB), history is lost when Redis restarts.

### PostgreSQL

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

**When to use it:** Multi-host deployments where you want durable, queryable task history using standard SQL, or where PostgreSQL is already your primary database.

**Caveats:** Tables are created automatically on first startup. Uses a `psycopg2.ThreadedConnectionPool` with a dedicated thread pool, so connections are reused across operations rather than opened per call.

**Pool sizing:** `min_conn` sets how many connections are kept open at all times (default 1). `max_conn` sets the upper bound (default 5). A connection is borrowed from the pool for each backend operation and returned immediately after. If all connections are in use when a new operation starts, it will block until one is released. Set `max_conn` to at least `max_workers` so no thread ever has to wait for a connection.

**Thread pool:** `max_workers` controls the size of the dedicated thread pool that runs all backend I/O (default 4). This thread pool is separate from FastAPI's default asyncio executor, so backend operations do not compete with sync task execution. The default is suitable for most deployments.

**Pool injection:** If your application already manages a `psycopg2` connection pool, pass it directly via the `pool` parameter. The backend will use it without closing it on shutdown:

```python
import psycopg2.pool
from fastapi_taskflow.backends import PostgresBackend

existing_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn)
backend = PostgresBackend(pool=existing_pool)
```

### MySQL / MariaDB

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

**When to use it:** Multi-host deployments where MySQL or MariaDB is your primary database.

**Caveats:** Tables are created automatically on first startup. One persistent connection is kept per executor thread and reused across operations. Stale connections dropped by MySQL's `wait_timeout` are detected and reconnected automatically.

**Thread pool:** `max_workers` controls the size of the dedicated thread pool (default 4). Because each thread holds exactly one connection, `max_workers` also caps the total number of open MySQL connections. Raise it if backend operations are queuing under high load, keeping in mind that each additional worker opens one more connection to MySQL.

## Writing a custom backend

Subclass `SnapshotBackend` and implement the six abstract methods. The example below stores records in plain dictionaries to illustrate the contract without real I/O:

```python
from datetime import datetime
from fastapi_taskflow.backends.base import SnapshotBackend
from fastapi_taskflow.models import TaskRecord


class InMemoryBackend(SnapshotBackend):
    """Demonstration backend. Not suitable for production."""

    def __init__(self) -> None:
        self._history: dict[str, TaskRecord] = {}
        self._pending: dict[str, TaskRecord] = {}

    async def save(self, records: list[TaskRecord]) -> int:
        # Upsert: overwriting is safe because save is called repeatedly.
        for r in records:
            self._history[r.task_id] = r
        return len(records)

    async def load(self) -> list[TaskRecord]:
        return list(self._history.values())

    async def save_pending(self, records: list[TaskRecord]) -> int:
        self._pending = {r.task_id: r for r in records}
        return len(records)

    async def load_pending(self) -> list[TaskRecord]:
        return list(self._pending.values())

    async def clear_pending(self) -> None:
        self._pending.clear()

    async def close(self) -> None:
        pass  # nothing to release
```

Then pass it to `TaskManager` the same way as any built-in backend:

```python
from fastapi_taskflow import TaskManager

backend = InMemoryBackend()
task_manager = TaskManager(snapshot_backend=backend, snapshot_interval=60.0)
```

!!! warning
    The `save` method must upsert, not insert. If it inserts duplicates instead, the history store will grow without bound as the periodic flush repeatedly writes the same records.

!!! tip
    For production custom backends, consider overriding `completed_ids` with a targeted query. The default implementation loads all history into memory and filters in Python, which becomes expensive as the history grows.
