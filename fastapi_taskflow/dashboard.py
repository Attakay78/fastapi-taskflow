"""
SSE dashboard for live task visibility.

Mounted automatically by TaskAdmin at ``{path}/dashboard``.

Architecture
------------
* ``GET {path}/dashboard``          — full HTML page; opens an EventSource
* ``GET {path}/dashboard/metrics``  — HTML fragment (metrics section)
* ``GET {path}/dashboard/tasks``    — HTML fragment (task table body)
* ``GET {path}/dashboard/stream``   — SSE stream; emits a single ``state``
                                      JSON event on every store mutation
                                      (rapid bursts coalesced per client)
"""

from __future__ import annotations

import asyncio
import html
import json
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .auth import verify_token, COOKIE_NAME

if TYPE_CHECKING:
    from .manager import TaskManager
    from .models import TaskRecord

# ---------------------------------------------------------------------------
# HTML fragment helpers (used by /metrics and /tasks HTTP endpoints)
# ---------------------------------------------------------------------------

_STATUS_COLOR = {
    "pending": ("#f3f4f6", "#6b7280"),
    "running": ("#eff6ff", "#2563eb"),
    "success": ("#f0fdf4", "#16a34a"),
    "failed": ("#fef2f2", "#dc2626"),
    "interrupted": ("#fffbeb", "#d97706"),
    "cancelled": ("#fce7f3", "#be185d"),
}


