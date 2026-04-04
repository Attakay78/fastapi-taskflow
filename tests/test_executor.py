from fastapi_taskflow.executor import execute_task
from fastapi_taskflow.models import TaskConfig, TaskStatus
from fastapi_taskflow.store import TaskStore


def make_store(task_id: str = "t1") -> TaskStore:
    store = TaskStore()
    store.create(task_id, "func", (), {})
    return store


# ---------------------------------------------------------------------------
# Sync tasks
# ---------------------------------------------------------------------------


async def test_sync_task_success():
    store = make_store()
    results = []

    def func():
        results.append(1)

    await execute_task(func, "t1", TaskConfig(), store, (), {})

    assert results == [1]
    assert store.get("t1").status == TaskStatus.SUCCESS
    assert store.get("t1").start_time is not None
    assert store.get("t1").end_time is not None


async def test_sync_task_with_args():
    store = make_store()
    captured = []

    def func(x, y, z=0):
        captured.append((x, y, z))

    await execute_task(func, "t1", TaskConfig(), store, (1, 2), {"z": 3})

    assert captured == [(1, 2, 3)]


# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------


async def test_async_task_success():
    store = make_store()
    results = []

    async def func():
        results.append("done")

    await execute_task(func, "t1", TaskConfig(), store, (), {})

    assert results == ["done"]
    assert store.get("t1").status == TaskStatus.SUCCESS


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


async def test_no_retry_on_failure():
    store = make_store()
    attempts = []

    def func():
        attempts.append(1)
        raise RuntimeError("boom")

    await execute_task(func, "t1", TaskConfig(retries=0, delay=0), store, (), {})

    assert len(attempts) == 1
    assert store.get("t1").status == TaskStatus.FAILED
    assert "boom" in store.get("t1").error


async def test_retries_exhaust_and_fail():
    store = make_store()
    attempts = []

    def func():
        attempts.append(1)
        raise ValueError("oops")

    await execute_task(func, "t1", TaskConfig(retries=2, delay=0), store, (), {})

    assert len(attempts) == 3  # 1 initial + 2 retries
    record = store.get("t1")
    assert record.status == TaskStatus.FAILED
    assert record.retries_used == 2
    assert "oops" in record.error


async def test_retry_succeeds_on_second_attempt():
    store = make_store()
    attempts = []

    def func():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("not yet")

    await execute_task(func, "t1", TaskConfig(retries=3, delay=0), store, (), {})

    assert len(attempts) == 2
    assert store.get("t1").status == TaskStatus.SUCCESS
    assert store.get("t1").retries_used == 1


async def test_retries_used_not_set_on_first_success():
    store = make_store()

    def func():
        pass

    await execute_task(func, "t1", TaskConfig(retries=2, delay=0), store, (), {})

    assert store.get("t1").retries_used == 0
