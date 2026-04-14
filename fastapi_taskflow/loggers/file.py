"""File-based logger that writes task events to a rotating or watched log file."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler, WatchedFileHandler
from typing import Literal

from .base import LifecycleEvent, LogEvent, TaskObserver


class FileLogger(TaskObserver):
    """Writes task log entries and lifecycle events to a plain text file.

    Each log line has the form::

        [task_id_short] [func_name] message text

    Each lifecycle line has the form::

        [task_id_short] [func_name] 2024-01-01T12:00:00 -- RUNNING

    Thread Safety
    ~~~~~~~~~~~~~
    A single instance is safe for concurrent use across multiple threads and
    the asyncio event loop within one process.

    Multi-Process Deployments
    ~~~~~~~~~~~~~~~~~~~~~~~~~
    ``mode="rotate"`` (the default) uses :class:`~logging.handlers.RotatingFileHandler`,
    which is not safe when multiple OS processes write to the same file.
    Use ``mode="watched"`` for multi-process same-host deployments where an
    external tool such as ``logrotate`` handles rotation and the handler
    reopens the file automatically after it is replaced.

    For multi-host deployments (Redis backend), each host writes its own file.
    Use a log shipper (Loki, Datadog, Fluentd) to aggregate them.

    Args:
        path: File path to write to. Created if absent.
        max_bytes: Maximum file size before rotation. Default is 10 MB.
            Ignored when *mode* is ``"watched"``.
        backup_count: Number of rotated files to keep. Default is 5.
            Ignored when *mode* is ``"watched"``.
        mode: ``"rotate"`` uses :class:`~logging.handlers.RotatingFileHandler`.
            ``"watched"`` uses :class:`~logging.handlers.WatchedFileHandler` for
            multi-process deployments with external rotation.
        log_lifecycle: When ``True``, lifecycle transitions (RUNNING, SUCCESS,
            FAILED, INTERRUPTED) are also written to the file. Default is ``False``.
        min_level: Minimum severity for :meth:`on_log` entries. Lifecycle events
            use their natural severity regardless. Default is ``"info"``.

    Example::

        task_manager = TaskManager(loggers=[
            FileLogger("tasks.log", log_lifecycle=True),
        ])
    """

    def __init__(
        self,
        path: str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        mode: Literal["rotate", "watched"] = "rotate",
        log_lifecycle: bool = False,
        min_level: str = "info",
    ) -> None:
        super().__init__(min_level=min_level)
        self._log_lifecycle = log_lifecycle

        # Use a unique logger name per path so multiple FileLogger instances
        # targeting different files never share handlers.
        logger_name = f"fastapi_taskflow.file_logger.{path}"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False  # never pollute the root logger

        if not self._logger.handlers:
            if mode == "watched":
                handler: logging.Handler = WatchedFileHandler(path, encoding="utf-8")
            else:
                handler = RotatingFileHandler(
                    path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    async def on_log(self, event: LogEvent) -> None:
        """Write *event* to the log file if it meets the minimum level.

        Args:
            event: The log entry to write.
        """
        if not self._should_log(event.level):
            return
        self._logger.info(
            "[%s] [%s] %s",
            event.task_id[:8],
            event.func_name,
            event.message,
        )

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        """Write a lifecycle transition line if *log_lifecycle* is enabled.

        Args:
            event: The lifecycle event to write.
        """
        if not self._log_lifecycle:
            return
        if not self._should_log_lifecycle(event):
            return
        self._logger.info(
            "[%s] [%s] %s -- %s",
            event.task_id[:8],
            event.func_name,
            event.timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
            event.status.value.upper(),
        )

    async def close(self) -> None:
        """Flush and close all file handlers."""
        for handler in list(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)
