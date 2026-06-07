"""Instance registry for multi-instance deployments.

Each running instance writes its base URL into the shared backend under the
metadata key ``_instance_registry``. Peer instances are discovered by reading
that key. A periodic heartbeat refreshes the entry so stale entries from
crashed instances are naturally excluded.

This is opt-in: nothing runs unless ``instance_url`` is passed to
``TaskManager``. The backend must implement ``save_metadata`` / ``load_metadata``
(SQLite, Redis, Postgres, and MySQL all do; the in-memory default does not).

The peer endpoint (``/__peer/tasks``) must be reachable from every other
instance over the network. In production deployments it should be firewalled
to internal traffic only, as it returns raw task records without authentication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .backends.base import SnapshotBackend

_log = logging.getLogger(__name__)

_REGISTRY_KEY = "_instance_registry"


class InstanceRegistry:
    """Maintains this instance's entry in the shared backend registry and
    provides peer discovery for fan-out aggregation.

    Args:
        backend: Shared snapshot backend used for registry storage.
        instance_url: Public base URL of this instance, e.g.
            ``"http://10.0.0.1:8000"``. Peers use this to call the internal
            tasks endpoint.
        tasks_prefix: URL prefix where the tasks router is mounted, e.g.
            ``"/api/tasks"``. Appended to ``instance_url`` when building the
            fan-out URL. Defaults to ``""``.
        ttl: Seconds after which an entry is considered stale and excluded
            from peer lists. Should be at least ``2 * heartbeat_interval``.
        heartbeat_interval: Seconds between heartbeat writes to the backend.
    """

    def __init__(
        self,
        backend: "SnapshotBackend",
        instance_url: str,
        tasks_prefix: str = "",
        ttl: int = 90,
        heartbeat_interval: int = 30,
    ) -> None:
        self._backend = backend
        self._instance_url = instance_url.rstrip("/")
        self._tasks_prefix = tasks_prefix.rstrip("/")
        self._ttl = ttl
        self._heartbeat_interval = heartbeat_interval
        self._instance_id = str(uuid.uuid4())
        self._task: Optional[asyncio.Task] = None

    @property
    def instance_id(self) -> str:
        """Unique ID assigned to this instance at startup."""
        return self._instance_id

    async def start(self) -> None:
        """Write the initial registry entry and start the heartbeat loop."""
        await self._write_entry()
        self._task = asyncio.create_task(
            self._heartbeat_loop(), name="taskflow-instance-heartbeat"
        )

    async def stop(self) -> None:
        """Cancel the heartbeat loop and remove this instance from the registry."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._remove_entry()

    async def peers(self) -> list[dict]:
        """Return registry entries for live peer instances, excluding self.

        An entry is considered live if its ``last_seen`` unix timestamp is
        within ``ttl`` seconds of now.

        Returns:
            List of dicts with ``instance_id``, ``url``, and
            ``tasks_prefix`` keys for each reachable peer.
        """
        try:
            raw = await self._backend.load_metadata(_REGISTRY_KEY)
        except Exception:
            _log.exception("fastapi-taskflow: failed to read instance registry")
            return []

        if not raw:
            return []

        try:
            entries: dict = json.loads(raw)
        except Exception:
            _log.warning("fastapi-taskflow: instance registry contains invalid JSON")
            return []

        now = time.time()
        result = []
        for iid, info in entries.items():
            if iid == self._instance_id:
                continue
            if now - info.get("last_seen", 0) > self._ttl:
                continue
            result.append(
                {
                    "instance_id": iid,
                    "url": info.get("url", ""),
                    "tasks_prefix": info.get("tasks_prefix", ""),
                }
            )
        return result

    async def _write_entry(self) -> None:
        try:
            raw = await self._backend.load_metadata(_REGISTRY_KEY)
            entries: dict = json.loads(raw) if raw else {}
        except Exception:
            entries = {}

        entries[self._instance_id] = {
            "url": self._instance_url,
            "tasks_prefix": self._tasks_prefix,
            "last_seen": time.time(),
        }

        try:
            await self._backend.save_metadata(_REGISTRY_KEY, json.dumps(entries))
        except Exception:
            _log.exception(
                "fastapi-taskflow: failed to write instance registry entry for %s",
                self._instance_id,
            )

    async def _remove_entry(self) -> None:
        try:
            raw = await self._backend.load_metadata(_REGISTRY_KEY)
            entries: dict = json.loads(raw) if raw else {}
        except Exception:
            return

        entries.pop(self._instance_id, None)

        try:
            await self._backend.save_metadata(_REGISTRY_KEY, json.dumps(entries))
        except Exception:
            _log.exception(
                "fastapi-taskflow: failed to remove instance registry entry for %s",
                self._instance_id,
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            await self._write_entry()
