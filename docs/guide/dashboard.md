# Dashboard

fastapi-taskflow includes a live admin dashboard at `/tasks/dashboard` (or whatever path you configure on `TaskAdmin`).

![Dashboard](../assets/images/dashboard.png)

## Mounting

The dashboard is included automatically when you use `TaskAdmin`:

```python
TaskAdmin(app, task_manager)
# Dashboard available at /tasks/dashboard
```

Custom path:

```python
TaskAdmin(app, task_manager, path="/admin/tasks")
# Dashboard available at /admin/tasks/dashboard
```

## Features

- Live task list updated over SSE (no polling, no page refresh)
- Time-range filter: all, last 1h, 6h, 24h, 7d
- Status filter: all, pending, running, success, failed, interrupted
- Function filter populated from registered tasks
- Search by task ID or function name
- Sortable columns (sort preference persisted across reloads)
- Pagination (30 tasks per page)
- Task detail slide-in panel with three tabs: **Details**, **Logs**, **Error**
- Copy task ID to clipboard
- Metrics row: total, pending, running, success, failed, interrupted, success rate, avg duration
- Pause live updates — buffers incoming SSE events and shows a new-task count; resumes by applying the latest state immediately
- Bulk retry — select multiple failed or interrupted tasks and retry them in one click

## Task detail tabs

The detail panel has three tabs. **Logs** and **Error** are only shown when the task has data for them.

| Tab | Content |
|-----|---------|
| Details | Task ID, function, status, timestamps, duration, retries, analytics, recent runs |
| Logs | Timestamped log entries emitted via `task_log()`. Retry attempts are separated by a `--- Retry N ---` marker. |
| Error | Error message and full stack trace for failed and interrupted tasks. |

## Showing task arguments

```python
TaskAdmin(app, task_manager, display_func_args=True)
```

When enabled, the arguments passed to each task are stored and shown in the detail panel. Useful for debugging without digging through logs.

!!! note
    Arguments are only stored if a persistence backend is configured. In-memory only mode stores them for the current session.

## Poll interval

The SSE stream wakes up immediately on any local task mutation. It also wakes on a configurable poll interval to pick up completed tasks from other instances that have flushed to the shared backend.

```python
TaskAdmin(app, task_manager, poll_interval=5.0)  # default: 30.0 seconds
```

When no backend is configured, the poll interval is used only to send a keep-alive comment. No backend read is performed.

Lower values give fresher cross-instance visibility at the cost of more frequent backend reads. The backend read is cached (default 5s window) so multiple concurrent dashboard clients share a single read per interval.

## Multi-instance deployments

When running multiple instances behind a load balancer, the dashboard shows the live tasks for whichever instance the SSE stream is connected to. Completed tasks from all instances are visible via the shared backend.

For consistent live task visibility, route all dashboard traffic to a single instance using sticky sessions. See the [multi-instance guide](multi-instance.md) for load balancer configuration examples.

## Authentication

The dashboard and all task API endpoints can be protected with login. See the [Authentication guide](authentication.md) for full details.

```python
TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

## Retrying tasks

Failed and interrupted tasks show a **Retry this task** button in the detail panel. Clicking it creates a new task with a fresh task ID using the same function, args, and kwargs as the original. The original record stays in history unchanged.

Interrupted tasks show an additional warning that the function may have already partially executed. Only click retry if you know the function is safe to run again.

The same action is available via the API:

```bash
curl -X POST http://localhost:8000/tasks/{task_id}/retry
```

Response:

```json
{"task_id": "new-uuid", "task": {...}}
```

The retry will fail with `409` if the function is no longer registered in the current process (for example, it was renamed or removed since the task was created).

## Endpoints

All routes are relative to the `path` you configure (default `/tasks`).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | JSON list of all tasks |
| `GET` | `/tasks/metrics` | JSON aggregated statistics |
| `GET` | `/tasks/{task_id}` | JSON single task detail |
| `POST` | `/tasks/{task_id}/retry` | Retry a failed or interrupted task |
| `GET` | `/tasks/dashboard` | HTML dashboard |
| `GET` | `/tasks/dashboard/stream` | SSE event stream |

## SSE event format

The dashboard subscribes to `/tasks/dashboard/stream` which emits a single `state` event on each update:

```
event: state
data: {"tasks": [...], "metrics": {...}}
```

The client re-renders the full table on each event. There is no partial update.
