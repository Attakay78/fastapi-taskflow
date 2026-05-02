"""ProcessExecutor: runs task functions in a separate OS process.

Use this executor for CPU-bound work that would block the event loop if run as
a coroutine or in a thread -- PDF rendering, image resizing, data compression,
numerical computation, and similar tasks.

The pool is backed by :class:`concurrent.futures.ProcessPoolExecutor` with the
``spawn`` start method, which gives each worker a clean interpreter state
without copying open database connections, asyncio loop state, or file
descriptors from the parent. Worker startup is slightly slower than ``fork``
but the cost is amortized over the worker's lifetime, and behaviour is
consistent across Linux, macOS, and Windows.

Constraints
-----------
The following constraints apply to ``executor='process'`` tasks:

* **Function serialization.** When cloudpickle is installed
  (``pip install "fastapi-taskflow[process]"``), the function itself is
  serialized and sent to the worker, so closures, lambdas, and
  locally-defined functions are all supported. Without cloudpickle, the
  function is sent by qualified name and must be defined at module level.

* **Picklable arguments.** All positional and keyword arguments must survive
  a serialization round-trip. cloudpickle extends what is accepted (lambdas,
  closures, locally-defined classes). OS-level resources (file handles,
  sockets, database connections) cannot cross a process boundary with any
  serializer. Non-serializable arguments raise :exc:`TaskArgumentError` at
  ``add_task()`` time before the task is stored, converting a cryptic worker
  crash into a clear API error.

* **Picklable return values.** The function's return value is transferred
  back to the parent via pickle. Functions returning non-picklable objects
  will fail with a serialization error on completion.

* **Argument encryption boundary.** When ``encrypt_args_key`` is set on
  :class:`~fastapi_taskflow.TaskManager`, args are decrypted in the parent
  before being pickled and sent to the worker. The IPC transport is a local
  pipe; there is no network exposure.

* **Eager dispatch bypass.** When ``eager=True`` is also set, process dispatch
  is bypassed and the task runs in-process with a logged warning. Eager mode
  is incompatible with the process pool, which is not started until needed.

* **No real-time log streaming.** Log records emitted by
  :func:`~fastapi_taskflow.task_logging.task_log` inside the worker are
  collected and delivered to the parent after the function returns. Dashboard
  log entries appear all at once rather than incrementally.

Shutdown
--------
:meth:`ProcessExecutor.shutdown` waits up to ``process_shutdown_timeout``
seconds for in-flight tasks, then terminates workers forcefully. Tasks that
were mid-execution at hard shutdown are marked ``INTERRUPTED`` and handled
by the existing ``requeue_on_interrupt`` mechanism.

Worker recycling
----------------
Workers are not recycled after a fixed number of tasks in v1. If memory
growth is observed, restart the parent process.

Execution flow
--------------
Here is the full path a task travels from ``add_task()`` to a result:

1. **Enqueue** — ``ManagedBackgroundTasks.add_task()`` calls
   ``ProcessExecutor.validate_args()``, which does a dry pickle round-trip on
   ``(args, kwargs)`` and raises :exc:`TaskArgumentError` immediately if they
   are not serializable. The task is then written to the ``TaskStore`` with
   status ``PENDING``.

2. **Dispatch** — ``execute_task()`` (in ``executor.py``) calls
   ``ProcessExecutor.dispatch()``. Dispatch serializes the function and
   arguments::

       func_payload = cloudpickle.dumps(func)   # or qualified name string
       args_bytes   = cloudpickle.dumps(args)
       kwargs_bytes = cloudpickle.dumps(kwargs)

   The four-tuple ``(func_payload, args_bytes, kwargs_bytes, context_dict)``
   is submitted to the ``ProcessPoolExecutor`` as a single pickle-able
   ``payload`` argument to :func:`_worker_entrypoint`.

3. **IPC** — ``pool.submit()`` pickles the ``payload`` tuple with standard
   pickle and sends it to a worker over the OS pipe managed by
   ``concurrent.futures``. The function itself is already bytes at this point,
   so the IPC layer only needs to pickle one bytes blob per field.

4. **Worker** — :func:`_worker_entrypoint` runs inside the spawned process:

   a. Deserializes the function (``cloudpickle.loads`` or qualified-name
      import), args, and kwargs.
   b. Installs a ``task_log`` sink that accumulates :class:`_LogRecord`
      objects in a local list instead of writing anywhere.
   c. Calls the function. For ``async def`` functions, a one-shot
      ``asyncio.run()`` event loop is created and torn down per invocation.
   d. On success, returns :class:`_WorkerOutcome` ``(result, log_records)``.
      On failure, raises :class:`_WorkerException` ``(exc_str, tb_str,
      log_records)`` so logs accumulated before the error reach the parent.

5. **Result** — Back in ``dispatch()``, ``asyncio.wrap_future()`` resolves:

   * **Success** — log records are replayed through ``sink()`` (which writes
     them to the ``TaskStore`` and any configured observers), then the result
     value is returned up to ``execute_task()``.
   * **_WorkerException** — logs are replayed, a ``RuntimeError`` carrying
     the worker traceback is raised so ``execute_task()`` can store the
     stacktrace on the task record and apply the retry/DLQ logic.
   * **BrokenExecutor** — a worker died (OOM, segfault). The pool reference
     is reset to ``None`` so the next ``dispatch()`` call creates a fresh
     pool, then ``RuntimeError`` is raised so the retry loop can count the
     attempt.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import inspect
import logging
import multiprocessing
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    import cloudpickle as _pickler  # type: ignore[import-untyped]

    _CLOUDPICKLE_AVAILABLE = True
except ImportError:
    import pickle as _pickler  # type: ignore[assignment]

    _CLOUDPICKLE_AVAILABLE = False

from .base import TaskExecutionContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class TaskArgumentError(ValueError):
    """Raised when task arguments cannot be used by the configured executor.

    Currently only raised by :class:`ProcessExecutor` when ``add_task()`` is
    called with arguments that cannot be serialized for transfer to the worker
    process. When ``cloudpickle`` is installed, the check uses cloudpickle
    (which handles lambdas, closures, and locally-defined classes). Without
    cloudpickle, standard :mod:`pickle` is used and the constraint is stricter.

    OS-level resources such as open file handles, database connections, and
    sockets cannot cross a process boundary regardless of the serializer. Pass
    an ID or path instead and let the worker open its own connection.

    Example::

        @task_manager.task(executor="process")
        def render(template: str) -> bytes:
            ...

        # This raises TaskArgumentError immediately, before any work is stored:
        tasks.add_task(render, open("f.html"))  # file objects are not serializable
    """


# ---------------------------------------------------------------------------
# Internal wire types
# ---------------------------------------------------------------------------


@dataclass
class _LogRecord:
    """A single log entry collected in a worker and sent back to the parent.

    All fields are basic Python types so the record survives pickle without
    any special handling.

    Attributes:
        task_id: UUID of the task that emitted this entry.
        message: The log message text.
        level: Severity string matching the values accepted by ``task_log()``.
        extra: Arbitrary structured data from the ``**extra`` kwargs.
    """

    task_id: str
    message: str
    level: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _WorkerOutcome:
    """Successful result from a worker process, including accumulated logs.

    The worker always returns a :class:`_WorkerOutcome` on success. Log
    records are bundled here rather than streamed so the parent receives them
    without needing a separate drain thread or queue.

    Attributes:
        result: The return value of the task function.
        log_records: Log entries emitted via ``task_log()`` during execution,
            in the order they were emitted.
    """

    result: Any
    log_records: list[_LogRecord] = field(default_factory=list)


class _WorkerException(Exception):
    """Worker-side exception carrier, picklable across the process boundary.

    Raised by :func:`_worker_entrypoint` when the task function raises so that
    log records accumulated before the failure reach the parent alongside the
    error information. The parent catches this, applies the logs via the sink,
    and re-raises a :exc:`RuntimeError` with the worker traceback attached.

    Attributes:
        exc_str: String representation of the original exception, including
            the type name (e.g. ``"ValueError: something went wrong"``).
        tb_str: Full formatted traceback from the worker process.
        log_records: Log entries emitted before the exception was raised.
    """

    def __init__(
        self,
        exc_str: str,
        tb_str: str,
        log_records: list[_LogRecord],
    ) -> None:
        super().__init__(exc_str)
        self.tb_str = tb_str
        self.log_records = log_records

    def __reduce__(self) -> tuple:
        return (self.__class__, (str(self.args[0]), self.tb_str, self.log_records))


# ---------------------------------------------------------------------------
# Worker-side helpers (must be module-level to be picklable)
# ---------------------------------------------------------------------------


def _qualified_name(func: Callable[..., Any]) -> str:
    """Return the fully-qualified importable name for *func*.

    Args:
        func: A module-level callable.

    Returns:
        A string of the form ``"module.path:qualname"`` suitable for
        passing to :func:`_resolve_qualified_name` in the worker.
    """
    return f"{func.__module__}:{func.__qualname__}"


def _resolve_qualified_name(name: str) -> Callable[..., Any]:
    """Import and return the callable identified by *name*.

    Args:
        name: A string in the form ``"module.path:qualname"`` as produced by
            :func:`_qualified_name`.

    Returns:
        The callable object.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the qualified name does not exist on the module.
    """
    module_path, qualname = name.split(":", 1)
    module = importlib.import_module(module_path)
    obj: Any = module
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _is_module_level(func: Callable[..., Any]) -> bool:
    """Return ``True`` when *func* appears to be a module-level callable.

    Checks that the function's ``__qualname__`` contains no dots. A dotted
    qualname indicates the function is nested inside a class or another
    function (e.g. ``OuterClass.method``, ``outer.<locals>.inner``).

    Note: this check is intentionally limited to qualname inspection so it can
    be called safely at decoration time. Python assigns the function name to
    the module namespace only after the decorator returns, so a ``getattr``
    check against ``sys.modules`` would always fail at that point. Full
    importability is verified inside :func:`_worker_entrypoint` when the
    worker resolves the function by name.

    Args:
        func: The callable to check.

    Returns:
        ``True`` if the qualname is a plain identifier (not nested, not a
        lambda); ``False`` for closures, lambdas, and functions nested inside
        other functions or classes.
    """
    # Dots indicate nesting (e.g. "Outer.method", "outer.<locals>.inner").
    # Angle brackets indicate a lambda or other synthetic name ("<lambda>").
    return "." not in func.__qualname__ and "<" not in func.__qualname__


def _worker_entrypoint(payload: tuple) -> _WorkerOutcome:
    """Run a task function inside a worker process and return the outcome.

    This function is submitted to :class:`concurrent.futures.ProcessPoolExecutor`
    by :meth:`ProcessExecutor.dispatch`. It must be a module-level function to
    be picklable by the parent.

    Sets up :func:`~fastapi_taskflow.task_logging.task_log` and
    :func:`~fastapi_taskflow.task_logging.get_task_context` so they behave
    identically to how they behave inside async and thread tasks. Log records
    are accumulated in a local list and returned with the result rather than
    dispatched in real time.

    For ``async def`` functions, a one-shot :func:`asyncio.run` event loop is
    created for the invocation and torn down on completion. This is appropriate
    for CPU-bound async code; for heavy I/O workloads that happen to be async,
    use the thread executor instead.

    Args:
        payload: A four-tuple of
            ``(func_payload, args_bytes, kwargs_bytes, context_dict)``
            as assembled by :meth:`ProcessExecutor.dispatch`.
            ``func_payload`` is either ``bytes`` (cloudpickle-serialized
            function, when cloudpickle is installed) or a ``str`` qualified
            name (``"module:qualname"``, when only stdlib pickle is available).
            ``args_bytes`` and ``kwargs_bytes`` are serialized with
            :mod:`cloudpickle` when available, otherwise with :mod:`pickle`.

    Returns:
        A :class:`_WorkerOutcome` on success.

    Raises:
        _WorkerException: When the task function raises any exception. The
            exception carries the accumulated log records so they reach the
            parent even on failure.
    """
    func_payload: bytes | str
    args_bytes: bytes
    kwargs_bytes: bytes
    context_dict: dict[str, Any]
    func_payload, args_bytes, kwargs_bytes, context_dict = payload

    func: Callable[..., Any] = (
        _pickler.loads(func_payload)  # type: ignore[arg-type]
        if isinstance(func_payload, bytes)
        else _resolve_qualified_name(func_payload)  # type: ignore[arg-type]
    )
    args: tuple = _pickler.loads(args_bytes)
    kwargs: dict = _pickler.loads(kwargs_bytes)

    log_records: list[_LogRecord] = []
    task_id: str = context_dict["task_id"]

    def _local_sink(message: str, level: str, extra: dict) -> None:
        log_records.append(
            _LogRecord(task_id=task_id, message=message, level=level, extra=extra or {})
        )

    from fastapi_taskflow.task_logging import (
        TaskContext,
        _log_sink,
        _task_context,
    )

    ctx_obj = TaskContext(
        task_id=context_dict["task_id"],
        func_name=context_dict["func_name"],
        attempt=context_dict["attempt"],
        tags=context_dict.get("tags", {}),
    )

    t_sink = _log_sink.set(_local_sink)
    t_ctx = _task_context.set(ctx_obj)
    try:
        if inspect.iscoroutinefunction(func):
            # One-shot event loop per invocation. This is appropriate for
            # CPU-bound async work; the startup/teardown cost is negligible
            # compared to the actual computation.
            result = asyncio.run(func(*args, **kwargs))
        else:
            result = func(*args, **kwargs)
        return _WorkerOutcome(result=result, log_records=log_records)
    except BaseException as exc:
        tb_str = traceback.format_exc()
        exc_str = f"{type(exc).__name__}: {exc}"
        raise _WorkerException(exc_str, tb_str, log_records) from exc
    finally:
        _log_sink.reset(t_sink)
        _task_context.reset(t_ctx)


# ---------------------------------------------------------------------------
# ProcessExecutor
# ---------------------------------------------------------------------------


class ProcessExecutor:
    """Runs task functions in a :class:`concurrent.futures.ProcessPoolExecutor`.

    Workers are spawned (not forked) so each starts with a clean interpreter
    state, avoiding hazards from copied database connections, asyncio loop
    state, and open file descriptors. The pool is created once and reused for
    all process executor tasks dispatched through this manager instance.

    Log records emitted via :func:`~fastapi_taskflow.task_logging.task_log`
    inside the worker are delivered to the parent after the function returns.
    They are then applied via the attempt-level sink so they appear on the task
    record and reach any configured :class:`~fastapi_taskflow.loggers.TaskObserver`.

    When the worker process dies unexpectedly (OOM kill, segfault), the pool
    transitions to a broken state. :meth:`dispatch` marks the event in the
    exception message, resets the pool reference so the next dispatch creates a
    fresh pool, and re-raises so the retry loop can count the attempt.

    Attributes:
        name: Executor identifier. Always ``"process"``.
    """

    name: str = "process"

    def __init__(
        self,
        max_workers: Optional[int] = None,
        shutdown_timeout: float = 30.0,
    ) -> None:
        """
        Args:
            max_workers: Maximum number of worker processes. ``None`` defaults
                to :func:`os.cpu_count` at first pool creation. Each worker is
                a full Python interpreter; size this according to available
                memory (roughly 50-100 MB resident per worker).
            shutdown_timeout: Seconds to wait for in-flight tasks during
                :meth:`shutdown`. Tasks still running after the timeout are
                terminated and their records are marked ``INTERRUPTED``.
                Corresponds to ``process_shutdown_timeout`` on
                :class:`~fastapi_taskflow.TaskManager`.
        """
        self._max_workers = max_workers
        self._shutdown_timeout = shutdown_timeout
        self._pool: Optional[ProcessPoolExecutor] = None
        self._shutdown_called = False

    def _ensure_pool(self) -> ProcessPoolExecutor:
        """Return the pool, creating it lazily on first call.

        The pool is not created at :meth:`__init__` time so users who never
        opt into ``executor='process'`` pay zero cost -- no idle worker
        processes and no pool initialisation overhead.

        Returns:
            The live :class:`concurrent.futures.ProcessPoolExecutor`.

        Raises:
            RuntimeError: If called after :meth:`shutdown`.
        """
        if self._shutdown_called:
            raise RuntimeError(
                "Cannot dispatch a process executor task after shutdown. "
                "The process pool has been closed."
            )
        if self._pool is None:
            ctx = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(
                max_workers=self._max_workers,
                mp_context=ctx,
            )
        return self._pool

    async def dispatch(
        self,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
        context: TaskExecutionContext,
        exec_ctx: Any,
        sink: Callable[[str, str, dict], None],
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        """Submit *func* to the process pool and await the result.

        Serializes the function by qualified name and all arguments via pickle,
        submits the payload to :class:`concurrent.futures.ProcessPoolExecutor`,
        and wraps the resulting :class:`concurrent.futures.Future` for async
        awaiting. Log records collected in the worker are applied to the parent
        via *sink* after the future resolves, regardless of success or failure.

        The ``exec_ctx`` parameter is accepted for protocol compatibility but
        is not used. Contextvars do not cross the process boundary; the worker
        reconstructs context from the serialized ``context_dict`` in the
        payload.

        Args:
            func: A module-level callable previously validated by
                :meth:`validate`.
            args: Positional arguments. Must be picklable.
            kwargs: Keyword arguments. Must be picklable.
            context: Per-task context serialized into the worker payload.
            exec_ctx: Ignored. Kept for protocol compatibility.
            sink: Log sink for the current attempt. Applied to log records
                returned by the worker after the future resolves.
            loop: Running event loop (unused; kept for protocol compatibility).

        Returns:
            Whatever *func* returns.

        Raises:
            RuntimeError: When the task function raised an exception inside the
                worker. The ``_worker_traceback`` attribute on the raised
                exception holds the full worker-side traceback string so the
                executor in the parent can store it on the task record for the
                dashboard error panel.
            RuntimeError: When the worker process died unexpectedly (OOM,
                segfault). The pool reference is reset so the next dispatch
                creates a fresh pool. The existing retry and requeue mechanisms
                handle re-execution.
            asyncio.CancelledError: When the dispatch was cancelled before the
                worker completed. Pending-but-unstarted futures are cancelled.
                Already-running workers continue in the background.
        """
        pool = self._ensure_pool()
        func_payload: bytes | str = (
            _pickler.dumps(func) if _CLOUDPICKLE_AVAILABLE else _qualified_name(func)
        )
        payload = (
            func_payload,
            _pickler.dumps(args),
            _pickler.dumps(kwargs),
            context.to_dict(),
        )
        future = pool.submit(_worker_entrypoint, payload)
        try:
            outcome: _WorkerOutcome = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # Cancel a queued-but-unstarted future. Already-running workers
            # cannot be interrupted mid-execution.
            future.cancel()
            raise
        except _WorkerException as exc:
            # Apply log records collected before the failure, then re-raise
            # as RuntimeError with the worker traceback attached so the outer
            # execute_task can store it on the task record.
            for rec in exc.log_records:
                try:
                    sink(rec.message, rec.level, rec.extra)
                except Exception:
                    pass
            err = RuntimeError(str(exc))
            err._worker_traceback = exc.tb_str  # type: ignore[attr-defined]
            raise err from exc
        except concurrent.futures.BrokenExecutor as exc:
            # A worker died (OOM kill, segfault, etc.). Reset the pool so the
            # next dispatch starts fresh workers.
            self._pool = None
            raise RuntimeError(
                f"Worker process for task {context.task_id!r} died unexpectedly "
                f"({type(exc).__name__}). The process pool has been reset; the "
                "next dispatch will start fresh workers."
            ) from exc

        # Success path: apply logs then return the result.
        for rec in outcome.log_records:
            try:
                sink(rec.message, rec.level, rec.extra)
            except Exception:
                pass

        return outcome.result

    async def shutdown(self, timeout: float | None = None) -> None:
        """Drain in-flight tasks and terminate the worker pool.

        Waits up to *timeout* seconds (or ``self._shutdown_timeout`` when
        *timeout* is ``None``) for any in-flight process tasks to complete,
        then shuts down the pool. Already-running workers are given this
        window to finish; pending-but-unstarted futures are cancelled
        immediately.

        Idempotent. Safe to call more than once.

        Args:
            timeout: Seconds to wait. ``None`` uses the configured
                ``process_shutdown_timeout``.
        """
        if self._shutdown_called or self._pool is None:
            self._shutdown_called = True
            return

        self._shutdown_called = True
        wait_seconds = timeout if timeout is not None else self._shutdown_timeout
        pool = self._pool
        self._pool = None

        event_loop = asyncio.get_running_loop()
        try:
            # pool.shutdown is blocking; run it in the default thread pool to
            # avoid stalling the event loop during the drain window.
            await asyncio.wait_for(
                event_loop.run_in_executor(
                    None,
                    lambda: pool.shutdown(wait=True, cancel_futures=True),
                ),
                timeout=wait_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "fastapi-taskflow: process pool did not drain within %.1fs -- "
                "forcing shutdown. In-flight process tasks will be marked INTERRUPTED.",
                wait_seconds,
            )
            # Force-terminate any workers still running after the timeout.
            await event_loop.run_in_executor(
                None,
                lambda: pool.shutdown(wait=False, cancel_futures=True),
            )
        except Exception:
            logger.exception(
                "fastapi-taskflow: error while shutting down the process pool."
            )

    def validate(self, func: Callable[..., Any]) -> None:
        """Reject functions that cannot be dispatched to the worker process.

        When cloudpickle is installed, the function itself is serialized and
        sent to the worker, so closures, lambdas, and locally-defined functions
        are all supported with no restrictions.

        Without cloudpickle, the function is sent by qualified name and the
        worker imports it. In that case, the function must be defined at module
        level (no dots in ``__qualname__``). Install
        ``fastapi-taskflow[process]`` to lift this restriction.

        Args:
            func: The decorated function.

        Raises:
            ValueError: If cloudpickle is not installed and *func* is not a
                module-level callable.
        """
        if _CLOUDPICKLE_AVAILABLE:
            return
        if not _is_module_level(func):
            raise ValueError(
                f"Task '{func.__name__}' uses executor='process' but is not a "
                "module-level function. Without cloudpickle, process tasks must "
                "be importable by qualified name. Install "
                "'fastapi-taskflow[process]' to allow closures, lambdas, and "
                "locally-defined functions."
            )

    def validate_args(self, args: tuple, kwargs: dict) -> None:
        """Reject non-serializable arguments before the task is stored.

        Performs a dry serialization check on the combined arguments using
        cloudpickle when available, otherwise standard pickle. This converts
        a cryptic worker-side failure into a clear :exc:`TaskArgumentError`
        at ``add_task()`` time, before the task is written to the store.

        cloudpickle extends what is accepted: lambdas, closures, and
        locally-defined classes are serializable. OS-level resources such as
        open file handles, sockets, and database connections are not
        serializable by any library and always fail this check.

        The cost is one serialization call per enqueue. For process executor
        tasks (CPU-bound by definition), the enqueue rate is low and this
        overhead is negligible.

        Args:
            args: Positional arguments to validate.
            kwargs: Keyword arguments to validate.

        Raises:
            TaskArgumentError: If the arguments cannot be serialized for
                transfer to the worker process.
        """
        try:
            _pickler.dumps(args)
            _pickler.dumps(kwargs)
        except Exception as exc:
            raise TaskArgumentError(
                f"Task uses executor='process' but the provided arguments are "
                f"not serializable: {exc}. OS-level resources (file handles, "
                "sockets, database connections) cannot cross a process boundary. "
                "Pass an ID or path instead."
            ) from exc


# ---------------------------------------------------------------------------
# LazyProcessExecutor
# ---------------------------------------------------------------------------


class LazyProcessExecutor:
    """Wraps :class:`ProcessExecutor` and defers pool creation until first dispatch.

    Registered in the executor registry on every :class:`~fastapi_taskflow.TaskManager`
    regardless of whether any task ever uses ``executor='process'``. Users who
    never opt in pay zero cost because :class:`ProcessExecutor._ensure_pool` is
    only called on the first ``dispatch()`` call.

    All method calls are delegated to the underlying :class:`ProcessExecutor`
    instance, which is created eagerly at :meth:`__init__` time (just the
    Python object, not the OS processes).

    Attributes:
        name: Executor identifier. Always ``"process"``.
    """

    name: str = "process"

    def __init__(
        self,
        max_workers: Optional[int] = None,
        shutdown_timeout: float = 30.0,
    ) -> None:
        """
        Args:
            max_workers: Forwarded to :class:`ProcessExecutor`. ``None``
                defaults to :func:`os.cpu_count` at pool creation time.
            shutdown_timeout: Forwarded to :class:`ProcessExecutor`.
        """
        self._inner = ProcessExecutor(
            max_workers=max_workers,
            shutdown_timeout=shutdown_timeout,
        )

    async def dispatch(
        self,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
        context: TaskExecutionContext,
        exec_ctx: Any,
        sink: Callable[[str, str, dict], None],
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        """Delegate to the inner :class:`ProcessExecutor`.

        The pool is created here on first call. See
        :meth:`ProcessExecutor.dispatch` for full documentation.
        """
        return await self._inner.dispatch(
            func, args, kwargs, context, exec_ctx, sink, loop
        )

    async def shutdown(self, timeout: float | None = None) -> None:
        """Delegate to the inner :class:`ProcessExecutor`.

        See :meth:`ProcessExecutor.shutdown` for full documentation.
        """
        await self._inner.shutdown(timeout=timeout)

    def validate(self, func: Callable[..., Any]) -> None:
        """Delegate to the inner :class:`ProcessExecutor`.

        See :meth:`ProcessExecutor.validate` for full documentation.
        """
        self._inner.validate(func)

    def validate_args(self, args: tuple, kwargs: dict) -> None:
        """Delegate to the inner :class:`ProcessExecutor`.

        See :meth:`ProcessExecutor.validate_args` for full documentation.
        """
        self._inner.validate_args(args, kwargs)

    @property
    def pool_is_alive(self) -> bool:
        """``True`` when the underlying pool has been created and not yet shut down."""
        return self._inner._pool is not None
