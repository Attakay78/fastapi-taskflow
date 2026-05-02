"""Pluggable snapshot backends for fastapi-taskflow.

Built-in backends:

* :class:`SqliteBackend`   -- zero-dependency, local SQLite file (default)
* :class:`RedisBackend`    -- shared Redis store; requires ``pip install "fastapi-taskflow[redis]"``
* :class:`PostgresBackend` -- PostgreSQL; requires ``pip install "fastapi-taskflow[postgres]"``
* :class:`MySQLBackend`    -- MySQL/MariaDB; requires ``pip install "fastapi-taskflow[mysql]"``

To write a custom backend, subclass :class:`SnapshotBackend` and implement
its abstract methods::

    from fastapi_taskflow.backends import SnapshotBackend
    from fastapi_taskflow.models import TaskRecord

    class MyBackend(SnapshotBackend):
        async def save(self, records: list[TaskRecord]) -> int: ...
        async def load(self) -> list[TaskRecord]: ...
        async def save_pending(self, records: list[TaskRecord]) -> int: ...
        async def load_pending(self) -> list[TaskRecord]: ...
        async def clear_pending(self) -> None: ...
        async def close(self) -> None: ...

    task_manager = TaskManager(snapshot_backend=MyBackend(...))
"""

from .base import SnapshotBackend
from .mysql import MySQLBackend
from .postgres import PostgresBackend
from .redis import RedisBackend
from .sqlite import SqliteBackend

__all__ = [
    "SnapshotBackend",
    "SqliteBackend",
    "RedisBackend",
    "PostgresBackend",
    "MySQLBackend",
]
