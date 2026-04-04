"""Tests for the htmx + SSE dashboard routes."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_taskflow import TaskAdmin, TaskManager
from fastapi_taskflow.models import TaskStatus


def _build_app(path: str = "/tasks") -> tuple[FastAPI, TaskManager]:
    tm = TaskManager()
    app = FastAPI()
    TaskAdmin(app, tm, path=path)

    @tm.task()
    def work() -> None:
        pass

    @app.post("/run")
    def run(tasks=Depends(tm.get_tasks)):
        return {"task_id": tasks.add_task(work)}

    return app, tm


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------


def test_dashboard_returns_html():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/tasks/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_dashboard_uses_event_source():
    app, _ = _build_app()
    with TestClient(app) as client:
        body = client.get("/tasks/dashboard").text
    assert "EventSource" in body
    assert "Geist" in body  # Vercel font


def test_dashboard_uses_event_source_not_polling():
    app, _ = _build_app()
    with TestClient(app) as client:
        body = client.get("/tasks/dashboard").text
    assert "EventSource" in body
    assert "every 2s" not in body
    assert "every 5s" not in body
    assert "setInterval" not in body


def test_dashboard_references_stream_url():
    app, _ = _build_app()
    with TestClient(app) as client:
        body = client.get("/tasks/dashboard").text
    assert "/tasks/dashboard/stream" in body


def test_dashboard_renders_client_side():
    app, _ = _build_app()
    with TestClient(app) as client:
        body = client.get("/tasks/dashboard").text
    # Rendering is driven by JS — no server-side swap attributes
    assert "renderMetrics" in body
    assert "renderTable" in body
    assert "hx-get" not in body


def test_dashboard_custom_path():
    app, _ = _build_app(path="/admin/tasks")
    with TestClient(app) as client:
        resp = client.get("/admin/tasks/dashboard")
        body = resp.text
    assert resp.status_code == 200
    assert "/admin/tasks/dashboard/stream" in body
    assert "renderMetrics" in body
    assert "renderTable" in body


# ---------------------------------------------------------------------------
# SSE stream endpoint
# ---------------------------------------------------------------------------


def test_stream_route_registered():
    # Verify the SSE route exists without hitting it (hitting it blocks
    # indefinitely because the SSE generator never finishes).
    from fastapi.routing import APIRoute

    app, _ = _build_app()
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/tasks/dashboard/stream" in paths


def test_stream_response_headers_encoded_in_route():
    from fastapi import Request
    from unittest.mock import AsyncMock, MagicMock

    import asyncio

    from fastapi_taskflow.dashboard import create_dashboard_router

    tm = TaskManager()
    router = create_dashboard_router(tm, prefix="/tasks")
    stream_route = next(r for r in router.routes if r.path == "/tasks/dashboard/stream")

    mock_request = MagicMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=True)

    response = asyncio.get_event_loop().run_until_complete(
        stream_route.endpoint(mock_request)
    )

    assert response.media_type == "text/event-stream"
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("x-accel-buffering") == "no"


async def test_sse_generator_pushes_json_state():
    import json
    from fastapi import Request
    from unittest.mock import AsyncMock, MagicMock

    from fastapi_taskflow.dashboard import _sse_generator

    _, tm = _build_app()
    mock_req = MagicMock(spec=Request)
    mock_req.is_disconnected = AsyncMock(return_value=True)

    events = []
    async for event in _sse_generator(tm, mock_req):
        events.append(event)

    # One state event pushed immediately, then disconnect detected
    assert len(events) == 1
    assert events[0].startswith("event: state\ndata: ")

    payload = json.loads(events[0].split("data: ", 1)[1].strip())
    assert "tasks" in payload
    assert "metrics" in payload
    assert isinstance(payload["tasks"], list)
    assert "total" in payload["metrics"]


def test_stream_excluded_from_openapi():
    app, _ = _build_app()
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    assert "/tasks/dashboard/stream" not in schema.get("paths", {})


# ---------------------------------------------------------------------------
# Store subscriber mechanism
# ---------------------------------------------------------------------------


async def test_store_notifies_subscriber_on_update():
    from fastapi_taskflow.store import TaskStore

    store = TaskStore()
    store.create("t1", "func", (), {})

    q = store.add_subscriber()
    store.update("t1", status=TaskStatus.SUCCESS)

    # The queue should have one notification
    item = q.get_nowait()
    assert item == "change"


async def test_store_coalesces_rapid_changes():
    from fastapi_taskflow.store import TaskStore

    store = TaskStore()
    store.create("t1", "func", (), {})

    q = store.add_subscriber()
    # Three rapid updates — queue maxsize=1 means only one notification
    store.update("t1", status=TaskStatus.RUNNING)
    store.update("t1", status=TaskStatus.SUCCESS)
    store.update("t1", retries_used=1)

    assert q.qsize() == 1


async def test_store_remove_subscriber_stops_notifications():
    from fastapi_taskflow.store import TaskStore

    store = TaskStore()
    store.create("t1", "func", (), {})

    q = store.add_subscriber()
    store.remove_subscriber(q)
    store.update("t1", status=TaskStatus.SUCCESS)

    assert q.empty()


# ---------------------------------------------------------------------------
# Metrics fragment
# ---------------------------------------------------------------------------


def test_metrics_fragment_returns_html():
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.get("/tasks/dashboard/metrics")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_metrics_fragment_shows_counts():
    app, tm = _build_app()
    tm.store.create("t1", "work", (), {})
    tm.store.update("t1", status=TaskStatus.SUCCESS)
    tm.store.create("t2", "work", (), {})
    tm.store.update("t2", status=TaskStatus.FAILED)

    with TestClient(app) as client:
        body = client.get("/tasks/dashboard/metrics").text

    assert "2" in body
    assert "1" in body


# ---------------------------------------------------------------------------
# Tasks fragment
# ---------------------------------------------------------------------------


def test_tasks_fragment_empty_state():
    app, _ = _build_app()
    with TestClient(app) as client:
        body = client.get("/tasks/dashboard/tasks").text
    assert "No tasks recorded yet" in body


def test_tasks_fragment_shows_task():
    app, tm = _build_app()
    tm.store.create("abc-123", "send_email", (), {})
    tm.store.update("abc-123", status=TaskStatus.RUNNING)

    with TestClient(app) as client:
        body = client.get("/tasks/dashboard/tasks").text

    assert "send_email" in body
    assert "running" in body
    assert "abc-123"[:8] in body


def test_tasks_fragment_escapes_html_in_error():
    app, tm = _build_app()
    tm.store.create("t1", "func", (), {})
    tm.store.update("t1", status=TaskStatus.FAILED, error="<script>alert(1)</script>")

    with TestClient(app) as client:
        body = client.get("/tasks/dashboard/tasks").text

    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# Excluded from OpenAPI schema
# ---------------------------------------------------------------------------


def test_dashboard_routes_excluded_from_openapi():
    app, _ = _build_app()
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    paths = schema.get("paths", {})
    assert "/tasks/dashboard" not in paths
    assert "/tasks/dashboard/tasks" not in paths
    assert "/tasks/dashboard/metrics" not in paths
