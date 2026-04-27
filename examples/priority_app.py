"""
Example demonstrating priority queues and eager dispatch:
  - priority:  higher-priority tasks run before lower-priority ones
  - eager:     task starts immediately, before the response is sent

Run with:
    uvicorn examples.priority_app:app --reload

Try these requests:

  # Enqueue a high-priority OTP alongside a low-priority report.
  # The OTP runs first regardless of submission order.
  curl -X POST "http://localhost:8000/report?user_id=1"
  curl -X POST "http://localhost:8000/otp?phone=555-0100"

  # Eager dispatch — the notification starts before the HTTP response is sent.
  curl -X POST "http://localhost:8000/notify?user_id=1&message=hello"

  # Decorator-level default priority — override it per call.
  curl -X POST "http://localhost:8000/process?item_id=99&priority=10"
  curl -X POST "http://localhost:8000/process?item_id=100"

  # Dashboard
  open "http://localhost:8000/tasks/dashboard"
"""

import asyncio
import time

from fastapi import Depends, FastAPI

from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="priority.db", snapshot_interval=30.0)
app = FastAPI(title="fastapi-taskflow priority demo")
TaskAdmin(app, task_manager, display_func_args=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@task_manager.task(retries=2, delay=0.5, priority=9)
async def send_otp(phone: str) -> None:
    """Send a one-time password. High priority by default (9)."""
    print(f"[send_otp] Sending OTP to {phone}")
    await asyncio.sleep(0.1)
    print(f"[send_otp] OTP delivered to {phone}")


@task_manager.task(priority=1)
def generate_report(user_id: int) -> None:
    """Generate a background report. Low priority by default (1)."""
    print(f"[generate_report] Generating report for user {user_id}")
    time.sleep(0.5)
    print(f"[generate_report] Report ready for user {user_id}")


@task_manager.task(eager=True)
async def notify_user(user_id: int, message: str) -> None:
    """Push a notification. Starts immediately, before the response is sent."""
    print(f"[notify_user] Notifying user {user_id}: {message}")
    await asyncio.sleep(0.05)
    print(f"[notify_user] Done notifying user {user_id}")


# Decorator sets priority=5 (mid-range). Per-call priority overrides it.
@task_manager.task(priority=5)
def process_item(item_id: int) -> None:
    """Process an item. Default priority 5; callers can raise or lower it."""
    print(f"[process_item] Processing item {item_id}")
    time.sleep(0.1)
    print(f"[process_item] Done with item {item_id}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/otp", summary="Send OTP — high priority (9)")
async def otp(
    phone: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues send_otp at its decorator default (priority=9).
    Even if a low-priority report is already queued, the OTP runs first.
    """
    task_id = tasks.add_task(send_otp, phone)
    return {"task_id": task_id, "priority": 9}


@app.post("/report", summary="Generate report — low priority (1)")
async def report(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues generate_report at its decorator default (priority=1).
    Any higher-priority tasks enqueued after this one will run first.
    """
    task_id = tasks.add_task(generate_report, user_id)
    return {"task_id": task_id, "priority": 1}


@app.post("/notify", summary="Push a notification — eager dispatch")
async def notify(
    user_id: int,
    message: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues notify_user with eager=True from the decorator.
    The task starts via asyncio.create_task before this response is sent.
    """
    task_id = tasks.add_task(notify_user, user_id, message)
    return {"task_id": task_id, "dispatch": "eager"}


@app.post("/process", summary="Process an item — overridable priority")
async def process(
    item_id: int,
    priority: int | None = None,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues process_item. Omit `priority` to use the decorator default (5).
    Pass `priority=10` to jump ahead of everything else in the queue,
    or `priority=1` to defer until higher-priority work is done.
    """
    task_id = tasks.add_task(process_item, item_id, priority=priority)
    resolved = priority if priority is not None else 5
    return {"task_id": task_id, "priority": resolved}
