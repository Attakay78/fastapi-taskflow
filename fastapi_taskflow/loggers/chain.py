"""Logger chain that fans out to multiple TaskObserver instances."""

from __future__ import annotations

import logging

from .base import LifecycleEvent, LogEvent, TaskObserver

_log = logging.getLogger(__name__)


class LoggerChain(TaskObserver):
    """Fan-out logger that dispatches events to a list of :class:`TaskObserver` instances.

    Each observer is called in order. Errors raised by any one observer are
    caught, logged to stderr, and never propagate -- the remaining observers
    still receive the event and the task is never affected.

    ``min_level`` on the chain itself is set to ``"debug"`` so it never filters
    events before dispatching. Each observer applies its own ``min_level``.

    Args:
        loggers: Ordered list of observers to dispatch to.

    Example::

        chain = LoggerChain([
            FileLogger("tasks.log"),
            LogfireLogger(min_level="warning"),
        ])
    """

    def __init__(self, loggers: list[TaskObserver]) -> None:
        super().__init__(min_level="debug")
        self._loggers = loggers

    async def on_log(self, event: LogEvent) -> None:
        """Dispatch *event* to every observer, isolating errors per observer.

        Args:
            event: The log entry to dispatch.
        """
        for obs in self._loggers:
            try:
                await obs.on_log(event)
            except Exception:
                _log.exception(
                    "fastapi-taskflow: logger %s raised in on_log",
                    type(obs).__name__,
                )

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        """Dispatch *event* to every observer, isolating errors per observer.

        Args:
            event: The lifecycle event to dispatch.
        """
        for obs in self._loggers:
            try:
                await obs.on_lifecycle(event)
            except Exception:
                _log.exception(
                    "fastapi-taskflow: logger %s raised in on_lifecycle",
                    type(obs).__name__,
                )

    async def startup(self) -> None:
        """Call ``startup()`` on every observer, isolating errors per observer."""
        for obs in self._loggers:
            try:
                await obs.startup()
            except Exception:
                _log.exception(
                    "fastapi-taskflow: logger %s raised in startup",
                    type(obs).__name__,
                )

    async def close(self) -> None:
        """Call ``close()`` on every observer, isolating errors per observer."""
        for obs in self._loggers:
            try:
                await obs.close()
            except Exception:
                _log.exception(
                    "fastapi-taskflow: logger %s raised in close",
                    type(obs).__name__,
                )
