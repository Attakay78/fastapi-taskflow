"""
File-based task log writer.

Writes entries emitted via :func:`task_log` to a plain text file so that
standard tools (``tail -f``, ``grep``, log shippers) can consume them.

Usage::

    task_manager = TaskManager(
        snapshot_db="tasks.db",
        log_file="tasks.log",
    )

Each line written has the form::

    [task_id_short] [func_name] 2024-01-01T12:00:00 message text

Thread safety
-------------
A single :class:`TaskFileLogger` instance is safe for concurrent use across
multiple threads (sync tasks run in a thread pool) and the asyncio event loop
within **one process**.

Multi-process deployments
--------------------------
Python's :class:`~logging.handlers.RotatingFileHandler` (the default) is
**not** safe when multiple OS processes write to the same file, because the
rotation step is not atomic across processes.  For same-host multi-instance
deployments (e.g. multiple uvicorn workers sharing a SQLite backend), choose
one of the following strategies:

* **Separate files per instance** (recommended): give each instance a
  distinct path, e.g. ``log_file="tasks-1.log"``.  Each file is rotated
  independently and can be tailed or shipped separately.

* **External rotation** (``log_file_mode="watched"``): keep a single shared
  log file and let an external tool such as ``logrotate`` rotate it.
  :class:`~logging.handlers.WatchedFileHandler` detects when the file is
  replaced and reopens it automatically.  This is safe for multi-process use
  because no Python process performs the rotation itself.

For multi-host deployments (Redis backend), each host writes its own log file.
Use a log shipper (Loki, Datadog, Fluentd, CloudWatch) to aggregate them.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler, WatchedFileHandler
from typing import Literal


class TaskFileLogger:
    """
    Appends task log entries to a plain text file.

    Args:
        path: File path to write to (created if absent).
        max_bytes: Maximum file size before rotation (default 10 MB).
            Ignored when *mode* is ``"watched"``.
        backup_count: Number of rotated files to keep (default 5).
            Ignored when *mode* is ``"watched"``.
        mode: ``"rotate"`` (default) uses
            :class:`~logging.handlers.RotatingFileHandler` — thread-safe
            within a single process.  ``"watched"`` uses
            :class:`~logging.handlers.WatchedFileHandler` — safe for
            multi-process same-host deployments with external rotation.
        log_lifecycle: When ``True``, also write a line for task lifecycle
            transitions (RUNNING, SUCCESS, FAILED, INTERRUPTED).
    """

    def __init__(
        self,
        path: str,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        mode: Literal["rotate", "watched"] = "rotate",
        log_lifecycle: bool = False,
    ) -> None:
        self._path = path
        self._log_lifecycle = log_lifecycle

        # Use a unique logger name per file path so multiple TaskFileLogger
        # instances targeting different files do not share handlers.
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, task_id: str, func_name: str, message: str) -> None:
        """Write one log entry line to the file."""
        self._logger.info("[%s] [%s] %s", task_id[:8], func_name, message)

    def lifecycle(self, task_id: str, func_name: str, event: str) -> None:
        """
        Write a lifecycle event line if *log_lifecycle* is enabled.

        Called by the executor on status transitions.  *event* is one of
        ``"RUNNING"``, ``"SUCCESS"``, ``"FAILED"``, or ``"INTERRUPTED"``.
        """
        if self._log_lifecycle:
            from datetime import datetime, timezone

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            self._logger.info("[%s] [%s] %s -- %s", task_id[:8], func_name, ts, event)

    def close(self) -> None:
        """Flush and close all file handlers (called on app shutdown)."""
        for handler in list(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)
