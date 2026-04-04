"""SQLite snapshot backend (zero extra dependencies)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from .base import SnapshotBackend

if TYPE_CHECKING:
    from ..models import TaskRecord

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS task_snapshots (
    task_id        TEXT    PRIMARY KEY,
    func_name      TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    created_at     TEXT,
    start_time     TEXT,
    end_time       TEXT,
    duration       REAL,
    retries_used   INTEGER DEFAULT 0,
    error          TEXT,
    snapshotted_at TEXT,
    args_json      TEXT,
    kwargs_json    TEXT
)
"""

# Separate table for tasks that were pending at shutdown and need requeue.
_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS task_pending_requeue (
    task_id     TEXT PRIMARY KEY,
    func_name   TEXT NOT NULL,
    created_at  TEXT,
    retries_used INTEGER DEFAULT 0,
    args_json   TEXT,
    kwargs_json TEXT
)
"""

# Migrations applied to databases created before a column existed.
_MIGRATIONS = [
    "ALTER TABLE task_snapshots ADD COLUMN args_json TEXT",
    "ALTER TABLE task_snapshots ADD COLUMN kwargs_json TEXT",
]

_UPSERT_HISTORY = """
INSERT OR REPLACE INTO task_snapshots
    (task_id, func_name, status, created_at, start_time, end_time,
     duration, retries_used, error, snapshotted_at, args_json, kwargs_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_PENDING = """
INSERT OR REPLACE INTO task_pending_requeue
    (task_id, func_name, created_at, retries_used, args_json, kwargs_json)
VALUES (?, ?, ?, ?, ?, ?)
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
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(_CREATE_HISTORY)
            conn.execute(_CREATE_PENDING)
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists

    def _save_sync(self, records: "list[TaskRecord]") -> int:
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
            )
            for t in records
        ]
        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(_UPSERT_HISTORY, rows)
        return len(rows)

    def _save_pending_sync(self, records: "list[TaskRecord]") -> int:
        rows = [
            (
                t.task_id,
                t.func_name,
                t.created_at.isoformat(),
                t.retries_used,
                json.dumps(list(t.args), default=repr),
                json.dumps(t.kwargs, default=repr),
            )
            for t in records
        ]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM task_pending_requeue")
            if rows:
                conn.executemany(_UPSERT_PENDING, rows)
        return len(rows)

    def _load_pending_sync(self) -> "list[TaskRecord]":
        from ..models import TaskRecord, TaskStatus

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM task_pending_requeue").fetchall()

        records: list[TaskRecord] = []
        for row in rows:
            d = dict(row)
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
                )
            )
        return records

    def _clear_pending_sync(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM task_pending_requeue")

    def _load_sync(self) -> "list[TaskRecord]":
        from ..models import TaskRecord, TaskStatus

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM task_snapshots").fetchall()

        records: list[TaskRecord] = []
        for row in rows:
            d = dict(row)
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
