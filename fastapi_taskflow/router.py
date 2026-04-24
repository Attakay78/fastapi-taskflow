from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from .auth import COOKIE_NAME, decode_token
from .executor import make_background_func
from .models import AuditEntry, TaskStatus

_UNIT_MAP = {
    "min": "minutes",
    "hour": "hours",
    "day": "days",
}

if TYPE_CHECKING:
    from .manager import TaskManager


def _actor(request: Request, secret_key: str | None) -> str:
    """Return the authenticated username or 'anonymous'."""
    if secret_key is None:
        return "anonymous"
    return decode_token(request.cookies.get(COOKIE_NAME, ""))


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
        """Return all task records from the in-memory store merged with the backend.

        Returns:
            List of serialized :class:`~fastapi_taskflow.models.TaskRecord` dicts,
            one per known task invocation.
        """
        return [t.to_dict() for t in await task_manager.merged_list()]

    # Fixed paths must be defined before the /{task_id} catch-all.
    @router.get("/audit", summary="Audit log of user actions")
    def get_audit() -> list[dict[str, Any]]:
        """Return the in-memory audit log (last 1000 entries, newest first).

        Records retry and cancel actions with the actor username and timestamp.
        """
        return [e.to_dict() for e in reversed(task_manager._audit_log)]

    # Define /metrics before /{task_id} so the fixed path wins.
    @router.get("/metrics", summary="Aggregated task statistics")
    async def get_metrics() -> dict[str, Any]:
        """Return aggregated counts and timing statistics across all tasks.

        Returns:
            A dict with keys: ``total``, ``success``, ``failed``, ``running``,
            ``pending``, ``interrupted``, ``success_rate``, and
            ``avg_duration_seconds``.
        """
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
        """Return a single task record by its UUID.

        Checks the in-memory store first, then falls back to the backend.

        Args:
            task_id: The UUID assigned when the task was enqueued.

        Returns:
            Serialized :class:`~fastapi_taskflow.models.TaskRecord` dict.

        Raises:
            HTTPException: 404 if no task with *task_id* exists.
        """
        # Check in-memory first (fastest path), then fall back to backend.
        record = task_manager.store.get(task_id)
        if record is None and task_manager._scheduler is not None:
            all_records = await task_manager.merged_list()
            record = next((r for r in all_records if r.task_id == task_id), None)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return record.to_dict()

    @router.post("/{task_id}/cancel", summary="Cancel a pending or running task")
    async def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
        """Cancel a task that is pending or currently running.

        For ``pending`` tasks the status is set to ``cancelled`` immediately
        and persisted. For ``running`` tasks the asyncio Task is cancelled;
        the status transitions to ``cancelled`` asynchronously once the
        executor handles the CancelledError.

        Raises 404 if not found, 400 if the task cannot be cancelled.
        """
        record = task_manager.store.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")

        if record.status == TaskStatus.PENDING:
            task_manager.store.update(
                task_id,
                status=TaskStatus.CANCELLED,
                end_time=datetime.now(timezone.utc),
            )
            if task_manager._scheduler is not None:
                await task_manager._scheduler.flush_one(task_id)

        elif record.status == TaskStatus.RUNNING:
            inner = task_manager._running_tasks.get(task_id)
            if inner is None or inner.done():
                raise HTTPException(
                    status_code=400,
                    detail="Task is running but no cancellable handle is registered. "
                    "Only async tasks support mid-execution cancellation.",
                )
            inner.cancel()

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Only pending or running tasks can be cancelled. "
                f"Current status: {record.status.value}",
            )

        task_manager._audit_log.append(
            AuditEntry(
                entry_id=str(uuid.uuid4()),
                action="cancel",
                task_id=task_id,
                actor=_actor(request, secret_key),
                timestamp=datetime.now(timezone.utc),
            )
        )

        updated = task_manager.store.get(task_id)
        return {"task_id": task_id, "task": updated.to_dict() if updated else {}}

    @router.post("/{task_id}/retry", summary="Retry a failed or interrupted task")
    async def retry_task(task_id: str, request: Request) -> dict[str, Any]:
        """
        Re-enqueue a task that has status ``failed`` or ``interrupted``.

        The original task record is left unchanged in history. A new task is
        created with a fresh task ID using the same function, args, and kwargs
        as the original. Returns the new task record.

        Raises 404 if the task is not found, 400 if its status does not allow
        retry, or 409 if the function is no longer registered in the process.
        """
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
        new_task_id = str(uuid.uuid4())

        scheduler = task_manager._scheduler
        backend = scheduler._backend if scheduler is not None else None
        on_success = scheduler.flush_one if scheduler is not None else None

        task_manager.store.create(
            new_task_id, func.__name__, record.args, record.kwargs, tags=record.tags
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
            logger=task_manager.logger,
            encryptor=task_manager.fernet,
            semaphore=task_manager._task_semaphore,
            sync_executor=task_manager._sync_executor,
            running_tasks=task_manager._running_tasks,
        )

        asyncio.create_task(wrapped())

        task_manager._audit_log.append(
            AuditEntry(
                entry_id=str(uuid.uuid4()),
                action="retry",
                task_id=task_id,
                actor=_actor(request, secret_key),
                timestamp=datetime.now(timezone.utc),
                detail={"new_task_id": new_task_id},
            )
        )

        new_record = task_manager.store.get(new_task_id)
        return {
            "task_id": new_task_id,
            "task": new_record.to_dict() if new_record else {},
        }

    @router.delete("/history", summary="Delete task history older than a given window")
    async def delete_history(
        value: int = Query(..., gt=0, description="Number of time units."),
        unit: str = Query(..., description="Time unit: min, hour, or day."),
    ) -> dict[str, Any]:
        """Delete terminal task records (success, failed, interrupted) whose
        end_time is older than the given window.

        Pending and running tasks are never deleted.

        Args:
            value: A positive integer.
            unit: One of min, hour, day.

        Returns:
            Dict with ``deleted`` (total count), ``store`` (in-memory), and
            ``backend`` (persisted) counts.

        Raises:
            HTTPException: 400 if unit is not recognised.
        """
        if unit not in _UNIT_MAP:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid unit '{unit}'. Accepted: {', '.join(_UNIT_MAP)}.",
            )
        delta = timedelta(**{_UNIT_MAP[unit]: value})

        cutoff = datetime.now(timezone.utc) - delta

        store_deleted = task_manager.store.delete_completed_before(cutoff)

        backend_deleted = 0
        if task_manager._scheduler is not None:
            backend_deleted = await task_manager._scheduler._backend.delete_before(
                cutoff
            )
            task_manager._invalidate_backend_cache()

        return {
            "deleted": store_deleted + backend_deleted,
            "store": store_deleted,
            "backend": backend_deleted,
        }

    return router
