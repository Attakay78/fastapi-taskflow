"""
Example showing task_log() and rich error/stacktrace display.

Run with:
    uvicorn examples.task_logging_app:app --reload

Then try:
    # Successful async task — logs appear in the dashboard detail panel
    curl -X POST "http://localhost:8000/report?user_id=42"

    # Failing task — error message + collapsible stack trace shown in dashboard
    curl -X POST "http://localhost:8000/report?user_id=0"

    # Sync task that fails on first attempt and succeeds on retry —
    # each attempt's logs are grouped under a '--- Retry N ---' separator
    curl -X POST "http://localhost:8000/sync?user_id=7"

    open "http://localhost:8000/tasks/dashboard"
"""

import asyncio
import time

from fastapi import Depends, FastAPI

from fastapi_taskflow import TaskAdmin, TaskManager, task_log

task_manager = TaskManager()
app = FastAPI(title="task_log demo")
TaskAdmin(app, task_manager, display_func_args=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@task_manager.task(retries=2, delay=0.2, backoff=2.0)
async def generate_report(user_id: int) -> None:
    """
    Async task that emits log entries at each stage.

    Passing user_id=0 triggers a ValueError so the error message and full
    stack trace are captured and displayed in the dashboard detail panel.
    """
    task_log("Starting report generation")
    task_log(f"Fetching data for user {user_id}")
    await asyncio.sleep(0.05)

    if user_id == 0:
        task_log("User ID is invalid — aborting")
        raise ValueError(f"Invalid user_id: {user_id!r}. Must be a positive integer.")

    task_log("Aggregating metrics")
    await asyncio.sleep(0.05)

    task_log("Writing output to storage")
    await asyncio.sleep(0.02)

    task_log(f"Report complete for user {user_id}")


@task_manager.task(retries=1, delay=0.1)
def sync_inventory(user_id: int) -> None:
    """
    Sync task that always fails on the first attempt (odd user_id) and retries.

    Demonstrates per-retry log grouping — each attempt's logs appear under
    a '--- Retry N ---' separator in the dashboard Logs panel.
    """
    task_log(f"Beginning inventory sync for user {user_id}")
    time.sleep(0.03)

    if user_id % 2 != 0:
        task_log("Stale lock detected — will retry")
        raise RuntimeError(f"Inventory lock held for user {user_id}, try again")

    task_log("Lock acquired")
    time.sleep(0.02)
    task_log("Inventory sync complete")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/report", summary="Generate a user report (async, with logs)")
def queue_report(user_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(generate_report, user_id=user_id)
    return {"queued": True, "task_id": task_id}


@app.post("/sync", summary="Sync inventory (sync task, retries on first attempt)")
def queue_sync(user_id: int, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(sync_inventory, user_id=user_id)
    return {"queued": True, "task_id": task_id}
