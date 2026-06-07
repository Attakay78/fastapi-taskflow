"""Tests for named queue dispatch, backpressure, concurrency, and config."""

import asyncio

import pytest

from fastapi_taskflow import ManagedBackgroundTasks, QueueFullError, TaskManager
from fastapi_taskflow.models import QueueConfig, TaskStatus


# ---------------------------------------------------------------------------
# Store and config — no event loop required
# ---------------------------------------------------------------------------


def test_queue_field_defaults_to_default():
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.queue == "default"


def test_queue_stored_on_record_when_queue_system_active():
    tm = TaskManager(max_size=100)

    @tm.task(queue="email")
    def send_email():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(send_email)

    record = tm.store.get(task_id)
    assert record is not None
    assert record.queue == "email"


def test_per_call_queue_overrides_decorator_queue():
    tm = TaskManager(queues={"reports": QueueConfig(), "email": QueueConfig()})

    @tm.task(queue="email")
    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work, queue="reports")

    record = tm.store.get(task_id)
    assert record.queue == "reports"


def test_decorator_queue_stored_in_config():
    tm = TaskManager(queues={"jobs": QueueConfig()})

    @tm.task(queue="jobs")
    def work():
        pass

    config = tm.registry.get_config(work)
    assert config is not None
    assert config.queue == "jobs"


def test_queue_field_in_to_dict():
    tm = TaskManager(queues={"notifications": QueueConfig()})

    @tm.task(queue="notifications")
    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    task_id = managed.add_task(work)

    d = tm.store.get(task_id).to_dict()
    assert "queue" in d
    assert d["queue"] == "notifications"


def test_no_named_queue_system_uses_starlette_list():
    """Without queues/max_size, tasks still go through Starlette BackgroundTasks."""
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work)
    managed.add_task(work)

    assert len(managed.tasks) == 2


async def test_named_queue_system_bypasses_starlette_list():
    """When named queues are active, tasks do not appear in Starlette's task list."""
    tm = TaskManager(queues={"default": QueueConfig()})

    def work():
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work)
        managed.add_task(work)

        assert len(managed.tasks) == 0
    finally:
        await tm.shutdown()


async def test_max_size_only_activates_named_queue_system():
    """Passing only max_size (no queues dict) still activates the named queue system."""
    tm = TaskManager(max_size=10)

    def work():
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        task_id = managed.add_task(work)

        assert len(managed.tasks) == 0
        assert tm.store.get(task_id).queue == "default"
    finally:
        await tm.shutdown()


def test_queue_config_stored_correctly():
    tm = TaskManager(
        queues={
            "fast": QueueConfig(concurrency=10, max_size=200),
            "slow": QueueConfig(concurrency=2),
        }
    )

    assert "fast" in tm._queue_configs
    assert tm._queue_configs["fast"].concurrency == 10
    assert tm._queue_configs["fast"].max_size == 200
    assert tm._queue_configs["slow"].concurrency == 2
    assert tm._queue_configs["slow"].max_size is None


def test_default_queue_auto_created_when_missing():
    tm = TaskManager(queues={"email": QueueConfig(concurrency=5)})

    assert "default" in tm._queue_configs
    assert "email" in tm._queue_configs


def test_default_queue_inherits_max_size_and_concurrency():
    tm = TaskManager(max_concurrent_tasks=8, max_size=50)

    cfg = tm._queue_configs.get("default")
    assert cfg is not None
    assert cfg.concurrency == 8
    assert cfg.max_size == 50


# ---------------------------------------------------------------------------
# Backpressure — QueueFullError
# ---------------------------------------------------------------------------


async def test_queue_full_error_raised_when_max_size_exceeded():
    tm = TaskManager(queues={"tight": QueueConfig(max_size=2)})

    def work():
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work, queue="tight")
        managed.add_task(work, queue="tight")

        with pytest.raises(QueueFullError):
            managed.add_task(work, queue="tight")
    finally:
        await tm.shutdown()


