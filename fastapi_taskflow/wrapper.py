import uuid
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from .executor import make_background_func
from .manager import TaskManager
from .models import TaskConfig


class ManagedBackgroundTasks(BackgroundTasks):
    """
    A ``BackgroundTasks`` subclass that intercepts ``add_task`` to inject
    retries, status tracking, and task IDs.

    Because it IS a ``BackgroundTasks``, ``isinstance`` checks pass and
    type-checkers understand it as a drop-in replacement.

    When constructed via ``Depends(task_manager.get_tasks)`` or
    ``Depends(task_manager.background_tasks)`` it receives the native
    ``BackgroundTasks`` instance that FastAPI manages for the request, and
    shares its task list — so Starlette executes the wrapped tasks after the
    response is sent exactly as normal.

    Recommended usage::

        @app.post("/signup")
        def signup(
            email: str,
            background_tasks: ManagedBackgroundTasks = Depends(task_manager.background_tasks),
        ):
            task_id = background_tasks.add_task(send_email, email)
            return {"task_id": task_id}

    Or with the original alias::

        @app.post("/signup")
        def signup(email: str, tasks=Depends(task_manager.get_tasks)):
            task_id = tasks.add_task(send_email, email)
            return {"task_id": task_id}
    """

    def __init__(
        self,
        task_manager: TaskManager,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> None:
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
        **kwargs: Any,
    ) -> str:
        """
        Enqueue *func* as a managed background task.

        Args:
            func: The task function to run in the background.
            *args: Positional arguments forwarded to *func*.
            idempotency_key: Optional caller-provided key.  If a non-failed
                task with the same key already exists in the in-process store,
                its ``task_id`` is returned immediately and *func* is not
                enqueued again.  When a shared backend is configured, the key
                is also checked and recorded there so cross-instance dedup
                works for the same logical operation.
            **kwargs: Keyword arguments forwarded to *func*.

        Returns:
            The ``task_id`` for the (possibly existing) task.
        """
        # In-process dedup: check the in-memory store first (fast, no I/O).
        if idempotency_key is not None:
            existing = self._task_manager.store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing.task_id

        task_id = str(uuid.uuid4())
        config = self._task_manager.registry.get_config(func) or TaskConfig()

        self._task_manager.store.create(
            task_id, func.__name__, args, kwargs, idempotency_key=idempotency_key
        )

        # Wire up backend and on_success callback when a scheduler is present.
        scheduler = self._task_manager._scheduler
        backend = scheduler._backend if scheduler is not None else None
        on_success = scheduler.flush_one if scheduler is not None else None

        wrapped = make_background_func(
            func,
            task_id,
            config,
            self._task_manager.store,
            args,
            kwargs,
            backend=backend,
            on_success=on_success,
        )

        super().add_task(wrapped)  # appends to self.tasks (shared with native if set)
        return task_id
