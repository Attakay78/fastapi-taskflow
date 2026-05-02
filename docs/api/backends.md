# Backends

Snapshot backends persist task history and pending-requeue state between process restarts.

> **Guide:** [Backends](../guide/backends.md) covers choosing a backend, multi-instance deployments, and implementing a custom backend.

---

## SnapshotBackend (ABC)

`SnapshotBackend` is the abstract base class every backend must implement. Import it from:

```python
from fastapi_taskflow.backends.base import SnapshotBackend
```

Backends handle two separate storage concerns:

- **History** (`save` / `load`) -- completed tasks kept for observability and the dashboard. Written periodically by the snapshot scheduler and read back on startup.
- **Requeue** (`save_pending` / `load_pending` / `clear_pending`) -- tasks that had not finished when the app shut down. Stored separately so they can be re-dispatched on the next startup without polluting the history log.

### Abstract methods

All five methods below are required. A backend that does not need a feature (for example, an in-memory backend) should still implement them, returning empty results where appropriate.

#### `save(records) -> int`

```python
async def save(self, records: list[TaskRecord]) -> int
```

Persists completed task records to the history store. Should upsert so repeated calls are idempotent. Returns the number of records written or updated.

#### `load() -> list[TaskRecord]`

```python
async def load(self) -> list[TaskRecord]
```

Returns all previously persisted completed task records. Called at startup to repopulate the in-memory store.

#### `save_pending(records) -> int`

```python
async def save_pending(self, records: list[TaskRecord]) -> int
```

Persists unfinished tasks at shutdown so they can be requeued on the next startup. Replaces any previously saved pending snapshot wholesale. Called once on shutdown when `requeue_pending=True`. Returns the number of records written.

#### `load_pending() -> list[TaskRecord]`

```python
async def load_pending(self) -> list[TaskRecord]
```

Returns all tasks saved by `save_pending()`. Called once on startup, before `clear_pending()`.

#### `clear_pending() -> None`

```python
async def clear_pending(self) -> None
```

Deletes all records saved by `save_pending()`. Called after pending tasks have been re-dispatched, so they are not executed again on subsequent restarts.

#### `close() -> None`

```python
async def close(self) -> None
```

Releases any held resources (connections, file handles, etc.). Called by the snapshot scheduler on shutdown.

### Optional methods

These methods have default implementations in `SnapshotBackend` but can be overridden for better performance or distributed correctness.

| Method | Signature | Default behaviour |
|--------|-----------|-------------------|
| `claim_pending` | `(task_id: str) -> bool` | Always returns `True`. Override to prevent duplicate dispatch in multi-instance deployments. |
| `check_idempotency_key` | `(key: str) -> str \| None` | Always returns `None`. Override to enable cross-instance idempotency key deduplication. |
| `record_idempotency_key` | `(key: str, task_id: str) -> None` | No-op. Override to persist idempotency keys after successful completion. |
| `completed_ids` | `(task_ids: list[str]) -> set[str]` | Loads all history and filters in Python. Override for a targeted query. |
| `delete_before` | `(cutoff: datetime) -> int` | No-op, returns `0`. Override to support the retention pruning feature. |
| `acquire_schedule_lock` | `(key: str, ttl: int) -> bool` | Always returns `True`. Override to prevent duplicate scheduled firings across instances. |

---

## SqliteBackend

`SqliteBackend` stores history in a `task_snapshots` table and pending tasks in a `task_pending_requeue` table. Schema migrations are handled automatically on first connection. No extra dependencies are required.

```python
from fastapi_taskflow.backends.sqlite import SqliteBackend

backend = SqliteBackend("tasks.db")
```

```python
task_manager = TaskManager(snapshot_backend=SqliteBackend("tasks.db"))
```

You can also use the shorthand `snapshot_db="tasks.db"` on `TaskManager`, which creates a `SqliteBackend` automatically.

### Supported features