async def test_queue_full_error_message_includes_queue_name():
    tm = TaskManager(queues={"exports": QueueConfig(max_size=1)})

    def work():
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work, queue="exports")

        with pytest.raises(QueueFullError, match="exports"):
            managed.add_task(work, queue="exports")
    finally:
        await tm.shutdown()


async def test_max_size_on_default_queue():
    tm = TaskManager(max_size=1)

    def work():
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work)

        with pytest.raises(QueueFullError):
            managed.add_task(work)
    finally:
        await tm.shutdown()


# ---------------------------------------------------------------------------
# Task execution — tasks complete successfully via named queues
# ---------------------------------------------------------------------------


async def test_named_queue_task_reaches_success():
    tm = TaskManager(queues={"jobs": QueueConfig()})
    results: list[str] = []

    @tm.task(queue="jobs")
    async def work(val: str) -> None:
        results.append(val)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        task_id = managed.add_task(work, "hello")

        await asyncio.sleep(0.15)
    finally:
        await tm.shutdown()

    assert results == ["hello"]
    assert tm.store.get(task_id).status == TaskStatus.SUCCESS


async def test_multiple_named_queues_run_independently():
    tm = TaskManager(
        queues={
            "alpha": QueueConfig(),
            "beta": QueueConfig(),
        }
    )
    alpha_results: list[str] = []
    beta_results: list[str] = []

    @tm.task(queue="alpha")
    async def alpha_work(val: str) -> None:
        alpha_results.append(val)

    @tm.task(queue="beta")
    async def beta_work(val: str) -> None:
        beta_results.append(val)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(alpha_work, "a1")
        managed.add_task(alpha_work, "a2")
        managed.add_task(beta_work, "b1")

        await asyncio.sleep(0.2)
    finally:
        await tm.shutdown()

    assert set(alpha_results) == {"a1", "a2"}
    assert beta_results == ["b1"]


async def test_per_call_queue_routes_task_correctly():
    """Specifying queue= on add_task() overrides any decorator-level default."""
    tm = TaskManager(queues={"primary": QueueConfig(), "secondary": QueueConfig()})

    @tm.task(queue="primary")
    async def work() -> None:
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        tid = managed.add_task(work, queue="secondary")

        await asyncio.sleep(0.15)
    finally:
        await tm.shutdown()

    record = tm.store.get(tid)
    assert record.queue == "secondary"
    assert record.status == TaskStatus.SUCCESS


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------


async def test_concurrency_limit_caps_simultaneous_tasks():
    """At most concurrency= tasks run at once inside a named queue."""
    tm = TaskManager(queues={"capped": QueueConfig(concurrency=2)})

    max_concurrent = 0
    active = 0
    gate = asyncio.Event()

    async def work() -> None:
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        await gate.wait()
        active -= 1

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        for _ in range(5):
            managed.add_task(work, queue="capped")

        # Give the drainer time to dispatch up to the concurrency cap.
        await asyncio.sleep(0.1)
        assert max_concurrent <= 2

        gate.set()
        await asyncio.sleep(0.2)
    finally:
        await tm.shutdown()


# ---------------------------------------------------------------------------
# Priority ordering within a named queue
# ---------------------------------------------------------------------------


async def test_named_queue_respects_priority_ordering():
    """Higher-priority tasks are drained first within a named queue."""
    tm = TaskManager(queues={"work": QueueConfig(concurrency=1)})
    order: list[int] = []
    gate = asyncio.Event()

    async def work(p: int) -> None:
        await gate.wait()
        order.append(p)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)

        # Enqueue with concurrency=1 so we can control drain order via the gate.
        # The first task immediately acquires the semaphore; the remaining three
        # sit in the heap and will be drained in priority order.
        managed.add_task(work, 1, queue="work", priority=1)
        managed.add_task(work, 10, queue="work", priority=10)
        managed.add_task(work, 5, queue="work", priority=5)

        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.3)
    finally:
        await tm.shutdown()

    assert sorted(order) == [1, 5, 10]
    # Highest priority (10) must have run before lowest (1).
    assert order.index(10) < order.index(1)


# ---------------------------------------------------------------------------
# queue_stats()
# ---------------------------------------------------------------------------


