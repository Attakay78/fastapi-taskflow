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


def test_metrics_includes_interrupted():
    app, tm = _build_app()
    tm.store.create("x1", "dummy", (1,), {})
    from fastapi_taskflow.models import TaskStatus

    tm.store.update("x1", status=TaskStatus.INTERRUPTED)
    with TestClient(app) as client:
        data = client.get("/tasks/metrics").json()
    assert data["interrupted"] == 1


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/retry
# ---------------------------------------------------------------------------


def test_retry_failed_task():
    app, tm = _build_app()
    from fastapi_taskflow.models import TaskStatus

    tm.store.create("r1", "dummy", (1,), {})
    tm.store.update("r1", status=TaskStatus.FAILED, error="boom")

    with TestClient(app) as client:
        resp = client.post("/tasks/r1/retry")

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] != "r1"
    assert body["task"]["func_name"] == "dummy"


def test_retry_interrupted_task():
    app, tm = _build_app()
    from fastapi_taskflow.models import TaskStatus

    tm.store.create("r2", "dummy", (1,), {})
    tm.store.update("r2", status=TaskStatus.INTERRUPTED)

    with TestClient(app) as client:
        resp = client.post("/tasks/r2/retry")

    assert resp.status_code == 200
    assert resp.json()["task_id"] != "r2"


def test_retry_pending_task_returns_400():
    app, _ = _build_app()
    with TestClient(app) as client:
        post_resp = client.post("/run")
        task_id = post_resp.json()["task_id"]
        resp = client.post(f"/tasks/{task_id}/retry")
    assert resp.status_code == 400


def test_retry_not_found_returns_404():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.post("/tasks/no-such-task/retry")
    assert resp.status_code == 404


def test_retry_unregistered_function_returns_409():
    app, tm = _build_app()
    from fastapi_taskflow.models import TaskStatus

    # Create a task for a function that is not in the registry
    tm.store.create("r3", "ghost_func", (), {})
    tm.store.update("r3", status=TaskStatus.FAILED)

    with TestClient(app) as client:
        resp = client.post("/tasks/r3/retry")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /tasks/history
# ---------------------------------------------------------------------------


def test_delete_history_removes_old_completed_tasks():
    from datetime import datetime, timezone, timedelta
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    old_end = datetime.now(timezone.utc) - timedelta(hours=2)
    tm.store.create("old1", "dummy", (1,), {})
    tm.store.update("old1", status=TaskStatus.SUCCESS, end_time=old_end)

    with TestClient(app) as client:
        resp = client.delete("/tasks/history?value=1&unit=hour")

    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == 1
    assert data["store"] == 1
    assert data["backend"] == 0
    assert tm.store.get("old1") is None


def test_delete_history_keeps_recent_tasks():
    from datetime import datetime, timezone, timedelta
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    recent_end = datetime.now(timezone.utc) - timedelta(minutes=10)
    tm.store.create("new1", "dummy", (1,), {})
    tm.store.update("new1", status=TaskStatus.SUCCESS, end_time=recent_end)

    with TestClient(app) as client:
        resp = client.delete("/tasks/history?value=1&unit=hour")

    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0
    assert tm.store.get("new1") is not None


def test_delete_history_invalid_unit_returns_400():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.delete("/tasks/history?value=1&unit=week")
    assert resp.status_code == 400


def test_delete_history_missing_value_returns_422():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.delete("/tasks/history?unit=hour")
    assert resp.status_code == 422


def test_delete_history_zero_value_returns_422():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.delete("/tasks/history?value=0&unit=hour")
    assert resp.status_code == 422


def test_delete_history_skips_live_tasks():
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    tm.store.create("live1", "dummy", (1,), {})
    tm.store.update("live1", status=TaskStatus.RUNNING)

    with TestClient(app) as client:
        resp = client.delete("/tasks/history?value=1&unit=min")

    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0
    assert tm.store.get("live1") is not None


# ---------------------------------------------------------------------------
# POST /tasks/bulk-retry
# ---------------------------------------------------------------------------


def test_bulk_retry_selected_tasks():
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    tm.store.create("b1", "dummy", (1,), {})
    tm.store.update("b1", status=TaskStatus.FAILED, error="err")
    tm.store.create("b2", "dummy", (2,), {})
    tm.store.update("b2", status=TaskStatus.FAILED, error="err")

    with TestClient(app) as client:
        resp = client.post("/tasks/bulk-retry", json={"task_ids": ["b1", "b2"]})

    assert resp.status_code == 200
    data = resp.json()
    assert data["dispatched"] == 2
    assert data["skipped"] == 0
    assert len(data["results"]) == 2


def test_bulk_retry_skips_non_failed():
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    tm.store.create("b3", "dummy", (1,), {})
    tm.store.update("b3", status=TaskStatus.SUCCESS)
    tm.store.create("b4", "dummy", (2,), {})
    tm.store.update("b4", status=TaskStatus.FAILED, error="err")

    with TestClient(app) as client:
        resp = client.post("/tasks/bulk-retry", json={"task_ids": ["b3", "b4"]})

    data = resp.json()
    assert data["dispatched"] == 1
    assert data["skipped"] == 1


def test_bulk_retry_skips_unknown_ids():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.post("/tasks/bulk-retry", json={"task_ids": ["no-such-id"]})

    data = resp.json()
    assert data["dispatched"] == 0
    assert data["skipped"] == 1


def test_bulk_retry_empty_body():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.post("/tasks/bulk-retry", json={"task_ids": []})

    data = resp.json()
    assert data["dispatched"] == 0
    assert data["skipped"] == 0


# ---------------------------------------------------------------------------
# POST /tasks/retry-failed
# ---------------------------------------------------------------------------


def test_retry_failed_window_replays_in_window():
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    tm.store.create("w1", "dummy", (1,), {})
    tm.store.update("w1", status=TaskStatus.FAILED, error="err")

    with TestClient(app) as client:
        resp = client.post("/tasks/retry-failed?since=1h")

    assert resp.status_code == 200
    data = resp.json()
    assert data["dispatched"] == 1


def test_retry_failed_window_invalid_since():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.post("/tasks/retry-failed?since=bad")

    assert resp.status_code == 400


def test_retry_failed_window_func_name_filter():
    from fastapi_taskflow.models import TaskStatus

    app, tm = _build_app()
    tm.store.create("w2", "dummy", (1,), {})
    tm.store.update("w2", status=TaskStatus.FAILED, error="err")
    tm.store.create("w3", "other_func", (1,), {})
    tm.store.update("w3", status=TaskStatus.FAILED, error="err")

    with TestClient(app) as client:
        resp = client.post("/tasks/retry-failed?since=1h&func_name=dummy")

    data = resp.json()
    assert data["dispatched"] == 1
    assert data["skipped"] == 0


def test_retry_failed_window_all():
    from fastapi_taskflow.models import TaskStatus
    from datetime import datetime, timedelta, timezone

    app, tm = _build_app()
    old_time = datetime.now(timezone.utc) - timedelta(days=30)
    tm.store.create("w4", "dummy", (1,), {})
    tm.store.update("w4", status=TaskStatus.FAILED, error="err", end_time=old_time)
    record = tm.store.get("w4")
    if record:
        record.created_at = old_time

    with TestClient(app) as client:
        # 1h window should NOT include 30-day-old task
        resp1 = client.post("/tasks/retry-failed?since=1h")
        assert resp1.json()["dispatched"] == 0

        # 'all' window should include it
        resp2 = client.post("/tasks/retry-failed?since=all")
        assert resp2.json()["dispatched"] == 1
