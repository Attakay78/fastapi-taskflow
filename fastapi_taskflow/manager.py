from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Callable, Optional

from .models import TaskConfig
from .registry import TaskRegistry
from .store import TaskStore

from fastapi import BackgroundTasks

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .backends.base import SnapshotBackend
    from .models import TaskRecord
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
        merged_list_ttl: float = 5.0,
    ) -> None:
        self.registry = TaskRegistry()
        self.store = TaskStore()

        # Cache for merged_list() backend reads.
        # The in-memory store is always merged fresh; only the backend read
        # (other instances' completed tasks) is cached to avoid a full DB/Redis
        # read on every SSE event or dashboard request.
        self._merged_list_ttl = merged_list_ttl
        self._backend_cache: "list[TaskRecord]" = []
        self._backend_cache_ts: float = 0.0
        # Lock ensures concurrent callers wait for one refresh rather than
        # all racing to call backend.load() simultaneously when the cache expires.
        self._backend_cache_lock: asyncio.Lock | None = None

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
        requeue_on_interrupt: bool = False,
    ) -> Callable:
        """Decorator factory that registers a function with execution config.

        Args:
            requeue_on_interrupt: When ``True`` and ``requeue_pending=True`` is
                set on the :class:`TaskManager`, a task that was mid-execution
                (``RUNNING``) at shutdown is saved as ``PENDING`` and re-executed
                on the next startup.  Only set this for tasks whose function is
                **idempotent** — i.e. safe to run from scratch even if it
                partially executed before the crash (e.g. a pure DB sync).
                When ``False`` (the default), interrupted tasks are saved to
                history with status ``INTERRUPTED`` so they are visible in the
                dashboard but are **not** re-executed automatically.
        """

        def decorator(func: Callable) -> Callable:
            config = TaskConfig(
                retries=retries,
                delay=delay,
                backoff=backoff,
                persist=persist,
                name=name or func.__name__,
                requeue_on_interrupt=requeue_on_interrupt,
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

    async def merged_list(self) -> "list[TaskRecord]":
        """
        Return all known task records, merging the in-memory store with
        completed records from the backend.

        This gives a unified view across multiple instances sharing the same
        backend (SQLite on the same host, or Redis):

        * Live tasks (PENDING / RUNNING) only exist in the calling instance's
          in-memory store — they are included as-is, always fresh.
        * Completed tasks (SUCCESS / FAILED / INTERRUPTED) from **other**
          instances are pulled from the backend and included.
        * If the same task_id appears in both places the in-memory record
          wins — it always has the most up-to-date status.

        Falls back to ``store.list()`` when no backend is configured.

        The backend read is cached for ``merged_list_ttl`` seconds (default 5s)
        to avoid a full DB or Redis scan on every SSE event or dashboard refresh.
        The in-memory merge always uses the latest live data regardless of cache.
        """
        live: dict[str, "TaskRecord"] = {t.task_id: t for t in self.store.list()}

        if self._scheduler is None:
            return list(live.values())

        # Lazily create the lock inside the running event loop.
        if self._backend_cache_lock is None:
            self._backend_cache_lock = asyncio.Lock()

        now = time.monotonic()
        if now - self._backend_cache_ts > self._merged_list_ttl:
            async with self._backend_cache_lock:
                # Re-check inside the lock — a concurrent caller may have
                # already refreshed the cache while we were waiting.
                if time.monotonic() - self._backend_cache_ts > self._merged_list_ttl:
                    self._backend_cache = await self._scheduler._backend.load()
                    self._backend_cache_ts = time.monotonic()

        merged: dict[str, "TaskRecord"] = {r.task_id: r for r in self._backend_cache}
        # In-memory always wins — live status is more current than the snapshot.
        merged.update(live)
        return list(merged.values())

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
