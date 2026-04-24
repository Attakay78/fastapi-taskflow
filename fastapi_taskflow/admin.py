from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .auth import TaskAuthBackend
    from .manager import TaskManager

_AuthParam = Union[
    tuple,  # (username, password)
    list,  # [(username, password), ...]
    "TaskAuthBackend",
    None,
]


class TaskAdmin:
    """Mounts task observability routes onto a FastAPI app and manages the scheduler lifecycle.

    All setup happens in the constructor, so you don't need to keep a reference::

        task_manager = TaskManager(snapshot_db="tasks.db")
        TaskAdmin(app, task_manager)

        # With authentication:
        TaskAdmin(app, task_manager, auth=("admin", "secret"))
        TaskAdmin(app, task_manager, auth=[("alice", "pw1"), ("bob", "pw2")])
        TaskAdmin(app, task_manager, auth=MyBackend(), secret_key="...", token_expiry=3600)

        # Custom mount prefix:
        TaskAdmin(app, task_manager, path="/admin/tasks")

    Routes mounted (relative to *path*):

    * ``GET  {path}``                  -- JSON list of all tasks
    * ``GET  {path}/metrics``          -- aggregated statistics
    * ``GET  {path}/{task_id}``        -- single task detail
    * ``POST {path}/{task_id}/retry``  -- retry a failed/interrupted task
    * ``POST {path}/{task_id}/cancel`` -- cancel a pending task
    * ``GET  {path}/audit``            -- audit log (when *auth* is set)
    * ``GET  {path}/dashboard``        -- live HTML dashboard
    * ``GET  {path}/auth/login``       -- login page (when *auth* is set)
    * ``POST {path}/auth/login``       -- process login form
    * ``GET  {path}/auth/logout``      -- clear session cookie
    """

    def __init__(
        self,
        app: "FastAPI",
        task_manager: "TaskManager",
        path: str = "/tasks",
        display_func_args: bool = False,
        auto_install: bool = False,
        auth: _AuthParam = None,
        token_expiry: int = 86400,
        secret_key: str | None = None,
        poll_interval: float = 30.0,
        title: str = "fastapi-taskflow",
        retention_days: float | None = None,
    ) -> None:
        """
        Args:
            app: The FastAPI application to mount routes onto.
            task_manager: The :class:`~fastapi_taskflow.manager.TaskManager`
                whose tasks will be exposed.
            path: URL prefix for all mounted routes. Default is ``"/tasks"``.
            display_func_args: When ``True``, task arguments are included in
                the dashboard task list. Disable if args may contain sensitive data.
            auto_install: When ``True``, calls :meth:`~fastapi_taskflow.manager.TaskManager.install`
                on *app* so all ``BackgroundTasks`` routes receive managed injection
                automatically. Equivalent to calling ``task_manager.install(app)``
                before creating ``TaskAdmin``.
            auth: Enables login-protected access to the dashboard and API.
                Accepts a ``(username, password)`` tuple, a list of such tuples,
                or a :class:`~fastapi_taskflow.auth.TaskAuthBackend` instance for
                custom authentication logic. ``None`` (default) means no auth.
            token_expiry: Session token lifetime in seconds. Default is 86400 (24 hours).
                Only relevant when *auth* is set.
            secret_key: HMAC secret used to sign session tokens. A secure random
                key is generated automatically when *auth* is set and this is omitted.
                Pass an explicit value to keep sessions valid across restarts.
            poll_interval: How often (seconds) the dashboard polls for updates when
                SSE is unavailable. Default is 30 seconds.
            title: Display name shown in the dashboard header badge and on the login
                page. Defaults to ``"fastapi-taskflow"``.
            retention_days: Override the retention policy set on *task_manager*.
                Terminal task records older than this many days are pruned
                automatically every ~6 hours. ``None`` leaves the manager's
                setting unchanged.
        """
        if auto_install:
            task_manager.install(app)

        if retention_days is not None and task_manager._scheduler is not None:
            task_manager._scheduler._retention_days = retention_days

        from .auth import resolve_backend

        backend = resolve_backend(auth)

        resolved_secret: str | None = None
        if backend is not None:
            resolved_secret = secret_key or secrets.token_hex(32)

        from .dashboard import create_dashboard_router
        from .router import create_router

        if backend is not None:
            assert resolved_secret is not None
            from .auth import create_auth_router

            app.include_router(
                create_auth_router(
                    backend, resolved_secret, token_expiry, prefix=path, title=title
                )
            )

        # Dashboard must be registered before the main router so that
        # /tasks/dashboard is matched before the /{task_id} catch-all.
        app.include_router(
            create_dashboard_router(
                task_manager,
                prefix=path,
                display_func_args=display_func_args,
                secret_key=resolved_secret,
                login_path=f"{path}/auth/login" if backend is not None else None,
                poll_interval=poll_interval,
                title=title,
            )
        )
        app.include_router(
            create_router(
                task_manager,
                prefix=path,
                secret_key=resolved_secret,
            )
        )

        task_manager.init_app(app)
