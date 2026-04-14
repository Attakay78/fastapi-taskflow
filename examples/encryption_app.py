"""
Example demonstrating task argument encryption with Fernet.

When encrypt_args_key is set on TaskManager, args and kwargs are encrypted
with Fernet (AES-128-CBC + HMAC) before being stored in the in-memory store,
the SQLite/Redis backend, and any log output. The executor decrypts them just
before calling the function — they are never logged or persisted in plain text.

This is useful when tasks carry sensitive data such as:
  - API keys or access tokens
  - Payment card or bank account details
  - Passwords or PII

Requirements:
    pip install "fastapi-taskflow[encryption]"

Run with:
    uvicorn examples.encryption_app:app --reload

Then try:
    # Enqueue a payment task — args are encrypted at rest
    curl -X POST "http://localhost:8000/pay" \\
         -H "Content-Type: application/json" \\
         -d '{"user_id": 42, "card_token": "tok_visa_4242", "amount": 99.99}'

    # Inspect the stored task — card_token and amount are NOT visible in plain text
    curl "http://localhost:8000/tasks"

Key generation (run once, store in an environment variable or secrets manager):
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import asyncio
import os
import time

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from fastapi_taskflow import TaskAdmin, TaskManager, get_task_context, task_log

# ---------------------------------------------------------------------------
# Key management
#
# In production, load the key from an environment variable or a secrets
# manager (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager, etc.).
# Never hard-code the key in source code.
#
# Generate a key once with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# ---------------------------------------------------------------------------
ENCRYPTION_KEY = os.environ.get(
    "TASK_ENCRYPTION_KEY",
    # Fallback key for running the example without setup — replace in production.
    "kfPdL1yV8JqMeXr3HnNwZo5sCbTuGiA6WvYm0Ek2RfQ=",
)

task_manager = TaskManager(
    snapshot_db="encrypted_tasks.db",
    snapshot_interval=30.0,
    encrypt_args_key=ENCRYPTION_KEY,
)
app = FastAPI(title="fastapi-taskflow encryption demo")
TaskAdmin(app, task_manager, display_func_args=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def charge_payment(user_id: int, card_token: str, amount: float) -> None:
    """
    Process a payment.

    card_token and amount are encrypted at rest — they only exist in plain
    text inside this function body and are never written to the task store,
    the SQLite database, or any log file.
    """
    ctx = get_task_context()
    # Safe to log user_id; card_token is only referenced here, never stored.
    task_log(
        "Charging payment",
        user_id=user_id,
        amount=amount,
        attempt=ctx.attempt if ctx else 0,
    )
    time.sleep(0.1)  # simulate payment gateway call
    task_log("Payment accepted", user_id=user_id)


@task_manager.task(retries=2, delay=0.5)
async def send_receipt(user_id: int, email: str, amount: float) -> None:
    """
    Send a payment receipt email.

    email and amount are encrypted in the task queue — this task is safe
    to use with user PII.
    """
    task_log("Sending receipt", user_id=user_id, amount=amount)
    await asyncio.sleep(0.05)
    task_log("Receipt sent", user_id=user_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class PaymentRequest(BaseModel):
    user_id: int
    card_token: str
    amount: float
    email: str = "user@example.com"


@app.post("/pay", summary="Process a payment (args encrypted at rest)")
def pay(
    body: PaymentRequest,
    tasks=Depends(task_manager.get_tasks),
):
    """
    Enqueues charge_payment and send_receipt.

    The card_token, amount, and email are encrypted by TaskManager before
    being stored. The task records returned by GET /tasks will NOT contain
    these values in plain text.
    """
    charge_id = tasks.add_task(
        charge_payment,
        user_id=body.user_id,
        card_token=body.card_token,
        amount=body.amount,
        tags={"user_id": str(body.user_id), "type": "charge"},
    )
    receipt_id = tasks.add_task(
        send_receipt,
        user_id=body.user_id,
        email=body.email,
        amount=body.amount,
        tags={"user_id": str(body.user_id), "type": "receipt"},
    )
    return {
        "charge_task_id": charge_id,
        "receipt_task_id": receipt_id,
        "note": "card_token and email are encrypted at rest",
    }