| Feature | Supported |
|---------|-----------|
| Task history | Yes |
| Pending requeue | Yes |
| Idempotency keys | Yes |
| Distributed schedule locking | Yes (same-host multi-process) |
| Retention pruning (`delete_before`) | Yes |
| Multi-host distributed locking | No (use `RedisBackend`) |

### Extra method: `query()`

`query()` is not part of the `SnapshotBackend` ABC. It is available only on `SqliteBackend` for direct inspection of historical records.

```python
records = backend.query(
    status: str | None = None,
    func_name: str | None = None,
    limit: int = 100,
) -> list[dict]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | `str \| None` | `None` | Filter by status string (for example `"failed"` or `"success"`). `None` returns all statuses. |
| `func_name` | `str \| None` | `None` | Filter by the registered function name. `None` returns all functions. |
| `limit` | `int` | `100` | Maximum number of rows to return. Results are ordered newest first. |

Returns a `list[dict]`, where each dict corresponds to a row in `task_snapshots`.

**Example:**

```python
backend = SqliteBackend("tasks.db")
failed = backend.query(status="failed", func_name="send_email", limit=50)
for row in failed:
    print(row["task_id"], row["error"])
```

---

## RedisBackend

`RedisBackend` stores history as Redis hashes with a set-based index. Pending tasks are stored in a separate key namespace. Supports distributed schedule locking, making it the recommended backend for multi-instance deployments.

```bash
pip install "fastapi-taskflow[redis]"
```

```python
from fastapi_taskflow.backends.redis import RedisBackend

backend = RedisBackend(
    url="redis://localhost:6379/0",
    prefix="fastapi_taskflow",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | `"redis://localhost:6379/0"` | Redis connection URL. |
| `prefix` | `str` | `"fastapi_taskflow"` | Key prefix for all keys written by this backend. Change this when sharing a Redis instance with other applications. |

### Supported features

| Feature | Supported |
|---------|-----------|
| Task history | Yes |
| Pending requeue | Yes |
| Idempotency keys | Yes |
| Distributed schedule locking | Yes |
| Retention pruning (`delete_before`) | Yes |
| Multi-host distributed locking | Yes |

---

## PostgresBackend

`PostgresBackend` stores history in a `task_snapshots` table and pending tasks in a `task_pending_requeue` table. Tables and indexes are created automatically on first connection. Uses `psycopg2` with `asyncio.to_thread` for non-blocking async operation.

```bash
pip install "fastapi-taskflow[postgres]"
```

```python
from fastapi_taskflow.backends.postgres import PostgresBackend

backend = PostgresBackend("postgresql://user:pass@localhost:5432/mydb")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | required | PostgreSQL connection string in `postgresql://` format. |

### Supported features

| Feature | Supported |
|---------|-----------|
| Task history | Yes |
| Pending requeue | Yes |
| Idempotency keys | No |
| Distributed schedule locking | No (use `RedisBackend` for multi-instance) |
| Retention pruning (`delete_before`) | Yes |

---

## MySQLBackend

`MySQLBackend` stores history in a `task_snapshots` table and pending tasks in a `task_pending_requeue` table. Tables are created automatically on first connection. Uses `PyMySQL` with `asyncio.to_thread` for non-blocking async operation. Compatible with MariaDB.

```bash
pip install "fastapi-taskflow[mysql]"
```

```python
from fastapi_taskflow.backends.mysql import MySQLBackend

backend = MySQLBackend("mysql://root:secret@localhost:3306/mydb")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | `str` | required | MySQL server hostname. |
| `port` | `int` | `3306` | MySQL server port. |
| `user` | `str` | required | Database username. |
| `password` | `str` | required | Database password. |
| `database` | `str` | required | Name of the database to connect to. |

### Supported features

| Feature | Supported |
|---------|-----------|
| Task history | Yes |
| Pending requeue | Yes |
| Idempotency keys | No |
| Distributed schedule locking | No (use `RedisBackend` for multi-instance) |
| Retention pruning (`delete_before`) | Yes |
