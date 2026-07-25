"""MySQL snapshot backend.

Requires ``PyMySQL``::

    pip install "fastapi-taskflow[mysql]"

Usage::

    from fastapi_taskflow import TaskManager
    from fastapi_taskflow.backends import MySQLBackend

    task_manager = TaskManager(
        snapshot_backend=MySQLBackend("mysql://user:pass@localhost/mydb"),
        snapshot_interval=30.0,
    )
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from .base import SCHEDULED_COLUMNS, ThreadedSnapshotBackend, row_to_scheduled
from ..models import ScheduledOnce, TaskRecord, TaskStatus


_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS task_snapshots (
    task_id           VARCHAR(64)      NOT NULL PRIMARY KEY,
    func_name         TEXT             NOT NULL,
    status            VARCHAR(32)      NOT NULL,
    created_at        TEXT,
    start_time        TEXT,
    end_time          TEXT,
    duration          DOUBLE,
    retries_used      INT              DEFAULT 0,
    error             TEXT,
    snapshotted_at    TEXT,
    args_json         TEXT,
    kwargs_json       TEXT,
    logs_json         TEXT,
    stacktrace        TEXT,
    encrypted_payload TEXT,
    source            VARCHAR(32)      DEFAULT 'manual',
    priority          INT,
    executor          VARCHAR(32),
    queue             VARCHAR(128)     DEFAULT 'default'
)
"""

