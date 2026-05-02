# Injection Patterns

This page explains how fastapi-taskflow integrates with FastAPI's dependency injection system and which pattern fits your situation.

## Why injection matters

FastAPI's `BackgroundTasks` type gets special treatment. When FastAPI sees that annotation in a route signature, it creates the instance directly, bypassing the normal dependency injection graph entirely. That means `dependency_overrides` cannot intercept it.

fastapi-taskflow needs to intercept that moment so the injected instance is a `ManagedBackgroundTasks` instead of a plain `BackgroundTasks`. That is what `install()` does: it replaces the class reference FastAPI uses internally so every injection point in your app receives a managed instance.

There are three ways to wire this up. All three produce a `ManagedBackgroundTasks` instance and behave identically at runtime.

## Pattern 1: Native `BackgroundTasks` annotation

This is the recommended starting point for existing codebases. No import changes, no signature changes.

**Requires** `auto_install=True` on `TaskAdmin`, or a call to `task_manager.install(app)`.

```python hl_lines="4 5"
from fastapi import BackgroundTasks

@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_email, address=email)
    return {"task_id": task_id}
```

Because `install()` patches the class FastAPI uses to create the instance, your annotation does not need to change. The object you receive is already a `ManagedBackgroundTasks`.

## Pattern 2: Explicit `ManagedBackgroundTasks` annotation

**Requires** `auto_install=True` or `task_manager.install(app)`.

```python
from fastapi_taskflow import ManagedBackgroundTasks

@app.post("/webhook")
def webhook(background_tasks: ManagedBackgroundTasks):
    task_id = background_tasks.add_task(process_webhook, {"event": "order.created"})
    return {"task_id": task_id}
```

Use this when you want the annotation to communicate intent clearly, or when your type checker needs to know that `add_task` returns a `str` (the task UUID).

!!! note "Why `install()` is still required"
    Annotating `ManagedBackgroundTasks` alone is not enough. FastAPI ignores the subclass and injects the base type anyway. The patch from `install()` is still what makes the injected object a managed instance.

## Pattern 3: Explicit `Depends`

This pattern does not need `install()` at all. It goes through the normal FastAPI dependency graph, so no process-wide patch is applied.

```python
from fastapi import Depends
from fastapi_taskflow import TaskManager

task_manager = TaskManager()

@app.post("/signup")
def signup(email: str, tasks=Depends(task_manager.get_tasks)):
    task_id = tasks.add_task(send_email, address=email)
    return {"task_id": task_id}
```

Choose this when you cannot apply the process-wide patch, for example when you run multiple FastAPI apps in a single process and only want managed injection on one of them.

## Pattern summary

| Pattern | `install()` required | Type checker sees return type |
|---|---|---|
| `background_tasks: BackgroundTasks` | Yes | No (base type) |
| `background_tasks: ManagedBackgroundTasks` | Yes | Yes |
| `tasks=Depends(task_manager.get_tasks)` | No | Only if annotated explicitly |

## Multi-level dependencies

FastAPI lets you declare `BackgroundTasks` at multiple levels: in a route, in a dependency, and in sub-dependencies. fastapi-taskflow supports the same, with behaviour that depends on which pattern you use.

### With `install()` active

FastAPI creates one `BackgroundTasks` instance per request and reuses it at every injection point. Because `install()` replaces the class used to create that instance, every `BackgroundTasks` annotation at every level receives the same `ManagedBackgroundTasks` object.

```python
def notify_service(background_tasks: BackgroundTasks):
    background_tasks.add_task(send_notification, ...)   # managed

@app.post("/signup")
def signup(background_tasks: BackgroundTasks, svc=Depends(notify_service)):
    background_tasks.add_task(send_email, ...)          # managed, same instance
```

### With `Depends(task_manager.get_tasks)`

FastAPI caches dependency results within a request. If multiple levels declare `Depends(task_manager.get_tasks)`, FastAPI calls `get_tasks` once and passes the same instance everywhere.

```python
def notify_service(tasks=Depends(task_manager.get_tasks)):
    tasks.add_task(send_notification, ...)

@app.post("/signup")
def signup(tasks=Depends(task_manager.get_tasks), svc=Depends(notify_service)):
    tasks.add_task(send_email, ...)   # same instance as notify_service receives
```

## The mixed case to avoid

!!! warning "Mixing patterns without `install()` causes silent data loss"
    If a sub-dependency uses `Depends(task_manager.get_tasks)` but the route declares `BackgroundTasks` without `install()` active, the two are different objects. Tasks added through the native annotation are not tracked.

```python hl_lines="4 8"
# install() is NOT active in this example

def notify_service(tasks=Depends(task_manager.get_tasks)):
    tasks.add_task(send_notification, ...)   # managed, tracked

@app.post("/signup")
def signup(background_tasks: BackgroundTasks, svc=Depends(notify_service)):
    background_tasks.add_task(send_email, ...)   # NOT managed, no UUID, no retries
```

Both task lists share the same underlying Starlette list, so both tasks run. The problem is silent: the route-level call bypasses the managed wrapper, so that task gets no UUID, no retry tracking, and no dashboard visibility.

The fix is to either activate `install()` so the route annotation is also managed, or switch the route to `Depends(task_manager.get_tasks)` to be consistent throughout.

## Using `install()` without the dashboard

If you are not mounting `TaskAdmin`, call `install()` directly on your app:

```python
from fastapi_taskflow import TaskManager
from fastapi import FastAPI

task_manager = TaskManager()
app = FastAPI()
task_manager.install(app)
```

!!! warning "Process-wide scope"
    `install()` modifies a module-level reference inside `fastapi.dependencies.utils`. It applies to every route in the process, not just the app you pass in. In a single-app deployment this is a non-issue. If you run multiple FastAPI apps in the same process and only want managed injection on one of them, use Pattern 3 instead.

## Type checker behaviour

`BackgroundTasks.add_task()` is typed to return `None`. If your type checker flags that a variable assigned from `add_task()` is `None`, switch to the `ManagedBackgroundTasks` annotation (Pattern 2) or add an explicit annotation:

```python
from fastapi_taskflow import ManagedBackgroundTasks

@app.post("/order")
def create_order(tasks: ManagedBackgroundTasks):
    task_id: str = tasks.add_task(process_order, order_id=42)
    return {"task_id": task_id}
```

!!! tip
    Pattern 2 is the cleanest option when you care about return-type accuracy in your IDE. The behaviour at runtime is identical to Pattern 1.
