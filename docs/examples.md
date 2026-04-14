# Examples

All examples are runnable FastAPI applications in the [`examples/`](https://github.com/Attakay78/fastapi-taskflow/tree/main/examples) directory of the repository.

Each example covers a distinct feature area and can be started independently with `uvicorn`.

---

## [basic_app.py](https://github.com/Attakay78/fastapi-taskflow/blob/main/examples/basic_app.py)

The starting point. Shows the two most common injection patterns and how to attach tags to a task at enqueue time.

**Covers:** `TaskManager`, `TaskAdmin`, `@task_manager.task`, `auto_install`, `tags`, sync and async tasks.

```bash
uvicorn examples.basic_app:app --reload

curl -X POST "http://localhost:8000/signup?email=user@example.com&plan=pro"
curl -X POST "http://localhost:8000/webhook"
curl "http://localhost:8000/tasks"
open "http://localhost:8000/tasks/dashboard"
```

---

## [resilience_app.py](https://github.com/Attakay78/fastapi-taskflow/blob/main/examples/resilience_app.py)

Demonstrates crash recovery and deduplication.

**Covers:** `requeue_pending`, `requeue_on_interrupt`, `idempotency_key`.

```bash
uvicorn examples.resilience_app:app --reload

# Idempotency: second call returns the original task_id, task does not run again
curl -X POST "http://localhost:8000/notify?order_id=42&idempotency_key=order-42"
curl -X POST "http://localhost:8000/notify?order_id=42&idempotency_key=order-42"

# Start a long sync task, stop uvicorn (Ctrl+C), restart
# sync_user_data will be requeued; send_welcome_email will appear as INTERRUPTED
curl -X POST "http://localhost:8000/sync?user_id=1"
curl -X POST "http://localhost:8000/welcome?user_id=1"
```

---

## [task_logging_app.py](https://github.com/Attakay78/fastapi-taskflow/blob/main/examples/task_logging_app.py)

Shows structured task logging with levels and extra fields, and how to read task metadata from inside a running task.

**Covers:** `task_log(level=, **extra)`, `get_task_context()`, per-retry log grouping.

```bash
uvicorn examples.task_logging_app:app --reload

# Async task with structured logs
curl -X POST "http://localhost:8000/report?user_id=42"

# Failing task: error + stack trace in dashboard
curl -X POST "http://localhost:8000/report?user_id=0"

# Sync task that fails on first attempt and retries
curl -X POST "http://localhost:8000/sync?user_id=7"

open "http://localhost:8000/tasks/dashboard"
```

---

## [observability_app.py](https://github.com/Attakay78/fastapi-taskflow/blob/main/examples/observability_app.py)

Full observer setup with multiple loggers and structured log extras. Includes a self-contained `InMemoryLogger` test at the bottom of the file that runs without a server.

**Covers:** `FileLogger`, `StdoutLogger`, `InMemoryLogger`, `loggers=[]`, `tags`, `get_task_context()`.

```bash
uvicorn examples.observability_app:app --reload

curl -X POST "http://localhost:8000/invoice?user_id=1&amount=99.00"
tail -f tasks.log

# Run the self-contained test (no server needed)
python examples/observability_app.py
```

---

## [encryption_app.py](https://github.com/Attakay78/fastapi-taskflow/blob/main/examples/encryption_app.py)

Demonstrates Fernet-based argument encryption. Task args and kwargs are encrypted at `add_task()` time and never stored in plain text. The dashboard and API show an `encrypted_payload` field in place of args.

**Covers:** `encrypt_args_key`, key generation, sensitive data (card tokens, email addresses).

```bash
pip install "fastapi-taskflow[encryption]"

# Generate a key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export TASK_ENCRYPTION_KEY="<output>"

uvicorn examples.encryption_app:app --reload

curl -X POST "http://localhost:8000/pay" \
     -H "Content-Type: application/json" \
     -d '{"user_id": 42, "card_token": "tok_visa_4242", "amount": 99.99}'

# Inspect stored record: card_token is not visible in plain text
curl "http://localhost:8000/tasks"
```
