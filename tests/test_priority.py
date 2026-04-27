"""Tests for priority queue dispatch ordering and per-call/decorator overrides."""

import asyncio

from fastapi_taskflow import ManagedBackgroundTasks, TaskManager
from fastapi_taskflow.models import TaskStatus


# ---------------------------------------------------------------------------
# Store record
# ---------------------------------------------------------------------------


def test_priority_stored_on_record():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work, priority=7)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.priority == 7


def test_no_priority_stored_as_none():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.priority is None


def test_decorator_priority_stored_on_record():
    tm = TaskManager()

    @tm.task(priority=5)
    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.priority == 5


def test_per_call_priority_overrides_decorator():
    """Per-call priority wins over the decorator-level default."""
    tm = TaskManager()

    @tm.task(priority=3)
    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work, priority=9)

    record = tm.store.get(task_id)
    assert record.priority == 9


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_task_decorator_sets_priority_config():
    tm = TaskManager()

    @tm.task(priority=8)
    def work():
        pass

    config = tm.registry.get_config(work)
    assert config is not None
    assert config.priority == 8


def test_task_decorator_default_priority_is_none():
    tm = TaskManager()

    @tm.task()
    def work():
        pass

    config = tm.registry.get_config(work)
    assert config is not None
    assert config.priority is None


# ---------------------------------------------------------------------------
# Queue routing — priority tasks go to priority queue, not Starlette list
# ---------------------------------------------------------------------------


def test_priority_task_not_added_to_starlette_list():
    """Tasks with priority are routed to the priority queue, not BackgroundTasks.tasks."""
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work, priority=5)

    # Priority tasks bypass Starlette's task list entirely.
    assert len(managed.tasks) == 0


def test_no_priority_task_added_to_starlette_list():
    """Tasks without priority go through the normal Starlette mechanism."""
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work)

    assert len(managed.tasks) == 1


def test_mixed_priority_and_normal_tasks_routed_correctly():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work)  # normal
    managed.add_task(work, priority=3)  # priority queue
    managed.add_task(work)  # normal

    # Only the two normal tasks land in the Starlette list.
    assert len(managed.tasks) == 2


# ---------------------------------------------------------------------------
# Dispatch ordering — higher priority dequeued first
# ---------------------------------------------------------------------------


async def test_priority_ordering():
    """Higher priority items are dequeued before lower priority ones."""
    tm = TaskManager()
    dispatch_order: list[int] = []

    # Use an event to block execution until all tasks are queued, so ordering
    # is determined by priority and not by timing.
    gate = asyncio.Event()

    async def work(p: int) -> None:
        await gate.wait()
        dispatch_order.append(p)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work, 1, priority=1)
        managed.add_task(work, 10, priority=10)
        managed.add_task(work, 5, priority=5)

        # Let the worker dispatch all three tasks as asyncio Tasks.
        await asyncio.sleep(0.05)

        # Release and wait for all three to complete.
        gate.set()
        await asyncio.sleep(0.1)
    finally:
        await tm.shutdown()

    # All three tasks must have run.
    assert sorted(dispatch_order) == [1, 5, 10]
    # Highest priority runs first.
    assert dispatch_order[0] == 10


async def test_equal_priority_fifo_ordering():
    """Tasks with identical priority execute in arrival (FIFO) order."""
    tm = TaskManager()
    dispatch_order: list[int] = []
    gate = asyncio.Event()

    async def work(seq: int) -> None:
        await gate.wait()
        dispatch_order.append(seq)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        for i in range(5):
            managed.add_task(work, i, priority=5)

        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.1)
    finally:
        await tm.shutdown()

    assert dispatch_order == list(range(5))


async def test_priority_queue_runs_tasks_to_success():
    """Priority-routed tasks complete and their store records reach SUCCESS."""
    tm = TaskManager()
    results: list[str] = []

    async def work(name: str) -> None:
        results.append(name)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        id_high = managed.add_task(work, "high", priority=9)
        id_low = managed.add_task(work, "low", priority=1)

        await asyncio.sleep(0.15)
    finally:
        await tm.shutdown()

    assert tm.store.get(id_high).status == TaskStatus.SUCCESS
    assert tm.store.get(id_low).status == TaskStatus.SUCCESS
    assert set(results) == {"high", "low"}


# ---------------------------------------------------------------------------
# to_dict() serialisation
# ---------------------------------------------------------------------------


def test_task_record_to_dict_includes_priority():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work, priority=6)

    d = tm.store.get(task_id).to_dict()
    assert "priority" in d
    assert d["priority"] == 6


def test_task_record_to_dict_priority_none_for_normal_task():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work)

    d = tm.store.get(task_id).to_dict()
    assert d["priority"] is None
