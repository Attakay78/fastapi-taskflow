import pytest
from fastapi_taskflow import TaskManager
from fastapi_taskflow.executor import execute_task
from fastapi_taskflow.loggers.file import FileLogger
from fastapi_taskflow.loggers.base import LifecycleEvent, LogEvent
from fastapi_taskflow.models import TaskConfig, TaskStatus
from fastapi_taskflow.store import TaskStore
from fastapi_taskflow.task_logging import task_log
from datetime import datetime, timezone


def _ts():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# FileLogger unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_creates_file(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path)
    event = LogEvent(
        task_id="abc12345-0000-0000-0000-000000000000",
        func_name="send_email",
        message="hello",
        level="info",
        timestamp=_ts(),
        attempt=0,
    )
    await fl.on_log(event)
    await fl.close()

    lines = open(path).readlines()
    assert len(lines) == 1
    assert "[abc12345]" in lines[0]
    assert "[send_email]" in lines[0]
    assert "hello" in lines[0]


@pytest.mark.asyncio
async def test_multiple_writes_appended(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path)
    await fl.on_log(
        LogEvent(
            task_id="task0001-0000-0000-0000-000000000000",
            func_name="func_a",
            message="first",
            level="info",
            timestamp=_ts(),
            attempt=0,
        )
    )
    await fl.on_log(
        LogEvent(
            task_id="task0002-0000-0000-0000-000000000000",
            func_name="func_b",
            message="second",
            level="info",
            timestamp=_ts(),
            attempt=0,
        )
    )
    await fl.close()

    lines = open(path).readlines()
    assert len(lines) == 2
    assert "func_a" in lines[0]
    assert "func_b" in lines[1]


@pytest.mark.asyncio
async def test_lifecycle_off_by_default(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path)  # log_lifecycle=False by default
    event = LifecycleEvent(
        task_id="abc12345-0000-0000-0000-000000000000",
        func_name="func",
        status=TaskStatus.RUNNING,
        timestamp=_ts(),
        attempt=0,
        retries_used=0,
    )
    await fl.on_lifecycle(event)
    await fl.close()

    content = open(path).read()
    assert content == ""


@pytest.mark.asyncio
async def test_lifecycle_on(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path, log_lifecycle=True)
    event = LifecycleEvent(
        task_id="abc12345-0000-0000-0000-000000000000",
        func_name="send_email",
        status=TaskStatus.SUCCESS,
        timestamp=_ts(),
        attempt=0,
        retries_used=0,
    )
    await fl.on_lifecycle(event)
    await fl.close()

    lines = open(path).readlines()
    assert len(lines) == 1
    assert "SUCCESS" in lines[0]
    assert "[abc12345]" in lines[0]


@pytest.mark.asyncio
async def test_task_id_truncated_to_8_chars(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path)
    await fl.on_log(
        LogEvent(
            task_id="abcdefgh-1234-5678-abcd-000000000000",
            func_name="func",
            message="msg",
            level="info",
            timestamp=_ts(),
            attempt=0,
        )
    )
    await fl.close()

    line = open(path).read()
    assert "[abcdefgh]" in line
    assert "abcdefgh-1234" not in line


# ---------------------------------------------------------------------------
# Integration: FileLogger wired through executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_log_written_to_file(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path, log_lifecycle=True)
    store = TaskStore()
    store.create("t1", "my_func", (), {})

    async def my_func():
        task_log("step one")
        task_log("step two")

    await execute_task(my_func, "t1", TaskConfig(), store, (), {}, logger=fl)
    await fl.close()

    lines = open(path).readlines()
    # 2 task_log lines + RUNNING + SUCCESS lifecycle = 4 lines
    assert len(lines) == 4
    content = "".join(lines)
    assert "step one" in content
    assert "step two" in content
    assert "RUNNING" in content
    assert "SUCCESS" in content


@pytest.mark.asyncio
async def test_retry_separator_written_to_file(tmp_path):
    # Retry separators go to the in-memory store only, not the file logger.
    # task_log() entries from retry attempts are forwarded to the file logger.
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path)
    store = TaskStore()
    store.create("t2", "flaky_func", (), {})
    attempts = []

    def flaky_func():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("not yet")
        task_log("done")

    await execute_task(
        flaky_func, "t2", TaskConfig(retries=2, delay=0), store, (), {}, logger=fl
    )
    await fl.close()

    content = open(path).read()
    assert "done" in content


@pytest.mark.asyncio
async def test_failed_task_lifecycle_written(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = FileLogger(path, log_lifecycle=True)
    store = TaskStore()
    store.create("t3", "bad_func", (), {})

    def bad_func():
        raise ValueError("boom")

    await execute_task(bad_func, "t3", TaskConfig(), store, (), {}, logger=fl)
    await fl.close()

    content = open(path).read()
    assert "FAILED" in content
    assert "RUNNING" in content


# ---------------------------------------------------------------------------
# Shutdown: INTERRUPTED written via flush_pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_lifecycle_written_on_flush_pending(tmp_path):
    from fastapi_taskflow.snapshot import SnapshotScheduler

    db = str(tmp_path / "tasks.db")
    log_path = str(tmp_path / "tasks.log")
    tm = TaskManager(snapshot_db=db, log_file=log_path, log_lifecycle=True)

    @tm.task()
    def work() -> None:
        pass

    s = SnapshotScheduler(tm, db_path=db, interval=9999, requeue_pending=True)
    tm.store.create("r1", "work", (), {})
    tm.store.update("r1", status=TaskStatus.RUNNING)

    await s.flush_pending()
    await tm.logger.close()

    content = open(log_path).read()
    assert "INTERRUPTED" in content
    assert "[r1]" in content


# ---------------------------------------------------------------------------
# Integration: TaskManager log_file param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_manager_log_file(tmp_path):
    path = str(tmp_path / "tasks.log")
    tm = TaskManager(log_file=path, log_lifecycle=True)

    @tm.task()
    async def greet(name: str):
        task_log(f"Hello {name}")

    from fastapi_taskflow.wrapper import ManagedBackgroundTasks

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(greet, "world")

    # Run all queued background tasks
    for bt in managed.tasks:
        await bt.func(*bt.args, **bt.kwargs)

    await tm.logger.close()

    content = open(path).read()
    assert "Hello world" in content
    assert "greet" in content
