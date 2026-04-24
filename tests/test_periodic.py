"""Tests for the periodic task scheduler.

Covers:
- ScheduledEntry.compute_next for every= and cron= triggers
- PeriodicScheduler fires tasks at the right time
- PeriodicScheduler respects the distributed lock (backend present)
- Distributed lock: SQLite acquire/release behaviour
- TaskManager.schedule() decorator registration
- schedule() raises on bad arguments
- Scheduled runs produce TaskRecord with source="scheduled"
- Manually enqueued scheduled functions produce source="manual"
- run_on_startup fires immediately on first tick
"""

import asyncio
from datetime import datetime, timezone

import pytest

from fastapi_taskflow import TaskManager
from fastapi_taskflow.models import TaskConfig, TaskStatus
from fastapi_taskflow.periodic import PeriodicScheduler, ScheduledEntry, _next_every
from fastapi_taskflow.store import TaskStore


# ---------------------------------------------------------------------------
# _next_every helper
# ---------------------------------------------------------------------------


def test_next_every_advances_by_interval():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = _next_every(300, now)
    assert result == datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# ScheduledEntry.compute_next
# ---------------------------------------------------------------------------


def test_compute_next_every():
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    entry = ScheduledEntry(
        func=lambda: None,
        config=TaskConfig(),
        every=60.0,
        cron=None,
        run_on_startup=False,
    )
    result = entry.compute_next(now)
    assert result == datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)


def test_compute_next_cron():
    pytest.importorskip("croniter")
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    entry = ScheduledEntry(
        func=lambda: None,
        config=TaskConfig(),
        every=None,
        cron="0 * * * *",
        run_on_startup=False,
    )
    result = entry.compute_next(now)
    assert result == datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_compute_next_cron_with_timezone():
    pytest.importorskip("croniter")
    # 00:00 UTC is 20:00 America/New_York (UTC-4 in summer / UTC-5 in winter)
    # Use a winter date so offset is predictable: UTC-5
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    entry = ScheduledEntry(
        func=lambda: None,
        config=TaskConfig(),
        every=None,
        cron="0 9 * * *",  # 09:00 local time
        run_on_startup=False,
        timezone="America/New_York",
    )
    result = entry.compute_next(now)
    # 09:00 New York (UTC-5) = 14:00 UTC
    assert result == datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)


