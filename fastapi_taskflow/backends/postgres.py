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

import asyncio
import json
from datetime import datetime, timedelta

from .base import SnapshotBackend
from ..models import TaskRecord, TaskStatus


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

_UPSERT_HISTORY = """
INSERT INTO task_snapshots
    (task_id, func_name, status, created_at, start_time, end_time,
     duration, retries_used, error, snapshotted_at, args_json, kwargs_json,
     logs_json, stacktrace, encrypted_payload, source, priority, executor)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    executor          = EXCLUDED.executor
"""

_UPSERT_PENDING = """
INSERT INTO task_pending_requeue
    (task_id, func_name, created_at, retries_used, args_json, kwargs_json, encrypted_payload)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (task_id) DO UPDATE SET
    func_name         = EXCLUDED.func_name,
    created_at        = EXCLUDED.created_at,
    retries_used      = EXCLUDED.retries_used,
    args_json         = EXCLUDED.args_json,
    kwargs_json       = EXCLUDED.kwargs_json,
    encrypted_payload = EXCLUDED.encrypted_payload
"""


class PostgresBackend(SnapshotBackend):
    """Persist task snapshots to a PostgreSQL database.

    Uses ``psycopg2`` with ``asyncio.to_thread`` so the async interface
    stays non-blocking without pulling in an additional async driver.

    Args:
        url: PostgreSQL connection string, e.g.
            ``"postgresql://user:pass@localhost:5432/mydb"``.

    Example::

        from fastapi_taskflow import TaskManager
        from fastapi_taskflow.backends import PostgresBackend

        task_manager = TaskManager(
            snapshot_backend=PostgresBackend(
                "postgresql://user:pass@localhost/mydb"
            ),
        )
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self):
        try:
            import psycopg2  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "PostgresBackend requires psycopg2. "
                "Install it with: pip install 'fastapi-taskflow[postgres]'"
            ) from exc
        return psycopg2.connect(self._url)

    def _init_db(self) -> None:
        """Create tables and apply any pending column migrations."""
        _migrations = [
            "ALTER TABLE task_snapshots ADD COLUMN IF NOT EXISTS executor TEXT",
        ]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(_CREATE_HISTORY)
                    cur.execute(_CREATE_PENDING)
                    cur.execute(_CREATE_IDEMPOTENCY)
                    cur.execute(_CREATE_SCHEDULE_LOCKS)
                    for migration in _migrations:
                        cur.execute(migration)
        finally:
            conn.close()

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
            )
            for t in records
        ]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.executemany(_UPSERT_HISTORY, rows)
        finally:
            conn.close()
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
            )
            for t in records
        ]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM task_pending_requeue")
                    if rows:
                        cur.executemany(_UPSERT_PENDING, rows)
        finally:
            conn.close()
        return len(records)

    def _load_sync(self) -> list[TaskRecord]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM task_snapshots")
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
        return [_row_to_record(d) for d in rows]

    def _load_pending_sync(self) -> list[TaskRecord]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM task_pending_requeue")
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
        return [_row_to_pending_record(d) for d in rows]

    def _clear_pending_sync(self) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM task_pending_requeue")
        finally:
            conn.close()

    def _claim_pending_sync(self, task_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM task_pending_requeue WHERE task_id = %s",
                        (task_id,),
                    )
                    return cur.rowcount == 1
        finally:
            conn.close()

    def _check_idempotency_key_sync(self, key: str) -> str | None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM task_idempotency_keys WHERE idem_key = %s",
                    (key,),
                )
                row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _record_idempotency_key_sync(self, key: str, task_id: str) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO task_idempotency_keys (idem_key, task_id, created_at)"
                        " VALUES (%s, %s, %s) ON CONFLICT (idem_key) DO NOTHING",
                        (key, task_id, datetime.utcnow().isoformat()),
                    )
        finally:
            conn.close()

    def _delete_before_sync(self, cutoff: str) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM task_snapshots"
                        " WHERE end_time IS NOT NULL AND end_time < %s"
                        " AND status IN ('success', 'failed', 'interrupted')",
                        (cutoff,),
                    )
                    return cur.rowcount
        finally:
            conn.close()

    def _completed_ids_sync(self, task_ids: list[str]) -> set[str]:
        placeholders = ",".join(["%s"] * len(task_ids))
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT task_id FROM task_snapshots"
                    f" WHERE task_id IN ({placeholders}) AND status = 'success'",
                    task_ids,
                )
                return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

    def _acquire_schedule_lock_sync(self, key: str, ttl: int) -> bool:
        now = datetime.utcnow()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        now_iso = now.isoformat()
        conn = self._connect()
        try:
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
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # SnapshotBackend interface
    # ------------------------------------------------------------------

    async def save(self, records: list[TaskRecord]) -> int:
        return await asyncio.to_thread(self._save_sync, records)

    async def load(self) -> list[TaskRecord]:
        return await asyncio.to_thread(self._load_sync)

    async def save_pending(self, records: list[TaskRecord]) -> int:
        return await asyncio.to_thread(self._save_pending_sync, records)

    async def load_pending(self) -> list[TaskRecord]:
        return await asyncio.to_thread(self._load_pending_sync)

    async def clear_pending(self) -> None:
        await asyncio.to_thread(self._clear_pending_sync)

    async def claim_pending(self, task_id: str) -> bool:
        return await asyncio.to_thread(self._claim_pending_sync, task_id)

    async def check_idempotency_key(self, key: str) -> str | None:
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
        pass  # connections are opened and closed per operation


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
    )
