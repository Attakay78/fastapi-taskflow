from __future__ import annotations
import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_STATUS_COLOR = {
    "pending": ("#f3f4f6", "#6b7280"),
    "running": ("#eff6ff", "#2563eb"),
    "success": ("#f0fdf4", "#16a34a"),
    "failed": ("#fef2f2", "#dc2626"),
    "interrupted": ("#fffbeb", "#d97706"),
    "cancelled": ("#fce7f3", "#be185d"),
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
