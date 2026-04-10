from fastapi_taskflow import TaskManager
from fastapi_taskflow.executor import execute_task
from fastapi_taskflow.file_logger import TaskFileLogger
from fastapi_taskflow.models import TaskConfig
from fastapi_taskflow.store import TaskStore
from fastapi_taskflow.task_logging import task_log


# ---------------------------------------------------------------------------
# TaskFileLogger unit tests
# ---------------------------------------------------------------------------


def test_write_creates_file(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path)
    fl.write("abc12345", "send_email", "2024-01-01T00:00:00 hello")
    fl.close()

    lines = open(path).readlines()
    assert len(lines) == 1
    assert "[abc12345]" in lines[0]
    assert "[send_email]" in lines[0]
    assert "hello" in lines[0]


def test_multiple_writes_appended(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path)
    fl.write("task0001", "func_a", "2024-01-01T00:00:00 first")
    fl.write("task0002", "func_b", "2024-01-01T00:00:01 second")
    fl.close()

    lines = open(path).readlines()
    assert len(lines) == 2
    assert "func_a" in lines[0]
    assert "func_b" in lines[1]


def test_lifecycle_off_by_default(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path)
    fl.lifecycle("abc12345", "func", "RUNNING")
    fl.close()

    content = open(path).read()
    assert content == ""


def test_lifecycle_on(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path, log_lifecycle=True)
    fl.lifecycle("abc12345", "send_email", "SUCCESS")
    fl.close()

    lines = open(path).readlines()
    assert len(lines) == 1
    assert "SUCCESS" in lines[0]
    assert "[abc12345]" in lines[0]


def test_task_id_truncated_to_8_chars(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path)
    fl.write("abcdefgh-1234-5678-abcd-000000000000", "func", "2024-01-01T00:00:00 msg")
    fl.close()

    line = open(path).read()
    assert "[abcdefgh]" in line
    assert "abcdefgh-1234" not in line


# ---------------------------------------------------------------------------
# Integration: file logger wired through executor
# ---------------------------------------------------------------------------


async def test_task_log_written_to_file(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path, log_lifecycle=True)
    store = TaskStore()
    store.create("t1", "my_func", (), {})

    async def my_func():
        task_log("step one")
        task_log("step two")

    await execute_task(my_func, "t1", TaskConfig(), store, (), {}, file_logger=fl)
    fl.close()

    lines = open(path).readlines()
    # 2 task_log lines + RUNNING + SUCCESS lifecycle = 4 lines
    assert len(lines) == 4
    content = "".join(lines)
    assert "step one" in content
    assert "step two" in content
    assert "RUNNING" in content
    assert "SUCCESS" in content


async def test_retry_separator_written_to_file(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path)
    store = TaskStore()
    store.create("t2", "flaky_func", (), {})
    attempts = []

    def flaky_func():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("not yet")
        task_log("done")

    await execute_task(
        flaky_func, "t2", TaskConfig(retries=2, delay=0), store, (), {}, file_logger=fl
    )
    fl.close()

    content = open(path).read()
    assert "--- Retry 1 ---" in content
    assert "done" in content


async def test_failed_task_lifecycle_written(tmp_path):
    path = str(tmp_path / "tasks.log")
    fl = TaskFileLogger(path, log_lifecycle=True)
    store = TaskStore()
    store.create("t3", "bad_func", (), {})

    def bad_func():
        raise ValueError("boom")

    await execute_task(bad_func, "t3", TaskConfig(), store, (), {}, file_logger=fl)
    fl.close()

    content = open(path).read()
    assert "FAILED" in content
    assert "RUNNING" in content


# ---------------------------------------------------------------------------
# Shutdown: INTERRUPTED written to file via flush_pending
# ---------------------------------------------------------------------------


async def test_interrupted_lifecycle_written_on_flush_pending(tmp_path):
    from fastapi_taskflow.models import TaskStatus
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
    tm.file_logger.close()

    content = open(log_path).read()
    assert "INTERRUPTED" in content
    assert "[r1]" in content


# ---------------------------------------------------------------------------
# Integration: TaskManager log_file param
# ---------------------------------------------------------------------------


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

    tm.file_logger.close()

    content = open(path).read()
    assert "Hello world" in content
    assert "greet" in content
