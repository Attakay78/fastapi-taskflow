from typing import Callable

from .models import TaskConfig


class TaskRegistry:
    """Maps decorated functions to their TaskConfig."""

    def __init__(self) -> None:
        self._by_id: dict[int, TaskConfig] = {}
        # func_name → func — used to re-execute tasks restored from a snapshot
        self._by_name: dict[str, Callable] = {}

    def register(self, func: Callable, config: TaskConfig) -> None:
        self._by_id[id(func)] = config
        self._by_name[config.name or func.__name__] = func

    def get_config(self, func: Callable) -> TaskConfig | None:
        return self._by_id.get(id(func))

    def get_by_name(self, name: str) -> tuple[Callable, TaskConfig] | None:
        """Return ``(func, config)`` for a registered function name, or ``None``."""
        func = self._by_name.get(name)
        if func is None:
            return None
        config = self._by_id.get(id(func))
        if config is None:
            return None
        return func, config

    def is_registered(self, func: Callable) -> bool:
        return id(func) in self._by_id
