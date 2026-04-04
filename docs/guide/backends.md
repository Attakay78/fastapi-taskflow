# Custom Backends

Snapshot backends follow a simple abstract base class. You can implement your own to persist task history to any storage system.

## The SnapshotBackend ABC

```python
from fastapi_taskflow.backends.base import SnapshotBackend
from fastapi_taskflow.models import TaskRecord

class MyBackend(SnapshotBackend):

    async def save(self, records: list[TaskRecord]) -> int:
        # Persist completed tasks (success/failed).
        # Should upsert so repeated calls are idempotent.
        # Returns number of records written.
        ...

    async def load(self) -> list[TaskRecord]:
        # Return all persisted completed task records.
        ...

    async def save_pending(self, records: list[TaskRecord]) -> int:
        # Persist unfinished tasks for requeue on next startup.
        # Called once on shutdown. Replaces any previous snapshot wholesale.
        # Returns number of records written.
        ...

    async def load_pending(self) -> list[TaskRecord]:
        # Return tasks saved via save_pending.
        # Called once on startup before clear_pending.
        ...

    async def clear_pending(self) -> None:
        # Delete all pending records after they have been requeued.
        ...

    async def close(self) -> None:
        # Release connections, flush buffers, etc.
        ...
```

## Using your backend

```python
from fastapi_taskflow import TaskManager

backend = MyBackend()
task_manager = TaskManager(snapshot_backend=backend, snapshot_interval=60.0)
```

## Storage separation

The ABC deliberately separates two concerns:

- **History** (`save` / `load`) — completed tasks kept for observability and the dashboard
- **Requeue** (`save_pending` / `load_pending` / `clear_pending`) — unfinished tasks saved at shutdown for re-execution on startup

Keep these in separate tables or key namespaces so they never mix.

## Built-in backends

| Backend | Import | Notes |
|---------|--------|-------|
| SQLite | `fastapi_taskflow.backends.sqlite.SqliteBackend` | Default. No extra dependencies. Includes `query()`. |
| Redis | `fastapi_taskflow.backends.redis.RedisBackend` | Requires `pip install "fastapi-taskflow[redis]"`. |
