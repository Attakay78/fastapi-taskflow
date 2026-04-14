# Argument Encryption

When tasks carry sensitive data (API keys, payment details, PII), you can encrypt args and kwargs at rest using Fernet (AES-128-CBC + HMAC).

With `encrypt_args_key` set, task arguments are encrypted at `add_task()` time and decrypted only inside the executor just before the function is called. They are never stored in plain text in the in-memory store, the SQLite or Redis backend, or any log file.

## Installation

Encryption requires the `cryptography` package:

```bash
pip install "fastapi-taskflow[encryption]"
```

## Setup

```python
import os
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="tasks.db",
    encrypt_args_key=os.environ["TASK_ENCRYPTION_KEY"],
)
```

## Generating a key

Generate a key once and store it in an environment variable or a secrets manager. Never hard-code it in source.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the output in your environment:

```bash
export TASK_ENCRYPTION_KEY="<output from above>"
```

Or in a secrets manager (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager):

```python
import os
import boto3

secret = boto3.client("secretsmanager").get_secret_value(SecretId="task-encryption-key")
os.environ["TASK_ENCRYPTION_KEY"] = secret["SecretString"]
```

## Usage

After setting `encrypt_args_key`, the behaviour of `add_task()` and the task function itself does not change:

```python
@task_manager.task(retries=3, delay=1.0)
def charge_payment(user_id: int, card_token: str, amount: float) -> None:
    # card_token is only in plain text inside this function body.
    # It was never written to the task store or the database.
    task_log("Charging payment", user_id=user_id, amount=amount)
    gateway.charge(card_token, amount)


@app.post("/pay")
def pay(user_id: int, card_token: str, amount: float, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(
        charge_payment,
        user_id=user_id,
        card_token=card_token,
        amount=amount,
    )
    return {"task_id": task_id}
```

Inspecting the task store or the database will show an `encrypted_payload` field in place of the args. The `args` and `kwargs` fields will be empty tuples and dicts.

## What is encrypted

- Task `args` and `kwargs` passed to `add_task()`.
- The payload is stored as a Fernet token (URL-safe base64) in the `encrypted_payload` field on `TaskRecord`.
- Encryption uses `pickle.dumps((args, kwargs))` before Fernet encryption. Decryption reverses this.

## What is not encrypted

- `func_name` - always stored and logged in plain text.
- `task_id`, `status`, timestamps, retry counts - stored in plain text.
- `task_log()` messages and extras - always stored in plain text. Do not log sensitive values with `task_log()`.
- Tags attached via `tags=` - stored in plain text.

## Key rotation

To rotate the encryption key:

1. Generate a new key.
2. Drain or complete any in-flight tasks that were encrypted with the old key (they will fail to decrypt after rotation).
3. Update the environment variable to the new key and redeploy.

Tasks enqueued before rotation that are still in the pending store will fail to decrypt and must be re-submitted manually after rotation.

## See also

- [TaskManager reference](../api/task-manager.md)
- [Adding Tasks](adding-tasks.md)
