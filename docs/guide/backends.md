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

## Storage separation

The ABC deliberately separates two concerns:

- **History** (`save` / `load`): completed tasks kept for observability and the dashboard.
- **Requeue** (`save_pending` / `load_pending` / `clear_pending`): unfinished tasks saved at shutdown for re-execution on the next startup.

Keep these in separate tables or key namespaces so they never mix.

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

**Caveats:** Tables are created automatically on first startup. All operations wrap `psycopg2` in `asyncio.to_thread` to keep the interface non-blocking.

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

**Caveats:** Tables are created automatically on first startup. All operations use `PyMySQL` wrapped in `asyncio.to_thread`.

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
