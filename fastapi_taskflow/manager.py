from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import BackgroundTasks

from .loggers.chain import LoggerChain
from .loggers.file import FileLogger
from .models import TaskConfig
from .registry import TaskRegistry
from .store import TaskStore

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .backends.base import SnapshotBackend
    from .loggers.base import TaskObserver
    from .loggers.chain import LoggerChain
    from .models import TaskRecord
    from .periodic import PeriodicScheduler
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
        loggers: Optional[list["TaskObserver"]] = None,
        # Convenience shorthands that create a FileLogger internally.
        # Pass loggers=[FileLogger(...)] directly for more control.
        log_file: Optional[str] = None,
        log_file_max_bytes: int = 10 * 1024 * 1024,
        log_file_backup_count: int = 5,
        log_file_mode: str = "rotate",
        log_lifecycle: bool = False,
        encrypt_args_key: Optional[bytes | str] = None,
        max_concurrent_tasks: Optional[int] = None,
        max_sync_threads: Optional[int] = None,
        retention_days: Optional[float] = None,
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
            loggers: List of :class:`~fastapi_taskflow.loggers.TaskObserver`
                instances to receive structured task events. All observers run
                independently -- an error in one never affects the others or the
                task itself. Pass multiple for fan-out to different destinations::

                    TaskManager(loggers=[
                        FileLogger("tasks.log"),
                        LogfireLogger(),
                    ])

            log_file: Shorthand for adding a single
                :class:`~fastapi_taskflow.loggers.FileLogger`. Equivalent to
                ``loggers=[FileLogger(log_file)]``. Ignored when *loggers* already
                contains a ``FileLogger`` for the same path.
            log_file_max_bytes: Maximum file size before rotation. Default is 10 MB.
                Ignored when *log_file_mode* is ``"watched"``.
            log_file_backup_count: Number of rotated log files to keep. Default is 5.
                Ignored when *log_file_mode* is ``"watched"``.
            log_file_mode: ``"rotate"`` (default) uses
                :class:`~logging.handlers.RotatingFileHandler`, safe for a single
                process. ``"watched"`` uses
                :class:`~logging.handlers.WatchedFileHandler` for multi-process
                deployments where an external tool (e.g. logrotate) handles rotation.
            log_lifecycle: When ``True``, task lifecycle transitions are also
                written when using the *log_file* shorthand.
            encrypt_args_key: A ``cryptography.fernet.Fernet`` key used to
                encrypt task args and kwargs at rest. When set, args and kwargs
                are never stored in plain text -- they are encrypted at enqueue
                time and decrypted only when the executor is about to call the
                function. Accepts a URL-safe base64 string or raw bytes as
                returned by ``Fernet.generate_key()``. Requires the
                ``cryptography`` package::

                    pip install "fastapi-taskflow[encryption]"

                Generate a key once and store it in an environment variable or
                secrets manager::

                    from cryptography.fernet import Fernet
                    key = Fernet.generate_key().decode()  # store this securely

                    task_manager = TaskManager(encrypt_args_key=key)

            max_concurrent_tasks: Maximum number of async tasks that may run
                concurrently on the event loop. When set, an
                ``asyncio.Semaphore`` is acquired before each task execution
                and released on completion. Tasks that exceed this limit wait
                for a slot without blocking the event loop or delaying request
                handlers. Defaults to ``None`` (no limit, existing behaviour).

                Tune this based on your workload. IO-bound tasks (network calls,
                email, webhooks) tolerate higher values (10-20). Reduce it if
                you observe elevated request latency under task burst load::

                    TaskManager(max_concurrent_tasks=10)

            max_sync_threads: Maximum number of threads in the dedicated thread
                pool used to run sync task functions. When set, sync tasks are
                offloaded to this isolated pool instead of the default
                ``asyncio`` thread pool, preventing a burst of sync tasks from
                exhausting threads needed by sync request handlers. Defaults to
                ``None`` (uses ``asyncio.to_thread``, existing behaviour).

                Set this to roughly ``os.cpu_count() + 4`` for IO-bound sync
                tasks, or closer to ``os.cpu_count()`` for CPU-bound ones::

                    TaskManager(max_sync_threads=8)

            retention_days: Automatically delete terminal task records (success,
                failed, cancelled) older than this many days. Pruning runs
                approximately every 6 hours during the snapshot loop. Defaults
                to ``None`` (no automatic pruning). Can also be set via
                ``TaskAdmin(retention_days=...)``.
        """
        self.registry = TaskRegistry()
        self.store = TaskStore()

        # Concurrency controls — both default to None (opt-in, old behaviour
        # preserved when not set).
        self._task_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent_tasks)
            if max_concurrent_tasks is not None
            else None
        )

        self._sync_executor: concurrent.futures.ThreadPoolExecutor | None = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=max_sync_threads,
                thread_name_prefix="taskflow-sync",
            )
            if max_sync_threads is not None
            else None
        )

        self.fernet: Any = None
        if encrypt_args_key is not None:
            try:
                from cryptography.fernet import Fernet
            except ImportError as exc:
                raise ImportError(
                    "Task argument encryption requires the 'cryptography' package. "
                    "Install it with: pip install 'fastapi-taskflow[encryption]'"
                ) from exc
            key = (
                encrypt_args_key
                if isinstance(encrypt_args_key, bytes)
                else encrypt_args_key.encode()
            )
            self.fernet = Fernet(key)

        all_loggers: list["TaskObserver"] = list(loggers or [])
        if log_file is not None:
            all_loggers.append(
                FileLogger(
                    log_file,
                    max_bytes=log_file_max_bytes,
                    backup_count=log_file_backup_count,
                    mode=log_file_mode,  # type: ignore[arg-type]
                    log_lifecycle=log_lifecycle,
                )
            )

        self.logger: Optional["LoggerChain"] = None
        if all_loggers:
            self.logger = LoggerChain(all_loggers)

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

        self._audit_log: collections.deque = collections.deque(maxlen=1000)
        self._running_tasks: dict = {}

        self._app: Optional["FastAPI"] = None
        self._scheduler = None
        self._periodic_scheduler: Optional["PeriodicScheduler"] = None
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
                retention_days=retention_days,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init_app(self, app: "FastAPI") -> None:
        """Register startup and shutdown hooks on *app*.

        Calling this is equivalent to what :class:`~fastapi_taskflow.TaskAdmin`
        does internally. Use it when you want lifecycle management without
        mounting the dashboard or any other routes::

            task_manager = TaskManager(snapshot_db="tasks.db")
            app = FastAPI()
            task_manager.init_app(app)

        Calling this more than once on the same app is safe — the hooks are
        only registered the first time.

        :class:`~fastapi_taskflow.TaskAdmin` calls this automatically, so you
        do not need to call it yourself when using ``TaskAdmin``.

        Args:
            app: The FastAPI application to attach lifecycle hooks to.
        """
        if self._app is app:
            return
        self._app = app
        app.router.on_startup.append(self.startup)
        app.router.on_shutdown.append(self.shutdown)

    async def startup(self) -> None:
        """Run all startup tasks for this manager.

        Call order:

        1. Restore persisted task history from the backend into the in-memory
           store (if a backend is configured).
        2. Re-dispatch tasks that were pending at the previous shutdown (if
           ``requeue_pending=True``).
        3. Start the periodic background flush loop.
        4. Call ``startup()`` on all configured observers (loggers).

        :class:`~fastapi_taskflow.TaskAdmin` calls this automatically on app
        startup. When not using ``TaskAdmin``, call this yourself in a lifespan
        handler::

            @asynccontextmanager
            async def lifespan(app):
                await task_manager.startup()
                yield
                await task_manager.shutdown()
        """
        if self._scheduler is not None:
            await self._scheduler.load()
            if self._scheduler._requeue_pending:
                await self._scheduler.requeue()
            self._scheduler.start()
        if self._periodic_scheduler is not None:
            self._periodic_scheduler.start()
        if self.logger is not None:
            await self.logger.startup()

    async def shutdown(self) -> None:
        """Run all shutdown tasks for this manager.

        Call order:

        1. Stop the periodic background flush loop.
        2. Flush all completed tasks to the backend.
        3. If ``requeue_pending=True``, save unfinished tasks so they can be
           re-dispatched on the next startup.
        4. Call ``close()`` on all configured observers (loggers).
        5. Shut down the dedicated sync task thread pool (if
           ``max_sync_threads`` was set), waiting for in-flight tasks to finish.

        :class:`~fastapi_taskflow.TaskAdmin` calls this automatically on app
        shutdown. When not using ``TaskAdmin``, call this yourself in a
        lifespan handler::

            @asynccontextmanager
            async def lifespan(app):
                await task_manager.startup()
                yield
                await task_manager.shutdown()
        """
        if self._periodic_scheduler is not None:
            self._periodic_scheduler.stop()
        if self._scheduler is not None:
            self._scheduler.stop()
            await self._scheduler.flush()
            if self._scheduler._requeue_pending:
                await self._scheduler.flush_pending()
        if self.logger is not None:
            await self.logger.close()
        if self._sync_executor is not None:
            self._sync_executor.shutdown(wait=True)

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

    def schedule(
        self,
        *,
        every: Optional[float] = None,
        cron: Optional[str] = None,
        retries: int = 0,
        delay: float = 0.0,
        backoff: float = 1.0,
        name: Optional[str] = None,
        run_on_startup: bool = False,
        timezone: str = "UTC",
    ) -> Callable:
        """Register a function as a periodic background task.

        The function is also registered in the task registry (as if
        decorated with ``@task_manager.task()``), so it can be enqueued
        manually via ``tasks.add_task()`` in addition to running on schedule.

        Exactly one of *every* or *cron* must be provided.

        Args:
            every: Interval in seconds between runs. A value of ``300``
                fires the task every 5 minutes. Mutually exclusive with
                *cron*.
            cron: Five-field cron expression (e.g. ``"0 * * * *"`` for
                every hour). Requires ``pip install 'fastapi-taskflow[scheduler]'``.
                Mutually exclusive with *every*.
            retries: Number of additional attempts after the first failure.
            delay: Seconds to wait before the first retry.
            backoff: Multiplier applied to *delay* on each retry.
            name: Override the display name in logs and the dashboard.
            run_on_startup: When ``True``, fire the task on the first
                scheduler tick (immediately after startup) rather than
                waiting for the first interval or cron slot.
            timezone: IANA timezone name used when evaluating *cron*
                expressions (e.g. ``"America/New_York"``). Ignored when
                *every* is used. Defaults to ``"UTC"``.

        Example::

            @task_manager.schedule(every=300, retries=1)
            async def health_check() -> None:
                ...

            @task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
            async def morning_report() -> None:
                ...

        Raises:
            ValueError: If neither or both of *every* and *cron* are provided.
            ImportError: If *cron* is used and ``croniter`` is not installed.
        """
        if (every is None) == (cron is None):
            raise ValueError(
                "Provide exactly one of 'every' (seconds) or 'cron' (expression)."
            )

        def decorator(func: Callable) -> Callable:
            config = TaskConfig(
                retries=retries,
                delay=delay,
                backoff=backoff,
                name=name or func.__name__,
            )
            self.registry.register(func, config)

            from .periodic import PeriodicScheduler, ScheduledEntry

            entry = ScheduledEntry(
                func=func,
                config=config,
                every=every,
                cron=cron,
                run_on_startup=run_on_startup,
                timezone=timezone,
            )

            backend = self._scheduler._backend if self._scheduler is not None else None

            if self._periodic_scheduler is None:
                self._periodic_scheduler = PeriodicScheduler(self, [], backend=backend)
                self._periodic_scheduler._entries.append(entry)
            else:
                # Scheduler already started — use _add_entry to push into
                # the live heap and wake the loop immediately.
                if self._periodic_scheduler._bg_task is not None:
                    self._periodic_scheduler._add_entry(entry)
                else:
                    self._periodic_scheduler._entries.append(entry)

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
                # Re-check inside the lock -- a concurrent caller may have
                # already refreshed the cache while we were waiting.
                if time.monotonic() - self._backend_cache_ts > self._merged_list_ttl:
                    self._backend_cache = await self._scheduler._backend.load()
                    self._backend_cache_ts = time.monotonic()

        merged: dict[str, "TaskRecord"] = {r.task_id: r for r in self._backend_cache}
        # In-memory always wins -- live status is more current than the snapshot.
        merged.update(live)
        return list(merged.values())

    def _invalidate_backend_cache(self) -> None:
        """Force the next merged_list() call to reload from the backend."""
        self._backend_cache = []
        self._backend_cache_ts = 0.0

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
