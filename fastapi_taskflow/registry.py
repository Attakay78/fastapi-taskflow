from typing import Callable

from .models import TaskConfig


class TaskRegistry:
    """Maps decorated functions to their :class:`~fastapi_taskflow.models.TaskConfig`.

    Populated at import time as each ``@task_manager.task()`` decorator runs.
    The executor and snapshot scheduler look up configs and functions here.

    Two lookup strategies are kept in sync:

    * By ``id(func)`` -- used at enqueue time when the caller already has the
      callable object. Fast and unambiguous.
    * By name string -- used when restoring tasks from a snapshot, where only
      the function name was persisted.
    """

    def __init__(self) -> None:
        self._by_id: dict[int, TaskConfig] = {}
        self._by_name: dict[str, Callable] = {}

    def register(self, func: Callable, config: TaskConfig) -> None:
        """Register *func* with the given *config*.

        Called automatically by the ``@task_manager.task()`` decorator.
        """
        self._by_id[id(func)] = config
        self._by_name[config.name or func.__name__] = func

    def get_config(self, func: Callable) -> TaskConfig | None:
        """Return the config for *func*, or ``None`` if it is not registered."""
        return self._by_id.get(id(func))

    def get_by_name(self, name: str) -> tuple[Callable, TaskConfig] | None:
        """Return ``(func, config)`` for a registered function name, or ``None``.

        Used by the snapshot scheduler to re-dispatch tasks loaded from
        a backend where only the function name was stored.
        """
        func = self._by_name.get(name)
        if func is None:
            return None
        config = self._by_id.get(id(func))
        if config is None:
            return None
        return func, config

    def is_registered(self, func: Callable) -> bool:
        """Return ``True`` if *func* has been decorated with ``@task_manager.task()``."""
        return id(func) in self._by_id
