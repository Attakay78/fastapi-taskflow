"""SQLite snapshot backend (zero extra dependencies)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from .base import ThreadedSnapshotBackend
from ..models import ScheduledOnce, TaskRecord, TaskStatus

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS task_snapshots (
    task_id           TEXT    PRIMARY KEY,
    func_name         TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    created_at        TEXT,
    start_time        TEXT,
    end_time          TEXT,
    duration          REAL,
    retries_used      INTEGER DEFAULT 0,
    error             TEXT,
    snapshotted_at    TEXT,
    args_json         TEXT,
    kwargs_json       TEXT,
    logs_json         TEXT,
    stacktrace        TEXT,
    encrypted_payload TEXT,
    source            TEXT DEFAULT 'manual',
    executor          TEXT,
    queue             TEXT DEFAULT 'default'
)
"""

# Separate table for tasks that were pending at shutdown and need requeue.
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
    idem_key    TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL
)
"""

_CREATE_SCHEDULE_LOCKS = """
CREATE TABLE IF NOT EXISTS task_schedule_locks (
    lock_key   TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
)
"""

_CREATE_METADATA = """
CREATE TABLE IF NOT EXISTS task_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

# Migrations applied to databases created before a column existed.
_MIGRATIONS = [
    "ALTER TABLE task_snapshots ADD COLUMN args_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN kwargs_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN logs_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN stacktrace TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN encrypted_payload TEXT",
    "ALTER TABLE task_pending_requeue ADD COLUMN encrypted_payload TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN source TEXT DEFAULT 'manual'",
    "ALTER TABLE task_snapshots ADD COLUMN priority INTEGER",
    "ALTER TABLE task_snapshots ADD COLUMN executor TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN queue TEXT DEFAULT 'default'",
    "ALTER TABLE task_pending_requeue ADD COLUMN queue TEXT DEFAULT 'default'",
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snap_status ON task_snapshots(status)",
    "CREATE INDEX IF NOT EXISTS idx_snap_end_status ON task_snapshots(end_time, status)",
    "CREATE INDEX IF NOT EXISTS idx_snap_snapshotted ON task_snapshots(snapshotted_at)",
    "CREATE INDEX IF NOT EXISTS idx_snap_func_name ON task_snapshots(func_name)",
    "CREATE INDEX IF NOT EXISTS idx_sched_fire_at ON task_scheduled_once(fire_at)",
]

_UPSERT_HISTORY = """
INSERT OR REPLACE INTO task_snapshots
    (task_id, func_name, status, created_at, start_time, end_time,
     duration, retries_used, error, snapshotted_at, args_json, kwargs_json,
     logs_json, stacktrace, encrypted_payload, source, priority, executor, queue)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_PENDING = """
INSERT OR REPLACE INTO task_pending_requeue
    (task_id, func_name, created_at, retries_used, args_json, kwargs_json,
     encrypted_payload, queue)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

