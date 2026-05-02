from __future__ import annotations
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ..auth import verify_token, COOKIE_NAME
from .helpers import _render_metrics, _render_task_rows
from .sse import _sse_generator
from .template import _dashboard_page, _serialize_registry

if TYPE_CHECKING:
    from ..manager import TaskManager


def create_dashboard_router(
    task_manager: "TaskManager",
    prefix: str,
    display_func_args: bool = False,
    secret_key: str | None = None,
    login_path: str | None = None,
    poll_interval: float = 30.0,
    title: str = "fastapi-taskflow",
) -> APIRouter:
    from fastapi.responses import RedirectResponse as _Redirect

    router = APIRouter(
        prefix=f"{prefix}/dashboard",
        include_in_schema=False,
    )

    logout_url = f"{prefix}/auth/logout" if secret_key else None

    def _check_cookie(request: Request):
        """Return True if authenticated (or no auth configured)."""
        if secret_key is None:
            return True
        return verify_token(secret_key, request.cookies.get(COOKIE_NAME, ""))

    @router.get("", response_class=HTMLResponse)
    def dashboard_page(request: Request):
        if not _check_cookie(request):
            assert login_path is not None
            return _Redirect(url=login_path, status_code=302)
        return HTMLResponse(
            _dashboard_page(
                prefix,
                show_args=display_func_args,
                logout_url=logout_url,
                title=title,
                registered_tasks=_serialize_registry(task_manager.registry),
                show_audit=secret_key is not None,
            )
        )

    @router.get("/stream")
    async def event_stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _sse_generator(
                task_manager,
                request,
                include_args=display_func_args,
                poll_interval=poll_interval,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    @router.get("/metrics", response_class=HTMLResponse)
    async def metrics_fragment(request: Request) -> HTMLResponse:
        if not _check_cookie(request):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Unauthorized")
        tasks = await task_manager.merged_list()
        return HTMLResponse(_render_metrics(tasks))

    @router.get("/tasks", response_class=HTMLResponse)
    async def tasks_fragment(request: Request) -> HTMLResponse:
        if not _check_cookie(request):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Unauthorized")
        tasks = await task_manager.merged_list()
        return HTMLResponse(_render_task_rows(tasks))

    return router
