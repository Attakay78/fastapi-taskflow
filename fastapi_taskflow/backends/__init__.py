"""
Pluggable snapshot backends for fastapi-taskflow.

Built-in backends
-----------------
* :class:`SqliteBackend`  — zero-dependency SQLite (default)
* :class:`RedisBackend`   — requires ``redis[asyncio]`` (``pip install redis``)

Custom backends
---------------
Subclass :class:`SnapshotBackend` and implement the four abstract methods::

    from fastapi_taskflow.backends import SnapshotBackend
    from fastapi_taskflow.models import TaskRecord

    class MyBackend(SnapshotBackend):
        async def save(self, records: list[TaskRecord]) -> int: ...
        async def load(self) -> list[TaskRecord]: ...
        async def close(self) -> None: ...

    task_manager = TaskManager(snapshot_backend=MyBackend(...))
"""

from .base import SnapshotBackend
from .redis import RedisBackend
from .sqlite import SqliteBackend

__all__ = ["SnapshotBackend", "SqliteBackend", "RedisBackend"]
