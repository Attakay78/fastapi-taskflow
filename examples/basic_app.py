"""
Basic example showing fastapi-taskflow in action.

Run with:
    uvicorn examples.basic_app:app --reload

Then try:
    curl -X POST "http://localhost:8000/signup?email=user@example.com"
    curl -X POST "http://localhost:8000/webhook"
    curl "http://localhost:8000/tasks"
    curl "http://localhost:8000/tasks/metrics"
"""

import time

from fastapi import BackgroundTasks, FastAPI

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
# task_manager.install(app) ensures the injected instance is ManagedBackgroundTasks.
@app.post("/signup", summary="Create user and queue a welcome email")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_email, address=email)
    return {"message": "User created", "task_id": task_id}


# --- Pattern 2: explicit managed type (also requires install) ---
# Declare ManagedBackgroundTasks to make the intent clear and get the
# full return type (task_id: str) without a cast. install() is still
# required — FastAPI ignores the subclass annotation and would inject
# native BackgroundTasks without the patch.
@app.post("/webhook", summary="Queue a webhook processing task")
def webhook(background_tasks: ManagedBackgroundTasks):
    task_id = background_tasks.add_task(process_webhook, {"event": "order.created", "id": 42})
    return {"queued": True, "task_id": task_id}
