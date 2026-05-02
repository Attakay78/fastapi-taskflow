# Models

Data classes and enumerations that represent task state throughout the fastapi-taskflow lifecycle.

> **Guide:** [Defining Tasks](../guide/defining-tasks.md) explains how these types are created and used at runtime.

---

## TaskStatus

`TaskStatus` is a `str` enum that tracks the lifecycle state of a task from creation to completion.

```python
from fastapi_taskflow.models import TaskStatus

class TaskStatus(str, Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    SUCCESS     = "success"
    FAILED      = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED   = "cancelled"
```

| Value | Meaning |
|-------|---------|
| `PENDING` | Task has been enqueued but has not started executing yet. |
| `RUNNING` | Task is currently executing. |
| `SUCCESS` | Task completed without raising an exception. Terminal. |
| `FAILED` | Task raised an exception on its final attempt after all retries were exhausted. Terminal. |
| `INTERRUPTED` | Task was mid-execution when the app shut down and `requeue_on_interrupt` was not enabled. Saved to history, visible in the dashboard, and not re-executed automatically. Can be retried via `POST /tasks/{task_id}/retry`. Terminal. |
| `CANCELLED` | Task was cancelled before execution via `POST /tasks/{task_id}/cancel`. Terminal; the task will not run. |

---

## TaskRecord

`TaskRecord` holds live and persisted state for a single task invocation.

```python
from fastapi_taskflow.models import TaskRecord
```

Created by `add_task()` and updated as the task progresses through its lifecycle. Stored in `TaskStore` in memory and persisted to the backend when the task completes.

```python
@dataclass
class TaskRecord:
    task_id:           str
    func_name:         str
    status:            TaskStatus
    args:              tuple
    kwargs:            dict
    created_at:        datetime
    start_time:        datetime | None
    end_time:          datetime | None
    retries_used:      int
    error:             str | None
    logs:              list[str]
    stacktrace:        str | None
    idempotency_key:   str | None
    tags:              dict[str, str]
    encrypted_payload: bytes | None
    source:            str
    priority:          int | None
    executor:          str | None
```

### Fields

Fields marked **auto** are set by the framework. Fields marked **caller** are provided at enqueue time via `add_task()`.

| Field | Type | Set by | Description |
|-------|------|--------|-------------|
| `task_id` | `str` | auto | UUID assigned when `add_task()` is called. |
| `func_name` | `str` | auto | Name of the function registered with `@task_manager.task()`. |
| `status` | `TaskStatus` | auto | Current lifecycle state. |
| `args` | `tuple` | caller | Positional arguments the task was called with. Empty when `encrypted_payload` is set. |
| `kwargs` | `dict` | caller | Keyword arguments the task was called with. Empty when `encrypted_payload` is set. |
| `created_at` | `datetime` | auto | UTC time when `add_task()` was called. |
| `start_time` | `datetime \| None` | auto | UTC time when the executor started running the function. `None` until the task begins. |
| `end_time` | `datetime \| None` | auto | UTC time when the task reached a terminal state. `None` until complete. |
| `retries_used` | `int` | auto | Number of retry attempts that have run so far. |
| `error` | `str \| None` | auto | String form of the last exception, if the task failed. `None` on success. |
| `logs` | `list[str]` | auto | Timestamped entries written via `task_log()`, in order. Each entry is formatted as `YYYY-MM-DDTHH:MM:SS message`. Retry separators (`--- Retry N ---`) are inserted automatically by the executor. For process tasks, all entries appear after the function returns. |
| `stacktrace` | `str \| None` | auto | Full Python traceback from the last failed attempt. `None` if the task succeeded or has not failed yet. For process tasks, this is the traceback captured inside the worker process. |
| `idempotency_key` | `str \| None` | caller | Optional caller-supplied deduplication key. If a non-failed task with the same key already exists, `add_task()` returns the existing `task_id` without enqueuing a new task. |
| `tags` | `dict[str, str]` | caller | Key/value labels attached at enqueue time. Forwarded to every `LogEvent` and `LifecycleEvent`. Stored as part of the snapshot payload, not as a separate column. |
| `encrypted_payload` | `bytes \| None` | auto | Fernet-encrypted `(args, kwargs)` when `encrypt_args_key` is configured on `TaskManager`. When present, `args` and `kwargs` are stored empty. Not included in `to_dict()` output or API responses. |
| `source` | `str` | auto | How the task was created: `"manual"` for tasks enqueued via `add_task()`, `"scheduled"` for tasks fired by the periodic scheduler. |
| `priority` | `int \| None` | caller | Priority level assigned at enqueue time. `None` when routed through the standard Starlette mechanism. Any integer when routed through the priority queue; higher values run first. |
| `executor` | `str \| None` | auto | The executor that ran (or will run) this task: `"async"`, `"thread"`, or `"process"`. Reflects the effective executor after auto-detection. Shown in the dashboard detail panel. |

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `duration` | `float \| None` | Elapsed seconds between `start_time` and `end_time`. `None` if the task has not yet completed. |

