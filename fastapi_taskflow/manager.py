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
    """The central object that ties all components together.

    Holds the :class:`~fastapi_taskflow.registry.TaskRegistry` and
    :class:`~fastapi_taskflow.store.TaskStore`, exposes the ``@task_manager.task``
    decorator, and provides FastAPI dependency helpers for injecting
    :class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks` into routes.

    Usage::

        task_manager = TaskManager(snapshot_db="tasks.db")

        @task_manager.task(retries=3, delay=2.0, backoff=2.0)
        def send_email(address: str) -> None:
            ...

        app = FastAPI()
        TaskAdmin(app, task_manager)  # mounts routes and manages lifecycle

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
        log_file: Optional[str] = None,
        log_file_max_bytes: int = 10 * 1024 * 1024,
        log_file_backup_count: int = 5,
        log_file_mode: str = "rotate",
        log_lifecycle: bool = False,
    ) -> None:
        """
        Args:
            snapshot_db: Shorthand for ``snapshot_backend=SqliteBackend(snapshot_db)``.
                Creates a SQLite file at the given path with no extra dependencies.
            snapshot_backend: A :class:`~fastapi_taskflow.backends.SnapshotBackend`
                instance (e.g. :class:`~fastapi_taskflow.backends.RedisBackend`).
                Takes precedence over *snapshot_db* when both are provided.
            snapshot_interval: How often (seconds) the scheduler flushes completed
                tasks to the backend. Default is 60 seconds.
            requeue_pending: When ``True``, tasks that had not finished at shutdown
                are saved and re-dispatched on the next startup. See
                :attr:`~fastapi_taskflow.models.TaskConfig.requeue_on_interrupt` for
                per-task control over interrupted (mid-execution) tasks.
            merged_list_ttl: How long (seconds) to cache the backend read inside
                :meth:`merged_list`. Only the backend portion is cached; in-memory
                tasks are always included fresh. Default is 5 seconds.
            log_file: Path to a plain-text log file. When set, every
                :func:`~fastapi_taskflow.task_logging.task_log` entry is written
                there in addition to the in-memory record.
            log_file_max_bytes: Maximum file size before rotation. Default is 10 MB.
                Ignored when *log_file_mode* is ``"watched"``.
            log_file_backup_count: Number of rotated log files to keep. Default is 5.
                Ignored when *log_file_mode* is ``"watched"``.
            log_file_mode: ``"rotate"`` (default) uses
                :class:`~logging.handlers.RotatingFileHandler`, safe for a single
                process. ``"watched"`` uses
                :class:`~logging.handlers.WatchedFileHandler` for multi-process
                deployments where an external tool (e.g. logrotate) handles rotation.
            log_lifecycle: When ``True``, task lifecycle transitions (RUNNING, SUCCESS,
                FAILED, INTERRUPTED) are also written to the log file.
        """
        self.registry = TaskRegistry()
        self.store = TaskStore()

        self.file_logger = None
        if log_file is not None:
            from .file_logger import TaskFileLogger

            self.file_logger = TaskFileLogger(
                log_file,
                max_bytes=log_file_max_bytes,
                backup_count=log_file_backup_count,
                mode=log_file_mode,  # type: ignore[arg-type]
                log_lifecycle=log_lifecycle,
            )

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
        """Register a function as a managed background task.

        Apply this decorator to any function you want to enqueue via
        ``tasks.add_task()``. The function itself is returned unchanged, so it
        can still be called directly in tests or other contexts.

        Args:
            retries: Number of additional attempts after the first failure.
            delay: Seconds to wait before the first retry.
            backoff: Multiplier applied to *delay* on each retry (e.g. ``2.0``
                for exponential backoff).
            name: Override the display name in logs and the dashboard.
            requeue_on_interrupt: When ``True`` (and ``requeue_pending=True`` on
                this ``TaskManager``), a task interrupted at shutdown is saved as
                PENDING and re-dispatched on the next startup. Only use this for
                idempotent functions -- those that are safe to run from scratch
                even if they partially completed before the crash.

        Example::

            @task_manager.task(retries=3, delay=1.0, backoff=2.0)
            def send_email(address: str) -> None:
                ...
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
        """FastAPI dependency that injects a :class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks`.

        FastAPI automatically resolves and injects the native ``BackgroundTasks``
        for the current request. The wrapper shares that task list so Starlette
        runs the tasks after the response is sent as normal.

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
        """Alias for :meth:`get_tasks` that reads more naturally as a ``Depends`` argument::

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
        """Return all known task records, merging the in-memory store with the backend.

        Provides a unified view across multiple instances sharing the same backend:

        * Live tasks (PENDING / RUNNING) come from this instance's in-memory store
          and are always fresh.
        * Completed tasks from other instances are loaded from the backend.
        * When the same ``task_id`` appears in both, the in-memory record wins
          because it has the most current status.

        Falls back to ``store.list()`` when no backend is configured.

        The backend read is cached for ``merged_list_ttl`` seconds (default 5s) to
        avoid a full DB or Redis scan on every SSE event or dashboard refresh.
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
        """Patch FastAPI so existing ``BackgroundTasks`` routes get managed tasks automatically.

        After calling this, routes that use the standard ``BackgroundTasks`` type
        hint will receive a :class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks`
        instance instead -- no route changes required::

            task_manager.install(app)  # call once before defining routes

            @app.post("/signup")
            def signup(email: str, background_tasks: BackgroundTasks):
                background_tasks.add_task(send_email, email)
                # background_tasks is now ManagedBackgroundTasks

        Works by patching ``fastapi.dependencies.utils.BackgroundTasks``, the
        exact reference FastAPI uses when creating the injected instance per request.
        The patch is process-wide, so call this at most once. If you only want
        managed injection on specific routes, use ``Depends(task_manager.get_tasks)``
        instead.

        After this call, all three patterns return a
        :class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks` instance:

        1. ``background_tasks: BackgroundTasks``  (zero migration)
        2. ``background_tasks: ManagedBackgroundTasks``  (explicit type)
        3. ``tasks = Depends(task_manager.get_tasks)``  (explicit dep)
        """
        import fastapi.dependencies.utils as _fdu
        from .wrapper import ManagedBackgroundTasks

        _tm = self

        class _BoundManaged(ManagedBackgroundTasks):
            """Zero-arg subclass so FastAPI can call BackgroundTasks()"""

            def __init__(self) -> None:
                super().__init__(_tm)

        self._installed_on = app
        _fdu.BackgroundTasks = _BoundManaged  # type: ignore[misc, assignment]
