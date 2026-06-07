from __future__ import annotations
import html
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

_STATUS_COLOR = {
    "pending": ("#f3f4f6", "#6b7280"),
    "running": ("#eff6ff", "#2563eb"),
    "success": ("#f0fdf4", "#16a34a"),
    "failed": ("#fef2f2", "#dc2626"),
    "interrupted": ("#fffbeb", "#d97706"),
    "cancelled": ("#fce7f3", "#be185d"),
    "rejected": ("#fef2f2", "#9f1239"),
}


def _fmt_duration(seconds: float) -> str:
    ms = seconds * 1000
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    if ms < 3_600_000:
        m = int(ms // 60_000)
        s = int((ms % 60_000) // 1000)
        return f"{m}m {s}s"
    h = int(ms // 3_600_000)
    m = int((ms % 3_600_000) // 60_000)
    return f"{h}h {m}m"


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
    avg = _fmt_duration(sum(durs) / len(durs)) if durs else "—"

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


def _render_queue_cards(queues: list[dict[str, Any]], prefix: str) -> str:
    """Render one card per named queue showing config and live counters.

    Each card displays the queue name, its concurrency and max_size limits,
    the number of tasks currently pending in the heap, running, and finished.
    An inline edit form lets operators update concurrency and max_size without
    a redeploy.

    Args:
        queues: List of queue stat dicts as returned by
            :meth:`~fastapi_taskflow.manager.TaskManager.queue_stats`.
        prefix: The URL prefix under which the task API is mounted (e.g.
            ``"/tasks"``). Used to build the PATCH endpoint URL.

    Returns:
        An HTML string containing one card per queue.
    """
    if not queues:
        return (
            '<div style="color:#9ca3af;padding:40px;text-align:center;font-size:0.9rem">'
            "No named queues configured. Pass <code>queues=</code> or <code>max_size=</code> "
            "to <code>TaskManager</code> to activate the named queue system."
            "</div>"
        )

    cards: list[str] = []
    for q in queues:
        name = html.escape(q["name"])
        concurrency = q["concurrency"] if q["concurrency"] is not None else ""
        max_size = q["max_size"] if q["max_size"] is not None else ""
        pending = q.get("pending", 0)
        running = q.get("running", 0)
        finished = q.get("finished", 0)
        rejected = q.get("rejected", 0)
        patch_url = f"{prefix}/queues/{html.escape(q['name'])}"

        is_full = q["max_size"] is not None and pending >= q["max_size"]
        border = "2px solid #dc2626" if is_full else "1px solid #e5e7eb"

        danger_badge = (
            '<span style="font-size:0.68rem;background:#fef2f2;color:#dc2626;'
            'padding:2px 8px;border-radius:4px;font-weight:600">FULL</span>'
            if is_full
            else ""
        )

        cards.append(
            f'<div style="background:white;border:{border};border-radius:10px;'
            f'padding:18px 20px;display:flex;flex-direction:column;gap:12px">'
            # Queue name header
            f'<div style="display:flex;align-items:center;gap:8px">'
            f'<span style="font-weight:600;font-size:1rem;color:#111;flex:1">{name}</span>'
            f"{danger_badge}"
            f'<span style="font-size:0.7rem;background:#f3f4f6;color:#6b7280;'
            f'padding:2px 8px;border-radius:4px;font-family:monospace">queue</span>'
            f"</div>"
            # Stat counters row — 4 columns when there are rejections, 3 otherwise
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">'
            + _queue_stat("Pending", str(pending), "#9ca3af")
            + _queue_stat("Running", str(running), "#2563eb")
            + _queue_stat("Finished", str(finished), "#16a34a")
            + _queue_stat(
                "Rejected", str(rejected), "#dc2626" if rejected else "#9ca3af"
            )
            + f"</div>"
            # Inline edit form
            f"<form onsubmit=\"updateQueue(event, '{patch_url}')\" "
            f'style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">'
            f'<div style="display:flex;flex-direction:column;gap:3px">'
            f'<label style="font-size:0.68rem;color:#888;font-weight:500">Concurrency</label>'
            f'<input type="number" name="concurrency" min="1" value="{concurrency}" '
            f'placeholder="unlimited" disabled '
            f'style="width:100px;padding:5px 8px;border:1px solid #e5e7eb;border-radius:5px;'
            f'font-size:0.82rem;background:#f9fafb;color:#6b7280">'
            f"</div>"
            f'<div style="display:flex;flex-direction:column;gap:3px">'
            f'<label style="font-size:0.68rem;color:#888;font-weight:500">Max size</label>'
            f'<input type="number" name="max_size" min="1" value="{max_size}" '
            f'placeholder="unlimited" disabled '
            f'style="width:100px;padding:5px 8px;border:1px solid #e5e7eb;border-radius:5px;'
            f'font-size:0.82rem;background:#f9fafb;color:#6b7280">'
            f"</div>"
            f'<button type="button" onclick="queueEditToggle(this)" '
            f'style="padding:5px 14px;background:white;color:#374151;border:1px solid #d1d5db;'
            f'border-radius:5px;font-size:0.82rem;cursor:pointer;height:30px;margin-bottom:1px">'
            f"Edit</button>"
            f'<button type="submit" style="display:none;padding:5px 14px;background:#111;'
            f"color:white;border:none;border-radius:5px;font-size:0.82rem;cursor:pointer;"
            f'height:30px;margin-bottom:1px">Save</button>'
            f"</form>"
            f"</div>"
        )

    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px">'
        + "".join(cards)
        + "</div>"
    )


def _queue_stat(label: str, value: str, color: str) -> str:
    """Render a single stat cell inside a queue card."""
    return (
        f'<div style="background:#f9fafb;border-radius:6px;padding:8px 10px">'
        f'<div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:.04em;'
        f'color:#888;font-weight:500;margin-bottom:3px">{label}</div>'
        f'<div style="font-size:1.1rem;font-weight:600;color:{color};'
        f'font-variant-numeric:tabular-nums">{value}</div>'
        f"</div>"
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
        duration = _fmt_duration(t.duration) if t.duration is not None else "—"
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
