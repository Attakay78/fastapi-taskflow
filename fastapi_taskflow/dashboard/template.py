from __future__ import annotations
import json
from typing import TYPE_CHECKING

from .css import DASHBOARD_CSS
from .html import DASHBOARD_HTML
from .js import DASHBOARD_JS

if TYPE_CHECKING:
    pass

_DASHBOARD_TEMPLATE = DASHBOARD_HTML.replace("__CSS_BLOCK__", DASHBOARD_CSS).replace(
    "__JS_BLOCK__", DASHBOARD_JS
)


def _serialize_registry(registry) -> str:
    """Serialize registered task functions and their configs as a JSON array."""
    result = []
    for name, func in registry._by_name.items():
        config = registry._by_id.get(id(func))
        if config is None:
            continue
        result.append(
            {
                "name": name,
                "retries": config.retries,
                "delay": config.delay,
                "backoff": config.backoff,
                "persist": config.persist,
                "requeue_on_interrupt": config.requeue_on_interrupt,
                "eager": config.eager,
                "priority": config.priority,
            }
        )
    result.sort(key=lambda x: x["name"])
    return json.dumps(result)


def _dashboard_page(
    base_path: str,
    show_args: bool = False,
    logout_url: str | None = None,
    title: str = "fastapi-taskflow",
    registered_tasks: str = "[]",
    show_audit: bool = False,
) -> str:
    stream_url = f"{base_path}/dashboard/stream"
    logout_btn = (
        f'<a href="{logout_url}" class="logout-btn">Sign out</a>' if logout_url else ""
    )
    return (
        _DASHBOARD_TEMPLATE.replace("__STREAM_URL__", stream_url)
        .replace("__TASKS_PREFIX__", base_path)
        .replace("__SHOW_ARGS__", "true" if show_args else "false")
        .replace("__SHOW_AUDIT__", "true" if show_audit else "false")
        .replace("__LOGOUT_BUTTON__", logout_btn)
        .replace("__TITLE__", title)
        .replace("__REGISTERED_TASKS__", registered_tasks)
    )