_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS task_pending_requeue (
    task_id           VARCHAR(64) NOT NULL PRIMARY KEY,
    func_name         TEXT        NOT NULL,
    created_at        TEXT,
    retries_used      INT         DEFAULT 0,
    args_json         TEXT,
    kwargs_json       TEXT,
    encrypted_payload TEXT
)
"""

_CREATE_IDEMPOTENCY = """
CREATE TABLE IF NOT EXISTS task_idempotency_keys (
    idem_key   VARCHAR(255) NOT NULL PRIMARY KEY,
    task_id    VARCHAR(64)  NOT NULL,
    created_at TEXT         NOT NULL
)
"""

_CREATE_SCHEDULE_LOCKS = """
CREATE TABLE IF NOT EXISTS task_schedule_locks (
    lock_key   VARCHAR(255) NOT NULL PRIMARY KEY,
    expires_at TEXT         NOT NULL
)
"""

# One-off tasks scheduled to fire at an exact future time. Keyed by the
# caller-supplied run_key so rescheduling replaces in place. fire_at is
# indexed because the scheduler queries it every refill tick.
_CREATE_SCHEDULED = """
CREATE TABLE IF NOT EXISTS task_scheduled_once (
    run_key           VARCHAR(191) NOT NULL PRIMARY KEY,
    func_name         TEXT         NOT NULL,
    fire_at           VARCHAR(64)  NOT NULL,
    created_at        TEXT,
    args_json         TEXT,
    kwargs_json       TEXT,
    encrypted_payload TEXT,
    queue             VARCHAR(128) DEFAULT 'default',
    priority          INT,
    idempotency_key   VARCHAR(191),
    tags_json         TEXT
)
"""

_INDEXES = [
    "CREATE INDEX idx_snap_status ON task_snapshots(status)",
    "CREATE INDEX idx_snap_end_status ON task_snapshots(end_time(32), status)",
    "CREATE INDEX idx_snap_snapshotted ON task_snapshots(snapshotted_at(32))",
    "CREATE INDEX idx_snap_func_name ON task_snapshots(func_name(191))",
    "CREATE INDEX idx_sched_fire_at ON task_scheduled_once(fire_at)",
]

_UPSERT_SCHEDULED = """
INSERT INTO task_scheduled_once
    (run_key, func_name, fire_at, created_at, args_json, kwargs_json,
     encrypted_payload, queue, priority, idempotency_key, tags_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    func_name         = VALUES(func_name),
    fire_at           = VALUES(fire_at),
    created_at        = VALUES(created_at),
    args_json         = VALUES(args_json),
    kwargs_json       = VALUES(kwargs_json),
    encrypted_payload = VALUES(encrypted_payload),
    queue             = VALUES(queue),
    priority          = VALUES(priority),
    idempotency_key   = VALUES(idempotency_key),
    tags_json         = VALUES(tags_json)
"""

_UPSERT_HISTORY = """
INSERT INTO task_snapshots
    (task_id, func_name, status, created_at, start_time, end_time,
     duration, retries_used, error, snapshotted_at, args_json, kwargs_json,
     logs_json, stacktrace, encrypted_payload, source, priority, executor, queue)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    func_name         = VALUES(func_name),
    status            = VALUES(status),
    created_at        = VALUES(created_at),
    start_time        = VALUES(start_time),
    end_time          = VALUES(end_time),
    duration          = VALUES(duration),
    retries_used      = VALUES(retries_used),
    error             = VALUES(error),
    snapshotted_at    = VALUES(snapshotted_at),
    args_json         = VALUES(args_json),
    kwargs_json       = VALUES(kwargs_json),
    logs_json         = VALUES(logs_json),
    stacktrace        = VALUES(stacktrace),
    encrypted_payload = VALUES(encrypted_payload),
    source            = VALUES(source),
    priority          = VALUES(priority),
    executor          = VALUES(executor),
    queue             = VALUES(queue)
"""

_UPSERT_PENDING = """
INSERT INTO task_pending_requeue
    (task_id, func_name, created_at, retries_used, args_json, kwargs_json, encrypted_payload, queue)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    func_name         = VALUES(func_name),
    created_at        = VALUES(created_at),
    retries_used      = VALUES(retries_used),
    args_json         = VALUES(args_json),
    kwargs_json       = VALUES(kwargs_json),
    encrypted_payload = VALUES(encrypted_payload),
    queue             = VALUES(queue)
"""


class MySQLBackend(ThreadedSnapshotBackend):
    """Persist task snapshots to a MySQL or MariaDB database.

    Uses ``PyMySQL`` with one persistent connection per executor thread and a
    dedicated ``ThreadPoolExecutor`` so backend I/O does not compete with the
    application's default asyncio executor.  Connections are created lazily on
    first use per thread and reused for the executor's lifetime.  A ``ping``
    on each borrow transparently reconnects sockets dropped by MySQL's
    ``wait_timeout``.

    Args:
        url: Connection string in the form ``mysql://user:pass@host:port/dbname``.
            Any query parameters are parsed and forwarded directly to
            ``pymysql.connect()``, so driver-level options such as
            ``ssl_ca``, ``ssl_verify_cert``, or ``connect_timeout`` can be
            passed inline: ``mysql://user:pass@host/db?ssl_ca=/path/to/ca.pem``.
            The MySQL CLI convention ``?ssl-mode=REQUIRED`` is also accepted
            and mapped to PyMySQL's ``ssl={}`` to enable SSL without requiring
            a certificate. Use PyMySQL spellings (underscores) for all other
            options.
        max_workers: Thread pool size for offloading sync operations (default 4).
            Each thread holds one persistent connection, so this also caps the
            number of open MySQL connections.

    Example::

        from fastapi_taskflow import TaskManager
        from fastapi_taskflow.backends import MySQLBackend

        task_manager = TaskManager(
            snapshot_backend=MySQLBackend("mysql://root:secret@localhost/mydb"),
        )

        # With SSL (cloud providers like Aiven):
        task_manager = TaskManager(
            snapshot_backend=MySQLBackend(
                "mysql://user:pass@host/mydb?ssl-mode=REQUIRED"
            ),
        )
    """

    supports_scheduled_once = True

    def __init__(self, url: str, *, max_workers: int = 4) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix="taskflow-mysql")
        self._connect_kwargs = self._parse_url(url)
        self._local = threading.local()
        self._all_conns: list = []
        self._conns_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_url(url: str) -> dict:
        parsed = urlparse(url)
        connect_kwargs: dict = dict(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username or "root",
            password=parsed.password or "",
            database=parsed.path.lstrip("/"),
        )
        if parsed.query:
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
                val = values[0] if len(values) == 1 else values
                if key == "ssl-mode":
                    if isinstance(val, str) and val.upper() != "DISABLED":
                        connect_kwargs.setdefault("ssl", {})
                elif "-" in key:
                    raise ValueError(
                        f"MySQLBackend: unsupported query parameter '{key}'. "
                        f"PyMySQL does not accept hyphenated parameter names. "
                        f"Use '{key.replace('-', '_')}' instead."
                    )
                else:
                    connect_kwargs[key] = val
        return connect_kwargs

    def _new_conn(self):
        try:
            import pymysql  # type: ignore[import-untyped]
            import pymysql.cursors  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "MySQLBackend requires PyMySQL. "
                "Install it with: pip install 'fastapi-taskflow[mysql]'"
            ) from exc
        conn = pymysql.connect(
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            **self._connect_kwargs,
        )
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @contextmanager
    def _get_conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._new_conn()
        try:
            self._local.conn.ping(reconnect=True)
        except Exception:
            self._local.conn = self._new_conn()
        yield self._local.conn

    @contextmanager
    def _transaction(self):
        with self._get_conn() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _init_db(self) -> None:
        """Create tables and apply any pending column migrations."""
        _migrations = [
            "ALTER TABLE task_snapshots ADD COLUMN executor VARCHAR(32)",
            "ALTER TABLE task_snapshots ADD COLUMN queue VARCHAR(128) DEFAULT 'default'",
            "ALTER TABLE task_pending_requeue ADD COLUMN queue VARCHAR(128) DEFAULT 'default'",
        ]
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_HISTORY)
                cur.execute(_CREATE_PENDING)
                cur.execute(_CREATE_IDEMPOTENCY)
                cur.execute(_CREATE_SCHEDULE_LOCKS)
                cur.execute(_CREATE_SCHEDULED)
                for migration in _migrations:
                    try:
                        cur.execute(migration)
                    except Exception:
                        pass  # column already exists
                for index in _INDEXES:
                    try:
                        cur.execute(index)
                    except Exception:
                        pass  # index already exists

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
        with self._transaction() as conn:
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
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM task_pending_requeue")
                if rows:
                    cur.executemany(_UPSERT_PENDING, rows)
        return len(records)

    def _load_sync(self) -> list[TaskRecord]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM task_snapshots")
                rows = cur.fetchall()
        return [_row_to_record(d) for d in rows]

    def _load_pending_sync(self) -> list[TaskRecord]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM task_pending_requeue")
                rows = cur.fetchall()
        return [_row_to_pending_record(d) for d in rows]

    def _clear_pending_sync(self) -> None:
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM task_pending_requeue")

    def _claim_pending_sync(self, task_id: str) -> bool:
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_pending_requeue WHERE task_id = %s",
                    (task_id,),
                )
                deleted = cur.rowcount
        return deleted == 1

    # ------------------------------------------------------------------
    # One-off schedules
    # ------------------------------------------------------------------

    def _save_scheduled_sync(self, entry: ScheduledOnce) -> None:
        with self._transaction() as conn:
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
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_scheduled_once WHERE run_key = %s",
                    (run_key,),
                )
                deleted = cur.rowcount
        return deleted == 1

    def _check_idempotency_key_sync(self, key: str) -> str | None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM task_idempotency_keys WHERE idem_key = %s",
                    (key,),
                )
                row = cur.fetchone()
        return row["task_id"] if row else None

    def _record_idempotency_key_sync(self, key: str, task_id: str) -> None:
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO task_idempotency_keys (idem_key, task_id, created_at)"
                    " VALUES (%s, %s, %s)",
                    (key, task_id, datetime.utcnow().isoformat()),
                )

    def _delete_before_sync(self, cutoff: str) -> int:
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_snapshots"
                    " WHERE end_time IS NOT NULL AND end_time < %s"
                    " AND status IN ('success', 'failed', 'interrupted')",
                    (cutoff,),
                )
                deleted = cur.rowcount
        return deleted

    def _delete_records_sync(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join(["%s"] * len(task_ids))
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM task_snapshots WHERE task_id IN ({placeholders})",
                    task_ids,
                )
                deleted = cur.rowcount
        return deleted

    def _completed_ids_sync(self, task_ids: list[str]) -> set[str]:
        placeholders = ",".join(["%s"] * len(task_ids))
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT task_id FROM task_snapshots"
                    f" WHERE task_id IN ({placeholders}) AND status = 'success'",
                    task_ids,
                )
                return {row["task_id"] for row in cur.fetchall()}

    def _acquire_schedule_lock_sync(self, key: str, ttl: int) -> bool:
        now = datetime.utcnow()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        now_iso = now.isoformat()
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_schedule_locks"
                    " WHERE lock_key = %s AND expires_at <= %s",
                    (key, now_iso),
                )
                cur.execute(
                    "INSERT IGNORE INTO task_schedule_locks (lock_key, expires_at)"
                    " VALUES (%s, %s)",
                    (key, expires_at),
                )
                acquired = cur.rowcount == 1
        return acquired

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
