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
    """
    Mounts the task observability routes onto a FastAPI application and
    manages the snapshot scheduler lifecycle (if enabled on the TaskManager).

    All work is done in the constructor — no need to keep a reference::

        task_manager = TaskManager(snapshot_db="tasks.db")
        TaskAdmin(app, task_manager)

        # With authentication:
        TaskAdmin(app, task_manager, auth=("admin", "secret"))
        TaskAdmin(app, task_manager, auth=[("alice", "pw1"), ("bob", "pw2")])
        TaskAdmin(app, task_manager, auth=MyBackend(), secret_key="...", token_expiry=3600)

        # Custom mount path:
        TaskAdmin(app, task_manager, path="/admin/tasks")

    Routes exposed (relative to *path*):

    * ``GET {path}``                  — JSON list of all tasks
    * ``GET {path}/metrics``          — JSON aggregated statistics
    * ``GET {path}/{task_id}``        — JSON single task detail
    * ``GET {path}/dashboard``        — live dashboard (HTML)
    * ``GET {path}/auth/login``       — login page (when auth is configured)
    * ``POST {path}/auth/login``      — process login (when auth is configured)
    * ``GET {path}/auth/logout``      — logout (when auth is configured)
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
    ) -> None:
        self._task_manager = task_manager

        if auto_install:
            task_manager.install(app)

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
                create_auth_router(backend, resolved_secret, token_expiry, prefix=path)
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
            )
        )
        app.include_router(
            create_router(
                task_manager,
                prefix=path,
                secret_key=resolved_secret,
            )
        )

        if task_manager._scheduler is not None:
            app.router.on_startup.append(self._on_startup)
            app.router.on_shutdown.append(self._on_shutdown)

    async def _on_startup(self) -> None:
        scheduler = self._task_manager._scheduler
        assert scheduler is not None
        await scheduler.load()
        if scheduler._requeue_pending:
            await scheduler.requeue()
        scheduler.start()

    async def _on_shutdown(self) -> None:
        scheduler = self._task_manager._scheduler
        assert scheduler is not None
        scheduler.stop()
        await scheduler.flush()
        if scheduler._requeue_pending:
            await scheduler.flush_pending()
