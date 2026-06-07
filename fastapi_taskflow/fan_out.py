"""HTTP fan-out to peer instances for multi-instance task aggregation.

Each peer exposes ``GET {tasks_prefix}/__peer/tasks`` which returns its
local in-memory task records as a JSON array. :func:`gather_peer_records`
calls all live peers concurrently, parses the responses into
:class:`~fastapi_taskflow.models.TaskRecord` objects, and returns the union.

Calls are made using :mod:`urllib.request` wrapped in
:func:`asyncio.to_thread` so no additional HTTP client dependency is needed.
Any peer that is unreachable or returns a bad response is silently skipped
and logged at WARNING level.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .instance_registry import InstanceRegistry
    from .models import TaskRecord

_log = logging.getLogger(__name__)

_PEER_ENDPOINT = "/__peer/tasks"


def _http_get(url: str, timeout: int) -> Optional[bytes]:
    """Perform a synchronous GET and return the response body, or None on error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except Exception as exc:
        _log.warning("fastapi-taskflow: fan-out GET %s failed: %s", url, exc)
        return None


async def fetch_peer_tasks(
    peer_url: str,
    tasks_prefix: str,
    timeout: int = 5,
) -> "list[TaskRecord]":
    """Fetch and deserialise all task records from one peer instance.

    Calls ``GET {peer_url}{tasks_prefix}/__peer/tasks`` in a thread so the
    event loop is not blocked.

    Args:
        peer_url: Base URL of the peer, e.g. ``"http://10.0.0.2:8000"``.
        tasks_prefix: Tasks router prefix on the peer, e.g. ``"/api/tasks"``.
        timeout: Request timeout in seconds.

    Returns:
        Parsed :class:`~fastapi_taskflow.models.TaskRecord` list, empty on
        any error.
    """
    from .models import TaskRecord

    url = peer_url.rstrip("/") + tasks_prefix.rstrip("/") + _PEER_ENDPOINT
    raw = await asyncio.to_thread(_http_get, url, timeout)
    if raw is None:
        return []

    try:
        records_raw: list[dict] = json.loads(raw)
    except Exception:
        _log.warning("fastapi-taskflow: could not parse peer response from %s", url)
        return []

    records: list[TaskRecord] = []
    for item in records_raw:
        try:
            records.append(TaskRecord.from_dict(item))
        except Exception:
            pass
    return records


async def gather_peer_records(
    registry: "InstanceRegistry",
    timeout: int = 5,
) -> "list[TaskRecord]":
    """Fan out to all live peers and return the combined task record list.

    Peers are discovered via the registry. All calls are made concurrently.
    Peers that fail are skipped without raising.

    Args:
        registry: The local :class:`~fastapi_taskflow.instance_registry.InstanceRegistry`
            used to discover peers.
        timeout: Per-peer HTTP request timeout in seconds.

    Returns:
        Combined list of :class:`~fastapi_taskflow.models.TaskRecord` objects
        from all reachable peers.
    """
    peers = await registry.peers()
    if not peers:
        return []

    results = await asyncio.gather(
        *[fetch_peer_tasks(p["url"], p["tasks_prefix"], timeout) for p in peers],
        return_exceptions=True,
    )

    combined: list["TaskRecord"] = []
    for r in results:
        if isinstance(r, list):
            combined.extend(r)
        elif isinstance(r, Exception):
            _log.warning("fastapi-taskflow: peer fan-out raised: %s", r)
    return combined