# REPLACE gives reschedule-by-run_key its semantics: scheduling again with an
# existing run_key overwrites fire_at rather than creating a second firing.
_UPSERT_SCHEDULED = """
INSERT OR REPLACE INTO task_scheduled_once
    (run_key, func_name, fire_at, created_at, args_json, kwargs_json,
     encrypted_payload, queue, priority, idempotency_key, tags_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class SqliteBackend(ThreadedSnapshotBackend):
    """
    Persist task snapshots to a local SQLite file.

    This is the default backend and requires no additional packages.

    Args:
        db_path: Path to the SQLite file (created automatically if absent).
        max_workers: Thread pool size for offloading sync operations (default 4).
            Each thread holds one persistent connection, so this also caps the
            number of open file handles.

    Example::

        from fastapi_taskflow import TaskManager
        from fastapi_taskflow.backends import SqliteBackend

        task_manager = TaskManager(
            snapshot_backend=SqliteBackend("tasks.db"),
            snapshot_interval=30.0,
        )

    The shorthand ``TaskManager(snapshot_db="tasks.db")`` is equivalent and
    remains fully supported for backwards compatibility.
    """

    supports_scheduled_once = True

    def __init__(self, db_path: str = "tasks.db", *, max_workers: int = 4) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix="taskflow-sqlite")
        self._db_path = db_path
        self._local = threading.local()
        self._all_conns: list = []
        self._conns_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            with self._conns_lock:
                self._all_conns.append(conn)
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        """Create tables and apply any pending schema migrations.

        Uses WAL journal mode so multiple processes can read the database
        concurrently while one writer is active (important for same-host
        multi-instance deployments sharing a single SQLite file).
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_HISTORY)
            conn.execute(_CREATE_PENDING)
            conn.execute(_CREATE_IDEMPOTENCY)
            conn.execute(_CREATE_SCHEDULE_LOCKS)
            conn.execute(_CREATE_METADATA)
            conn.execute(_CREATE_SCHEDULED)
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists
            for index in _INDEXES:
                conn.execute(index)

    def _save_sync(self, records: "list[TaskRecord]") -> int:
        now = datetime.utcnow().isoformat()
        conn = self._get_conn()
        with conn:
            conn.executemany(
                _UPSERT_HISTORY,
                (
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
                ),
            )
        return len(records)

    def _save_pending_sync(self, records: "list[TaskRecord]") -> int:
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM task_pending_requeue")
            if records:
                conn.executemany(
                    _UPSERT_PENDING,
                    (
                        (
                            t.task_id,
                            t.func_name,
                            t.created_at.isoformat(),
                            t.retries_used,
                            json.dumps(list(t.args), default=repr),
                            json.dumps(t.kwargs, default=repr),
                            t.encrypted_payload.decode()
                            if t.encrypted_payload
                            else None,
                            t.queue,
                        )
                        for t in records
                    ),
                )
        return len(records)

    def _load_pending_sync(self) -> "list[TaskRecord]":
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM task_pending_requeue")
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        records: list[TaskRecord] = []
        for d in rows:
            enc = d.get("encrypted_payload")
            records.append(
                TaskRecord(
                    task_id=d["task_id"],
                    func_name=d["func_name"],
                    status=TaskStatus.PENDING,
                    created_at=(
                        datetime.fromisoformat(d["created_at"])
                        if d["created_at"]
                        else datetime.utcnow()
                    ),
                    retries_used=d["retries_used"] or 0,
                    args=tuple(json.loads(d["args_json"]))
                    if d.get("args_json")
                    else (),
                    kwargs=json.loads(d["kwargs_json"]) if d.get("kwargs_json") else {},
                    encrypted_payload=enc.encode() if enc else None,
                    queue=d.get("queue") or "default",
                )
            )
        return records

    def _clear_pending_sync(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM task_pending_requeue")

    def _claim_pending_sync(self, task_id: str) -> bool:
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM task_pending_requeue WHERE task_id = ?", (task_id,)
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # One-off schedules
    # ------------------------------------------------------------------

    def _save_scheduled_sync(self, entry: "ScheduledOnce") -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
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

    def _load_due_sync(self, before: str) -> "list[ScheduledOnce]":
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM task_scheduled_once WHERE fire_at <= ? ORDER BY fire_at",
            (before,),
        )
        cols = [desc[0] for desc in cur.description]
        entries: list[ScheduledOnce] = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            enc = d.get("encrypted_payload")
            entries.append(
                ScheduledOnce(
                    run_key=d["run_key"],
                    func_name=d["func_name"],
                    fire_at=datetime.fromisoformat(d["fire_at"]),
                    created_at=(
                        datetime.fromisoformat(d["created_at"])
                        if d.get("created_at")
                        else datetime.now(timezone.utc)
                    ),
                    args=tuple(json.loads(d["args_json"]))
                    if d.get("args_json")
                    else (),
                    kwargs=json.loads(d["kwargs_json"]) if d.get("kwargs_json") else {},
                    encrypted_payload=enc.encode() if enc else None,
                    queue=d.get("queue") or "default",
                    priority=d.get("priority"),
                    idempotency_key=d.get("idempotency_key"),
                    tags=json.loads(d["tags_json"]) if d.get("tags_json") else {},
                )
            )
        return entries

    def _delete_scheduled_sync(self, run_key: str) -> bool:
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM task_scheduled_once WHERE run_key = ?", (run_key,)
            )
            return cur.rowcount == 1

    def _check_idempotency_key_sync(self, key: str) -> "str | None":
        conn = self._get_conn()
        row = conn.execute(
            "SELECT task_id FROM task_idempotency_keys WHERE idem_key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _record_idempotency_key_sync(self, key: str, task_id: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_idempotency_keys (idem_key, task_id, created_at)"
                " VALUES (?, ?, ?)",
                (key, task_id, datetime.utcnow().isoformat()),
            )

    def _delete_records_sync(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" * len(task_ids))
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                f"DELETE FROM task_snapshots WHERE task_id IN ({placeholders})",
                task_ids,
            )
            return cur.rowcount

    def _delete_before_sync(self, cutoff: str) -> int:
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM task_snapshots"
                " WHERE end_time IS NOT NULL AND end_time < ?"
                " AND status IN ('success', 'failed', 'interrupted')",
                (cutoff,),
            )
            return cur.rowcount

    def _completed_ids_sync(self, task_ids: list[str]) -> set[str]:
        placeholders = ",".join("?" * len(task_ids))
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT task_id FROM task_snapshots WHERE task_id IN ({placeholders})"
            " AND status = 'success'",
            task_ids,
        ).fetchall()
        return {row[0] for row in rows}

    def _acquire_schedule_lock_sync(self, key: str, ttl: int) -> bool:
        now = datetime.utcnow()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        now_iso = now.isoformat()
        conn = self._get_conn()
        with conn:
            conn.execute(
                "DELETE FROM task_schedule_locks WHERE lock_key = ? AND expires_at <= ?",
                (key, now_iso),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_schedule_locks (lock_key, expires_at)"
                " VALUES (?, ?)",
                (key, expires_at),
            )
            return cur.rowcount == 1

    def _load_sync(self) -> "list[TaskRecord]":
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM task_snapshots")
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        records: list[TaskRecord] = []
        for d in rows:
            enc = d.get("encrypted_payload")
            records.append(
                TaskRecord(
                    task_id=d["task_id"],
                    func_name=d["func_name"],
                    status=TaskStatus(d["status"]),
                    created_at=(
                        datetime.fromisoformat(d["created_at"])
                        if d["created_at"]
                        else datetime.utcnow()
                    ),
                    start_time=(
                        datetime.fromisoformat(d["start_time"])
                        if d["start_time"]
                        else None
                    ),
                    end_time=(
                        datetime.fromisoformat(d["end_time"]) if d["end_time"] else None
                    ),
                    retries_used=d["retries_used"] or 0,
                    error=d["error"],
                    args=tuple(json.loads(d["args_json"]))
                    if d.get("args_json")
                    else (),
                    kwargs=json.loads(d["kwargs_json"]) if d.get("kwargs_json") else {},
                    logs=json.loads(d["logs_json"]) if d.get("logs_json") else [],
                    stacktrace=d.get("stacktrace"),
                    encrypted_payload=enc.encode() if enc else None,
                    source=d.get("source") or "manual",
                    priority=d.get("priority"),
                    executor=d.get("executor"),
                    queue=d.get("queue") or "default",
                )
            )
        return records

    async def save_metadata(self, key: str, value: str) -> None:
        def _sync() -> None:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO task_metadata (key, value) VALUES (?, ?)",
                    (key, value),
                )

        await self._run(_sync)

    async def load_metadata(self, key: str) -> "str | None":
        def _sync() -> "str | None":
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value FROM task_metadata WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

        return await self._run(_sync)

    async def close(self) -> None:
        self._executor.shutdown(wait=True)
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()

    # ------------------------------------------------------------------
    # Query helper (SQLite-specific — not part of the base protocol)
    # ------------------------------------------------------------------

    def query(
        self,
        status: str | None = None,
        func_name: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Query historical task records directly from SQLite.

        Args:
            status: Filter by status (``"success"`` / ``"failed"``).
            func_name: Filter by function name.
            limit: Maximum rows to return.

        Returns:
            List of dicts, newest first.
        """
        sql = "SELECT * FROM task_snapshots WHERE 1=1"
        params: list = []

        if status:
            sql += " AND status = ?"
            params.append(status)
        if func_name:
            sql += " AND func_name = ?"
            params.append(func_name)

        sql += " ORDER BY snapshotted_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        return [dict(row) for row in rows]