def _badge(status: str) -> str:
    bg, fg = _STATUS_COLOR.get(status, ("#f3f4f6", "#6b7280"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f"border-radius:4px;font-size:0.72rem;font-weight:600;"
        f'letter-spacing:.02em">{html.escape(status)}</span>'
    )


def _metric_card(label: str, value: str, accent: str) -> str:
    return (
        f'<div style="background:white;border:1px solid #e5e7eb;border-radius:8px;padding:14px 16px">'
        f'<div style="font-size:0.68rem;text-transform:uppercase;letter-spacing:.05em;'
        f'color:#888;font-weight:500;margin-bottom:6px">{label}</div>'
        f'<div style="font-size:1.4rem;font-weight:600;color:#111;font-variant-numeric:tabular-nums">{value}</div>'
        f"</div>"
    )


def _render_metrics(tasks: list) -> str:
    total = len(tasks)
    success = sum(1 for t in tasks if t.status.value == "success")
    failed = sum(1 for t in tasks if t.status.value == "failed")
    running = sum(1 for t in tasks if t.status.value == "running")
    pending = sum(1 for t in tasks if t.status.value == "pending")
    interrupted = sum(1 for t in tasks if t.status.value == "interrupted")
    cancelled = sum(1 for t in tasks if t.status.value == "cancelled")
    rate = f"{success / total * 100:.1f}%" if total else "—"
    durs = [t.duration for t in tasks if t.duration is not None]
    avg = f"{sum(durs) / len(durs) * 1000:.0f} ms" if durs else "—"

    return (
        '<div style="display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:10px">'
        + _metric_card("Total", str(total), "#6366f1")
        + _metric_card("Pending", str(pending), "#9ca3af")
        + _metric_card("Running", str(running), "#7c3aed")
        + _metric_card("Success", str(success), "#16a34a")
        + _metric_card("Failed", str(failed), "#dc2626")
        + _metric_card("Interrupted", str(interrupted), "#d97706")
        + _metric_card("Cancelled", str(cancelled), "#be185d")
        + _metric_card("Success rate", rate, "#f59e0b")
        + _metric_card("Avg duration", avg, "#8b5cf6")
        + "</div>"
    )


def _render_task_rows(tasks: list) -> str:
    tasks = sorted(tasks, key=lambda t: t.created_at, reverse=True)
    if not tasks:
        return (
            '<tr><td colspan="6" style="text-align:center;color:#9ca3af;'
            'padding:40px;font-size:0.9rem">No tasks recorded yet.</td></tr>'
        )

    rows: list[str] = []
    for t in tasks:
        duration = f"{t.duration * 1000:.0f} ms" if t.duration is not None else "—"
        if t.error:
            short = html.escape(t.error[:60]) + ("…" if len(t.error) > 60 else "")
            error_cell = (
                f'<span style="color:#dc2626;font-size:0.8rem" '
                f'title="{html.escape(t.error)}">{short}</span>'
            )
        else:
            error_cell = '<span style="color:#d1d5db">—</span>'

        rows.append(
            '<tr style="border-bottom:1px solid #f3f4f6">'
            f'<td style="padding:10px 14px;font-family:monospace;font-size:0.75rem;color:#aaa">'
            f"{html.escape(t.task_id[:8])}…</td>"
            f'<td style="padding:10px 14px;font-weight:500;color:#111">{html.escape(t.func_name)}</td>'
            f'<td style="padding:10px 14px">{_badge(t.status.value)}</td>'
            f'<td style="padding:10px 14px;color:#6b7280;text-align:right">{duration}</td>'
            f'<td style="padding:10px 14px;color:#6b7280;text-align:center">{t.retries_used}</td>'
            f'<td style="padding:10px 14px">{error_cell}</td>'
            "</tr>"
        )
    return "".join(rows)


# ---------------------------------------------------------------------------
# SSE state event (JSON)
# ---------------------------------------------------------------------------


def _build_sse_state(
    tasks: list,
    include_args: bool = False,
    schedules: list | None = None,
) -> str:
    """
    Serialize the full task store as a single SSE ``state`` event.

    Payload shape::

        {
          "tasks":     [ <TaskRecord.to_dict()>, ... ],
          "metrics":   { "total": N, "pending": N, ... },
          "schedules": [ <schedule entry dict>, ... ]
        }

    When *include_args* is ``True`` each task dict also carries ``args`` and
    ``kwargs`` (serialised with ``repr()`` so arbitrary types are safe).
    Newlines are kept out of the data line so the SSE framing is unambiguous.
    """
    total = len(tasks)
    success = sum(1 for t in tasks if t.status.value == "success")
    failed = sum(1 for t in tasks if t.status.value == "failed")
    running = sum(1 for t in tasks if t.status.value == "running")
    pending = sum(1 for t in tasks if t.status.value == "pending")
    interrupted = sum(1 for t in tasks if t.status.value == "interrupted")
    cancelled = sum(1 for t in tasks if t.status.value == "cancelled")
    durs = [t.duration for t in tasks if t.duration is not None]

    metrics = {
        "total": total,
        "pending": pending,
        "running": running,
        "success": success,
        "failed": failed,
        "interrupted": interrupted,
        "cancelled": cancelled,
        "success_rate": round(success / total * 100, 1) if total else None,
        "avg_duration_ms": round(sum(durs) / len(durs) * 1000) if durs else None,
    }

    if include_args:
        task_dicts = [
            {
                **t.to_dict(),
                "args": [repr(a) for a in t.args],
                "kwargs": {k: repr(v) for k, v in t.kwargs.items()},
            }
            for t in tasks
        ]
    else:
        task_dicts = [t.to_dict() for t in tasks]

    payload = json.dumps(
        {"tasks": task_dicts, "metrics": metrics, "schedules": schedules or []}
    )
    return f"event: state\ndata: {payload}\n\n"


def _get_schedule_entries(task_manager: "TaskManager") -> list[dict]:
    """Serialise the registered schedule entries for the dashboard.

    Returns a list of dicts with ``func_name``, ``trigger``, ``next_run``,
    and ``last_status`` (the status of the most recent task record for this
    function, or ``None`` if no run has been recorded yet).
    """
    ps = task_manager._periodic_scheduler
    if ps is None:
        return []

    # Build a map of func_name -> most recent task record for fast lookup.
    all_tasks = task_manager.store.list()
    latest: dict[str, "TaskRecord"] = {}
    for t in all_tasks:
        if t.source != "scheduled":
            continue
        existing = latest.get(t.func_name)
        if existing is None or t.created_at > existing.created_at:
            latest[t.func_name] = t

    return [
        {
            "func_name": entry.func.__name__,
            "trigger": f"every {entry.every}s"
            if entry.every is not None
            else entry.cron,
            "next_run": entry.next_run.isoformat(),
            "last_status": last.status.value
            if (last := latest.get(entry.func.__name__)) is not None
            else None,
            "last_task_id": last.task_id if last is not None else None,
        }
        for entry in ps.entries
    ]


async def _sse_generator(
    task_manager: "TaskManager",
    request: Request,
    include_args: bool = False,
    poll_interval: float = 30.0,
) -> AsyncIterator[str]:
    """
    Yields SSE messages for the duration of the client connection.

    * Sends an immediate ``state`` event so the dashboard renders on first
      connect without waiting for a store change.
    * Wakes immediately on any local store mutation (in-process tasks).
    * On timeout:
      - No backend configured: sends a keep-alive comment only — local
        mutations are already instant, no backend read needed.
      - Backend configured: emits a full state refresh so completed tasks
        from other instances that flushed to the shared backend are picked up.
        Frequency controlled by *poll_interval* (default 30s).
    * Cleans up the subscriber queue on disconnect or CancelledError.
    """
    q = task_manager.store.add_subscriber()
    has_backend = task_manager._scheduler is not None
    try:
        tasks = await task_manager.merged_list()
        yield _build_sse_state(
            tasks,
            include_args=include_args,
            schedules=_get_schedule_entries(task_manager),
        )

        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(q.get(), timeout=poll_interval)
                # Local mutation — always emit a fresh state.
                tasks = await task_manager.merged_list()
                yield _build_sse_state(
                    tasks,
                    include_args=include_args,
                    schedules=_get_schedule_entries(task_manager),
                )
            except asyncio.TimeoutError:
                if not has_backend:
                    # Single instance, no backend — keep the connection alive
                    # without an unnecessary backend read.
                    yield ": keep-alive\n\n"
                else:
                    # Backend present — refresh to pick up other instances'
                    # completed tasks that flushed since the last local event.
                    tasks = await task_manager.merged_list()
                    yield _build_sse_state(
                        tasks,
                        include_args=include_args,
                        schedules=_get_schedule_entries(task_manager),
                    )
    except asyncio.CancelledError:
        pass
    finally:
        task_manager.store.remove_subscriber(q)


# ---------------------------------------------------------------------------
# Dashboard HTML page
# ---------------------------------------------------------------------------

# __STREAM_URL__ is replaced at request time by _dashboard_page().
_DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Task Dashboard · FastAPI-TaskFlow</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><rect width='200' height='200' rx='40' ry='40' fill='%23009688'/><polyline points='20,100 55,100 80,145 120,55 145,100 180,100' fill='none' stroke='white' stroke-width='18' stroke-linecap='round' stroke-linejoin='round'/></svg>">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script>(function(){var t=localStorage.getItem('tf-theme')||'light';document.documentElement.setAttribute('data-theme',t);})();</script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --db-bg: #f5f5f5; --db-surface: #ffffff; --db-surface-2: #fafafa;
      --db-surface-3: #f3f4f6; --db-surface-hover: #f9fafb;
      --db-border: #e5e7eb; --db-border-2: #f3f4f6;
      --db-text: #111111; --db-text-2: #374151; --db-text-3: #6b7280;
      --db-text-muted: #888888; --db-text-faint: #aaaaaa; --db-text-xfaint: #cccccc;
    }
    [data-theme="dark"] {
      --db-bg: #0d1117; --db-surface: #161b22; --db-surface-2: #1c2128;
      --db-surface-3: #21262d; --db-surface-hover: #1c2128;
      --db-border: #30363d; --db-border-2: #21262d;
      --db-text: #e6edf3; --db-text-2: #c9d1d9; --db-text-3: #8b949e;
      --db-text-muted: #8b949e; --db-text-faint: #6e7681; --db-text-xfaint: #484f58;
    }

    body {
      font-family: 'Geist', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 14px;
      background: var(--db-bg);
      color: var(--db-text);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }

    /* ── Header ─────────────────────────────────────────────── */
    .header {
      background: var(--db-surface);
      border-bottom: 1px solid var(--db-border);
      height: 52px;
      display: flex;
      align-items: center;
      padding: 0 24px;
      position: sticky;
      top: 0;
      z-index: 100;
      gap: 10px;
    }
    .header-icon { width: 26px; height: 26px; flex-shrink: 0; }
    .header-title { font-size: 14px; font-weight: 600; color: #009688; }
    .logout-btn { font-size: 12px; color: var(--db-text-3); text-decoration: none; padding: 4px 10px; border: 1px solid var(--db-border); border-radius: 5px; font-weight: 500; transition: color .15s, background .15s; }
    .logout-btn:hover { color: var(--db-text); background: var(--db-surface-3); }
    .header-badge { font-size: 11px; color: #009688; background: rgba(0,150,136,.08); border: 1px solid rgba(0,150,136,.25); border-radius: 4px; padding: 1px 7px; font-weight: 500; }
    .theme-btn { width: 28px; height: 28px; border: 1px solid var(--db-border); border-radius: 6px; background: var(--db-surface); cursor: pointer; color: var(--db-text-muted); display: flex; align-items: center; justify-content: center; transition: background .1s, border-color .1s, color .1s; flex-shrink: 0; }

    /* ── Main ───────────────────────────────────────────────── */
    .main { max-width: 1440px; margin: 0 auto; padding: 24px 24px 64px; }

    .dot { width: 7px; height: 7px; border-radius: 50%; background: #d1d5db; flex-shrink: 0; transition: background .3s; }
    .dot--live  { background: #17c964; animation: pulse 2s ease-in-out infinite; }
    .dot--error { background: #f31260; }
    .dot--connecting { background: #f59e0b; }
    @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }
    .status-label { font-size: 12px; color: var(--db-text-muted); font-weight: 500; }
    .status-label--live  { color: #16a34a; }
    .status-label--error { color: #dc2626; }

    /* ── Top row (metrics + filters) ────────────────────────── */
    .top-row { display: flex; gap: 16px; align-items: flex-start; margin-bottom: 8px; }
    .search-row { display: flex; gap: 16px; align-items: center; margin-bottom: 16px; }
    .search-row .search { flex: 1; height: 34px; }
    .search-row .filter-trigger-btn { width: 280px; flex-shrink: 0; height: 34px; font-size: 12px; justify-content: center; }
    .metrics {
      flex: 1;
      display: grid;
      grid-template-columns: repeat(9, minmax(0, 1fr));
      gap: 8px;
    }
    .filters-right {
      width: 280px;
      flex-shrink: 0;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      align-content: center;
    }
    .filters-right .sel,
    .filters-right .filter-trigger-btn { width: 100%; justify-content: center; height: 34px; font-size: 12px; }
    .filters-right .sel { -webkit-appearance: none; appearance: none; padding: 0 28px 0 10px; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%236b7280' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 8px center; }
    .filters-right .filter-trigger-btn { gap: 5px; }


    /* ── Inputs ─────────────────────────────────────────────── */
    .sel, .search {
      height: 32px;
      border: 1px solid var(--db-border); border-radius: 6px;
      padding: 0 10px; font-size: 13px; font-family: inherit;
      background: var(--db-surface); color: var(--db-text); outline: none;
      transition: border-color .15s, box-shadow .15s; cursor: pointer;
      width: 100%;
    }
    .sel option { background: var(--db-surface); color: var(--db-text); }
    .sel:hover, .search:hover { border-color: var(--db-text-muted); }
    .sel:focus, .search:focus { border-color: #009688; box-shadow: 0 0 0 3px rgba(0,150,136,.12); }
    .search { cursor: text; }
    .search::placeholder { color: var(--db-text-xfaint); }
    .metric-card {
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 10px 12px;
    }
    .metric-label {
      font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
      font-weight: 500; margin-bottom: 6px; opacity: .75;
    }
    .metric-value { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.5px; }

    .mc-total   { background: #f5f3ff; border-color: #e9d5ff; }
    .mc-total   .metric-label { color: #6d28d9; }
    .mc-total   .metric-value { color: #5b21b6; }

    .mc-pending { background: #f9fafb; border-color: #e5e7eb; }
    .mc-pending .metric-label { color: #6b7280; }
    .mc-pending .metric-value { color: #374151; }

    .mc-running { background: #eef2ff; border-color: #c7d2fe; }
    .mc-running .metric-label { color: #4338ca; }
    .mc-running .metric-value { color: #3730a3; }

    .mc-success { background: #f0fdf4; border-color: #bbf7d0; }
    .mc-success .metric-label { color: #15803d; }
    .mc-success .metric-value { color: #166534; }

    .mc-failed  { background: #fef2f2; border-color: #fecaca; }
    .mc-failed  .metric-label { color: #b91c1c; }
    .mc-failed  .metric-value { color: #991b1b; }

    .mc-interrupted { background: #fffbeb; border-color: #fcd34d; }
    .mc-interrupted .metric-label { color: #b45309; }
    .mc-interrupted .metric-value { color: #92400e; }

    .mc-rate    { background: #fffbeb; border-color: #fde68a; }
    .mc-rate    .metric-label { color: #b45309; }
    .mc-rate    .metric-value { color: #92400e; }

    .mc-avg     { background: #fdf4ff; border-color: #f0abfc; }
    .mc-avg     .metric-label { color: #a21caf; }
    .mc-avg     .metric-value { color: #86198f; }

    [data-theme="dark"] .mc-total   { background: rgba(124,58,237,.1); border-color: rgba(124,58,237,.25); }
    [data-theme="dark"] .mc-total   .metric-label { color: #a78bfa; }
    [data-theme="dark"] .mc-total   .metric-value { color: #c4b5fd; }
    [data-theme="dark"] .mc-pending { background: var(--db-surface-2); border-color: var(--db-border); }
    [data-theme="dark"] .mc-pending .metric-label { color: #9ca3af; }
    [data-theme="dark"] .mc-pending .metric-value { color: #d1d5db; }
    [data-theme="dark"] .mc-running { background: rgba(67,56,202,.12); border-color: rgba(99,102,241,.3); }
    [data-theme="dark"] .mc-running .metric-label { color: #818cf8; }
    [data-theme="dark"] .mc-running .metric-value { color: #a5b4fc; }
    [data-theme="dark"] .mc-success { background: rgba(21,128,61,.1); border-color: rgba(21,128,61,.3); }
    [data-theme="dark"] .mc-success .metric-label { color: #4ade80; }
    [data-theme="dark"] .mc-success .metric-value { color: #86efac; }
    [data-theme="dark"] .mc-failed  { background: rgba(185,28,28,.1); border-color: rgba(220,38,38,.25); }
    [data-theme="dark"] .mc-failed  .metric-label { color: #f87171; }
    [data-theme="dark"] .mc-failed  .metric-value { color: #fca5a5; }
    [data-theme="dark"] .mc-interrupted { background: rgba(180,83,9,.1); border-color: rgba(217,119,6,.3); }
    [data-theme="dark"] .mc-interrupted .metric-label { color: #fbbf24; }
    [data-theme="dark"] .mc-interrupted .metric-value { color: #fcd34d; }
    [data-theme="dark"] .mc-rate    { background: rgba(180,83,9,.1); border-color: rgba(245,158,11,.25); }
    [data-theme="dark"] .mc-rate    .metric-label { color: #fbbf24; }
    [data-theme="dark"] .mc-rate    .metric-value { color: #fcd34d; }
    [data-theme="dark"] .mc-avg     { background: rgba(162,28,175,.1); border-color: rgba(217,70,239,.25); }
    [data-theme="dark"] .mc-avg     .metric-label { color: #e879f9; }
    [data-theme="dark"] .mc-avg     .metric-value { color: #f0abfc; }

    /* ── Table ──────────────────────────────────────────────── */
    .table-wrap { background: var(--db-surface); border: 1px solid var(--db-border); border-radius: 10px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; }
    .th { padding: 9px 14px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--db-text-muted); font-weight: 600; background: var(--db-surface-2); border-bottom: 1px solid var(--db-border); cursor: pointer; user-select: none; white-space: nowrap; }
    .th:hover { color: var(--db-text-2); background: var(--db-surface-3); }
    .th--active { color: #009688; }
    .th--r { text-align: right; }
    .sort-icon { opacity: .4; font-size: 10px; margin-left: 3px; }
    .th--active .sort-icon { opacity: 1; }
    .td { padding: 10px 14px; border-bottom: 1px solid var(--db-border-2); font-size: 13px; color: var(--db-text-2); vertical-align: middle; }
    .row { cursor: pointer; transition: background .1s; }
    .row:hover { background: var(--db-surface-hover); }
    .row--selected { background: rgba(0,150,136,.07) !important; }
    .row:last-child .td { border-bottom: none; }
    .td--mono { font-family: 'Geist Mono', 'JetBrains Mono', 'Fira Code', monospace; font-size: 11.5px; color: var(--db-text-faint); }
    .td--func { font-weight: 500; color: var(--db-text); }
    .td--r    { text-align: right; color: var(--db-text-3); font-variant-numeric: tabular-nums; }
    .td--date { color: var(--db-text-faint); font-size: 12px; white-space: nowrap; padding-left: 24px; }
    .td--err  { max-width: 180px; }
    .err-text { color: #dc2626; font-size: 12px; }
    .muted { color: var(--db-text-xfaint); }
    .empty { text-align: center; padding: 52px 16px; color: var(--db-text-faint); font-size: 13px; }

    /* ── Badges ─────────────────────────────────────────────── */
    .badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; letter-spacing: .02em; }
    .badge--pending { background: #f3f4f6; color: #6b7280; }
    .badge--running { background: #ede9fe; color: #7c3aed; }
    .badge--success { background: #dcfce7; color: #16a34a; }
    .badge--failed       { background: #fee2e2; color: #dc2626; }
    .badge--interrupted  { background: #fffbeb; color: #d97706; }
    [data-theme="dark"] .badge--pending     { background: var(--db-surface-3); color: #9ca3af; }
    [data-theme="dark"] .badge--running     { background: rgba(124,58,237,.2); color: #a78bfa; }
    [data-theme="dark"] .badge--success     { background: rgba(22,163,74,.2); color: #4ade80; }
    [data-theme="dark"] .badge--failed      { background: rgba(220,38,38,.2); color: #f87171; }
    [data-theme="dark"] .badge--interrupted { background: rgba(217,119,6,.15); color: #fbbf24; }

    /* ── Detail Panel ───────────────────────────────────────── */
    .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.2); opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 200; }
    .backdrop--on { opacity: 1; pointer-events: auto; }
    [data-theme="dark"] .backdrop { background: rgba(0,0,0,.55); }
    .panel { position: fixed; top: 0; right: 0; bottom: 0; width: 440px; max-width: 100vw; background: var(--db-surface); border-left: 1px solid var(--db-border); transform: translateX(100%); transition: transform .25s cubic-bezier(.4,0,.2,1); z-index: 201; display: flex; flex-direction: column; }
    .panel--open { transform: translateX(0); }
    .panel-header { display: flex; flex-direction: column; padding: 12px 20px 0; border-bottom: 1px solid var(--db-border-2); flex-shrink: 0; gap: 0; }
    .panel-header-row { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; }
    .panel-title { font-size: 14px; font-weight: 600; color: var(--db-text); }
    .panel-tabs { display: flex; gap: 2px; margin: 0 -20px; padding: 0 20px; }
    .panel-tab { padding: 5px 11px; font-size: 12px; font-weight: 500; color: var(--db-text-3); background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; margin-bottom: -1px; border-radius: 4px 4px 0 0; transition: color .12s; }
    .panel-tab:hover { color: var(--db-text); }
    .panel-tab.panel-tab--active { color: #009688; border-bottom-color: #009688; }
    .panel-tab--error.panel-tab--active { color: #dc2626; border-bottom-color: #dc2626; }
    .panel-close { margin-left: auto; width: 28px; height: 28px; border: 1px solid var(--db-border); border-radius: 6px; background: var(--db-surface); cursor: pointer; display: flex; align-items: center; justify-content: center; color: var(--db-text-3); transition: background .1s, border-color .1s; }
    .panel-close:hover { background: var(--db-surface-3); color: var(--db-text); }
    .retry-btn { display: inline-flex; align-items: center; gap: 5px; padding: 5px 12px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface); color: var(--db-text-2); cursor: pointer; transition: background .1s, border-color .1s, color .1s; }
    .retry-btn:hover { background: var(--db-surface-3); border-color: #6366f1; color: #6366f1; }
    .retry-btn:disabled { opacity: .5; cursor: not-allowed; }
    .retry-btn--warn { border-color: #f59e0b; color: #b45309; }
    .retry-btn--warn:hover { background: #fffbeb; border-color: #d97706; color: #92400e; }
    .panel-body { flex: 1; overflow-y: auto; padding: 20px; overscroll-behavior: contain; }
    .panel-resize { position: absolute; top: 0; left: 0; width: 5px; height: 100%; cursor: col-resize; z-index: 1; }
    .panel-resize::after { content: ''; position: absolute; top: 50%; left: 1px; transform: translateY(-50%); width: 3px; height: 32px; border-radius: 2px; background: var(--db-border); transition: background .15s; }
    .panel-resize:hover::after, .panel-resize--active::after { background: #009688; }

    /* Copy button */
    .copy-btn { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; flex-shrink: 0; border: 1px solid var(--db-border); border-radius: 5px; background: var(--db-surface); cursor: pointer; color: var(--db-text-faint); transition: background .1s, border-color .1s, color .1s; }
    .copy-btn:hover { background: var(--db-surface-3); border-color: var(--db-text-faint); color: var(--db-text); }
    .copy-btn--row { width: 18px; height: 18px; border-radius: 3px; opacity: 0; margin-left: 5px; vertical-align: middle; flex-shrink: 0; }
    .row:hover .copy-btn--row { opacity: 1; }

    /* Toast */
    .toast { position: fixed; bottom: 24px; right: 24px; z-index: 400; background: var(--db-text); color: var(--db-bg); font-size: 13px; font-weight: 500; padding: 10px 16px; border-radius: 8px; display: flex; align-items: center; gap: 8px; box-shadow: 0 4px 14px rgba(0,0,0,.2); opacity: 0; transform: translateY(6px); transition: opacity .2s, transform .2s; pointer-events: none; }
    .toast--on { opacity: 1; transform: translateY(0); }

    /* Pagination */
    .pagination { display: flex; align-items: center; gap: 8px; margin-top: 10px; justify-content: flex-end; }
    .pg-btn { height: 30px; min-width: 30px; padding: 0 10px; border: 1px solid var(--db-border); border-radius: 6px; background: var(--db-surface); cursor: pointer; font-size: 12px; font-family: inherit; color: var(--db-text-2); transition: background .1s, border-color .1s; }
    .pg-btn:hover:not(:disabled) { background: var(--db-surface-3); }
    .pg-btn:disabled { opacity: .35; cursor: default; }
    .pg-info { font-size: 12px; color: var(--db-text-muted); }

    /* Detail content */
    .d-section { margin-bottom: 16px; }
    .d-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--db-text-faint); font-weight: 500; margin-bottom: 4px; }
    .d-val { font-size: 13px; color: var(--db-text); }
    .d-mono { font-family: 'Geist Mono', monospace; font-size: 11.5px; color: var(--db-text-3); word-break: break-all; }
    .d-func { font-weight: 600; font-size: 14px; }
    .d-error { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 12px; font-family: 'Geist Mono', monospace; white-space: pre-wrap; word-break: break-word; max-height: 120px; overflow-y: auto; }
    [data-theme="dark"] .d-error { background: rgba(220,38,38,.1); border-color: rgba(220,38,38,.3); color: #fca5a5; }
    .d-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--db-border); margin-bottom: 12px; }
    .d-tab { padding: 5px 12px; font-size: 12px; font-weight: 500; color: var(--db-text-3); background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; margin-bottom: -1px; border-radius: 4px 4px 0 0; transition: color .12s; }
    .d-tab:hover { color: var(--db-text); }
    .d-tab.d-tab--active { color: #009688; border-bottom-color: #009688; }
    .d-tab.d-tab--error.d-tab--active { color: #dc2626; border-bottom-color: #dc2626; }
    .d-tab-panel { display: none; }
    .d-tab-panel.d-tab-panel--active { display: block; }
    .d-logs { font-family: 'Geist Mono', monospace; font-size: 11.5px; color: var(--db-text-2); background: var(--db-surface-2); border: 1px solid var(--db-border); border-radius: 6px; padding: 10px 12px; max-height: 220px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.6; }
    .d-logs .log-line { display: flex; gap: 8px; }
    .d-logs .log-ts { color: var(--db-text-faint); flex-shrink: 0; }
    .d-logs .log-sep { color: var(--db-text-faint); font-style: italic; }
    .d-error-msg { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 12px; font-weight: 500; margin-bottom: 8px; }
    [data-theme="dark"] .d-error-msg { background: rgba(220,38,38,.1); border-color: rgba(220,38,38,.3); color: #f87171; }
    .d-stacktrace { font-size: 11px; color: #7f1d1d; background: #fff5f5; padding: 10px 12px; font-family: 'Geist Mono', monospace; white-space: pre-wrap; word-break: break-all; max-height: 280px; overflow-y: auto; border: 1px solid #fecaca; border-radius: 6px; }
    [data-theme="dark"] .d-stacktrace { color: #fca5a5; background: rgba(220,38,38,.08); border-color: rgba(220,38,38,.25); }
    .d-row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; margin-bottom: 16px; }
    .d-row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 10px; margin-bottom: 16px; }
    .divider { height: 1px; background: var(--db-border-2); margin: 20px 0; }
    .section-title { display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: var(--db-text-3); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }
    .analytics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .a-card { background: var(--db-surface-2); border: 1px solid var(--db-border-2); border-radius: 6px; padding: 10px 12px; }
    .a-label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--db-text-xfaint); font-weight: 500; margin-bottom: 3px; }
    .a-value { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
    .a-value--neutral { color: var(--db-text); }
    .a-value--success { color: #16a34a; }
    .a-value--danger  { color: #dc2626; }
    .a-value--running { color: #7c3aed; }
    .a-value--warning { color: #d97706; }
    [data-theme="dark"] .a-value--success { color: #4ade80; }
    [data-theme="dark"] .a-value--danger  { color: #f87171; }
    [data-theme="dark"] .a-value--running { color: #a78bfa; }
    [data-theme="dark"] .a-value--warning { color: #fbbf24; }
    .recent-runs { display: flex; flex-direction: column; gap: 6px; }
    .r-run { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid var(--db-border-2); border-radius: 6px; cursor: pointer; transition: background .1s; }
    .r-run:hover { background: var(--db-surface-2); }
    .r-run--cur { background: rgba(0,150,136,.07); border-color: rgba(0,150,136,.25); }
    .r-dur { font-size: 11px; margin-left: auto; color: var(--db-text-faint); }
    .arg-idx  { font-size: 10px; color: var(--db-text-xfaint); min-width: 18px; text-align: right; }
    .arg-key  { font-size: 11px; color: var(--db-text-3); }
    .arg-eq   { font-size: 11px; color: var(--db-text-xfaint); }
    .arg-code { background: var(--db-surface-2); border: 1px solid var(--db-border-2); border-radius: 4px; padding: 2px 7px; font-size: 11.5px; }

    /* ── Pause button ───────────────────────────────────────── */
    .pause-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface); color: var(--db-text-2); cursor: pointer; transition: background .1s, border-color .1s; }
    .pause-btn:hover { background: var(--db-surface-3); }
    .pause-btn--paused { border-color: #f59e0b; color: #b45309; background: #fffbeb; }
    [data-theme="dark"] .pause-btn--paused { background: rgba(180,83,9,.15); color: #fbbf24; border-color: rgba(217,119,6,.4); }
    .new-badge { display: inline-block; background: #f59e0b; color: #fff; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; margin-left: 2px; }
    .source-badge { display: inline-block; background: rgba(0,150,136,.12); color: #009688; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; margin-left: 4px; letter-spacing: .02em; }
    [data-theme="dark"] .source-badge { background: rgba(0,150,136,.2); color: #4db6ac; }

    /* ── Filter / Clear history trigger buttons ─────────────── */
    .filter-trigger-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface); color: var(--db-text-2); cursor: pointer; transition: background .1s, border-color .1s; white-space: nowrap; }
    .filter-trigger-btn:hover { background: var(--db-surface-3); }
    .filter-trigger-btn--active { border-color: #009688; color: #009688; background: rgba(0,150,136,.07); }
    [data-theme="dark"] .filter-trigger-btn--active { background: rgba(0,150,136,.15); }
    .filter-trigger-btn--danger { color: #dc2626; }
    .filter-trigger-btn--danger:hover { background: #fef2f2; border-color: #fca5a5; }
    [data-theme="dark"] .filter-trigger-btn--danger { color: #f87171; }
    [data-theme="dark"] .filter-trigger-btn--danger:hover { background: rgba(220,38,38,.12); border-color: rgba(248,113,113,.3); }

    /* ── Popup modals ────────────────────────────────────────── */
    .popup-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 200; align-items: center; justify-content: center; }
    .popup-backdrop--open { display: flex; }
    .popup { background: var(--db-surface); border: 1px solid var(--db-border); border-radius: 12px; padding: 24px; width: 340px; max-width: 95vw; box-shadow: 0 20px 60px rgba(0,0,0,.18); }
    [data-theme="dark"] .popup { box-shadow: 0 20px 60px rgba(0,0,0,.5); }
    .popup-title { font-size: 15px; font-weight: 700; color: var(--db-text); margin: 0 0 4px; letter-spacing: -.01em; }
    .popup-desc { font-size: 12px; color: var(--db-text-2); margin: 0 0 18px; line-height: 1.5; }
    .popup-field { margin-bottom: 14px; }
    .popup-label { display: block; font-size: 11px; font-weight: 600; color: var(--db-text-2); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
    .popup-row { display: flex; gap: 8px; }
    .popup-input { flex: 1; padding: 8px 10px; font-size: 13px; border: 1px solid var(--db-border); border-radius: 7px; background: var(--db-bg); color: var(--db-text); outline: none; transition: border-color .15s; font-family: inherit; }
    .popup-input:focus { border-color: #009688; }
    .popup-select { padding: 8px 10px; font-size: 13px; border: 1px solid var(--db-border); border-radius: 7px; background: var(--db-bg); color: var(--db-text); outline: none; cursor: pointer; font-family: inherit; }
    .popup-select:focus { border-color: #009688; }
    .popup-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }
    .popup-btn { padding: 7px 16px; font-size: 12px; font-weight: 600; border-radius: 7px; cursor: pointer; border: 1px solid transparent; transition: background .1s, opacity .1s; font-family: inherit; }
    .popup-btn--primary { background: #009688; color: #fff; border-color: #009688; }
    .popup-btn--primary:hover { background: #00796b; border-color: #00796b; }
    .popup-btn--danger { background: #dc2626; color: #fff; border-color: #dc2626; }
    .popup-btn--danger:hover { background: #b91c1c; border-color: #b91c1c; }
    .popup-btn--cancel { background: transparent; color: var(--db-text-2); border-color: var(--db-border); }
    .popup-btn--cancel:hover { background: var(--db-surface-3); }
    .popup-warning { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fca5a5; border-radius: 7px; padding: 8px 10px; margin-top: 12px; line-height: 1.5; }
    [data-theme="dark"] .popup-warning { background: rgba(220,38,38,.1); border-color: rgba(248,113,113,.3); color: #f87171; }

    /* ── Bulk retry toolbar ─────────────────────────────────── */
    .bulk-bar { display: none; align-items: center; gap: 10px; padding: 8px 12px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 7px; margin-bottom: 10px; font-size: 13px; color: #1e40af; }
    .bulk-bar--on { display: flex; }
    [data-theme="dark"] .bulk-bar { background: rgba(37,99,235,.12); border-color: rgba(99,162,235,.25); color: #93c5fd; }
    .bulk-btn { padding: 5px 14px; font-size: 12px; font-weight: 600; border-radius: 5px; border: 1px solid #3b82f6; background: #3b82f6; color: #fff; cursor: pointer; transition: background .1s; }
    .bulk-btn:hover { background: #2563eb; }
    .bulk-btn:disabled { opacity: .5; cursor: not-allowed; }
    .bulk-btn--cancel { background: transparent; border-color: var(--db-border); color: var(--db-text-2); }
    .bulk-btn--cancel:hover { background: var(--db-surface-3); }

    /* ── Checkbox column ────────────────────────────────────── */
    .th--check, .td--check { width: 36px; padding: 0 0 0 12px; }
    .td--check { vertical-align: middle; }
    input.row-check { cursor: pointer; accent-color: #3b82f6; width: 14px; height: 14px; }

    /* ── DLQ toolbar ────────────────────────────────────────── */
    .dlq-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
    .dlq-label { font-size: 13px; color: var(--db-text-2); }
    .dlq-select { font-size: 13px; padding: 4px 8px; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface-2); color: var(--db-text); cursor: pointer; }

    /* ── Tab bar (segmented control) ────────────────────────── */
    .tab-bar { display: inline-flex; gap: 2px; margin-bottom: 16px; background: var(--db-surface-3); border: 1px solid var(--db-border); border-radius: 9px; padding: 3px; }
    .tab-btn { background: none; border: none; border-radius: 7px; padding: 5px 14px; font-size: 12.5px; font-weight: 500; color: var(--db-text-muted); cursor: pointer; transition: color .15s, background .15s, box-shadow .15s; white-space: nowrap; }
    .tab-btn:hover { color: var(--db-text-2); background: rgba(0,0,0,.04); }
    [data-theme="dark"] .tab-btn:hover { background: rgba(255,255,255,.05); }
    .tab-btn--active { background: var(--db-surface); color: var(--db-text); box-shadow: 0 1px 3px rgba(0,0,0,.10), 0 0 0 1px rgba(0,0,0,.06); }
    [data-theme="dark"] .tab-btn--active { box-shadow: 0 1px 3px rgba(0,0,0,.4), 0 0 0 1px rgba(255,255,255,.06); }
    .tab-count { display: inline-block; background: rgba(0,150,136,.12); color: #009688; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; margin-left: 4px; }
    [data-theme="dark"] .tab-count { background: rgba(0,150,136,.2); color: #4db6ac; }
    .tab-btn--active .tab-count { background: rgba(0,150,136,.15); }


    /* ── Cancelled badge ────────────────────────────────────── */
    .badge--cancelled { background: #fce7f3; color: #be185d; }
    [data-theme="dark"] .badge--cancelled { background: rgba(190,24,93,.2); color: #f472b6; }

    /* ── Cancelled metric card ──────────────────────────────── */
    .mc-cancelled { background: #fce7f3; border-color: #f9a8d4; }
    .mc-cancelled .metric-label { color: #9d174d; }
    .mc-cancelled .metric-value { color: #831843; }
    [data-theme="dark"] .mc-cancelled { background: rgba(190,24,93,.1); border-color: rgba(244,114,182,.25); }
    [data-theme="dark"] .mc-cancelled .metric-label { color: #f472b6; }
    [data-theme="dark"] .mc-cancelled .metric-value { color: #fbcfe8; }

    /* ── Cancel button ──────────────────────────────────────── */
    .cancel-btn { display: inline-flex; align-items: center; gap: 5px; padding: 5px 12px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid #fca5a5; background: var(--db-surface); color: #dc2626; cursor: pointer; transition: background .1s, border-color .1s; }
    .cancel-btn:hover { background: #fef2f2; border-color: #ef4444; }
    .cancel-btn:disabled { opacity: .5; cursor: not-allowed; }

    /* ── Audit table ────────────────────────────────────────── */
    .audit-empty { text-align: center; padding: 40px; color: var(--db-text-faint); font-size: 13px; }

    /* ── Registered tasks grid ───────────────────────────────── */
    .rtask-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
    .rtask-card { background: var(--db-surface); border: 1px solid var(--db-border); border-radius: 8px; padding: 12px 14px; transition: border-color .15s, box-shadow .15s; }
    .rtask-card:hover { border-color: #009688; box-shadow: 0 2px 8px rgba(0,150,136,.08); }
    .rtask-name { font-family: 'Geist Mono', monospace; font-size: 12.5px; font-weight: 600; color: var(--db-text); margin-bottom: 10px; word-break: break-all; line-height: 1.4; }
    .rtask-pills { display: flex; flex-wrap: wrap; gap: 4px; }
    .rtask-pill { font-size: 10.5px; font-weight: 500; padding: 2px 7px; border-radius: 4px; background: var(--db-surface-3); color: var(--db-text-3); border: 1px solid var(--db-border-2); white-space: nowrap; }
    .rtask-pill--accent { background: rgba(0,150,136,.08); color: #009688; border-color: rgba(0,150,136,.2); }
    [data-theme="dark"] .rtask-pill--accent { background: rgba(0,150,136,.15); color: #4db6ac; }

  </style>
</head>
<body>

<header class="header">
  <svg class="header-icon" width="26" height="26" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
    <rect width="200" height="200" rx="40" ry="40" fill="#009688"/>
    <polyline points="20,100 55,100 80,145 120,55 145,100 180,100" fill="none" stroke="white" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
  <span class="header-title">Task Dashboard</span>
  <span class="header-badge">__TITLE__</span>
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span class="dot dot--connecting" id="live-dot"></span>
    <span class="status-label" id="live-label">Connecting&#8230;</span>
    <button class="pause-btn" id="pause-btn" onclick="togglePause()" title="Pause live updates">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      Pause
    </button>
    <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" title="Switch to dark mode">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
    __LOGOUT_BUTTON__
  </div>
</header>

<main class="main">

  <!-- Top row: metrics left, filters right -->
  <div class="top-row">
    <!-- Metrics -->
    <div class="metrics">
      <div class="metric-card mc-total">  <div class="metric-label">Total</div>        <div class="metric-value" id="metric-total">&#8212;</div></div>
      <div class="metric-card mc-pending"><div class="metric-label">Pending</div>       <div class="metric-value" id="metric-pending">&#8212;</div></div>
      <div class="metric-card mc-running"><div class="metric-label">Running</div>       <div class="metric-value" id="metric-running">&#8212;</div></div>
      <div class="metric-card mc-success"><div class="metric-label">Success</div>       <div class="metric-value" id="metric-success">&#8212;</div></div>
      <div class="metric-card mc-failed"> <div class="metric-label">Failed</div>        <div class="metric-value" id="metric-failed">&#8212;</div></div>
      <div class="metric-card mc-interrupted"><div class="metric-label">Interrupted</div>  <div class="metric-value" id="metric-interrupted">&#8212;</div></div>
      <div class="metric-card mc-cancelled"><div class="metric-label">Cancelled</div>    <div class="metric-value" id="metric-cancelled">&#8212;</div></div>
      <div class="metric-card mc-rate">   <div class="metric-label">Success Rate</div> <div class="metric-value" id="metric-rate">&#8212;</div></div>
      <div class="metric-card mc-avg">    <div class="metric-label">Avg Duration</div> <div class="metric-value" id="metric-avg">&#8212;</div></div>
    </div>
    <!-- Filters -->
    <div class="filters-right">
      <select class="sel" id="status-filter">
        <option value="all">All statuses</option>
        <option value="pending">Pending</option>
        <option value="running">Running</option>
        <option value="success">Success</option>
        <option value="failed">Failed</option>
        <option value="interrupted">Interrupted</option>
        <option value="cancelled">Cancelled</option>
      </select>
      <select class="sel" id="func-filter">
        <option value="all">All functions</option>
      </select>
      <button class="filter-trigger-btn" id="time-filter-btn" onclick="openTimeFilterPopup()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <span id="time-filter-label">All time</span>
      </button>
      <button class="filter-trigger-btn" onclick="exportCsv()" title="Export current view as CSV">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Export CSV
      </button>
    </div>
  </div>

  <!-- Search + clear history row -->
  <div class="search-row">
    <input type="search" class="search" id="search-input" placeholder="Search by ID or function&#8230;" autocomplete="off">
    <button class="filter-trigger-btn filter-trigger-btn--danger" onclick="openClearHistoryPopup()">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
      Clear history
    </button>
  </div>

  <!-- Tab bar -->
  <div class="tab-bar">
    <button class="tab-btn tab-btn--active" id="tab-view" onclick="showTab('view')">View<span class="tab-count" id="tab-view-count"></span></button>
    <button class="tab-btn" id="tab-deadletters" onclick="showTab('deadletters')">Dead Letters<span class="tab-count" id="tab-deadletters-count" style="background:rgba(220,38,38,.12);color:#dc2626"></span></button>
    <button class="tab-btn" id="tab-audit" onclick="showTab('audit')" style="display:none">Audit<span class="tab-count" id="tab-audit-count"></span></button>
    <button class="tab-btn" id="tab-schedules" onclick="showTab('schedules')">Schedules<span class="tab-count" id="tab-schedules-count"></span></button>
    <button class="tab-btn" id="tab-tasks" onclick="showTab('tasks')">Tasks<span class="tab-count" id="tab-tasks-count"></span></button>
  </div>

  <!-- View panel (task run history) -->
  <div id="panel-view">

  <!-- Bulk retry toolbar -->
  <div class="bulk-bar" id="bulk-bar">
    <span id="bulk-label">0 tasks selected</span>
    <button class="bulk-btn" id="bulk-retry-btn" onclick="bulkRetry()">Retry selected</button>
    <button class="bulk-btn bulk-btn--cancel" onclick="clearSelection()">Clear</button>
  </div>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="th th--check"><input type="checkbox" class="row-check" id="select-all-check" onclick="toggleSelectAll(this)" title="Select all retryable on this page"></th>
          <th class="th" data-sort="task_id" onclick="setSort('task_id')">ID <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="func_name" onclick="setSort('func_name')">Function <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="status" onclick="setSort('status')">Status <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="duration" onclick="setSort('duration')">Duration <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="retries_used" onclick="setSort('retries_used')">Retries <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="created_at" onclick="setSort('created_at')">Created <span class="sort-icon">&#8595;</span></th>
          <th class="th" data-sort="priority" onclick="setSort('priority')">Priority <span class="sort-icon">&#8661;</span></th>
          <th class="th">Error</th>
        </tr>
      </thead>
      <tbody id="tasks-tbody">
        <tr><td colspan="9" class="empty">Connecting&#8230;</td></tr>
      </tbody>
    </table>
  </div>
  <div id="pagination"></div>

  </div>

  <!-- Dead Letters panel -->
  <div id="panel-deadletters" style="display:none">

    <!-- Toolbar: time-window replay -->
    <div class="dlq-toolbar">
      <span class="dlq-label">Replay window:</span>
      <select id="dlq-window-select" class="dlq-select">
        <option value="1h">Last 1 hour</option>
        <option value="6h" selected>Last 6 hours</option>
        <option value="24h">Last 24 hours</option>
        <option value="7d">Last 7 days</option>
        <option value="all">All time</option>
      </select>
      <button class="bulk-btn" onclick="dlqReplayWindow()">Replay window</button>
    </div>

    <!-- Bulk bar: shown when rows are checked -->
    <div class="bulk-bar" id="dlq-bulk-bar">
      <span id="dlq-bulk-label">0 tasks selected</span>
      <button class="bulk-btn" onclick="dlqReplaySelected()">Replay selected</button>
      <button class="bulk-btn bulk-btn--cancel" onclick="dlqClearSelection()">Clear</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="th th--check"><input type="checkbox" class="row-check" id="dlq-select-all" onclick="dlqToggleAll(this)" title="Select all"></th>
            <th class="th">ID</th>
            <th class="th">Function</th>
            <th class="th">Status</th>
            <th class="th">Duration</th>
            <th class="th">Created</th>
            <th class="th">Error</th>
          </tr>
        </thead>
        <tbody id="deadletters-tbody"></tbody>
      </table>
    </div>
  </div>


  <!-- Audit panel -->
  <div id="panel-audit" style="display:none">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="th">Time</th>
          <th class="th">Action</th>
          <th class="th">Task ID</th>
          <th class="th">Actor</th>
          <th class="th">Detail</th>
        </tr></thead>
        <tbody id="audit-tbody"><tr><td colspan="5" class="audit-empty">Loading&#8230;</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Schedules panel -->
  <div id="panel-schedules" style="display:none">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="th">Function</th>
          <th class="th">Trigger</th>
          <th class="th">Next Run</th>
          <th class="th">Last Status</th>
        </tr></thead>
        <tbody id="schedules-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Tasks panel (registered functions) -->
  <div id="panel-tasks" style="display:none">
    <div id="registered-tasks-list"></div>
  </div>

</main>

<div class="toast" id="toast"></div>
<div class="backdrop" id="detail-backdrop"></div>

<!-- Time filter popup -->
<div class="popup-backdrop" id="time-filter-popup" onclick="closeTimeFilterPopup(event)">
  <div class="popup" onclick="event.stopPropagation()">
    <div class="popup-title">Filter by time</div>
    <div class="popup-desc">Show tasks created within the last N units. Leave empty to show all.</div>
    <div class="popup-field">
      <label class="popup-label">Time window</label>
      <div class="popup-row">
        <input type="number" class="popup-input" id="tf-popup-val" placeholder="e.g. 6" min="1" autocomplete="off">
        <select class="popup-select" id="tf-popup-unit">
          <option value="min">Minutes</option>
          <option value="hour" selected>Hours</option>
          <option value="day">Days</option>
        </select>
      </div>
    </div>
    <div class="popup-actions">
      <button class="popup-btn popup-btn--cancel" onclick="clearTimeFilter()">Clear filter</button>
      <button class="popup-btn popup-btn--primary" onclick="applyTimeFilter()">Apply</button>
    </div>
  </div>
</div>

<!-- DLQ replay window confirmation popup -->
<div class="popup-backdrop" id="dlq-replay-popup" onclick="closeDlqReplayPopup(event)">
  <div class="popup" onclick="event.stopPropagation()">
    <div class="popup-title">Replay failed tasks</div>
    <div class="popup-desc" id="dlq-replay-popup-desc">Re-enqueue all failed tasks in the selected window. Each task will run again with its original arguments.</div>
    <div class="popup-actions">
      <button class="popup-btn popup-btn--cancel" onclick="closeDlqReplayPopup()">Cancel</button>
      <button class="popup-btn popup-btn--primary" id="dlq-replay-confirm-btn" onclick="confirmDlqReplayWindow()">Replay</button>
    </div>
  </div>
</div>

<!-- Clear history popup -->
<div class="popup-backdrop" id="clear-history-popup" onclick="closeClearHistoryPopup(event)">
  <div class="popup" onclick="event.stopPropagation()">
    <div class="popup-title">Clear task history</div>
    <div class="popup-desc">Delete completed tasks (success, failed, interrupted) older than the entered period.</div>
    <div class="popup-field">
      <label class="popup-label">Delete tasks older than</label>
      <div class="popup-row">
        <input type="number" class="popup-input" id="ch-popup-val" placeholder="e.g. 7" min="1" autocomplete="off">
        <select class="popup-select" id="ch-popup-unit">
          <option value="min">Minutes</option>
          <option value="hour">Hours</option>
          <option value="day" selected>Days</option>
        </select>
      </div>
    </div>
    <div class="popup-warning">This action cannot be undone. Pending and running tasks are never deleted.</div>
    <div class="popup-actions">
      <button class="popup-btn popup-btn--cancel" onclick="closeClearHistoryPopup()">Cancel</button>
      <button class="popup-btn popup-btn--danger" onclick="confirmClearHistory()">Delete</button>
    </div>
  </div>
</div>

<div class="panel" id="detail-panel">
  <div class="panel-resize" id="panel-resize-handle"></div>
  <div class="panel-header">
    <div class="panel-header-row">
      <span class="panel-title">Task Detail</span>
      <button class="panel-close" id="detail-close" aria-label="Close">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
    <div class="panel-tabs" id="detail-tabs"></div>
  </div>
  <div class="panel-body" id="detail-content"></div>
</div>

<script>
  const STREAM_URL        = '__STREAM_URL__';
  const SHOW_ARGS         = __SHOW_ARGS__;
  const TASKS_PREFIX      = '__TASKS_PREFIX__';
  const REGISTERED_TASKS  = __REGISTERED_TASKS__;
  const SHOW_AUDIT        = __SHOW_AUDIT__;

  // ── State ──────────────────────────────────────────────────────
  const PAGE_SIZE = 30;
  let state = { tasks: [], metrics: {}, schedules: [] };
  let sortCol      = localStorage.getItem('tf-sort-col') || 'created_at';
  let sortDir      = localStorage.getItem('tf-sort-dir') || 'desc';
  let statusFilter = localStorage.getItem('tf-status')  || 'all';
  let funcFilter   = localStorage.getItem('tf-func')    || 'all';
  let searchQuery  = localStorage.getItem('tf-search')  || '';
  let timeFilterVal  = localStorage.getItem('tf-time-val')  || '';
  let timeFilterUnit = localStorage.getItem('tf-time-unit') || 'hour';

  let selectedId   = null;
  let currentPage  = 0;
  let paused       = localStorage.getItem('tf-paused') === '1';
  let pendingState = null;
  let pendingNewTasks = 0;
  let selectedIds  = new Set();

  // ── SSE ────────────────────────────────────────────────────────
  function connect() {
    const src = new EventSource(STREAM_URL);
    src.addEventListener('state', function(e) {
      var incoming = JSON.parse(e.data);
      if (paused) {
        var prevCount = pendingState ? pendingState.tasks.length : state.tasks.length;
        var newCount  = incoming.tasks.length - prevCount;
        if (newCount > 0) pendingNewTasks += newCount;
        pendingState = incoming;
        var btn = document.getElementById('pause-btn');
        if (btn) btn.innerHTML =
          '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>'
          + ' Resume' + (pendingNewTasks > 0 ? ' <span class="new-badge">+' + pendingNewTasks + '</span>' : '');
        return;
      }
      applyState(incoming);
    });
    src.onopen  = () => setStatus('live');
    src.onerror = () => { setStatus('error'); src.close(); setTimeout(connect, 3000); };
  }

  function applyState(newState) {
    state = newState;
    renderMetrics();
    populateFuncFilter();
    renderTable();
    if (selectedId) {
      const t = state.tasks.find(function(t) { return t.task_id === selectedId; });
      if (t) renderDetail(t); else closeDetail();
    }
    renderSchedules();
    renderDeadLetters();
    var vcnt = document.getElementById('tab-view-count');
    if (vcnt) vcnt.textContent = state.tasks.length > 0 ? state.tasks.length : '';
  }

  function showTab(name) {
    ['view', 'deadletters', 'audit', 'schedules', 'tasks'].forEach(function(t) {
      document.getElementById('panel-' + t).style.display = name === t ? '' : 'none';
      var tabEl = document.getElementById('tab-' + t);
      if (tabEl) tabEl.classList.toggle('tab-btn--active', name === t);
    });
    if (name === 'audit') fetchAudit();
  }

  function renderSchedules() {
    var tbody = document.getElementById('schedules-tbody');
    if (!tbody) return;
    var entries = state.schedules || [];
    var scnt = document.getElementById('tab-schedules-count');
    if (scnt) scnt.textContent = entries.length > 0 ? entries.length : '';
    if (entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#9ca3af;padding:40px;font-size:0.9rem">No scheduled tasks registered.</td></tr>';
      return;
    }
    tbody.innerHTML = entries.map(function(e) {
      var statusHtml = e.last_status
        ? '<span class="badge badge--' + e.last_status + '">' + esc(e.last_status) + '</span>'
        : '<span class="muted">\u2014</span>';
      var nextRun = e.next_run ? fmtDate(e.next_run) : '\u2014';
      return '<tr>'
        + '<td class="td td--func">' + esc(e.func_name) + '</td>'
        + '<td class="td"><code>' + esc(e.trigger) + '</code></td>'
        + '<td class="td">' + nextRun + '</td>'
        + '<td class="td">' + statusHtml + '</td>'
        + '</tr>';
    }).join('');
  }

  function syncPauseBtn() {
    var btn = document.getElementById('pause-btn');
    if (!btn) return;
    if (paused) {
      btn.classList.add('pause-btn--paused');
      btn.title = 'Resume live updates';
      btn.innerHTML =
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>'
        + ' Resume' + (pendingNewTasks > 0 ? ' <span class="new-badge">+' + pendingNewTasks + '</span>' : '');
    } else {
      btn.classList.remove('pause-btn--paused');
      btn.title = 'Pause live updates';
      btn.innerHTML =
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
        + ' Pause';
    }
  }

  function togglePause() {
    paused = !paused;
    localStorage.setItem('tf-paused', paused ? '1' : '0');
    if (paused) {
      pendingNewTasks = 0;
      pendingState    = null;
    } else {
      if (pendingState) {
        applyState(pendingState);
        pendingState    = null;
        pendingNewTasks = 0;
      }
    }
    syncPauseBtn();
  }

  function setStatus(s) {
    const dot = document.getElementById('live-dot');
    const lbl = document.getElementById('live-label');
    dot.className = 'dot dot--' + s;
    lbl.className = 'status-label status-label--' + s;
    lbl.textContent = s === 'live' ? 'Live' : s === 'error' ? 'Disconnected' : 'Connecting\u2026';
  }

  // ── Metrics ────────────────────────────────────────────────────
  function renderMetrics() {
    const m = state.metrics;
    const map = {
      total:        m.total       ?? 0,
      pending:      m.pending     ?? 0,
      running:      m.running     ?? 0,
      success:      m.success     ?? 0,
      failed:       m.failed      ?? 0,
      interrupted:  m.interrupted ?? 0,
      cancelled:    m.cancelled   ?? 0,
      rate:    m.success_rate    != null ? m.success_rate    + '%'  : '\u2014',
      avg:     m.avg_duration_ms != null ? m.avg_duration_ms + ' ms': '\u2014',
    };
    for (const [k, v] of Object.entries(map)) {
      const el = document.getElementById('metric-' + k);
      if (el) el.textContent = v;
    }
  }

  // ── Filter + Sort ──────────────────────────────────────────────
  function timeFilterCutoff() {
    var v = parseInt(timeFilterVal, 10);
    if (!v || v <= 0) return null;
    var msMap = { min: 60000, hour: 3600000, day: 86400000 };
    var ms = msMap[timeFilterUnit] || 3600000;
    return Date.now() - (v * ms);
  }

  function filteredSorted() {
    var cutoff = timeFilterCutoff();
    var q = searchQuery.trim().toLowerCase();
    return state.tasks
      .filter(function(t) {
        if (statusFilter !== 'all' && t.status !== statusFilter) return false;
        if (funcFilter   !== 'all' && t.func_name !== funcFilter) return false;
        if (cutoff !== null) {
          var ts = t.created_at ? new Date(t.created_at).getTime() : 0;
          if (ts < cutoff) return false;
        }
        if (q) {
          if (!t.task_id.toLowerCase().includes(q) && !t.func_name.toLowerCase().includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => {
        let va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
        if (typeof va === 'string') { va = va.toLowerCase(); vb = String(vb).toLowerCase(); }
        if (va < vb) return sortDir === 'asc' ? -1 : 1;
        if (va > vb) return sortDir === 'asc' ?  1 : -1;
        return 0;
      });
  }

  // ── Bulk selection helpers ─────────────────────────────────────
  function isRetryable(t) { return t.status === 'failed' || t.status === 'interrupted'; }

  function updateBulkBar() {
    var bar = document.getElementById('bulk-bar');
    var lbl = document.getElementById('bulk-label');
    var n   = selectedIds.size;
    if (n > 0) {
      bar.classList.add('bulk-bar--on');
      lbl.textContent = n + ' task' + (n !== 1 ? 's' : '') + ' selected';
    } else {
      bar.classList.remove('bulk-bar--on');
    }
    // Update select-all checkbox state
    var allCheck = document.getElementById('select-all-check');
    if (allCheck) {
      var pageRetryable = Array.from(document.querySelectorAll('.row-check:not(#select-all-check)')).filter(function(c) { return !c.disabled; });
      allCheck.checked       = pageRetryable.length > 0 && pageRetryable.every(function(c) { return c.checked; });
      allCheck.indeterminate = !allCheck.checked && pageRetryable.some(function(c) { return c.checked; });
    }
  }

  function toggleSelect(checkbox, taskId) {
    if (checkbox.checked) { selectedIds.add(taskId); } else { selectedIds.delete(taskId); }
    updateBulkBar();
  }

  function toggleSelectAll(masterCb) {
    document.querySelectorAll('.row-check:not(#select-all-check)').forEach(function(cb) {
      if (!cb.disabled) {
        cb.checked = masterCb.checked;
        var id = cb.dataset.id;
        if (masterCb.checked) { selectedIds.add(id); } else { selectedIds.delete(id); }
      }
    });
    updateBulkBar();
  }

  function clearSelection() {
    selectedIds.clear();
    document.querySelectorAll('.row-check:not(#select-all-check)').forEach(function(cb) { cb.checked = false; });
    var allCheck = document.getElementById('select-all-check');
    if (allCheck) { allCheck.checked = false; allCheck.indeterminate = false; }
    updateBulkBar();
  }

  function bulkRetry() {
    var ids = Array.from(selectedIds);
    if (!ids.length) return;
    var btn = document.getElementById('bulk-retry-btn');
    btn.disabled = true;
    btn.textContent = 'Retrying ' + ids.length + '...';
    var done = 0, failed = 0;
    ids.forEach(function(taskId) {
      fetch(TASKS_PREFIX + '/' + taskId + '/retry', { method: 'POST' })
        .then(function(r) { if (!r.ok) throw new Error(); })
        .catch(function() { failed++; })
        .finally(function() {
          done++;
          if (done === ids.length) {
            btn.disabled = false;
            btn.textContent = 'Retry selected';
            clearSelection();
            if (failed) showToast(failed + ' retries failed');
          }
        });
    });
  }

  // ── Time filter popup ──────────────────────────────────────────
  function openTimeFilterPopup() {
    document.getElementById('tf-popup-val').value = timeFilterVal;
    document.getElementById('tf-popup-unit').value = timeFilterUnit;
    document.getElementById('time-filter-popup').classList.add('popup-backdrop--open');
    document.getElementById('tf-popup-val').focus();
  }
  function closeTimeFilterPopup(e) {
    if (!e || e.target === document.getElementById('time-filter-popup')) {
      document.getElementById('time-filter-popup').classList.remove('popup-backdrop--open');
    }
  }
  function applyTimeFilter() {
    var val = document.getElementById('tf-popup-val').value;
    var unit = document.getElementById('tf-popup-unit').value;
    timeFilterVal = val; timeFilterUnit = unit; currentPage = 0;
    localStorage.setItem('tf-time-val', val);
    localStorage.setItem('tf-time-unit', unit);
    var btn = document.getElementById('time-filter-btn');
    var label = document.getElementById('time-filter-label');
    if (val && parseInt(val, 10) > 0) {
      var unitLabels = { min: 'min', hour: 'hr', day: 'd' };
      label.textContent = 'Last ' + val + unitLabels[unit];
      btn.classList.add('filter-trigger-btn--active');
    } else {
      label.textContent = 'All time';
      btn.classList.remove('filter-trigger-btn--active');
    }
    document.getElementById('time-filter-popup').classList.remove('popup-backdrop--open');
    renderTable();
  }
  function clearTimeFilter() {
    document.getElementById('tf-popup-val').value = '';
    applyTimeFilter();
  }

  // ── Clear history popup ────────────────────────────────────────
  function openClearHistoryPopup() {
    document.getElementById('ch-popup-val').value = '';
    document.getElementById('clear-history-popup').classList.add('popup-backdrop--open');
    document.getElementById('ch-popup-val').focus();
  }
  function closeClearHistoryPopup(e) {
    if (!e || e.target === document.getElementById('clear-history-popup')) {
      document.getElementById('clear-history-popup').classList.remove('popup-backdrop--open');
    }
  }
  function confirmClearHistory() {
    var val = parseInt(document.getElementById('ch-popup-val').value, 10);
    var unit = document.getElementById('ch-popup-unit').value;
    if (!val || val <= 0) { showToast('Enter a number first'); return; }
    document.getElementById('clear-history-popup').classList.remove('popup-backdrop--open');
    fetch(TASKS_PREFIX + '/history?value=' + val + '&unit=' + unit, { method: 'DELETE' })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        showToast('Deleted ' + data.deleted + ' task' + (data.deleted !== 1 ? 's' : ''));
      })
      .catch(function() { showToast('Failed to clear history'); });
  }

  // ── Table ──────────────────────────────────────────────────────
  function renderTable() {
    var all   = filteredSorted();
    var total = all.length;

    // Clamp page
    var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (currentPage >= totalPages) currentPage = totalPages - 1;

    var tasks = all.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

    var tbody = document.getElementById('tasks-tbody');
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No tasks match the current filters.</td></tr>';
      renderPagination(total);
      updateBulkBar();
      return;
    }

    tbody.innerHTML = tasks.map(function(t) {
      var dur  = t.duration != null ? (t.duration * 1000).toFixed(0) + ' ms' : '\u2014';
      var date = t.created_at ? fmtDate(t.created_at) : '\u2014';
      var err  = t.error
        ? '<span class="err-text" title="' + esc(t.error) + '">' + esc(t.error.slice(0,48)) + (t.error.length > 48 ? '\u2026' : '') + '</span>'
        : '<span class="muted">\u2014</span>';
      var sel = t.task_id === selectedId ? ' row--selected' : '';
      var retryable = isRetryable(t);
      var checked   = selectedIds.has(t.task_id) ? ' checked' : '';
      var disabled  = retryable ? '' : ' disabled';
      var checkCell = '<td class="td td--check" onclick="event.stopPropagation()">'
        + '<input type="checkbox" class="row-check" data-id="' + esc(t.task_id) + '"'
        + checked + disabled + ' onchange="toggleSelect(this, this.dataset.id)">'
        + '</td>';
      return '<tr class="row' + sel + '" onclick="openDetail(this.dataset.id)" data-id="' + esc(t.task_id) + '">'
        + checkCell
        + '<td class="td td--mono" style="white-space:nowrap">' + esc(t.task_id.slice(0,8)) + '\u2026'
        + '<button class="copy-btn copy-btn--row" data-val="' + esc(t.task_id) + '" onclick="event.stopPropagation();copyId(this)" title="Copy full ID"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>'
        + '</td>'
        + '<td class="td td--func">' + esc(t.func_name) + (t.source === 'scheduled' ? ' <span class="source-badge">scheduled</span>' : '') + '</td>'
        + '<td class="td">'           + badge(t.status) + '</td>'
        + '<td class="td">'     + dur + '</td>'
        + '<td class="td">'     + t.retries_used + '</td>'
        + '<td class="td td--date">'  + date + '</td>'
        + '<td class="td">'     + (t.priority != null ? priorityBadge(t.priority) : '<span class="muted">—</span>') + '</td>'
        + '<td class="td td--err">'   + err + '</td>'
        + '</tr>';
    }).join('');

    renderPagination(total);
    updateBulkBar();
  }

  // ── Pagination ────────────────────────────────────────────────
  function renderPagination(total) {
    const el = document.getElementById('pagination');
    if (!el) return;
    const totalPages = Math.ceil(total / PAGE_SIZE);
    if (totalPages <= 1) { el.innerHTML = ''; return; }
    const start = currentPage * PAGE_SIZE + 1;
    const end   = Math.min((currentPage + 1) * PAGE_SIZE, total);
    el.innerHTML =
      '<div class="pagination">'
      + '<button class="pg-btn" onclick="goPage(' + (currentPage - 1) + ')"'
      +   (currentPage === 0 ? ' disabled' : '') + '>\u2190 Prev</button>'
      + '<span class="pg-info">' + start + '\u2013' + end + ' of ' + total + '</span>'
      + '<button class="pg-btn" onclick="goPage(' + (currentPage + 1) + ')"'
      +   (currentPage >= totalPages - 1 ? ' disabled' : '') + '>Next \u2192</button>'
      + '</div>';
  }

  function goPage(p) { currentPage = p; renderTable(); }

  function badge(status) {
    var cls = { pending:'badge--pending', running:'badge--running', success:'badge--success', failed:'badge--failed', interrupted:'badge--interrupted', cancelled:'badge--cancelled' };
    return '<span class="badge ' + (cls[status] || 'badge--pending') + '">' + esc(status) + '</span>';
  }

  function priorityBadge(p) {
    // Color scale: high priority (>=8) teal, medium (5-7) amber, low (<=4) gray.
    var bg = p >= 8 ? 'rgba(0,150,136,.12)' : p >= 5 ? 'rgba(245,158,11,.12)' : 'rgba(107,114,128,.1)';
    var fg = p >= 8 ? '#009688' : p >= 5 ? '#b45309' : '#6b7280';
    return '<span style="background:' + bg + ';color:' + fg + ';padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600;font-variant-numeric:tabular-nums">' + p + '</span>';
  }

  function showToast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.style.opacity = '1';
    t.style.transform = 'translateY(0)';
    setTimeout(function() { t.style.opacity = '0'; t.style.transform = 'translateY(8px)'; }, 3000);
  }

  // ── Sort ───────────────────────────────────────────────────────
  function setSort(col) {
    if (sortCol === col) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = col;
      sortDir = (col === 'created_at' || col === 'duration' || col === 'priority') ? 'desc' : 'asc';
    }
    localStorage.setItem('tf-sort-col', sortCol);
    localStorage.setItem('tf-sort-dir', sortDir);
    document.querySelectorAll('[data-sort]').forEach(function(th) {
      var c = th.dataset.sort;
      th.querySelector('.sort-icon').textContent = c !== sortCol ? '\u21d5' : sortDir === 'asc' ? '\u2191' : '\u2193';
      th.classList.toggle('th--active', c === sortCol);
    });
    currentPage = 0;
    renderTable();
  }

  // ── Function filter ────────────────────────────────────────────
  function populateFuncFilter() {
    const sel  = document.getElementById('func-filter');
    const prev = sel.value;
    const fns  = [...new Set(state.tasks.map(t => t.func_name))].sort();
    sel.innerHTML = '<option value="all">All functions</option>'
      + fns.map(f => '<option value="' + esc(f) + '"' + (f === prev ? ' selected' : '') + '>' + esc(f) + '</option>').join('');
  }

  // ── Detail Panel ───────────────────────────────────────────────
  function openDetail(taskId) {
    selectedId = taskId;
    const task = state.tasks.find(t => t.task_id === taskId);
    if (!task) return;
    renderDetail(task);
    document.getElementById('detail-panel').classList.add('panel--open');
    document.getElementById('detail-backdrop').classList.add('backdrop--on');
    document.body.style.overflow = 'hidden';
    document.querySelectorAll('.row').forEach(r => r.classList.toggle('row--selected', r.dataset.id === taskId));
  }

  function closeDetail() {
    selectedId = null;
    document.getElementById('detail-panel').classList.remove('panel--open');
    document.getElementById('detail-backdrop').classList.remove('backdrop--on');
    document.getElementById('detail-tabs').innerHTML = '';
    document.body.style.overflow = '';
    document.querySelectorAll('.row').forEach(r => r.classList.remove('row--selected'));
  }

  function renderDetail(task) {
    const ft = state.tasks.filter(t => t.func_name === task.func_name);
    const fTotal   = ft.length;
    const fSuccess = ft.filter(t => t.status === 'success').length;
    const fFailed  = ft.filter(t => t.status === 'failed').length;
    const fRunning = ft.filter(t => t.status === 'running').length;
    const fPending = ft.filter(t => t.status === 'pending').length;
    const fDurs    = ft.filter(t => t.duration != null).map(t => t.duration * 1000);
    const fAvg  = fDurs.length ? (fDurs.reduce((a,b)=>a+b,0)/fDurs.length).toFixed(0) : null;
    const fMin  = fDurs.length ? Math.min(...fDurs).toFixed(0) : null;
    const fMax  = fDurs.length ? Math.max(...fDurs).toFixed(0) : null;
    const fP95  = fDurs.length >= 2 ? (function(){ var s=[...fDurs].sort(function(a,b){return a-b;}); return s[Math.floor(s.length*0.95)].toFixed(0); })() : null;
    const fRate = fTotal ? (fSuccess / fTotal * 100).toFixed(1) : null;
    const rateCls = fRate == null ? 'neutral' : fRate >= 80 ? 'success' : fRate >= 50 ? 'warning' : 'danger';

    const recent = [...ft].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||'')).slice(0,5);

    const dur     = task.duration   != null ? (task.duration * 1000).toFixed(0) + ' ms' : '\u2014';
    const created = task.created_at ? fmtDate(task.created_at) : '\u2014';
    const started = task.start_time ? fmtDate(task.start_time) : '\u2014';
    const ended   = task.end_time   ? fmtDate(task.end_time)   : '\u2014';

    const hasLogs  = task.logs  && task.logs.length;
    const hasError = task.error;

    // ── Build header tabs ────────────────────────────────────────
    const tabsEl = document.getElementById('detail-tabs');
    tabsEl.innerHTML = '';

    function makeTab(label, panelId, extraClass) {
      var btn = document.createElement('button');
      btn.className = 'panel-tab' + (extraClass ? ' ' + extraClass : '');
      btn.dataset.panel = panelId;
      btn.onclick = function() { switchTab(this); };
      btn.textContent = label;
      tabsEl.appendChild(btn);
      return btn;
    }

    makeTab('Details', 'panel-details').classList.add('panel-tab--active');
    if (hasLogs)  makeTab('Logs (' + task.logs.length + ')', 'panel-logs');
    if (hasError) makeTab('Error', 'panel-error', 'panel-tab--error');

    // ── Details panel ────────────────────────────────────────────
    var detailsHtml =
      '<div class="d-section"><div class="d-label">Task ID</div>'
      + '<div style="display:flex;align-items:center;gap:8px;margin-top:4px">'
      + '<div class="d-mono" style="flex:1;word-break:break-all">' + esc(task.task_id) + '</div>'
      + '<button class="copy-btn" onclick="copyId(this)" data-val="' + esc(task.task_id) + '" title="Copy task ID">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
      + '<rect x="9" y="9" width="13" height="13" rx="2"/>'
      + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
      + '</svg></button>'
      + '</div></div>'

      + '<div class="d-row2">'
      + '<div class="d-section"><div class="d-label">Function</div><div class="d-val d-func">' + esc(task.func_name) + '</div></div>'
      + '<div class="d-section"><div class="d-label">Status</div><div>' + badge(task.status) + '</div></div>'
      + '</div>'

      + ((task.status === 'pending' || task.status === 'running') ?
          '<div class="d-section" style="margin-top:4px">'
          + '<button class="cancel-btn" id="cancel-btn-' + esc(task.task_id) + '" data-task-id="' + esc(task.task_id) + '" onclick="cancelTask(this.dataset.taskId, this)">'
          + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
          + 'Cancel task'
          + '</button>'
          + (task.status === 'running' ?
              '<div style="font-size:11px;color:var(--db-text-muted);margin-top:5px">Async tasks are cancelled immediately. Sync tasks stop waiting but the underlying thread runs to completion.</div>'
            : '')
          + '</div>'
        : '')

      + ((task.status === 'failed' || task.status === 'interrupted') ?
          '<div class="d-section" style="margin-top:4px">'
          + '<button class="retry-btn retry-btn--warn" id="retry-btn-' + esc(task.task_id) + '" data-task-id="' + esc(task.task_id) + '" onclick="retryTask(this.dataset.taskId, this)">'
          + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.5"/></svg>'
          + 'Retry this task'
          + '</button>'
          + (task.status === 'interrupted' ?
              '<div style="font-size:11px;color:var(--db-text-muted);margin-top:5px">This task was mid-execution when the app shut down. Retry only if you are sure the function did not already complete its side effects.</div>'
            : '')
          + '</div>'
        : '')

      + '<div class="d-row3">'
      + dSec('Created', '<span style="font-size:12px">' + created + '</span>')
      + dSec('Started', '<span style="font-size:12px">' + started + '</span>')
      + dSec('Ended',   '<span style="font-size:12px">' + ended   + '</span>')
      + '</div>'

      + '<div class="d-row3">'
      + dSec('Duration',     '<span class="d-val">' + dur + '</span>')
      + dSec('Retries Used', '<span class="d-val">' + task.retries_used + '</span>')
      + dSec('Priority', task.priority != null ? priorityBadge(task.priority) : '<span class="d-val muted">—</span>')
      + '</div>'

      + (SHOW_ARGS && ((task.args && task.args.length) || (task.kwargs && Object.keys(task.kwargs).length)) ?
          '<div class="divider"></div>'
          + '<div class="section-title">'
          + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>'
          + 'Arguments</div>'
          + (task.args && task.args.length ?
              '<div class="d-section"><div class="d-label">Positional args</div>'
              + task.args.map(function(a, i) {
                  return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
                    + '<span class="arg-idx">' + i + '</span>'
                    + '<code class="d-mono arg-code">' + esc(a) + '</code>'
                    + '</div>';
                }).join('')
              + '</div>'
            : '')
          + (task.kwargs && Object.keys(task.kwargs).length ?
              '<div class="d-section"><div class="d-label">Keyword args</div>'
              + Object.entries(task.kwargs).map(function(kv) {
                  return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
                    + '<span class="arg-key">' + esc(kv[0]) + '</span>'
                    + '<span class="arg-eq">=</span>'
                    + '<code class="d-mono arg-code">' + esc(kv[1]) + '</code>'
                    + '</div>';
                }).join('')
              + '</div>'
            : '')
        : '')

      + '<div class="divider"></div>'

      + '<div class="section-title">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>'
      + 'Function Analytics \u00b7 ' + esc(task.func_name) + '</div>'
      + '<div class="analytics-grid" style="margin-bottom:4px">'
      + aCard('Total Runs',    fTotal,                                        'neutral')
      + aCard('Success',       fSuccess,                                      'success')
      + aCard('Failed',        fFailed,  fFailed  > 0 ? 'danger'  : 'neutral')
      + aCard('Running',       fRunning, fRunning > 0 ? 'running' : 'neutral')
      + aCard('Success Rate',  fRate  != null ? fRate  + '%'  : '\u2014', rateCls)
      + aCard('Avg Duration',  fAvg   != null ? fAvg   + ' ms': '\u2014', 'neutral')
      + aCard('Min Duration',  fMin   != null ? fMin   + ' ms': '\u2014', 'neutral')
      + aCard('Max Duration',  fMax   != null ? fMax   + ' ms': '\u2014', 'neutral')
      + aCard('P95 Duration',  fP95   != null ? fP95   + ' ms': '\u2014', 'neutral')
      + '</div>'

      + '<div class="divider"></div>'

      + '<div class="section-title">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
      + 'Recent Runs \u00b7 ' + esc(task.func_name) + '</div>'
      + '<div class="recent-runs">'
      + recent.map(r =>
          '<div class="r-run' + (r.task_id === task.task_id ? ' r-run--cur' : '') + '" onclick="openDetail(this.dataset.id)" data-id="' + r.task_id + '">'
          + '<span class="d-mono" style="font-size:11px">' + esc(r.task_id.slice(0,8)) + '\u2026</span>'
          + badge(r.status)
          + '<span class="r-dur">'
          + (r.duration != null ? (r.duration*1000).toFixed(0)+' ms' : '\u2014')
          + '</span></div>'
        ).join('')
      + '</div>';

    // ── Logs panel ───────────────────────────────────────────────
    var logsHtml = hasLogs
      ? '<div class="d-logs">'
        + task.logs.map(function(line) {
            if (line.startsWith('--- ')) return '<div class="log-sep">' + esc(line) + '</div>';
            var ts = line.slice(0, 19), msg = line.slice(20);
            return '<div class="log-line"><span class="log-ts">' + esc(ts) + '</span><span>' + esc(msg) + '</span></div>';
          }).join('')
        + '</div>'
      : '';

    // ── Error panel ──────────────────────────────────────────────
    var errorHtml = hasError
      ? '<div class="d-error-msg">' + esc(task.error) + '</div>'
        + (task.stacktrace ? '<div class="d-stacktrace">' + esc(task.stacktrace) + '</div>' : '')
      : '';

    // ── Render all three panels into detail-content ──────────────
    document.getElementById('detail-content').innerHTML =
        '<div id="panel-details" class="d-tab-panel d-tab-panel--active">' + detailsHtml + '</div>'
      + (hasLogs  ? '<div id="panel-logs"  class="d-tab-panel">' + logsHtml  + '</div>' : '')
      + (hasError ? '<div id="panel-error" class="d-tab-panel">' + errorHtml + '</div>' : '');
  }

  function switchTab(btn) {
    var panelId = btn.dataset.panel;
    document.getElementById('detail-tabs').querySelectorAll('.panel-tab').forEach(function(t) {
      t.classList.remove('panel-tab--active');
    });
    document.getElementById('detail-content').querySelectorAll('.d-tab-panel').forEach(function(p) {
      p.classList.remove('d-tab-panel--active');
    });
    btn.classList.add('panel-tab--active');
    var el = document.getElementById(panelId);
    if (el) el.classList.add('d-tab-panel--active');
  }

  function dSec(label, inner) {
    return '<div class="d-section"><div class="d-label">' + label + '</div>' + inner + '</div>';
  }

  function aCard(label, value, cls) {
    return '<div class="a-card"><div class="a-label">' + label + '</div>'
      + '<div class="a-value a-value--' + cls + '">' + value + '</div></div>';
  }

  // ── Toast ──────────────────────────────────────────────────────
  let _toastTimer = null;
  function showToast(msg) {
    const el = document.getElementById('toast');
    el.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#17c964" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> ' + msg;
    el.classList.add('toast--on');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function() { el.classList.remove('toast--on'); }, 2200);
  }

  // ── Copy ───────────────────────────────────────────────────────
  function copyId(btn) {
    navigator.clipboard.writeText(btn.dataset.val).then(function() {
      const prev = btn.innerHTML;
      btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      btn.style.color = '#16a34a';
      btn.style.borderColor = '#bbf7d0';
      setTimeout(function() { btn.innerHTML = prev; btn.style.color = ''; btn.style.borderColor = ''; }, 1500);
      showToast('Task ID copied to clipboard');
    });
  }

  // ── CSV Export ────────────────────────────────────────────────
  function exportCsv() {
    var tasks = filteredSorted();
    var headers = ['ID', 'Function', 'Status', 'Duration (ms)', 'Retries', 'Created', 'Error'];
    function csvCell(v) {
      var s = String(v ?? '');
      if (s.includes(',') || s.includes('"') || s.includes('\n') || s.includes('\r')) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }
    var rows = [headers].concat(tasks.map(function(t) {
      return [
        t.task_id,
        t.func_name,
        t.status,
        t.duration != null ? (t.duration * 1000).toFixed(0) : '',
        t.retries_used,
        t.created_at || '',
        t.error ? t.error.replace(/[\r\n]+/g, ' ') : '',
      ].map(csvCell);
    }));
    var csv = rows.map(function(r) { return r.join(','); }).join('\r\n');
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'tasks-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Exporting ' + tasks.length + ' task' + (tasks.length !== 1 ? 's' : ''));
  }

  // ── Utilities ──────────────────────────────────────────────────
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function fmtDate(iso) {
    try {
      return new Date(iso).toLocaleString(undefined, { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit' });
    } catch(e) { return iso; }
  }

  // ── Event listeners ────────────────────────────────────────────
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      closeTimeFilterPopup();
      closeClearHistoryPopup();
    }
  });
  document.getElementById('tf-popup-val').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') applyTimeFilter();
  });
  document.getElementById('search-input').addEventListener('input', function(e) {
    searchQuery = e.target.value; currentPage = 0;
    localStorage.setItem('tf-search', searchQuery); renderTable();
  });
  document.getElementById('status-filter').addEventListener('change', function(e) {
    statusFilter = e.target.value; currentPage = 0;
    localStorage.setItem('tf-status', statusFilter); renderTable();
  });
  document.getElementById('func-filter').addEventListener('change', function(e) {
    funcFilter = e.target.value; currentPage = 0;
    localStorage.setItem('tf-func', funcFilter); renderTable();
  });
  document.getElementById('detail-close').addEventListener('click', closeDetail);
  document.getElementById('detail-backdrop').addEventListener('click', closeDetail);

  // ── Restore persisted filter UI state ──────────────────────────
  (function() {
    var si = document.getElementById('search-input');
    if (si) si.value = searchQuery;
    var sf = document.getElementById('status-filter');
    if (sf) sf.value = statusFilter;
    // Restore time filter button label
    if (timeFilterVal && parseInt(timeFilterVal, 10) > 0) {
      var unitLabels = { min: 'min', hour: 'hr', day: 'd' };
      var btn = document.getElementById('time-filter-btn');
      var label = document.getElementById('time-filter-label');
      if (label) label.textContent = 'Last ' + timeFilterVal + unitLabels[timeFilterUnit];
      if (btn) btn.classList.add('filter-trigger-btn--active');
    }
    // Show audit tab only when auth is enabled
    if (SHOW_AUDIT) {
      var auditTab = document.getElementById('tab-audit');
      if (auditTab) auditTab.style.display = '';
    }
    // Sort indicators
    document.querySelectorAll('[data-sort]').forEach(function(th) {
      var c = th.dataset.sort;
      var icon = th.querySelector('.sort-icon');
      if (icon) icon.textContent = c !== sortCol ? '\u21d5' : sortDir === 'asc' ? '\u2191' : '\u2193';
      th.classList.toggle('th--active', c === sortCol);
    });
  })();

  // ── Theme ──────────────────────────────────────────────────────
  function toggleTheme() {
    var cur = document.documentElement.getAttribute('data-theme') || 'light';
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('tf-theme', next);
    updateThemeBtn(next);
  }
  function updateThemeBtn(theme) {
    var btn = document.getElementById('theme-btn');
    if (!btn) return;
    btn.title = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    btn.innerHTML = theme === 'dark'
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  }

  function retryTask(taskId, btn) {
    btn.disabled = true;
    btn.textContent = 'Retrying...';
    fetch(TASKS_PREFIX + '/' + taskId + '/retry', { method: 'POST' })
      .then(function(res) {
        if (!res.ok) {
          return res.json().then(function(body) {
            throw new Error(body.detail || 'Retry failed');
          });
        }
        return res.json();
      })
      .then(function(data) {
        btn.textContent = 'Queued as ' + data.task_id.slice(0, 8) + '...';
        btn.style.borderColor = '#16a34a';
        btn.style.color = '#16a34a';
      })
      .catch(function(err) {
        btn.disabled = false;
        btn.textContent = 'Retry this task';
        alert('Could not retry task: ' + err.message);
      });
  }

  // ── Registered Tasks tab ──────────────────────────────────────
  function renderRegisteredTasks() {
    var container = document.getElementById('registered-tasks-list');
    if (!container) return;
    var tcnt = document.getElementById('tab-tasks-count');
    if (!REGISTERED_TASKS || REGISTERED_TASKS.length === 0) {
      container.innerHTML = '<div class="empty">No tasks registered.</div>';
      return;
    }
    if (tcnt) tcnt.textContent = REGISTERED_TASKS.length;
    var cards = REGISTERED_TASKS.map(function(t) {
      var pills = [];
      pills.push(rtaskPill(t.retries + (t.retries === 1 ? ' retry' : ' retries'), t.retries > 0));
      if (t.delay > 0) pills.push(rtaskPill(t.delay + 's delay', false));
      if (t.backoff !== 1.0 && t.delay > 0) pills.push(rtaskPill(t.backoff + 'x backoff', false));
      if (t.persist) pills.push(rtaskPill('persist', true));
      if (t.requeue_on_interrupt) pills.push(rtaskPill('requeue', true));
      if (t.eager) pills.push(rtaskPill('eager', true));
      if (t.priority != null) pills.push(rtaskPill('priority ' + t.priority, true));
      return '<div class="rtask-card">'
        + '<div class="rtask-name">' + esc(t.name) + '</div>'
        + '<div class="rtask-pills">' + pills.join('') + '</div>'
        + '</div>';
    });
    container.innerHTML = '<div class="rtask-grid">' + cards.join('') + '</div>';
  }

  function rtaskPill(text, accent) {
    return '<span class="rtask-pill' + (accent ? ' rtask-pill--accent' : '') + '">' + esc(text) + '</span>';
  }


  // ── Dead Letters ───────────────────────────────────────────────
  var dlqSelected = new Set();

  function dlqToggleRow(id, cb, evt) {
    evt && evt.stopPropagation();
    if (cb.checked) dlqSelected.add(id);
    else dlqSelected.delete(id);
    dlqUpdateBar();
  }

  function dlqToggleAll(cb) {
    document.querySelectorAll('.dlq-row-check').forEach(function(c) {
      c.checked = cb.checked;
      var id = c.dataset.id;
      if (cb.checked) dlqSelected.add(id);
      else dlqSelected.delete(id);
    });
    dlqUpdateBar();
  }

  function dlqUpdateBar() {
    var bar   = document.getElementById('dlq-bulk-bar');
    var label = document.getElementById('dlq-bulk-label');
    if (!bar) return;
    if (dlqSelected.size > 0) {
      bar.classList.add('bulk-bar--on');
      label.textContent = dlqSelected.size + (dlqSelected.size === 1 ? ' task selected' : ' tasks selected');
    } else {
      bar.classList.remove('bulk-bar--on');
    }
  }

  function dlqClearSelection() {
    dlqSelected.clear();
    document.querySelectorAll('.dlq-row-check').forEach(function(c) { c.checked = false; });
    var all = document.getElementById('dlq-select-all');
    if (all) all.checked = false;
    dlqUpdateBar();
  }

  function dlqReplaySelected() {
    var ids = Array.from(dlqSelected);
    if (!ids.length) return;
    fetch(TASKS_PREFIX + '/bulk-retry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_ids: ids})
    }).then(function(r) {
      return r.json().then(function(data) {
        if (!r.ok) { showToast('Replay failed: ' + (data.detail || 'unknown error')); return; }
        dlqClearSelection();
        showToast('Replayed ' + data.dispatched + ' task(s)' + (data.skipped ? ', ' + data.skipped + ' skipped' : ''));
      });
    }).catch(function(e) { showToast('Network error'); });
  }

  function dlqReplayWindow() {
    var since = document.getElementById('dlq-window-select').value;
    var labels = {'1h':'last 1 hour','6h':'last 6 hours','24h':'last 24 hours','7d':'last 7 days','all':'all time'};
    var desc = document.getElementById('dlq-replay-popup-desc');
    if (desc) desc.textContent = 'Re-enqueue all failed tasks from the ' + (labels[since] || since) + '. Each task will run again with its original arguments.';
    document.getElementById('dlq-replay-popup').classList.add('popup-backdrop--open');
  }

  function closeDlqReplayPopup(e) {
    if (e && e.target !== document.getElementById('dlq-replay-popup')) return;
    document.getElementById('dlq-replay-popup').classList.remove('popup-backdrop--open');
  }

  function confirmDlqReplayWindow() {
    var since = document.getElementById('dlq-window-select').value;
    var btn = document.getElementById('dlq-replay-confirm-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Replaying…'; }
    document.getElementById('dlq-replay-popup').classList.remove('popup-backdrop--open');
    fetch(TASKS_PREFIX + '/retry-failed?since=' + encodeURIComponent(since), {
      method: 'POST'
    }).then(function(r) {
      return r.json().then(function(data) {
        if (btn) { btn.disabled = false; btn.textContent = 'Replay'; }
        if (!r.ok) { showToast('Replay failed: ' + (data.detail || 'unknown error')); return; }
        showToast('Replayed ' + data.dispatched + ' task(s)' + (data.skipped ? ', ' + data.skipped + ' skipped' : ''));
      });
    }).catch(function(e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Replay'; }
      showToast('Network error');
    });
  }

  function renderDeadLetters() {
    var tbody = document.getElementById('deadletters-tbody');
    var cnt   = document.getElementById('tab-deadletters-count');
    if (!tbody) return;
    var failed = state.tasks.filter(function(t) { return t.status === 'failed'; });
    failed.sort(function(a, b) { return (b.created_at || '').localeCompare(a.created_at || ''); });
    if (cnt) cnt.textContent = failed.length > 0 ? failed.length : '';
    dlqSelected.clear();
    dlqUpdateBar();
    if (!failed.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No failed tasks.</td></tr>';
      return;
    }
    tbody.innerHTML = failed.map(function(t) {
      var dur  = t.duration != null ? (t.duration * 1000).toFixed(0) + ' ms' : '\u2014';
      var date = t.created_at ? fmtDate(t.created_at) : '\u2014';
      var err  = t.error
        ? '<span class="err-text" title="' + esc(t.error) + '">' + esc(t.error.slice(0,60)) + (t.error.length > 60 ? '\u2026' : '') + '</span>'
        : '<span class="muted">\u2014</span>';
      var chk  = '<td class="td td--check" onclick="event.stopPropagation()">'
        + '<input type="checkbox" class="row-check dlq-row-check" data-id="' + esc(t.task_id) + '"'
        + ' onclick="dlqToggleRow(\'' + esc(t.task_id) + '\', this, event)">'
        + '</td>';
      return '<tr class="row" onclick="openDetail(this.dataset.id)" data-id="' + esc(t.task_id) + '">'
        + chk
        + '<td class="td td--mono">' + esc(t.task_id.slice(0,8)) + '\u2026</td>'
        + '<td class="td td--func">' + esc(t.func_name) + '</td>'
        + '<td class="td">'           + badge(t.status) + '</td>'
        + '<td class="td">'           + dur + '</td>'
        + '<td class="td td--date">'  + date + '</td>'
        + '<td class="td td--err">'   + err + '</td>'
        + '</tr>';
    }).join('');
  }

  // ── Audit log ─────────────────────────────────────────────────
  function fetchAudit() {
    var tbody = document.getElementById('audit-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">Loading\u2026</td></tr>';
    fetch(TASKS_PREFIX + '/audit')
      .then(function(r) { return r.json(); })
      .then(function(entries) {
        var cnt = document.getElementById('tab-audit-count');
        if (cnt) cnt.textContent = entries.length > 0 ? entries.length : '';
        if (!entries.length) {
          tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">No audit entries yet.</td></tr>';
          return;
        }
        tbody.innerHTML = entries.map(function(e) {
          var detail = e.detail && Object.keys(e.detail).length
            ? Object.entries(e.detail).map(function(kv){ return kv[0] + ': ' + String(kv[1]).slice(0,8) + '\u2026'; }).join(', ')
            : '\u2014';
          var actionCls = e.action === 'cancel' ? 'color:#dc2626' : 'color:#7c3aed';
          return '<tr>'
            + '<td class="td" style="white-space:nowrap;font-size:12px">' + esc(fmtDate(e.timestamp)) + '</td>'
            + '<td class="td"><span style="font-weight:600;' + actionCls + '">' + esc(e.action) + '</span></td>'
            + '<td class="td td--mono" style="cursor:pointer" onclick="openDetail(\'' + esc(e.task_id) + '\')">' + esc(e.task_id.slice(0,8)) + '\u2026</td>'
            + '<td class="td">' + esc(e.actor) + '</td>'
            + '<td class="td" style="font-size:12px;color:var(--db-text-muted)">' + esc(detail) + '</td>'
            + '</tr>';
        }).join('');
      })
      .catch(function() {
        tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">Failed to load audit log.</td></tr>';
      });
  }

  // ── Cancel task ────────────────────────────────────────────────
  function cancelTask(taskId, btn) {
    btn.disabled = true;
    btn.textContent = 'Cancelling\u2026';
    fetch(TASKS_PREFIX + '/' + taskId + '/cancel', { method: 'POST' })
      .then(function(res) {
        if (!res.ok) return res.json().then(function(b) { throw new Error(b.detail || 'Cancel failed'); });
        return res.json();
      })
      .then(function() {
        btn.textContent = 'Cancelled';
        btn.style.borderColor = '#be185d';
        btn.style.color = '#be185d';
        showToast('Task cancelled');
      })
      .catch(function(err) {
        btn.disabled = false;
        btn.textContent = 'Cancel task';
        showToast('Could not cancel: ' + err.message);
      });
  }


  // ── Panel resize ───────────────────────────────────────────────
  (function() {
    var panel  = document.getElementById('detail-panel');
    var handle = document.getElementById('panel-resize-handle');
    if (!panel || !handle) return;

    var MIN_W = 320;
    function maxW() { return Math.min(900, Math.round(window.innerWidth * 0.82)); }

    // Restore persisted width
    var saved = parseInt(localStorage.getItem('tf-panel-width'), 10);
    if (saved && saved >= MIN_W && saved <= maxW()) {
      panel.style.width = saved + 'px';
    }

    handle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      handle.classList.add('panel-resize--active');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      // Disable the open/close transition while dragging so movement is instant
      panel.style.transition = 'none';

      function onMove(e) {
        var w = Math.max(MIN_W, Math.min(maxW(), window.innerWidth - e.clientX));
        panel.style.width = w + 'px';
      }

      function onUp() {
        handle.classList.remove('panel-resize--active');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        panel.style.transition = '';
        var w = parseInt(panel.style.width, 10);
        if (w >= MIN_W) localStorage.setItem('tf-panel-width', w);
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Double-click the handle to reset to the default width
    handle.addEventListener('dblclick', function() {
      panel.style.width = '440px';
      localStorage.removeItem('tf-panel-width');
    });
  })();

  connect();
  syncPauseBtn();
  renderRegisteredTasks();
  updateThemeBtn(document.documentElement.getAttribute('data-theme') || 'light');
</script>
</body>
</html>"""


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


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


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
