"""PostgreSQL snapshot backend.

Requires ``psycopg2-binary`` (or ``psycopg2`` for production builds)::

    pip install "fastapi-taskflow[postgres]"

Usage::

    from fastapi_taskflow import TaskManager
    from fastapi_taskflow.backends import PostgresBackend

    task_manager = TaskManager(
        snapshot_backend=PostgresBackend("postgresql://user:pass@localhost/mydb"),
        snapshot_interval=30.0,
    )
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from .base import SCHEDULED_COLUMNS, ThreadedSnapshotBackend, row_to_scheduled
from ..models import ScheduledOnce, TaskRecord, TaskStatus


_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS task_snapshots (
    task_id           TEXT             PRIMARY KEY,
    func_name         TEXT             NOT NULL,
    status            TEXT             NOT NULL,
    created_at        TEXT,
    start_time        TEXT,
    end_time          TEXT,
    duration          DOUBLE PRECISION,
    retries_used      INTEGER          DEFAULT 0,
    error             TEXT,
    snapshotted_at    TEXT,
    args_json         TEXT,
    kwargs_json       TEXT,
    logs_json         TEXT,
    stacktrace        TEXT,
    encrypted_payload TEXT,
    source            TEXT             DEFAULT 'manual',
    priority          INTEGER,
    executor          TEXT
)
"""

