from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import Request

if TYPE_CHECKING:
    from ..manager import TaskManager
    from ..models import TaskRecord


def _build_sse_state(
    tasks: list,
    include_args: bool = False,
    schedules: list | None = None,
) -> str:
    """
    Serialize the full task store as a single SSE ``state`` event.

    Payload shape::

        {
          "tasks":     [ <TaskRecord.to_dict()>, ... ],
          "metrics":   { "total": N, "pending": N, ... },
          "schedules": [ <schedule entry dict>, ... ]
        }

    When *include_args* is ``True`` each task dict also carries ``args`` and
    ``kwargs`` (serialised with ``repr()`` so arbitrary types are safe).
    Newlines are kept out of the data line so the SSE framing is unambiguous.
    """
    total = len(tasks)
    success = sum(1 for t in tasks if t.status.value == "success")
    failed = sum(1 for t in tasks if t.status.value == "failed")
    running = sum(1 for t in tasks if t.status.value == "running")
    pending = sum(1 for t in tasks if t.status.value == "pending")
    interrupted = sum(1 for t in tasks if t.status.value == "interrupted")
    cancelled = sum(1 for t in tasks if t.status.value == "cancelled")
    durs = [t.duration for t in tasks if t.duration is not None]

    metrics = {
        "total": total,
        "pending": pending,
        "running": running,
        "success": success,
        "failed": failed,
        "interrupted": interrupted,
        "cancelled": cancelled,
        "success_rate": round(success / total * 100, 1) if total else None,
        "avg_duration_ms": round(sum(durs) / len(durs) * 1000) if durs else None,
    }

    if include_args:
        task_dicts = [
            {
                **t.to_dict(),
                "args": [repr(a) for a in t.args],
                "kwargs": {k: repr(v) for k, v in t.kwargs.items()},
            }
            for t in tasks
        ]
    else:
        task_dicts = [t.to_dict() for t in tasks]

    payload = json.dumps(
        {"tasks": task_dicts, "metrics": metrics, "schedules": schedules or []}
    )
    return f"event: state\ndata: {payload}\n\n"


def _get_schedule_entries(task_manager: "TaskManager") -> list[dict]:
    """Serialise the registered schedule entries for the dashboard.

    Returns a list of dicts with ``func_name``, ``trigger``, ``next_run``,
    and ``last_status`` (the status of the most recent task record for this
    function, or ``None`` if no run has been recorded yet).
    """
    ps = task_manager._periodic_scheduler
    if ps is None:
        return []

    # Build a map of func_name -> most recent task record for fast lookup.
    all_tasks = task_manager.store.list()
    latest: dict[str, "TaskRecord"] = {}
    for t in all_tasks:
        if t.source != "scheduled":
            continue
        existing = latest.get(t.func_name)
        if existing is None or t.created_at > existing.created_at:
            latest[t.func_name] = t

    return [
        {
            "func_name": entry.func.__name__,
            "trigger": f"every {entry.every}s"
            if entry.every is not None
            else entry.cron,
            "next_run": entry.next_run.isoformat(),
            "last_status": last.status.value
            if (last := latest.get(entry.func.__name__)) is not None
            else None,
            "last_task_id": last.task_id if last is not None else None,
        }
        for entry in ps.entries
    ]


async def _sse_generator(
    task_manager: "TaskManager",
    request: Request,
    include_args: bool = False,
    poll_interval: float = 30.0,
) -> AsyncIterator[str]:
    """
    Yields SSE messages for the duration of the client connection.

    * Sends an immediate ``state`` event so the dashboard renders on first
      connect without waiting for a store change.
    * Wakes immediately on any local store mutation (in-process tasks).
    * On timeout:
      - No backend configured: sends a keep-alive comment only — local
        mutations are already instant, no backend read needed.
      - Backend configured: emits a full state refresh so completed tasks
        from other instances that flushed to the shared backend are picked up.
        Frequency controlled by *poll_interval* (default 30s).
    * Cleans up the subscriber queue on disconnect or CancelledError.
    """
    q = task_manager.store.add_subscriber()
    has_backend = task_manager._scheduler is not None
    try:
        tasks = await task_manager.merged_list()
        yield _build_sse_state(
            tasks,
            include_args=include_args,
            schedules=_get_schedule_entries(task_manager),
        )

        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(q.get(), timeout=poll_interval)
                # Local mutation — always emit a fresh state.
                tasks = await task_manager.merged_list()
                yield _build_sse_state(
                    tasks,
                    include_args=include_args,
                    schedules=_get_schedule_entries(task_manager),
                )
            except asyncio.TimeoutError:
                if not has_backend:
                    # Single instance, no backend — keep the connection alive
                    # without an unnecessary backend read.
                    yield ": keep-alive\n\n"
                else:
                    # Backend present — refresh to pick up other instances'
                    # completed tasks that flushed since the last local event.
                    tasks = await task_manager.merged_list()
                    yield _build_sse_state(
                        tasks,
                        include_args=include_args,
                        schedules=_get_schedule_entries(task_manager),
                    )
    except asyncio.CancelledError:
        pass
    finally:
        task_manager.store.remove_subscriber(q)
