"""
Scheduled tasks example showing periodic task execution with fastapi-taskflow.

Run with:
    uvicorn examples.scheduled_app:app --reload

Then try:
    curl "http://localhost:8000/tasks"
    open "http://localhost:8000/tasks/dashboard"

Scheduled tasks fire automatically in the background:
- health_check runs every 30 seconds
- daily_cleanup runs every 5 minutes (shortened for demo purposes)

Both are also triggerable manually from a route.
"""

import asyncio
import time

from fastapi import Depends, FastAPI

from fastapi_taskflow import TaskAdmin, TaskManager, task_log

task_manager = TaskManager(snapshot_db="scheduled_tasks.db")
app = FastAPI(title="fastapi-taskflow scheduled tasks demo")
TaskAdmin(app, task_manager)


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


@task_manager.schedule(every=30, retries=2, delay=5.0, run_on_startup=True)
async def health_check() -> None:
    """Runs every 30 seconds. Fires once immediately on startup."""
    task_log("Running health check")
    await asyncio.sleep(0.1)  # simulate a lightweight check
    task_log("Health check passed", level="info")


@task_manager.schedule(every=300, retries=1)
def daily_cleanup() -> None:
    """Runs every 5 minutes (shortened from daily for demo purposes)."""
    task_log("Starting cleanup")
    time.sleep(0.05)  # simulate cleanup work
    task_log("Cleanup complete")


# ---------------------------------------------------------------------------
# Manual trigger routes
# ---------------------------------------------------------------------------
# Scheduled tasks are also registered in the task registry, so they can be
# enqueued manually in addition to running on schedule.


@app.post("/health/run-now", summary="Trigger a health check immediately")
async def trigger_health_check(tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(health_check)
    return {"task_id": task_id, "triggered": "health_check"}


@app.post("/cleanup/run-now", summary="Trigger the cleanup task immediately")
async def trigger_cleanup(tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(daily_cleanup)
    return {"task_id": task_id, "triggered": "daily_cleanup"}


@app.get("/schedule", summary="List registered schedules")
def list_schedules():
    ps = task_manager._periodic_scheduler
    if ps is None:
        return {"schedules": []}
    entries = []
    for e in ps.entries:
        entries.append(
            {
                "func_name": e.func.__name__,
                "trigger": f"every {e.every}s" if e.every is not None else e.cron,
                "next_run": e.next_run.isoformat(),
                "run_on_startup": e.run_on_startup,
            }
        )
    return {"schedules": entries}
