# Backends

## SnapshotBackend (ABC)

```python
from fastapi_taskflow.backends.base import SnapshotBackend
```

Abstract base class all backends must implement. See [Custom Backends](../guide/backends.md) for the full method signatures and a worked example.

## SqliteBackend

```python
from fastapi_taskflow.backends.sqlite import SqliteBackend

backend = SqliteBackend("tasks.db")
```

Stores history in a `task_snapshots` table and pending tasks in a `task_pending_requeue` table. Handles schema migrations automatically on first connection.

### Extra method: `query()`

Not part of the ABC. Only available on `SqliteBackend`.

```python
records = backend.query(
    status: str | None = None,
    func_name: str | None = None,
    limit: int = 100,
) -> list[dict]
```

Or via the scheduler shortcut:

```python
task_manager._scheduler.query(status="failed", func_name="send_email")
```

## RedisBackend

```bash
pip install "fastapi-taskflow[redis]"
```

```python
from fastapi_taskflow.backends.redis import RedisBackend

backend = RedisBackend(
    url: str = "redis://localhost:6379/0",
    prefix: str = "fastapi_taskflow",
)
```

Stores history as Redis hashes with a set-based index. Pending tasks are stored in a separate key namespace.
