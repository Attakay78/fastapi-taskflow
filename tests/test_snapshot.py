from fastapi_taskflow import TaskManager
from fastapi_taskflow.models import TaskStatus
from fastapi_taskflow.snapshot import SnapshotScheduler


def _make_scheduler(tmp_path) -> tuple[TaskManager, SnapshotScheduler]:
    tm = TaskManager()
    db = str(tmp_path / "tasks.db")
    scheduler = SnapshotScheduler(tm, db_path=db, interval=9999)
    return tm, scheduler


async def test_flush_persists_completed_tasks(tmp_path):
    tm, scheduler = _make_scheduler(tmp_path)

    # Manually create a completed task in the store
    tm.store.create("t1", "send_email", (), {})
    tm.store.update("t1", status=TaskStatus.SUCCESS)

    rows_written = await scheduler.flush()
    assert rows_written == 1

    records = scheduler.query()
    assert len(records) == 1
    assert records[0]["task_id"] == "t1"
    assert records[0]["status"] == "success"


async def test_flush_skips_pending_tasks(tmp_path):
    tm, scheduler = _make_scheduler(tmp_path)

    tm.store.create("t1", "func", (), {})
    # Leave as PENDING

    rows_written = await scheduler.flush()
    assert rows_written == 0
    assert scheduler.query() == []


async def test_flush_idempotent(tmp_path):
    tm, scheduler = _make_scheduler(tmp_path)

    tm.store.create("t1", "func", (), {})
    tm.store.update("t1", status=TaskStatus.FAILED)

    await scheduler.flush()
    await scheduler.flush()  # second flush is an upsert

    records = scheduler.query()
    assert len(records) == 1  # no duplicate rows


async def test_query_filter_by_status(tmp_path):
    tm, scheduler = _make_scheduler(tmp_path)

    tm.store.create("t1", "func", (), {})
    tm.store.update("t1", status=TaskStatus.SUCCESS)
    tm.store.create("t2", "func", (), {})
    tm.store.update("t2", status=TaskStatus.FAILED)

    await scheduler.flush()

    successes = scheduler.query(status="success")
    assert len(successes) == 1
    assert successes[0]["task_id"] == "t1"

    failures = scheduler.query(status="failed")
    assert len(failures) == 1
    assert failures[0]["task_id"] == "t2"


async def test_query_filter_by_func_name(tmp_path):
    tm, scheduler = _make_scheduler(tmp_path)

    tm.store.create("t1", "send_email", (), {})
    tm.store.update("t1", status=TaskStatus.SUCCESS)
    tm.store.create("t2", "process_data", (), {})
    tm.store.update("t2", status=TaskStatus.SUCCESS)

    await scheduler.flush()

    results = scheduler.query(func_name="send_email")
    assert len(results) == 1
    assert results[0]["func_name"] == "send_email"


# ---------------------------------------------------------------------------
# Load (restore on restart)
# ---------------------------------------------------------------------------


async def test_load_restores_records_into_store(tmp_path):
    db = str(tmp_path / "tasks.db")

    # First "run": persist two completed tasks
    tm1 = TaskManager()
    s1 = SnapshotScheduler(tm1, db_path=db, interval=9999)
    tm1.store.create("t1", "send_email", (), {})
    tm1.store.update("t1", status=TaskStatus.SUCCESS)
    tm1.store.create("t2", "process_data", (), {})
    tm1.store.update("t2", status=TaskStatus.FAILED, error="timeout")
    await s1.flush()

    # Second "run": fresh store, load from DB
    tm2 = TaskManager()
    s2 = SnapshotScheduler(tm2, db_path=db, interval=9999)
    loaded = await s2.load()

    assert loaded == 2
    assert tm2.store.get("t1").status == TaskStatus.SUCCESS
    assert tm2.store.get("t2").status == TaskStatus.FAILED
    assert tm2.store.get("t2").error == "timeout"


async def test_load_does_not_overwrite_live_tasks(tmp_path):
    db = str(tmp_path / "tasks.db")

    # Seed the DB
    tm1 = TaskManager()
    s1 = SnapshotScheduler(tm1, db_path=db, interval=9999)
    tm1.store.create("t1", "func", (), {})
    tm1.store.update("t1", status=TaskStatus.SUCCESS)
    await s1.flush()

    # New run: task already in the store (e.g. re-queued before load)
    tm2 = TaskManager()
    tm2.store.create("t1", "func", (), {})
    tm2.store.update("t1", status=TaskStatus.RUNNING)

    s2 = SnapshotScheduler(tm2, db_path=db, interval=9999)
    await s2.load()

    # The live RUNNING record must not be overwritten by the old SUCCESS snapshot
    assert tm2.store.get("t1").status == TaskStatus.RUNNING


