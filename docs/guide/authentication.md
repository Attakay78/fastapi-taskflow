# Authentication

This page explains how to protect the dashboard and task API with login, how to use a custom auth backend, how to configure session tokens, and how auth works at runtime.

## Why protect the dashboard?

The dashboard and API endpoints expose task history, function arguments, and runtime metrics. In a production deployment or on a shared network, you will want to restrict access to those routes.

fastapi-taskflow includes a built-in login system so you can add protection without pulling in a separate auth library. By default, all routes are open. Auth is opt-in.

## Single user setup

Pass a `(username, password)` tuple to `TaskAdmin`:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auth=("admin", "secret"))
```

That is the minimum configuration. The dashboard and all API endpoints are now protected.

## Multiple users

Pass a list of `(username, password)` tuples:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auth=[
    ("alice", "password1"),
    ("bob",   "password2"),
])
```

Any user in the list can log in. There is no role differentiation; all authenticated users have the same access.

## Custom backend

For anything beyond a static user list, subclass `TaskAuthBackend` and implement `authenticate_user`. The method receives the submitted username and password and returns `True` if the credentials are valid:

```python hl_lines="10"
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager, TaskAuthBackend


class DatabaseAuthBackend(TaskAuthBackend):
    def authenticate_user(self, username: str, password: str) -> bool:
        user = db.query(User).filter_by(username=username).first()
        if user is None:
            return False
        return check_password_hash(user.password_hash, password)


task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager, auth=DatabaseAuthBackend())
```

You can verify credentials against any source: a database, LDAP, an external identity provider, or a secrets manager. The only contract is the return value.

!!! tip
    Keep `authenticate_user` fast. It runs on every login attempt and blocks the response until it returns. For slow credential checks (network calls, for example), consider adding a short-lived in-process cache for successful verifications.

## Token configuration

fastapi-taskflow uses HMAC-SHA256 signed tokens stored as HTTP-only cookies. Two parameters control session behavior:

| Parameter | Default | Description |
|---|---|---|
| `token_expiry` | `86400` | Seconds until the session token expires (default: 24 hours) |
| `secret_key` | `None` | Signing key. Auto-generated on each startup if not set. |

```python
import os

TaskAdmin(
    app,
    task_manager,
    auth=("admin", "secret"),
    secret_key=os.environ["TASKFLOW_SECRET_KEY"],
    token_expiry=3600,  # 1-hour sessions
)
```

!!! warning "Set an explicit `secret_key` in production"
    If `secret_key` is not set, a random key is generated each time the application starts. Every existing session is invalidated on every restart or redeploy.

    Set an explicit `secret_key` loaded from an environment variable or secrets manager to keep sessions valid across restarts:

    ```python
    import os

    TaskAdmin(
        app,
        task_manager,
        auth=("admin", "secret"),
        secret_key=os.environ["TASKFLOW_SECRET_KEY"],
    )
    ```

    Use a long, random string (at least 32 characters) and treat it like a password. Anyone who knows the secret key can forge valid session tokens.

## How auth works at runtime

When `auth` is configured, every request to the dashboard and API goes through a token check:

1. Unauthenticated requests to `/tasks/dashboard` are redirected to `/tasks/auth/login`.
2. Unauthenticated requests to the JSON API endpoints (`/tasks`, `/tasks/metrics`, `/tasks/{id}`) receive a `401 Unauthorized` response, not a redirect. This keeps API clients from receiving an unexpected HTML login page.
3. On successful login, a signed token is written as an HTTP-only `taskflow_token` cookie. HTTP-only cookies are not readable from JavaScript.
4. Visiting `/tasks/auth/logout` clears the cookie and redirects to the login page.
5. The SSE stream at `/tasks/dashboard/stream` is not separately auth-protected. The dashboard page that embeds it is, so only authenticated sessions reach the stream in practice.

!!! note
    The login page is served at `{path}/auth/login` and matches the dashboard's visual style. If you mount `TaskAdmin` at a custom path prefix, the login path moves with it.

## Disabling auth for internal-only deployments

If the dashboard is only reachable inside a private network or behind an authenticating reverse proxy, you can leave auth disabled entirely. This is the default:

```python
from fastapi import FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager()
app = FastAPI()

TaskAdmin(app, task_manager)  # auth=None by default; all routes are open
```

!!! info
    Leaving auth disabled is appropriate when access is already controlled at the network or reverse-proxy layer. Do not expose an unauthenticated dashboard to the public internet.
