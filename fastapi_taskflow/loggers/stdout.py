"""Stdout logger that prints task events to standard output."""

from __future__ import annotations

from .base import LifecycleEvent, LogEvent, TaskObserver


class StdoutLogger(TaskObserver):
    """Prints task log entries and lifecycle events to stdout.

    Useful during development and in environments where stdout is captured
    by a process manager (Docker, systemd, etc.).

    Args:
        log_lifecycle: When ``True``, lifecycle transitions (RUNNING, SUCCESS,
            FAILED, INTERRUPTED) are also printed. Default is ``True``.
        min_level: Minimum severity to print. Default is ``"info"``.

    Example::

        task_manager = TaskManager(loggers=[StdoutLogger(min_level="debug")])
    """

    def __init__(
        self,
        *,
        log_lifecycle: bool = True,
        min_level: str = "info",
    ) -> None:
        super().__init__(min_level=min_level)
        self._log_lifecycle = log_lifecycle

    async def on_log(self, event: LogEvent) -> None:
        """Print *event* to stdout if it meets the minimum level.

        Args:
            event: The log entry to print.
        """
        if not self._should_log(event.level):
            return
        ts = event.timestamp.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"[{event.task_id[:8]}] [{event.func_name}] {ts} {event.message}")

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        """Print a lifecycle transition if *log_lifecycle* is enabled.

        Args:
            event: The lifecycle event to print.
        """
        if not self._log_lifecycle:
            return
        if not self._should_log_lifecycle(event):
            return
        ts = event.timestamp.strftime("%Y-%m-%dT%H:%M:%S")
        print(
            f"[{event.task_id[:8]}] [{event.func_name}] {ts} -- {event.status.value.upper()}"
        )
