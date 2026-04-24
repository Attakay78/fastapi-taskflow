"""Redis snapshot backend.

Requires the ``redis`` package with asyncio support::

    pip install "redis[asyncio]"

Each task is stored as a Redis hash under the key
``{prefix}:{task_id}`` and the full set of task IDs is tracked in a
Redis set at ``{prefix}:_index`` so loads are O(n) without a full
key-scan.

Example::

    from fastapi_taskflow import TaskManager
    from fastapi_taskflow.backends import RedisBackend

    task_manager = TaskManager(
        snapshot_backend=RedisBackend(url="redis://localhost:6379/0"),
        snapshot_interval=30.0,
    )
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .base import SnapshotBackend
from ..models import TaskRecord, TaskStatus

_INDEX_SUFFIX = ":_index"
_PENDING_SUFFIX = ":_pending"


class RedisBackend(SnapshotBackend):
    """
    Persist task snapshots in Redis.

    Each completed task is stored as a Redis hash.  All task IDs are
    tracked in a dedicated Redis set so that :meth:`load` can retrieve
    them without a ``SCAN`` command.

    Args:
        url: Redis connection URL (default ``"redis://localhost:6379/0"``).
        prefix: Key prefix applied to every key written by this backend
            (default ``"fbtm:snapshots"``).  Useful when sharing a Redis
            instance across multiple services.
        ttl: Optional TTL in seconds applied to each task hash.  ``None``
            (default) means keys never expire.
        **client_kwargs: Additional keyword arguments forwarded to
            ``redis.asyncio.from_url``.

    Note:
        The ``redis`` package is **not** installed by default.  Add it to
        your project with ``pip install "redis[asyncio]"``.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        prefix: str = "fbtm:snapshots",
        ttl: int | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._url = url
        self._prefix = prefix
        self._ttl = ttl
        self._client_kwargs = client_kwargs
        self._client: Any = None  # lazily initialised

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import redis.asyncio as aioredis  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ImportError(
                    "RedisBackend requires the 'redis' package. "
                    'Install it with: pip install "redis[asyncio]"'
                ) from exc
            self._client = aioredis.from_url(self._url, **self._client_kwargs)
        return self._client

    def _key(self, task_id: str) -> str:
        return f"{self._prefix}:{task_id}"

    def _index_key(self) -> str:
        return f"{self._prefix}{_INDEX_SUFFIX}"

    def _pending_key(self, task_id: str) -> str:
        return f"{self._prefix}{_PENDING_SUFFIX}:{task_id}"

    def _pending_index_key(self) -> str:
        return f"{self._prefix}{_PENDING_SUFFIX}:_index"

    @staticmethod
    def _record_to_mapping(record: "TaskRecord") -> dict[str, str]:
        """Serialise a TaskRecord to a flat string mapping (Redis hash fields)."""
        return {
            "task_id": record.task_id,
            "func_name": record.func_name,
            "status": record.status.value,
            "created_at": record.created_at.isoformat(),
            "start_time": record.start_time.isoformat() if record.start_time else "",
            "end_time": record.end_time.isoformat() if record.end_time else "",
            "duration": str(record.duration) if record.duration is not None else "",
            "retries_used": str(record.retries_used),
            "error": record.error or "",
            "args_json": json.dumps(list(record.args), default=repr),
            "kwargs_json": json.dumps(record.kwargs, default=repr),
            "logs_json": json.dumps(record.logs),
            "stacktrace": record.stacktrace or "",
            "encrypted_payload": record.encrypted_payload.decode()
            if record.encrypted_payload
            else "",
            "source": record.source,
        }

    @staticmethod
    def _mapping_to_record(mapping: dict[str, str]) -> "TaskRecord":
        return TaskRecord(
            task_id=mapping["task_id"],
            func_name=mapping["func_name"],
            status=TaskStatus(mapping["status"]),
            created_at=(
                datetime.fromisoformat(mapping["created_at"])
                if mapping.get("created_at")
                else datetime.utcnow()
            ),
            start_time=(
                datetime.fromisoformat(mapping["start_time"])
                if mapping.get("start_time")
                else None
            ),
            end_time=(
                datetime.fromisoformat(mapping["end_time"])
                if mapping.get("end_time")
                else None
            ),
            retries_used=int(mapping.get("retries_used", 0) or 0),
            error=mapping.get("error") or None,
            args=tuple(json.loads(mapping["args_json"]))
            if mapping.get("args_json")
            else (),
            kwargs=json.loads(mapping["kwargs_json"])
            if mapping.get("kwargs_json")
            else {},
            logs=json.loads(mapping["logs_json"]) if mapping.get("logs_json") else [],
            stacktrace=mapping.get("stacktrace") or None,
            encrypted_payload=(
                enc.encode() if (enc := mapping.get("encrypted_payload")) else None
            ),
            source=mapping.get("source") or "manual",
        )

    # ------------------------------------------------------------------
    # SnapshotBackend interface
    # ------------------------------------------------------------------

    async def save(self, records: "list[TaskRecord]") -> int:
        if not records:
            return 0

        client = self._get_client()
        pipe = client.pipeline()

        for record in records:
            key = self._key(record.task_id)
            pipe.hset(key, mapping=self._record_to_mapping(record))
            if self._ttl is not None:
                pipe.expire(key, self._ttl)
            pipe.sadd(self._index_key(), record.task_id)

        await pipe.execute()
        return len(records)

    async def load(self) -> "list[TaskRecord]":
        client = self._get_client()

        task_ids = await client.smembers(self._index_key())
        if not task_ids:
            return []

        pipe = client.pipeline()
        for task_id in task_ids:
            pipe.hgetall(
                self._key(task_id.decode() if isinstance(task_id, bytes) else task_id)
            )

        results = await pipe.execute()
        records: list[TaskRecord] = []
        for raw in results:
            if not raw:
                continue
            # redis-py returns bytes keys/values; decode them
            mapping = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in raw.items()
            }
            if mapping.get("task_id"):
                records.append(self._mapping_to_record(mapping))

        return records

    async def save_pending(self, records: "list[TaskRecord]") -> int:
        client = self._get_client()
        pipe = client.pipeline()

        # Clear the existing pending index and all its hashes first
        existing_ids = await client.smembers(self._pending_index_key())
        for tid in existing_ids:
            pipe.delete(
                self._pending_key(tid.decode() if isinstance(tid, bytes) else tid)
            )
        pipe.delete(self._pending_index_key())

        for record in records:
            key = self._pending_key(record.task_id)
            mapping = {
                "task_id": record.task_id,
                "func_name": record.func_name,
                "created_at": record.created_at.isoformat(),
                "retries_used": str(record.retries_used),
                "args_json": json.dumps(list(record.args), default=repr),
                "kwargs_json": json.dumps(record.kwargs, default=repr),
                "encrypted_payload": record.encrypted_payload.decode()
                if record.encrypted_payload
                else "",
            }
            pipe.hset(key, mapping=mapping)
            pipe.sadd(self._pending_index_key(), record.task_id)

        await pipe.execute()
        return len(records)

    async def load_pending(self) -> "list[TaskRecord]":
        client = self._get_client()
        task_ids = await client.smembers(self._pending_index_key())
        if not task_ids:
            return []

        pipe = client.pipeline()
        for tid in task_ids:
            pipe.hgetall(
                self._pending_key(tid.decode() if isinstance(tid, bytes) else tid)
            )

        results = await pipe.execute()
        records: list[TaskRecord] = []
        for raw in results:
            if not raw:
                continue
            d = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in raw.items()
            }
            if not d.get("task_id"):
                continue
            records.append(
                TaskRecord(
                    task_id=d["task_id"],
                    func_name=d["func_name"],
                    status=TaskStatus.PENDING,
                    created_at=(
                        datetime.fromisoformat(d["created_at"])
                        if d.get("created_at")
                        else datetime.utcnow()
                    ),
                    retries_used=int(d.get("retries_used", 0) or 0),
                    args=tuple(json.loads(d["args_json"]))
                    if d.get("args_json")
                    else (),
                    kwargs=json.loads(d["kwargs_json"]) if d.get("kwargs_json") else {},
                    encrypted_payload=(
                        enc.encode() if (enc := d.get("encrypted_payload")) else None
                    ),
                )
            )
        return records

    async def clear_pending(self) -> None:
        client = self._get_client()
        task_ids = await client.smembers(self._pending_index_key())
        pipe = client.pipeline()
        for tid in task_ids:
            pipe.delete(
                self._pending_key(tid.decode() if isinstance(tid, bytes) else tid)
            )
        pipe.delete(self._pending_index_key())
        await pipe.execute()

    async def claim_pending(self, task_id: str) -> bool:
        """Atomically delete the pending hash for task_id.

        Returns True if this caller deleted it (i.e. we own the task),
        False if another instance already claimed it.
        """
        client = self._get_client()
        pipe = client.pipeline()
        pipe.delete(self._pending_key(task_id))
        pipe.srem(self._pending_index_key(), task_id)
        results = await pipe.execute()
        # results[0] = number of keys deleted (1 if we got it, 0 if already gone)
        return results[0] == 1

    @staticmethod
    def _schedule_lock_key(key: str) -> str:
        return f"taskflow:schedule_lock:{key}"

    def _idem_key(self, key: str) -> str:
        return f"{self._prefix}:idem:{key}"

    async def check_idempotency_key(self, key: str) -> "str | None":
        client = self._get_client()
        value = await client.get(self._idem_key(key))
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else value

    async def record_idempotency_key(self, key: str, task_id: str) -> None:
        client = self._get_client()
        # NX = only set if not already recorded (first writer wins)
        await client.set(self._idem_key(key), task_id, nx=True)

    async def delete_before(self, cutoff: datetime) -> int:
        client = self._get_client()
        task_ids = await client.smembers(self._index_key())
        if not task_ids:
            return 0

        cutoff_iso = cutoff.isoformat()
        pipe = client.pipeline()
        for tid in task_ids:
            pipe.hmget(
                self._key(tid.decode() if isinstance(tid, bytes) else tid),
                "end_time",
                "status",
            )
        results = await pipe.execute()

        to_delete = []
        for tid, fields in zip(task_ids, results):
            end_time_raw, status_raw = fields
            if not end_time_raw or not status_raw:
                continue
            end_time = (
                end_time_raw.decode()
                if isinstance(end_time_raw, bytes)
                else end_time_raw
            )
            status = (
                status_raw.decode() if isinstance(status_raw, bytes) else status_raw
            )
            if status not in ("success", "failed", "interrupted"):
                continue
            if end_time < cutoff_iso:
                to_delete.append(tid.decode() if isinstance(tid, bytes) else tid)

        if not to_delete:
            return 0

        pipe = client.pipeline()
        for tid in to_delete:
            pipe.delete(self._key(tid))
            pipe.srem(self._index_key(), tid)
        await pipe.execute()
        return len(to_delete)

    async def completed_ids(self, task_ids: list[str]) -> set[str]:
        if not task_ids:
            return set()
        client = self._get_client()
        pipe = client.pipeline()
        for task_id in task_ids:
            pipe.hget(self._key(task_id), "status")
        results = await pipe.execute()
        return {
            task_id
            for task_id, status in zip(task_ids, results)
            if status is not None
            and (status.decode() if isinstance(status, bytes) else status) == "success"
        }

    async def acquire_schedule_lock(self, key: str, ttl: int) -> bool:
        """Acquire a distributed schedule lock using Redis SET NX.

        Args:
            key: Lock identifier.
            ttl: Lock lifetime in seconds.

        Returns:
            ``True`` if the lock was acquired, ``False`` otherwise.
        """
        client = self._get_client()
        result = await client.set(self._schedule_lock_key(key), "1", nx=True, ex=ttl)
        return result is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
