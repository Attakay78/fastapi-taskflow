from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import concurrent.futures.thread as _cft
import inspect
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, cast

import heapq

from fastapi import BackgroundTasks

from .executors.async_executor import AsyncExecutor
from .executors.process_executor import LazyProcessExecutor
from .executors.thread_executor import ThreadExecutor
from .loggers.chain import LoggerChain
from .loggers.file import FileLogger
from .models import QueueConfig, TaskConfig
from .registry import TaskRegistry
from .store import TaskStore

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .backends.base import SnapshotBackend
    from .executors.base import Executor
    from .loggers.base import TaskObserver
    from .loggers.chain import LoggerChain
    from .models import TaskRecord
    from .instance_registry import InstanceRegistry
    from .periodic import PeriodicScheduler
    from .wrapper import ManagedBackgroundTasks


class QueueFullError(RuntimeError):
    """Raised by ``add_task()`` when a named queue has reached its ``max_size`` limit.

    Callers should catch this and return an appropriate HTTP response (typically
    429 Too Many Requests) rather than letting it propagate as a 500.

    Example::

        from fastapi_taskflow import QueueFullError

        @app.post("/export")
        def export(tasks=Depends(task_manager.background_tasks)):
            try:
                task_id = tasks.add_task(generate_report, user_id)
            except QueueFullError:
                raise HTTPException(429, "Report queue is full, try again later")
            return {"task_id": task_id}
    """


class _TaskQueue:
    """An in-process priority queue with optional concurrency and size limits.

    Each named queue owns a heap (for task ordering by priority), an optional
    semaphore (for concurrency capping), and a long-lived drainer coroutine
    that continuously pops tasks and dispatches them.

    The heap stores ``(-priority, seq, task_id, wrapped_callable)`` tuples.
    Negating priority makes Python's min-heap return the highest-priority
    task first. The sequence number breaks ties between equal priorities in
    arrival order (FIFO).

    This class is internal. Users interact with named queues through
    :class:`~fastapi_taskflow.manager.TaskManager` and
    :class:`~fastapi_taskflow.models.QueueConfig`.

    Args:
        name: Queue identifier, used in log messages and API responses.
        config: The :class:`~fastapi_taskflow.models.QueueConfig` that governs
            this queue's concurrency limit and max pending size.
    """

    def __init__(self, name: str, config: QueueConfig) -> None:
        self.name = name
        self.config = config

        # _heap stores (-priority, seq, task_id, wrapped) tuples.
        # Access must be protected by _heap_lock since add_task can be called
        # from request handlers (any thread) while the drainer runs on the
        # event loop.
        self._heap: list = []
        self._heap_lock = threading.Lock()
        self._seq: int = 0

        # Signals the drainer that at least one item is waiting.
        self._has_work: asyncio.Event | None = None

        # Semaphore created at startup (needs running event loop for older Python).
        self._sem: asyncio.Semaphore | None = None

        # The drainer asyncio.Task started by TaskManager.startup().
        self._drainer_task: asyncio.Task | None = None

        # Cumulative count of tasks rejected by QueueFullError since startup.
        self._rejected_count: int = 0

    def _setup(self) -> None:
        """Create asyncio primitives once the event loop is running.

        Called by :meth:`~fastapi_taskflow.manager.TaskManager.startup` so
        that ``asyncio.Event`` and ``asyncio.Semaphore`` are bound to the
        correct running loop.
        """
        self._has_work = asyncio.Event()
        self._sem = (
            asyncio.Semaphore(self.config.concurrency)
            if self.config.concurrency is not None
            else None
        )

    def enqueue(self, task_id: str, priority: int | None, wrapped: Any) -> None:
        """Push a task onto the heap, respecting the ``max_size`` limit.

        Args:
            task_id: The UUID of the already-created store record.
            priority: Execution priority. Higher integers run first. Pass
                ``None`` for tasks without an explicit priority (they run in
                arrival order alongside any ``priority=0`` tasks).
            wrapped: Zero-argument async callable produced by
                :func:`~fastapi_taskflow.executor.make_background_func`.

        Raises:
            QueueFullError: When ``config.max_size`` is set and the number of
                tasks currently waiting in the heap equals or exceeds that limit.
        """
        with self._heap_lock:
            if (
                self.config.max_size is not None
                and len(self._heap) >= self.config.max_size
            ):
                self._rejected_count += 1
                raise QueueFullError(
                    f"Queue '{self.name}' is full ({self.config.max_size} tasks pending). "
                    "Raise max_size or reduce enqueue rate."
                )
            self._seq += 1
            # Negate priority so the min-heap pops highest-priority first.
            heapq.heappush(self._heap, (-(priority or 0), self._seq, task_id, wrapped))

        if self._has_work is not None:
            self._has_work.set()

    @property
    def pending_count(self) -> int:
        """Number of tasks currently waiting in the heap."""
        with self._heap_lock:
            return len(self._heap)

    async def _drainer(self) -> None:
        """Continuously drain the heap and dispatch tasks as asyncio Tasks.

        Waits for the ``_has_work`` event, then pops tasks one by one. If a
        concurrency semaphore is configured, it is acquired before dispatch
        and released inside ``_run_slot`` when the task completes. This means
        the drainer may briefly block at ``await self._sem.acquire()`` when
        all concurrency slots are full, which is intentional: it creates
        back-pressure at the dispatch point without blocking the event loop
        for other coroutines.

        Cancelled cleanly by :meth:`~fastapi_taskflow.manager.TaskManager.shutdown`.
        Any tasks still in the heap at that point remain as ``PENDING`` in the
        store and are handled by the snapshot backend on next startup.
        """
        assert self._has_work is not None, "_setup() must be called before _drainer()"
        while True:
            try:
                await self._has_work.wait()
                while True:
                    with self._heap_lock:
                        if not self._heap:
                            self._has_work.clear()
                            break
                        _, _, _task_id, wrapped = heapq.heappop(self._heap)
                    if self._sem is not None:
                        await self._sem.acquire()
                    asyncio.create_task(self._run_slot(wrapped))
            except asyncio.CancelledError:
                break

    async def _run_slot(self, wrapped: Any) -> None:
        """Run a task and release the concurrency semaphore slot when done.

        Args:
            wrapped: Zero-argument async callable from
                :func:`~fastapi_taskflow.executor.make_background_func`.
        """
        try:
            await wrapped()
        finally:
            if self._sem is not None:
                self._sem.release()

    def stats(self) -> dict:
        """Return a snapshot of this queue's current state for the API and dashboard.

        Returns:
            Dict with ``name``, ``concurrency``, ``max_size``, and
            ``pending`` (number of tasks waiting in the heap).
        """
        return {
            "name": self.name,
            "concurrency": self.config.concurrency,
            "max_size": self.config.max_size,
            "pending": self.pending_count,
        }


