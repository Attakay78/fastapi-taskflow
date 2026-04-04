from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_taskflow import TaskAdmin, TaskManager


def _build_app() -> tuple[FastAPI, TaskManager]:
    tm = TaskManager()
    app = FastAPI()
    TaskAdmin(app, tm)

    @tm.task()
    def dummy(x: int) -> None:
        pass

    @app.post("/run")
    def run(tasks=Depends(tm.get_tasks)):
        task_id = tasks.add_task(dummy, 1)
        return {"task_id": task_id}

    return app, tm


# ---------------------------------------------------------------------------
# GET /tasks
# ---------------------------------------------------------------------------


def test_list_tasks_empty():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_tasks_after_enqueue():
    app, _ = _build_app()
    with TestClient(app) as client:
        client.post("/run")
        resp = client.get("/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# GET /tasks/metrics
# ---------------------------------------------------------------------------


def test_metrics_empty():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/tasks/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["success_rate"] == 0.0


def test_metrics_after_tasks():
    app, _ = _build_app()
    with TestClient(app) as client:
        client.post("/run")
        client.post("/run")
        resp = client.get("/tasks/metrics")
    data = resp.json()
    assert data["total"] == 2


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}
# ---------------------------------------------------------------------------


def test_get_task_not_found():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/tasks/does-not-exist")
    assert resp.status_code == 404


def test_get_task_by_id():
    app, _ = _build_app()
    with TestClient(app) as client:
        post_resp = client.post("/run")
        task_id = post_resp.json()["task_id"]
        resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == task_id


def test_metrics_route_not_shadowed_by_task_id_route():
    """Ensure /tasks/metrics resolves before /tasks/{task_id}."""
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/tasks/metrics")
    assert resp.status_code == 200
    assert "total" in resp.json()
