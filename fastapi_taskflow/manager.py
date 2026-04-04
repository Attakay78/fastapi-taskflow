from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .models import TaskConfig
from .registry import TaskRegistry
from .store import TaskStore

from fastapi import BackgroundTasks

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .backends.base import SnapshotBackend
    from .wrapper import ManagedBackgroundTasks


class TaskManager:
    """
    Central object that holds the registry and store, and exposes the
    ``@task_manager.task`` decorator and ``get_tasks`` FastAPI dependency.

    Usage::

        task_manager = TaskManager(snapshot_db="tasks.db")

        @task_manager.task(retries=3, delay=2.0, backoff=2.0)
        def send_email(address: str) -> None:
            ...

        # Mount the admin dashboard (routes + lifecycle):
        admin = TaskAdmin(app, task_manager)

        # Inject managed tasks into any route:
        @app.post("/signup")
        def signup(email: str, tasks=Depends(task_manager.get_tasks)):
            task_id = tasks.add_task(send_email, email)
            return {"task_id": task_id}
    """

    def __init__(
        self,
        *,
        snapshot_db: Optional[str] = None,
        snapshot_backend: Optional["SnapshotBackend"] = None,
        snapshot_interval: float = 60.0,
        requeue_pending: bool = False,
    ) -> None:
        self.registry = TaskRegistry()
        self.store = TaskStore()

        self._scheduler = None
        if snapshot_backend is not None or snapshot_db is not None:
            from .snapshot import SnapshotScheduler

            if snapshot_backend is None:
                from .backends.sqlite import SqliteBackend

                snapshot_backend = SqliteBackend(snapshot_db)  # type: ignore[arg-type]

            self._scheduler = SnapshotScheduler(
                self,
                backend=snapshot_backend,
                interval=snapshot_interval,
                requeue_pending=requeue_pending,
            )

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def task(
        self,
        *,
        retries: int = 0,
        delay: float = 0.0,
        backoff: float = 1.0,
        persist: bool = False,
        name: Optional[str] = None,
    ) -> Callable:
        """Decorator factory that registers a function with execution config."""

        def decorator(func: Callable) -> Callable:
            config = TaskConfig(
                retries=retries,
                delay=delay,
                backoff=backoff,
                persist=persist,
                name=name or func.__name__,
            )
            self.registry.register(func, config)
            return func

        return decorator

    # ------------------------------------------------------------------
    # FastAPI dependency
    # ------------------------------------------------------------------

    def get_tasks(self, background_tasks: BackgroundTasks) -> "ManagedBackgroundTasks":
        """
        FastAPI dependency that injects a :class:`ManagedBackgroundTasks` instance.

        FastAPI injects the native ``BackgroundTasks`` for the current request
        automatically; the wrapper shares its task list so Starlette executes
        the wrapped tasks after the response is sent.

        Use with ``Depends``::

            @app.post("/signup")
            def signup(email: str, tasks=Depends(task_manager.get_tasks)):
                task_id = tasks.add_task(send_email, email)
                return {"task_id": task_id}
        """
        from .wrapper import ManagedBackgroundTasks

        return ManagedBackgroundTasks(self, background_tasks)

    @property
    def background_tasks(
        self,
    ) -> Callable[["BackgroundTasks"], "ManagedBackgroundTasks"]:
        """
        Alias for :meth:`get_tasks` that enables the natural-feeling pattern::

            @app.post("/signup")
            def signup(
                email: str,
                background_tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
            ):
                task_id = background_tasks.add_task(send_email, email)
                return {"task_id": task_id}
        """
        return self.get_tasks

    def install(self, app: "FastAPI") -> None:
        """
        Transparently intercept FastAPI's ``BackgroundTasks`` injection so that
        routes can keep their existing signature unchanged::

            # No migration required after calling task_manager.install(app)
            @app.post("/signup")
            def signup(email: str, background_tasks: BackgroundTasks):
                background_tasks.add_task(send_email, email)
                # background_tasks is now a ManagedBackgroundTasks instance

        Works by patching ``fastapi.dependencies.utils.BackgroundTasks`` — the
        exact reference FastAPI calls when it creates the injected instance for
        every request.  The patch is process-wide (not scoped to ``app``), so
        call this at most once per process.  If you have multiple apps and only
        want managed injection on one of them, use ``Depends(task_manager.get_tasks)``
        instead.

        Call once at app startup, before routes are defined::

            task_manager.install(app)

        After this call, all three patterns work and return a
        :class:`ManagedBackgroundTasks` instance:

        1. ``background_tasks: BackgroundTasks``  (zero migration)
        2. ``background_tasks: ManagedBackgroundTasks``  (explicit type)
        3. ``tasks = Depends(task_manager.get_tasks)``  (explicit dep)
        """
        import fastapi.dependencies.utils as _fdu
        from .wrapper import ManagedBackgroundTasks

        _tm = self

        class _BoundManaged(ManagedBackgroundTasks):
            """Zero-arg subclass so FastAPI can call BackgroundTasks() at line 721."""

            def __init__(self) -> None:
                super().__init__(_tm)

        self._installed_on = app
        _fdu.BackgroundTasks = _BoundManaged  # type: ignore[misc, assignment]