### Methods

#### `to_dict() -> dict`

Returns a JSON-serialisable representation of the record, used by the REST API and the dashboard.

Includes: `task_id`, `func_name`, `status`, `created_at`, `start_time`, `end_time`, `duration`, `retries_used`, `error`, `logs`, `stacktrace`, `tags`, `source`, `priority`, `executor`.

Does not include `args`, `kwargs`, or `encrypted_payload`.

```python
record = task_manager.store.get("abc123")
data = record.to_dict()
# data["status"] == "success"
# data["executor"] == "process"
# data["logs"] == ["2026-04-30T10:00:01 Report complete"]
```

---

## TaskContext

`TaskContext` provides execution metadata accessible from inside a running task via `get_task_context()`.

```python
from fastapi_taskflow import get_task_context

@task_manager.task()
def my_task() -> None:
    ctx = get_task_context()
    print(ctx.task_id, ctx.attempt)
```

```python
@dataclass
class TaskContext:
    task_id:   str
    func_name: str
    attempt:   int
    tags:      dict[str, str]
```

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | UUID of the currently running task. |
| `func_name` | `str` | Name of the task function as registered with `@task_manager.task()`. |
| `attempt` | `int` | Zero-based retry index. `0` means the first run, `1` means the first retry. |
| `tags` | `dict[str, str]` | Key/value labels attached at enqueue time. |

`TaskContext` is available inside process tasks too. The worker reconstructs it from the serialized payload before calling the function.

See the [Task Context guide](../guide/task-context.md) for usage examples.

---

## TaskConfig

`TaskConfig` holds execution settings stored alongside a registered function in the task registry. You never instantiate this directly; it is created by `@task_manager.task()` and stored in `TaskRegistry`.

```python
@dataclass
class TaskConfig:
    retries:              int = 0
    delay:                float = 0.0
    backoff:              float = 1.0
    persist:              bool = False
    name:                 str | None = None
    requeue_on_interrupt: bool = False
    eager:                bool = False
    priority:             int | None = None
    executor:             str | None = None
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `retries` | `int` | `0` | Additional attempts after the first failure. A value of `3` means up to 4 total attempts. |
| `delay` | `float` | `0.0` | Seconds to wait before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to `delay` on each subsequent retry. `2.0` gives exponential backoff. |
| `persist` | `bool` | `False` | Save this task record to the backend for requeue on restart. |
| `name` | `str \| None` | `None` | Display name in logs and the dashboard. Defaults to the function's `__name__`. |
| `requeue_on_interrupt` | `bool` | `False` | When `True`, a task interrupted at shutdown is reset to `PENDING` and re-dispatched on next startup. Only safe for idempotent tasks. |
| `eager` | `bool` | `False` | Dispatch via `asyncio.create_task` immediately when `add_task()` is called rather than after the response is sent. |
| `priority` | `int \| None` | `None` | Execution priority. `None` routes through the standard Starlette mechanism. Any integer routes through the priority queue; higher values run first. |
| `executor` | `str \| None` | `None` | Configured executor. `"async"`, `"thread"`, or `"process"`. `None` means auto-detect from the function signature at dispatch time. |

---

## AuditEntry

`AuditEntry` is a single record in the in-memory audit log, written whenever a retry or cancel action is performed via the API.

```python
@dataclass
class AuditEntry:
    entry_id:  str
    action:    str
    task_id:   str
    actor:     str
    timestamp: datetime
    detail:    dict
```

| Field | Type | Description |
|-------|------|-------------|
| `entry_id` | `str` | UUID assigned to this audit entry. |
| `action` | `str` | The action taken: `"retry"` or `"cancel"`. |
| `task_id` | `str` | The task that was acted on. |
| `actor` | `str` | Username of the authenticated user, or `"anonymous"` when auth is not configured. |
| `timestamp` | `datetime` | UTC time when the action occurred. |
| `detail` | `dict` | Action-specific data. For retries, includes `{"new_task_id": "..."}` with the ID of the newly enqueued task. |

The last 1000 entries are kept in memory. Access them via `GET /tasks/audit`.
