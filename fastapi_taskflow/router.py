from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

if TYPE_CHECKING:
    from .manager import TaskManager


def create_router(
    task_manager: "TaskManager",
    prefix: str = "/tasks",
    secret_key: str | None = None,
) -> APIRouter:
    """
    Build and return a FastAPI router that exposes task observability endpoints.

    Routes (relative to *prefix*):

    * ``GET {prefix}``          — list all tasks
    * ``GET {prefix}/metrics``  — aggregated statistics
    * ``GET {prefix}/{task_id}``— single task detail
    """
    dependencies = []
    if secret_key is not None:
        from .auth import make_api_guard
        dependencies.append(make_api_guard(secret_key))

    router = APIRouter(prefix=prefix, tags=["tasks"], dependencies=dependencies)

    @router.get("", summary="List all tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return [t.to_dict() for t in task_manager.store.list()]

    # Define /metrics before /{task_id} so the fixed path wins.
    @router.get("/metrics", summary="Aggregated task statistics")
    def get_metrics() -> dict[str, Any]:
        tasks = task_manager.store.list()
        total = len(tasks)
        success = sum(1 for t in tasks if t.status.value == "success")
        failed = sum(1 for t in tasks if t.status.value == "failed")
        running = sum(1 for t in tasks if t.status.value == "running")
        pending = sum(1 for t in tasks if t.status.value == "pending")

        durations = [t.duration for t in tasks if t.duration is not None]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "running": running,
            "pending": pending,
            "success_rate": success / total if total > 0 else 0.0,
            "avg_duration_seconds": round(avg_duration, 6),
        }

    @router.get("/{task_id}", summary="Get a single task by ID")
    def get_task(task_id: str) -> dict[str, Any]:
        record = task_manager.store.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return record.to_dict()

    return router
