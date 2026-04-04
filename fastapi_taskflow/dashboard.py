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

if TYPE_CHECKING:
    from .manager import TaskManager

# ---------------------------------------------------------------------------
# HTML fragment helpers (used by /metrics and /tasks HTTP endpoints)
# ---------------------------------------------------------------------------

_STATUS_COLOR = {
    "pending": ("#f3f4f6", "#6b7280"),
    "running": ("#eff6ff", "#2563eb"),
    "success": ("#f0fdf4", "#16a34a"),
    "failed":  ("#fef2f2", "#dc2626"),
}


def _badge(status: str) -> str:
    bg, fg = _STATUS_COLOR.get(status, ("#f3f4f6", "#6b7280"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:4px;font-size:0.72rem;font-weight:600;'
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


def _render_metrics(task_manager: "TaskManager") -> str:
    tasks   = task_manager.store.list()
    total   = len(tasks)
    success = sum(1 for t in tasks if t.status.value == "success")
    failed  = sum(1 for t in tasks if t.status.value == "failed")
    running = sum(1 for t in tasks if t.status.value == "running")
    pending = sum(1 for t in tasks if t.status.value == "pending")
    rate    = f"{success / total * 100:.1f}%" if total else "—"
    durs    = [t.duration for t in tasks if t.duration is not None]
    avg     = f"{sum(durs) / len(durs) * 1000:.0f} ms" if durs else "—"

    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px">'
        + _metric_card("Total",        str(total),   "#6366f1")
        + _metric_card("Pending",      str(pending), "#9ca3af")
        + _metric_card("Running",      str(running), "#7c3aed")
        + _metric_card("Success",      str(success), "#16a34a")
        + _metric_card("Failed",       str(failed),  "#dc2626")
        + _metric_card("Success rate", rate,         "#f59e0b")
        + _metric_card("Avg duration", avg,          "#8b5cf6")
        + "</div>"
    )


def _render_task_rows(task_manager: "TaskManager") -> str:
    tasks = sorted(
        task_manager.store.list(),
        key=lambda t: t.created_at,
        reverse=True,
    )
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
            f'{html.escape(t.task_id[:8])}…</td>'
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


def _build_sse_state(task_manager: "TaskManager", include_args: bool = False) -> str:
    """
    Serialize the full task store as a single SSE ``state`` event.

    Payload shape::

        {
          "tasks":   [ <TaskRecord.to_dict()>, … ],
          "metrics": { "total": N, "pending": N, … }
        }

    When *include_args* is ``True`` each task dict also carries ``args`` and
    ``kwargs`` (serialised with ``repr()`` so arbitrary types are safe).
    Newlines are kept out of the data line so the SSE framing is unambiguous.
    """
    tasks   = task_manager.store.list()
    total   = len(tasks)
    success = sum(1 for t in tasks if t.status.value == "success")
    failed  = sum(1 for t in tasks if t.status.value == "failed")
    running = sum(1 for t in tasks if t.status.value == "running")
    pending = sum(1 for t in tasks if t.status.value == "pending")
    durs    = [t.duration for t in tasks if t.duration is not None]

    metrics = {
        "total":           total,
        "pending":         pending,
        "running":         running,
        "success":         success,
        "failed":          failed,
        "success_rate":    round(success / total * 100, 1) if total else None,
        "avg_duration_ms": round(sum(durs) / len(durs) * 1000) if durs else None,
    }

    task_dicts = []
    for t in tasks:
        d = t.to_dict()
        if include_args:
            d["args"]   = [repr(a) for a in t.args]
            d["kwargs"] = {k: repr(v) for k, v in t.kwargs.items()}
        task_dicts.append(d)

    payload = json.dumps({"tasks": task_dicts, "metrics": metrics})
    return f"event: state\ndata: {payload}\n\n"


async def _sse_generator(
    task_manager: "TaskManager",
    request: Request,
    include_args: bool = False,
) -> AsyncIterator[str]:
    """
    Yields SSE messages for the duration of the client connection.

    * Sends an immediate ``state`` event so the dashboard renders on first
      connect without waiting for a store change.
    * Blocks on the subscriber queue; each entry represents one or more
      coalesced store mutations.
    * Sends a keep-alive comment every 30 s so proxies don't drop the stream.
    * Cleans up the subscriber queue on disconnect or CancelledError.
    """
    q = task_manager.store.add_subscriber()
    try:
        yield _build_sse_state(task_manager, include_args=include_args)

        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(q.get(), timeout=30.0)
                yield _build_sse_state(task_manager, include_args=include_args)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        task_manager.store.remove_subscriber(q)


# ---------------------------------------------------------------------------
# Dashboard HTML page
# ---------------------------------------------------------------------------

# __STREAM_URL__ is replaced at request time by _dashboard_page().
_DASHBOARD_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Task Dashboard · FastAPI-TaskFlow</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><rect width='200' height='200' rx='40' ry='40' fill='%23111111'/><polyline points='20,100 55,100 80,145 120,55 145,100 180,100' fill='none' stroke='white' stroke-width='18' stroke-linecap='round' stroke-linejoin='round'/></svg>">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Geist', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 14px;
      background: #f5f5f5;
      color: #111;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }

    /* ── Header ─────────────────────────────────────────────── */
    .header {
      background: #fff;
      border-bottom: 1px solid #e5e7eb;
      height: 52px;
      display: flex;
      align-items: center;
      padding: 0 24px;
      position: sticky;
      top: 0;
      z-index: 100;
      gap: 10px;
    }
    .header-icon {
      width: 26px; height: 26px;
      flex-shrink: 0;
    }
    .header-title { font-size: 14px; font-weight: 600; color: #111; }
    .logout-btn { font-size: 12px; color: #6b7280; text-decoration: none; padding: 4px 10px; border: 1px solid #e5e7eb; border-radius: 5px; font-weight: 500; transition: color .15s, background .15s; }
    .logout-btn:hover { color: #111; background: #f3f4f6; }
    .header-badge {
      font-size: 11px; color: #888;
      background: #f3f4f6; border: 1px solid #e5e7eb;
      border-radius: 4px; padding: 1px 7px; font-weight: 500;
    }

    /* ── Main ───────────────────────────────────────────────── */
    .main { max-width: 1440px; margin: 0 auto; padding: 24px 24px 64px; }

    .dot { width: 7px; height: 7px; border-radius: 50%; background: #d1d5db; flex-shrink: 0; transition: background .3s; }
    .dot--live  { background: #17c964; animation: pulse 2s ease-in-out infinite; }
    .dot--error { background: #f31260; }
    .dot--connecting { background: #f59e0b; }
    @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }
    .status-label { font-size: 12px; color: #888; font-weight: 500; }
    .status-label--live  { color: #16a34a; }
    .status-label--error { color: #dc2626; }

    /* ── Top row (metrics + filters) ───────────────────────── */
    .top-row {
      display: flex;
      gap: 16px;
      align-items: stretch;
      margin-bottom: 16px;
    }
    .metrics {
      flex: 1;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 10px;
      align-content: start;
    }
    .filters-right {
      display: flex;
      flex-direction: column;
      gap: 8px;
      width: 252px;
      flex-shrink: 0;
      justify-content: center;
    }
    .filter-row {
      display: flex;
      gap: 8px;
    }
    .filter-row .sel { flex: 1; min-width: 0; }

    /* ── Task count row ─────────────────────────────────────── */
    .task-count { font-size: 12px; color: #aaa; margin-bottom: 8px; display: block; }

    /* ── Inputs ─────────────────────────────────────────────── */
    .sel, .search {
      height: 32px;
      border: 1px solid #e5e7eb; border-radius: 6px;
      padding: 0 10px; font-size: 13px; font-family: inherit;
      background: #fff; color: #111; outline: none;
      transition: border-color .15s, box-shadow .15s; cursor: pointer;
      width: 100%;
    }
    .sel:hover, .search:hover { border-color: #c0c0c0; }
    .sel:focus, .search:focus { border-color: #0070f3; box-shadow: 0 0 0 3px rgba(0,112,243,.1); }
    .search { cursor: text; }
    .search::placeholder { color: #ccc; }
    .metric-card {
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 14px 16px;
    }
    .metric-label {
      font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
      font-weight: 500; margin-bottom: 6px; opacity: .75;
    }
    .metric-value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.5px; }

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

    .mc-rate    { background: #fffbeb; border-color: #fde68a; }
    .mc-rate    .metric-label { color: #b45309; }
    .mc-rate    .metric-value { color: #92400e; }

    .mc-avg     { background: #fdf4ff; border-color: #f0abfc; }
    .mc-avg     .metric-label { color: #a21caf; }
    .mc-avg     .metric-value { color: #86198f; }

    /* ── Table ──────────────────────────────────────────────── */
    .table-wrap { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; }
    .th {
      padding: 9px 14px; text-align: left;
      font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
      color: #888; font-weight: 600;
      background: #fafafa; border-bottom: 1px solid #e5e7eb;
      cursor: pointer; user-select: none; white-space: nowrap;
    }
    .th:hover { color: #333; background: #f3f4f6; }
    .th--active { color: #0070f3; }
    .th--r { text-align: right; }
    .sort-icon { opacity: .4; font-size: 10px; margin-left: 3px; }
    .th--active .sort-icon { opacity: 1; }
    .td { padding: 10px 14px; border-bottom: 1px solid #f3f4f6; font-size: 13px; color: #374151; vertical-align: middle; }
    .row { cursor: pointer; transition: background .1s; }
    .row:hover { background: #f9fafb; }
    .row--selected { background: #eff6ff !important; }
    .row:last-child .td { border-bottom: none; }
    .td--mono { font-family: 'Geist Mono', 'JetBrains Mono', 'Fira Code', monospace; font-size: 11.5px; color: #aaa; }
    .td--func { font-weight: 500; color: #111; }
    .td--r    { text-align: right; color: #6b7280; font-variant-numeric: tabular-nums; }
    .td--date { color: #999; font-size: 12px; white-space: nowrap; padding-left: 24px; }
    .td--err  { max-width: 180px; }
    .err-text { color: #dc2626; font-size: 12px; }
    .muted { color: #d1d5db; }
    .empty { text-align: center; padding: 52px 16px; color: #bbb; font-size: 13px; }

    /* ── Badges ─────────────────────────────────────────────── */
    .badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; letter-spacing: .02em; }
    .badge--pending { background: #f3f4f6; color: #6b7280; }
    .badge--running { background: #ede9fe; color: #7c3aed; }
    .badge--success { background: #dcfce7; color: #16a34a; }
    .badge--failed  { background: #fee2e2; color: #dc2626; }

    /* ── Detail Panel ───────────────────────────────────────── */
    .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.2); opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 200; }
    .backdrop--on { opacity: 1; pointer-events: auto; }
    .panel {
      position: fixed; top: 0; right: 0; bottom: 0; width: 440px; max-width: 100vw;
      background: #fff; border-left: 1px solid #e5e7eb;
      transform: translateX(100%);
      transition: transform .25s cubic-bezier(.4,0,.2,1);
      z-index: 201; display: flex; flex-direction: column;
    }
    .panel--open { transform: translateX(0); }
    .panel-header { display: flex; align-items: center; gap: 10px; padding: 14px 20px; border-bottom: 1px solid #f3f4f6; flex-shrink: 0; }
    .panel-title { font-size: 14px; font-weight: 600; color: #111; }
    .panel-close {
      margin-left: auto; width: 28px; height: 28px;
      border: 1px solid #e5e7eb; border-radius: 6px; background: #fff; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      color: #666; transition: background .1s, border-color .1s;
    }
    .panel-close:hover { background: #f3f4f6; border-color: #ccc; color: #111; }
    .panel-body { flex: 1; overflow-y: auto; padding: 20px; overscroll-behavior: contain; }

    /* Copy button */
    .copy-btn {
      display: inline-flex; align-items: center; justify-content: center;
      width: 26px; height: 26px; flex-shrink: 0;
      border: 1px solid #e5e7eb; border-radius: 5px;
      background: #fff; cursor: pointer; color: #aaa;
      transition: background .1s, border-color .1s, color .1s;
    }
    .copy-btn:hover { background: #f3f4f6; border-color: #bbb; color: #333; }

    /* Toast */
    .toast {
      position: fixed; bottom: 24px; right: 24px; z-index: 400;
      background: #111; color: #fff;
      font-size: 13px; font-weight: 500;
      padding: 10px 16px; border-radius: 8px;
      display: flex; align-items: center; gap: 8px;
      box-shadow: 0 4px 14px rgba(0,0,0,.2);
      opacity: 0; transform: translateY(6px);
      transition: opacity .2s, transform .2s;
      pointer-events: none;
    }
    .toast--on { opacity: 1; transform: translateY(0); }

    /* Pagination */
    .pagination { display: flex; align-items: center; gap: 8px; margin-top: 10px; justify-content: flex-end; }
    .pg-btn {
      height: 30px; min-width: 30px; padding: 0 10px;
      border: 1px solid #e5e7eb; border-radius: 6px;
      background: #fff; cursor: pointer;
      font-size: 12px; font-family: inherit; color: #374151;
      transition: background .1s, border-color .1s;
    }
    .pg-btn:hover:not(:disabled) { background: #f3f4f6; border-color: #ccc; }
    .pg-btn:disabled { opacity: .35; cursor: default; }
    .pg-info { font-size: 12px; color: #888; }

    /* Detail content */
    .d-section { margin-bottom: 16px; }
    .d-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #bbb; font-weight: 500; margin-bottom: 4px; }
    .d-val { font-size: 13px; color: #111; }
    .d-mono { font-family: 'Geist Mono', monospace; font-size: 11.5px; color: #555; word-break: break-all; }
    .d-func { font-weight: 600; font-size: 14px; }
    .d-error { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 12px; font-family: 'Geist Mono', monospace; white-space: pre-wrap; word-break: break-word; max-height: 120px; overflow-y: auto; }
    .d-row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; margin-bottom: 16px; }
    .d-row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 10px; margin-bottom: 16px; }
    .divider { height: 1px; background: #f3f4f6; margin: 20px 0; }
    .section-title { display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: #666; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }
    .analytics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .a-card { background: #fafafa; border: 1px solid #f0f0f0; border-radius: 6px; padding: 10px 12px; }
    .a-label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: #ccc; font-weight: 500; margin-bottom: 3px; }
    .a-value { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
    .recent-runs { display: flex; flex-direction: column; gap: 6px; }
    .r-run { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid #f0f0f0; border-radius: 6px; cursor: pointer; transition: background .1s; }
    .r-run:hover { background: #fafafa; }
    .r-run--cur { background: #eff6ff; border-color: #bfdbfe; }

  </style>
</head>
<body>

<header class="header">
  <svg class="header-icon" width="26" height="26" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
    <rect width="200" height="200" rx="40" ry="40" fill="#111111"/>
    <polyline points="20,100 55,100 80,145 120,55 145,100 180,100" fill="none" stroke="white" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
  <span class="header-title">Task Dashboard</span>
  <span class="header-badge">fastapi-taskflow</span>
  <div style="margin-left:auto;display:flex;align-items:center;gap:14px">
    <span class="dot dot--connecting" id="live-dot"></span>
    <span class="status-label" id="live-label">Connecting\u2026</span>
    __LOGOUT_BUTTON__
  </div>
</header>

<main class="main">

  <!-- Top row: metrics left, filters right -->
  <div class="top-row">
    <div class="metrics">
      <div class="metric-card mc-total">  <div class="metric-label">Total</div>        <div class="metric-value" id="metric-total">\u2014</div></div>
      <div class="metric-card mc-pending"><div class="metric-label">Pending</div>       <div class="metric-value" id="metric-pending">\u2014</div></div>
      <div class="metric-card mc-running"><div class="metric-label">Running</div>       <div class="metric-value" id="metric-running">\u2014</div></div>
      <div class="metric-card mc-success"><div class="metric-label">Success</div>       <div class="metric-value" id="metric-success">\u2014</div></div>
      <div class="metric-card mc-failed"> <div class="metric-label">Failed</div>        <div class="metric-value" id="metric-failed">\u2014</div></div>
      <div class="metric-card mc-rate">   <div class="metric-label">Success Rate</div> <div class="metric-value" id="metric-rate">\u2014</div></div>
      <div class="metric-card mc-avg">    <div class="metric-label">Avg Duration</div> <div class="metric-value" id="metric-avg">\u2014</div></div>
    </div>
    <div class="filters-right">
      <input type="text" class="search" id="search-input" placeholder="Search by ID or function\u2026">
      <div class="filter-row">
        <select class="sel" id="status-filter">
          <option value="all">All statuses</option>
          <option value="pending">Pending</option>
          <option value="running">Running</option>
          <option value="success">Success</option>
          <option value="failed">Failed</option>
        </select>
        <select class="sel" id="func-filter">
          <option value="all">All functions</option>
        </select>
      </div>
    </div>
  </div>

  <!-- Task count + table -->
  <span class="task-count" id="task-count"></span>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="th" data-sort="task_id" onclick="setSort('task_id')">ID <span class="sort-icon">\u21d5</span></th>
          <th class="th" data-sort="func_name" onclick="setSort('func_name')">Function <span class="sort-icon">\u21d5</span></th>
          <th class="th" data-sort="status" onclick="setSort('status')">Status <span class="sort-icon">\u21d5</span></th>
          <th class="th th--r" data-sort="duration" onclick="setSort('duration')">Duration <span class="sort-icon">\u21d5</span></th>
          <th class="th th--r" data-sort="retries_used" onclick="setSort('retries_used')">Retries <span class="sort-icon">\u21d5</span></th>
          <th class="th" data-sort="created_at" onclick="setSort('created_at')">Created <span class="sort-icon">\u2193</span></th>
          <th class="th">Error</th>
        </tr>
      </thead>
      <tbody id="tasks-tbody">
        <tr><td colspan="7" class="empty">Connecting\u2026</td></tr>
      </tbody>
    </table>
  </div>
  <div id="pagination"></div>

</main>

<div class="toast" id="toast"></div>
<div class="backdrop" id="detail-backdrop"></div>

<div class="panel" id="detail-panel">
  <div class="panel-header">
    <span class="panel-title">Task Detail</span>
    <button class="panel-close" id="detail-close" aria-label="Close">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    </button>
  </div>
  <div class="panel-body" id="detail-content"></div>
</div>

<script>
  const STREAM_URL = '__STREAM_URL__';
  const SHOW_ARGS  = __SHOW_ARGS__;

  // ── State ──────────────────────────────────────────────────────
  const PAGE_SIZE = 30;
  let state = { tasks: [], metrics: {} };
  let sortCol     = 'created_at';
  let sortDir     = 'desc';
  let statusFilter = 'all';
  let funcFilter   = 'all';
  let searchText   = '';
  let selectedId   = null;
  let currentPage  = 0;

  // ── SSE ────────────────────────────────────────────────────────
  function connect() {
    const src = new EventSource(STREAM_URL);
    src.addEventListener('state', function(e) {
      state = JSON.parse(e.data);
      renderMetrics();
      populateFuncFilter();
      renderTable();
      if (selectedId) {
        const t = state.tasks.find(t => t.task_id === selectedId);
        if (t) renderDetail(t); else closeDetail();
      }
    });
    src.onopen  = () => setStatus('live');
    src.onerror = () => { setStatus('error'); src.close(); setTimeout(connect, 3000); };
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
      total:   m.total   ?? 0,
      pending: m.pending ?? 0,
      running: m.running ?? 0,
      success: m.success ?? 0,
      failed:  m.failed  ?? 0,
      rate:    m.success_rate    != null ? m.success_rate    + '%'  : '\u2014',
      avg:     m.avg_duration_ms != null ? m.avg_duration_ms + ' ms': '\u2014',
    };
    for (const [k, v] of Object.entries(map)) {
      const el = document.getElementById('metric-' + k);
      if (el) el.textContent = v;
    }
  }

  // ── Filter + Sort ──────────────────────────────────────────────
  function filteredSorted() {
    return state.tasks
      .filter(t => {
        if (statusFilter !== 'all' && t.status !== statusFilter) return false;
        if (funcFilter   !== 'all' && t.func_name !== funcFilter) return false;
        if (searchText) {
          const q = searchText.toLowerCase();
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

  // ── Table ──────────────────────────────────────────────────────
  function renderTable() {
    const all   = filteredSorted();
    const total = all.length;

    // Clamp page
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (currentPage >= totalPages) currentPage = totalPages - 1;

    const tasks = all.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

    const cnt = document.getElementById('task-count');
    cnt.textContent = total === state.tasks.length
      ? total + ' task' + (total !== 1 ? 's' : '')
      : total + ' of ' + state.tasks.length + ' tasks';

    const tbody = document.getElementById('tasks-tbody');
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No tasks match the current filters.</td></tr>';
      renderPagination(total);
      return;
    }

    tbody.innerHTML = tasks.map(t => {
      const dur  = t.duration != null ? (t.duration * 1000).toFixed(0) + ' ms' : '\u2014';
      const date = t.created_at ? fmtDate(t.created_at) : '\u2014';
      const err  = t.error
        ? '<span class="err-text" title="' + esc(t.error) + '">' + esc(t.error.slice(0,48)) + (t.error.length > 48 ? '\u2026' : '') + '</span>'
        : '<span class="muted">\u2014</span>';
      const sel = t.task_id === selectedId ? ' row--selected' : '';
      return '<tr class="row' + sel + '" onclick="openDetail(this.dataset.id)" data-id="' + t.task_id + '">'
        + '<td class="td td--mono">'  + esc(t.task_id.slice(0,8)) + '\u2026</td>'
        + '<td class="td td--func">'  + esc(t.func_name) + '</td>'
        + '<td class="td">'           + badge(t.status) + '</td>'
        + '<td class="td td--r">'     + dur + '</td>'
        + '<td class="td td--r">'     + t.retries_used + '</td>'
        + '<td class="td td--date">'  + date + '</td>'
        + '<td class="td td--err">'   + err + '</td>'
        + '</tr>';
    }).join('');

    renderPagination(total);
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
    const cls = { pending:'badge--pending', running:'badge--running', success:'badge--success', failed:'badge--failed' };
    return '<span class="badge ' + (cls[status] || 'badge--pending') + '">' + esc(status) + '</span>';
  }

  // ── Sort ───────────────────────────────────────────────────────
  function setSort(col) {
    if (sortCol === col) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = col;
      sortDir = (col === 'created_at' || col === 'duration') ? 'desc' : 'asc';
    }
    document.querySelectorAll('[data-sort]').forEach(th => {
      const c = th.dataset.sort;
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
    const fRate = fTotal ? (fSuccess / fTotal * 100).toFixed(1) : null;
    const rateColor = fRate == null ? '#111' : fRate >= 80 ? '#16a34a' : fRate >= 50 ? '#d97706' : '#dc2626';

    const recent = [...ft].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||'')).slice(0,5);

    const dur     = task.duration   != null ? (task.duration * 1000).toFixed(0) + ' ms' : '\u2014';
    const created = task.created_at ? fmtDate(task.created_at) : '\u2014';
    const started = task.start_time ? fmtDate(task.start_time) : '\u2014';
    const ended   = task.end_time   ? fmtDate(task.end_time)   : '\u2014';

    document.getElementById('detail-content').innerHTML =
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

      + '<div class="d-row3">'
      + dSec('Created', '<span style="font-size:12px">' + created + '</span>')
      + dSec('Started', '<span style="font-size:12px">' + started + '</span>')
      + dSec('Ended',   '<span style="font-size:12px">' + ended   + '</span>')
      + '</div>'

      + '<div class="d-row2">'
      + dSec('Duration',     '<span class="d-val">' + dur + '</span>')
      + dSec('Retries Used', '<span class="d-val">' + task.retries_used + '</span>')
      + '</div>'

      + (task.error ? '<div class="d-section"><div class="d-label">Error</div><div class="d-error">' + esc(task.error) + '</div></div>' : '')

      + (SHOW_ARGS && ((task.args && task.args.length) || (task.kwargs && Object.keys(task.kwargs).length)) ?
          '<div class="divider"></div>'
          + '<div class="section-title">'
          + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>'
          + 'Arguments</div>'
          + (task.args && task.args.length ?
              '<div class="d-section"><div class="d-label">Positional args</div>'
              + task.args.map(function(a, i) {
                  return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
                    + '<span style="font-size:10px;color:#ccc;min-width:18px;text-align:right">' + i + '</span>'
                    + '<code class="d-mono" style="background:#f9fafb;border:1px solid #f0f0f0;border-radius:4px;padding:2px 7px;font-size:11.5px">' + esc(a) + '</code>'
                    + '</div>';
                }).join('')
              + '</div>'
            : '')
          + (task.kwargs && Object.keys(task.kwargs).length ?
              '<div class="d-section"><div class="d-label">Keyword args</div>'
              + Object.entries(task.kwargs).map(function(kv) {
                  return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
                    + '<span style="font-size:11px;color:#888;min-width:fit-content">' + esc(kv[0]) + '</span>'
                    + '<span style="color:#ccc;font-size:11px">=</span>'
                    + '<code class="d-mono" style="background:#f9fafb;border:1px solid #f0f0f0;border-radius:4px;padding:2px 7px;font-size:11.5px">' + esc(kv[1]) + '</code>'
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
      + aCard('Total Runs',    fTotal,                           '#111')
      + aCard('Success',       fSuccess,                         '#16a34a')
      + aCard('Failed',        fFailed,  fFailed  > 0 ? '#dc2626' : '#111')
      + aCard('Running',       fRunning, fRunning > 0 ? '#7c3aed' : '#111')
      + aCard('Success Rate',  fRate  != null ? fRate  + '%'  : '\u2014', rateColor)
      + aCard('Avg Duration',  fAvg   != null ? fAvg   + ' ms': '\u2014', '#111')
      + aCard('Min Duration',  fMin   != null ? fMin   + ' ms': '\u2014', '#111')
      + aCard('Max Duration',  fMax   != null ? fMax   + ' ms': '\u2014', '#111')
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
          + '<span style="font-size:11px;margin-left:auto;color:#999">'
          + (r.duration != null ? (r.duration*1000).toFixed(0)+' ms' : '\u2014')
          + '</span></div>'
        ).join('')
      + '</div>';
  }

  function dSec(label, inner) {
    return '<div class="d-section"><div class="d-label">' + label + '</div>' + inner + '</div>';
  }

  function aCard(label, value, color) {
    return '<div class="a-card"><div class="a-label">' + label + '</div>'
      + '<div class="a-value" style="color:' + color + '">' + value + '</div></div>';
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
  document.getElementById('status-filter').addEventListener('change', e => { statusFilter = e.target.value; currentPage = 0; renderTable(); });
  document.getElementById('func-filter').addEventListener('change',   e => { funcFilter   = e.target.value; currentPage = 0; renderTable(); });
  document.getElementById('search-input').addEventListener('input',   e => { searchText   = e.target.value; currentPage = 0; renderTable(); });
  document.getElementById('detail-close').addEventListener('click', closeDetail);
  document.getElementById('detail-backdrop').addEventListener('click', closeDetail);

  connect();
</script>
</body>
</html>"""


def _dashboard_page(
    base_path: str,
    show_args: bool = False,
    logout_url: str | None = None,
) -> str:
    stream_url = f"{base_path}/dashboard/stream"
    logout_btn = (
        f'<a href="{logout_url}" class="logout-btn">Sign out</a>'
        if logout_url else ""
    )
    return (
        _DASHBOARD_TEMPLATE
        .replace("__STREAM_URL__", stream_url)
        .replace("__SHOW_ARGS__", "true" if show_args else "false")
        .replace("__LOGOUT_BUTTON__", logout_btn)
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
        from .auth import verify_token, COOKIE_NAME
        return verify_token(secret_key, request.cookies.get(COOKIE_NAME, ""))

    @router.get("", response_class=HTMLResponse)
    def dashboard_page(request: Request):
        if not _check_cookie(request):
            assert login_path is not None
            return _Redirect(url=login_path, status_code=302)
        return HTMLResponse(
            _dashboard_page(prefix, show_args=display_func_args, logout_url=logout_url)
        )

    @router.get("/stream")
    async def event_stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _sse_generator(task_manager, request, include_args=display_func_args),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    @router.get("/metrics", response_class=HTMLResponse)
    def metrics_fragment(request: Request) -> HTMLResponse:
        if not _check_cookie(request):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Unauthorized")
        return HTMLResponse(_render_metrics(task_manager))

    @router.get("/tasks", response_class=HTMLResponse)
    def tasks_fragment(request: Request) -> HTMLResponse:
        if not _check_cookie(request):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Unauthorized")
        return HTMLResponse(_render_task_rows(task_manager))

    return router
