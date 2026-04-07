from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class TaskConfig:
    retries: int = 0
    delay: float = 0.0
    backoff: float = 1.0  # multiplier applied to delay on each retry
    persist: bool = False
    name: str | None = None


@dataclass
class TaskRecord:
    task_id: str
    func_name: str
    status: TaskStatus
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    start_time: datetime | None = None
    end_time: datetime | None = None
    retries_used: int = 0
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    stacktrace: str | None = None

    @property
    def duration(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "func_name": self.func_name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": self.duration,
            "retries_used": self.retries_used,
            "error": self.error,
            "logs": list(self.logs),
            "stacktrace": self.stacktrace,
        }