class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are daemon threads.

    Daemon threads do not prevent process exit. When a thread task is still
    running at shutdown, the task has already been saved as PENDING (by the
    CancelledError handler in execute_task), so letting the thread die with
    the process is safe, it will be requeued on the next startup.

    Without this, Python 3.13's asyncio.run() calls shutdown_default_executor(300)
    and blocks for up to 5 minutes waiting for non-daemon threads.
    """

    def _adjust_thread_count(self) -> None:
        # _idle_semaphore was added in Python 3.12; guard for older versions.
        idle = getattr(self, "_idle_semaphore", None)
        if idle is not None and idle.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            t = threading.Thread(
                target=_cft._worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
            )
            t.daemon = True
            t.start()
            cast(set, self._threads).add(t)
            # Do NOT add to _cft._threads_queues. The atexit _python_exit()
            # handler joins every thread in that dict; joining a running daemon
            # thread blocks process exit. Daemon threads are killed automatically
            # when the process exits, so we don't need to join them.


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

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

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
        max_process_workers: Optional[int] = None,
        process_shutdown_timeout: float = 30.0,
        retention_days: Optional[float] = None,
        queues: Optional[dict[str, "QueueConfig"]] = None,
        max_size: Optional[int] = None,
        retry_replaces_original: bool = True,
        instance_url: Optional[str] = None,
        instance_tasks_prefix: str = "",
        registry_ttl: int = 90,
        registry_heartbeat: int = 30,
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
                ``asyncio.Semaphore`` is acquired before each async task
                execution and released on completion. Tasks that exceed this
                limit wait for a slot without blocking the event loop or
                delaying request handlers. Defaults to ``None`` (no limit).

                Tune this based on your workload. IO-bound tasks (network
                calls, email, webhooks) tolerate higher values (10-20). Reduce
                it if you observe elevated request latency under task burst
                load::

                    TaskManager(max_concurrent_tasks=10)

            max_sync_threads: Maximum number of threads in the dedicated thread
                pool used to run sync task functions. When set, sync tasks are
                offloaded to this isolated pool instead of the default
                ``asyncio`` thread pool, preventing a burst of sync tasks from
                exhausting threads needed by sync request handlers. Defaults to
                ``None`` (uses ``asyncio.to_thread``)::

                    TaskManager(max_sync_threads=8)

            max_process_workers: Maximum number of worker processes in the
                :class:`concurrent.futures.ProcessPoolExecutor` used by
                ``executor='process'`` tasks. ``None`` (the default) uses
                :func:`os.cpu_count` at pool creation time. The pool is
                created lazily on the first dispatch of a process executor
                task; users who never opt in pay zero cost.

                Each worker is a full Python interpreter. Size this according
                to available memory (roughly 50-100 MB resident per worker on
                a typical application). For pure CPU-bound work, a value of
                ``os.cpu_count()`` is appropriate. For mixed workloads, reduce
                it to leave threads and event loop capacity for other tasks::

                    TaskManager(max_process_workers=4)

            process_shutdown_timeout: Seconds to wait for in-flight process
                executor tasks during :meth:`shutdown`. Tasks still running
                after this window are terminated and their records are marked
                ``INTERRUPTED``, where the existing ``requeue_on_interrupt``
                mechanism applies. Default is 30 seconds.
            retention_days: Automatically delete terminal task records (success,
                failed, cancelled) older than this many days. Pruning runs
                approximately every 6 hours during the snapshot loop. Defaults
                to ``None`` (no automatic pruning). Can also be set via
                ``TaskAdmin(retention_days=...)``.
            queues: Named queues with individual concurrency and backpressure
                settings. When provided, all tasks (except those dispatched
                with ``eager=True``) are routed through the named queue system
                instead of the standard Starlette ``BackgroundTasks`` path. A
                ``"default"`` queue is created automatically if not included;
                its ``concurrency`` defaults to *max_concurrent_tasks* and its
                ``max_size`` defaults to *max_size*::

                    from fastapi_taskflow import QueueConfig

                    TaskManager(
                        max_sync_threads=10,
                        queues={
                            "email":   QueueConfig(concurrency=30, max_size=500),
                            "reports": QueueConfig(concurrency=4,  max_size=50),
                        },
                    )

            max_size: Global backpressure limit for the implicit ``"default"``
                queue. When the default queue has this many tasks pending,
                ``add_task()`` raises :exc:`QueueFullError`. Ignored when
                *queues* defines its own ``"default"`` entry. Setting this
                parameter activates the named queue system even if *queues*
                is not provided::

                    TaskManager(max_size=1000)

            retry_replaces_original: When ``True``, retrying a task via the
                API (single retry, bulk retry, or the timed bulk retry) removes
                the original record from the in-memory store and the backend
                after the new task is dispatched. The new task carries the same
                function, args, and kwargs as the original. The dashboard and
                history log will show only the new run.

                When ``False``, both records are kept. The original stays
                visible in its terminal state (``failed``, ``interrupted``,
                or ``rejected``) and the new task appears alongside it as a
                separate entry::

                    TaskManager(retry_replaces_original=True)

            instance_url: Public base URL of this instance, e.g.
                ``"http://10.0.0.1:8000"``. When set alongside a shared backend
                that implements ``save_metadata`` / ``load_metadata`` (SQLite,
                Redis, Postgres, MySQL), this instance registers itself in the
                backend so that the dashboard can fan out to all peers and show
                an aggregated task view. Requires a backend to be configured.
                No registration or fan-out occurs when this is ``None``::

                    TaskManager(
                        snapshot_backend=RedisBackend("redis://redis:6379"),
                        instance_url="http://10.0.0.1:8000",
                    )

            instance_tasks_prefix: URL prefix where the tasks router is mounted
                on this instance, e.g. ``"/api/tasks"``. Used by peers to build
                the fan-out URL ``{instance_url}{instance_tasks_prefix}/__peer/tasks``.
                Must match the prefix passed to ``TaskAdmin`` or the router
                ``prefix=`` argument. Defaults to ``""`` (tasks router at root).
            registry_ttl: Seconds before a peer registry entry is considered
                stale and excluded from fan-out calls. Should be at least
                ``2 * registry_heartbeat``. Defaults to ``90``.
            registry_heartbeat: Seconds between heartbeat writes that keep
                this instance's registry entry fresh. Defaults to ``30``.
        """
        self.registry = TaskRegistry()
        self.store = TaskStore()

        # Concurrency controls for the async and thread executors.
        # Both default to None (opt-in; existing behaviour preserved when not set).
        _semaphore: asyncio.Semaphore | None = (
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

        # When no user-supplied pool, use a daemon-thread pool so that a thread
        # task still running at shutdown does not block asyncio.run()'s
        # shutdown_default_executor() call (which waits up to 300 s in Python 3.13+).
        self._default_thread_pool: _DaemonThreadPoolExecutor | None = (
            _DaemonThreadPoolExecutor(thread_name_prefix="taskflow-thread")
            if max_sync_threads is None
            else None
        )

        _thread_pool = self._sync_executor or self._default_thread_pool

        # Executor registry: keyed by the string values accepted by executor= on @task.
        # LazyProcessExecutor is always registered but creates no OS processes until
        # the first process executor task is dispatched.
        self._executors: dict[str, "Executor"] = {
            "async": AsyncExecutor(semaphore=_semaphore),
            "thread": ThreadExecutor(pool=_thread_pool),
            "process": LazyProcessExecutor(
                max_workers=max_process_workers,
                shutdown_timeout=process_shutdown_timeout,
            ),
        }

        self._process_shutdown_timeout = process_shutdown_timeout
        self.retry_replaces_original = retry_replaces_original
        self._instance_url = instance_url
        self._instance_tasks_prefix = instance_tasks_prefix
        self._registry_ttl = registry_ttl
        self._registry_heartbeat = registry_heartbeat
        self._instance_registry: Optional["InstanceRegistry"] = None

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

        # Priority queue entries are (-priority, seq, task_id, wrapped_callable).
        # Negating priority makes the min-heap return the highest priority first.
        # The sequence number is a monotonically increasing tiebreaker so that
        # tasks with equal priority execute in arrival order (FIFO), and the
        # callable is never reached in a tuple comparison.
        self._priority_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._priority_seq: int = 0
        self._priority_worker_task: Optional[asyncio.Task] = None

        # Named queue system. Active when `queues` or `max_size` is provided.
        # _queue_configs holds the raw QueueConfig values; _queues holds the
        # live _TaskQueue objects created at startup (needs a running event loop).
        _use_named_queues = queues is not None or max_size is not None
        if _use_named_queues:
            _all_configs: dict[str, QueueConfig] = dict(queues or {})
            if "default" not in _all_configs:
                _all_configs["default"] = QueueConfig(
                    concurrency=max_concurrent_tasks,
                    max_size=max_size,
                )
            self._queue_configs: dict[str, QueueConfig] = _all_configs
        else:
            self._queue_configs = {}
        # Populated at startup once the event loop is running.
        self._queues: dict[str, _TaskQueue] = {}

        self._app: Optional["FastAPI"] = None
        self._shutdown_event: asyncio.Event | None = None
        self._prev_sigint: Any = None
        self._prev_sigterm: Any = None
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

        Calling this more than once on the same app is safe -- the hooks are
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
        self._shutdown_event = asyncio.Event()
        self._install_signal_handlers()
        if self._scheduler is not None:
            await self._scheduler.load()
            if self._scheduler._requeue_pending:
                await self._scheduler.requeue()
            self._scheduler.start()
        if self._periodic_scheduler is not None:
            self._periodic_scheduler.start()
        if self.logger is not None:
            await self.logger.startup()

        if self._instance_url and self._scheduler is not None:
            from .instance_registry import InstanceRegistry

            self._instance_registry = InstanceRegistry(
                backend=self._scheduler._backend,
                instance_url=self._instance_url,
                tasks_prefix=self._instance_tasks_prefix,
                ttl=self._registry_ttl,
                heartbeat_interval=self._registry_heartbeat,
            )
            await self._instance_registry.start()

        # Start named queue drainers when the queue system is active.
        if self._queue_configs:
            # Load any queue config overrides persisted by a previous
            # update_queue_config() call so live edits survive restarts.
            if self._scheduler is not None:
                import json as _json

                try:
                    raw = await self._scheduler._backend.load_metadata("queue_configs")
                    if raw:
                        overrides: dict = _json.loads(raw)
                        for qname, vals in overrides.items():
                            if qname in self._queue_configs:
                                self._queue_configs[qname].concurrency = vals.get(
                                    "concurrency"
                                )
                                self._queue_configs[qname].max_size = vals.get(
                                    "max_size"
                                )
                except Exception:
                    pass  # corrupt or missing metadata is not fatal

            for name, config in self._queue_configs.items():
                q = _TaskQueue(name, config)
                q._setup()
                q._drainer_task = asyncio.create_task(
                    q._drainer(), name=f"taskflow-queue-{name}"
                )
                self._queues[name] = q
        else:
            # Legacy mode: single priority queue worker.
            self._priority_worker_task = asyncio.create_task(
                self._run_priority_worker(), name="taskflow-priority-worker"
            )

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
        6. Drain in-flight process executor tasks up to ``process_shutdown_timeout``
           seconds, then terminate any remaining workers.

        :class:`~fastapi_taskflow.TaskAdmin` calls this automatically on app
        shutdown. When not using ``TaskAdmin``, call this yourself in a
        lifespan handler::

            @asynccontextmanager
            async def lifespan(app):
                await task_manager.startup()
                yield
                await task_manager.shutdown()
        """
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        self.store.notify_shutdown()

        # Stop named queue drainers when the queue system is active.
        if self._queues:
            for q in self._queues.values():
                if q._drainer_task is not None:
                    q._drainer_task.cancel()
                    try:
                        await q._drainer_task
                    except asyncio.CancelledError:
                        pass
                    q._drainer_task = None
            self._queues.clear()
        elif self._priority_worker_task is not None:
            # Legacy mode cleanup.
            self._priority_worker_task.cancel()
            try:
                await self._priority_worker_task
            except asyncio.CancelledError:
                pass
            self._priority_worker_task = None
        if self._periodic_scheduler is not None:
            self._periodic_scheduler.stop()
        if self._instance_registry is not None:
            await self._instance_registry.stop()
            self._instance_registry = None
        if self._scheduler is not None:
            self._scheduler.stop()
            await self._scheduler.flush()
            await self._scheduler.flush_pending()
        if self.logger is not None:
            await self.logger.close()
        if self._sync_executor is not None:
            self._sync_executor.shutdown(wait=True)
        if self._default_thread_pool is not None:
            # Daemon threads die with the process, no need to wait.
            # cancel_futures drops queued-but-not-started items; running items
            # were already marked PENDING by the CancelledError handler.
            self._default_thread_pool.shutdown(wait=False, cancel_futures=True)
        # Drain the process executor pool if it was started, waiting up to the
        # configured timeout before forcefully terminating remaining workers.
        process_executor = self._executors.get("process")
        if process_executor is not None:
            await process_executor.shutdown(timeout=self._process_shutdown_timeout)
        self._restore_signal_handlers()

    def _install_signal_handlers(self) -> None:
        """Chain our shutdown notification onto the OS signal handlers.

        Called from startup() after uvicorn has already installed its own
        SIGINT/SIGTERM handlers. We wrap them so that when Ctrl+C arrives we
        immediately push the shutdown sentinel into every SSE subscriber queue
        — before uvicorn even starts its shutdown sequence. By the time uvicorn
        reaches its "waiting for connections" loop, the SSE connections are
        already closed and it exits without waiting.

        Uses signal.signal() (not loop.add_signal_handler) because uvicorn
        also uses signal.signal(), and we need to chain, not replace.
        call_soon_threadsafe is used because signal handlers interrupt the
        event loop and asyncio Queue operations must run inside the loop.
        """
        import signal as _signal

        loop = asyncio.get_running_loop()
        store = self.store
        shutdown_event = self._shutdown_event

        self._prev_sigint = _signal.getsignal(_signal.SIGINT)
        self._prev_sigterm = _signal.getsignal(_signal.SIGTERM)

        def _handler(sig: int, frame: Any) -> None:
            try:
                loop.call_soon_threadsafe(store.notify_shutdown)
                if shutdown_event is not None:
                    loop.call_soon_threadsafe(shutdown_event.set)
            except RuntimeError:
                pass
            prev = self._prev_sigint if sig == _signal.SIGINT else self._prev_sigterm
            if callable(prev):
                prev(sig, frame)

        try:
            _signal.signal(_signal.SIGINT, _handler)
            _signal.signal(_signal.SIGTERM, _handler)
        except (OSError, ValueError):
            pass  # not the main thread, or signal not supported

    def _restore_signal_handlers(self) -> None:
        """Restore the signal handlers saved by _install_signal_handlers."""
        import signal as _signal

        try:
            if self._prev_sigint is not None:
                _signal.signal(_signal.SIGINT, self._prev_sigint)
            if self._prev_sigterm is not None:
                _signal.signal(_signal.SIGTERM, self._prev_sigterm)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Executor helpers
    # ------------------------------------------------------------------

    def _resolve_executor(self, func: Callable, config: TaskConfig) -> "Executor":
        """Return the concrete executor for *func* based on its registered config.

        When ``config.executor`` is set explicitly (``"async"``, ``"thread"``,
        or ``"process"``), that value is used directly. When it is ``None``,
        the executor is inferred from the function signature: ``"async"`` for
        ``async def`` functions and ``"thread"`` for plain ``def`` functions.

        Args:
            func: The task function.
            config: The :class:`~fastapi_taskflow.models.TaskConfig` attached
                to *func* by the ``@task`` decorator.

        Returns:
            The :class:`~fastapi_taskflow.executors.base.Executor` instance
            registered under the resolved executor name.
        """
        if config.executor is not None:
            return self._executors[config.executor]
        name = "async" if inspect.iscoroutinefunction(func) else "thread"
        return self._executors[name]

    # ------------------------------------------------------------------
    # Priority queue
    # ------------------------------------------------------------------

    def _get_queue(self, name: str) -> "_TaskQueue":
        """Return the live queue object for *name*, falling back to ``"default"``.

        Called at task dispatch time by
        :class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks`. If *name*
        does not match any configured queue, the default queue is returned and
        a warning is logged so misconfigured ``queue=`` values are visible
        without crashing the request.

        Args:
            name: The queue name specified on ``add_task()`` or via the
                ``@task_manager.task(queue=...)`` decorator.

        Returns:
            The :class:`_TaskQueue` registered under *name*, or the
            ``"default"`` queue if *name* is not found.
        """
        q = self._queues.get(name)
        if q is None:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "fastapi-taskflow: unknown queue %r, routing to 'default'.", name
            )
            q = self._queues["default"]
        return q

    def queue_stats(self) -> list[dict]:
        """Return a snapshot of every named queue's configuration and live state.

        Includes the queue name, configured limits, number of tasks currently
        waiting in the heap, and the number of running and finished tasks drawn
        from the in-memory store.

        Returns:
            List of dicts, one per queue, sorted by name. Each dict contains:
            ``name``, ``concurrency``, ``max_size``, ``pending`` (heap depth),
            ``running`` (store count), and ``finished`` (terminal store count).
        """
        result = []
        all_records = self.store.list()
        for name, q in sorted(self._queues.items()):
            queue_records = [r for r in all_records if r.queue == name]
            running = sum(1 for r in queue_records if r.status.value == "running")
            finished = sum(
                1
                for r in queue_records
                if r.status.value in ("success", "failed", "interrupted", "cancelled")
            )
            rejected = sum(1 for r in queue_records if r.status.value == "rejected")
            s = q.stats()
            s["running"] = running
            s["finished"] = finished
            s["rejected"] = rejected
            result.append(s)
        return result

    def update_queue_config(
        self, name: str, concurrency: Optional[int], max_size: Optional[int]
    ) -> dict:
        """Update the concurrency and/or max_size of a live named queue.

        Changes take effect immediately for new tasks entering the queue.
        In-flight tasks that already acquired a semaphore slot are not affected.
        If the new concurrency is larger than the old value, the semaphore is
        released enough times to grant the extra slots; if smaller, the
        difference is silently absorbed as existing slots are released naturally.

        Args:
            name: Queue name. Must match an existing configured queue.
            concurrency: New concurrency limit, or ``None`` to remove the limit.
            max_size: New maximum pending size, or ``None`` to remove the limit.

        Returns:
            The updated queue stats dict from :meth:`_TaskQueue.stats`.

        Raises:
            KeyError: If *name* is not a known queue.
        """
        q = self._queues.get(name)
        if q is None:
            raise KeyError(f"No queue named {name!r}.")

        old_concurrency = q.config.concurrency
        q.config.concurrency = concurrency
        q.config.max_size = max_size

        # Adjust the live semaphore to reflect the new concurrency value.
        if concurrency is None:
            q._sem = None
        elif old_concurrency is None or q._sem is None:
            q._sem = asyncio.Semaphore(concurrency)
        else:
            diff = concurrency - old_concurrency
            if diff > 0:
                # More slots available: release the difference so waiting
                # dispatches can proceed immediately.
                for _ in range(diff):
                    q._sem.release()
            # Shrinking: existing _sem value is fine; slots drain naturally.
            # We replace the semaphore object with a fresh one at the new value
            # so that the internal counter is correct from this point forward.
            q._sem = asyncio.Semaphore(concurrency)

        # Update the config registry and persist to the backend if available.
        if name in self._queue_configs:
            self._queue_configs[name] = q.config

        if self._scheduler is not None:
            import json as _json

            payload = {
                qname: {"concurrency": cfg.concurrency, "max_size": cfg.max_size}
                for qname, cfg in self._queue_configs.items()
            }
            try:
                asyncio.get_event_loop().create_task(
                    self._scheduler._backend.save_metadata(
                        "queue_configs", _json.dumps(payload)
                    )
                )
            except RuntimeError:
                pass  # no running loop in test context

        return q.stats()

    def enqueue_priority(self, task_id: str, priority: int, wrapped: Any) -> None:
        """Push a wrapped task onto the priority queue.

        Called by :class:`~fastapi_taskflow.wrapper.ManagedBackgroundTasks`
        when ``add_task()`` is called with an explicit priority. The worker
        coroutine drains the queue and dispatches tasks in descending priority
        order, with FIFO ordering for tasks that share the same priority level.

        Args:
            task_id: ID of the already-created store record.
            priority: Execution priority. Higher values run first.
            wrapped: Zero-argument async callable produced by
                :func:`~fastapi_taskflow.executor.make_background_func`.
        """
        self._priority_seq += 1
        # Negate priority: PriorityQueue is a min-heap, so the lowest value
        # is dequeued first. Negating makes the highest priority come out first.
        self._priority_queue.put_nowait(
            (-priority, self._priority_seq, task_id, wrapped)
        )

    async def _run_priority_worker(self) -> None:
        """Drain the priority queue and dispatch tasks as asyncio Tasks.

        Runs for the lifetime of the application. Each item pulled from the
        queue is dispatched immediately via ``asyncio.create_task``; actual
        concurrency is governed by the ``max_concurrent_tasks`` semaphore
        inside :func:`~fastapi_taskflow.executor.execute_task`. Equal-priority
        tasks are dispatched in arrival order (FIFO).

        Cancelled cleanly by :meth:`shutdown`. Any items still in the queue
        at shutdown remain as ``PENDING`` in the store and are handled by the
        existing interrupted-task and requeue mechanisms.
        """
        while True:
            try:
                *_, wrapped = await self._priority_queue.get()
                asyncio.create_task(wrapped())
                self._priority_queue.task_done()
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def task(
        self,
        *,
        executor: Optional[Literal["async", "thread", "process"]] = None,
        retries: int = 0,
        delay: float = 0.0,
        backoff: float = 1.0,
        persist: bool = False,
        name: Optional[str] = None,
        requeue_on_interrupt: bool = False,
        eager: bool = False,
        priority: Optional[int] = None,
        queue: Optional[str] = None,
    ) -> Callable:
        """Register a function as a managed background task.

        Apply this decorator to any function you want to enqueue via
        ``tasks.add_task()``. The function itself is returned unchanged, so it
        can still be called directly in tests or other contexts.

        Args:
            executor: Explicit executor selection. One of ``"async"``,
                ``"thread"``, or ``"process"``. When omitted (the default),
                the executor is chosen automatically based on the function
                signature: ``"async"`` for ``async def`` functions and
                ``"thread"`` for plain ``def`` functions.

                Use ``executor="process"`` to route the task through a
                :class:`concurrent.futures.ProcessPoolExecutor` worker, which
                is appropriate for CPU-bound work. Process tasks must be
                module-level functions with picklable arguments. See
                :mod:`fastapi_taskflow.executors.process_executor` for full
                constraints::

                    @task_manager.task(executor="process", retries=2)
                    def render_pdf(template: str, data: dict) -> bytes:
                        ...

                Mismatches are caught at decoration time, not at enqueue time:
                ``executor='async'`` on a sync function raises :exc:`ValueError`
                immediately. ``executor='thread'`` on an async function does
                the same.
            retries: Number of additional attempts after the first failure.
            delay: Seconds to wait before the first retry.
            backoff: Multiplier applied to *delay* on each retry (e.g. ``2.0``
                for exponential backoff).
            name: Override the display name in logs and the dashboard.
            persist: Activates the requeue machinery for this function without
                setting ``requeue_pending=True`` on the manager. Tasks that were
                never started at shutdown will be re-dispatched on the next
                startup. Tasks that were mid-execution are only re-dispatched if
                ``requeue_on_interrupt`` is also ``True``.
            requeue_on_interrupt: Re-dispatch this task on startup if it was
                mid-execution when the server shut down. Requires ``persist=True``
                or ``requeue_pending=True`` on the manager, otherwise the requeue
                step never runs. Only use this on functions that are safe to run
                from scratch even if they partially completed.
            eager: When ``True``, dispatch via ``asyncio.create_task``
                immediately when ``add_task()`` is called, before FastAPI sends
                the response. Per-call ``eager`` on ``add_task()`` overrides
                this value. Note: ``eager=True`` combined with
                ``executor='process'`` logs a warning and falls back to
                in-process execution, because the process pool cannot be used
                before the response is sent.
            priority: Route this function through the priority queue instead of
                Starlette's background task list. Higher values run first.
                Conventional range is 1 (lowest) to 10 (highest). Per-call
                ``priority`` on ``add_task()`` overrides this value. ``None``
                uses the standard dispatch path (existing behaviour).
            queue: Named queue to route tasks from this function into. Must
                match a key in the ``queues`` dict passed to
                :class:`~fastapi_taskflow.manager.TaskManager`. Per-call
                ``queue`` on ``add_task()`` overrides this value. ``None``
                routes to the ``"default"`` queue when the named queue system
                is active, or to the standard Starlette path otherwise.

        Example::

            @task_manager.task(retries=3, delay=1.0, backoff=2.0)
            def send_email(address: str) -> None:
                ...

        Raises:
            ValueError: If *executor* is explicitly set and the function's
                signature is incompatible (e.g. ``executor='async'`` on a
                sync function, or ``executor='process'`` on a non-module-level
                function).
        """

        def decorator(func: Callable) -> Callable:
            config = TaskConfig(
                retries=retries,
                delay=delay,
                backoff=backoff,
                persist=persist,
                name=name or func.__name__,
                requeue_on_interrupt=requeue_on_interrupt,
                eager=eager,
                priority=priority,
                executor=executor,
                queue=queue,
            )
            # Run static validation on the decorated function if an executor
            # was explicitly requested. Auto-detected executors have no
            # constraints to enforce at decoration time.
            if executor is not None:
                self._executors[executor].validate(func)
            self.registry.register(func, config)
            # persist=True on the decorator is equivalent to requeue_pending=True
            # on the TaskManager, scoped to this function. Promote the flag so
            # the scheduler's flush_pending/requeue paths activate automatically.
            if config.persist and self._scheduler is not None:
                self._scheduler._requeue_pending = True
            return func

        return decorator

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

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
        executor: Optional[Literal["async", "thread", "process"]] = None,
        queue: Optional[str] = None,
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
            executor: Force a specific executor for each firing. ``"async"``
                runs the function as a coroutine (default for async functions),
                ``"thread"`` runs it in a thread pool (default for sync
                functions), and ``"process"`` runs it in a separate OS process.
                When ``None``, the executor is auto-detected from the function
                signature. Process tasks must be module-level importable
                functions.
            queue: Named queue to route each firing into. When the named
                queue system is active (``queues=`` passed to
                :class:`TaskManager`), the task is subject to that queue's
                concurrency limit and backpressure. Defaults to ``"default"``.

        Example::

            @task_manager.schedule(every=300, retries=1)
            async def health_check() -> None:
                ...

            @task_manager.schedule(cron="0 9 * * *", timezone="America/New_York")
            async def morning_report() -> None:
                ...

            @task_manager.schedule(every=3600, executor="process")
            def rebuild_index() -> None:
                ...

            @task_manager.schedule(every=60, queue="reports")
            def sync_report() -> None:
                ...

        Raises:
            ValueError: If neither or both of *every* and *cron* are provided.
            ValueError: If *executor* is explicitly set and the function's
                signature is incompatible (e.g. ``executor='async'`` on a sync
                function, or ``executor='process'`` on a non-module-level
                function).
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
                executor=executor,
                queue=queue,
            )
            if executor is not None:
                self._executors[executor].validate(func)
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
                # Scheduler already started -- use _add_entry to push into
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

    # ------------------------------------------------------------------
    # Multi-instance
    # ------------------------------------------------------------------

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

        # Overlay peer tasks from other instances. Peer records are more current
        # than the backend snapshot but less current than this instance's live store.
        if self._instance_registry is not None:
            from .fan_out import gather_peer_records

            peer_records = await gather_peer_records(self._instance_registry)
            for r in peer_records:
                merged[r.task_id] = r

        # In-memory always wins -- live status is more current than any snapshot.
        merged.update(live)
        return list(merged.values())

    def _invalidate_backend_cache(self) -> None:
        """Force the next merged_list() call to reload from the backend."""
        self._backend_cache = []
        self._backend_cache_ts = 0.0

    # ------------------------------------------------------------------
    # Patch
    # ------------------------------------------------------------------

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