async def test_load_empty_db_returns_zero(tmp_path):
    tm, scheduler = _make_scheduler(tmp_path)
    loaded = await scheduler.load()
    assert loaded == 0
    assert tm.store.list() == []


async def test_flush_and_load_preserves_args_and_kwargs(tmp_path):
    db = str(tmp_path / "tasks.db")

    tm1 = TaskManager()
    s1 = SnapshotScheduler(tm1, db_path=db, interval=9999)
    tm1.store.create("t1", "send_email", ("user@example.com",), {"retries": 3})
    tm1.store.update("t1", status=TaskStatus.SUCCESS)
    await s1.flush()

    tm2 = TaskManager()
    s2 = SnapshotScheduler(tm2, db_path=db, interval=9999)
    await s2.load()

    record = tm2.store.get("t1")
    assert record.args == ("user@example.com",)
    assert record.kwargs == {"retries": 3}


async def test_flush_and_load_empty_args_kwargs(tmp_path):
    db = str(tmp_path / "tasks.db")

    tm1 = TaskManager()
    s1 = SnapshotScheduler(tm1, db_path=db, interval=9999)
    tm1.store.create("t1", "no_args_func", (), {})
    tm1.store.update("t1", status=TaskStatus.SUCCESS)
    await s1.flush()

    tm2 = TaskManager()
    s2 = SnapshotScheduler(tm2, db_path=db, interval=9999)
    await s2.load()

    record = tm2.store.get("t1")
    assert record.args == ()
    assert record.kwargs == {}


# ---------------------------------------------------------------------------
# Pending requeue
# ---------------------------------------------------------------------------


async def test_flush_pending_saves_unfinished_tasks(tmp_path):
    db = str(tmp_path / "tasks.db")
    tm = TaskManager()
    s = SnapshotScheduler(tm, db_path=db, interval=9999, requeue_pending=True)

    tm.store.create("p1", "work", ("hello",), {"x": 1})
    # leave as PENDING

    count = await s.flush_pending()
    assert count == 1

    records = await s._backend.load_pending()
    assert len(records) == 1
    assert records[0].task_id == "p1"
    assert records[0].args == ("hello",)
    assert records[0].kwargs == {"x": 1}


async def test_flush_pending_normalises_running_to_pending(tmp_path):
    db = str(tmp_path / "tasks.db")
    tm = TaskManager()
    s = SnapshotScheduler(tm, db_path=db, interval=9999, requeue_pending=True)

    tm.store.create("r1", "work", (), {})
    tm.store.update("r1", status=TaskStatus.RUNNING)

    await s.flush_pending()

    # Store record should be normalised to PENDING
    assert tm.store.get("r1").status == TaskStatus.PENDING
    records = await s._backend.load_pending()
    assert records[0].status == TaskStatus.PENDING


async def test_requeue_dispatches_and_clears(tmp_path):
    import asyncio

    db = str(tmp_path / "tasks.db")
    executed: list[str] = []

    tm = TaskManager()

    @tm.task()
    def work(val: str) -> None:
        executed.append(val)

    s = SnapshotScheduler(tm, db_path=db, interval=9999, requeue_pending=True)

    # Simulate shutdown: save a pending task
    tm.store.create("q1", "work", ("hello",), {})
    await s.flush_pending()

    # Simulate startup on a fresh TaskManager with the same DB
    tm2 = TaskManager()

    @tm2.task()
    def work(val: str) -> None:  # noqa: F811
        executed.append(val)

    s2 = SnapshotScheduler(tm2, db_path=db, interval=9999, requeue_pending=True)
    requeued = await s2.requeue()
    assert requeued == 1

    # Let the event loop run the dispatched task
    await asyncio.sleep(0.05)
    assert "hello" in executed

    # Pending store must be cleared
    assert await s2._backend.load_pending() == []


async def test_requeue_skips_unregistered_function(tmp_path):
    db = str(tmp_path / "tasks.db")
    tm = TaskManager()
    s = SnapshotScheduler(tm, db_path=db, interval=9999, requeue_pending=True)

    # Save a pending task for a function name that won't be registered
    tm.store.create("u1", "ghost_func", (), {})
    await s.flush_pending()

    # Fresh manager with no registered functions
    tm2 = TaskManager()
    s2 = SnapshotScheduler(tm2, db_path=db, interval=9999, requeue_pending=True)
    requeued = await s2.requeue()

    assert requeued == 0
    # Pending store cleared even when nothing was dispatched
    assert await s2._backend.load_pending() == []
