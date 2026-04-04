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
- Status filter: all, pending, running, success, failed
- Function filter populated from registered tasks
- Search by task ID or function name
- Sortable columns
- Pagination (30 tasks per page)
- Task detail slide-in panel with timeline and metadata
- Copy task ID to clipboard
- Metrics row: total, pending, running, success, failed, success rate, avg duration

## Showing task arguments

```python
TaskAdmin(app, task_manager, display_func_args=True)
```

When enabled, the arguments passed to each task are stored and shown in the detail panel. Useful for debugging without digging through logs.

!!! note
    Arguments are only stored if a persistence backend is configured. In-memory only mode stores them for the current session.

## Authentication

The dashboard and all task API endpoints can be protected with login. See the [Authentication guide](authentication.md) for full details.

```python
TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

## Endpoints

All routes are relative to the `path` you configure (default `/tasks`).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | JSON list of all tasks |
| `GET` | `/tasks/metrics` | JSON aggregated statistics |
| `GET` | `/tasks/{task_id}` | JSON single task detail |
| `GET` | `/tasks/dashboard` | HTML dashboard |
| `GET` | `/tasks/dashboard/stream` | SSE event stream |

## SSE event format

The dashboard subscribes to `/tasks/dashboard/stream` which emits a single `state` event on each update:

```
event: state
data: {"tasks": [...], "metrics": {...}}
```

The client re-renders the full table on each event. There is no partial update.
