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

    def add_task(self, func: Callable, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
        """
        Enqueue *func* as a managed background task.

        Returns the generated ``task_id`` which can be used to query task
        status via the observability endpoints.
        """
        task_id = str(uuid.uuid4())

        config = self._task_manager.registry.get_config(func) or TaskConfig()

        self._task_manager.store.create(task_id, func.__name__, args, kwargs)

        wrapped = make_background_func(
            func, task_id, config, self._task_manager.store, args, kwargs
        )

        super().add_task(wrapped)  # appends to self.tasks (shared with native if set)
        return task_id
