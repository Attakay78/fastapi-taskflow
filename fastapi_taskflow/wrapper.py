import contextvars
import pickle
import uuid
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from .executor import make_background_func
from .manager import TaskManager
from .models import TaskConfig


class ManagedBackgroundTasks(BackgroundTasks):
    """A ``BackgroundTasks`` subclass that adds retries, status tracking, and task IDs.

    Because it subclasses ``BackgroundTasks``, it passes ``isinstance`` checks
    and works as a drop-in replacement everywhere FastAPI expects the original type.

    When injected via ``Depends(task_manager.get_tasks)``, it receives the native
    ``BackgroundTasks`` instance for the current request and shares its task list,
    so Starlette runs the tasks after the response is sent as normal.

    Usage::

        @app.post("/signup")
        def signup(
            email: str,
            background_tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
        ):
            task_id = background_tasks.add_task(send_email, email)
            return {"task_id": task_id}
    """

    def __init__(
        self,
        task_manager: TaskManager,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> None:
        """
        Args:
            task_manager: The :class:`~fastapi_taskflow.manager.TaskManager` that
                holds the registry and store used when enqueuing tasks.
            background_tasks: The native ``BackgroundTasks`` instance created by
                FastAPI for the current request. When provided, this wrapper shares
                its task list so Starlette runs both managed and unmanaged tasks
                after the response is sent. Omit when constructing outside a request
                context (e.g. in tests or the ``install()`` patch).
        """
        super().__init__()  # initialises self.tasks = []
        self._task_manager = task_manager
        if background_tasks is not None:
            # Share the native task list so Starlette executes our tasks when
            # the response is sent.
            self.tasks = background_tasks.tasks

    def add_task(  # type: ignore[override]
        self,
        func: Callable,
        *args: Any,
        idempotency_key: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> str:
        """Enqueue *func* as a managed background task and return its ``task_id``.

        Args:
            func: The task function to run. Must be registered with
                ``@task_manager.task()`` to get retries and config applied.
                Unregistered functions are accepted and run with default settings.
            *args: Positional arguments forwarded to *func*.
            idempotency_key: Optional deduplication key. If a non-failed task
                with the same key already exists in this process, its ``task_id``
                is returned immediately and *func* is not enqueued again. When a
                shared backend is configured, the key is also checked there to
                prevent duplicate execution across multiple instances.
            tags: Key/value labels attached to this task invocation. Forwarded to
                every :class:`~fastapi_taskflow.loggers.LogEvent` and
                :class:`~fastapi_taskflow.loggers.LifecycleEvent` emitted for this
                task, allowing observers to slice metrics or logs by label.
            **kwargs: Keyword arguments forwarded to *func*.

        Returns:
            The ``task_id`` of the enqueued (or already-existing) task.
        """
        # In-process dedup: check the in-memory store first (fast, no I/O).
        if idempotency_key is not None:
            existing = self._task_manager.store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing.task_id

        task_id = str(uuid.uuid4())
        config = self._task_manager.registry.get_config(func) or TaskConfig()

        # Capture the caller's contextvars context for trace context propagation.
        # This snapshot is taken here (in the request handler) so OTel spans and
        # other trace state flow into the background execution transparently.
        captured_ctx = contextvars.copy_context()

        # Encrypt args/kwargs if an encryption key is configured.
        fernet = self._task_manager.fernet
        if fernet is not None:
            encrypted_payload = fernet.encrypt(pickle.dumps((args, kwargs)))
            store_args: tuple = ()
            store_kwargs: dict = {}
        else:
            encrypted_payload = None
            store_args = args
            store_kwargs = kwargs

        self._task_manager.store.create(
            task_id,
            func.__name__,
            store_args,
            store_kwargs,
            idempotency_key=idempotency_key,
            tags=tags,
            encrypted_payload=encrypted_payload,
        )

        scheduler = self._task_manager._scheduler
        backend = scheduler._backend if scheduler is not None else None
        on_success = scheduler.flush_one if scheduler is not None else None

        wrapped = make_background_func(
            func,
            task_id,
            config,
            self._task_manager.store,
            store_args,
            store_kwargs,
            backend=backend,
            on_success=on_success,
            logger=self._task_manager.logger,
            encryptor=fernet,
            captured_ctx=captured_ctx,
            semaphore=self._task_manager._task_semaphore,
            sync_executor=self._task_manager._sync_executor,
        )

        super().add_task(wrapped)  # appends to self.tasks (shared with native if set)
        return task_id