def test_compute_next_cron_raises_without_croniter(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "croniter":
            raise ImportError("no croniter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    entry = ScheduledEntry(
        func=lambda: None,
        config=TaskConfig(),
        every=None,
        cron="0 * * * *",
        run_on_startup=False,
    )
    with pytest.raises(ImportError, match="croniter"):
        entry.compute_next(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# TaskManager.schedule() registration
# ---------------------------------------------------------------------------


def test_schedule_registers_task_in_registry():
    tm = TaskManager()

    @tm.schedule(every=60)
    def my_task() -> None:
        pass

    assert tm.registry.get_config(my_task) is not None


def test_schedule_creates_periodic_scheduler():
    tm = TaskManager()

    @tm.schedule(every=60)
    def my_task() -> None:
        pass

    assert tm._periodic_scheduler is not None
    assert len(tm._periodic_scheduler.entries) == 1


def test_schedule_multiple_tasks():
    tm = TaskManager()

    @tm.schedule(every=60)
    def task_a() -> None:
        pass

    @tm.schedule(every=120)
    def task_b() -> None:
        pass

    assert len(tm._periodic_scheduler.entries) == 2


def test_schedule_raises_without_trigger():
    tm = TaskManager()
    with pytest.raises(ValueError, match="exactly one"):

        @tm.schedule()
        def my_task() -> None:
            pass


def test_schedule_raises_with_both_triggers():
    tm = TaskManager()
    with pytest.raises(ValueError, match="exactly one"):

        @tm.schedule(every=60, cron="0 * * * *")
        def my_task() -> None:
            pass


def test_schedule_function_still_callable():
    """Decorated function must remain directly callable (not wrapped)."""
    tm = TaskManager()
    results = []

    @tm.schedule(every=60)
    def my_task() -> None:
        results.append(1)

    my_task()
    assert results == [1]


# ---------------------------------------------------------------------------
# PeriodicScheduler.start sets next_run correctly
# ---------------------------------------------------------------------------


async def test_start_sets_next_run_in_future():
    tm = TaskManager()

    @tm.schedule(every=300)
    def my_task() -> None:
        pass

    before = datetime.now(timezone.utc)
    tm._periodic_scheduler.start()
    entry = tm._periodic_scheduler.entries[0]
    tm._periodic_scheduler.stop()

    assert entry.next_run > before


async def test_start_run_on_startup_sets_next_run_now():
    tm = TaskManager()

    @tm.schedule(every=300, run_on_startup=True)
    def my_task() -> None:
        pass

    tm._periodic_scheduler.start()
    entry = tm._periodic_scheduler.entries[0]
    tm._periodic_scheduler.stop()

    # next_run should be <= now (set to current time, not future)
    assert entry.next_run <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# PeriodicScheduler fires tasks and produces TaskRecords
# ---------------------------------------------------------------------------


async def test_scheduler_fires_task_and_creates_record():
    tm = TaskManager()
    fired = []

    @tm.schedule(every=0.5)
    async def quick_task() -> None:
        fired.append(1)

    tm._periodic_scheduler.start()
    await asyncio.sleep(2.5)
    tm._periodic_scheduler.stop()

    # Give in-flight tasks a moment to complete
    await asyncio.sleep(0.2)

    assert len(fired) >= 1

    records = [r for r in tm.store.list() if r.func_name == "quick_task"]
    assert len(records) >= 1
    assert all(r.source == "scheduled" for r in records)
    assert any(r.status == TaskStatus.SUCCESS for r in records)


async def test_scheduler_fires_sync_task():
    tm = TaskManager()
    fired = []

    @tm.schedule(every=0.5)
    def sync_task() -> None:
        fired.append(1)

    tm._periodic_scheduler.start()
    await asyncio.sleep(2.5)
    tm._periodic_scheduler.stop()
    await asyncio.sleep(0.2)

    assert len(fired) >= 1
    records = [r for r in tm.store.list() if r.func_name == "sync_task"]
    assert any(r.status == TaskStatus.SUCCESS for r in records)


# ---------------------------------------------------------------------------
# source field
# ---------------------------------------------------------------------------


async def test_manual_enqueue_has_source_manual():
    from fastapi import BackgroundTasks

    from fastapi_taskflow.wrapper import ManagedBackgroundTasks

    tm = TaskManager()

    @tm.task()
    async def my_task() -> None:
        pass

    bt = ManagedBackgroundTasks(tm, BackgroundTasks())
    task_id = bt.add_task(my_task)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.source == "manual"


def test_store_create_default_source_is_manual():
    store = TaskStore()
    record = store.create("t1", "func", (), {})
    assert record.source == "manual"


def test_store_create_scheduled_source():
    store = TaskStore()
    record = store.create("t1", "func", (), {}, source="scheduled")
    assert record.source == "scheduled"


def test_task_record_to_dict_includes_source():
    store = TaskStore()
    record = store.create("t1", "func", (), {}, source="scheduled")
    d = record.to_dict()
    assert d["source"] == "scheduled"


# ---------------------------------------------------------------------------
# Distributed lock (no backend — default always acquires)
# ---------------------------------------------------------------------------


async def test_scheduler_without_backend_always_fires():
    """Without a backend, the default lock always returns True."""
    tm = TaskManager()
    fired = []

    @tm.schedule(every=0.5)
    async def no_lock_task() -> None:
        fired.append(1)

    tm._periodic_scheduler.start()
    await asyncio.sleep(2.5)
    tm._periodic_scheduler.stop()
    await asyncio.sleep(0.2)

    assert len(fired) >= 1


# ---------------------------------------------------------------------------
# Distributed lock — SQLite backend
# ---------------------------------------------------------------------------


async def test_sqlite_schedule_lock_acquired_first_time(tmp_path):
    from fastapi_taskflow.backends.sqlite import SqliteBackend

    backend = SqliteBackend(str(tmp_path / "tasks.db"))
    acquired = await backend.acquire_schedule_lock("schedule:my_task", ttl=60)
    assert acquired is True


async def test_sqlite_schedule_lock_blocked_second_time(tmp_path):
    from fastapi_taskflow.backends.sqlite import SqliteBackend

    backend = SqliteBackend(str(tmp_path / "tasks.db"))
    first = await backend.acquire_schedule_lock("schedule:my_task", ttl=60)
    second = await backend.acquire_schedule_lock("schedule:my_task", ttl=60)
    assert first is True
    assert second is False


async def test_sqlite_schedule_lock_different_keys_independent(tmp_path):
    from fastapi_taskflow.backends.sqlite import SqliteBackend

    backend = SqliteBackend(str(tmp_path / "tasks.db"))
    a = await backend.acquire_schedule_lock("schedule:task_a", ttl=60)
    b = await backend.acquire_schedule_lock("schedule:task_b", ttl=60)
    assert a is True
    assert b is True


async def test_sqlite_schedule_lock_expires_and_reacquires(tmp_path):
    from fastapi_taskflow.backends.sqlite import SqliteBackend

    backend = SqliteBackend(str(tmp_path / "tasks.db"))
    # Acquire with a 0-second TTL so it expires immediately
    first = await backend.acquire_schedule_lock("schedule:my_task", ttl=0)
    # Wait a moment and try again — expired lock should be gone
    await asyncio.sleep(0.05)
    second = await backend.acquire_schedule_lock("schedule:my_task", ttl=60)
    assert first is True
    assert second is True


# ---------------------------------------------------------------------------
# Distributed lock prevents double-firing in PeriodicScheduler
# ---------------------------------------------------------------------------


async def test_lock_prevents_double_fire(tmp_path):
    """Two schedulers sharing a SQLite backend should fire each entry once."""
    from fastapi_taskflow.backends.sqlite import SqliteBackend

    db = str(tmp_path / "tasks.db")
    backend = SqliteBackend(db)

    tm1 = TaskManager()
    tm2 = TaskManager()
    fired = []

    def tracked() -> None:
        fired.append(1)

    config = TaskConfig()
    tm1.registry.register(tracked, config)
    tm2.registry.register(tracked, config)

    entry1 = ScheduledEntry(
        func=tracked, config=config, every=0.5, cron=None, run_on_startup=True
    )
    entry2 = ScheduledEntry(
        func=tracked, config=config, every=0.5, cron=None, run_on_startup=True
    )

    ps1 = PeriodicScheduler(tm1, [entry1], backend=backend)
    ps2 = PeriodicScheduler(tm2, [entry2], backend=backend)

    ps1.start()
    ps2.start()
    await asyncio.sleep(2.5)
    ps1.stop()
    ps2.stop()
    await asyncio.sleep(0.2)

    # Without a lock, two schedulers at 0.5s over 2.5s = ~10 fires total.
    # With the lock, only one fires per slot, so ~5 fires.
    # We assert strictly less than the no-lock maximum.
    assert len(fired) < 10
