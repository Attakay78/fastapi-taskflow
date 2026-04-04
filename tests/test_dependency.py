"""Tests for task_manager.get_tasks dependency and TaskAdmin lifecycle."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_taskflow import TaskAdmin, TaskManager


def _build_app(task_manager: TaskManager, path: str = "/tasks") -> FastAPI:
    app = FastAPI()
    TaskAdmin(app, task_manager, path=path)

    @task_manager.task()
    def work(x: int) -> int:
        return x * 2

    @app.post("/run")
    def run(x: int, tasks=Depends(task_manager.get_tasks)):
        task_id = tasks.add_task(work, x)
        return {"task_id": task_id}

    return app


# ---------------------------------------------------------------------------
# get_tasks dependency
# ---------------------------------------------------------------------------


def test_dependency_returns_task_id():
    tm = TaskManager()
    app = _build_app(tm)
    with TestClient(app) as client:
        resp = client.post("/run?x=5")
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert len(task_id) == 36  # UUID4


def test_dependency_task_appears_in_store():
    tm = TaskManager()
    app = _build_app(tm)
    with TestClient(app) as client:
        resp = client.post("/run?x=5")
        task_id = resp.json()["task_id"]
        detail = client.get(f"/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["func_name"] == "work"


def test_dependency_each_request_gets_unique_id():
    tm = TaskManager()
    app = _build_app(tm)
    with TestClient(app) as client:
        ids = {client.post("/run?x=1").json()["task_id"] for _ in range(5)}
    assert len(ids) == 5


# ---------------------------------------------------------------------------
# TaskAdmin — custom path
# ---------------------------------------------------------------------------


def test_custom_path_is_used():
    tm = TaskManager()
    app = _build_app(tm, path="/admin/tasks")
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 404
        assert client.get("/admin/tasks").status_code == 200


def test_default_path_is_tasks():
    tm = TaskManager()
    app = _build_app(tm)
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 200


# ---------------------------------------------------------------------------
# Auto-snapshot lifecycle
# ---------------------------------------------------------------------------


def test_snapshot_scheduler_starts_and_stops(tmp_path):
    tm = TaskManager(snapshot_db=str(tmp_path / "tasks.db"), snapshot_interval=9999)
    app = _build_app(tm)

    with TestClient(app):
        assert tm._scheduler._bg_task is not None

    # Shutdown ran — bg_task should be cancelled
    assert tm._scheduler._bg_task is None


async def test_snapshot_flush_on_shutdown(tmp_path):
    db = str(tmp_path / "tasks.db")
    tm = TaskManager(snapshot_db=db, snapshot_interval=9999)
    app = _build_app(tm)

    with TestClient(app) as client:
        client.post("/run?x=1")
        client.post("/run?x=2")

    records = tm._scheduler.query()
    assert len(records) == 2


def test_no_scheduler_without_snapshot_db():
    tm = TaskManager()
    assert tm._scheduler is None
    app = _build_app(tm)
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 200


async def test_dashboard_shows_data_from_previous_run(tmp_path):
    db = str(tmp_path / "tasks.db")

    # Simulate first run: enqueue + complete a task, then flush on shutdown
    tm1 = TaskManager(snapshot_db=db, snapshot_interval=9999)
    app1 = _build_app(tm1)
    with TestClient(app1) as client:
        client.post("/run?x=1")
        client.post("/run?x=2")
    # TestClient.__exit__ triggers shutdown → flush is called

    # Simulate restart: fresh TaskManager with the same DB
    tm2 = TaskManager(snapshot_db=db, snapshot_interval=9999)
    app2 = _build_app(tm2)
    with TestClient(app2) as client:
        tasks = client.get("/tasks").json()

    assert len(tasks) == 2


# ---------------------------------------------------------------------------
# background_tasks property alias + install()
# ---------------------------------------------------------------------------


def test_background_tasks_property_alias():
    tm = TaskManager()
    app = FastAPI()
    TaskAdmin(app, tm)

    @tm.task()
    def work(x: int) -> int:
        return x * 2

    @app.post("/run-alias")
    def run_alias(x: int, background_tasks=Depends(tm.background_tasks)):
        task_id = background_tasks.add_task(work, x)
        return {"task_id": task_id}

    with TestClient(app) as client:
        resp = client.post("/run-alias?x=5")
    assert resp.status_code == 200
    assert len(resp.json()["task_id"]) == 36


def test_install_transparent_injection(monkeypatch):
    """After install(), bare BackgroundTasks annotation injects ManagedBackgroundTasks."""
    import fastapi.dependencies.utils as _fdu

    from fastapi import BackgroundTasks

    from fastapi_taskflow import ManagedBackgroundTasks

    # Register the original with monkeypatch so pytest restores it after this test.
    monkeypatch.setattr(_fdu, "BackgroundTasks", _fdu.BackgroundTasks)

    tm = TaskManager()
    app = FastAPI()
    TaskAdmin(app, tm)
    tm.install(app)

    @tm.task()
    def work() -> None:
        pass

    injected: list = []

    @app.post("/run-bare")
    def run_bare(background_tasks: BackgroundTasks):
        injected.append(background_tasks)
        task_id = background_tasks.add_task(work)
        return {"task_id": task_id}

    with TestClient(app) as client:
        resp = client.post("/run-bare")

    assert resp.status_code == 200
    assert len(resp.json()["task_id"]) == 36
    assert isinstance(injected[0], ManagedBackgroundTasks)
