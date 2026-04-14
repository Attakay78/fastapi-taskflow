"""
Basic example showing fastapi-taskflow in action.

Run with:
    uvicorn examples.basic_app:app --reload

Then try:
    curl -X POST "http://localhost:8000/signup?email=user@example.com"
    curl -X POST "http://localhost:8000/signup?email=vip@example.com&plan=pro"
    curl -X POST "http://localhost:8000/webhook"
    curl "http://localhost:8000/tasks"
    curl "http://localhost:8000/tasks/metrics"
    open "http://localhost:8000/tasks/dashboard"
"""

import time

from fastapi import BackgroundTasks, Depends, FastAPI

from fastapi_taskflow import ManagedBackgroundTasks, TaskAdmin, TaskManager

# snapshot_db enables SQLite persistence; the scheduler is managed automatically.
task_manager = TaskManager(snapshot_db="tasks.db", snapshot_interval=30.0)
app = FastAPI(title="fastapi-taskflow demo")
# auto_install=True patches FastAPI's BackgroundTasks injection so that bare
# `background_tasks: BackgroundTasks` params receive a ManagedBackgroundTasks instance.
TaskAdmin(app, task_manager, display_func_args=True, auto_install=True)


@task_manager.task(retries=3, delay=1.0, backoff=2.0, persist=True)
def send_email(address: str) -> None:
    """Simulated email send with retry support."""
    print(f"Sending email to {address}")
    # raise RuntimeError("SMTP server not responding")  # simulate failure to trigger retries
    time.sleep(0.05)


@task_manager.task(retries=1, delay=0.5)
async def process_webhook(payload: dict) -> None:
    """Async task — awaited directly by FastAPI."""
    print(f"Processing webhook: {payload}")


# --- Pattern 1: zero-migration ---
# Use the native BackgroundTasks type annotation — no import changes needed.
# auto_install=True ensures the injected instance is ManagedBackgroundTasks.
@app.post("/signup", summary="Create user and queue a welcome email")
def signup(email: str, plan: str = "free", background_tasks: BackgroundTasks = None):
    # tags attach key/value labels to the task — visible in the dashboard and
    # forwarded to every structured log event emitted by the task.
    task_id = background_tasks.add_task(
        send_email,
        address=email,
        tags={"plan": plan, "source": "signup"},
    )
    return {"message": "User created", "task_id": task_id}


# --- Pattern 2: explicit dependency injection ---
# Declare ManagedBackgroundTasks via Depends for a clear, typed signature.
@app.post("/webhook", summary="Queue a webhook processing task")
def webhook(tasks: ManagedBackgroundTasks = Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        process_webhook,
        {"event": "order.created", "id": 42},
        tags={"source": "webhook"},
    )
    return {"queued": True, "task_id": task_id}
