"""SQLite snapshot backend (zero extra dependencies)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from .base import SnapshotBackend
from ..models import TaskRecord, TaskStatus

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
    source            TEXT DEFAULT 'manual'
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

# Migrations applied to databases created before a column existed.
_MIGRATIONS = [
    "ALTER TABLE task_snapshots ADD COLUMN args_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN kwargs_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN logs_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN stacktrace TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN encrypted_payload TEXT",
    "ALTER TABLE task_pending_requeue ADD COLUMN encrypted_payload TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN source TEXT DEFAULT 'manual'",
]

_UPSERT_HISTORY = """
INSERT OR REPLACE INTO task_snapshots
    (task_id, func_name, status, created_at, start_time, end_time,
     duration, retries_used, error, snapshotted_at, args_json, kwargs_json,
     logs_json, stacktrace, encrypted_payload, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_PENDING = """
INSERT OR REPLACE INTO task_pending_requeue
    (task_id, func_name, created_at, retries_used, args_json, kwargs_json,
     encrypted_payload)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


class SqliteBackend(SnapshotBackend):
    """
    Persist task snapshots to a local SQLite file.

    This is the default backend and requires no additional packages.

    Args:
        db_path: Path to the SQLite file (created automatically if absent).

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

    def __init__(self, db_path: str = "tasks.db") -> None:
        self._db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers (synchronous — offloaded to a thread by the caller)
    # ------------------------------------------------------------------

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
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists

    def _save_sync(self, records: "list[TaskRecord]") -> int:
        """Upsert *records* into the history table. Returns the number written."""
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self._db_path) as conn:
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
                    )
                    for t in records
                ),
            )
        return len(records)

    def _save_pending_sync(self, records: "list[TaskRecord]") -> int:
        """Replace the pending table with *records*. Returns the number written."""
        with sqlite3.connect(self._db_path) as conn:
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
                        )
                        for t in records
                    ),
                )
        return len(records)

    def _load_pending_sync(self) -> "list[TaskRecord]":
        """Read all rows from the pending table and return them as TaskRecord objects."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM task_pending_requeue").fetchall()

        records: list[TaskRecord] = []
        for row in rows:
            d = dict(row)
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
                )
            )
        return records

    def _clear_pending_sync(self) -> None:
        """Delete all rows from the pending table."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM task_pending_requeue")

    def _claim_pending_sync(self, task_id: str) -> bool:
        """Delete the pending row for *task_id* and return ``True`` if this call deleted it.

        SQLite's ``DELETE`` reports ``rowcount == 0`` when the row was already gone,
        which means another process claimed it first.
        """
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM task_pending_requeue WHERE task_id = ?", (task_id,)
            )
            return cur.rowcount == 1

    def _check_idempotency_key_sync(self, key: str) -> "str | None":
        """Return the ``task_id`` stored for *key*, or ``None`` if not found."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT task_id FROM task_idempotency_keys WHERE idem_key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _record_idempotency_key_sync(self, key: str, task_id: str) -> None:
        """Insert *key* -> *task_id* into the idempotency table (ignored if already present)."""
        from datetime import datetime

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_idempotency_keys (idem_key, task_id, created_at)"
                " VALUES (?, ?, ?)",
                (key, task_id, datetime.utcnow().isoformat()),
            )

    def _delete_before_sync(self, cutoff: str) -> int:
        """Delete terminal records older than *cutoff* (ISO format). Returns count deleted."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM task_snapshots"
                " WHERE end_time IS NOT NULL AND end_time < ?"
                " AND status IN ('success', 'failed', 'interrupted')",
                (cutoff,),
            )
            return cur.rowcount

    def _completed_ids_sync(self, task_ids: list[str]) -> set[str]:
        """Return the subset of *task_ids* that exist in history with success status."""
        placeholders = ",".join("?" * len(task_ids))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT task_id FROM task_snapshots WHERE task_id IN ({placeholders})"
                " AND status = 'success'",
                task_ids,
            ).fetchall()
        return {row[0] for row in rows}

    def _acquire_schedule_lock_sync(self, key: str, ttl: int) -> bool:
        """Try to insert a lock row, replacing it if it has expired.

        Returns ``True`` if the lock was acquired, ``False`` if another
        instance holds a live lock for *key*.
        """
        now = datetime.utcnow()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        now_iso = now.isoformat()
        with sqlite3.connect(self._db_path) as conn:
            # Delete any expired lock for this key first.
            conn.execute(
                "DELETE FROM task_schedule_locks WHERE lock_key = ? AND expires_at <= ?",
                (key, now_iso),
            )
            try:
                conn.execute(
                    "INSERT INTO task_schedule_locks (lock_key, expires_at) VALUES (?, ?)",
                    (key, expires_at),
                )
                return True
            except sqlite3.IntegrityError:
                # Another instance holds a live (non-expired) lock.
                return False

    def _load_sync(self) -> "list[TaskRecord]":
        """Read all rows from the history table and return them as TaskRecord objects."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM task_snapshots").fetchall()

        records: list[TaskRecord] = []
        for row in rows:
            d = dict(row)
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
                )
            )
        return records

    # ------------------------------------------------------------------
    # SnapshotBackend interface
    # ------------------------------------------------------------------

    async def save(self, records: "list[TaskRecord]") -> int:
        return await asyncio.to_thread(self._save_sync, records)

    async def load(self) -> "list[TaskRecord]":
        return await asyncio.to_thread(self._load_sync)

    async def save_pending(self, records: "list[TaskRecord]") -> int:
        return await asyncio.to_thread(self._save_pending_sync, records)

    async def load_pending(self) -> "list[TaskRecord]":
        return await asyncio.to_thread(self._load_pending_sync)

    async def clear_pending(self) -> None:
        await asyncio.to_thread(self._clear_pending_sync)

    async def claim_pending(self, task_id: str) -> bool:
        return await asyncio.to_thread(self._claim_pending_sync, task_id)

    async def check_idempotency_key(self, key: str) -> "str | None":
        return await asyncio.to_thread(self._check_idempotency_key_sync, key)

    async def record_idempotency_key(self, key: str, task_id: str) -> None:
        await asyncio.to_thread(self._record_idempotency_key_sync, key, task_id)

    async def delete_before(self, cutoff: datetime) -> int:
        return await asyncio.to_thread(self._delete_before_sync, cutoff.isoformat())

    async def completed_ids(self, task_ids: list[str]) -> set[str]:
        if not task_ids:
            return set()
        return await asyncio.to_thread(self._completed_ids_sync, task_ids)

    async def acquire_schedule_lock(self, key: str, ttl: int) -> bool:
        return await asyncio.to_thread(self._acquire_schedule_lock_sync, key, ttl)

    async def close(self) -> None:
        pass  # SQLite connections are opened/closed per-operation

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
