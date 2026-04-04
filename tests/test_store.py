from fastapi_taskflow.models import TaskStatus
from fastapi_taskflow.store import TaskStore


def test_create_and_get():
    store = TaskStore()
    record = store.create("t1", "my_func", (1, 2), {"key": "val"})
    assert record.task_id == "t1"
    assert record.func_name == "my_func"
    assert record.status == TaskStatus.PENDING
    assert store.get("t1") is record


def test_update_status():
    store = TaskStore()
    store.create("t1", "f", (), {})
    store.update("t1", status=TaskStatus.RUNNING)
    assert store.get("t1").status == TaskStatus.RUNNING


def test_update_missing_returns_none():
    store = TaskStore()
    result = store.update("nonexistent", status=TaskStatus.RUNNING)
    assert result is None


def test_list_returns_all():
    store = TaskStore()
    store.create("t1", "f", (), {})
    store.create("t2", "g", (), {})
    assert len(store.list()) == 2


def test_get_missing_returns_none():
    store = TaskStore()
    assert store.get("ghost") is None


def test_clear():
    store = TaskStore()
    store.create("t1", "f", (), {})
    store.clear()
    assert store.list() == []
