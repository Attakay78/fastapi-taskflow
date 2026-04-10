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

    * ``GET {prefix}``           -- list all tasks
    * ``GET {prefix}/metrics``   -- aggregated statistics
    * ``GET {prefix}/{task_id}`` -- single task detail
    """
    dependencies = []
    if secret_key is not None:
        from .auth import make_api_guard

        dependencies.append(make_api_guard(secret_key))

    router = APIRouter(prefix=prefix, tags=["tasks"], dependencies=dependencies)

    @router.get("", summary="List all tasks")
    async def list_tasks() -> list[dict[str, Any]]:
        return [t.to_dict() for t in await task_manager.merged_list()]

    # Define /metrics before /{task_id} so the fixed path wins.
    @router.get("/metrics", summary="Aggregated task statistics")
    async def get_metrics() -> dict[str, Any]:
        tasks = await task_manager.merged_list()
        total = len(tasks)
        success = sum(1 for t in tasks if t.status.value == "success")
        failed = sum(1 for t in tasks if t.status.value == "failed")
        running = sum(1 for t in tasks if t.status.value == "running")
        pending = sum(1 for t in tasks if t.status.value == "pending")
        interrupted = sum(1 for t in tasks if t.status.value == "interrupted")

        durations = [t.duration for t in tasks if t.duration is not None]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "running": running,
            "pending": pending,
            "interrupted": interrupted,
            "success_rate": success / total if total > 0 else 0.0,
            "avg_duration_seconds": round(avg_duration, 6),
        }

    @router.get("/{task_id}", summary="Get a single task by ID")
    async def get_task(task_id: str) -> dict[str, Any]:
        # Check in-memory first (fastest path), then fall back to backend.
        record = task_manager.store.get(task_id)
        if record is None and task_manager._scheduler is not None:
            all_records = await task_manager.merged_list()
            record = next((r for r in all_records if r.task_id == task_id), None)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return record.to_dict()

    @router.post("/{task_id}/retry", summary="Retry a failed or interrupted task")
    async def retry_task(task_id: str) -> dict[str, Any]:
        """
        Re-enqueue a task that has status ``failed`` or ``interrupted``.

        The original task record is left unchanged in history. A new task is
        created with a fresh task ID using the same function, args, and kwargs
        as the original. Returns the new task record.

        Raises 404 if the task is not found, 400 if its status does not allow
        retry, or 409 if the function is no longer registered in the process.
        """
        from .models import TaskStatus

        record = task_manager.store.get(task_id)
        if record is None and task_manager._scheduler is not None:
            all_records = await task_manager.merged_list()
            record = next((r for r in all_records if r.task_id == task_id), None)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")

        if record.status not in (TaskStatus.FAILED, TaskStatus.INTERRUPTED):
            raise HTTPException(
                status_code=400,
                detail=f"Only failed or interrupted tasks can be retried. "
                f"Current status: {record.status.value}",
            )

        result = task_manager.registry.get_by_name(record.func_name)
        if result is None:
            raise HTTPException(
                status_code=409,
                detail=f"Function '{record.func_name}' is no longer registered "
                f"in this process and cannot be retried.",
            )

        func, config = result
        import uuid

        new_task_id = str(uuid.uuid4())

        scheduler = task_manager._scheduler
        backend = scheduler._backend if scheduler is not None else None
        on_success = scheduler.flush_one if scheduler is not None else None

        from .executor import make_background_func

        task_manager.store.create(
            new_task_id, func.__name__, record.args, record.kwargs
        )

        wrapped = make_background_func(
            func,
            new_task_id,
            config,
            task_manager.store,
            record.args,
            record.kwargs,
            backend=backend,
            on_success=on_success,
            file_logger=task_manager.file_logger,
        )

        # Schedule via asyncio directly since we have no BackgroundTasks instance here.
        import asyncio

        asyncio.ensure_future(wrapped())

        new_record = task_manager.store.get(new_task_id)
        return {
            "task_id": new_task_id,
            "task": new_record.to_dict() if new_record else {},
        }

    return router
