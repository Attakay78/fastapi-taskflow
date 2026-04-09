"""
Example demonstrating resilience features:
  - requeue_on_interrupt: safely requeue idempotent tasks after a crash
  - idempotency_key:       prevent duplicate execution of the same logical operation

Run with:
    uvicorn examples.resilience_app:app --reload

Try these requests:

  # Idempotency — submit the same order notification twice with the same key.
  # The second call returns the original task_id and does NOT run the task again.
  curl -X POST "http://localhost:8000/notify?order_id=42&idempotency_key=order-42-notified"
  curl -X POST "http://localhost:8000/notify?order_id=42&idempotency_key=order-42-notified"

  # No key — always runs, even for the same order_id.
  curl -X POST "http://localhost:8000/notify?order_id=42"
  curl -X POST "http://localhost:8000/notify?order_id=42"

  # Requeue on interrupt demo — start a long sync, then restart the server.
  # The sync task will be requeued; the email task will be marked INTERRUPTED.
  curl -X POST "http://localhost:8000/sync?user_id=1"
  curl -X POST "http://localhost:8000/welcome?user_id=1"
  # Now stop uvicorn (Ctrl+C) and restart — sync_user_data will re-run,
  # send_welcome_email will appear as INTERRUPTED in the dashboard.

  # Dashboard
  curl "http://localhost:8000/tasks"
"""

import time

from fastapi import Depends, FastAPI

from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

task_manager = TaskManager(
    snapshot_db="resilience.db",
    snapshot_interval=30.0,
    # requeue_pending=True enables both flush-on-shutdown and requeue-on-startup.
    # Without this, requeue_on_interrupt has no effect.
    requeue_pending=True,
)
app = FastAPI(title="fastapi-taskflow resilience demo")
TaskAdmin(app, task_manager, display_func_args=True)


# ---------------------------------------------------------------------------
# Task: send_welcome_email
#
# NOT marked requeue_on_interrupt — if the server crashes mid-execution this
# task will appear as INTERRUPTED in the dashboard. It will NOT be re-run
# automatically because the email may have already been sent.
# ---------------------------------------------------------------------------
@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_welcome_email(user_id: int) -> None:
    """Send a welcome email. Not safe to re-run if interrupted mid-execution."""
    print(f"[send_welcome_email] Sending to user {user_id}")
    time.sleep(2)  # simulate network call
    print(f"[send_welcome_email] Sent to user {user_id}")


# ---------------------------------------------------------------------------
# Task: sync_user_data
#
# Marked requeue_on_interrupt=True — this task only writes to a database
# using upsert semantics, so running it twice produces the same result.
# If the server crashes while it is running, it will be saved as PENDING
# and re-executed from scratch on the next startup.
# ---------------------------------------------------------------------------
@task_manager.task(retries=2, delay=0.5, requeue_on_interrupt=True)
def sync_user_data(user_id: int) -> None:
    """Sync user data to the analytics DB. Idempotent — safe to requeue."""
    print(f"[sync_user_data] Syncing user {user_id}")
    time.sleep(3)  # simulate a long DB operation
    print(f"[sync_user_data] Done syncing user {user_id}")


# ---------------------------------------------------------------------------
# Task: notify_order
#
# Used with an idempotency_key to prevent duplicate notifications for the
# same order even if the endpoint is called multiple times (e.g. retried
# requests, double-clicks, or duplicate webhook deliveries).
# ---------------------------------------------------------------------------
@task_manager.task(retries=2, delay=0.5)
def notify_order(order_id: int) -> None:
    """Notify downstream systems about an order. Deduplicated via idempotency_key."""
    print(f"[notify_order] Notifying for order {order_id}")
    time.sleep(0.1)
    print(f"[notify_order] Done notifying for order {order_id}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/welcome", summary="Queue a welcome email (not requeue-safe)")
def welcome(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues send_welcome_email. If the server crashes while this task is
    running, it will appear as INTERRUPTED in the dashboard and will NOT
    be re-executed automatically.
    """
    task_id = tasks.add_task(send_welcome_email, user_id)
    return {"task_id": task_id, "note": "will appear as INTERRUPTED if crashed mid-run"}


@app.post("/sync", summary="Queue a user data sync (requeue-safe)")
def sync(
    user_id: int,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues sync_user_data. If the server crashes while this task is running,
    it will be requeued as PENDING and re-executed on the next startup.
    Safe because the underlying operation is idempotent (upsert).
    """
    task_id = tasks.add_task(sync_user_data, user_id)
    return {
        "task_id": task_id,
        "note": "will be requeued automatically if crashed mid-run",
    }


@app.post("/notify", summary="Notify about an order (idempotency_key dedup)")
def notify(
    order_id: int,
    idempotency_key: str | None = None,
    tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
):
    """
    Queues notify_order with an optional idempotency_key.

    If the same idempotency_key is submitted again (e.g. duplicate request,
    client retry), the original task_id is returned and the task is NOT
    enqueued or executed again.

    Without an idempotency_key, every call enqueues a new execution regardless
    of whether an identical task is already running.

    Example:
        # First call — task is enqueued
        POST /notify?order_id=42&idempotency_key=order-42-notified

        # Second call with same key — original task_id returned, no duplicate
        POST /notify?order_id=42&idempotency_key=order-42-notified
    """
    task_id = tasks.add_task(notify_order, order_id, idempotency_key=idempotency_key)
    return {
        "task_id": task_id,
        "idempotency_key": idempotency_key,
    }
