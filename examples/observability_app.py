"""
Example demonstrating the pluggable observability system.

Features shown:
  - FileLogger:     writes structured task events to a rotating log file
  - StdoutLogger:   prints events to stdout (useful in containers / dev)
  - InMemoryLogger: captures events in memory for assertions in tests
  - LoggerChain:    fan-out to multiple observers simultaneously
  - tags:           key/value labels forwarded to every log and lifecycle event
  - task_log():     structured log with level= and **extra keyword fields
  - get_task_context(): access task metadata from inside any helper function

Run with:
    uvicorn examples.observability_app:app --reload

Then try:
    curl -X POST "http://localhost:8000/invoice?user_id=1&amount=99.00"
    curl -X POST "http://localhost:8000/invoice?user_id=0&amount=0"
    curl "http://localhost:8000/tasks"
    tail -f tasks.log

Testing with InMemoryLogger (no server needed):
    python examples/observability_app.py
"""

import asyncio
import time

from fastapi import Depends, FastAPI

from fastapi_taskflow import (
    FileLogger,
    InMemoryLogger,
    StdoutLogger,
    TaskAdmin,
    TaskManager,
    get_task_context,
    task_log,
)

# ---------------------------------------------------------------------------
# Configure observers
#
# FileLogger  — rotating log file, lifecycle events included.
# StdoutLogger — prints to stdout; useful in containers where stdout is
#                captured by the logging agent.
# Both are passed as a list; LoggerChain fans out to all of them internally.
# ---------------------------------------------------------------------------
file_logger = FileLogger(
    "tasks.log",
    max_bytes=5 * 1024 * 1024,  # 5 MB per file
    backup_count=3,
    log_lifecycle=True,  # also log RUNNING / SUCCESS / FAILED transitions
    min_level="info",
)

stdout_logger = StdoutLogger(
    log_lifecycle=True,
    min_level="info",
)

task_manager = TaskManager(
    snapshot_db="observability.db",
    loggers=[file_logger, stdout_logger],
)
app = FastAPI(title="fastapi-taskflow observability demo")
TaskAdmin(app, task_manager, display_func_args=True)


# ---------------------------------------------------------------------------
# A helper function to show that get_task_context() works anywhere in the
# call stack, not just at the top level of the task function.
# ---------------------------------------------------------------------------
def _validate_invoice(user_id: int, amount: float) -> None:
    ctx = get_task_context()
    task_log(
        "Validating invoice",
        level="debug",
        user_id=user_id,
        amount=amount,
        attempt=ctx.attempt if ctx else 0,
    )
    if user_id <= 0:
        raise ValueError(f"Invalid user_id: {user_id}")
    if amount <= 0:
        raise ValueError(f"Invalid amount: {amount}")


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@task_manager.task(retries=2, delay=0.5, backoff=2.0)
async def process_invoice(user_id: int, amount: float) -> None:
    """
    Async task that validates and processes an invoice.

    Demonstrates:
    - task_log() with level= and **extra fields
    - get_task_context() for current attempt number
    - tags flowing through to log events
    """
    ctx = get_task_context()
    task_log(
        "Invoice processing started",
        user_id=user_id,
        amount=amount,
        attempt=ctx.attempt if ctx else 0,
        # tags attached at enqueue time are available on ctx.tags
        source=ctx.tags.get("source", "unknown") if ctx else "unknown",
    )

    await asyncio.sleep(0.02)
    _validate_invoice(user_id, amount)

    task_log("Charging payment gateway", level="info", user_id=user_id, amount=amount)
    await asyncio.sleep(0.05)

    task_log("Invoice processed successfully", user_id=user_id, amount=amount)


@task_manager.task(retries=1, delay=0.2)
def send_invoice_email(user_id: int, amount: float) -> None:
    """
    Sync task that sends an invoice confirmation email.

    Shows that task_log() and get_task_context() work identically in sync tasks.
    """
    ctx = get_task_context()
    task_log(
        "Sending invoice email",
        user_id=user_id,
        amount=amount,
        attempt=ctx.attempt if ctx else 0,
    )
    time.sleep(0.03)
    task_log("Invoice email sent", user_id=user_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/invoice", summary="Process an invoice and send confirmation email")
def create_invoice(
    user_id: int,
    amount: float,
    tasks=Depends(task_manager.get_tasks),
):
    """
    Enqueues two tasks with tags.

    Tags are attached at enqueue time and forwarded to every log and lifecycle
    event, making it easy to filter logs by user_id or source in downstream
    systems (e.g. Loki, Datadog, CloudWatch).
    """
    common_tags = {"user_id": str(user_id), "source": "invoice_api"}

    process_id = tasks.add_task(
        process_invoice,
        user_id=user_id,
        amount=amount,
        tags=common_tags,
    )
    email_id = tasks.add_task(
        send_invoice_email,
        user_id=user_id,
        amount=amount,
        tags=common_tags,
    )
    return {"process_task_id": process_id, "email_task_id": email_id}


# ---------------------------------------------------------------------------
# Self-contained test using InMemoryLogger (no server, no file I/O).
#
# Run: python examples/observability_app.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from fastapi_taskflow import InMemoryLogger, TaskManager
    from fastapi_taskflow.executor import execute_task
    from fastapi_taskflow.models import TaskConfig
    from fastapi_taskflow.store import TaskStore

    mem_logger = InMemoryLogger()
    tm = TaskManager(loggers=[mem_logger])

    @tm.task(retries=0)
    async def greet(name: str) -> None:
        task_log("Hello from task", name=name, level="info")

    store = TaskStore()
    store.create("task-1", "greet", ("Alice",), {}, tags={"env": "test"})

    async def _run():
        await execute_task(
            greet,
            "task-1",
            TaskConfig(),
            store,
            ("Alice",),
            {},
            logger=tm.logger,
        )

        print(f"Log events captured:       {len(mem_logger.log_events)}")
        print(f"Lifecycle events captured: {len(mem_logger.lifecycle_events)}")
        for ev in mem_logger.log_events:
            print(f"  [{ev.level}] {ev.message}  extra={ev.extra}")
        for ev in mem_logger.lifecycle_events:
            print(f"  [lifecycle] {ev.func_name} -> {ev.status.value}")

    asyncio.run(_run())
