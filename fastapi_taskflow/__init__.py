"""
fastapi-taskflow
======================

A lightweight decorator-driven task manager that upgrades FastAPI
``BackgroundTasks`` with retries, visibility, and SQLite persistence —
without changing how developers enqueue tasks.

Quick start::

    from fastapi_taskflow import TaskManager, TaskAdmin

    task_manager = TaskManager(snapshot_db="tasks.db")

    @task_manager.task(retries=3, delay=2.0, backoff=2.0)
    def send_email(address: str) -> None:
        ...

    app = FastAPI()
    TaskAdmin(app, task_manager)                  # mounts /tasks routes + lifecycle

    @app.post("/signup")
    def signup(email: str, tasks=Depends(task_manager.get_tasks)):
        task_id = tasks.add_task(send_email, email)
        return {"task_id": task_id}
"""

from .admin import TaskAdmin
from .auth import TaskAuthBackend
from .backends import RedisBackend, SnapshotBackend, SqliteBackend
from .manager import TaskManager
from .models import TaskConfig, TaskRecord, TaskStatus
from .snapshot import SnapshotScheduler
from .task_logging import task_log
from .wrapper import ManagedBackgroundTasks

__all__ = [
    "TaskAdmin",
    "TaskAuthBackend",
    "TaskManager",
    "ManagedBackgroundTasks",
    "TaskStatus",
    "TaskRecord",
    "TaskConfig",
    "SnapshotScheduler",
    "SnapshotBackend",
    "SqliteBackend",
    "RedisBackend",
    "task_log",
]

__version__ = "0.1.0"
