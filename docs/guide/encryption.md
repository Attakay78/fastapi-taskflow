# Argument Encryption

This page explains why you might want to encrypt task arguments, how to enable encryption, what is and is not protected, and how to rotate your key safely.

## Why encrypt task arguments

When you enqueue a task, its arguments are serialised and saved to the task store. Without encryption, anything passed to `add_task()` is readable by anyone with access to that store, whether that is a SQLite file on disk, a Redis key, or a database row.

Tasks that carry PII (names, email addresses, national IDs), payment data (card tokens, account numbers), or short-lived secrets (API keys, one-time passwords) should not leave those values sitting in plain text.

With `encrypt_args_key` set, arguments are encrypted at `add_task()` time and only decrypted inside the executor, just before the function is called. The plain-text values are never written to the in-memory store, the SQLite or Redis backend, or any log file.

## Installation

Encryption depends on the `cryptography` package, which is not installed by default:

```bash
pip install "fastapi-taskflow[encryption]"
```

## Generating a key

Generate a key once using the `cryptography` library. Keep this value secret and never commit it to source control.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The output is a URL-safe base64 string, for example:

```
dGhpcyBpcyBhIHRlc3Qga2V5IGZvciBkb2NzIQ==
```

Store it somewhere safe before the next step.

## Configuring TaskManager

Pass the key to `TaskManager` via an environment variable:

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    encrypt_args_key=os.environ["TASK_ENCRYPTION_KEY"],
)
```

After this, `add_task()` and the task function itself work exactly the same way. The encryption and decryption happen transparently.

```python
import os
from fastapi import Depends, FastAPI
from fastapi_taskflow import TaskManager, task_log

task_manager = TaskManager(
    snapshot_db="tasks.db",
    encrypt_args_key=os.environ["TASK_ENCRYPTION_KEY"],
)
app = FastAPI()


@task_manager.task(retries=3, delay=1.0)
def charge_payment(user_id: int, card_token: str, amount: float) -> None:
    # card_token is plain text inside this function.
    # It was never written to the store or the database.
    task_log("Charging payment", user_id=user_id, amount=amount)
    gateway.charge(card_token, amount)


@app.post("/pay")
def pay(
    user_id: int,
    card_token: str,
    amount: float,
    tasks=Depends(task_manager.get_tasks),
):
    task_id = tasks.add_task(
        charge_payment,
        user_id=user_id,
        card_token=card_token,
        amount=amount,
    )
    return {"task_id": task_id}
```

Inspecting the task record in the store or database shows an `encrypted_payload` field in place of the arguments. The `args` and `kwargs` fields are empty.

## What is encrypted

- Task `args` and `kwargs` passed to `add_task()`.
- The payload is stored as a Fernet token (URL-safe base64, AES-128-CBC + HMAC) in the `encrypted_payload` field on `TaskRecord`.

## What is not encrypted

| Field | Stored as |
|---|---|
| `func_name` | Plain text always. |
| `task_id`, `status`, timestamps, retry counts | Plain text always. |
| `task_log()` messages and extra keyword arguments | Plain text always. |
| Tags attached via `tags=` | Plain text always. |

!!! warning
    Do not log sensitive values with `task_log()`. Those messages are stored and displayed in the dashboard as plain text, regardless of whether argument encryption is enabled.

## Where to store the key

**Environment variable** is the simplest approach for most deployments:

```bash
export TASK_ENCRYPTION_KEY="<your generated key>"
```

**Secrets manager** is the right choice for production environments where you want centralised access control and audit logging:

```python
import boto3

secret = boto3.client("secretsmanager").get_secret_value(
    SecretId="myapp/task-encryption-key"
)
os.environ["TASK_ENCRYPTION_KEY"] = secret["SecretString"]
```

The same pattern works with HashiCorp Vault, GCP Secret Manager, and Azure Key Vault.

!!! tip
    Never hard-code the key in source code. If the key is ever committed to a repository, rotate it immediately and treat any encrypted payloads as potentially compromised.

## Key rotation

Rotating a Fernet key is a destructive operation because tasks encrypted with the old key cannot be decrypted with the new one. Follow this sequence to avoid data loss:

1. Wait for all in-flight tasks (PENDING and RUNNING) to complete. Tasks still in the store at rotation time will fail with a decryption error after the key changes.
2. Generate a new key with the command above.
3. Update the environment variable (or secrets manager entry) to the new key.
4. Redeploy your application.

Any tasks that were still pending when you rotated will fail on their next execution attempt. They must be re-submitted manually after the new key is in place.

!!! note
    There is no automatic re-encryption of existing records. The rotation process assumes you can either drain pending tasks first, or accept that a small number may need to be resubmitted.

## See also

- [TaskManager reference](../api/task-manager.md)
- [Adding Tasks](adding-tasks.md)
