"""Tests for one-off scheduled tasks (``schedule_once`` / ``run_at``).

Covers:
- Backend storage contract: upsert-by-run_key, horizon query, atomic claim
- run_key replaces rather than duplicates (rescheduling a moved deadline)
- claim_scheduled is exactly-once across concurrent callers
- cancel_scheduled removes from the backend, not just the in-memory heap
- The horizon window bounds what is held in memory
- Entries inside the horizon are armed immediately, beyond it are not
- Firing produces a normal TaskRecord with source="scheduled"
- Args, kwargs, tags and encrypted payloads survive the round trip
- run_key and idempotency_key are independent namespaces
- schedule_once fails loudly when the backend cannot support it
- One-off entries never enter the dashboard's schedule entry list
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends.base import SnapshotBackend
from fastapi_taskflow.backends.sqlite import SqliteBackend
from fastapi_taskflow.dashboard.sse import (
    _get_schedule_entries,
    _get_schedule_summary,
)
from fastapi_taskflow.models import ScheduledOnce, TaskStatus
from fastapi_taskflow.periodic import PeriodicScheduler, ScheduledEntry


@pytest.fixture
def db_path():
    path = os.path.join(tempfile.mkdtemp(), "sched.db")
    yield path


@pytest.fixture
def backend(db_path):
    b = SqliteBackend(db_path)
    yield b


def _entry(run_key="k1", func_name="my_task", seconds=60, **kw):
    return ScheduledOnce(
        run_key=run_key,
        func_name=func_name,
        fire_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        **kw,
    )


# ---------------------------------------------------------------------------
# Backend storage contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_due_round_trip(backend):
    await backend.save_scheduled(
        _entry(args=("tx1",), kwargs={"reason": "timeout"}, tags={"env": "prod"},
               priority=5, queue="reports", idempotency_key="idem-1")
    )
    due = await backend.load_due(datetime.now(timezone.utc) + timedelta(minutes=5))

    assert len(due) == 1
    e = due[0]
    assert e.run_key == "k1"
    assert e.args == ("tx1",)
    assert e.kwargs == {"reason": "timeout"}
    assert e.tags == {"env": "prod"}
    assert e.priority == 5
    assert e.queue == "reports"
    assert e.idempotency_key == "idem-1"


@pytest.mark.asyncio
async def test_load_due_excludes_entries_beyond_the_bound(backend):
    await backend.save_scheduled(_entry("soon", seconds=60))
    await backend.save_scheduled(_entry("later", seconds=48 * 3600))

    due = await backend.load_due(datetime.now(timezone.utc) + timedelta(minutes=5))

    assert [e.run_key for e in due] == ["soon"]


@pytest.mark.asyncio
async def test_same_run_key_replaces_rather_than_duplicates(backend):
    """The motivating case: a deadline moved, so reschedule under the same key."""
    await backend.save_scheduled(_entry("k1", seconds=60))
    await backend.save_scheduled(_entry("k1", seconds=120))

    due = await backend.load_due(datetime.now(timezone.utc) + timedelta(minutes=5))

    assert len(due) == 1
    # The later fire_at won.
    assert due[0].fire_at > datetime.now(timezone.utc) + timedelta(seconds=90)


@pytest.mark.asyncio
async def test_claim_scheduled_is_exactly_once(backend):
    await backend.save_scheduled(_entry("k1"))

    first = await backend.claim_scheduled("k1")
    second = await backend.claim_scheduled("k1")

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_claim_scheduled_under_concurrency_yields_one_winner(backend):
    await backend.save_scheduled(_entry("k1"))

    results = await asyncio.gather(*[backend.claim_scheduled("k1") for _ in range(20)])

    assert sum(results) == 1


@pytest.mark.asyncio
async def test_delete_scheduled_reports_whether_anything_was_removed(backend):
    await backend.save_scheduled(_entry("k1"))

    assert await backend.delete_scheduled("k1") is True
    assert await backend.delete_scheduled("k1") is False
    assert await backend.delete_scheduled("never-existed") is False


@pytest.mark.asyncio
async def test_entries_survive_a_new_backend_instance(backend, db_path):
    """R1: a pending firing must outlive the process that scheduled it."""
    await backend.save_scheduled(_entry("k1", args=("tx1",)))
    await backend.close()

    reopened = SqliteBackend(db_path)
    due = await reopened.load_due(datetime.now(timezone.utc) + timedelta(minutes=5))

    assert [e.run_key for e in due] == ["k1"]
    assert due[0].args == ("tx1",)
    await reopened.close()


# ---------------------------------------------------------------------------
# TaskManager API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_once_persists_to_backend(db_path):
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task(x):
        pass

    await tm.schedule_once(
        my_task, "tx1", run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        run_key="ac:tx1",
    )

    pending = await tm.list_scheduled()
    assert [e.run_key for e in pending] == ["ac:tx1"]
    assert pending[0].args == ("tx1",)


@pytest.mark.asyncio
async def test_schedule_once_treats_naive_datetime_as_utc(db_path):
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    naive = datetime.now() + timedelta(hours=1)
    await tm.schedule_once(my_task, run_at=naive, run_key="k1")

    pending = await tm.list_scheduled()
    assert pending[0].fire_at.tzinfo is not None


@pytest.mark.asyncio
async def test_cancel_scheduled_removes_from_backend(db_path):
    """R4: cancel must reach the backend, or a restart re-arms the cancelled task."""
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    await tm.schedule_once(
        my_task, run_at=datetime.now(timezone.utc) + timedelta(hours=1), run_key="k1"
    )
    removed = await tm.cancel_scheduled("k1")

    assert removed is True
    assert await tm.list_scheduled() == []


@pytest.mark.asyncio
async def test_cancel_scheduled_is_false_for_unknown_key(db_path):
    tm = TaskManager(snapshot_db=db_path)
    assert await tm.cancel_scheduled("never-scheduled") is False


@pytest.mark.asyncio
async def test_schedule_once_requires_a_backend():
    tm = TaskManager()

    @tm.task()
    def my_task():
        pass

    with pytest.raises(RuntimeError, match="requires a snapshot backend"):
        await tm.schedule_once(
            my_task, run_at=datetime.now(timezone.utc), run_key="k1"
        )


@pytest.mark.asyncio
async def test_schedule_once_rejects_backend_without_support(db_path):
    """A custom backend must opt in explicitly rather than silently dropping."""

    class BareBackend(SnapshotBackend):
        async def save(self, records):
            return 0

        async def load(self):
            return []

        async def save_pending(self, records):
            return 0

        async def load_pending(self):
            return []

        async def clear_pending(self):
            pass

        async def close(self):
            pass

    tm = TaskManager(snapshot_backend=BareBackend())

    @tm.task()
    def my_task():
        pass

    with pytest.raises(RuntimeError, match="does not support one-off schedules"):
        await tm.schedule_once(
            my_task, run_at=datetime.now(timezone.utc), run_key="k1"
        )


@pytest.mark.asyncio
async def test_run_key_and_idempotency_key_are_independent(db_path):
    """Rescheduling under a run_key must not be blocked by idempotency dedup."""
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    for offset in (1, 2, 3):
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(hours=offset),
            run_key="same-key",
            idempotency_key="same-idem",
        )

    pending = await tm.list_scheduled()
    assert len(pending) == 1
    assert pending[0].idempotency_key == "same-idem"


# ---------------------------------------------------------------------------
# Scheduler behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_off_fires_and_produces_a_task_record(db_path):
    tm = TaskManager(snapshot_db=db_path)
    fired = []

    @tm.task()
    def my_task(value):
        fired.append(value)

    # A scheduler must exist for one-offs to be driven.
    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await tm.schedule_once(
            my_task,
            "tx1",
            run_at=datetime.now(timezone.utc) + timedelta(milliseconds=50),
            run_key="ac:tx1",
        )
        await asyncio.sleep(0.6)

        assert fired == ["tx1"]
        records = [r for r in tm.store.list() if r.func_name == "my_task"]
        assert len(records) == 1
        assert records[0].source == "scheduled"
        assert records[0].status == TaskStatus.SUCCESS
        # The backend row is consumed by the claim.
        assert await tm.list_scheduled() == []
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_one_off_does_not_refire(db_path):
    """recurring=False means dropped from the heap, not re-pushed."""
    tm = TaskManager(snapshot_db=db_path)
    calls = []

    @tm.task()
    def my_task():
        calls.append(1)

    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(milliseconds=50),
            run_key="k1",
        )
        await asyncio.sleep(0.8)
        assert len(calls) == 1
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_cancelled_one_off_never_fires(db_path):
    tm = TaskManager(snapshot_db=db_path)
    calls = []

    @tm.task()
    def my_task():
        calls.append(1)

    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(milliseconds=300),
            run_key="k1",
        )
        await tm.cancel_scheduled("k1")
        await asyncio.sleep(0.6)
        assert calls == []
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_rescheduling_before_fire_uses_the_new_time(db_path):
    """The deadline moved: only the later firing should happen, exactly once."""
    tm = TaskManager(snapshot_db=db_path)
    calls = []

    @tm.task()
    def my_task():
        calls.append(datetime.now(timezone.utc))

    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(milliseconds=100),
            run_key="k1",
        )
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(milliseconds=400),
            run_key="k1",
        )
        await asyncio.sleep(0.9)
        assert len(calls) == 1
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_pending_entry_is_rearmed_on_startup(db_path):
    """R1: an entry scheduled before a restart still fires after it."""
    seed = SqliteBackend(db_path)
    await seed.save_scheduled(
        ScheduledOnce(
            run_key="k1",
            func_name="my_task",
            fire_at=datetime.now(timezone.utc) + timedelta(milliseconds=50),
        )
    )
    await seed.close()

    tm = TaskManager(snapshot_db=db_path)
    calls = []

    @tm.task()
    def my_task():
        calls.append(1)

    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await asyncio.sleep(0.6)
        assert calls == [1]
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_entry_beyond_the_horizon_is_not_armed_in_memory(db_path):
    """The horizon is what keeps the heap bounded regardless of pending count."""
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    @tm.schedule(every=3600)
    def keepalive():
        pass

    tm._periodic_scheduler._horizon = 60.0

    await tm.startup()
    try:
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(hours=48),
            run_key="far",
        )
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            run_key="near",
        )

        armed = set(tm._periodic_scheduler._one_offs)
        assert armed == {"near"}
        # Both are still persisted; only memory is bounded.
        assert len(await tm.list_scheduled()) == 2
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_refill_arms_entries_as_the_horizon_advances(db_path):
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    @tm.schedule(every=3600)
    def keepalive():
        pass

    scheduler = tm._periodic_scheduler
    # Narrow horizon: the entry starts outside it and so is not armed.
    scheduler._horizon = 60.0

    await tm.schedule_once(
        my_task,
        run_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        run_key="k1",
    )
    assert scheduler._one_offs == {}

    # Widening the horizon is equivalent to time advancing toward the entry.
    scheduler._horizon = 3600.0
    await scheduler._refill(datetime.now(timezone.utc))
    assert set(scheduler._one_offs) == {"k1"}


@pytest.mark.asyncio
async def test_refill_is_idempotent_for_unchanged_entries(db_path):
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    @tm.schedule(every=3600)
    def keepalive():
        pass

    scheduler = tm._periodic_scheduler
    scheduler._horizon = 3600.0

    await tm.schedule_once(
        my_task,
        run_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        run_key="k1",
    )
    for _ in range(5):
        await scheduler._refill(datetime.now(timezone.utc))

    assert len(scheduler._one_offs) == 1
    # No tombstone churn: repeated refills must not stack dead heap entries.
    # Recurring entries only enter the heap on start(), so the one-off is
    # the only member here.
    assert len(scheduler._heap) == 1
    assert not scheduler._heap[0].cancelled


@pytest.mark.asyncio
async def test_encrypted_args_survive_the_round_trip(db_path):
    from cryptography.fernet import Fernet

    tm = TaskManager(snapshot_db=db_path, encrypt_args_key=Fernet.generate_key())
    received = []

    @tm.task()
    def my_task(secret):
        received.append(secret)

    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await tm.schedule_once(
            my_task,
            "hunter2",
            run_at=datetime.now(timezone.utc) + timedelta(milliseconds=50),
            run_key="k1",
        )
        await asyncio.sleep(0.6)
        assert received == ["hunter2"]
    finally:
        await tm.shutdown()


@pytest.mark.asyncio
async def test_unregistered_function_does_not_arm(db_path):
    """A stale entry whose function is gone must not crash the loop."""
    seed = SqliteBackend(db_path)
    await seed.save_scheduled(
        ScheduledOnce(
            run_key="k1",
            func_name="function_from_a_previous_deploy",
            fire_at=datetime.now(timezone.utc) + timedelta(milliseconds=50),
        )
    )
    await seed.close()

    tm = TaskManager(snapshot_db=db_path)

    @tm.schedule(every=3600)
    def keepalive():
        pass

    await tm.startup()
    try:
        await asyncio.sleep(0.5)
        assert tm._periodic_scheduler._one_offs == {}
    finally:
        await tm.shutdown()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_recurring_entries_are_listed_for_the_dashboard():
    tm = TaskManager()

    @tm.schedule(every=60)
    def recurring_task():
        pass

    entries = _get_schedule_entries(tm)
    assert [e["func_name"] for e in entries] == ["recurring_task"]


@pytest.mark.asyncio
async def test_one_offs_are_excluded_from_the_dashboard_entry_list(db_path):
    """One-offs are unbounded; enumerating them per SSE tick blocks the loop."""
    tm = TaskManager(snapshot_db=db_path)

    @tm.task()
    def my_task():
        pass

    @tm.schedule(every=60)
    def recurring_task():
        pass

    for i in range(50):
        await tm.schedule_once(
            my_task,
            run_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            run_key=f"k{i}",
        )

    entries = _get_schedule_entries(tm)
    assert [e["func_name"] for e in entries] == ["recurring_task"]
    assert _get_schedule_summary(tm) == {"one_off_armed": 50}


def test_scheduler_entries_property_excludes_one_offs():
    tm = TaskManager()
    scheduler = PeriodicScheduler(tm, [])
    scheduler._one_offs["k1"] = ScheduledEntry(
        func=lambda: None,
        config=None,
        every=None,
        cron=None,
        run_on_startup=False,
        once=_entry(),
    )
    assert scheduler.entries == []
    assert scheduler.pending_one_off_count == 1


# ---------------------------------------------------------------------------
# Shared SQL row decoder
#
# Postgres and MySQL return positional tuples decoded by row_to_scheduled.
# Neither server is exercised by this suite, so the column-order contract is
# pinned here: a mismatch between the SELECT list and the unpacking would
# silently map values onto the wrong fields.
# ---------------------------------------------------------------------------


def test_scheduled_columns_matches_row_decoder_order():
    from fastapi_taskflow.backends.base import SCHEDULED_COLUMNS, row_to_scheduled

    columns = [c.strip() for c in SCHEDULED_COLUMNS.split(",")]
    assert columns == [
        "run_key",
        "func_name",
        "fire_at",
        "created_at",
        "args_json",
        "kwargs_json",
        "encrypted_payload",
        "queue",
        "priority",
        "idempotency_key",
        "tags_json",
    ]

    fire_at = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc)
    row = (
        "ac:tx1",
        "auto_complete",
        fire_at.isoformat(),
        created.isoformat(),
        '["tx1"]',
        '{"reason": "timeout"}',
        None,
        "reports",
        7,
        "idem-1",
        '{"env": "prod"}',
    )

    entry = row_to_scheduled(row)

    assert entry.run_key == "ac:tx1"
    assert entry.func_name == "auto_complete"
    assert entry.fire_at == fire_at
    assert entry.created_at == created
    assert entry.args == ("tx1",)
    assert entry.kwargs == {"reason": "timeout"}
    assert entry.encrypted_payload is None
    assert entry.queue == "reports"
    assert entry.priority == 7
    assert entry.idempotency_key == "idem-1"
    assert entry.tags == {"env": "prod"}


def test_row_decoder_handles_null_columns():
    from fastapi_taskflow.backends.base import row_to_scheduled

    row = (
        "k1",
        "my_task",
        datetime(2026, 7, 25, tzinfo=timezone.utc).isoformat(),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

    entry = row_to_scheduled(row)

    assert entry.args == ()
    assert entry.kwargs == {}
    assert entry.tags == {}
    assert entry.queue == "default"
    assert entry.priority is None
    assert entry.idempotency_key is None
    assert entry.created_at is not None


def test_sql_upsert_placeholder_count_matches_columns():
    """A miscounted VALUES list fails only at runtime, on a server we can't run here."""
    from fastapi_taskflow.backends import mysql, postgres, sqlite

    for statement, marker in (
        (sqlite._UPSERT_SCHEDULED, "?"),
        (postgres._UPSERT_SCHEDULED, "%s"),
        (mysql._UPSERT_SCHEDULED, "%s"),
    ):
        values_clause = statement.split("VALUES")[1].split(")")[0]
        assert values_clause.count(marker) == 11, statement


def test_compute_next_rejects_one_off_entries():
    entry = ScheduledEntry(
        func=lambda: None,
        config=None,
        every=None,
        cron=None,
        run_on_startup=False,
        once=_entry(),
    )
    assert entry.recurring is False
    with pytest.raises(ValueError, match="not meaningful"):
        entry.compute_next(datetime.now(timezone.utc))