_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS task_pending_requeue (
    task_id           TEXT PRIMARY KEY,
    func_name         TEXT NOT NULL,
    created_at        TEXT,
    retries_used      INTEGER DEFAULT 0,
    args_json         TEXT,
    kwargs_json       TEXT,
    encrypted_payload TEXT
)
"""

_CREATE_IDEMPOTENCY = """
CREATE TABLE IF NOT EXISTS task_idempotency_keys (
    idem_key   TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_SCHEDULE_LOCKS = """
CREATE TABLE IF NOT EXISTS task_schedule_locks (
    lock_key   TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
)
"""

# One-off tasks scheduled to fire at an exact future time. Keyed by the
# caller-supplied run_key so rescheduling replaces in place. fire_at is
# indexed because the scheduler queries it every refill tick.
_CREATE_SCHEDULED = """
CREATE TABLE IF NOT EXISTS task_scheduled_once (
    run_key           TEXT PRIMARY KEY,
    func_name         TEXT NOT NULL,
    fire_at           TEXT NOT NULL,
    created_at        TEXT,
    args_json         TEXT,
    kwargs_json       TEXT,
    encrypted_payload TEXT,
    queue             TEXT DEFAULT 'default',
    priority          INTEGER,
    idempotency_key   TEXT,
    tags_json         TEXT
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snap_status ON task_snapshots(status)",
    "CREATE INDEX IF NOT EXISTS idx_snap_end_status ON task_snapshots(end_time, status)",
    "CREATE INDEX IF NOT EXISTS idx_snap_snapshotted ON task_snapshots(snapshotted_at)",
    "CREATE INDEX IF NOT EXISTS idx_snap_func_name ON task_snapshots(func_name)",
    "CREATE INDEX IF NOT EXISTS idx_sched_fire_at ON task_scheduled_once(fire_at)",
]

_UPSERT_SCHEDULED = """
INSERT INTO task_scheduled_once
    (run_key, func_name, fire_at, created_at, args_json, kwargs_json,
     encrypted_payload, queue, priority, idempotency_key, tags_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_key) DO UPDATE SET
    func_name         = EXCLUDED.func_name,
    fire_at           = EXCLUDED.fire_at,
    created_at        = EXCLUDED.created_at,
    args_json         = EXCLUDED.args_json,
    kwargs_json       = EXCLUDED.kwargs_json,
    encrypted_payload = EXCLUDED.encrypted_payload,
    queue             = EXCLUDED.queue,
    priority          = EXCLUDED.priority,
    idempotency_key   = EXCLUDED.idempotency_key,
    tags_json         = EXCLUDED.tags_json
"""

_UPSERT_HISTORY = """
INSERT INTO task_snapshots
    (task_id, func_name, status, created_at, start_time, end_time,
     duration, retries_used, error, snapshotted_at, args_json, kwargs_json,
     logs_json, stacktrace, encrypted_payload, source, priority, executor, queue)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (task_id) DO UPDATE SET
    func_name         = EXCLUDED.func_name,
    status            = EXCLUDED.status,
    created_at        = EXCLUDED.created_at,
    start_time        = EXCLUDED.start_time,
    end_time          = EXCLUDED.end_time,
    duration          = EXCLUDED.duration,
    retries_used      = EXCLUDED.retries_used,
    error             = EXCLUDED.error,
    snapshotted_at    = EXCLUDED.snapshotted_at,
    args_json         = EXCLUDED.args_json,
    kwargs_json       = EXCLUDED.kwargs_json,
    logs_json         = EXCLUDED.logs_json,
    stacktrace        = EXCLUDED.stacktrace,
    encrypted_payload = EXCLUDED.encrypted_payload,
    source            = EXCLUDED.source,
    priority          = EXCLUDED.priority,
    executor          = EXCLUDED.executor,
    queue             = EXCLUDED.queue
"""

_UPSERT_PENDING = """
INSERT INTO task_pending_requeue
    (task_id, func_name, created_at, retries_used, args_json, kwargs_json, encrypted_payload, queue)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (task_id) DO UPDATE SET
    func_name         = EXCLUDED.func_name,
    created_at        = EXCLUDED.created_at,
    retries_used      = EXCLUDED.retries_used,
    args_json         = EXCLUDED.args_json,
    kwargs_json       = EXCLUDED.kwargs_json,
    encrypted_payload = EXCLUDED.encrypted_payload,
    queue             = EXCLUDED.queue
"""


class PostgresBackend(ThreadedSnapshotBackend):
    """Persist task snapshots to a PostgreSQL database.

    Uses ``psycopg2`` with a ``ThreadedConnectionPool`` and a dedicated
    ``ThreadPoolExecutor`` so backend I/O does not compete with the
    application's default asyncio executor.

    Args:
        url: PostgreSQL connection string, e.g.
            ``"postgresql://user:pass@localhost:5432/mydb"``.
            Either ``url`` or ``pool`` must be provided.
        pool: An existing ``psycopg2.pool.ThreadedConnectionPool`` to reuse.
            When provided, ``close()`` will not shut the pool down.
        min_conn: Minimum connections kept open in the pool (default 1).
        max_conn: Maximum connections the pool will open (default 5).
        max_workers: Thread pool size for offloading sync operations (default 4).

    Example::

        from fastapi_taskflow import TaskManager
        from fastapi_taskflow.backends import PostgresBackend

        task_manager = TaskManager(
            snapshot_backend=PostgresBackend(
                "postgresql://user:pass@localhost/mydb"
            ),
        )
    """

    supports_scheduled_once = True

    def __init__(
        self,
        url: str | None = None,
        *,
        pool=None,
        min_conn: int = 1,
        max_conn: int = 5,
        max_workers: int = 4,
    ) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix="taskflow-pg")
        if pool is not None:
            self._pool = pool
            self._owns_pool = False
        elif url is not None:
            self._pool = self._create_pool(url, min_conn, max_conn)
            self._owns_pool = True
        else:
            raise ValueError("Either url or pool must be provided")
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _create_pool(url: str, min_conn: int, max_conn: int):
        try:
            import psycopg2.pool  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "PostgresBackend requires psycopg2. "
                "Install it with: pip install 'fastapi-taskflow[postgres]'"
            ) from exc
        return psycopg2.pool.ThreadedConnectionPool(min_conn, max_conn, url)

    @contextmanager
    def _get_conn(self):
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def _init_db(self) -> None:
        """Create tables and apply any pending column migrations."""
        _migrations = [
            "ALTER TABLE task_snapshots ADD COLUMN IF NOT EXISTS executor TEXT",
            "ALTER TABLE task_snapshots ADD COLUMN IF NOT EXISTS queue TEXT DEFAULT 'default'",
            "ALTER TABLE task_pending_requeue ADD COLUMN IF NOT EXISTS queue TEXT DEFAULT 'default'",
        ]
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(_CREATE_HISTORY)
                    cur.execute(_CREATE_PENDING)
                    cur.execute(_CREATE_IDEMPOTENCY)
                    cur.execute(_CREATE_SCHEDULE_LOCKS)
                    cur.execute(_CREATE_SCHEDULED)
                    for migration in _migrations:
                        cur.execute(migration)
                    for index in _INDEXES:
                        cur.execute(index)

    def _save_sync(self, records: list[TaskRecord]) -> int:
        now = datetime.utcnow().isoformat()
        rows = [
            (
                t.task_id,
                t.func_name,
                t.status.value,
                t.created_at.isoformat(),
                t.start_time.isoformat() if t.start_time else None,
                t.end_time.isoformat() if t.end_time else None,
                t.duration,
                t.retries_used,
                t.error,
                now,
                json.dumps(list(t.args), default=repr),
                json.dumps(t.kwargs, default=repr),
                json.dumps(t.logs),
                t.stacktrace,
                t.encrypted_payload.decode() if t.encrypted_payload else None,
                t.source,
                t.priority,
                t.executor,
                t.queue,
            )
            for t in records
        ]
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.executemany(_UPSERT_HISTORY, rows)
        return len(records)

    def _save_pending_sync(self, records: list[TaskRecord]) -> int:
        rows = [
            (
                t.task_id,
                t.func_name,
                t.created_at.isoformat(),
                t.retries_used,
                json.dumps(list(t.args), default=repr),
                json.dumps(t.kwargs, default=repr),
                t.encrypted_payload.decode() if t.encrypted_payload else None,
                t.queue,
            )
            for t in records
        ]
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM task_pending_requeue")
                    if rows:
                        cur.executemany(_UPSERT_PENDING, rows)
        return len(records)

    def _load_sync(self) -> list[TaskRecord]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM task_snapshots")
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return [_row_to_record(d) for d in rows]

    def _load_pending_sync(self) -> list[TaskRecord]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM task_pending_requeue")
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return [_row_to_pending_record(d) for d in rows]

    def _clear_pending_sync(self) -> None:
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM task_pending_requeue")

    def _claim_pending_sync(self, task_id: str) -> bool:
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM task_pending_requeue WHERE task_id = %s",
                        (task_id,),
                    )
                    return cur.rowcount == 1

    # ------------------------------------------------------------------
    # One-off schedules
    # ------------------------------------------------------------------

    def _save_scheduled_sync(self, entry: ScheduledOnce) -> None:
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        _UPSERT_SCHEDULED,
                        (
                            entry.run_key,
                            entry.func_name,
                            entry.fire_at.isoformat(),
                            entry.created_at.isoformat(),
                            json.dumps(list(entry.args), default=repr),
                            json.dumps(entry.kwargs, default=repr),
                            entry.encrypted_payload.decode()
                            if entry.encrypted_payload
                            else None,
                            entry.queue,
                            entry.priority,
                            entry.idempotency_key,
                            json.dumps(entry.tags) if entry.tags else None,
                        ),
                    )

    def _load_due_sync(self, before: str) -> list[ScheduledOnce]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {SCHEDULED_COLUMNS} FROM task_scheduled_once "
                    "WHERE fire_at <= %s ORDER BY fire_at",
                    (before,),
                )
                rows = cur.fetchall()
        return [row_to_scheduled(r) for r in rows]

    def _delete_scheduled_sync(self, run_key: str) -> bool:
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM task_scheduled_once WHERE run_key = %s",
                        (run_key,),
                    )
                    return cur.rowcount == 1

    def _check_idempotency_key_sync(self, key: str) -> str | None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM task_idempotency_keys WHERE idem_key = %s",
                    (key,),
                )
                row = cur.fetchone()
        return row[0] if row else None

    def _record_idempotency_key_sync(self, key: str, task_id: str) -> None:
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO task_idempotency_keys (idem_key, task_id, created_at)"
                        " VALUES (%s, %s, %s) ON CONFLICT (idem_key) DO NOTHING",
                        (key, task_id, datetime.utcnow().isoformat()),
                    )

    def _delete_before_sync(self, cutoff: str) -> int:
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM task_snapshots"
                        " WHERE end_time IS NOT NULL AND end_time < %s"
                        " AND status IN ('success', 'failed', 'interrupted')",
                        (cutoff,),
                    )
                    return cur.rowcount

    def _delete_records_sync(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join(["%s"] * len(task_ids))
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM task_snapshots WHERE task_id IN ({placeholders})",
                        task_ids,
                    )
                    return cur.rowcount

    def _completed_ids_sync(self, task_ids: list[str]) -> set[str]:
        placeholders = ",".join(["%s"] * len(task_ids))
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT task_id FROM task_snapshots"
                    f" WHERE task_id IN ({placeholders}) AND status = 'success'",
                    task_ids,
                )
                return {row[0] for row in cur.fetchall()}

    def _acquire_schedule_lock_sync(self, key: str, ttl: int) -> bool:
        now = datetime.utcnow()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        now_iso = now.isoformat()
        with self._get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM task_schedule_locks"
                        " WHERE lock_key = %s AND expires_at <= %s",
                        (key, now_iso),
                    )
                    cur.execute(
                        "INSERT INTO task_schedule_locks (lock_key, expires_at)"
                        " VALUES (%s, %s) ON CONFLICT (lock_key) DO NOTHING",
                        (key, expires_at),
                    )
                    return cur.rowcount == 1

    async def close(self) -> None:
        self._executor.shutdown(wait=False)
        if self._owns_pool:
            self._pool.closeall()


# ------------------------------------------------------------------
# Shared row-to-record helpers
# ------------------------------------------------------------------


def _row_to_record(d: dict) -> TaskRecord:
    enc = d.get("encrypted_payload")
    return TaskRecord(
        task_id=d["task_id"],
        func_name=d["func_name"],
        status=TaskStatus(d["status"]),
        created_at=(
            datetime.fromisoformat(d["created_at"])
            if d.get("created_at")
            else datetime.utcnow()
        ),
        start_time=(
            datetime.fromisoformat(d["start_time"]) if d.get("start_time") else None
        ),
        end_time=(datetime.fromisoformat(d["end_time"]) if d.get("end_time") else None),
        retries_used=d.get("retries_used") or 0,
        error=d.get("error"),
        args=tuple(json.loads(d["args_json"])) if d.get("args_json") else (),
        kwargs=json.loads(d["kwargs_json"]) if d.get("kwargs_json") else {},
        logs=json.loads(d["logs_json"]) if d.get("logs_json") else [],
        stacktrace=d.get("stacktrace"),
        encrypted_payload=enc.encode() if enc else None,
        source=d.get("source") or "manual",
        priority=d.get("priority"),
        executor=d.get("executor"),
        queue=d.get("queue") or "default",
    )


def _row_to_pending_record(d: dict) -> TaskRecord:
    enc = d.get("encrypted_payload")
    return TaskRecord(
        task_id=d["task_id"],
        func_name=d["func_name"],
        status=TaskStatus.PENDING,
        created_at=(
            datetime.fromisoformat(d["created_at"])
            if d.get("created_at")
            else datetime.utcnow()
        ),
        retries_used=d.get("retries_used") or 0,
        args=tuple(json.loads(d["args_json"])) if d.get("args_json") else (),
        kwargs=json.loads(d["kwargs_json"]) if d.get("kwargs_json") else {},
        encrypted_payload=enc.encode() if enc else None,
        queue=d.get("queue") or "default",
    )
