# Injection Patterns

fastapi-taskflow supports three ways to inject managed tasks into your routes. All three produce a `ManagedBackgroundTasks` instance and behave identically at runtime.

## How FastAPI injects BackgroundTasks

FastAPI treats `BackgroundTasks` as a special built-in type. When it sees the annotation in a route signature, it creates the instance directly (not via the dependency injection graph). This means `dependency_overrides` cannot intercept it — the instantiation is hardcoded.

fastapi-taskflow works around this by patching the reference FastAPI uses to create the instance. `auto_install=True` or `task_manager.install(app)` replaces that reference process-wide so the injected instance is a `ManagedBackgroundTasks`.

## Pattern 1: Native BackgroundTasks annotation (zero migration)

Requires `auto_install=True` on `TaskAdmin` or a call to `task_manager.install(app)`.

```python
from fastapi import BackgroundTasks

@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_email, address=email)
    return {"task_id": task_id}
```

This is the preferred pattern for existing codebases. No imports change, no signature changes.

## Pattern 2: Explicit ManagedBackgroundTasks annotation

Also requires `auto_install=True`. Annotating `ManagedBackgroundTasks` alone is not enough — FastAPI still ignores the subclass and injects the base type without the patch.

```python
from fastapi_taskflow import ManagedBackgroundTasks

@app.post("/webhook")
def webhook(background_tasks: ManagedBackgroundTasks):
    task_id = background_tasks.add_task(process_webhook, {"event": "order.created"})
    return {"task_id": task_id}
```

Use this when you want the annotation to communicate intent clearly, or when type checkers need to know `add_task` returns `str`.

## Pattern 3: Explicit Depends (no install required)

```python
from fastapi import Depends

@app.post("/signup")
def signup(email: str, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(send_email, address=email)
    return {"task_id": task_id}
```

This pattern works without `install()` because it goes through the normal FastAPI dependency graph. Use it if you cannot or do not want to apply the process-wide patch, or if you have multiple apps in one process.

## Pattern summary

| Pattern | `install()` required | Type checker friendly |
|---------|---------------------|----------------------|
| `background_tasks: BackgroundTasks` | Yes | No (native type) |
| `background_tasks: ManagedBackgroundTasks` | Yes | Yes |
| `tasks=Depends(task_manager.get_tasks)` | No | No (untyped unless annotated) |

## No dashboard

If you are not using `TaskAdmin`, call `install` directly:

```python
task_manager = TaskManager()
app = FastAPI()
task_manager.install(app)
```

!!! warning "Process-wide patch"
    `install()` modifies a module-level reference inside `fastapi.dependencies.utils`. It applies to every route in the process, not just the app you pass. In single-app deployments this is a non-issue. If you run multiple FastAPI apps in the same process and only want managed injection on one of them, use Pattern 3 instead.
