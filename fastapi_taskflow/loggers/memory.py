"""In-memory logger for collecting task events in tests."""

from __future__ import annotations

from .base import LifecycleEvent, LogEvent, TaskObserver


class InMemoryLogger(TaskObserver):
    """Stores task events in memory for use in tests.

    Accumulates all :class:`LogEvent` and :class:`LifecycleEvent` objects in
    plain lists so test assertions can inspect them after a task completes::

        logger = InMemoryLogger()
        task_manager = TaskManager(loggers=[logger])

        # ... run tasks ...

        assert logger.log_events[0].message == "Connecting to SMTP"
        assert logger.lifecycle_events[-1].status == TaskStatus.SUCCESS

    Args:
        min_level: Minimum severity to record for log entries. Defaults to
            ``"debug"`` so every entry is captured unless filtered.

    Attributes:
        log_events: All :class:`LogEvent` objects recorded since creation or
            the last :meth:`clear` call.
        lifecycle_events: All :class:`LifecycleEvent` objects recorded since
            creation or the last :meth:`clear` call.
    """

    def __init__(self, *, min_level: str = "debug") -> None:
        super().__init__(min_level=min_level)
        self.log_events: list[LogEvent] = []
        self.lifecycle_events: list[LifecycleEvent] = []

    async def on_log(self, event: LogEvent) -> None:
        """Record *event* if it meets the minimum level.

        Args:
            event: The log entry to record.
        """
        if self._should_log(event.level):
            self.log_events.append(event)

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        """Record *event* unconditionally.

        Args:
            event: The lifecycle event to record.
        """
        self.lifecycle_events.append(event)

    def clear(self) -> None:
        """Remove all recorded events."""
        self.log_events.clear()
        self.lifecycle_events.clear()
