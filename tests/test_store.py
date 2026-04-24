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


# ---------------------------------------------------------------------------
# delete_completed_before
# ---------------------------------------------------------------------------


def test_delete_completed_before_removes_terminal_records():
    from datetime import datetime, timezone, timedelta

    store = TaskStore()
    store.create("old", "f", (), {})
    old_end = datetime.now(timezone.utc) - timedelta(hours=2)
    store.update("old", status=TaskStatus.SUCCESS, end_time=old_end)

    store.create("recent", "f", (), {})
    recent_end = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.update("recent", status=TaskStatus.SUCCESS, end_time=recent_end)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    deleted = store.delete_completed_before(cutoff)

    assert deleted == 1
    assert store.get("old") is None
    assert store.get("recent") is not None


def test_delete_completed_before_skips_live_tasks():
    from datetime import datetime, timezone, timedelta

    store = TaskStore()
    store.create("pending", "f", (), {})
    store.create("running", "f", (), {})
    store.update("running", status=TaskStatus.RUNNING)

    cutoff = datetime.now(timezone.utc) + timedelta(hours=1)
    deleted = store.delete_completed_before(cutoff)

    assert deleted == 0
    assert store.get("pending") is not None
    assert store.get("running") is not None


def test_delete_completed_before_removes_failed_and_interrupted():
    from datetime import datetime, timezone, timedelta

    store = TaskStore()
    old = datetime.now(timezone.utc) - timedelta(days=2)

    store.create("f1", "f", (), {})
    store.update("f1", status=TaskStatus.FAILED, end_time=old)

    store.create("i1", "f", (), {})
    store.update("i1", status=TaskStatus.INTERRUPTED, end_time=old)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    deleted = store.delete_completed_before(cutoff)

    assert deleted == 2
    assert store.get("f1") is None
    assert store.get("i1") is None
