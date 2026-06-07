"""
Example demonstrating named queues with per-queue concurrency and backpressure:
  - named queues:  isolate task types so a burst in one cannot starve another
  - concurrency:   cap how many tasks run at once per queue
  - max_size:      reject new tasks when a queue is full (backpressure)
  - per-call override: route a task to a different queue at enqueue time

Run with:
    uvicorn examples.named_queues_app:app --reload

Try these requests:

  # Enqueue a transactional email (routes to the "email" queue).
  curl -X POST "http://localhost:8000/signup?email=alice@example.com"

  # Generate a report (routes to the "reports" queue, concurrency=2).
  curl -X POST "http://localhost:8000/export?user_id=1"

  # Fire a webhook (routes to the "webhooks" queue).
  curl -X POST "http://localhost:8000/webhook?url=https://example.com/hook&payload=ping"

  # Deliberately overflow the reports queue (max_size=5) to see a 429.
  for i in $(seq 1 10); do curl -s -o /dev/null -w "%{http_code}\\n" -X POST "http://localhost:8000/export?user_id=$i"; done

  # Check live queue stats (pending/running/finished per queue).
  curl "http://localhost:8000/queue-stats"

  # Override the queue at call time — send an urgent report via the email queue.
  curl -X POST "http://localhost:8000/export?user_id=99&queue=email"

  # Dashboard
  open "http://localhost:8000/tasks/dashboard"
"""

import asyncio
import time

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from fastapi_taskflow import (
    ManagedBackgroundTasks,
    QueueFullError,
    TaskAdmin,
    TaskManager,
)
from fastapi_taskflow.models import QueueConfig

task_manager = TaskManager(
    snapshot_db="named_queues.db",
    snapshot_interval=30.0,
    requeue_pending=True,
    queues={
        # High-throughput transactional emails: up to 10 concurrent, 200 pending.
        "email": QueueConfig(concurrency=10, max_size=200),
        # Heavy report generation: only 2 at a time to avoid saturating CPU.
        "reports": QueueConfig(concurrency=2, max_size=5),
        # Outbound webhook delivery: moderate concurrency, limited backlog.
        "webhooks": QueueConfig(concurrency=5, max_size=50),
        # General tasks that do not need a dedicated queue.
        "default": QueueConfig(concurrency=4, max_size=100),
    },
)

app = FastAPI(title="fastapi-taskflow named queues demo")
TaskAdmin(app, task_manager, display_func_args=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@task_manager.task(queue="email", retries=3, delay=1.0, backoff=2.0)
async def send_welcome_email(email: str) -> None:
    """Send a welcome email after signup. Retries three times with backoff."""
    print(f"[send_welcome_email] Sending to {email}")
    await asyncio.sleep(0.2)
    print(f"[send_welcome_email] Delivered to {email}")


@task_manager.task(queue="reports", retries=1, delay=5.0)
def generate_export(user_id: int) -> None:
    """Build a data export for a user. CPU-bound, limited to 2 at once."""
    print(f"[generate_export] Building export for user {user_id}")
    time.sleep(0.5)
    print(f"[generate_export] Export ready for user {user_id}")


@task_manager.task(queue="webhooks", retries=5, delay=2.0, backoff=1.5)
async def deliver_webhook(url: str, payload: str) -> None:
    """Deliver an outbound webhook with aggressive retry."""
    print(f"[deliver_webhook] POST {url} -> {payload}")
    await asyncio.sleep(0.1)
    print(f"[deliver_webhook] Delivered to {url}")


@task_manager.task()
async def log_event(event: str) -> None:
    """Lightweight audit log write. Uses the default queue."""
    print(f"[log_event] {event}")
    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/signup", summary="Sign up a new user")
async def signup(
    email: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Enqueues a welcome email via the 'email' queue (concurrency=10).
    Also logs the signup event via the 'default' queue.
    Both task IDs are returned so callers can poll for status.
    """
    email_id = tasks.add_task(send_welcome_email, email)
    log_id = tasks.add_task(log_event, f"signup:{email}")
    return {"email_task_id": email_id, "log_task_id": log_id}


@app.post("/export", summary="Generate a data export")
async def export(
    user_id: int,
    queue: str | None = None,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Enqueues a report via the 'reports' queue (concurrency=2, max_size=5).
    When the queue is full a 429 is returned immediately so the caller can
    back off and retry rather than silently losing the request.

    Pass ?queue=email to override the target queue at call time.
    """
    try:
        task_id = tasks.add_task(generate_export, user_id, queue=queue)
    except QueueFullError as exc:
        return JSONResponse(
            status_code=429,
            content={
                "error": "report queue is full, try again shortly",
                "detail": str(exc),
            },
        )
    resolved_queue = queue or "reports"
    return {"task_id": task_id, "queue": resolved_queue}


@app.post("/webhook", summary="Fire an outbound webhook")
async def webhook(
    url: str,
    payload: str,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Enqueues webhook delivery via the 'webhooks' queue (concurrency=5).
    Returns a 429 when the webhook backlog is full.
    """
    try:
        task_id = tasks.add_task(deliver_webhook, url, payload)
    except QueueFullError as exc:
        return JSONResponse(
            status_code=429,
            content={"error": "webhook queue is full", "detail": str(exc)},
        )
    return {"task_id": task_id}


@app.get("/queue-stats", summary="Live stats for every named queue")
def queue_stats():
    """
    Returns a snapshot of each queue's configuration and current state:
    pending tasks in the heap, running tasks, and finished tasks.

    Example response:
        [
          {"name": "default",  "concurrency": 4,  "max_size": 100, "pending": 0, "running": 0, "finished": 2},
          {"name": "email",    "concurrency": 10, "max_size": 200, "pending": 3, "running": 2, "finished": 41},
          {"name": "reports",  "concurrency": 2,  "max_size": 5,   "pending": 1, "running": 2, "finished": 7},
          {"name": "webhooks", "concurrency": 5,  "max_size": 50,  "pending": 0, "running": 1, "finished": 18}
        ]
    """
    return task_manager.queue_stats()
