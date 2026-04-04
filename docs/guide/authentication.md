# Authentication

By default, the dashboard and task API endpoints are open. Pass an `auth` argument to `TaskAdmin` to require login.

## Enabling auth

### Single user

```python
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

### Multiple users

```python
TaskAdmin(app, task_manager, auth=[
    ("alice", "password1"),
    ("bob",   "password2"),
])
```

### Custom backend

Subclass `TaskAuthBackend` to verify credentials against any source — a database, LDAP, an external service, or anything else:

```python
from fastapi_taskflow import TaskAdmin, TaskAuthBackend

class MyAuthBackend(TaskAuthBackend):
    def authenticate_user(self, username: str, password: str) -> bool:
        # Return True if credentials are valid, False otherwise
        return check_your_database(username, password)

TaskAdmin(app, task_manager, auth=MyAuthBackend())
```

## Token configuration

Authentication uses HMAC-SHA256 signed tokens stored as HTTP-only cookies.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `token_expiry` | `86400` | Seconds until the token expires (default: 24 hours) |
| `secret_key` | `None` | Signing key. Auto-generated if not set. |

```python
TaskAdmin(
    app,
    task_manager,
    auth=("admin", "secret"),
    secret_key="a-long-random-string",
    token_expiry=3600,  # 1 hour sessions
)
```

!!! warning "Persistent sessions"
    If `secret_key` is omitted, a random key is generated each time the application starts. All active sessions are invalidated on restart. Set an explicit `secret_key` if you need sessions to survive restarts.

## How it works

When `auth` is configured:

1. Unauthenticated requests to `/tasks/dashboard` are redirected to `/tasks/auth/login`.
2. Unauthenticated requests to the JSON API endpoints (`/tasks`, `/tasks/metrics`, `/tasks/{id}`) receive a `401 Unauthorized` response.
3. On successful login, a signed token is set as an HTTP-only `taskflow_token` cookie.
4. The SSE stream (`/tasks/dashboard/stream`) is not auth-protected — the dashboard page that embeds it is.
5. Visiting `/tasks/auth/logout` clears the cookie and redirects to the login page.

## Login page

The login page is served at `{path}/auth/login` and matches the dashboard's visual style.

![Login page showing fastapi-taskflow branding with username and password fields](../assets/images/dashboard.png)

## Disabling auth

Set `auth=None` (the default) to serve all routes without any authentication:

```python
TaskAdmin(app, task_manager)  # no auth
```