async def test_queue_stats_returns_all_queues():
    tm = TaskManager(
        queues={
            "alpha": QueueConfig(concurrency=5, max_size=100),
            "beta": QueueConfig(),
        }
    )

    await tm.startup()
    try:
        stats = tm.queue_stats()
    finally:
        await tm.shutdown()

    names = {s["name"] for s in stats}
    assert "alpha" in names
    assert "beta" in names
    assert "default" in names


async def test_queue_stats_reflects_config():
    tm = TaskManager(queues={"q": QueueConfig(concurrency=3, max_size=25)})

    await tm.startup()
    try:
        stats = {s["name"]: s for s in tm.queue_stats()}
    finally:
        await tm.shutdown()

    q = stats["q"]
    assert q["concurrency"] == 3
    assert q["max_size"] == 25


async def test_queue_stats_finished_count():
    tm = TaskManager(queues={"jobs": QueueConfig()})
    results: list[int] = []

    @tm.task(queue="jobs")
    async def work(n: int) -> None:
        results.append(n)

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work, 1)
        managed.add_task(work, 2)

        await asyncio.sleep(0.2)

        stats = {s["name"]: s for s in tm.queue_stats()}
    finally:
        await tm.shutdown()

    assert stats["jobs"]["finished"] == 2
    assert stats["jobs"]["running"] == 0


# ---------------------------------------------------------------------------
# Unknown queue fallback
# ---------------------------------------------------------------------------


async def test_unknown_queue_falls_back_to_default(caplog):
    tm = TaskManager(queues={"default": QueueConfig()})
    results: list[str] = []

    async def work(val: str) -> None:
        results.append(val)

    await tm.startup()
    try:
        import logging

        with caplog.at_level(logging.WARNING):
            managed = ManagedBackgroundTasks(tm)
            # "ghost" is not a configured queue — should fall back to default.
            managed.add_task(work, "x", queue="ghost")

        await asyncio.sleep(0.15)
    finally:
        await tm.shutdown()

    assert results == ["x"]
    assert any("ghost" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# update_queue_config()
# ---------------------------------------------------------------------------


async def test_update_queue_config_changes_max_size():
    tm = TaskManager(queues={"q": QueueConfig(max_size=1)})

    def work():
        pass

    await tm.startup()
    try:
        managed = ManagedBackgroundTasks(tm)
        managed.add_task(work, queue="q")

        with pytest.raises(QueueFullError):
            managed.add_task(work, queue="q")

        # Raise the limit and confirm a second task is now accepted.
        tm.update_queue_config("q", concurrency=None, max_size=10)
        managed.add_task(work, queue="q")
    finally:
        await tm.shutdown()


async def test_update_queue_config_raises_for_unknown_queue():
    tm = TaskManager(queues={"q": QueueConfig()})

    await tm.startup()
    try:
        with pytest.raises(KeyError):
            tm.update_queue_config("nonexistent", concurrency=5, max_size=None)
    finally:
        await tm.shutdown()


async def test_update_queue_config_returns_stats():
    tm = TaskManager(queues={"q": QueueConfig(concurrency=2, max_size=50)})

    await tm.startup()
    try:
        result = tm.update_queue_config("q", concurrency=5, max_size=100)
    finally:
        await tm.shutdown()

    assert result["name"] == "q"
    assert result["concurrency"] == 5
    assert result["max_size"] == 100


# ---------------------------------------------------------------------------
# Backward compatibility — legacy mode unchanged when no queues are configured
# ---------------------------------------------------------------------------


def test_legacy_mode_uses_starlette_list_without_priority():
    """Without queues or max_size, plain tasks go through Starlette."""
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work)

    assert len(managed.tasks) == 1
    assert not tm._queues  # no named queues created before startup


def test_legacy_mode_priority_bypasses_starlette_list():
    """Priority tasks in legacy mode bypass Starlette, going to the priority queue."""
    tm = TaskManager()

    def work():
        pass

    managed = ManagedBackgroundTasks(tm)
    managed.add_task(work, priority=5)

    assert len(managed.tasks) == 0
