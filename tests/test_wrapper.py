from fastapi import BackgroundTasks

from fastapi_taskflow import ManagedBackgroundTasks, TaskManager
from fastapi_taskflow.models import TaskStatus


def test_add_task_returns_uuid():
    tm = TaskManager()

    @tm.task(retries=1)
    def work(x):
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work, 42)

    assert isinstance(task_id, str)
    assert len(task_id) == 36  # UUID4


def test_task_created_in_store():
    tm = TaskManager()

    @tm.task()
    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.func_name == "work"
    assert record.status == TaskStatus.PENDING


def test_unregistered_func_still_works():
    tm = TaskManager()

    def plain():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(plain)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.func_name == "plain"


def test_multiple_tasks_get_unique_ids():
    tm = TaskManager()

    @tm.task()
    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    ids = {managed.add_task(work) for _ in range(5)}
    assert len(ids) == 5


def test_is_background_tasks_subclass():
    tm = TaskManager()
    assert isinstance(ManagedBackgroundTasks(tm), BackgroundTasks)


def test_tasks_list_populated_after_add_task():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work)
    assert len(managed.tasks) == 1


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def test_idempotency_key_dedup_returns_existing_task_id():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    id1 = managed.add_task(work, idempotency_key="op-1")
    id2 = managed.add_task(work, idempotency_key="op-1")

    assert id1 == id2
    assert len(tm.store.list()) == 1


def test_idempotency_key_allows_resubmit_after_failure():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    id1 = managed.add_task(work, idempotency_key="op-2")
    tm.store.update(
        id1,
        status=__import__(
            "fastapi_taskflow.models", fromlist=["TaskStatus"]
        ).TaskStatus.FAILED,
    )

    id2 = managed.add_task(work, idempotency_key="op-2")
    assert id1 != id2


def test_idempotency_key_allows_resubmit_after_interrupted():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    id1 = managed.add_task(work, idempotency_key="op-3")
    from fastapi_taskflow.models import TaskStatus

    tm.store.update(id1, status=TaskStatus.INTERRUPTED)

    id2 = managed.add_task(work, idempotency_key="op-3")
    assert id1 != id2
