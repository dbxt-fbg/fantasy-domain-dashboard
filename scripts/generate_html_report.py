#!/usr/bin/env python3
"""
Generate HTML dashboard reports with better formatting.
"""

import html
import json
import re
import sys
import os
from collections import Counter
from pathlib import Path
from datetime import datetime
from typing import Optional


def _is_working_day(d) -> bool:
    """Saturday = 5, Sunday = 6 in Python's weekday()."""
    return d.weekday() < 5


def _working_days_between(start, end) -> int:
    """Count working days in the inclusive range [start, end]. 0 if end < start."""
    from datetime import timedelta as _td
    if end < start:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if _is_working_day(cur):
            days += 1
        cur += _td(days=1)
    return days

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.competencies import (
    TITLE_TO_LEVEL,
    get_competency_payload,
)
from utils.statuses import (
    CLOSED_STATUSES,
    IN_PROGRESS_STATUSES,
    OPEN_STATUSES,
    EXCLUDED_STATUSES,
    bucket_for,
    sql_placeholders,
)
from utils.io import atomic_write as _atomic_write
from utils.nav import generate_nav_menu
from utils.project_name import project_label
from utils.sprint_names import format_long as fmt_sprint_long
from utils.sprint_names import format_short as fmt_sprint_short
from utils.sprint_names import format_slot as fmt_sprint_slot
from utils.sprint_names import format_milestone as fmt_sprint_milestone
from database.schema import get_connection
from database.queries import (
    parse_iso_tz,
    get_current_sprint,
    get_sprint_metrics,
    get_sprint_burndown,
    get_all_developers_metrics,
    get_developer_tickets,
    get_developer_tickets_bulk,
    get_pr_metrics,
    get_pr_metrics_bulk,
    get_review_metrics,
    get_review_metrics_bulk,
    get_team_cycle_time,
    get_developer_cycle_time,
    get_developer_cycle_time_bulk,
    get_developer_cycle_per_point,
    get_developer_cycle_per_point_bulk,
    get_team_throughput,
    get_developer_throughput,
    get_developer_throughput_bulk,
    get_team_pr_review_time,
    get_pr_approvals_by_developer,
    get_sprint_commitment_accuracy,
    get_pr_size_distribution,
    get_time_to_first_review,
    get_review_load_by_reviewer,
    get_time_in_status,
    get_status_churn,
    get_blocked_time,
    get_sprint_scope_change,
    get_pr_size_vs_merge_time,
    get_hygiene_aging_summary,
    get_one_on_one_meeting,
    get_one_on_one_meetings_bulk,
    get_flow_efficiency,
    get_rework_rate,
    get_predictability,
)


def render_html(*, title: str, content: str, body_class: str = "page-project") -> str:
    """Render a full page. body_class drives the page-specific theme."""
    return HTML_TEMPLATE.format(title=title, content=content, body_class=body_class)


# Which body class to use per active_page key — mirrors nav.PRIMARY_NAV.
_PAGE_THEME = {
    "project-fantasy": "page-project",
    "features":        "page-project",
    "readiness":       "page-project",
    "delivery-excellence": "page-project",
    "stories":         "page-project",
    "story-points":    "page-project",
    "epics":           "page-project",
    "pull-requests":   "page-project",
    "past-sprints":    "page-project",
    "stakeholders":    "page-project",
    "dependencies":    "page-project",
    "mbr":             "page-project",
    "team-members":    "page-team",
    "logs":            "page-logs",
    "hygiene":         "page-hygiene",
}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;600;700;800&family=Saira:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="assets/dashboard.css?v=scoreboard-1">
    <script src="assets/dashboard.js?v=gantt-open-stories-1" defer></script>
    <style>
        /* Page-specific overrides only — shared styles live in assets/dashboard.css */
    </style>
    <!-- Interactive handlers (toggleAccordion, sortTable, etc.) live in assets/dashboard.js -->
</head>
<body class="{body_class}">
    <div class="container">
        {content}
    </div>
</body>
</html>
"""


def _build_burndown_axes(start_date, end_date, today):
    """Compute the working-day axis used by every burndown chart.

    Returns a dict with: working_days (list[date]), wd_index (date→int),
    wd_total (int domain), wd_elapsed (int through today), wd_index_for
    (callable: date → x-index, snapping weekends back to the prior Friday),
    days_remaining (calendar days to end_date).
    """
    from datetime import timedelta as _td

    total_calendar_days = max((end_date - start_date).days, 1)
    working_days = [
        start_date + _td(days=i)
        for i in range(total_calendar_days + 1)
        if _is_working_day(start_date + _td(days=i))
    ]
    wd_index = {d: i for i, d in enumerate(working_days)}
    wd_total = max(len(working_days) - 1, 1)

    def wd_index_for(d):
        if d in wd_index:
            return wd_index[d]
        cur = d
        while cur >= start_date:
            cur -= _td(days=1)
            if cur in wd_index:
                return wd_index[cur]
        return 0

    wd_elapsed = _working_days_between(start_date, min(today, end_date))
    if wd_elapsed > 0:
        wd_elapsed -= 1  # zero-indexed
    wd_elapsed = max(0, min(wd_elapsed, wd_total))
    days_remaining = max(0, (end_date - today).days)

    return {
        'working_days': working_days,
        'wd_index': wd_index,
        'wd_total': wd_total,
        'wd_index_for': wd_index_for,
        'wd_elapsed': wd_elapsed,
        'days_remaining': days_remaining,
    }


def _render_burndown_chart(
    *,
    title: str,
    section_id: str,
    axes: dict,
    series: list,
    summary_cards: list,
    legend: list,
    ideal_points: list,
    projection_points: list = None,
    today_in_sprint: bool,
    y_format: str = '{v:.0f}',
) -> str:
    """Render a burndown chart's SVG block + summary row.

    Args:
        title: Chart heading text.
        section_id: HTML id for the wrapping <div class="section">.
        axes: Output of _build_burndown_axes().
        series: Iterable of dicts {name, points: list[str], color, dots: list[(x,y)]}.
            `points` are pre-formatted "x,y" strings for a polyline; `dots`
            are floats. Render order matches list order.
        summary_cards: List of dicts {label, value, sub} for the top row.
        legend: List of dicts {kind: 'solid'|'dashed', color, label}.
        ideal_points: Two "x,y" strings for the dashed grey ideal line.
        projection_points: Optional two "x,y" strings for the orange forecast.
        today_in_sprint: Whether to draw the "Today" vertical marker.
        y_format: Format string for y-axis labels (use 'SP' suffix when needed).
    """
    svg_w, svg_h = 900, 280
    pad_l, pad_r, pad_t, pad_b = 52, 20, 18, 34
    inner_w = svg_w - pad_l - pad_r
    inner_h = svg_h - pad_t - pad_b

    wd_total = axes['wd_total']
    wd_elapsed = axes['wd_elapsed']
    working_days = axes['working_days']

    # Compute max y from supplied series + ideal anchor (the first ideal point's y
    # is encoded in its string but the caller already factored it into series).
    # We accept that callers pre-compute axis max — pass via summary_cards or
    # don't; instead, peek at points to find max y. Simpler: take an explicit
    # max via an extra arg? For now the caller pre-shapes points so this is fine.

    def x_at(off):
        return pad_l + (off / wd_total) * inner_w

    # Y-axis: derive 5 ticks from the largest y-coord found in any series point.
    all_ys = []
    for s in series:
        for p in s.get('points', []):
            try:
                all_ys.append(float(p.split(',')[1]))
            except (ValueError, IndexError):
                pass
    # The polyline points are already in pixel space (callers used y_at). To
    # render axis tick labels we need their pre-pixel values — those should be
    # supplied via axes['y_ticks']: list[(y_px, label)].
    y_ticks = axes.get('y_ticks', [])
    x_ticks = axes.get('x_ticks', [])

    grid_svg = ''.join(
        f'<line class="chart-grid-line" x1="{pad_l}" y1="{y:.1f}" x2="{svg_w - pad_r}" y2="{y:.1f}" />'
        for y, _ in y_ticks
    )
    y_label_svg = ''.join(
        f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#8194a6" font-size="11">{lbl}</text>'
        for y, lbl in y_ticks
    )
    x_label_svg = ''.join(
        f'<text x="{x:.1f}" y="{svg_h - pad_b + 18}" text-anchor="middle" fill="#8194a6" font-size="11">{lbl}</text>'
        for x, lbl in x_ticks
    )

    today_marker_svg = ''
    if today_in_sprint:
        tx = x_at(wd_elapsed)
        today_marker_svg = (
            f'<line x1="{tx:.1f}" y1="{pad_t}" x2="{tx:.1f}" y2="{svg_h - pad_b}" '
            f'stroke="#56cdf9" stroke-width="1" stroke-dasharray="3,3" opacity="0.6" />'
            f'<text x="{tx:.1f}" y="{pad_t - 4}" text-anchor="middle" fill="#56cdf9" font-size="10">Today</text>'
        )

    projection_svg = ''
    if projection_points:
        projection_svg = (
            f'<polyline fill="none" stroke="#fbbf24" stroke-width="2" stroke-dasharray="4,3" '
            f'points="{" ".join(projection_points)}" />'
        )

    series_svg_parts = []
    for s in series:
        pts = s.get('points', [])
        color = s.get('color', '#2dd4a7')
        if pts:
            stroke_w = s.get('stroke_width', 2.5)
            dasharray = s.get('dasharray')
            extra = f' stroke-dasharray="{dasharray}"' if dasharray else ''
            opacity = s.get('opacity')
            op_attr = f' opacity="{opacity}"' if opacity is not None else ''
            series_svg_parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="{stroke_w}"{extra}{op_attr} points="{" ".join(pts)}" />'
            )
        for cx, cy in s.get('dots', []):
            series_svg_parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{color}" stroke="#0f1620" stroke-width="1.5" />'
            )
    series_svg = ''.join(series_svg_parts)

    def _summary_html(c):
        color_attr = f' style="color:{c["color"]};"' if c.get("color") else ''
        return (
            f'<div class="burndown-summary-item">'
            f'<div class="burndown-summary-label">{c["label"]}</div>'
            f'<div class="burndown-summary-value"{color_attr}>{c["value"]}</div>'
            f'<div class="burndown-summary-sub">{c["sub"]}</div>'
            f'</div>'
        )

    def _legend_html(l):
        if l["kind"] == "solid":
            style = f'style="background:{l["color"]};"'
        else:
            style = f'style="border-color:{l["color"]};"'
        return f'<div><span class="swatch swatch-{l["kind"]}" {style}></span>{l["label"]}</div>'

    summary_html = ''.join(_summary_html(c) for c in summary_cards)
    legend_html = ''.join(_legend_html(l) for l in legend)

    return f"""
        <div class="section" id="{section_id}">
            <div class="chart-container">
                <div class="chart-title">{title}</div>
                <div class="burndown-summary">{summary_html}</div>
                <div class="burndown-svg-wrap">
                    <svg viewBox="0 0 {svg_w} {svg_h}" preserveAspectRatio="xMidYMid meet" style="width: 100%; height: 320px; display: block;">
                        {grid_svg}
                        {y_label_svg}
                        {x_label_svg}
                        <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{svg_h - pad_b}" stroke="#243340" stroke-width="1" />
                        <line x1="{pad_l}" y1="{svg_h - pad_b}" x2="{svg_w - pad_r}" y2="{svg_h - pad_b}" stroke="#243340" stroke-width="1" />
                        <polyline fill="none" stroke="#566375" stroke-width="2" stroke-dasharray="4,4" points="{" ".join(ideal_points)}" />
                        {projection_svg}
                        {series_svg}
                        {today_marker_svg}
                    </svg>
                </div>
                <div class="burndown-legend">{legend_html}</div>
            </div>
        </div>
    """


def _build_role_maps(config: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Return (name_to_role, name_to_role) dicts for quick ticket filtering.

    Returns:
        name_to_role  — display_name → 'BE' | 'FE'
        id_to_role    — jira_account_id → 'BE' | 'FE'
    """
    name_to_role: dict[str, str] = {}
    id_to_role: dict[str, str] = {}
    for member in config.get('team_members', []):
        role = member.get('role')
        if not role:
            continue
        name = member.get('name', '')
        jid = member.get('jira_account_id', '')
        if name:
            name_to_role[name] = role
        if jid:
            id_to_role[jid] = role
    return name_to_role, id_to_role


def _partition_tickets_by_role(
    tickets: list[dict],
    name_to_role: dict[str, str],
    assignee_key: str = 'assignee_display_name',
) -> dict[str, list[dict]]:
    """Split a ticket list into {'BE': [...], 'FE': [...], 'other': [...]}.

    Tickets whose assignee doesn't map to a known role land in 'other'.
    """
    buckets: dict[str, list[dict]] = {'BE': [], 'FE': [], 'other': []}
    for t in tickets:
        role = name_to_role.get(t.get(assignee_key) or '')
        buckets.get(role, buckets['other']).append(t)
    return buckets


# Role buckets reported on the Stories / Story Points pages. 'other' holds
# tickets whose assignee isn't mapped to a BE/FE role (unassigned, PMs, QA,
# not-yet-rostered hires). It MUST be carried through so per-sprint banner
# totals equal the sum of the rendered role blocks AND reconcile with the
# Sprint Reports page, which counts every assignee. See _partition_tickets_by_role.
_REPORTED_ROLES = ('BE', 'FE', 'other')


def _role_metrics(tickets_by_role: dict[str, list[dict]]) -> dict[str, dict]:
    """Return per-role counts for closed/in_progress/open ticket lists.

    tickets_by_role maps 'closed' | 'in_progress' | 'open' → role partition dict.
    Returns {'BE': {...}, 'FE': {...}, 'other': {...}} so callers can sum across
    all reported roles without dropping unassigned/non-BE-FE work.
    """
    result = {}
    for role in _REPORTED_ROLES:
        closed = len(tickets_by_role['closed'].get(role, []))
        in_prog = len(tickets_by_role['in_progress'].get(role, []))
        open_ = len(tickets_by_role['open'].get(role, []))
        total = closed + in_prog + open_
        result[role] = {
            'total': total,
            'closed': closed,
            'in_progress': in_prog,
            'open': open_,
            'completion': (closed / total * 100) if total > 0 else 0,
        }
    return result


def _fmt_sp(v: float) -> str:
    """Format a story-point value: whole numbers lose the '.0', halves keep
    one decimal (e.g. 3.0 -> '3', 3.5 -> '3.5').

    Rounding SP to whole numbers for display caused per-role headers to sum to
    a different number than their sprint banner (each rounded independently),
    so SP figures are shown at their true 0.5 granularity instead.
    """
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def _scope_change_chip(db_path: str, sprint_id: int) -> str:
    """A subtle banner chip showing mid-sprint scope change, or '' if N/A.

    Only renders when the sprint has ≥2 snapshots AND the delta is material
    (|Δ| ≥ 2 SP) — small wobble from rounding/rescoping isn't worth the noise.
    Added scope reads amber (it's the usual reason commitment % looks low);
    removed scope reads muted. Snapshot-based (committed total over time), so
    it's labeled distinctly from the ticket-bucket totals beside it.
    """
    sc = get_sprint_scope_change(db_path, sprint_id)
    if not sc or abs(sc['delta_sp']) < 2:
        return ''
    if sc['delta_sp'] > 0:
        color = '#fbbf24'
        label = f"+{_fmt_sp(sc['delta_sp'])} <small>scope added</small>"
    else:
        color = '#8194a6'
        label = f"{_fmt_sp(sc['delta_sp'])} <small>scope removed</small>"
    start_sp = _fmt_sp(sc['start_sp'])
    end_sp = _fmt_sp(sc['end_sp'])
    pct = sc['pct']
    title = (f"Committed {start_sp} SP at sprint start, {end_sp} SP now "
             f"({pct:+g}%). Snapshot-based.")
    return (
        f'<span class="epic-sprint-stat" style="color: {color};" '
        f'title="{title}">{label}</span>'
    )


def _role_sp_metrics(closed, in_prog, open_) -> dict:
    """Sum story_points per role partition, return per-role SP dict.

    Includes the 'other' bucket (see _REPORTED_ROLES) so banner totals
    reconcile with the rendered role blocks and with the Sprint Reports page.
    """
    def _sum(lst):
        return sum((t.get('story_points') or 0) for t in lst)

    result = {}
    for role in _REPORTED_ROLES:
        c = _sum(closed.get(role, []))
        ip = _sum(in_prog.get(role, []))
        o = _sum(open_.get(role, []))
        total = c + ip + o
        result[role] = {
            'completed': c, 'in_progress': ip, 'open': o,
            'total': total,
            'completion': (c / total * 100) if total > 0 else 0,
        }
    return result


def _render_sprint_story_bar_chart(db_path: str, sprint_prefix: str, config: dict, *, current_sprint_id: int | None) -> str:
    """Render a sprint-over-sprint story-completion bar chart (Stories only).

    Mirrors `_render_sprint_completion_bar_chart` on the SP page but counts
    Story tickets instead of summing story points. Closed sprints use
    `status_at_sprint_end`; the active sprint falls back to live status via
    COALESCE so the in-flight bar reads "now." Counts every assignee (BE, FE,
    and unassigned/other) so the bar matches the per-sprint banners below and
    the Sprint Reports page.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT sprint_id, sprint_name, start_date, end_date, state
              FROM sprints
             WHERE sprint_name LIKE ? || '%'
               AND COALESCE(is_placeholder, 0) = 0
               AND (state = 'closed'
                    OR sprint_id = ?
                    OR (state = 'active' AND date(end_date) >= date('now')))
             ORDER BY end_date DESC
             LIMIT 12
            """,
            (sprint_prefix, current_sprint_id or -1),
        )
        sprint_rows = [dict(r) for r in cursor.fetchall()]
        if not sprint_rows:
            return ''
        sprint_rows.reverse()

        bars = []
        # Restrict to the same {closed | in-progress | open} buckets the
        # per-sprint banners use, so a ticket with an unrecognized status
        # doesn't inflate the "committed" bar relative to its banner total.
        bar_known = list(set(CLOSED_STATUSES) | set(IN_PROGRESS_STATUSES) | set(OPEN_STATUSES))
        bar_ph = sql_placeholders(bar_known)
        for s in sprint_rows:
            cursor.execute(
                f"""
                SELECT COALESCE(status_at_sprint_end, status) AS sprint_end_status
                  FROM tickets
                 WHERE sprint_id = ?
                   AND issue_type = 'Story'
                   AND COALESCE(status_at_sprint_end, status) IN ({bar_ph})
                """,
                (s['sprint_id'], *bar_known),
            )
            rows = cursor.fetchall()
            committed = len(rows)
            completed = sum(1 for r in rows if r['sprint_end_status'] in CLOSED_STATUSES)
            bars.append({
                'label': fmt_sprint_short(s['sprint_name']),
                'committed': committed,
                'completed': completed,
                'is_active': s['sprint_id'] == current_sprint_id,
            })
    finally:
        conn.close()

    if not bars or all(b['committed'] == 0 for b in bars):
        return ''

    svg_w, svg_h = 900, 280
    pad_l, pad_r, pad_t, pad_b = 52, 20, 32, 50
    inner_w = svg_w - pad_l - pad_r
    inner_h = svg_h - pad_t - pad_b
    n = len(bars)
    slot_w = inner_w / n
    bar_w = min(slot_w * 0.78, 64)

    max_y = max((max(b['committed'], b['completed']) for b in bars), default=1) or 1

    def _nice(v):
        import math
        if v <= 0:
            return 1
        magnitude = 10 ** math.floor(math.log10(v))
        for m in (1, 2, 2.5, 5, 10):
            cand = m * magnitude
            if cand >= v:
                return cand
        return 10 * magnitude
    axis_max = _nice(max_y)

    def y_at(v):
        return pad_t + (1 - v / axis_max) * inner_h

    def x_center(i):
        return pad_l + slot_w * (i + 0.5)

    grid_svg, y_label_svg = '', ''
    for i in range(5):
        v = axis_max * (4 - i) / 4
        y = y_at(v)
        grid_svg += f'<line class="chart-grid-line" x1="{pad_l}" y1="{y:.1f}" x2="{svg_w - pad_r}" y2="{y:.1f}" />'
        y_label_svg += f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#8194a6" font-size="11">{v:.0f}</text>'

    bar_svg_parts = []
    for i, b in enumerate(bars):
        cx = x_center(i)
        x_left = cx - bar_w / 2
        committed_top = y_at(b['committed']) if b['committed'] > 0 else y_at(0)
        completed_top = y_at(b['completed']) if b['completed'] > 0 else y_at(0)
        baseline = y_at(0)
        committed_h = baseline - committed_top
        completed_h = baseline - completed_top
        active_stroke = ' stroke="#7dd3fc" stroke-width="1.5"' if b['is_active'] else ''
        if committed_h > 0.5:
            bar_svg_parts.append(
                f'<rect x="{x_left:.1f}" y="{committed_top:.1f}" width="{bar_w:.1f}" '
                f'height="{committed_h:.1f}" fill="#243340" opacity="0.45" rx="3"{active_stroke} />'
            )
        if completed_h > 0.5:
            bar_svg_parts.append(
                f'<rect x="{x_left:.1f}" y="{completed_top:.1f}" width="{bar_w:.1f}" '
                f'height="{completed_h:.1f}" fill="#2dd4a7" rx="3" />'
            )
        top_y = min(committed_top, completed_top)
        pct = (b['completed'] / b['committed'] * 100) if b['committed'] > 0 else 0
        bar_svg_parts.append(
            f'<text x="{cx:.1f}" y="{top_y - 6:.1f}" text-anchor="middle" '
            f'fill="#cdd9e5" font-size="11" font-weight="600">'
            f'{b["completed"]}/{b["committed"]} '
            f'<tspan fill="#8194a6" font-weight="500">({pct:.0f}%)</tspan>'
            f'</text>'
        )
        suffix = ' (active)' if b['is_active'] else ''
        label = (b['label'] + suffix).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        bar_svg_parts.append(
            f'<text x="{cx:.1f}" y="{svg_h - pad_b + 16:.1f}" text-anchor="middle" '
            f'fill="#cdd9e5" font-size="11">{label}</text>'
        )

    bar_svg = ''.join(bar_svg_parts)

    legend_html = (
        '<div class="burndown-legend">'
        '<div><span class="swatch swatch-solid" style="background:#2dd4a7;"></span>Completed Stories</div>'
        '<div><span class="swatch swatch-solid" style="background:#243340; opacity:0.45;"></span>Committed Stories</div>'
        '<div><span class="swatch swatch-solid" style="background:transparent; border:1.5px solid #7dd3fc;"></span>Active sprint</div>'
        '</div>'
    )

    return f"""
        <div class="section" id="stories-completion-bars">
            <div class="chart-container">
                <div class="chart-title">📊 Stories Completed — Sprint over Sprint</div>
                <div class="burndown-svg-wrap">
                    <svg viewBox="0 0 {svg_w} {svg_h}" preserveAspectRatio="xMidYMid meet" style="width: 100%; height: 320px; display: block;">
                        {grid_svg}
                        {y_label_svg}
                        <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{svg_h - pad_b}" stroke="#243340" stroke-width="1" />
                        <line x1="{pad_l}" y1="{svg_h - pad_b}" x2="{svg_w - pad_r}" y2="{svg_h - pad_b}" stroke="#243340" stroke-width="1" />
                        {bar_svg}
                    </svg>
                </div>
                {legend_html}
            </div>
        </div>
    """


def _render_stories_burndown_html(sprint: dict, db_path: str) -> str:
    """Build the SVG burndown chart for one sprint's Stories.

    Returns '' if no daily snapshots exist for the sprint. Forecast/projection
    line is only drawn when we have at least one working day of data and a
    positive burn rate — otherwise that line collapses to a single point.
    """
    burndown = get_sprint_burndown(db_path, sprint['sprint_id'])
    if not burndown:
        return ''

    from datetime import timedelta

    start_date = parse_iso_tz(sprint['start_date']).date()
    end_date = parse_iso_tz(sprint['end_date']).date()
    today = datetime.now().date()

    axes = _build_burndown_axes(start_date, end_date, today)
    wd_total = axes['wd_total']
    wd_elapsed = axes['wd_elapsed']
    days_remaining = axes['days_remaining']
    wd_index_for = axes['wd_index_for']
    working_days = axes['working_days']

    burndown_in_sprint = [
        d for d in burndown
        if datetime.fromisoformat(d['snapshot_date']).date() >= start_date
    ]
    if not burndown_in_sprint:
        burndown_in_sprint = burndown

    burndown_by_date = {
        datetime.fromisoformat(d['snapshot_date']).date(): d
        for d in burndown_in_sprint
    }

    start_remaining = burndown_in_sprint[0].get('open_tickets', burndown_in_sprint[0].get('total_tickets', 0))
    current_remaining = burndown_in_sprint[-1].get('open_tickets', 0)
    ideal_remaining_today = start_remaining - (start_remaining / wd_total) * wd_elapsed if wd_total > 0 else 0
    ahead_behind = ideal_remaining_today - current_remaining

    tickets_burned = max(start_remaining - current_remaining, 0)
    tickets_per_wd = (tickets_burned / wd_elapsed) if wd_elapsed > 0 else 0
    projected_extra_wd = (current_remaining / tickets_per_wd) if tickets_per_wd > 0 else None
    projected_finish_date = None
    if projected_extra_wd is not None:
        remaining_wd = int(round(projected_extra_wd))
        cur = today
        while remaining_wd > 0:
            cur += timedelta(days=1)
            if _is_working_day(cur):
                remaining_wd -= 1
        projected_finish_date = cur

    svg_w, svg_h = 900, 280
    pad_l, pad_r, pad_t, pad_b = 46, 20, 18, 34
    inner_w = svg_w - pad_l - pad_r
    inner_h = svg_h - pad_t - pad_b
    max_remaining = max(
        start_remaining,
        max((d.get('open_tickets', 0) for d in burndown_in_sprint), default=start_remaining),
    )
    if max_remaining <= 0:
        max_remaining = 1

    def x_at(off):
        return pad_l + (off / wd_total) * inner_w if wd_total > 0 else pad_l

    def y_at(v):
        return pad_t + (1 - v / max_remaining) * inner_h

    axes['y_ticks'] = [
        (y_at(round(max_remaining * (4 - i) / 4)), round(max_remaining * (4 - i) / 4))
        for i in range(5)
    ]
    if wd_total <= 10:
        x_ticks_idx = list(range(wd_total + 1))
    else:
        step = max(1, wd_total // 7)
        x_ticks_idx = list(range(0, wd_total + 1, step))
        if wd_total not in x_ticks_idx:
            x_ticks_idx.append(wd_total)
    axes['x_ticks'] = [
        (x_at(idx), f"{working_days[min(idx, len(working_days)-1)].month}/{working_days[min(idx, len(working_days)-1)].day}")
        for idx in x_ticks_idx
    ]

    actual_points, actual_dots, seen = [], [], set()
    for d in sorted(burndown_by_date):
        xidx = wd_index_for(d)
        if xidx in seen:
            continue
        seen.add(xidx)
        y_val = burndown_by_date[d].get('open_tickets', 0)
        actual_points.append(f"{x_at(xidx):.1f},{y_at(y_val):.1f}")
        actual_dots.append((x_at(xidx), y_at(y_val)))

    ideal_points = [
        f"{x_at(0):.1f},{y_at(start_remaining):.1f}",
        f"{x_at(wd_total):.1f},{y_at(0):.1f}",
    ]
    projection_points = None
    if projected_extra_wd is not None and wd_elapsed > 0:
        projection_points = [
            f"{x_at(wd_elapsed):.1f},{y_at(current_remaining):.1f}",
            f"{x_at(wd_elapsed + projected_extra_wd):.1f},{y_at(0):.1f}",
        ]

    if ahead_behind > 0.5:
        pace_label = f"<span style='color: #6ee7c3;'>↑ {ahead_behind:.0f} ahead of ideal</span>"
    elif ahead_behind < -0.5:
        pace_label = f"<span style='color: #fda4a0;'>↓ {abs(ahead_behind):.0f} behind ideal</span>"
    else:
        pace_label = "<span style='color: #cdd9e5;'>on pace</span>"

    forecast_text = projected_finish_date.strftime('%b %d') if projected_finish_date else '—'
    forecast_delta = ''
    if projected_finish_date:
        delta_days = (projected_finish_date - end_date).days
        forecast_delta = (
            f" ({abs(delta_days)}d early)" if delta_days < 0
            else f" ({delta_days}d late)" if delta_days > 0
            else ' (on time)'
        )

    return _render_burndown_chart(
        title='📈 Sprint Burndown Chart',
        section_id=f'burndown-chart-{sprint["sprint_id"]}',
        axes=axes,
        series=[{'name': 'Actual', 'points': actual_points, 'dots': actual_dots, 'color': '#2dd4a7'}],
        summary_cards=[
            {'label': 'Remaining',  'value': str(current_remaining), 'sub': f'of {start_remaining} tickets'},
            {'label': 'Pace',       'value': pace_label, 'sub': f'ideal: {ideal_remaining_today:.0f} remaining'},
            {'label': 'Time Left',  'value': f'{days_remaining}d', 'sub': f'working day {wd_elapsed + 1} of {wd_total + 1}'},
            {'label': 'Forecast Finish', 'value': forecast_text, 'sub': f'at current pace{forecast_delta}'},
        ],
        legend=[
            {'kind': 'solid', 'color': '#2dd4a7', 'label': 'Actual'},
            {'kind': 'dashed', 'color': '#566375', 'label': 'Ideal'},
            {'kind': 'dashed', 'color': '#fbbf24', 'label': 'Projected'},
            {'kind': 'dashed', 'color': '#56cdf9', 'label': 'Today'},
        ],
        ideal_points=ideal_points,
        projection_points=projection_points,
        today_in_sprint=(start_date <= today <= end_date),
    )


def _render_flow_breakdown(db_path: str) -> str:
    """Render the flow section: time-in-status horizontal bars + churn callout.

    Team-wide, last 30 days (the status-history window). Returns '' when there
    isn't enough closed-interval data to draw bars. Time-in-status comes from
    the deduped `status_changes` table; churn from a DISTINCT'd read of
    `ticket_status_history` (see get_status_churn for the dedup rationale).
    """
    stages = get_time_in_status(db_path, days=30)
    churn = get_status_churn(db_path, days=30)
    if not stages:
        return ''

    max_h = max(s['avg_hours'] for s in stages) or 1
    # Stages that are queues/waits read amber; active-work stages read blue.
    wait_stages = {'To Do', 'Product Discovery', 'Engineering Unpacking',
                   'Committed', 'Blocked', 'Ready for Testing'}

    bars = ''
    for s in stages:
        pct = s['avg_hours'] / max_h * 100
        days = s['avg_hours'] / 24
        val_label = f"{s['avg_hours']:.0f}h" if s['avg_hours'] < 48 else f"{days:.1f}d"
        color = '#fbbf24' if s['status'] in wait_stages else '#38bdf8'
        bars += f"""
            <div class="flow-stage-row">
                <div class="flow-stage-name">{s['status']}</div>
                <div class="flow-stage-track">
                    <div class="flow-stage-fill" style="width: {pct:.0f}%; background: {color};"></div>
                </div>
                <div class="flow-stage-val">{val_label} <span style="color: var(--text-faint); font-weight: 400;">· n={s['sample']}</span></div>
            </div>
        """

    # Churn callout — only meaningful when there's at least one bounce.
    if churn['total'] > 0:
        parts = []
        if churn['review_bounces']:
            parts.append(f"{churn['review_bounces']} review bounce{'es' if churn['review_bounces'] != 1 else ''}")
        if churn['reopens']:
            parts.append(f"{churn['reopens']} reopen{'s' if churn['reopens'] != 1 else ''}")
        churn_detail = ' · '.join(parts)
        churn_html = f"""
            <div class="flow-churn">
                <span class="flow-churn-val">{churn['total']}</span>
                <span class="flow-churn-label">backward transitions (last 30d) — {churn_detail}.
                Work bouncing back from review/done signals rework or unclear scope.</span>
            </div>
        """
    else:
        churn_html = """
            <div class="flow-churn flow-churn-clean">
                <span class="flow-churn-val">0</span>
                <span class="flow-churn-label">backward transitions in the last 30 days — work is flowing forward cleanly.</span>
            </div>
        """

    return f"""
        <div class="flow-breakdown" style="margin-bottom: 16px;">
            <div class="flow-breakdown-title">⏳ Time in Status <span style="color: var(--text-faint); font-weight: 400; font-size: 12px;">— team-wide, avg working hours, last 30 days</span></div>
            <div class="flow-stages">{bars}</div>
            {churn_html}
        </div>
    """


def _render_stories_sprint_block(sprint: dict, db_path: str, config: dict, *, is_active: bool) -> str:
    """Render one collapsible Stories block for a single sprint.

    Active sprint also gets a team-metrics row (cycle time, throughput, PR
    review time, sprint commitment). Closed sprints stick to burndown +
    BE/FE breakdown so the page stays readable as history grows. Closed
    sprints place tickets by `status_at_sprint_end` so the bucket counts
    match the Sprint Reports page.
    """
    sprint_id = sprint['sprint_id']
    id_suffix = f"-{sprint_id}"

    closed_set = set(CLOSED_STATUSES)
    inprog_set = set(IN_PROGRESS_STATUSES)
    open_set = set(OPEN_STATUSES)
    all_statuses = list(closed_set | inprog_set | open_set)

    placeholders = ",".join("?" for _ in all_statuses)
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT ticket_key, summary, status,
                   COALESCE(status_at_sprint_end, status) AS sprint_end_status,
                   assignee_account_id, assignee_display_name, story_points,
                   issue_type, ticket_url
              FROM tickets
             WHERE sprint_id = ?
               AND issue_type = 'Story'
               AND COALESCE(status_at_sprint_end, status) IN ({placeholders})
             ORDER BY ticket_key
            """,
            [sprint_id] + all_statuses,
        )
        all_tickets = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    closed_tickets_list = [t for t in all_tickets if t['sprint_end_status'] in closed_set]
    in_progress_tickets_list = [t for t in all_tickets if t['sprint_end_status'] in inprog_set]
    open_tickets_list = [t for t in all_tickets if t['sprint_end_status'] in open_set]

    name_to_role, _ = _build_role_maps(config)
    closed_by_role = _partition_tickets_by_role(closed_tickets_list, name_to_role)
    inprog_by_role = _partition_tickets_by_role(in_progress_tickets_list, name_to_role)
    open_by_role = _partition_tickets_by_role(open_tickets_list, name_to_role)
    role_m = _role_metrics({'closed': closed_by_role, 'in_progress': inprog_by_role, 'open': open_by_role})

    # Sum across ALL reported roles (BE+FE+other) so the banner total equals
    # the sum of the role blocks below and reconciles with Sprint Reports.
    banner_closed = sum(role_m[r]['closed'] for r in _REPORTED_ROLES)
    banner_in_progress = sum(role_m[r]['in_progress'] for r in _REPORTED_ROLES)
    banner_open = sum(role_m[r]['open'] for r in _REPORTED_ROLES)
    banner_total = banner_closed + banner_in_progress + banner_open
    banner_completion = (banner_closed / banner_total * 100) if banner_total > 0 else 0
    banner_html = (
        f'<span class="epic-sprint-stat" style="color: #2dd4a7;">{banner_closed} <small>closed</small></span>'
        f'<span class="epic-sprint-stat" style="color: #fbbf24;">{banner_in_progress + banner_open} <small>remaining</small></span>'
        f'<span class="epic-sprint-stat" style="color: #38bdf8;">{banner_total} <small>total</small></span>'
        f'<span class="epic-sprint-stat" style="color: #7dd3fc;">{banner_completion:.0f}% <small>complete</small></span>'
        f'{_scope_change_chip(db_path, sprint_id)}'
    )

    is_empty = banner_total == 0
    empty_class = ' epic-sprint-empty' if is_empty else ''
    open_attr = ' open' if is_active else ''

    burndown_html = _render_stories_burndown_html(sprint, db_path)

    # Team-metrics row only shown for the active sprint. Cycle/throughput are
    # in-flight indicators; PR-review-time is a 30-day team-wide rolling
    # window that doesn't make sense to repeat per closed sprint.
    team_metrics_html = ''
    if is_active:
        team_cycle_time = get_team_cycle_time(db_path, sprint_id)
        team_throughput = get_team_throughput(db_path, sprint_id, days=7)
        team_pr_review_time = get_team_pr_review_time(db_path, days=30)
        commitment_accuracy = get_sprint_commitment_accuracy(db_path, sprint_id)
        # Flow metrics (team-wide, 30-day window — status history only spans
        # ~early May, so a per-sprint slice would be too thin to average).
        blocked = get_blocked_time(db_path, days=30)
        commit_card_class = (
            'success' if commitment_accuracy['accuracy'] >= 80
            else 'warning' if commitment_accuracy['accuracy'] >= 60
            else 'info'
        )
        # Blocked card turns amber only when something is blocked right now.
        blocked_card_class = 'warning' if blocked['currently_blocked'] > 0 else ''
        blocked_value = (
            f"{blocked['currently_blocked']}"
            if blocked['currently_blocked'] > 0
            else f"{blocked['ticket_count']}"
        )
        blocked_sub = (
            f"blocked now · {blocked['total_hours']/24:.1f}d total (30d)"
            if blocked['currently_blocked'] > 0
            else f"blocked in last 30d · {blocked['total_hours']/24:.1f}d total"
        )
        team_metrics_html = f"""
            <div class="metrics-grid" style="margin-bottom: 16px;">
                <div class="metric-card info">
                    <div class="metric-label">Avg Cycle Time</div>
                    <div class="metric-value">{f"{team_cycle_time:.1f}" if team_cycle_time else "N/A"}</div>
                    <div class="metric-subtext">days to complete</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">Throughput</div>
                    <div class="metric-value">{team_throughput}</div>
                    <div class="metric-subtext">tickets per week</div>
                </div>
                <div class="metric-card">
                    <div class="metric-label">PR Review Time</div>
                    <div class="metric-value">{f"{team_pr_review_time:.0f}h" if team_pr_review_time else "N/A"}</div>
                    <div class="metric-subtext">avg to merge</div>
                </div>
                <div class="metric-card {commit_card_class}">
                    <div class="metric-label">Sprint Commitment</div>
                    <div class="metric-value">{commitment_accuracy['accuracy']}%</div>
                    <div class="metric-subtext">{commitment_accuracy['completed']}/{commitment_accuracy['planned']} tickets</div>
                </div>
                <div class="metric-card {blocked_card_class}">
                    <div class="metric-label">Blocked</div>
                    <div class="metric-value">{blocked_value}</div>
                    <div class="metric-subtext">{blocked_sub}</div>
                </div>
            </div>
            {_render_flow_breakdown(db_path)}
        """

    out = f"""
        <details class="epic-sprint-block{empty_class}"{open_attr}>
            <summary class="epic-sprint-summary">
                <span class="epic-sprint-caret">▸</span>
                <span class="epic-sprint-name">{fmt_sprint_long(sprint['sprint_name'])}</span>
                <span class="epic-sprint-counts">{banner_html}</span>
            </summary>
            <div class="epic-sprint-body">
                {burndown_html}
                {team_metrics_html}
    """

    if is_empty:
        out += (
            '<div style="color:var(--text-muted); font-size:13px; '
            'font-style:italic; padding:8px 4px;">No Story tickets recorded for this sprint.</div>'
        )

    role_config = [
        ('BE', 'be', '⚙️ Backend'),
        ('FE', 'fe', '🎨 Frontend'),
        ('other', 'other', '👥 Other / Unassigned'),
    ]
    role_color = {'BE': '#2dd4a7', 'FE': '#c4b5fd', 'other': '#8194a6'}
    for role, rid_base, role_label in role_config:
        rm = role_m[role]
        # Only surface the catch-all block when it holds work, so a
        # well-rostered sprint isn't cluttered with an empty "Other" row.
        if role == 'other' and rm['total'] == 0:
            continue
        rid = f"{rid_base}{id_suffix}"
        r_closed = closed_by_role[role]
        r_inprog = inprog_by_role[role]
        r_open = open_by_role[role]
        role_key = role.lower()
        accent = role_color.get(role, '#38bdf8')

        out += f"""
            <details class="epic-role-block role-{role_key}">
                <summary class="epic-role-summary">
                    <span class="epic-role-caret">▸</span>
                    <span class="epic-role-name">{role_label}</span>
                    <span class="epic-role-counts">
                        <span class="epic-role-stat" style="color: #2dd4a7;">{rm['closed']} <small>closed</small></span>
                        <span class="epic-role-stat" style="color: #fbbf24;">{rm['in_progress']} <small>in progress</small></span>
                        <span class="epic-role-stat" style="color: #38bdf8;">{rm['open']} <small>open</small></span>
                        <span class="epic-role-stat" style="color: {accent};">{rm['total']} <small>total</small></span>
                        <span class="epic-role-stat" style="color: #7dd3fc;">{rm['completion']:.0f}% <small>complete</small></span>
                    </span>
                </summary>
                <div class="epic-role-body">
                    <div class="metrics-grid">
                        <button type="button" class="metric-card success" onclick="toggleAccordion('{rid}-closed-panel')" aria-controls="{rid}-closed-panel" aria-expanded="false">
                            <div class="metric-label">Closed</div>
                            <div class="metric-value clickable">{rm['closed']}</div>
                            <div class="metric-subtext">{rm['completion']:.1f}% complete · Click to view</div>
                        </button>
                        <button type="button" class="metric-card warning" onclick="toggleAccordion('{rid}-inprogress-panel')" aria-controls="{rid}-inprogress-panel" aria-expanded="false">
                            <div class="metric-label">In Progress</div>
                            <div class="metric-value clickable">{rm['in_progress']}</div>
                            <div class="metric-subtext">Click to view</div>
                        </button>
                        <button type="button" class="metric-card info" onclick="toggleAccordion('{rid}-open-panel')" aria-controls="{rid}-open-panel" aria-expanded="false">
                            <div class="metric-label">Open / To Do</div>
                            <div class="metric-value clickable">{rm['open']}</div>
                            <div class="metric-subtext">Click to view</div>
                        </button>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: {rm['completion']}%"></div>
                    </div>

                    <div id="{rid}-closed-panel" class="accordion-panel">
                        <div class="accordion-content">
                            <div class="accordion-header">✅ Closed Tickets ({len(r_closed)})</div>
                            <div class="ticket-grid">
        """

        for ticket in r_closed:
            out += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    <span style="color: #566375; font-size: 12px;"> • {ticket['assignee_display_name'] or 'Unassigned'}</span>
                                </div>
            """

        out += f"""
                            </div>
                        </div>
                    </div>

                    <div id="{rid}-inprogress-panel" class="accordion-panel">
                        <div class="accordion-content">
                            <div class="accordion-header">🔄 In Progress Tickets ({len(r_inprog)})</div>
                            <div class="ticket-grid">
        """

        for ticket in r_inprog:
            out += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    <span style="color: #566375; font-size: 12px;"> • {ticket['assignee_display_name'] or 'Unassigned'} • {ticket['status']}</span>
                                </div>
            """

        out += f"""
                            </div>
                        </div>
                    </div>

                    <div id="{rid}-open-panel" class="accordion-panel">
                        <div class="accordion-content">
                            <div class="accordion-header">📋 Open / To Do Tickets ({len(r_open)})</div>
                            <div class="ticket-grid">
        """

        for ticket in r_open:
            out += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    <span style="color: #566375; font-size: 12px;"> • {ticket['assignee_display_name'] or 'Unassigned'} • {ticket['status']}</span>
                                </div>
            """

        out += """
                            </div>
                        </div>
                    </div>
                </div>
            </details>
        """

    out += """
            </div>
        </details>
    """
    return out


def generate_team_html(config: dict, output_path: Path):
    """Generate HTML team dashboard."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    active_sprint = get_current_sprint(db_path, sprint_prefix)

    # Pull every FNTSY sprint (active + closed) so each gets its own
    # collapsible block. Newest-first: the active sprint sits at the top
    # of the list and the rest read backwards through history.
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT sprint_id, jira_sprint_id, sprint_name, state, start_date, end_date, goal
              FROM sprints
             WHERE sprint_name LIKE ? || '%'
               AND COALESCE(is_placeholder, 0) = 0
               AND state IN ('active', 'closed')
             ORDER BY start_date DESC
            """,
            (sprint_prefix,),
        )
        all_sprints = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    if not all_sprints:
        print("No sprints found")
        return

    header_sprint = active_sprint or all_sprints[0]
    active_sprint_id = active_sprint['sprint_id'] if active_sprint else None

    completion_bar_chart_html = _render_sprint_story_bar_chart(
        db_path, sprint_prefix, config, current_sprint_id=active_sprint_id,
    )

    sprint_blocks_html = ''.join(
        _render_stories_sprint_block(
            s, db_path, config,
            is_active=(s['sprint_id'] == active_sprint_id),
        )
        for s in all_sprints
    )

    page = f"""
        <header>
            <h1>📊 Team Dashboard</h1>
            <div class="subtitle">{fmt_sprint_long(header_sprint['sprint_name'])} • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('stories')}
        <div class="content">
{completion_bar_chart_html}
{sprint_blocks_html}
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    html = render_html(
        title=f"Team Dashboard - {fmt_sprint_long(header_sprint['sprint_name'])}",
        content=page,
        body_class=_PAGE_THEME["stories"],
    )

    _atomic_write(output_path, html)
    print(f"Team HTML dashboard generated: {output_path}")


def _member_filename(dev_name: str) -> str:
    """Filename used for each per-member page."""
    return f"member_{dev_name.replace(' ', '_')}.html"


def _compute_dev_status(dev, expected_completion_pct):
    """Return the quick-jump status bucket (on-track / at-risk / needs-attention) for a dev."""
    completed = dev['tickets_completed']
    in_progress = dev['tickets_in_progress']
    todo = dev['tickets_todo']
    total = completed + in_progress + todo
    rate = (completed / total * 100) if total > 0 else 0
    gap = rate - expected_completion_pct

    if total == 0 or (completed == 0 and in_progress == 0):
        return 'needs-attention'
    if gap < -35:
        return 'needs-attention'
    if gap < -15 or in_progress > 5:
        return 'at-risk'
    return 'on-track'


def _build_member_sub_nav(developers, dev_status_map, active_dev_name=None, dev_role_map=None):
    """Build the secondary nav row of team-member pills split into BE / FE groups."""
    dev_role_map = dev_role_map or {}
    summary_counts = {'on-track': 0, 'at-risk': 0, 'needs-attention': 0}
    for s in dev_status_map.values():
        summary_counts[s] = summary_counts.get(s, 0) + 1

    def _pill(dev):
        dev_name = dev['developer_name']
        pill_status = dev_status_map.get(dev['developer_id'], 'on-track')
        active_cls = ' active' if dev_name == active_dev_name else ''
        return (
            f'<a href="{_member_filename(dev_name)}" class="member-pill {pill_status}{active_cls}">'
            f'<span class="dot"></span>{dev_name}</a>'
        )

    sorted_devs = sorted(developers, key=lambda x: x['developer_name'])
    be_devs = [d for d in sorted_devs if dev_role_map.get(d['developer_name'], 'BE') == 'BE']
    fe_devs = [d for d in sorted_devs if dev_role_map.get(d['developer_name'], 'BE') == 'FE']

    be_pills = ''.join(_pill(d) for d in be_devs)
    fe_pills = ''.join(_pill(d) for d in fe_devs)

    groups_html = ''
    if be_pills:
        groups_html += f'<div class="member-pills-group"><span class="member-pills-label">Backend</span><div class="member-pills">{be_pills}</div></div>'
    if fe_pills:
        groups_html += f'<div class="member-pills-group"><span class="member-pills-label">Frontend</span><div class="member-pills">{fe_pills}</div></div>'

    return f"""
        <nav class="sub-nav member-sub-nav">
            <div class="member-sub-nav-inner">
                <div class="member-sub-nav-legend">
                    <span><span class="legend-dot on-track"></span>On Track · {summary_counts['on-track']}</span>
                    <span><span class="legend-dot at-risk"></span>At Risk · {summary_counts['at-risk']}</span>
                    <span><span class="legend-dot needs-attention"></span>Needs Attention · {summary_counts['needs-attention']}</span>
                </div>
                {groups_html}
            </div>
        </nav>
    """


def _build_member_card_html(dev, config, db_path, sprint,
                             sprint_elapsed_days, sprint_total_days,
                             sprint_days_remaining, expected_completion_pct,
                             *,
                             bulk):
    """Return the HTML for one developer's card (card + ticket accordions).

    `bulk` is a dict of pre-fetched per-developer data (see
    generate_team_members_html). Avoids issuing 7 DB queries per dev × N devs.

    Returns a tuple (card_html, status_bucket) — status_bucket is what the
    sub-nav uses to color the pill.
    """
    dev_name = dev['developer_name']
    dev_id = dev['developer_id']

    completed = dev['tickets_completed']
    in_progress = dev['tickets_in_progress']
    todo = dev['tickets_todo']
    total_assigned = completed + in_progress + todo

    dev_tickets = bulk['tickets'].get(dev_id, {})

    # Bucket via the shared taxonomy so the Team Members page agrees with
    # Stories / Story Points / SP-consistency checks.
    completed_tickets = []
    in_progress_tickets = []
    todo_tickets = []
    for ticket_status, tickets in dev_tickets.items():
        if ticket_status in CLOSED_STATUSES:
            completed_tickets.extend(tickets)
        elif ticket_status in IN_PROGRESS_STATUSES:
            in_progress_tickets.extend(tickets)
        elif ticket_status in OPEN_STATUSES:
            todo_tickets.extend(tickets)
        # EXCLUDED_STATUSES (Abandoned/Duplicate) are filtered out at the SQL
        # level by get_developer_tickets_bulk.

    def _sum_sp(tickets):
        return sum((t.get('story_points') or 0) for t in tickets)

    completed_sp = _sum_sp(completed_tickets)
    in_progress_sp = _sum_sp(in_progress_tickets)
    todo_sp = _sum_sp(todo_tickets)
    total_sp = completed_sp + in_progress_sp + todo_sp
    sp_completion_rate = (completed_sp / total_sp * 100) if total_sp > 0 else 0

    github_username = bulk['id_to_github'].get(dev_id)
    dev_level = bulk['id_to_level'].get(dev_id)

    pr_metrics = bulk['pr_metrics'].get(github_username) if github_username else None
    review_metrics = bulk['review_metrics'].get(github_username) if github_username else None

    dev_cycle_time = bulk['cycle_time'].get(dev_id)
    dev_throughput = bulk['throughput'].get(dev_id, 0.0)
    meeting_info = bulk['meetings'].get(dev_name)

    # Real cycle-time-per-point from ticket_status_history when we have it.
    # Falls back to a proxy (team cycle time / avg SP per completed ticket) when
    # history is still accumulating — we mark that case with "~" in the UI.
    cycle_per_point = bulk['cycle_per_point'].get(dev_id)
    cycle_per_point_is_proxy = False
    if cycle_per_point is None:
        avg_sp_per_ticket = (completed_sp / len(completed_tickets)) if completed_tickets else 0
        if dev_cycle_time and avg_sp_per_ticket > 0:
            cycle_per_point = dev_cycle_time / avg_sp_per_ticket
            cycle_per_point_is_proxy = True

    # SP throughput over the elapsed sprint window, normalized to a 7-day rate.
    sp_throughput_per_week = (
        (completed_sp / sprint_elapsed_days) * 7
        if sprint_elapsed_days > 0 else 0
    )

    completion_rate = (completed / total_assigned * 100) if total_assigned > 0 else 0
    pace_gap = completion_rate - expected_completion_pct

    insights = []
    concerns = []
    status = "on-track"
    status_text = "On Track"

    if total_assigned > 0:
        pace_summary = (
            f"{completion_rate:.0f}% complete vs ~{expected_completion_pct:.0f}% expected "
            f"at day {sprint_elapsed_days} of {sprint_total_days}"
        )
        if pace_gap >= 10:
            insights.append(("🚀", f"Ahead of pace — {pace_summary} ({completed}/{total_assigned} tickets)", "positive"))
        elif pace_gap >= -15:
            insights.append(("✅", f"On pace — {pace_summary} ({completed}/{total_assigned} tickets)", "positive"))
        elif pace_gap >= -35:
            insights.append(("⚠️", f"Slightly behind pace — {pace_summary} ({completed}/{total_assigned} tickets)", "concern"))
            status, status_text = "at-risk", "At Risk"
        else:
            concerns.append(("🚨", f"Well behind pace — {pace_summary} ({completed}/{total_assigned} tickets)", "critical"))
            status, status_text = "needs-attention", "Needs Attention"

    if in_progress > 5:
        concerns.append(("⚠️", f"High WIP with {in_progress} tickets in progress - may indicate blockers or context switching", "concern"))
        if status == "on-track":
            status, status_text = "at-risk", "At Risk"
    elif in_progress >= 2 and in_progress <= 4:
        insights.append(("✅", f"Healthy WIP with {in_progress} tickets in progress", "positive"))
    elif in_progress == 1:
        insights.append(("✅", f"Focused work with {in_progress} ticket in progress", "positive"))

    if pr_metrics:
        open_prs = pr_metrics['open_pr_count']
        avg_merge_time = pr_metrics['avg_hours_to_merge']
        merged_count = pr_metrics['merged_pr_count_last_n_days']

        if merged_count >= 10:
            insights.append(("🚀", f"High PR throughput with {merged_count} PRs merged in last 30 days", "positive"))
        elif merged_count >= 5:
            insights.append(("✅", f"Good PR activity with {merged_count} PRs merged in last 30 days", "positive"))

        if avg_merge_time:
            if avg_merge_time < 24:
                insights.append(("⚡", f"Fast PR turnaround averaging {avg_merge_time:.1f} hours to merge", "positive"))
            elif avg_merge_time > 72:
                concerns.append(("⏱️", f"Slow PR cycle time averaging {avg_merge_time:.1f} hours to merge - may need review process improvement", "concern"))

        if open_prs > 5:
            concerns.append(("📝", f"{open_prs} open PRs - may indicate review bottleneck or stale branches", "concern"))
        elif open_prs > 0:
            insights.append(("📝", f"{open_prs} open PRs awaiting review", "positive"))

    if review_metrics:
        if review_metrics['approvals'] == 0:
            concerns.append(("👀", "No PR approvals given in the last 90 days — not participating in code review", "concern"))
            if status == "on-track":
                status, status_text = "at-risk", "At Risk"
        if review_metrics['pr_comments'] == 0:
            concerns.append(("💬", "No PR comments left in the last 90 days — not giving feedback on others' PRs", "concern"))
            if status == "on-track":
                status, status_text = "at-risk", "At Risk"

    if total_assigned == 0:
        concerns.append(("❓", "No tickets assigned in current sprint - may need work allocation", "critical"))
        status, status_text = "needs-attention", "Needs Attention"
    elif completed == 0 and in_progress == 0:
        concerns.append(("⚠️", "No progress on assigned tickets - check for blockers or availability", "critical"))
        status, status_text = "needs-attention", "Needs Attention"

    # Capacity check: the team's baseline is 8 SP/sprint/engineer. Flag
    # anyone under-capacity so it shows up on the card.
    SP_CAPACITY_FLOOR = 8
    if total_sp < SP_CAPACITY_FLOOR:
        concerns.append((
            "📉",
            f"Under capacity — only {total_sp:g} SP assigned this sprint (target {SP_CAPACITY_FLOOR} SP/engineer)",
            "concern",
        ))
        if status == "on-track":
            status, status_text = "at-risk", "At Risk"

    if total_assigned > 0 and completed > 0:
        remaining = todo + in_progress
        elapsed = max(sprint_elapsed_days, 1)
        tickets_per_day = completed / elapsed
        if tickets_per_day > 0:
            days_to_complete = remaining / tickets_per_day
            if days_to_complete <= sprint_days_remaining:
                insights.append(("📈", f"On pace to finish remaining {remaining} tickets in ~{days_to_complete:.0f} days (sprint has {sprint_days_remaining} left)", "positive"))
            elif days_to_complete <= sprint_days_remaining * 1.5:
                insights.append(("📊", f"Slightly off pace — {remaining} tickets would take ~{days_to_complete:.0f} days, sprint has {sprint_days_remaining} left", "concern"))
            else:
                concerns.append(("📉", f"Unlikely to finish — {remaining} tickets would take ~{days_to_complete:.0f} days at current pace, sprint has {sprint_days_remaining} left", "concern"))

    meeting_html = ""
    if meeting_info:
        next_meeting = ""
        if meeting_info.get('next_occurrence'):
            try:
                next_dt = parse_iso_tz(meeting_info['next_occurrence'])
                next_meeting = next_dt.strftime('%b %d')
            except Exception:
                pass
        day = meeting_info.get('day_of_week', 'N/A')
        time_str = meeting_info.get('time_of_day', 'N/A')
        duration = meeting_info.get('duration_minutes', 0)
        next_bit = f" · next {next_meeting}" if next_meeting else ""
        meeting_html = f"""
                    <div class="performance-meeting" title="1-on-1 · {duration} min">
                        <span>📅</span>
                        <span><strong>{day} {time_str}</strong>{next_bit}</span>
                    </div>
                """

    level_html = (
        f'<span class="performance-level" title="Engineering level">{dev_level}</span>'
        if dev_level else ''
    )
    competency_btn = ''
    if dev_level and dev_level in TITLE_TO_LEVEL:
        competency_btn = (
            f'<button type="button" class="nav-link secondary competency-btn" '
            f'data-level-title="{dev_level}" '
            f'data-dev-name="{dev_name}">View Competencies</button>'
        )

    edit_btn = (
        f'<button type="button" class="nav-link secondary member-edit-btn" '
        f'data-dev-name="{html.escape(dev_name)}" '
        f'data-github-username="{html.escape(github_username or "")}" '
        f'data-jira-account-id="{html.escape(dev_id or "")}" '
        f'data-level="{html.escape(dev_level or "")}">Edit Details</button>'
    )

    card = f"""
                <div class="performance-card {status}">
                    <div class="performance-header">
                        <div class="performance-header-left">
                            <div class="performance-name">{dev_name}</div>
                            {level_html}
                            {meeting_html}
                        </div>
                        <div class="performance-header-right">
                            {competency_btn}
                            {edit_btn}
                            <div class="performance-status {status}">{status_text}</div>
                        </div>
                    </div>
            """

    # Assessments (concerns + insights) — moved to the top so they read before
    # the per-section metrics. Empty when nothing notable is flagged.
    if concerns or insights:
        card += '\n                    <div class="insights-section">\n'
        for icon, text, concern_type in concerns:
            card += f"""
                        <div class="insight-item insight-{concern_type}">
                            <div class="insight-icon">{icon}</div>
                            <div class="insight-text">{text}</div>
                        </div>
                """
        for icon, text, insight_type in insights:
            card += f"""
                        <div class="insight-item insight-{insight_type}">
                            <div class="insight-icon">{icon}</div>
                            <div class="insight-text">{text}</div>
                        </div>
                """
        card += '\n                    </div>\n'

    # Current Sprint section — collapsible. Holds Jira metrics, Story Points,
    # GitHub activity, and the per-status ticket accordions.
    card += f"""
                    <details class="member-current-sprint">
                        <summary class="member-current-sprint-summary">
                            <span class="member-current-sprint-chevron" aria-hidden="true">▶</span>
                            <span class="member-current-sprint-title">Current Sprint · {fmt_sprint_long(sprint['sprint_name'])}</span>
                            <span class="member-current-sprint-meta">{total_assigned} tickets · {total_sp:g} SP · {completed} completed</span>
                        </summary>
                        <div class="member-current-sprint-body">

                    <div class="metric-group">
                        <div class="metric-group-title">🎫 Jira · Stories</div>
                        <div class="performance-metrics">
                            <div class="perf-metric">
                                <div class="perf-metric-label">Total Stories</div>
                                <div class="perf-metric-value compact">{total_assigned}</div>
                            </div>
                            <div class="perf-metric" title="Actual completion vs expected given sprint elapsed time">
                                <div class="perf-metric-label">Completion Rate</div>
                                <div class="perf-metric-value">{completion_rate:.0f}%</div>
                                <div class="perf-metric-subtext">expected ~{expected_completion_pct:.0f}% (day {sprint_elapsed_days}/{sprint_total_days})</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">Completed</div>
                                <button type="button" class="perf-metric-value clickable success" onclick="toggleAccordion('{dev_id}-completed')" aria-controls="{dev_id}-completed" aria-expanded="false">{completed}</button>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">In Progress</div>
                                <button type="button" class="perf-metric-value clickable warning" onclick="toggleAccordion('{dev_id}-inprogress')" aria-controls="{dev_id}-inprogress" aria-expanded="false">{in_progress}</button>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">To Do</div>
                                <button type="button" class="perf-metric-value clickable info" onclick="toggleAccordion('{dev_id}-todo')" aria-controls="{dev_id}-todo" aria-expanded="false">{todo}</button>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">Cycle Time</div>
                                <div class="perf-metric-value compact">{f"{dev_cycle_time:.1f}d" if dev_cycle_time else "N/A"}</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">Throughput</div>
                                <div class="perf-metric-value compact">{dev_throughput}/wk</div>
                            </div>
                        </div>
                    </div>

                    <div class="metric-group">
                        <div class="metric-group-title">📏 Jira · Story Points</div>
                        <div class="performance-metrics">
                            <div class="perf-metric">
                                <div class="perf-metric-label">Total SP</div>
                                <div class="perf-metric-value compact">{total_sp:g}</div>
                            </div>
                            <div class="perf-metric" title="Actual SP completion vs expected given sprint elapsed time">
                                <div class="perf-metric-label">SP Completion</div>
                                <div class="perf-metric-value">{sp_completion_rate:.0f}%</div>
                                <div class="perf-metric-subtext">expected ~{expected_completion_pct:.0f}% (day {sprint_elapsed_days}/{sprint_total_days})</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">Completed SP</div>
                                <div class="perf-metric-value" style="color: #2dd4a7;">{completed_sp:g}</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">In Progress SP</div>
                                <div class="perf-metric-value" style="color: #fbbf24;">{in_progress_sp:g}</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">To Do SP</div>
                                <div class="perf-metric-value" style="color: #38bdf8;">{todo_sp:g}</div>
                            </div>
                            <div class="perf-metric" title="{'Estimate: (cycle_time / avg_SP_per_ticket). No per-ticket history yet; real value once status history accumulates.' if cycle_per_point_is_proxy else 'Real avg days-per-point from ticket status history'}">
                                <div class="perf-metric-label">Cycle Time / Point</div>
                                <div class="perf-metric-value compact">{('~' + f'{cycle_per_point:.1f}d') if (cycle_per_point and cycle_per_point_is_proxy) else (f'{cycle_per_point:.1f}d' if cycle_per_point else 'N/A')}</div>
                            </div>
                            <div class="perf-metric" title="Story points completed per week during this sprint">
                                <div class="perf-metric-label">SP Throughput</div>
                                <div class="perf-metric-value compact">{sp_throughput_per_week:.1f}/wk</div>
                            </div>
                        </div>
                    </div>
            """

    if pr_metrics or review_metrics:
        card += """
                    <div class="metric-group">
                        <div class="metric-group-title">🐙 GitHub · Activity</div>
                        <div class="performance-metrics">
                """
        if pr_metrics:
            card += f"""
                            <div class="perf-metric">
                                <div class="perf-metric-label">PRs (30d)</div>
                                <div class="perf-metric-value">{pr_metrics['merged_pr_count_last_n_days']}</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">Open PRs</div>
                                <div class="perf-metric-value">{pr_metrics['open_pr_count']}</div>
                            </div>
                    """
            if pr_metrics['avg_hours_to_merge']:
                card += f"""
                            <div class="perf-metric">
                                <div class="perf-metric-label">Avg Merge Time</div>
                                <div class="perf-metric-value compact">{pr_metrics['avg_hours_to_merge']:.0f}h</div>
                            </div>
                        """
        if review_metrics:
            card += f"""
                            <div class="perf-metric" title="PR reviews APPROVED in the last 90 days">
                                <div class="perf-metric-label">Approvals (90d)</div>
                                <div class="perf-metric-value">{review_metrics['approvals']}</div>
                            </div>
                            <div class="perf-metric" title="PR reviews with CHANGES_REQUESTED in the last 90 days">
                                <div class="perf-metric-label">Changes Req. (90d)</div>
                                <div class="perf-metric-value">{review_metrics['changes_requested']}</div>
                            </div>
                            <div class="perf-metric" title="Inline comments left on code diffs in the last 90 days">
                                <div class="perf-metric-label">Review Comments (90d)</div>
                                <div class="perf-metric-value">{review_metrics['review_comments']}</div>
                            </div>
                            <div class="perf-metric" title="Issue-level comments on PRs in the last 90 days">
                                <div class="perf-metric-label">PR Comments (90d)</div>
                                <div class="perf-metric-value">{review_metrics['pr_comments']}</div>
                            </div>
                    """
        card += """
                        </div>
                    </div>
                """

    # Ticket accordions
    card += f"""
                    <div id="{dev_id}-completed" class="accordion-panel">
                        <div class="accordion-content">
                            <div class="accordion-header">✅ Completed Tickets ({len(completed_tickets)})</div>
                            <div class="ticket-grid">
            """
    for ticket in completed_tickets:
        card += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                </div>
                """
    card += f"""
                            </div>
                        </div>
                    </div>
                    <div id="{dev_id}-inprogress" class="accordion-panel">
                        <div class="accordion-content">
                            <div class="accordion-header">🔄 In Progress Tickets ({len(in_progress_tickets)})</div>
                            <div class="ticket-grid">
            """
    for ticket in in_progress_tickets:
        card += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                </div>
                """
    card += f"""
                            </div>
                        </div>
                    </div>
                    <div id="{dev_id}-todo" class="accordion-panel">
                        <div class="accordion-content">
                            <div class="accordion-header">📋 To Do Tickets ({len(todo_tickets)})</div>
                            <div class="ticket-grid">
            """
    for ticket in todo_tickets:
        card += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                </div>
                """
    card += """
                            </div>
                        </div>
                    </div>
                        </div>
                    </details>
            """

    # Append a "Next Sprint" section for the role-matched upcoming sprint.
    dev_role = None
    for member in config.get('team_members', []):
        if member.get('jira_account_id') == dev_id:
            dev_role = member.get('role')
            break
    card += _build_member_next_sprint_html(
        db_path, dev_id, config['jira']['sprint_prefix'], dev_role,
    )

    # Append a "Past Sprints" section showing how this engineer did in each
    # closed sprint. Sprint-end status drives the rollups so rolled-over
    # tickets aren't credited (or punished) by post-sprint movement.
    card += _build_member_past_sprints_html(
        db_path, dev_id, config['jira']['sprint_prefix'],
        github_username=github_username,
    )

    # Close the performance-card wrapper now that everything is appended.
    card += "\n                </div>\n"

    return card, status


def _build_member_next_sprint_html(db_path: str, dev_id: str, sprint_prefix: str, dev_role: str) -> str:
    """Return a collapsible 'Next Sprint' block for this developer.

    Finds the soonest future sprint whose name ends with the dev's role suffix
    (e.g. '… FE' or '… BE'). If no role-suffixed sprint exists yet, returns ''.
    """
    if not dev_role:
        return ''

    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        # Find the earliest future sprint whose name includes the role suffix.
        cursor.execute(
            """
            SELECT sprint_id, sprint_name, start_date, end_date
              FROM sprints
             WHERE sprint_name LIKE ? || '%'
               AND state = 'future'
               AND (sprint_name LIKE '% FE' OR sprint_name LIKE '% BE')
               AND sprint_name LIKE ? || ' %'
             ORDER BY start_date ASC
             LIMIT 1
            """,
            (sprint_prefix, f'% {dev_role}'),
        )
        # sqlite LIKE is positional — rebuild with correct suffix pattern
        cursor.execute(
            """
            SELECT sprint_id, sprint_name, start_date, end_date
              FROM sprints
             WHERE sprint_name LIKE ? || '%'
               AND state = 'future'
               AND sprint_name LIKE ?
             ORDER BY start_date ASC
             LIMIT 1
            """,
            (sprint_prefix, f'% {dev_role}'),
        )
        row = cursor.fetchone()
        if not row:
            return ''

        next_sprint_id = row['sprint_id']
        next_sprint_name = row['sprint_name']
        start_date = row['start_date']
        end_date = row['end_date']

        # Format date range for display
        try:
            sd = parse_iso_tz(start_date).strftime('%b %d')
            ed = parse_iso_tz(end_date).strftime('%b %d')
            date_range = f'{sd} – {ed}'
        except Exception:
            date_range = ''

        # Tickets assigned to this dev in the next sprint (Story + Bug, non-excluded)
        excl_ph = sql_placeholders(EXCLUDED_STATUSES)
        cursor.execute(
            f"""
            SELECT ticket_key, summary, status, story_points, ticket_url, issue_type
              FROM tickets
             WHERE sprint_id = ?
               AND assignee_account_id = ?
               AND issue_type IN ('Story', 'Bug')
               AND status NOT IN ({excl_ph})
             ORDER BY issue_type, story_points DESC
            """,
            (next_sprint_id, dev_id, *EXCLUDED_STATUSES),
        )
        tickets = cursor.fetchall()

    finally:
        conn.close()

    ticket_count = len(tickets)
    total_sp = sum((t['story_points'] or 0) for t in tickets)
    sp_str = f'{total_sp:g} SP · ' if total_sp else ''

    ticket_rows = ''
    for t in tickets:
        sp_badge = f'<span class="next-sprint-sp">{t["story_points"]:g} SP</span>' if t['story_points'] else ''
        status_cls = 'status-in-progress' if t['status'] in IN_PROGRESS_STATUSES else 'status-open'
        ticket_rows += f"""
                        <div class="ticket-item">
                            <a href="{t['ticket_url']}" class="ticket-key" target="_blank">{t['ticket_key'].split('_')[0]}</a>
                            {html.escape(t['summary'] or '')}
                            {sp_badge}
                        </div>"""

    empty_note = '' if tickets else '<div class="next-sprint-empty">No tickets assigned yet</div>'

    return f"""
                    <details class="member-current-sprint">
                        <summary class="member-current-sprint-summary">
                            <span class="member-current-sprint-chevron" aria-hidden="true">▶</span>
                            <span class="member-current-sprint-title">Next Sprint · {next_sprint_name}</span>
                            <span class="member-current-sprint-meta">{sp_str}{ticket_count} tickets · {date_range}</span>
                        </summary>
                        <div class="member-current-sprint-body">
                            <div class="ticket-grid">
                                {ticket_rows}
                                {empty_note}
                            </div>
                        </div>
                    </details>
    """


def _sprint_length_days(start_iso: str, end_iso: str) -> int:
    """Length of a sprint in calendar days (>= 1)."""
    try:
        s = parse_iso_tz((start_iso or ''))
        e = parse_iso_tz((end_iso or ''))
        return max(int((e - s).total_seconds() / 86400), 1)
    except Exception:
        return 14  # standard FNTSY sprint length, used only when dates parse fails


def _github_activity_in_window(
    db_path: str, github_username: str, start_iso: str, end_iso: str,
) -> dict:
    """Compute GitHub PR + review metrics for a single user during a window.

    Mirrors `get_pr_metrics` / `get_review_metrics` shape but constrained to
    [start_iso, end_iso] instead of "last N days." Used for the per-past-sprint
    GitHub Activity panel.

    Returns a dict with: opened_count, merged_count, avg_hours_to_merge,
    approvals, changes_requested, review_comments, pr_comments.
    Returns zeros when github_username is empty.
    """
    out = {
        'opened_count': 0, 'merged_count': 0, 'avg_hours_to_merge': None,
        'approvals': 0, 'changes_requested': 0, 'review_comments': 0,
        'pr_comments': 0,
    }
    if not github_username or not start_iso or not end_iso:
        return out

    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        # PRs opened in window (by created_at)
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt FROM github_prs
             WHERE author_github_username = ?
               AND created_at >= ? AND created_at <= ?
            """,
            (github_username, start_iso, end_iso),
        )
        out['opened_count'] = (cursor.fetchone() or {'cnt': 0})['cnt'] or 0

        # PRs merged in window
        cursor.execute(
            """
            SELECT created_at, merged_at FROM github_prs
             WHERE author_github_username = ?
               AND state = 'merged'
               AND merged_at >= ? AND merged_at <= ?
               AND created_at IS NOT NULL AND merged_at IS NOT NULL
            """,
            (github_username, start_iso, end_iso),
        )
        merge_hours = []
        for row in cursor.fetchall():
            row = dict(row)
            try:
                created = parse_iso_tz(row['created_at'])
                merged = parse_iso_tz(row['merged_at'])
            except Exception:
                continue
            hours = (merged - created).total_seconds() / 3600.0
            if hours > 0:
                merge_hours.append(hours)
        out['merged_count'] = len(merge_hours)
        out['avg_hours_to_merge'] = (
            round(sum(merge_hours) / len(merge_hours), 1) if merge_hours else None
        )

        # Reviews given in window
        cursor.execute(
            """
            SELECT
                SUM(CASE WHEN state = 'APPROVED' THEN 1 ELSE 0 END) AS approvals,
                SUM(CASE WHEN state = 'CHANGES_REQUESTED' THEN 1 ELSE 0 END) AS changes_requested,
                COALESCE(SUM(inline_comment_count), 0) AS review_comments
              FROM github_reviews
             WHERE reviewer_github_username = ?
               AND submitted_at >= ? AND submitted_at <= ?
            """,
            (github_username, start_iso, end_iso),
        )
        row = dict(cursor.fetchone() or {})
        out['approvals'] = row.get('approvals') or 0
        out['changes_requested'] = row.get('changes_requested') or 0
        out['review_comments'] = row.get('review_comments') or 0

        # Issue-level comments in window
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt FROM github_pr_comments
             WHERE commenter_github_username = ?
               AND created_at >= ? AND created_at <= ?
            """,
            (github_username, start_iso, end_iso),
        )
        out['pr_comments'] = (dict(cursor.fetchone() or {'cnt': 0})).get('cnt') or 0
    finally:
        conn.close()
    return out


def _build_member_past_sprints_html(
    db_path: str,
    dev_id: str,
    sprint_prefix: str,
    github_username: str = None,
) -> str:
    """Render a per-engineer "Past Sprints" panel.

    Each closed sprint renders a collapsible block with the same three metric
    groups as the current-sprint card (Jira Stories, Jira Story Points, GitHub
    Activity), scoped to that sprint's window. Sprint-end status drives the
    Jira buckets so rolled-over tickets aren't credited or punished by what
    happened after the sprint closed. GitHub PR/review activity is windowed
    to the sprint's [start_date, end_date] range.
    """
    if not dev_id:
        return ''

    in_code_review_plus = frozenset((
        'In Review',
        'In code review',
        'Testing in progress',
        'Ready for Testing',
        'Released to Test',
        'Ready for Prod Deployment',
    )) | set(CLOSED_STATUSES)

    excl_ph = sql_placeholders(EXCLUDED_STATUSES)
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT sprint_id, jira_sprint_id, sprint_name, start_date, end_date
              FROM sprints
             WHERE sprint_name LIKE ? || '%' AND date(end_date) < date('now')
               AND COALESCE(is_placeholder, 0) = 0
             ORDER BY end_date DESC
             LIMIT 8
            """,
            (sprint_prefix,),
        )
        past_sprints = [dict(r) for r in cursor.fetchall()]
        if not past_sprints:
            return ''

        sprint_blocks = []
        for sprint_idx, s in enumerate(past_sprints):
            cursor.execute(
                f"""
                SELECT ticket_key, summary, status,
                       COALESCE(status_at_sprint_end, status) AS sprint_end_status,
                       story_points, ticket_url, issue_type
                  FROM tickets
                 WHERE sprint_id = ?
                   AND assignee_account_id = ?
                   AND issue_type IN ('Story', 'Bug')
                   AND status NOT IN ({excl_ph})
                 ORDER BY ticket_key
                """,
                (s['sprint_id'], dev_id, *EXCLUDED_STATUSES),
            )
            tickets = [dict(r) for r in cursor.fetchall()]
            # Strip the cross-sprint "_s<jira_sprint_id>" suffix on rolled-over
            # Story/Bug rows so the link text shows the bare Jira key.
            for t in tickets:
                t['ticket_key'] = t['ticket_key'].split('_s', 1)[0]
            if not tickets:
                continue

            # Jira buckets (sprint-end status drives these — same logic as
            # the Past Sprint Reports page).
            completed_tickets = [t for t in tickets if t['sprint_end_status'] in CLOSED_STATUSES]
            in_progress_tickets = [t for t in tickets if t['sprint_end_status'] in IN_PROGRESS_STATUSES]
            todo_tickets = [t for t in tickets if t['sprint_end_status'] in OPEN_STATUSES]

            completed_count = len(completed_tickets)
            in_progress_count = len(in_progress_tickets)
            todo_count = len(todo_tickets)
            total_count = len(tickets)

            completed_sp = sum((t['story_points'] or 0.0) for t in completed_tickets)
            in_progress_sp = sum((t['story_points'] or 0.0) for t in in_progress_tickets)
            todo_sp = sum((t['story_points'] or 0.0) for t in todo_tickets)
            review_plus_sp = sum(
                (t['story_points'] or 0.0)
                for t in tickets
                if t['sprint_end_status'] in in_code_review_plus
            )
            total_sp = sum((t['story_points'] or 0.0) for t in tickets)
            sp_completion_rate = (completed_sp / total_sp * 100) if total_sp > 0 else 0
            completion_rate = (completed_count / total_count * 100) if total_count > 0 else 0

            # Cycle-time / throughput already filter by sprint_id, so they
            # work for past sprints as-is. Window throughput to the sprint
            # length (capped at 28 to avoid a 0-day denominator on weird data).
            sprint_cycle_time = get_developer_cycle_time(db_path, s['sprint_id'], dev_id)
            sprint_days = _sprint_length_days(s['start_date'], s['end_date'])
            sprint_throughput = get_developer_throughput(
                db_path, s['sprint_id'], dev_id, days=max(sprint_days, 1),
            )
            sp_throughput_per_week = (
                (completed_sp / sprint_days) * 7 if sprint_days > 0 else 0
            )

            # GitHub activity scoped to the sprint window. Skip the section
            # entirely if we don't know the engineer's GitHub login (otherwise
            # the query would need to scan everyone).
            gh_metrics = (
                _github_activity_in_window(db_path, github_username, s['start_date'], s['end_date'])
                if github_username else None
            )

            rows = []
            for t in tickets:
                sp = _format_sp(t['story_points'] or 0.0)
                type_color = '#fbbf24' if t['issue_type'] == 'Bug' else '#8194a6'
                rows.append(f"""
                                <div style="background: #131c27; border-left: 3px solid #243340; border-radius: 6px; padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; gap: 12px;">
                                    <div style="flex: 1; min-width: 0;">
                                        <a href="{html.escape(t['ticket_url'] or '')}" target="_blank" style="color: #56cdf9; text-decoration: none; font-weight: 600; font-size: 13px;">{html.escape(t['ticket_key'])}</a>
                                        <span style="color: {type_color}; font-size: 10px; font-weight: 600; margin-left: 6px;">{html.escape(t['issue_type'] or '')}</span>
                                        <span style="color: #cdd9e5; margin-left: 8px;">{html.escape(t['summary'] or '')}</span>
                                    </div>
                                    <div style="display: flex; gap: 10px; align-items: center; flex-shrink: 0;">
                                        <span style="color: #cdd9e5; font-size: 12px; font-variant-numeric: tabular-nums;">{sp} SP</span>
                                        {_status_badge(t['sprint_end_status'])}
                                    </div>
                                </div>
                """)

            metrics_html = f"""
                        <div class="metric-group">
                            <div class="metric-group-title">🎫 Jira · Stories</div>
                            <div class="performance-metrics">
                                <div class="perf-metric">
                                    <div class="perf-metric-label">Total Stories</div>
                                    <div class="perf-metric-value compact">{total_count}</div>
                                </div>
                                <div class="perf-metric" title="Stories completed at sprint close">
                                    <div class="perf-metric-label">Completion Rate</div>
                                    <div class="perf-metric-value">{completion_rate:.0f}%</div>
                                    <div class="perf-metric-subtext">{completed_count}/{total_count} at sprint close</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">Completed</div>
                                    <div class="perf-metric-value success compact">{completed_count}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">In Progress</div>
                                    <div class="perf-metric-value warning compact">{in_progress_count}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">To Do</div>
                                    <div class="perf-metric-value info compact">{todo_count}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">Cycle Time</div>
                                    <div class="perf-metric-value compact">{f"{sprint_cycle_time:.1f}d" if sprint_cycle_time else "N/A"}</div>
                                </div>
                                <div class="perf-metric" title="Tickets completed per week during this sprint">
                                    <div class="perf-metric-label">Throughput</div>
                                    <div class="perf-metric-value compact">{sprint_throughput:.1f}/wk</div>
                                </div>
                            </div>
                        </div>

                        <div class="metric-group">
                            <div class="metric-group-title">📏 Jira · Story Points</div>
                            <div class="performance-metrics">
                                <div class="perf-metric">
                                    <div class="perf-metric-label">Total SP</div>
                                    <div class="perf-metric-value compact">{_format_sp(total_sp)}</div>
                                </div>
                                <div class="perf-metric" title="SP completed at sprint close">
                                    <div class="perf-metric-label">SP Completion</div>
                                    <div class="perf-metric-value">{sp_completion_rate:.0f}%</div>
                                    <div class="perf-metric-subtext">{_format_sp(completed_sp)}/{_format_sp(total_sp)} SP</div>
                                </div>
                                <div class="perf-metric" title="SP in / past code review at sprint close">
                                    <div class="perf-metric-label">In Code Review+ SP</div>
                                    <div class="perf-metric-value" style="color: #38bdf8;">{_format_sp(review_plus_sp)}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">Completed SP</div>
                                    <div class="perf-metric-value" style="color: #2dd4a7;">{_format_sp(completed_sp)}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">In Progress SP</div>
                                    <div class="perf-metric-value" style="color: #fbbf24;">{_format_sp(in_progress_sp)}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">To Do SP</div>
                                    <div class="perf-metric-value" style="color: #38bdf8;">{_format_sp(todo_sp)}</div>
                                </div>
                                <div class="perf-metric" title="Story points completed per week during this sprint">
                                    <div class="perf-metric-label">SP Throughput</div>
                                    <div class="perf-metric-value compact">{sp_throughput_per_week:.1f}/wk</div>
                                </div>
                            </div>
                        </div>
            """

            if gh_metrics:
                metrics_html += f"""
                        <div class="metric-group">
                            <div class="metric-group-title">🐙 GitHub · Activity (sprint window)</div>
                            <div class="performance-metrics">
                                <div class="perf-metric" title="PRs merged during the sprint window">
                                    <div class="perf-metric-label">PRs Merged</div>
                                    <div class="perf-metric-value">{gh_metrics['merged_count']}</div>
                                </div>
                                <div class="perf-metric" title="PRs opened during the sprint window">
                                    <div class="perf-metric-label">PRs Opened</div>
                                    <div class="perf-metric-value">{gh_metrics['opened_count']}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">Avg Merge Time</div>
                                    <div class="perf-metric-value compact">{f"{gh_metrics['avg_hours_to_merge']:.0f}h" if gh_metrics['avg_hours_to_merge'] is not None else "N/A"}</div>
                                </div>
                                <div class="perf-metric" title="PR reviews APPROVED during the sprint window">
                                    <div class="perf-metric-label">Approvals</div>
                                    <div class="perf-metric-value">{gh_metrics['approvals']}</div>
                                </div>
                                <div class="perf-metric" title="PR reviews with CHANGES_REQUESTED during the sprint window">
                                    <div class="perf-metric-label">Changes Req.</div>
                                    <div class="perf-metric-value">{gh_metrics['changes_requested']}</div>
                                </div>
                                <div class="perf-metric" title="Inline review comments during the sprint window">
                                    <div class="perf-metric-label">Review Comments</div>
                                    <div class="perf-metric-value">{gh_metrics['review_comments']}</div>
                                </div>
                                <div class="perf-metric" title="PR-level comments during the sprint window">
                                    <div class="perf-metric-label">PR Comments</div>
                                    <div class="perf-metric-value">{gh_metrics['pr_comments']}</div>
                                </div>
                            </div>
                        </div>
                """

            sprint_blocks.append(f"""
                    <details class="member-current-sprint">
                        <summary class="member-current-sprint-summary">
                            <span class="member-current-sprint-chevron" aria-hidden="true">▶</span>
                            <span class="member-current-sprint-title">{"Last Sprint · " if sprint_idx == 0 else ""}{html.escape(s['sprint_name'])}</span>
                            <span class="member-current-sprint-meta">{len(tickets)} tickets · {_format_sp(total_sp)} SP · {completed_count} completed</span>
                        </summary>
                        <div class="member-current-sprint-body">
                            {metrics_html}
                            <div class="metric-group">
                                <div class="metric-group-title">📋 Tickets</div>
                                <div style="display: grid; gap: 6px;">
                                    {''.join(rows)}
                                </div>
                            </div>
                        </div>
                    </details>
            """)
    finally:
        conn.close()

    if not sprint_blocks:
        return ''

    return ''.join(sprint_blocks)


def generate_team_members_html(config: dict, output_path: Path):
    """Generate one page per developer with team members in the top nav.

    Each developer gets their own `member_<First_Last>.html` page. The team
    member links appear in the top navigation instead of in a sub-nav.
    """
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    sprint = get_current_sprint(db_path, sprint_prefix)
    if not sprint:
        print("No active sprint found")
        return

    developers = get_all_developers_metrics(db_path, sprint['sprint_id'])
    burndown = get_sprint_burndown(db_path, sprint['sprint_id'])

    # Sprint progress — lets us compare actual completion against time elapsed
    sprint_start = parse_iso_tz(sprint['start_date']).date()
    sprint_end = parse_iso_tz(sprint['end_date']).date()
    today = datetime.now().date()
    sprint_total_days = max((sprint_end - sprint_start).days, 1)
    sprint_elapsed_days = max(0, min((today - sprint_start).days, sprint_total_days))
    sprint_days_remaining = max(0, (sprint_end - today).days)
    expected_completion_pct = (sprint_elapsed_days / sprint_total_days) * 100

    # Pre-compute status buckets for every dev (drives the quick-jump pills)
    dev_status_map = {}
    for dev in developers or []:
        dev_status_map[dev['developer_id']] = _compute_dev_status(dev, expected_completion_pct)

    output_dir = output_path.parent

    # Captured by _render_page's closure; populated below after we discover
    # any past-sprint-only engineers so the sub-nav lists every member with
    # any data (current or historical).
    nav_devs = list(developers)

    # Build name → role map from config for BE/FE pill grouping
    dev_role_map = {m['name']: m.get('role', 'BE') for m in config.get('team_members', [])}

    def _render_page(active_dev_name, card_html, page_title):
        header = f"""
        <style>
            details.member-current-sprint {{ background: #131c27; border: 1px solid #1a2430; border-radius: 8px; padding: 12px 16px; margin: 14px 0; }}
            details.member-current-sprint > summary {{ list-style: none; cursor: pointer; outline: none; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
            details.member-current-sprint > summary::-webkit-details-marker {{ display: none; }}
            details.member-current-sprint .member-current-sprint-chevron {{ display: inline-block; width: 10px; color: #8194a6; transition: transform 0.15s; }}
            details.member-current-sprint[open] .member-current-sprint-chevron {{ transform: rotate(90deg); }}
            details.member-current-sprint > summary:hover .member-current-sprint-chevron {{ color: #cdd9e5; }}
            details.member-current-sprint .member-current-sprint-title {{ font-size: 15px; font-weight: 600; color: #f4f8fb; }}
            details.member-current-sprint .member-current-sprint-meta {{ color: #8194a6; font-size: 12px; margin-left: auto; }}
            details.member-current-sprint .member-current-sprint-body {{ margin-top: 14px; }}
        </style>
        <header>
            <h1>👥 {active_dev_name or "Team Members"}</h1>
            <div class="subtitle">{fmt_sprint_long(sprint['sprint_name'])} • Day {sprint_elapsed_days} of {sprint_total_days} ({sprint_days_remaining} remaining) • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu()}
        <div class="content">
            <div class="intro-banner">
                <p>Performance analysis based on sprint metrics, ticket velocity, PR activity, and work patterns. Completion rate is compared against expected progress given {sprint_elapsed_days}/{sprint_total_days} days elapsed (~{expected_completion_pct:.0f}% of sprint).</p>
            </div>
{card_html}
        </div>
{_render_competency_modal()}
{_render_member_edit_modal()}
        """
        return render_html(title=page_title, content=header, body_class=_PAGE_THEME["team-members"])

    if not developers:
        print("No team members with sprint data found")
        return

    # Surface engineers who have past-sprint work but no current-sprint
    # tickets. Without this, someone like Kevin Paquette (zero rows in M30.2)
    # would lose their member page entirely the moment a sprint rolled over.
    current_dev_ids = {d['developer_id'] for d in developers}
    extra_devs = []
    try:
        _conn = get_connection(db_path)
        _cur = _conn.cursor()
        _cur.execute(
            """
            SELECT DISTINCT t.assignee_account_id AS developer_id,
                            t.assignee_display_name AS developer_name
              FROM tickets t
              JOIN sprints s ON s.sprint_id = t.sprint_id
             WHERE s.sprint_name LIKE ? || '%'
               AND date(s.end_date) < date('now')
               AND COALESCE(s.is_placeholder, 0) = 0
               AND t.assignee_account_id IS NOT NULL
               AND t.issue_type IN ('Story', 'Bug')
               AND t.status NOT IN ({excl_ph})
            """.format(excl_ph=sql_placeholders(EXCLUDED_STATUSES)),
            (sprint_prefix, *EXCLUDED_STATUSES),
        )
        for row in _cur.fetchall():
            if row['developer_id'] not in current_dev_ids:
                extra_devs.append({
                    'developer_id': row['developer_id'],
                    'developer_name': row['developer_name'],
                    # Empty current-sprint metrics; the past-sprints panel still renders
                    'tickets_completed': 0, 'tickets_in_progress': 0, 'tickets_todo': 0,
                    'completed_story_points': 0, 'remaining_story_points': 0,
                    'assigned_story_points': 0,
                })
        _conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Past-sprint-only member discovery failed (non-fatal): %s", e
        )

    # Default status for the past-sprint-only engineers — keeps the sub-nav
    # color-coding sane without inventing fake current-sprint metrics.
    for dev in extra_devs:
        dev_status_map[dev['developer_id']] = 'no-current-sprint'

    all_devs = list(developers) + extra_devs
    nav_devs[:] = all_devs  # share with the _render_page closure
    sorted_devs = sorted(all_devs, key=lambda x: x['developer_name'])

    # Pre-fetch every per-developer dataset in batched queries so we don't
    # round-trip 7×N times during the per-member render loop.
    id_to_github = {}
    id_to_level = {}
    for member in config.get('team_members', []):
        jid = member.get('jira_account_id')
        if not jid:
            continue
        if member.get('github_username'):
            id_to_github[jid] = member['github_username']
        if member.get('level'):
            id_to_level[jid] = member['level']
    github_usernames = sorted(set(id_to_github.values()))

    bulk = {
        'tickets':         get_developer_tickets_bulk(db_path, sprint['sprint_id']),
        'pr_metrics':      get_pr_metrics_bulk(db_path, github_usernames, days=30),
        'review_metrics':  get_review_metrics_bulk(db_path, github_usernames, days=90),
        'cycle_time':      get_developer_cycle_time_bulk(db_path, sprint['sprint_id']),
        'cycle_per_point': get_developer_cycle_per_point_bulk(db_path, sprint['sprint_id']),
        'throughput':      get_developer_throughput_bulk(db_path, sprint['sprint_id'], days=7),
        'meetings':        get_one_on_one_meetings_bulk(db_path),
        'id_to_github':    id_to_github,
        'id_to_level':     id_to_level,
    }

    # Write one page per developer
    for dev in sorted_devs:
        card_html, _status = _build_member_card_html(
            dev, config, db_path, sprint,
            sprint_elapsed_days, sprint_total_days,
            sprint_days_remaining, expected_completion_pct,
            bulk=bulk,
        )
        member_path = output_dir / _member_filename(dev['developer_name'])
        _atomic_write(member_path, _render_page(
            dev['developer_name'],
            card_html,
            f"{dev['developer_name']} - Team Members",
        ))
        print(f"✅ Member page generated: {member_path}")

    return



def _render_sprint_completion_bar_chart(db_path: str, sprint_prefix: str, config: dict, *, current_sprint_id: int | None) -> str:
    """Render a sprint-over-sprint story-point completion bar chart.

    Each bar is a sprint: solid green for SP completed (status_at_sprint_end
    in CLOSED_STATUSES), faded grey for the remainder of committed SP. Active
    sprints use live status (no snapshot yet), closed sprints use the sprint-end
    snapshot — same source-of-truth as the Sprint Reports page so a sprint's
    bar reads the same here as it does there. Counts every assignee (BE, FE,
    and unassigned/other) so the bar matches the per-sprint banners below and
    the Sprint Reports page.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT sprint_id, sprint_name, start_date, end_date, state
              FROM sprints
             WHERE sprint_name LIKE ? || '%'
               AND COALESCE(is_placeholder, 0) = 0
               AND (state = 'closed'
                    OR sprint_id = ?
                    OR (state = 'active' AND date(end_date) >= date('now')))
             ORDER BY end_date DESC
             LIMIT 12
            """,
            (sprint_prefix, current_sprint_id or -1),
        )
        sprint_rows = [dict(r) for r in cursor.fetchall()]
        if not sprint_rows:
            return ''
        sprint_rows.reverse()  # chronological L→R

        bars = []
        # Restrict to the buckets the per-sprint banners use so the SP totals
        # reconcile exactly between the bar and each sprint's <details> banner.
        # Counts ALL assignees (no BE/FE filter) so the bar also matches the
        # Sprint Reports page.
        bar_known = list(set(CLOSED_STATUSES) | set(IN_PROGRESS_STATUSES) | set(OPEN_STATUSES))
        bar_ph = sql_placeholders(bar_known)
        for s in sprint_rows:
            cursor.execute(
                f"""
                SELECT COALESCE(status_at_sprint_end, status) AS sprint_end_status,
                       status, story_points
                  FROM tickets
                 WHERE sprint_id = ?
                   AND issue_type IN ('Story', 'Bug')
                   AND COALESCE(status_at_sprint_end, status) IN ({bar_ph})
                """,
                (s['sprint_id'], *bar_known),
            )
            rows = cursor.fetchall()
            committed = 0.0
            completed = 0.0
            for r in rows:
                sp = r['story_points'] or 0.0
                committed += sp
                # Active sprint hasn't been snapshotted, so sprint_end_status
                # falls back to live status via the COALESCE above — that's
                # exactly what we want for the in-flight bar.
                if r['sprint_end_status'] in CLOSED_STATUSES:
                    completed += sp
            bars.append({
                'label': fmt_sprint_short(s['sprint_name']),
                'committed': committed,
                'completed': completed,
                'is_active': s['sprint_id'] == current_sprint_id,
            })
    finally:
        conn.close()

    if not bars or all(b['committed'] == 0 for b in bars):
        return ''

    svg_w, svg_h = 900, 280
    pad_l, pad_r, pad_t, pad_b = 52, 20, 32, 50
    inner_w = svg_w - pad_l - pad_r
    inner_h = svg_h - pad_t - pad_b
    n = len(bars)
    # Reserve 22% of the slot for inter-bar gutters; clamp to a sensible width.
    slot_w = inner_w / n
    bar_w = min(slot_w * 0.78, 64)

    max_y = max((max(b['committed'], b['completed']) for b in bars), default=1) or 1
    # Round up to a tidy multiple so tick labels don't read like 47.3 SP.
    # The pad_t reservation above carries the headroom for the value label
    # that sits over the tallest bar.
    def _nice(v):
        import math
        if v <= 0:
            return 1
        magnitude = 10 ** math.floor(math.log10(v))
        for m in (1, 2, 2.5, 5, 10):
            cand = m * magnitude
            if cand >= v:
                return cand
        return 10 * magnitude
    axis_max = _nice(max_y)

    def y_at(v):
        return pad_t + (1 - v / axis_max) * inner_h

    def x_center(i):
        return pad_l + slot_w * (i + 0.5)

    grid_svg, y_label_svg = '', ''
    for i in range(5):
        v = axis_max * (4 - i) / 4
        y = y_at(v)
        grid_svg += f'<line class="chart-grid-line" x1="{pad_l}" y1="{y:.1f}" x2="{svg_w - pad_r}" y2="{y:.1f}" />'
        y_label_svg += f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#8194a6" font-size="11">{v:.0f} SP</text>'

    bar_svg_parts = []
    for i, b in enumerate(bars):
        cx = x_center(i)
        x_left = cx - bar_w / 2
        committed_top = y_at(b['committed']) if b['committed'] > 0 else y_at(0)
        completed_top = y_at(b['completed']) if b['completed'] > 0 else y_at(0)
        baseline = y_at(0)
        # Background bar = committed (faded). Foreground bar = completed (solid).
        committed_h = baseline - committed_top
        completed_h = baseline - completed_top
        active_stroke = ' stroke="#7dd3fc" stroke-width="1.5"' if b['is_active'] else ''
        if committed_h > 0.5:
            bar_svg_parts.append(
                f'<rect x="{x_left:.1f}" y="{committed_top:.1f}" width="{bar_w:.1f}" '
                f'height="{committed_h:.1f}" fill="#243340" opacity="0.45" rx="3"{active_stroke} />'
            )
        if completed_h > 0.5:
            bar_svg_parts.append(
                f'<rect x="{x_left:.1f}" y="{completed_top:.1f}" width="{bar_w:.1f}" '
                f'height="{completed_h:.1f}" fill="#2dd4a7" rx="3" />'
            )
        # Single label line above the taller of the two bars: "completed/committed (pct%)".
        top_y = min(committed_top, completed_top)
        pct = (b['completed'] / b['committed'] * 100) if b['committed'] > 0 else 0
        bar_svg_parts.append(
            f'<text x="{cx:.1f}" y="{top_y - 6:.1f}" text-anchor="middle" '
            f'fill="#cdd9e5" font-size="11" font-weight="600">'
            f'{_fmt_sp(b["completed"])}/{_fmt_sp(b["committed"])} '
            f'<tspan fill="#8194a6" font-weight="500">({pct:.0f}%)</tspan>'
            f'</text>'
        )
        # X-axis label (sprint short name); add an "(active)" suffix in muted text.
        suffix = ' (active)' if b['is_active'] else ''
        label = (b['label'] + suffix).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        bar_svg_parts.append(
            f'<text x="{cx:.1f}" y="{svg_h - pad_b + 16:.1f}" text-anchor="middle" '
            f'fill="#cdd9e5" font-size="11">{label}</text>'
        )

    bar_svg = ''.join(bar_svg_parts)

    # 3-sprint trailing average of completed SP — a velocity trend line that
    # smooths single-sprint noise into a capacity signal. We average over
    # SETTLED sprints only: not the active sprint, and not its concurrent
    # partner / any other sprint still showing 0 completed (e.g. the BE half
    # of the current milestone, which is state='closed' in Jira but hasn't
    # actually delivered yet). Including those would drag the line toward 0
    # and misrepresent sustained capacity.
    settled_idx = [
        i for i, b in enumerate(bars)
        if not b['is_active'] and b['completed'] > 0
    ]
    trend_svg = ''
    avg_now = None
    if len(settled_idx) >= 3:
        pts = []
        for pos, i in enumerate(settled_idx):
            window = [bars[j]['completed'] for j in settled_idx[max(0, pos - 2):pos + 1]]
            avg = sum(window) / len(window)
            avg_now = avg
            pts.append(f"{x_center(i):.1f},{y_at(avg):.1f}")
        dots = ''.join(
            f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="3" '
            f'fill="#7dd3fc" stroke="#0f1620" stroke-width="1.5" />'
            for p in pts
        )
        trend_svg = (
            f'<polyline fill="none" stroke="#7dd3fc" stroke-width="2" '
            f'stroke-dasharray="5,3" points="{" ".join(pts)}" />{dots}'
        )

    trend_legend = (
        '<div><span class="swatch swatch-dashed" style="border-color:#7dd3fc;"></span>3-sprint avg (completed)</div>'
        if trend_svg else ''
    )
    legend_html = (
        '<div class="burndown-legend">'
        '<div><span class="swatch swatch-solid" style="background:#2dd4a7;"></span>Completed SP</div>'
        '<div><span class="swatch swatch-solid" style="background:#243340; opacity:0.45;"></span>Committed SP</div>'
        '<div><span class="swatch swatch-solid" style="background:transparent; border:1.5px solid #7dd3fc;"></span>Active sprint</div>'
        f'{trend_legend}'
        '</div>'
    )
    avg_note = (
        f'<div class="chart-subtitle">Sustained velocity (3-sprint avg): '
        f'<strong>{_fmt_sp(avg_now)} SP/sprint</strong></div>'
        if avg_now is not None else ''
    )

    return f"""
        <div class="section" id="sp-completion-bars">
            <div class="chart-container">
                <div class="chart-title">📊 Story Points Completed — Sprint over Sprint</div>
                {avg_note}
                <div class="burndown-svg-wrap">
                    <svg viewBox="0 0 {svg_w} {svg_h}" preserveAspectRatio="xMidYMid meet" style="width: 100%; height: 320px; display: block;">
                        {grid_svg}
                        {y_label_svg}
                        <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{svg_h - pad_b}" stroke="#243340" stroke-width="1" />
                        <line x1="{pad_l}" y1="{svg_h - pad_b}" x2="{svg_w - pad_r}" y2="{svg_h - pad_b}" stroke="#243340" stroke-width="1" />
                        {bar_svg}
                        {trend_svg}
                    </svg>
                </div>
                {legend_html}
            </div>
        </div>
    """


def _render_sp_sprint_block(sprint: dict, db_path: str, config: dict, *, is_active: bool) -> str:
    """Render one collapsible story-points block for a single sprint.

    Includes the BE/FE burndown chart (when daily snapshots exist) and the
    BE/FE collapsed sub-sections with completed/in-progress/open ticket
    accordions. The active sprint opens by default; closed sprints stay
    collapsed so the page doesn't paint as one long scroll wall.
    """
    sprint_id = sprint['sprint_id']
    burndown = get_sprint_burndown(db_path, sprint_id)

    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        # ID suffix scopes accordion-panel ids per sprint so toggling one
        # sprint's "Completed" panel doesn't fight with another's.
        id_suffix = f"-{sprint_id}"
        burndown_html = ""

        # Story-points burndown chart — working-days only, with BE/FE series.
        # Only renders when developer_snapshots have daily rows for the sprint.
        if burndown and len(burndown) > 0:
            from collections import defaultdict

            start_date = parse_iso_tz(sprint['start_date']).date()
            end_date = parse_iso_tz(sprint['end_date']).date()
            today = datetime.now().date()

            axes = _build_burndown_axes(start_date, end_date, today)
            wd_total = axes['wd_total']
            wd_elapsed = axes['wd_elapsed']
            days_remaining = axes['days_remaining']
            wd_index_for = axes['wd_index_for']
            working_days = axes['working_days']

            def _remaining(day):
                return day.get('remaining_story_points', day.get('total_story_points', 0)) or 0

            burndown_in_sprint = [
                d for d in burndown
                if datetime.fromisoformat(d['snapshot_date']).date() >= start_date
            ]
            if not burndown_in_sprint:
                burndown_in_sprint = burndown

            burndown_by_date_sp = {
                datetime.fromisoformat(d['snapshot_date']).date(): d
                for d in burndown_in_sprint
            }

            cursor.execute(
                "SELECT COALESCE(SUM(story_points), 0) FROM tickets "
                "WHERE sprint_id = ? AND issue_type = 'Story'",
                (sprint_id,),
            )
            sprint_story_total = cursor.fetchone()[0] or 0
            start_remaining_sp = _remaining(burndown_in_sprint[0]) if _remaining(burndown_in_sprint[0]) > 0 else sprint_story_total
            current_remaining_sp = _remaining(burndown_in_sprint[-1])
            ideal_remaining_today = start_remaining_sp - (start_remaining_sp / wd_total) * wd_elapsed if wd_total > 0 else 0
            ahead_behind = ideal_remaining_today - current_remaining_sp

            _, id_to_role = _build_role_maps(config)
            conn_dev = get_connection(db_path)
            c_dev = conn_dev.cursor()
            c_dev.execute(
                "SELECT developer_id, snapshot_date, assigned_story_points, remaining_story_points "
                "FROM developer_snapshots WHERE sprint_id = ? AND snapshot_date >= ? ORDER BY snapshot_date",
                (sprint_id, start_date.isoformat()),
            )
            role_sp_by_date: dict[str, dict[str, float]] = defaultdict(lambda: {'BE': 0.0, 'FE': 0.0})
            role_assigned_by_date: dict[str, dict[str, float]] = defaultdict(lambda: {'BE': 0.0, 'FE': 0.0})
            for row in c_dev.fetchall():
                role = id_to_role.get(row['developer_id'])
                if role in ('BE', 'FE'):
                    role_sp_by_date[row['snapshot_date']][role] += row['remaining_story_points'] or 0
                    role_assigned_by_date[row['snapshot_date']][role] += row['assigned_story_points'] or 0
            conn_dev.close()

            be_by_date = {datetime.fromisoformat(ds).date(): v['BE'] for ds, v in role_sp_by_date.items()}
            fe_by_date = {datetime.fromisoformat(ds).date(): v['FE'] for ds, v in role_sp_by_date.items()}
            be_assigned_by_date = {datetime.fromisoformat(ds).date(): v['BE'] for ds, v in role_assigned_by_date.items()}
            fe_assigned_by_date = {datetime.fromisoformat(ds).date(): v['FE'] for ds, v in role_assigned_by_date.items()}

            start_be = be_by_date.get(min(be_by_date, default=start_date), 0) if be_by_date else 0
            start_fe = fe_by_date.get(min(fe_by_date, default=start_date), 0) if fe_by_date else 0
            current_be = be_by_date.get(max(be_by_date, default=start_date), 0) if be_by_date else 0
            current_fe = fe_by_date.get(max(fe_by_date, default=start_date), 0) if fe_by_date else 0
            total_be = be_assigned_by_date.get(max(be_assigned_by_date, default=start_date), 0) if be_assigned_by_date else 0
            total_fe = fe_assigned_by_date.get(max(fe_assigned_by_date, default=start_date), 0) if fe_assigned_by_date else 0
            start_total_be = be_assigned_by_date.get(min(be_assigned_by_date, default=start_date), 0) if be_assigned_by_date else 0
            start_total_fe = fe_assigned_by_date.get(min(fe_assigned_by_date, default=start_date), 0) if fe_assigned_by_date else 0

            svg_w, svg_h = 900, 280
            pad_l, pad_r, pad_t, pad_b = 52, 20, 18, 34
            inner_w = svg_w - pad_l - pad_r
            inner_h = svg_h - pad_t - pad_b
            max_sp_axis = max(
                start_remaining_sp,
                max((_remaining(d) for d in burndown_in_sprint), default=start_remaining_sp),
                start_be, start_fe,
            )
            if max_sp_axis <= 0:
                max_sp_axis = 1

            def x_at(off):
                return pad_l + (off / wd_total) * inner_w if wd_total > 0 else pad_l

            def y_at(v):
                return pad_t + (1 - v / max_sp_axis) * inner_h

            axes['y_ticks'] = [
                (y_at(max_sp_axis * (4 - i) / 4), f"{max_sp_axis * (4 - i) / 4:.0f} SP")
                for i in range(5)
            ]
            if wd_total <= 10:
                x_ticks_idx = list(range(wd_total + 1))
            else:
                step = max(1, wd_total // 7)
                x_ticks_idx = list(range(0, wd_total + 1, step))
                if wd_total not in x_ticks_idx:
                    x_ticks_idx.append(wd_total)
            axes['x_ticks'] = [
                (x_at(idx), f"{working_days[min(idx, len(working_days)-1)].month}/{working_days[min(idx, len(working_days)-1)].day}")
                for idx in x_ticks_idx
            ]

            def _series(date_map, value_fn=lambda v: v):
                seen, pts, dots = set(), [], []
                for d in sorted(date_map):
                    xidx = wd_index_for(d)
                    if xidx in seen:
                        continue
                    seen.add(xidx)
                    yv = value_fn(date_map[d])
                    pts.append(f"{x_at(xidx):.1f},{y_at(yv):.1f}")
                    dots.append((x_at(xidx), y_at(yv)))
                return pts, dots

            combined_points, _ = _series(burndown_by_date_sp, value_fn=_remaining)
            be_points, be_dots = _series(be_by_date)
            fe_points, fe_dots = _series(fe_by_date)

            ideal_points = [
                f"{x_at(0):.1f},{y_at(start_remaining_sp):.1f}",
                f"{x_at(wd_total):.1f},{y_at(0):.1f}",
            ]

            extra_series = []
            if start_be > 0:
                extra_series.append({
                    'name': 'BE Ideal',
                    'points': [f"{x_at(0):.1f},{y_at(start_be):.1f}", f"{x_at(wd_total):.1f},{y_at(0):.1f}"],
                    'color': '#2dd4a7', 'stroke_width': 1.5, 'dasharray': '4,4', 'opacity': 0.4,
                })
            if start_fe > 0:
                extra_series.append({
                    'name': 'FE Ideal',
                    'points': [f"{x_at(0):.1f},{y_at(start_fe):.1f}", f"{x_at(wd_total):.1f},{y_at(0):.1f}"],
                    'color': '#c4b5fd', 'stroke_width': 1.5, 'dasharray': '4,4', 'opacity': 0.4,
                })
            if combined_points:
                extra_series.append({
                    'name': 'Combined Actual',
                    'points': combined_points,
                    'color': '#243340', 'stroke_width': 1.5, 'dasharray': '2,3', 'opacity': 0.5,
                })

            be_series = {'name': 'BE Actual', 'points': be_points, 'dots': be_dots, 'color': '#2dd4a7'}
            fe_series = {'name': 'FE Actual', 'points': fe_points, 'dots': fe_dots, 'color': '#c4b5fd'}

            if ahead_behind > 0.5:
                pace_label = f"<span style='color: #6ee7c3;'>↑ {ahead_behind:.1f} SP ahead</span>"
            elif ahead_behind < -0.5:
                pace_label = f"<span style='color: #fda4a0;'>↓ {abs(ahead_behind):.1f} SP behind</span>"
            else:
                pace_label = "<span style='color: #cdd9e5;'>on pace</span>"

            scope_added = (total_be + total_fe) - (start_total_be + start_total_fe)
            if scope_added > 0.5:
                pace_vs_original = ahead_behind + scope_added
                if pace_vs_original < -0.5:
                    pace_sub = f'+{scope_added:.0f} scope · {abs(pace_vs_original):.0f} SP behind original plan'
                elif pace_vs_original > 0.5:
                    pace_sub = f'+{scope_added:.0f} scope · {pace_vs_original:.0f} SP ahead of original plan'
                else:
                    pace_sub = f'+{scope_added:.0f} scope · on pace vs original plan'
            else:
                pace_sub = f'ideal: {ideal_remaining_today:.0f} SP remaining'

            burndown_html = _render_burndown_chart(
                title='📈 Story Points Burndown — BE &amp; FE',
                section_id=f'sp-burndown-chart{id_suffix}',
                axes=axes,
                series=extra_series + [be_series, fe_series],
                summary_cards=[
                    {
                        'label': '⚙️ BE Remaining',
                        'value': f'{current_be:.0f} SP',
                        'sub': (
                            f'of {total_be:.0f} SP committed'
                            + (f' (+{total_be - start_total_be:.0f} added)' if total_be - start_total_be > 0.5 else '')
                        ),
                        'color': '#2dd4a7',
                    },
                    {
                        'label': '🎨 FE Remaining',
                        'value': f'{current_fe:.0f} SP',
                        'sub': (
                            f'of {total_fe:.0f} SP committed'
                            + (f' (+{total_fe - start_total_fe:.0f} added)' if total_fe - start_total_fe > 0.5 else '')
                        ),
                        'color': '#c4b5fd',
                    },
                    {'label': 'Overall Pace', 'value': pace_label, 'sub': pace_sub},
                    {'label': 'Time Left', 'value': f'{days_remaining}d', 'sub': f'working day {wd_elapsed + 1} of {wd_total + 1}'},
                ],
                legend=[
                    {'kind': 'solid', 'color': '#2dd4a7', 'label': 'BE Actual'},
                    {'kind': 'dashed', 'color': '#2dd4a7', 'label': 'BE Ideal'},
                    {'kind': 'solid', 'color': '#c4b5fd', 'label': 'FE Actual'},
                    {'kind': 'dashed', 'color': '#c4b5fd', 'label': 'FE Ideal'},
                    {'kind': 'dashed', 'color': '#243340', 'label': 'Combined'},
                    {'kind': 'dashed', 'color': '#56cdf9', 'label': 'Today'},
                ],
                ideal_points=ideal_points,
                today_in_sprint=(start_date <= today <= end_date),
            )

        # Collect tickets by status with story points (Stories+Bugs).
        # Closed sprints use status_at_sprint_end so ticket placement reflects
        # how the sprint actually ended; the active sprint falls back to live
        # status via COALESCE so the in-flight numbers read "now."
        closed_set = set(CLOSED_STATUSES)
        inprog_set = set(IN_PROGRESS_STATUSES)
        open_set = set(OPEN_STATUSES)
        all_statuses = list(closed_set | inprog_set | open_set)
        placeholders = ",".join("?" for _ in all_statuses)
        cursor.execute(
            f"""
            SELECT ticket_key, summary, ticket_url, assignee_display_name, status,
                   COALESCE(status_at_sprint_end, status) AS sprint_end_status,
                   story_points, issue_type
            FROM tickets
            WHERE sprint_id = ?
              AND issue_type IN ('Story', 'Bug')
              AND COALESCE(status_at_sprint_end, status) IN ({placeholders})
            ORDER BY story_points DESC, ticket_key
            """,
            [sprint_id] + all_statuses,
        )
        sp_rows = [
            {
                'ticket_key': r[0],
                'summary': r[1],
                'ticket_url': r[2],
                'assignee': r[3],
                'status': r[4],
                'sprint_end_status': r[5],
                'story_points': r[6] or 0,
                'issue_type': r[7],
            }
            for r in cursor.fetchall()
        ]

        closed_tickets = [t for t in sp_rows if t['sprint_end_status'] in closed_set]
        in_progress_tickets = [t for t in sp_rows if t['sprint_end_status'] in inprog_set]
        open_tickets = [t for t in sp_rows if t['sprint_end_status'] in open_set]

        name_to_role, _ = _build_role_maps(config)
        closed_by_role = _partition_tickets_by_role(closed_tickets, name_to_role, assignee_key='assignee')
        inprog_by_role = _partition_tickets_by_role(in_progress_tickets, name_to_role, assignee_key='assignee')
        open_by_role = _partition_tickets_by_role(open_tickets, name_to_role, assignee_key='assignee')
        sp_role_m = _role_sp_metrics(closed_by_role, inprog_by_role, open_by_role)

        # Sum across ALL reported roles (BE+FE+other) so the banner total
        # equals the sum of the role blocks below and reconciles with the
        # Sprint Reports page (which counts every assignee).
        banner_completed = sum(sp_role_m[r]['completed'] for r in _REPORTED_ROLES)
        banner_in_progress = sum(sp_role_m[r]['in_progress'] for r in _REPORTED_ROLES)
        banner_open = sum(sp_role_m[r]['open'] for r in _REPORTED_ROLES)
        banner_total = banner_completed + banner_in_progress + banner_open
        banner_remaining = banner_in_progress + banner_open
        banner_completion = (banner_completed / banner_total * 100) if banner_total > 0 else 0

        banner_html = (
            f'<span class="epic-sprint-stat" style="color: #2dd4a7;">{_fmt_sp(banner_completed)} <small>done</small></span>'
            f'<span class="epic-sprint-stat" style="color: #fbbf24;">{_fmt_sp(banner_remaining)} <small>remaining</small></span>'
            f'<span class="epic-sprint-stat" style="color: #38bdf8;">{_fmt_sp(banner_total)} <small>total SP</small></span>'
            f'<span class="epic-sprint-stat" style="color: #7dd3fc;">{banner_completion:.0f}% <small>complete</small></span>'
            f'{_scope_change_chip(db_path, sprint_id)}'
        )

        # Empty closed sprints get the same dimmed treatment as elsewhere on
        # the dashboard so a glance tells you "nothing here."
        is_empty = banner_total == 0
        empty_class = ' epic-sprint-empty' if is_empty else ''
        open_attr = ' open' if is_active else ''

        out = f"""
            <details class="epic-sprint-block{empty_class}"{open_attr}>
                <summary class="epic-sprint-summary">
                    <span class="epic-sprint-caret">▸</span>
                    <span class="epic-sprint-name">{fmt_sprint_long(sprint['sprint_name'])}</span>
                    <span class="epic-sprint-counts">{banner_html}</span>
                </summary>
                <div class="epic-sprint-body">
                    {burndown_html}
        """

        if is_empty:
            out += (
                '<div style="color:var(--text-muted); font-size:13px; '
                'font-style:italic; padding:8px 4px;">No tickets recorded for this sprint.</div>'
            )

        sp_role_config = [
            ('BE', 'sp-be', '⚙️ Backend'),
            ('FE', 'sp-fe', '🎨 Frontend'),
            ('other', 'sp-other', '👥 Other / Unassigned'),
        ]
        role_color = {'BE': '#2dd4a7', 'FE': '#c4b5fd', 'other': '#8194a6'}
        for role, rid_base, role_label in sp_role_config:
            rm = sp_role_m[role]
            # Only surface the catch-all block when it actually holds work —
            # an empty "Other" row is noise on every well-rostered sprint.
            if role == 'other' and rm['total'] == 0:
                continue
            rid = f"{rid_base}{id_suffix}"
            r_closed = closed_by_role[role]
            r_inprog = inprog_by_role[role]
            r_open = open_by_role[role]
            role_key = role.lower()
            accent = role_color.get(role, '#38bdf8')

            out += f"""
                <details class="epic-role-block role-{role_key}">
                    <summary class="epic-role-summary">
                        <span class="epic-role-caret">▸</span>
                        <span class="epic-role-name">{role_label}</span>
                        <span class="epic-role-counts">
                            <span class="epic-role-stat" style="color: #2dd4a7;">{_fmt_sp(rm['completed'])} <small>done</small></span>
                            <span class="epic-role-stat" style="color: #fbbf24;">{_fmt_sp(rm['in_progress'])} <small>in progress</small></span>
                            <span class="epic-role-stat" style="color: #38bdf8;">{_fmt_sp(rm['open'])} <small>open</small></span>
                            <span class="epic-role-stat" style="color: {accent};">{_fmt_sp(rm['total'])} <small>total SP</small></span>
                            <span class="epic-role-stat" style="color: #7dd3fc;">{rm['completion']:.0f}% <small>complete</small></span>
                        </span>
                    </summary>
                    <div class="epic-role-body">
                        <div class="metrics-grid">
                            <button type="button" class="metric-card success" onclick="toggleAccordion('{rid}-closed-panel')" aria-controls="{rid}-closed-panel" aria-expanded="false">
                                <div class="metric-label">Completed</div>
                                <div class="metric-value clickable">{rm['completed']:.1f}</div>
                                <div class="metric-subtext">{rm['completion']:.1f}% complete · Click to view</div>
                            </button>
                            <button type="button" class="metric-card warning" onclick="toggleAccordion('{rid}-inprogress-panel')" aria-controls="{rid}-inprogress-panel" aria-expanded="false">
                                <div class="metric-label">In Progress</div>
                                <div class="metric-value clickable">{rm['in_progress']:.1f}</div>
                                <div class="metric-subtext">Click to view</div>
                            </button>
                            <button type="button" class="metric-card info" onclick="toggleAccordion('{rid}-open-panel')" aria-controls="{rid}-open-panel" aria-expanded="false">
                                <div class="metric-label">Not Started</div>
                                <div class="metric-value clickable">{rm['open']:.1f}</div>
                                <div class="metric-subtext">Click to view</div>
                            </button>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: {rm['completion']}%"></div>
                        </div>

                        <div id="{rid}-closed-panel" class="accordion-panel">
                            <div class="accordion-content">
                                <div class="accordion-header">✅ Completed Tickets ({len(r_closed)} tickets, {rm['completed']:.1f} SP)</div>
                                <div class="ticket-grid">
            """

            for ticket in r_closed:
                sp_badge = f"<span style='color: #2dd4a7; font-weight: 600; margin-left: 8px;'>{ticket['story_points']:.1f} SP</span>" if ticket['story_points'] > 0 else ""
                out += f"""
                                    <div class="ticket-item">
                                        <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                        {ticket['summary']}
                                        {sp_badge}
                                        <span style="color: #566375; font-size: 12px;"> • {ticket['assignee'] or 'Unassigned'}</span>
                                    </div>
                """

            out += f"""
                                </div>
                            </div>
                        </div>

                        <div id="{rid}-inprogress-panel" class="accordion-panel">
                            <div class="accordion-content">
                                <div class="accordion-header">🔄 In Progress Tickets ({len(r_inprog)} tickets, {rm['in_progress']:.1f} SP)</div>
                                <div class="ticket-grid">
            """

            for ticket in r_inprog:
                sp_badge = f"<span style='color: #fbbf24; font-weight: 600; margin-left: 8px;'>{ticket['story_points']:.1f} SP</span>" if ticket['story_points'] > 0 else ""
                out += f"""
                                    <div class="ticket-item">
                                        <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                        {ticket['summary']}
                                        {sp_badge}
                                        <span style="color: #566375; font-size: 12px;"> • {ticket['assignee'] or 'Unassigned'} • {ticket['status']}</span>
                                    </div>
                """

            out += f"""
                                </div>
                            </div>
                        </div>

                        <div id="{rid}-open-panel" class="accordion-panel">
                            <div class="accordion-content">
                                <div class="accordion-header">📋 Open / To Do Tickets ({len(r_open)} tickets, {rm['open']:.1f} SP)</div>
                                <div class="ticket-grid">
            """

            for ticket in r_open:
                sp_badge = f"<span style='color: #38bdf8; font-weight: 600; margin-left: 8px;'>{ticket['story_points']:.1f} SP</span>" if ticket['story_points'] > 0 else ""
                out += f"""
                                    <div class="ticket-item">
                                        <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                        {ticket['summary']}
                                        {sp_badge}
                                        <span style="color: #566375; font-size: 12px;"> • {ticket['assignee'] or 'Unassigned'} • {ticket['status']}</span>
                                    </div>
                """

            out += """
                                </div>
                            </div>
                        </div>
                    </div>
                </details>
            """

        out += """
                </div>
            </details>
        """
        return out
    finally:
        conn.close()


def generate_story_points_html(config: dict, output_path: Path):
    """Generate HTML story points dashboard."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    active_sprint = get_current_sprint(db_path, sprint_prefix)

    # Pull every FNTSY sprint (active + closed) so each gets its own
    # collapsible block on the page. Newest-first: the active sprint sits
    # at the top of the list and the rest read backwards through history.
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT sprint_id, jira_sprint_id, sprint_name, state, start_date, end_date, goal
              FROM sprints
             WHERE sprint_name LIKE ? || '%'
               AND COALESCE(is_placeholder, 0) = 0
               AND state IN ('active', 'closed')
             ORDER BY start_date DESC
            """,
            (sprint_prefix,),
        )
        all_sprints = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    if not all_sprints:
        print("No sprints found")
        return

    # Header subtitle stays anchored to the active sprint; the bar chart
    # above the list still scopes its "active" outline by current_sprint_id.
    header_sprint = active_sprint or all_sprints[0]
    active_sprint_id = active_sprint['sprint_id'] if active_sprint else None

    nav_menu = generate_nav_menu('story-points')
    completion_bar_chart_html = _render_sprint_completion_bar_chart(
        db_path, sprint_prefix, config, current_sprint_id=active_sprint_id,
    )

    sprint_blocks_html = ''.join(
        _render_sp_sprint_block(
            s, db_path, config,
            is_active=(s['sprint_id'] == active_sprint_id),
        )
        for s in all_sprints
    )

    content = f"""
        <header>
            <h1>📊 Story Points Dashboard</h1>
            <div class="subtitle">{fmt_sprint_long(header_sprint['sprint_name'])} • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
        {nav_menu}
        <div class="content">
            <div class="intro-banner">
                <p>Sprint progress measured by story points instead of ticket count. Story points provide a more accurate measure of work complexity and effort. The active sprint expands by default; closed sprints stay collapsed.</p>
            </div>

            {completion_bar_chart_html}

            {sprint_blocks_html}

            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    html = render_html(
        title=f"Story Points - {fmt_sprint_long(header_sprint['sprint_name'])}",
        content=content,
        body_class=_PAGE_THEME["story-points"],
    )

    _atomic_write(output_path, html)
    print(f"✅ Story points dashboard generated: {output_path}")



def _build_full_sprint_sequence(sprints, target_milestone=None, target_sprint_in_milestone=None):
    """Order the real sprint rows for chronological rendering.

    The Jira collector now ingests every active/future sprint directly from
    the board's Agile API, so the dashboard no longer synthesises placeholder
    rows for slots that were "missing" — every slot it should care about is
    a real DB row. This function just sorts those rows (start_date asc, FE
    before BE within a shared start_date) and tags each with the fields the
    rest of the page expects.

    Returns a list of dicts:
        sprint_id    — real DB id
        sprint_name  — display name
        short_label  — e.g. "M30.3 FE" / "M30.3 BE" / "M30.2"
        start_date   — ISO string (UTC)
        end_date     — ISO string (UTC)
        placeholder  — always False (kept for back-compat with downstream readers)
        role         — 'FE' | 'BE' | None  (None for pre-split sprints)
    """
    import re as _re

    def parse_role(sprint_name):
        m = _re.search(r'\b(FE|BE)\s*$', sprint_name)
        return m.group(1) if m else None

    if not sprints:
        return []

    ordered = sorted(
        sprints,
        key=lambda s: (
            parse_iso_tz(s['start_date']).date(),
            {'FE': 0, 'BE': 1}.get(parse_role(s['sprint_name']), 2),
        ),
    )

    out = []
    for s in ordered:
        short = s['sprint_name'].replace('FNTSY ', '').replace(' Sprint', '')
        out.append({
            'sprint_id': s['sprint_id'],
            'sprint_name': s['sprint_name'],
            'short_label': short,
            'start_date': s['start_date'],
            'end_date': s['end_date'],
            'placeholder': False,
            'role': parse_role(s['sprint_name']),
        })
    return out


def persist_placeholder_sprints(db_path: str, sequence) -> int:
    """Sweep any leftover placeholder rows from older runs.

    Placeholder synthesis is gone — every active/future sprint comes from
    the Jira board's Agile API now. This function is kept only to clean up
    any synthetic rows (`is_placeholder = 1`) that the prior code wrote into
    the sprints table. It runs once per regen and turns into a no-op as
    soon as the table is clean.
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM sprints WHERE is_placeholder = 1")
        deleted = cur.rowcount or 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def _classify_epic_prefix(summary: str) -> str:
    """Return 'be' | 'fe' | 'none' based on the [BE]/[FE] prefix convention.

    Matches are case-insensitive and tolerate optional whitespace, colons,
    or dashes immediately after the bracket (e.g. "[BE] foo", "[be]: foo",
    "[FE]-bar"). The hygiene rule `epics_no_prefix` uses the same convention.
    """
    if not summary:
        return 'none'
    import re as _re
    m = _re.match(r'^\s*\[\s*(BE|FE)\s*\]', summary, flags=_re.IGNORECASE)
    if not m:
        return 'none'
    return m.group(1).lower()


def _render_epics_per_sprint_line_chart(sprints, epics_by_sprint, *, exclude_completed: bool = False):
    """Render three stacked SVG line charts — BE, FE, and missing-prefix — each with
    its own y-axis scale and a shared x-axis so columns align with the Gantt below.

    When ``exclude_completed`` is True, epics whose status is in the closed
    bucket (Done / Released / Resolved / Closed) are dropped from every
    bucket count. Used to render the "Hide Completed Epics" variant of the
    chart that the toolbar toggle swaps in.
    """
    if not sprints:
        return ''

    full_seq = _build_full_sprint_sequence(sprints)
    if not full_seq:
        return ''

    import re as _re
    from datetime import timedelta as _td

    def _milestone_key(short_label):
        """Slot label only (e.g. "M30.3") so concurrent FE/BE Jira sprints
        merge into a single x-axis point. The Epic Summary accordion below
        keeps them separate per Jira sprint — the chart deliberately rolls
        up to the milestone level for a tighter visual."""
        return fmt_sprint_slot(short_label)

    # Group full_seq entries by milestone slot, preserving order of first
    # occurrence. Concurrent FE/BE Jira sprints share dates — collapse them
    # into one x-axis point and sum their epic counts so the line doesn't
    # zigzag across sibling sprints.
    seen_keys = {}
    ordered_keys = []
    # Dedupe (epic_key, milestone) to avoid double-counting an epic that
    # appears in BOTH the FE Sprint and BE Sprint of the same milestone.
    counted_pairs: set[tuple[str, str]] = set()
    for s in full_seq:
        mk = _milestone_key(s['short_label'])
        if mk not in seen_keys:
            seen_keys[mk] = {
                'short_label': mk,
                'sprint_names': [],
                'start_date': s['start_date'],
                'end_date': s['end_date'],
                'placeholder': s['placeholder'],
                'counts': {'be': 0, 'fe': 0, 'none': 0},
            }
            ordered_keys.append(mk)
        entry = seen_keys[mk]
        entry['sprint_names'].append(s['sprint_name'])
        # A group is real if ANY member is real
        if not s['placeholder']:
            entry['placeholder'] = False
        # Accumulate epic counts across all sprints in this milestone. The
        # DB stores one row per (epic, sprint) pair, so an epic that's in
        # both the FE Sprint AND the BE Sprint of the same milestone would
        # otherwise count twice — guard with `counted_pairs`.
        sprint_epics = epics_by_sprint.get(s['sprint_id'], []) if s['sprint_id'] is not None else []
        for epic in sprint_epics:
            if exclude_completed and bucket_for(epic.get('status') or '') == 'closed':
                continue
            bare_key = (epic.get('ticket_key') or '').split('_s', 1)[0]
            pair = (bare_key, mk)
            if pair in counted_pairs:
                continue
            counted_pairs.add(pair)
            entry['counts'][_classify_epic_prefix(epic.get('summary', ''))] += 1

    points = []
    for mk in ordered_keys:
        entry = seen_keys[mk]
        entry['counts']['total'] = sum(entry['counts'][k] for k in ('be', 'fe', 'none'))
        points.append(entry)

    n = len(points)

    # Shared x-axis geometry. Left padding accommodates 3-digit y-axis labels
    # right-aligned 8px from the plot edge — was 36 (clipped at 2 digits and
    # crowded the gridline).
    width = 880
    padding_left = 48
    padding_right = 16
    plot_w = width - padding_left - padding_right

    sprint_spans = []
    for p in points:
        start = parse_iso_tz(p['start_date']).date()
        end = parse_iso_tz(p['end_date']).date()
        mid = start + _td(days=(end - start).days / 2)
        sprint_spans.append((start, end, mid))

    seq_start = sprint_spans[0][0]
    seq_end = sprint_spans[-1][1]
    seq_total_days = max((seq_end - seq_start).days, 1)

    def x_for(i):
        return padding_left + ((sprint_spans[i][2] - seq_start).days / seq_total_days) * plot_w

    last_real_idx = max((i for i, p in enumerate(points) if not p['placeholder']), default=-1)
    real_idxs = list(range(0, last_real_idx + 1)) if last_real_idx >= 0 else []
    proj_idxs = list(range(last_real_idx, n)) if 0 <= last_real_idx < n - 1 else []

    SERIES = [
        {'key': 'be',   'label': '[BE] Backend',   'stroke': 'var(--info)',    'row_label': 'BE Epics'},
        {'key': 'fe',   'label': '[FE] Frontend',  'stroke': 'var(--success)', 'row_label': 'FE Epics'},
        {'key': 'none', 'label': 'Missing prefix', 'stroke': 'var(--danger)',  'row_label': 'No prefix'},
    ]

    # Hide the "Missing prefix" row entirely when no epic across the visible
    # range is missing a [BE]/[FE] tag. Showing a flat-zero red line burns
    # vertical space and adds visual noise for a problem that doesn't exist.
    # If a missing-prefix epic appears later, the row reappears automatically.
    SERIES = [
        s for s in SERIES
        if s['key'] != 'none' or any(p['counts'].get('none', 0) for p in points)
    ]

    # Per-chart height; bottom chart gets extra room for rotated x-axis labels.
    chart_h = 130
    padding_top = 20
    padding_bottom_mid = 10
    padding_bottom_last = 56

    def render_sub_chart(series_def, is_last):
        k = series_def['key']
        color = series_def['stroke']
        label = series_def['label']
        pb = padding_bottom_last if is_last else padding_bottom_mid
        plot_h = chart_h - padding_top - pb

        data_max = max((p['counts'][k] for p in points), default=0)

        # Pick a "nice" step (1, 2, 5, 10, 20, 50, 100, …) so every tick is
        # evenly spaced and the top tick lands at or above the data max.
        # The previous logic appended the raw max value as an extra tick,
        # which produced uneven spacing (e.g. for max=9: [0, 2, 4, 6, 8, 9]).
        def _nice_step(max_value: int, target_ticks: int = 5) -> int:
            if max_value <= 0:
                return 1
            rough = max(1, max_value / target_ticks)
            for candidate in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
                if candidate >= rough:
                    return candidate
            return candidate

        step = _nice_step(data_max)
        # Round axis max up to the next multiple of step so the line clears
        # the top gridline cleanly.
        axis_max = max(step, ((data_max + step - 1) // step) * step)
        tick_values = list(range(0, axis_max + 1, step))

        # Use axis_max (not raw data max) for the y mapping so the top tick
        # is the actual top of the plot area.
        max_val = axis_max

        def y_for(val):
            return padding_top + plot_h - (val / max_val * plot_h)

        grid_lines = []
        axis_labels = []
        for v in tick_values:
            y = y_for(v)
            grid_lines.append(
                f'<line x1="{padding_left}" y1="{y:.1f}" x2="{width - padding_right}" y2="{y:.1f}" '
                f'stroke="var(--border)" stroke-width="1" stroke-dasharray="2,3" opacity="0.35"/>'
            )
            axis_labels.append(
                f'<text x="{padding_left - 8}" y="{y + 4:.1f}" text-anchor="end" '
                f'font-size="11" fill="var(--text-muted)">{v}</text>'
            )

        # Polylines
        def seg(indices):
            return ' '.join(f'{x_for(i):.1f},{y_for(points[i]["counts"][k]):.1f}' for i in indices)

        polylines = []
        rp = seg(real_idxs)
        if rp:
            polylines.append(
                f'<polyline points="{rp}" fill="none" stroke="{color}" '
                f'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        pp = seg(proj_idxs)
        if pp:
            polylines.append(
                f'<polyline points="{pp}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" '
                f'stroke-dasharray="5,4" opacity="0.55"/>'
            )

        # Dots + value labels.
        #
        # Hollow marker = "no work to show here yet" — covers both
        # synthesized placeholders and real-but-empty sprints (zero of THIS
        # series at this slot). Solid marker = there's at least one epic of
        # this series. Same convention as the Gantt header dimming.
        dot_parts = []
        for i, p in enumerate(points):
            count = p['counts'][k]
            x = x_for(i)
            y = y_for(count)
            is_ph = p['placeholder']
            is_empty = count == 0
            sprint_names_str = ', '.join(p['sprint_names'])
            title = (f'{sprint_names_str}: {count} {label} epic{"s" if count != 1 else ""}'
                     .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
            hollow = is_ph or is_empty
            fill = 'var(--bg-surface)' if hollow else color
            stroke_col = color if hollow else 'var(--bg-container)'
            dot_parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{fill}" '
                f'stroke="{stroke_col}" stroke-width="2"><title>{title}</title></circle>'
            )
            if count > 0:
                dot_parts.append(
                    f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" '
                    f'font-size="10" font-weight="600" fill="{color}">{count}</text>'
                )

        # X-axis labels only on the bottom chart
        x_label_parts = []
        if is_last:
            for i, p in enumerate(points):
                x = x_for(i)
                lbl = p['short_label'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                x_label_parts.append(
                    f'<text x="{x:.1f}" y="{chart_h - pb + 18:.1f}" text-anchor="middle" '
                    f'font-size="11" fill="var(--text-muted)" '
                    f'transform="rotate(-22 {x:.1f} {chart_h - pb + 18:.1f})">{lbl}</text>'
                )

        return (
            f'<svg viewBox="0 0 {width} {chart_h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%; height:auto; display:block;">'
            + ''.join(grid_lines)
            + ''.join(polylines)
            + ''.join(dot_parts)
            + ''.join(axis_labels)
            + ''.join(x_label_parts)
            + '</svg>'
        )

    rows_html = ''
    for idx, series_def in enumerate(SERIES):
        is_last = (idx == len(SERIES) - 1)
        svg = render_sub_chart(series_def, is_last)
        color = series_def['stroke']
        row_label = series_def['row_label']
        mb = '' if is_last else 'margin-bottom:4px;'
        rows_html += (
            f'<div style="display:flex; align-items:stretch; {mb}">'
            f'<div style="width:250px; flex-shrink:0; display:flex; align-items:center; padding-right:10px;">'
            f'<div style="color:{color}; font-size:var(--fs-sm); font-weight:600;">{row_label}</div>'
            f'</div>'
            f'<div style="flex:1;">{svg}</div>'
            f'</div>'
        )

    has_missing = any(s['key'] == 'none' for s in SERIES)
    legend_extra = ' / <strong style="color:var(--danger);">missing prefix</strong>' if has_missing else ''
    legend_note = ' Red signal = Ticket Hygiene will flag under <code>epics_no_prefix</code>.' if has_missing else ''
    return f"""
                        <div style="min-width:800px;">
{rows_html}
                        </div>
                        <p style="margin-top:var(--space-3); color:var(--text-muted); font-size:var(--fs-sm);">
                            Epic-type tickets per sprint split by
                            <strong style="color:var(--info);">[BE] backend</strong> /
                            <strong style="color:var(--success);">[FE] frontend</strong>{legend_extra}.
                            Dashed tails = projected sprints.{legend_note}
                        </p>
"""


def generate_epics_html(config: dict, output_path: Path):
    """Generate HTML epics dashboard with Gantt chart."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    # Get all sprints (current and next 7 sprints)
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Real Jira sprints only. Window is "recent 2 closed + every active +
    # the next 6 future", giving the Gantt a stable left-to-right span
    # without truncating future milestones now that the collector ingests
    # the whole board (M32+ used to be invisible). is_placeholder = 0 stays
    # as a belt-and-braces guard against any stale synthetic rows that
    # haven't been swept yet.
    cursor.execute("""
        SELECT sprint_id, sprint_name, start_date, end_date, state
        FROM (
            SELECT sprint_id, sprint_name, start_date, end_date, state
            FROM sprints
            WHERE is_placeholder = 0
              AND sprint_name LIKE ? || '%'
              AND state = 'closed'
            ORDER BY start_date DESC
            LIMIT 2
        )
        UNION
        SELECT sprint_id, sprint_name, start_date, end_date, state
        FROM sprints
        WHERE is_placeholder = 0
          AND sprint_name LIKE ? || '%'
          AND state = 'active'
        UNION
        SELECT sprint_id, sprint_name, start_date, end_date, state
        FROM (
            SELECT sprint_id, sprint_name, start_date, end_date, state
            FROM sprints
            WHERE is_placeholder = 0
              AND sprint_name LIKE ? || '%'
              AND state = 'future'
            ORDER BY start_date ASC
            LIMIT 6
        )
        ORDER BY start_date ASC
    """, (sprint_prefix, sprint_prefix, sprint_prefix))

    sprints = []
    for row in cursor.fetchall():
        sprints.append({
            'sprint_id': row[0],
            'sprint_name': row[1],
            'start_date': row[2],
            'end_date': row[3],
            'state': row[4]
        })

    if not sprints:
        conn.close()
        print("No sprints found")
        return

    # Get all epics across these sprints
    sprint_ids = [s['sprint_id'] for s in sprints]
    placeholders = ','.join('?' * len(sprint_ids))

    cursor.execute(f"""
        SELECT
            t.ticket_key,
            t.summary,
            t.status,
            t.assignee_display_name,
            t.story_points,
            t.ticket_url,
            s.sprint_id,
            s.sprint_name,
            s.start_date,
            s.end_date
        FROM tickets t
        JOIN sprints s ON t.sprint_id = s.sprint_id
        WHERE t.issue_type = 'Epic'
            AND t.sprint_id IN ({placeholders})
        ORDER BY s.start_date ASC, t.ticket_key
    """, sprint_ids)

    # Drop epics that the team has marked as "don't surface" (onboarding
    # tickets, etc.) so they're hidden from the Gantt, accordion, and the
    # line chart in one shot. The legacy hygiene-only suppression has moved
    # to a top-level config key — see src/utils/ignored_epics.py.
    from utils.ignored_epics import load_ignored_epics, is_ignored
    ignored = load_ignored_epics(config)

    epics_by_sprint = {}
    all_epics = []
    for row in cursor.fetchall():
        if is_ignored(row[0], ignored):
            continue
        epic = {
            'ticket_key': row[0],
            'summary': row[1],
            'status': row[2],
            'assignee': row[3],
            'story_points': row[4] or 0,
            'ticket_url': row[5],
            'sprint_id': row[6],
            'sprint_name': row[7],
            'start_date': row[8],
            'end_date': row[9]
        }
        all_epics.append(epic)

        if epic['sprint_id'] not in epics_by_sprint:
            epics_by_sprint[epic['sprint_id']] = []
        epics_by_sprint[epic['sprint_id']].append(epic)

    # Open child-issue counts per epic. Read from epic_open_children, which the
    # collector populates via a dedicated parent-scoped Jira query. We can NOT
    # derive this from the tickets table: an epic's open children are commonly
    # backlog items with no sprint, and the sprint-scoped collection never
    # pulls those (see jira_collector_agent.count_open_children). Keyed on the
    # bare epic key (no "_s<sprint>" suffix). Empty until the first collection
    # after this shipped, in which case every epic shows 0.
    try:
        cursor.execute("SELECT epic_key, open_count FROM epic_open_children")
        open_stories_by_epic = {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        # Table may not exist yet on a DB that predates the migration.
        open_stories_by_epic = {}

    conn.close()

    # Build the full sprint sequence (real DB sprints + synthesized placeholders
    # through M31.4) so the Gantt chart spans the same calendar range as the
    # line chart above it. Persist placeholders to the sprints table on the
    # way through so every page reads dates from a single source instead of
    # recomputing them in memory each time.
    full_sprint_sequence = _build_full_sprint_sequence(sprints)
    try:
        persist_placeholder_sprints(db_path, full_sprint_sequence)
    except Exception as e:
        # Persisting is a nice-to-have here — the in-memory sequence is still
        # correct and the page will render fine. Log so QA can see if writes
        # are silently failing.
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "Could not persist placeholder sprints: %s", e
        )

    # Calculate date range for Gantt chart
    # Use the full sequence so that projected sprints sit on the timeline too.
    if full_sprint_sequence:
        all_dates = []
        for sprint in full_sprint_sequence:
            # parse_iso_tz returns None on a null/malformed date; skip those
            # endpoints rather than crashing the whole Epics page (a blank
            # page is the worst possible "fact" to show).
            for _bound in ('start_date', 'end_date'):
                _parsed = parse_iso_tz(sprint.get(_bound))
                if _parsed is not None:
                    all_dates.append(_parsed.date())

    if full_sprint_sequence and all_dates:
        chart_start = min(all_dates)
        chart_end = max(all_dates)
        total_days = (chart_end - chart_start).days
    else:
        chart_start = datetime.now().date()
        chart_end = chart_start
        total_days = 1

    # Epic-count-per-sprint line chart (shown above the Gantt). We render
    # two variants — full and "completed epics excluded" — and let the
    # gantt-hide-completed checkbox toggle which is visible. Server-side
    # SVG can't be edited live without a re-render, so we just emit both
    # and flip a CSS class. Cheap: both share the same x-axis; the
    # filtered variant only differs in y-values.
    chart_full = _render_epics_per_sprint_line_chart(sprints, epics_by_sprint)
    chart_filtered = _render_epics_per_sprint_line_chart(
        sprints, epics_by_sprint, exclude_completed=True
    )
    if chart_full or chart_filtered:
        line_chart_html = f"""
            <div class="section">
                <h2 class="section-title">📈 Epics per Sprint</h2>
                <div class="chart-container">
                    <div style="background:#131c27; border-radius:8px; padding:20px; border:1px solid #243340; overflow-x:auto;">
                        <div class="epics-chart-variant epics-chart-full">{chart_full}</div>
                        <div class="epics-chart-variant epics-chart-filtered" style="display:none;">{chart_filtered}</div>
                    </div>
                </div>
            </div>
        """
    else:
        line_chart_html = ''

    # Build content
    content = f"""
        <header>
            <h1>📋 Epics Dashboard</h1>
            <div class="subtitle">Epic Timeline & Status • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('epics')}
        <div class="content">
            <div class="intro-banner">
                <p>Gantt chart showing epic timelines across sprints. Each epic is displayed as a bar spanning its assigned sprint duration.</p>
            </div>

{line_chart_html}

            <!-- Gantt Chart -->
            <div class="section">
                <div class="chart-container">
                    <div class="chart-title-row" style="display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
                        <div class="chart-title" style="margin: 0;">📊 Epic Timeline (Gantt Chart)</div>
                        <label class="gantt-filter-chip" style="display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; font-size: 13px; color: var(--text-secondary);">
                            <input type="checkbox" id="gantt-hide-completed" style="cursor: pointer;">
                            <span>Hide Completed Epics</span>
                        </label>
                    </div>
                    <div style="background: #131c27; border-radius: 8px; padding: 12px; border: 1px solid #243340; overflow-x: auto;">
                        <div class="gantt-wrapper" style="min-width: 1500px; position: relative;">
                            <!-- Sortable column headers for the epic-label area.
                                 Widths must mirror the per-row sub-cells below so
                                 the columns line up with the cells underneath. -->
                            <div style="display: flex; align-items: center; height: 28px; border-bottom: 1px solid #1a2430; font-size: 11px; font-weight: 600; color: #8194a6; text-transform: uppercase; letter-spacing: 0.04em;">
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'key')" style="width: 80px; padding: 0 8px 0 0;">Key</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'status')" style="width: 110px; padding: 0 8px 0 0;">Status</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'summary')" style="width: 190px; padding: 0 8px 0 0;">Summary</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'assignee')" style="width: 60px; padding: 0 8px 0 0;">Assignee</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'open')" style="width: 70px; padding: 0 8px 0 0; text-align: right;" title="Open child stories/bugs remaining (not closed)">Open</div>
                                <div style="flex: 1;"></div>
                            </div>
                            <!-- Header with dates -->
                            <div style="display: flex; margin-bottom: 4px;">
                                <div style="width: 510px; flex-shrink: 0;"></div>
                                <div style="flex: 1; position: relative; height: 50px;">
    """

    # Use the full (real + projected) sequence so the Gantt x-axis matches
    # the line chart above.
    sorted_sprints = full_sprint_sequence

    def pct(days_from_start):
        return (days_from_start / total_days * 100) if total_days > 0 else 0

    # Group sprints by their date range so concurrent FE/BE pairs render as
    # two stacked rows inside the same column.  Non-split sprints (role=None)
    # render as a single full-height row.
    sprint_groups = []  # list of (start_date, end_date, [sprint, ...])
    for sprint in sorted_sprints:
        s_start = parse_iso_tz(sprint['start_date']).date()
        s_end = parse_iso_tz(sprint['end_date']).date()
        if sprint_groups and sprint_groups[-1][0] == s_start and sprint_groups[-1][1] == s_end:
            sprint_groups[-1][2].append(sprint)
        else:
            sprint_groups.append((s_start, s_end, [sprint]))

    # Sort columns chronologically (already true from full_sprint_sequence,
    # but be defensive in case the source order ever drifts), and order the
    # FE/BE rows inside each column with FE first → BE second so the stack
    # is consistent across all columns. Without this, columns where the
    # placeholder counterpart was appended *after* its real sibling render
    # in insertion order — e.g. M30.4 was [BE real, FE placeholder] while
    # M30.3 was [FE real, BE real], so the visual stack flipped between
    # columns.
    def _row_sort_key(s):
        # FE = 0, BE = 1, anything else = 2 (no-role rows render alone).
        return {'FE': 0, 'BE': 1}.get(s.get('role'), 2)

    sprint_groups.sort(key=lambda g: g[0])
    for g in sprint_groups:
        g[2].sort(key=_row_sort_key)

    import re as _re2

    def _short_gantt_label(sprint_name, role):
        """Compact "M30.3 FE" label for Gantt header cells.

        Always feeds the full canonical sprint_name to format_short so we
        get exactly one normalised shape across every cell (real, real
        role-less, placeholder, TBD, etc.). The earlier code passed the
        pre-shortened `short_label` which sometimes already contained the
        role and produced doubled tokens like "M30.3 2026.12 FE FE".
        """
        return fmt_sprint_short(sprint_name)

    # One column per date range; concurrent FE/BE sprints stack inside.
    for (col_start, col_end, col_sprints) in sprint_groups:
        left_pct = pct((col_start - chart_start).days)
        width_pct = pct((col_end - col_start).days)
        n_rows = len(col_sprints)
        # Slot label rows split the 40px name area; 18px reserved for the date.
        row_height = max(14, 40 // n_rows)
        rows_html = ''
        for sp in col_sprints:
            role = sp.get('role')
            role_color = {'BE': '#2dd4a7', 'FE': '#c4b5fd'}.get(role, '#cdd9e5')
            # Visually dim a sprint when it has zero epics — "real but empty"
            # looks identical to "synthesized placeholder" to the reader, so
            # both render italic at 55% opacity. Solid styling reappears the
            # moment any epic lands in the sprint. The tooltip distinguishes
            # the two cases for anyone who needs the provenance.
            placeholder = sp.get('placeholder')
            sprint_id = sp.get('sprint_id')
            epic_count = (
                len(epics_by_sprint.get(sprint_id, [])) if sprint_id is not None else 0
            )
            is_empty = epic_count == 0
            dim_style = 'opacity:0.55; font-style:italic;' if (placeholder or is_empty) else ''
            label = _short_gantt_label(sp['sprint_name'], role)
            safe_label = label.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            # Tooltip explains the styling: placeholder vs real-but-empty vs
            # populated, plus the full canonical sprint_name.
            if placeholder:
                tooltip_extra = ' · placeholder (no Jira sprint exists yet)'
            elif is_empty:
                tooltip_extra = ' · no epics assigned yet'
            else:
                tooltip_extra = f' · {epic_count} epic{"s" if epic_count != 1 else ""}'
            full_title = (sp['sprint_name'] + tooltip_extra).replace(
                '&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            rows_html += (
                f'<div title="{full_title}" '
                f'style="height:{row_height}px; min-width:0; display:flex; align-items:center; '
                f'justify-content:center; font-size:12px; font-weight:600; '
                f'color:{role_color}; {dim_style} '
                f'overflow:hidden; white-space:nowrap; text-overflow:ellipsis; padding:0 4px;">'
                f'{safe_label}</div>'
            )
        date_label = (
            f'<div style="height:14px; min-width:0; font-size:10px; color:#8194a6; '
            f'text-align:center; white-space:nowrap; overflow:hidden; text-overflow:clip; '
            f'border-top:1px solid #1a2430; margin-top:1px; padding-top:1px;">'
            f'{col_start.strftime("%b %d")} – {col_end.strftime("%b %d")}</div>'
        )
        content += (
            f'<div style="position:absolute; left:{left_pct:.2f}%; width:{width_pct:.2f}%; '
            f'height:50px; border-left:1px solid #243340; box-sizing:border-box; padding:1px 2px; '
            f'overflow:hidden; display:flex; flex-direction:column; justify-content:space-between;" '
            f'title="{col_start.strftime("%b %d, %Y")} – {col_end.strftime("%b %d, %Y")}">'
            f'<div style="display:flex; flex-direction:column; flex:1; min-height:0;">{rows_html}</div>'
            f'{date_label}</div>'
        )

    content += """
                                </div>
                            </div>
    """

    # Vertical grid lines across the full Gantt — one per sprint column,
    # spanning header bottom through the last epic row. Layered above the
    # row backgrounds via z-index but `pointer-events:none` so the bars
    # underneath stay clickable for their tooltips.
    grid_lines = []
    for (col_start, _col_end, _col_sprints) in sprint_groups:
        col_left = pct((col_start - chart_start).days)
        # Absolute % is relative to the timeline area only (the 510px first
        # column lives outside this overlay), so we offset by 510px on the
        # outer wrapper. Easiest: nest the overlay inside a sibling that
        # mirrors the same layout the row uses (510px label slot + flex bar).
        grid_lines.append(
            f'<div style="position:absolute; left:{col_left:.2f}%; '
            f'top:0; bottom:0; width:1px; background:#1a2430; '
            f'pointer-events:none;"></div>'
        )
    # Also draw the right edge of the last column for visual closure.
    grid_lines.append(
        '<div style="position:absolute; left:100%; top:0; bottom:0; '
        'width:1px; background:#1a2430; pointer-events:none;"></div>'
    )
    content += f"""
                            <!-- Vertical sprint dividers spanning the full chart height.
                                 The overlay sits on top of the timeline area only;
                                 the 510px epic-label column stays untouched.
                                 top:83px = 29px col-header + 50px date row + 4px margin. -->
                            <div style="position:absolute; left:510px; right:0;
                                 top:83px; bottom:0; pointer-events:none; z-index:1;">
                                {''.join(grid_lines)}
                            </div>

                            <!-- Epic rows -->
                            <div class="gantt-rows">
    """

    # Snap each epic to its column's exact bounds so the highlight fills
    # the grid cell flush with the vertical separators. Epics inherit their
    # sprint's `start_date`/`end_date` straight from Jira, where start_date
    # is the wall-clock moment the sprint was opened (e.g. 17:55:57) — that
    # produced bars 12-13 days wide inside a 14-day column. Looking up the
    # canonical `(col_start, col_end)` from `sprint_groups` makes every
    # epic in a given sprint share the same width.
    col_bounds_by_sprint_id = {}
    for (col_start, col_end, col_sprints) in sprint_groups:
        for sp in col_sprints:
            col_bounds_by_sprint_id[sp.get('sprint_id')] = (col_start, col_end)

    # Group rows by bare ticket key so each epic renders as a single row
    # whose bar spans the union of every sprint it's in. The DB stores one
    # row per (epic, sprint) — that's correct for accounting, wrong for
    # presentation. An epic that genuinely spans M30.2 → M30.3 (e.g. it was
    # carried over after planning) should show a bar covering both columns.
    # We pick the latest-sprint row for status/SP/assignee since that's the
    # epic's "current" state; the bar geometry uses earliest-start to
    # latest-end across all rows.
    from collections import OrderedDict
    epics_by_key = OrderedDict()
    for epic in all_epics:
        bare = epic['ticket_key'].split('_s', 1)[0]
        epics_by_key.setdefault(bare, []).append(epic)

    # Render each epic as a row
    for bare_key, rows in epics_by_key.items():
        # Latest sprint = largest sprint_id (DB autoincrements per insert,
        # which loosely tracks recency for non-placeholder real sprints).
        # When start_date is available on the row, prefer that — it's the
        # canonical ordering signal the rest of the page already uses.
        def _row_sort_key(r):
            try:
                return parse_iso_tz(r['start_date']).date()
            except Exception:
                return None
        rows_sorted = sorted(
            rows,
            key=lambda r: (_row_sort_key(r) or chart_start, r.get('sprint_id') or 0),
        )
        epic = rows_sorted[-1]  # latest-sprint row is the "current" view

        # Bar geometry: union of every sprint this epic appears in. Use the
        # snapped column bounds where available so the bar lines up flush
        # with grid columns.
        starts, ends = [], []
        for r in rows_sorted:
            cb = col_bounds_by_sprint_id.get(r.get('sprint_id'))
            if cb:
                starts.append(cb[0])
                ends.append(cb[1])
            else:
                try:
                    starts.append(parse_iso_tz(r['start_date']).date())
                    ends.append(parse_iso_tz(r['end_date']).date())
                except Exception:
                    pass
        if not starts:
            # Defensive: should never happen since we filter epics by sprint,
            # but if it does fall back to whatever the latest-row says.
            starts.append(parse_iso_tz(epic['start_date']).date())
            ends.append(parse_iso_tz(epic['end_date']).date())
        epic_sprint_start = min(starts)
        epic_sprint_end = max(ends)

        days_from_chart_start = (epic_sprint_start - chart_start).days
        days_in_epic_sprint = (epic_sprint_end - epic_sprint_start).days

        left_pct = (days_from_chart_start / total_days * 100) if total_days > 0 else 0
        width_pct = (days_in_epic_sprint / total_days * 100) if total_days > 0 else 0

        # Color based on status (uses canonical buckets so new in-progress
        # statuses like 'Testing in progress' don't fall through to grey).
        epic_bucket = bucket_for(epic['status'])
        if epic_bucket == 'closed':
            bar_color = '#2dd4a7'
            status_badge = 'background: #07372c; color: #6ee7c3;'
        elif epic['status'] == 'Blocked':
            bar_color = '#fb6a5f'
            status_badge = 'background: #5e1a17; color: #fda4a0;'
        elif epic_bucket == 'in_progress':
            bar_color = '#38bdf8'
            status_badge = 'background: #0c3a52; color: #9bdcfb;'
        else:
            bar_color = '#566375'
            status_badge = 'background: #1a2430; color: #8194a6;'

        sp_chip = (
            f"<span style='display:inline-block; padding:2px 6px; border-radius:3px; "
            f"background:#1a2430; color:#fcd34d; font-weight:600;'>"
            f"{epic['story_points']:.0f} SP</span>"
            if epic['story_points'] > 0 else ""
        )
        assignee_chip = (
            f"<span style='color:#8194a6;'>{epic['assignee']}</span>"
            if epic['assignee'] else
            "<span style='color:#566375; font-style:italic;'>Unassigned</span>"
        )
        # Strip the "_s<jira_sprint_id>" cross-sprint suffix that
        # refresh_jira_data appends so the same epic can have one DB row per
        # sprint it spans. Display the bare Jira key — the URL already points
        # at the right ticket regardless.
        display_key = epic['ticket_key'].split('_s', 1)[0]
        # Per-row sort keys for the column headers above. Stored on the row
        # itself so the JS sorter only has to look at one element per row,
        # and so 'Unassigned' sorts to the end consistently.
        assignee_sort = epic['assignee'] or '￿'  # Unassigned → bottom
        # Mark rows whose Jira status is in the completed set so the
        # "Hide Completed Epics" toolbar above can hide them client-side
        # without re-rendering. Use the canonical bucket so this agrees with
        # the per-sprint line chart (which also keys off bucket_for) and with
        # the FNTSY status vocabulary — NOT sync_project_fantasy's board states.
        epic_status = (epic['status'] or '').strip()
        is_completed = bucket_for(epic_status) == 'closed'
        # Open child issues remaining — looked up by the bare epic key in the
        # collector-populated epic_open_children table (display_key already has
        # the "_s<sprint>" suffix stripped).
        open_stories = open_stories_by_epic.get(display_key, 0)
        sort_attrs = (
            f'data-sort-key="{html.escape(display_key)}" '
            f'data-sort-status="{html.escape(epic["status"] or "")}" '
            f'data-sort-summary="{html.escape(epic["summary"] or "")}" '
            f'data-sort-assignee="{html.escape(assignee_sort)}" '
            f'data-sort-open="{open_stories}" '
            f'data-completed="{"1" if is_completed else "0"}"'
        )
        # Render the count muted when zero so non-empty epics stand out.
        open_cell = (
            f"<span style='color:#cdd9e5; font-weight:600; font-variant-numeric:tabular-nums;'>{open_stories}</span>"
            if open_stories > 0 else
            "<span style='color:#566375;'>0</span>"
        )
        # Initials for the narrow Assignee column — full name appears in tooltip.
        if epic['assignee']:
            parts = epic['assignee'].split()
            initials = (parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else '')).upper()
            assignee_cell = (
                f"<span style='color:#cdd9e5;' title='{html.escape(epic['assignee'])}'>{html.escape(initials)}</span>"
            )
        else:
            assignee_cell = "<span style='color:#566375; font-style:italic;' title='Unassigned'>—</span>"
        content += f"""
                            <div class="gantt-row" {sort_attrs} style="display: flex; align-items: stretch; min-height: 30px; border-bottom: 1px solid #1a2430;">
                                <!-- Five sortable sub-cells (Key/Status/Summary/Assignee/Open).
                                     Widths total 510px to match the column headers above. -->
                                <div style="width: 80px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; overflow: hidden; white-space: nowrap;">
                                    <a href="{epic['ticket_url']}" target="_blank" style="color: #56cdf9; text-decoration: none; font-weight: 600;">{display_key}</a>
                                </div>
                                <div style="width: 110px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; gap: 4px; overflow: hidden; white-space: nowrap;">
                                    <span style="display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; {status_badge};">{epic['status']}</span>
                                    {sp_chip}
                                </div>
                                <div style="width: 190px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; color: #cdd9e5; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;" title="{html.escape(epic['summary'] or '')}">
                                    <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{html.escape(epic['summary'] or '')}</span>
                                </div>
                                <div style="width: 60px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 12px; display: flex; align-items: center; overflow: hidden; white-space: nowrap;">
                                    {assignee_cell}
                                </div>
                                <div style="width: 70px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; justify-content: flex-end;" title="Open child stories/bugs remaining">
                                    {open_cell}
                                </div>

                                <!-- Timeline cell — fills the grid square edge-to-edge.
                                     No background tint, no bottom gap; the colored
                                     cell touches the next row's border directly. -->
                                <div style="flex: 1; position: relative; align-self: stretch;">
                                    <div style="position: absolute; left: {left_pct}%; width: {width_pct}%; top: 0; bottom: 0; background: {bar_color};"></div>
                                </div>
                            </div>
        """

    content += """
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Epic Summary by Sprint -->
            <div class="section">
                <h2 class="section-title">Epic Summary by Sprint</h2>
    """

    def _epic_row_html(epic: dict) -> str:
        """Render one epic row — same markup the section used before, just
        factored out so it can be reused inside each role subgroup."""
        _eb = bucket_for(epic['status'])
        status_color = (
            '#2dd4a7' if _eb == 'closed'
            else '#fb6a5f' if epic['status'] == 'Blocked'
            else '#38bdf8' if _eb == 'in_progress'
            else '#566375'
        )
        sp_html = (
            f"<span style='color: #fbbf24; font-weight: 600;'>{epic['story_points']:.0f} SP</span>"
            if epic['story_points'] > 0 else ""
        )
        # Strip the cross-sprint "_s<jira_sprint_id>" suffix from the display
        # key — Jira itself only knows it as the bare key, and the URL is
        # already correct.
        display_key = epic['ticket_key'].split('_s', 1)[0]
        return f"""
                        <div style="background: #131c27; border-left: 3px solid {status_color}; border-radius: 6px; padding: 12px; display: flex; justify-content: space-between; align-items: center;">
                            <div style="flex: 1;">
                                <a href="{epic['ticket_url']}" target="_blank" style="color: #56cdf9; text-decoration: none; font-weight: 600; font-size: 14px;">{display_key}</a>
                                <span style="color: #cdd9e5; margin-left: 8px;">{epic['summary']}</span>
                            </div>
                            <div style="display: flex; gap: 10px; align-items: center;">
                                {sp_html}
                                <span style="color: #8194a6; font-size: 12px;">{epic['assignee'] or 'Unassigned'}</span>
                                <span style="padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; background: #243340; color: #cdd9e5;">{epic['status']}</span>
                            </div>
                        </div>
        """

    # Map real-DB sprints (id, state) by their canonical name so the
    # full_sprint_sequence loop below can reuse their state when known.
    real_state_by_name = {s['sprint_name']: s.get('state') for s in sprints}

    # Iterate the full sequence so the Epic Summary mirrors the Gantt:
    # all 8 milestone slots × FE/BE pairs, with synthesized placeholders
    # rendered alongside real sprints. Empty (placeholder OR real-with-zero)
    # accordions render dimmed-italic and collapsed.
    for sprint in full_sprint_sequence:
        sprint_id = sprint.get('sprint_id')
        sprint_epics = (
            epics_by_sprint.get(sprint_id, []) if sprint_id is not None else []
        )
        total_sp = sum(e['story_points'] for e in sprint_epics)
        completed_count = sum(1 for e in sprint_epics if bucket_for(e['status']) == 'closed')
        in_progress_count = sum(1 for e in sprint_epics if bucket_for(e['status']) == 'in_progress')

        # Bucket epics by their [BE]/[FE] summary prefix. Anything without
        # a recognized prefix lands under "Other" so we never silently drop
        # epics — the hygiene `epics_no_prefix` rule already flags those
        # tickets, this view just keeps them visible alongside the bucketed ones.
        be_epics = []
        fe_epics = []
        other_epics = []
        for ep in sprint_epics:
            cls = _classify_epic_prefix(ep.get('summary') or '')
            if cls == 'be':
                be_epics.append(ep)
            elif cls == 'fe':
                fe_epics.append(ep)
            else:
                other_epics.append(ep)

        # Same dimming rule as the Gantt: synthesized placeholders AND
        # real-but-empty sprints render italic + 55% opacity. Active/future
        # sprints with epics start expanded; everything else stays collapsed
        # so the section leads with what actually has work to discuss.
        is_placeholder = bool(sprint.get('placeholder'))
        is_empty = len(sprint_epics) == 0
        state = real_state_by_name.get(sprint['sprint_name'], 'future')
        is_open_default = (
            (state or '').lower() in ('active', 'future')
            and not is_empty
        )
        open_attr = ' open' if is_open_default else ''
        empty_class = ' epic-sprint-empty' if (is_placeholder or is_empty) else ''
        if is_placeholder:
            tooltip_extra = ' · placeholder (no Jira sprint exists yet)'
        elif is_empty:
            tooltip_extra = ' · no epics assigned yet'
        else:
            tooltip_extra = ''
        full_title = (
            (fmt_sprint_long(sprint['sprint_name']) + tooltip_extra)
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        )

        content += f"""
                <details class="epic-sprint-block{empty_class}"{open_attr}>
                    <summary class="epic-sprint-summary" title="{full_title}">
                        <span class="epic-sprint-caret">▸</span>
                        <span class="epic-sprint-name">{fmt_sprint_long(sprint['sprint_name'])}</span>
                        <span class="epic-sprint-counts">
                            <span class="epic-sprint-stat" style="color: #2dd4a7;">{completed_count} <small>done</small></span>
                            <span class="epic-sprint-stat" style="color: #38bdf8;">{in_progress_count} <small>in progress</small></span>
                            <span class="epic-sprint-stat" style="color: #38bdf8;">{len(sprint_epics)} <small>total</small></span>
                            <span class="epic-sprint-stat" style="color: #fbbf24;">{total_sp:.0f} <small>SP</small></span>
                        </span>
                    </summary>
                    <div class="epic-sprint-body">
        """
        if is_empty:
            empty_msg = (
                'Placeholder sprint — no Jira ticket exists yet.'
                if is_placeholder
                else 'No epics assigned yet.'
            )
            content += (
                '<div style="color:var(--text-muted); font-size:13px; '
                'font-style:italic; padding:8px 4px;">'
                f'{empty_msg}</div>'
            )

        # Per-role subgroups inside the sprint. Each is itself collapsible
        # via <details>; sub-sprints inherit the parent's open/closed default
        # so a closed sprint stays fully collapsed on first paint.
        role_groups = (
            ('be',    '⚙️ Backend [BE]',   be_epics),
            ('fe',    '🎨 Frontend [FE]',  fe_epics),
            ('other', '📦 Other',          other_epics),
        )
        for role_key, role_label, role_epics in role_groups:
            if not role_epics:
                continue
            role_total_sp = sum(e['story_points'] for e in role_epics)
            role_done = sum(1 for e in role_epics if bucket_for(e['status']) == 'closed')
            role_in_prog = sum(1 for e in role_epics if bucket_for(e['status']) == 'in_progress')
            content += f"""
                        <details class="epic-role-block role-{role_key}"{open_attr}>
                            <summary class="epic-role-summary">
                                <span class="epic-role-caret">▸</span>
                                <span class="epic-role-name">{role_label}</span>
                                <span class="epic-role-counts">
                                    <span class="epic-role-stat" style="color: #2dd4a7;">{role_done} <small>done</small></span>
                                    <span class="epic-role-stat" style="color: #38bdf8;">{role_in_prog} <small>in progress</small></span>
                                    <span class="epic-role-stat" style="color: #38bdf8;">{len(role_epics)} <small>total</small></span>
                                    <span class="epic-role-stat" style="color: #fbbf24;">{role_total_sp:.0f} <small>SP</small></span>
                                </span>
                            </summary>
                            <div class="epic-role-body">
            """
            for epic in role_epics:
                content += _epic_row_html(epic)
            content += """
                            </div>
                        </details>
            """

        content += """
                    </div>
                </details>
        """

    content += """
            </div>

            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    # Write HTML file
    rendered = render_html(
        title=f"Epics Dashboard",
        content=content,
        body_class=_PAGE_THEME["epics"],
    )

    _atomic_write(output_path, rendered)
    print(f"✅ Epics dashboard generated: {output_path}")


def generate_past_sprints_html(config: dict, output_path: Path):
    """Generate the Sprint Reports page.

    Lists every FNTSY sprint (closed, active, and future) with its
    Story+Bug roster grouped by engineer. The active sprint renders
    expanded so the page leads with what's in flight; closed and future
    sprints render collapsed. Incomplete sprints (active/future/empty) are
    dimmed (italic + 55% opacity) so a glance still tells you what's settled
    vs still in flight.
    """
    from collections import defaultdict

    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    # "In Code Review+" rollup: anything from code-review through Done in the
    # forward pipeline. Excludes side-states (Blocked, Waiting for Customer)
    # since those aren't strictly "past code review."
    in_code_review_plus = frozenset((
        'In Review',
        'In code review',
        'Testing in progress',
        'Ready for Testing',
        'Released to Test',
        'Ready for Prod Deployment',
    )) | set(CLOSED_STATUSES)

    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Pull every FNTSY sprint — real Jira rows AND the synthesized FE/BE
    # placeholders we stored to fill missing slots. Order chronologically by
    # start_date so the page reads M30.1 → M30.2 → M30.3 FE → M30.3 BE →
    # M30.4 FE → M30.4 BE → … same sequence the Epics page shows. Within a
    # shared start_date (paired FE/BE concurrent slots), put FE first.
    cursor.execute(
        """
        SELECT sprint_id, jira_sprint_id, sprint_name, start_date, end_date, state,
               COALESCE(is_placeholder, 0) AS is_placeholder
          FROM sprints
         WHERE sprint_name LIKE ? || '%'
           AND COALESCE(is_placeholder, 0) = 0
         ORDER BY
           start_date ASC,
           CASE
             WHEN sprint_name LIKE '% FE' THEN 0
             WHEN sprint_name LIKE '% BE' THEN 1
             ELSE 2
           END,
           sprint_name
        """,
        (sprint_prefix,),
    )
    past_sprints = [dict(row) for row in cursor.fetchall()]

    excl_ph = sql_placeholders(EXCLUDED_STATUSES)

    sprint_blocks = []
    for s in past_sprints:
        # Pull status_at_sprint_end alongside the live status. The page should
        # show the snapshot value (status as of sprint close) so rolled-over
        # tickets aren't credited or punished by what happened after the sprint
        # ended. Falls back to the live status when the column is empty
        # (sprint hasn't been backfilled with the changelog-aware writer yet).
        cursor.execute(
            f"""
            SELECT ticket_key, summary, status,
                   COALESCE(status_at_sprint_end, status) AS sprint_end_status,
                   assignee_display_name, story_points, ticket_url, issue_type
              FROM tickets
             WHERE sprint_id = ?
               AND issue_type IN ('Story', 'Bug')
               AND status NOT IN ({excl_ph})
             ORDER BY ticket_key
            """,
            (s['sprint_id'], *EXCLUDED_STATUSES),
        )
        tickets = [dict(r) for r in cursor.fetchall()]
        # Strip the cross-sprint "_s<jira_sprint_id>" suffix that backfill adds
        # to rolled-over Story/Bug rows (and to multi-sprint epics) so the link
        # text + export show the bare Jira key.
        for t in tickets:
            t['ticket_key'] = t['ticket_key'].split('_s', 1)[0]

        if not tickets:
            sprint_blocks.append(_render_past_sprint_empty(s))
            continue

        groups = defaultdict(list)
        for t in tickets:
            name = t['assignee_display_name'] or 'Unassigned'
            groups[name].append(t)

        engineers = []
        for name, items in groups.items():
            completed_sp = sum(
                (it['story_points'] or 0.0)
                for it in items
                if it['sprint_end_status'] in CLOSED_STATUSES
            )
            in_review_plus_sp = sum(
                (it['story_points'] or 0.0)
                for it in items
                if it['sprint_end_status'] in in_code_review_plus
            )
            total_sp = sum((it['story_points'] or 0.0) for it in items)
            engineers.append({
                'name': name,
                'tickets': sorted(items, key=lambda x: x['ticket_key']),
                'completed_sp': completed_sp,
                'in_review_plus_sp': in_review_plus_sp,
                'total_sp': total_sp,
                'count': len(items),
            })

        engineers.sort(key=lambda e: (-(e['completed_sp']), e['name'] == 'Unassigned', e['name']))

        sprint_total_completed = sum(e['completed_sp'] for e in engineers)
        sprint_total_review_plus = sum(e['in_review_plus_sp'] for e in engineers)
        sprint_total_sp = sum(e['total_sp'] for e in engineers)
        sprint_total_count = sum(e['count'] for e in engineers)

        sprint_blocks.append(
            _render_past_sprint_block(
                s, engineers,
                sprint_total_completed, sprint_total_review_plus,
                sprint_total_sp, sprint_total_count,
            )
        )

    conn.close()

    if not past_sprints:
        body = (
            '<div class="section"><p style="color: var(--text-muted);">'
            'No sprints found in the database yet. Wait for the next collector run.'
            '</p></div>'
        )
    else:
        body = ''.join(sprint_blocks)

    content = f"""
        <style>
            /* Per-engineer disclosure inside an expanded sprint */
            details.past-sprint-engineer summary::-webkit-details-marker {{ display: none; }}
            details.past-sprint-engineer[open] .past-sprint-chevron {{ transform: rotate(90deg); }}
            details.past-sprint-engineer summary:hover .past-sprint-chevron {{ color: #cdd9e5; }}
            /* Top-level sprint disclosure */
            details.sprint-report-block summary::-webkit-details-marker {{ display: none; }}
            details.sprint-report-block summary::marker {{ content: ''; }}
            details.sprint-report-block[open] > summary .sprint-report-chevron {{ transform: rotate(90deg); }}
            details.sprint-report-block summary {{ cursor: pointer; user-select: none; }}
            details.sprint-report-block summary:hover .sprint-report-chevron {{ color: #cdd9e5; }}
            details.sprint-report-block.is-incomplete > summary .sprint-report-name {{
                font-style: italic;
                opacity: 0.6;
            }}
            details.sprint-report-block.is-incomplete > summary .sprint-report-meta {{
                opacity: 0.6;
            }}
            .sprint-report-state {{
                font-size: 11px;
                font-weight: 600;
                letter-spacing: 0.06em;
                text-transform: uppercase;
                padding: 2px 8px;
                border-radius: 999px;
                margin-left: 8px;
            }}
            .sprint-report-state.closed {{ background: #07372c; color: #6ee7c3; }}
            .sprint-report-state.active {{ background: #5a3a09; color: #fcd34d; }}
            .sprint-report-state.future {{ background: #1a2430; color: #8194a6; }}
            .sprint-report-state.placeholder {{ background: #0c3a52; color: #9bdcfb; }}
        </style>
        <header>
            <h1>📜 Sprint Reports</h1>
            <div class="subtitle">Stories &amp; Bugs grouped by engineer • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('past-sprints')}
        <div class="content">
            <div class="intro-banner">
                <p>One section per FNTSY sprint — closed sprints first, then active and future. The active sprint stays expanded; closed and future sprints appear collapsed (and in-flight/upcoming ones are dimmed). Engineers are sorted by completed story points (Done / Closed / Resolved). Click any sprint header to collapse or expand it.</p>
            </div>
            {body}
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    html_doc = render_html(
        title="Sprint Reports",
        content=content,
        body_class=_PAGE_THEME["past-sprints"],
    )
    _atomic_write(output_path, html_doc)
    print(f"✅ Sprint Reports dashboard generated: {output_path}")


def _render_past_sprint_empty(sprint: dict) -> str:
    """Section shown when a sprint has no tickets in the DB.

    Placeholder rows (synthesized FE/BE counterparts) get a distinct
    "placeholder" pill so the user can tell them apart from real Jira
    sprints that simply have nothing assigned yet. Both render dimmed; the
    active sprint stays expanded (consistent with populated sprints) even
    when empty, everything else collapsed.
    """
    name = html.escape(fmt_sprint_long(sprint['sprint_name']))
    is_placeholder = bool(sprint.get('is_placeholder'))
    state = (sprint.get('state') or '').lower()
    open_attr = ' open' if (state == 'active' and not is_placeholder) else ''
    if is_placeholder:
        state_class = 'placeholder'
        state_label = 'PLACEHOLDER'
    elif state in ('closed', 'active', 'future'):
        state_class = state
        state_label = state.upper()
    else:
        state_class = 'future'
        state_label = 'UNKNOWN'
    if is_placeholder:
        body_msg = (
            "Synthesised placeholder — no real Jira sprint exists for this "
            "FE/BE counterpart yet. Create it in Jira to populate this slot."
        )
    elif state == 'closed':
        cmd = f"python3 scripts/backfill_past_sprint.py --sprint-id {sprint['jira_sprint_id']}"
        body_msg = (
            f'No data for this sprint. Run <code>{html.escape(cmd)}</code> to populate it.'
        )
    elif state == 'active':
        body_msg = "No tickets assigned to this sprint yet — work hasn't been planned in."
    else:
        body_msg = "No tickets assigned yet — this sprint hasn't started."
    return f"""
            <details class="sprint-report-block is-incomplete" style="margin-bottom: 28px;"{open_attr}>
                <summary style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; padding: 8px 0; border-bottom: 2px solid var(--border);">
                    <h2 class="section-title sprint-report-name" style="margin: 0; border-bottom: none; padding-bottom: 0;">
                        <span class="sprint-report-chevron" aria-hidden="true" style="display: inline-block; width: 12px; color: #8194a6; transition: transform 0.15s; margin-right: 6px;">▶</span>
                        {name}
                        <span class="sprint-report-state {state_class}">{state_label}</span>
                    </h2>
                    <div class="sprint-report-meta" style="color: var(--text-muted); font-size: 13px;">no tickets · 0 SP</div>
                </summary>
                <p style="color: var(--text-muted); padding: 12px 4px;">{body_msg}</p>
            </details>
    """


def _status_badge(status: str) -> str:
    """Return a small inline badge for a ticket status, color-coded by bucket."""
    if status in CLOSED_STATUSES:
        bg, fg = '#07372c', '#6ee7c3'
    elif status in IN_PROGRESS_STATUSES:
        bg, fg = '#0c3a52', '#9bdcfb'
    elif status == 'Blocked':
        bg, fg = '#5e1a17', '#fda4a0'
    else:
        bg, fg = '#1a2430', '#8194a6'
    return (
        f'<span style="padding: 3px 8px; border-radius: 4px; font-size: 11px; '
        f'font-weight: 600; background: {bg}; color: {fg};">{html.escape(status)}</span>'
    )


def _format_sp(value: float) -> str:
    """Format story points: integer if whole, one decimal otherwise."""
    if value == int(value):
        return f"{int(value)}"
    return f"{value:.1f}"


def _render_past_sprint_block(
    sprint: dict,
    engineers: list,
    total_completed_sp: float,
    total_review_plus_sp: float,
    total_sp: float,
    total_count: int,
) -> str:
    """Render one sprint section with per-engineer subgroups.

    The active sprint(s) render expanded by default so the page leads with
    what's in flight right now; closed and future sprints render collapsed.
    Dimming is a separate axis: incomplete sprints (anything not closed)
    still render dimmed (italic + 60% opacity on the title and meta) so a
    glance distinguishes settled work from in-progress/upcoming.
    """
    name = html.escape(fmt_sprint_long(sprint['sprint_name']))
    start = sprint['start_date'][:10] if sprint['start_date'] else ''
    end = sprint['end_date'][:10] if sprint['end_date'] else ''
    state = (sprint.get('state') or '').lower()
    is_placeholder = bool(sprint.get('is_placeholder'))
    if is_placeholder:
        state_class = 'placeholder'
        state_label = 'PLACEHOLDER'
    elif state in ('closed', 'active', 'future'):
        state_class = state
        state_label = state.upper()
    else:
        state_class = 'future'
        state_label = 'UNKNOWN'
    is_complete = (state == 'closed' and not is_placeholder)
    # Expand the active sprint(s); collapse everything else (closed, future,
    # placeholder). Dimming still keys off is_complete so closed sprints stay
    # solid and in-flight/upcoming ones stay dimmed.
    is_active = (state == 'active' and not is_placeholder)
    open_attr = ' open' if is_active else ''
    block_class = 'sprint-report-block' + ('' if is_complete else ' is-incomplete')

    sprint_slug = re.sub(r'[^a-z0-9]+', '-', sprint['sprint_name'].lower()).strip('-') or 'sprint'
    sprint_block_id = f'sprint-block-{sprint_slug}'
    # Sprint-wide TSV: every ticket, with engineer name as a column.
    sprint_table_rows = []
    engineer_html = []
    for e in engineers:
        rows = []
        engineer_table_rows = []
        for t in e['tickets']:
            sp = _format_sp(t['story_points'] or 0.0)
            type_label = t['issue_type']
            type_color = '#fbbf24' if type_label == 'Bug' else '#8194a6'
            status_for_export = t.get('sprint_end_status') or t['status']
            rows.append(f"""
                            <div style="background: #131c27; border-left: 3px solid #243340; border-radius: 6px; padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; gap: 12px;">
                                <div style="flex: 1; min-width: 0;">
                                    <a href="{html.escape(t['ticket_url'] or '')}" target="_blank" style="color: #56cdf9; text-decoration: none; font-weight: 600; font-size: 13px;">{html.escape(t['ticket_key'])}</a>
                                    <span style="color: {type_color}; font-size: 10px; font-weight: 600; margin-left: 6px;">{html.escape(type_label)}</span>
                                    <span style="color: #cdd9e5; margin-left: 8px;">{html.escape(t['summary'] or '')}</span>
                                </div>
                                <div style="display: flex; gap: 10px; align-items: center; flex-shrink: 0;">
                                    <span style="color: #cdd9e5; font-size: 12px; font-variant-numeric: tabular-nums;">{sp} SP</span>
                                    {_status_badge(status_for_export)}
                                </div>
                            </div>
            """)
            engineer_table_rows.append([
                t['ticket_key'],
                type_label or '',
                t['summary'] or '',
                sp,
                status_for_export or '',
                t.get('ticket_url') or '',
            ])
            sprint_table_rows.append([
                e['name'],
                t['ticket_key'],
                type_label or '',
                t['summary'] or '',
                sp,
                status_for_export or '',
                t.get('ticket_url') or '',
            ])

        eng_slug = re.sub(r'[^a-z0-9]+', '-', e['name'].lower()).strip('-') or 'engineer'
        eng_block_id = f'sprint-{sprint_slug}-eng-{eng_slug}'
        eng_export_btns = _feature_export_buttons(
            group_id=eng_block_id,
            label=f"{fmt_sprint_long(sprint['sprint_name'])} — {e['name']}",
        )
        eng_export_table = _export_table_html(
            ['Ticket', 'Type', 'Summary', 'Story Points', 'Status', 'URL'],
            engineer_table_rows,
        )

        engineer_html.append(f"""
                <details class="past-sprint-engineer" id="{eng_block_id}" style="background: #1a2430; border-radius: 8px; padding: 16px; margin-bottom: 14px;">
                    <summary style="list-style: none; cursor: pointer; outline: none;">
                        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
                            <h3 style="font-size: 16px; color: #f4f8fb; margin: 0; display: flex; align-items: center; gap: 8px;">
                                <span class="past-sprint-chevron" aria-hidden="true" style="display: inline-block; width: 10px; color: #8194a6; transition: transform 0.15s;">▶</span>
                                {html.escape(e['name'])}
                            </h3>
                            <div style="display: flex; gap: 14px;">
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #38bdf8;">{_format_sp(e['in_review_plus_sp'])}</div>
                                    <div style="font-size: 10px; color: #8194a6;">In Code Review+</div>
                                </div>
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #2dd4a7;">{_format_sp(e['completed_sp'])}</div>
                                    <div style="font-size: 10px; color: #8194a6;">Completed SP</div>
                                </div>
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #38bdf8;">{_format_sp(e['total_sp'])}</div>
                                    <div style="font-size: 10px; color: #8194a6;">Total SP</div>
                                </div>
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #cdd9e5;">{e['count']}</div>
                                    <div style="font-size: 10px; color: #8194a6;">Stories</div>
                                </div>
                                {eng_export_btns}
                            </div>
                        </div>
                    </summary>
                    {eng_export_table}
                    <div style="display: grid; gap: 6px; margin-top: 12px;">
                        {''.join(rows)}
                    </div>
                </details>
        """)

    sprint_export_btns = _feature_export_buttons(
        group_id=sprint_block_id,
        label=f"Sprint Report — {fmt_sprint_long(sprint['sprint_name'])}",
    )
    sprint_export_table = _export_table_html(
        ['Engineer', 'Ticket', 'Type', 'Summary', 'Story Points', 'Status', 'URL'],
        sprint_table_rows,
    )

    # Carryover = committed work that wasn't closed at sprint end (rolled into
    # a later sprint or dropped). The honest companion to "completed": a sprint
    # can hit its SP number while still rolling a chunk of tickets. Only shown
    # for closed sprints, where "at sprint end" is meaningful.
    completed_count = sum(
        1 for e in engineers for t in e['tickets']
        if (t.get('sprint_end_status') or t['status']) in CLOSED_STATUSES
    )
    carry_count = total_count - completed_count
    carry_sp = total_sp - total_completed_sp
    carryover_meta = ''
    if is_complete and carry_count > 0:
        carryover_meta = (
            f' · <span style="color: var(--warning-text, #fbbf24);">'
            f'{carry_count} rolled ({_format_sp(carry_sp)} SP)</span>'
        )

    return f"""
            <details class="{block_class}" id="{sprint_block_id}" style="margin-bottom: 28px;"{open_attr}>
                <summary style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; padding: 8px 0; margin-bottom: 12px; border-bottom: 2px solid var(--border);">
                    <h2 class="section-title sprint-report-name" style="margin: 0; border-bottom: none; padding-bottom: 0;">
                        <span class="sprint-report-chevron" aria-hidden="true" style="display: inline-block; width: 12px; color: #8194a6; transition: transform 0.15s; margin-right: 6px;">▶</span>
                        {name}
                        <span class="sprint-report-state {state_class}">{state_label}</span>
                    </h2>
                    <div class="sprint-report-meta" style="color: var(--text-muted); font-size: 13px;">{start} → {end} · {total_count} tickets · {_format_sp(total_completed_sp)} completed · {_format_sp(total_review_plus_sp)} in code review+ · {_format_sp(total_sp)} total SP{carryover_meta}</div>
                    {sprint_export_btns}
                </summary>
                {sprint_export_table}
                {''.join(engineer_html)}
            </details>
    """


def generate_pull_requests_html(config: dict, output_path: Path):
    """Generate HTML repositories / pull requests dashboard."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    sprint = get_current_sprint(db_path, sprint_prefix)
    if not sprint:
        print("No active sprint found")
        return

    # Get PR metrics
    pr_size_dist = get_pr_size_distribution(db_path, days=30)
    team_pr_review_time = get_team_pr_review_time(db_path, days=30)
    pr_approvals = get_pr_approvals_by_developer(db_path, days=30)
    first_review = get_time_to_first_review(db_path, days=30)
    review_load = get_review_load_by_reviewer(db_path, days=30)
    size_vs_merge = get_pr_size_vs_merge_time(db_path, days=30)

    # Fetch open PRs grouped by repo
    conn = get_connection(db_path)
    cursor = conn.cursor()
    # Scope: repos whose name contains "fantasy" (case-insensitive).
    # SQLite's LIKE is case-insensitive for ASCII, so lower() isn't strictly needed,
    # but using it makes intent explicit.
    cursor.execute(
        """
        SELECT repository, pr_number, title, author_github_username, created_at, pr_url,
               lines_added, lines_deleted
        FROM github_prs
        WHERE state = 'open'
          -- Defence in depth: a row claiming 'open' but carrying a merge/close
          -- timestamp is contradictory data — never surface it as open.
          AND merged_at IS NULL
          AND closed_at IS NULL
          AND lower(repository) LIKE '%fantasy%'
        ORDER BY repository, created_at
        """
    )
    open_prs_rows = cursor.fetchall()
    conn.close()

    from collections import defaultdict
    now = datetime.now()

    def _age_days(created_at):
        try:
            created = parse_iso_tz(created_at)
            # Strip tz for simple subtraction with naive `now`
            created_naive = created.replace(tzinfo=None)
            return max(0, (now - created_naive).days)
        except Exception:
            return None

    repos = defaultdict(list)
    for row in open_prs_rows:
        repos[row['repository']].append({
            'pr_number': row['pr_number'],
            'title': row['title'] or '(no title)',
            'author': row['author_github_username'],
            'age_days': _age_days(row['created_at']),
            'url': row['pr_url'] or f"https://github.com/{row['repository']}/pull/{row['pr_number']}",
            'lines_added': row['lines_added'] or 0,
            'lines_deleted': row['lines_deleted'] or 0,
        })

    sorted_repos = sorted(repos.keys(), key=str.lower)

    content = f"""
        <header>
            <h1>📦 Repositories</h1>
            <div class="subtitle">{fmt_sprint_long(sprint['sprint_name'])} • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('pull-requests')}
        <div class="content">
            <div class="intro-banner">
                <p>Repositories monitored by this dashboard. Click any repo to expand the list of open pull requests and how long each has been waiting.</p>
            </div>

            <!-- Repositories with open PRs -->
            <div class="section">
                <h2 class="section-title">📂 Repositories ({len(sorted_repos)} with open PRs)</h2>
                <div class="repo-list">
    """

    for repo in sorted_repos:
        prs = repos[repo]
        open_count = len(prs)
        # Oldest PR age for the repo header
        oldest_age = max((p['age_days'] for p in prs if p['age_days'] is not None), default=0)
        repo_slug = repo.replace('/', '-').replace('.', '-')
        short_name = repo.split('/', 1)[1] if '/' in repo else repo
        org_name = repo.split('/', 1)[0] if '/' in repo else ''

        content += f"""
                    <div class="repo-row">
                        <button type="button" class="repo-header" onclick="toggleAccordion('repo-{repo_slug}')" aria-controls="repo-{repo_slug}" aria-expanded="false">
                            <div class="repo-name">
                                <span class="repo-icon">📦</span>
                                <span class="repo-short-name">{short_name}</span>
                                {f'<span class="repo-org">{org_name}</span>' if org_name else ''}
                            </div>
                            <div class="repo-stats">
                                <span class="repo-stat"><strong>{open_count}</strong> open</span>
                                <span class="repo-stat repo-stat-muted">oldest {oldest_age}d</span>
                                <span class="repo-caret">▸</span>
                            </div>
                        </button>
                        <div id="repo-{repo_slug}" class="accordion-panel">
                            <div class="accordion-content repo-pr-list">
                                <div class="pr-row pr-row-header">
                                    <div class="pr-title">PR</div>
                                    <div class="pr-author">Author</div>
                                    <div class="pr-age">Age</div>
                                    <div class="pr-size">Size</div>
                                </div>
        """

        # Oldest PRs first so attention-requiring items surface
        for pr in sorted(prs, key=lambda p: -(p['age_days'] or 0)):
            age = pr['age_days']
            if age is None:
                age_label = '—'
                age_class = 'pr-age-unknown'
            elif age >= 14:
                age_label = f"{age}d"
                age_class = 'pr-age-stale'
            elif age >= 7:
                age_label = f"{age}d"
                age_class = 'pr-age-old'
            elif age >= 1:
                age_label = f"{age}d"
                age_class = 'pr-age-recent'
            else:
                age_label = 'today'
                age_class = 'pr-age-recent'

            size_label = f"+{pr['lines_added']}/-{pr['lines_deleted']}" if (pr['lines_added'] or pr['lines_deleted']) else '—'
            content += f"""
                                <div class="pr-row">
                                    <div class="pr-title">
                                        <a href="{pr['url']}" target="_blank">#{pr['pr_number']}</a>
                                        <span class="pr-title-text">{pr['title']}</span>
                                    </div>
                                    <div class="pr-author">{pr['author']}</div>
                                    <div class="pr-age {age_class}">{age_label}</div>
                                    <div class="pr-size">{size_label}</div>
                                </div>
            """

        content += """
                            </div>
                        </div>
                    </div>
        """

    if not sorted_repos:
        content += """
                    <div class="empty-state">
                        <div class="icon">🎉</div>
                        <div>No open PRs across monitored repositories.</div>
                    </div>
        """

    content += """
                </div>
            </div>

            <!-- PR Size Distribution -->
            <div class="section">
                <div class="chart-container">
                    <div class="chart-title">📏 PR Size Distribution (Last 30 Days)</div>
                    <div style="display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; padding: 12px 0;">
    """

    total_prs = sum(pr_size_dist.values())
    sizes = [
        ('XS', pr_size_dist['xs'], '<50 lines', '#2dd4a7'),
        ('S', pr_size_dist['s'], '50-200', '#38bdf8'),
        ('M', pr_size_dist['m'], '200-400', '#fbbf24'),
        ('L', pr_size_dist['l'], '400-800', '#fb6a5f'),
        ('XL', pr_size_dist['xl'], '>800', '#5e1a17')
    ]

    for label, count, range_text, color in sizes:
        percentage = (count / total_prs * 100) if total_prs > 0 else 0
        content += f"""
                        <div style="text-align: center; background: #131c27; padding: 16px; border-radius: 8px;">
                            <div style="font-size: 12px; color: #8194a6; margin-bottom: 8px;">{label}</div>
                            <div style="font-size: 28px; font-weight: 700; color: {color}; margin-bottom: 4px;">{count}</div>
                            <div style="font-size: 11px; color: #566375;">{range_text}</div>
                            <div style="font-size: 11px; color: #566375; margin-top: 4px;">{percentage:.0f}%</div>
                        </div>
        """

    avg_review_time_str = f"{team_pr_review_time:.0f}h" if team_pr_review_time else "N/A"

    # Time-to-first-review: median is the headline (right-skewed), mean shown
    # as sub so a few stale PRs don't misrepresent the typical wait.
    if first_review:
        ttfr_value = f"{first_review['median_hours']:.0f}h"
        ttfr_sub = f"median · avg {first_review['avg_hours']:.0f}h ({first_review['pr_count']} PRs)"
    else:
        ttfr_value, ttfr_sub = "N/A", "no review data"

    content += """
                    </div>
                </div>
            </div>
    """

    # Merge time by PR size — pairs the two halves above (size + merge time)
    # to show the size→latency relationship with the team's own data. Scaled
    # to the largest bucket average; buckets with no PRs render as "—".
    svm_max = max((b['avg_hours'] for b in size_vs_merge if b['avg_hours']), default=0)
    if svm_max > 0:
        svm_colors = {'XS': '#2dd4a7', 'S': '#38bdf8', 'M': '#fbbf24', 'L': '#fb6a5f', 'XL': '#5e1a17'}
        svm_bars = ''
        for b in size_vs_merge:
            ah = b['avg_hours']
            color = svm_colors.get(b['bucket'], '#38bdf8')
            if ah:
                pct = ah / svm_max * 100
                val_label = f"{ah:.0f}h" if ah < 48 else f"{ah/24:.1f}d"
                fill = f'<div class="flow-stage-fill" style="width: {pct:.0f}%; background: {color};"></div>'
            else:
                val_label = '—'
                fill = ''
            svm_bars += f"""
                <div class="flow-stage-row">
                    <div class="flow-stage-name">{b['bucket']} <span style="color: var(--text-faint);">({b['label']})</span></div>
                    <div class="flow-stage-track">{fill}</div>
                    <div class="flow-stage-val">{val_label} <span style="color: var(--text-faint); font-weight: 400;">· n={b['count']}</span></div>
                </div>
            """
        content += f"""
            <div class="section">
                <div class="chart-container">
                    <div class="chart-title">⚖️ Merge Time by PR Size (Last 30 Days)</div>
                    <div class="chart-subtitle">Avg working hours from open to merge, by lines changed. Smaller PRs typically clear review faster.</div>
                    <div class="flow-stages">{svm_bars}</div>
                </div>
            </div>
        """

    content += f"""
            <!-- PR Review Metrics -->
            <div class="section">
                <h2 class="section-title">⏱️ Review Metrics</h2>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-label">Time to First Review</div>
                        <div class="metric-value">{ttfr_value}</div>
                        <div class="metric-subtext">{ttfr_sub}</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-label">Avg Review Time</div>
                        <div class="metric-value">{avg_review_time_str}</div>
                        <div class="metric-subtext">open to merge</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-label">Total PRs</div>
                        <div class="metric-value">{total_prs}</div>
                        <div class="metric-subtext">last 30 days</div>
                    </div>
                </div>
            </div>

            <!-- PR Activity by Developer -->
            <div class="section">
                <h2 class="section-title">👤 PR Activity by Developer (Last 30 Days)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Developer</th>
                            <th>PRs Created</th>
                            <th>PRs Merged</th>
                            <th>Avg Hours to Merge</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    # pr_approvals is actually a list of dicts with PR activity
    if pr_approvals:
        for dev_pr in pr_approvals:
            github_username = dev_pr.get('author_github_username', 'Unknown')
            pr_count = dev_pr.get('pr_count', 0)
            merged_count = dev_pr.get('merged_count', 0)
            avg_hours = dev_pr.get('avg_hours_to_merge')

            avg_hours_str = f"{avg_hours:.1f}h" if avg_hours else "N/A"

            content += f"""
                        <tr>
                            <td><strong>{github_username}</strong></td>
                            <td>{pr_count}</td>
                            <td>{merged_count}</td>
                            <td>{avg_hours_str}</td>
                        </tr>
            """

    content += """
                    </tbody>
                </table>
            </div>

            <!-- Review Activity by Reviewer -->
            <div class="section">
                <h2 class="section-title">🔍 Review Activity by Reviewer (Last 30 Days)</h2>
                <p style="color: var(--text-muted); font-size: 13px; margin-top: -8px;">
                    Who carries review load. A lopsided share concentrates context (and risk) in a few reviewers.
                </p>
                <table>
                    <thead>
                        <tr>
                            <th>Reviewer</th>
                            <th>Reviews</th>
                            <th>PRs Reviewed</th>
                            <th>Approved</th>
                            <th>Changes Req.</th>
                            <th>Inline Comments</th>
                            <th>Share</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    if review_load:
        for rv in review_load:
            content += f"""
                        <tr>
                            <td><strong>{rv['reviewer']}</strong></td>
                            <td>{rv['reviews']}</td>
                            <td>{rv['prs_reviewed']}</td>
                            <td>{rv['approved']}</td>
                            <td>{rv['changes_requested']}</td>
                            <td>{rv['inline_comments']}</td>
                            <td>{rv['share_pct']:.0f}%</td>
                        </tr>
            """
    else:
        content += """
                        <tr><td colspan="7" style="color: var(--text-muted); font-style: italic;">No review activity in the last 30 days.</td></tr>
        """

    content += """
                    </tbody>
                </table>
            </div>

            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    # Write HTML file
    html = render_html(
        title=f"Repositories - {fmt_sprint_long(sprint['sprint_name'])}",
        content=content,
        body_class=_PAGE_THEME["pull-requests"],
    )

    _atomic_write(output_path, html)
    print(f"✅ Repositories dashboard generated: {output_path}")


def generate_project_fantasy_html(output_path: Path):
    """Generate the Project: Fantasy roadmap page with a deliverable timeline.

    Dates match the Fantasy launch phases roadmap. 'Q4 2026' and '2027' are
    anchored to Oct 1 2026 and Jan 1 2027 for charting — rendered as "~" labels.
    """
    from datetime import date, timedelta

    phases = [
        {
            'name': 'Alpha (internal build)',
            'description': 'Focus on core functionality to access app, make picks, enter contests, and settle picks',
            'date_label': 'Jun 30, 2026',
            'date': date(2026, 6, 30),
            'approximate': False,
        },
        {
            'name': 'Fanatics Fest (internal build)',
            'description': 'Additional functionality and some polish to show users at Fanatics Fest',
            'date_label': 'Jul 15, 2026',
            'date': date(2026, 7, 15),
            'approximate': False,
        },
        {
            'name': 'Beta (external, friends & family)',
            'description': 'Account and pick-level functionality needed to put app in hands of real users for real testing',
            'date_label': 'Aug 12, 2026',
            'date': date(2026, 8, 12),
            'approximate': False,
        },
        {
            'name': 'Install-base activation',
            'description': 'Customer-ready app, with focus on functionality needed to activate existing FBG users',
            'date_label': 'Sep 9, 2026',
            'date': date(2026, 9, 9),
            'approximate': False,
        },
        {
            'name': 'Net-new customer acquisition',
            'description': 'Additional functionality needed to start acquiring net new DFS-first users',
            'date_label': 'Q4 2026',
            'date': date(2026, 10, 1),
            'approximate': True,
        },
        {
            'name': 'Marketing push',
            'description': '',
            'date_label': '2027',
            'date': date(2027, 1, 1),
            'approximate': True,
        },
    ]

    today = date.today()
    # Timeline spans from a little before the first deliverable to a little after the last
    first = min(p['date'] for p in phases)
    last = max(p['date'] for p in phases)
    timeline_start = min(today, first) - timedelta(days=14)
    timeline_end = last + timedelta(days=14)
    total_days = max((timeline_end - timeline_start).days, 1)

    # SVG geometry
    svg_w, svg_h = 1100, 300
    pad_l, pad_r, pad_t, pad_b = 40, 40, 70, 60
    inner_w = svg_w - pad_l - pad_r
    inner_h = svg_h - pad_t - pad_b
    baseline_y = pad_t + inner_h * 0.7  # timeline bar sits below middle, leaves room for labels above

    def x_at(d):
        days_from_start = (d - timeline_start).days
        return pad_l + (days_from_start / total_days) * inner_w

    # Month tick marks across the axis
    month_ticks_svg = ''
    # Start on the first of the month at or after timeline_start
    y = timeline_start.year
    m = timeline_start.month
    cur = date(y, m, 1)
    if cur < timeline_start:
        if m == 12:
            cur = date(y + 1, 1, 1)
        else:
            cur = date(y, m + 1, 1)
    while cur <= timeline_end:
        tx = x_at(cur)
        month_label = cur.strftime('%b %Y') if cur.month == 1 else cur.strftime('%b')
        month_ticks_svg += (
            f'<line x1="{tx:.1f}" y1="{baseline_y - 6}" x2="{tx:.1f}" y2="{baseline_y + 6}" stroke="#243340" stroke-width="1" />'
            f'<text x="{tx:.1f}" y="{baseline_y + 22}" text-anchor="middle" fill="#8194a6" font-size="10">{month_label}</text>'
        )
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    # Deliverable vertical lines + labels (stagger label Y to avoid overlap on dense clusters)
    phases_svg = ''
    # Colors cycle so adjacent markers are distinguishable. Scoreboard palette:
    # broadcast cyan leads, then the semantic state hues — no indigo/violet.
    colors = ['#38bdf8', '#2dd4a7', '#fbbf24', '#fb6a5f', '#9bdcfb', '#5eead4']
    for idx, phase in enumerate(phases):
        px = x_at(phase['date'])
        color = colors[idx % len(colors)]
        label_y = pad_t + (idx % 3) * 18 + 12  # stagger across 3 rows near the top
        dash = '' if not phase['approximate'] else 'stroke-dasharray="5,3"'
        marker = f'{"~" if phase["approximate"] else ""}{phase["date_label"]}'
        phases_svg += (
            # Vertical line from label row down to baseline
            f'<line x1="{px:.1f}" y1="{label_y + 6}" x2="{px:.1f}" y2="{baseline_y}" stroke="{color}" stroke-width="2" {dash} />'
            # Dot on baseline
            f'<circle cx="{px:.1f}" cy="{baseline_y}" r="5" fill="{color}" stroke="#0f1620" stroke-width="2" />'
            # Phase name above the line
            f'<text x="{px:.1f}" y="{label_y}" text-anchor="middle" fill="{color}" font-size="11" font-weight="600">{phase["name"]}</text>'
            # Date label above the dot
            f'<text x="{px:.1f}" y="{baseline_y - 12}" text-anchor="middle" fill="#cdd9e5" font-size="10">{marker}</text>'
        )

    # Today marker (only if today sits in the visible range)
    today_marker_svg = ''
    if timeline_start <= today <= timeline_end:
        tx = x_at(today)
        today_marker_svg = (
            f'<line x1="{tx:.1f}" y1="{pad_t}" x2="{tx:.1f}" y2="{svg_h - pad_b}" stroke="#56cdf9" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.8" />'
            f'<text x="{tx:.1f}" y="{svg_h - pad_b + 40}" text-anchor="middle" fill="#56cdf9" font-size="11" font-weight="600">Today · {today.strftime("%b %d, %Y")}</text>'
        )

    # Baseline bar (filled portion = progress to today)
    baseline_svg = (
        f'<line x1="{pad_l}" y1="{baseline_y}" x2="{svg_w - pad_r}" y2="{baseline_y}" stroke="#243340" stroke-width="3" />'
    )
    if timeline_start <= today <= timeline_end:
        baseline_svg += (
            f'<line x1="{pad_l}" y1="{baseline_y}" x2="{x_at(today):.1f}" y2="{baseline_y}" stroke="#38bdf8" stroke-width="3" />'
        )

    project_label_str = project_label()
    content = f"""
        <header>
            <h1>🎯 {project_label_str}</h1>
            <div class="subtitle">Launch roadmap • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('project-fantasy')}
        <div class="content">
            <div class="intro-banner">
                <p>Major deliverables for the Fantasy launch. Dates labelled with "~" are approximate (originally specified as a quarter or year).</p>
            </div>

            <!-- Timeline chart -->
            <div class="section">
                <div class="chart-container">
                    <div class="chart-title">📅 Deliverable Timeline</div>
                    <div class="roadmap-svg-wrap">
                        <svg viewBox="0 0 {svg_w} {svg_h}" preserveAspectRatio="xMidYMid meet" style="width: 100%; height: 320px; display: block;">
                            {month_ticks_svg}
                            {baseline_svg}
                            {phases_svg}
                            {today_marker_svg}
                        </svg>
                    </div>
                </div>
            </div>

            <!-- Deliverables table -->
            <div class="section">
                <h2 class="section-title">📋 Phases</h2>
                <table class="roadmap-table">
                    <thead>
                        <tr>
                            <th style="width: 26%;">Phase</th>
                            <th>Description</th>
                            <th style="width: 15%; text-align: right;">Date</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    for phase in phases:
        date_display = f"~{phase['date_label']}" if phase['approximate'] else phase['date_label']
        days_out = (phase['date'] - today).days
        if days_out < 0:
            relative = f'<span style="color: var(--text-faint);">{abs(days_out)}d ago</span>'
        elif days_out == 0:
            relative = '<span style="color: var(--success-text);">today</span>'
        else:
            relative = f'<span style="color: var(--text-muted);">in {days_out}d</span>'

        content += f"""
                        <tr>
                            <td><strong>{phase['name']}</strong></td>
                            <td>{phase['description']}</td>
                            <td style="text-align: right;">
                                <div>{date_display}</div>
                                <div style="font-size: 11px; margin-top: 4px;">{relative}</div>
                            </td>
                        </tr>
        """

    content += """
                    </tbody>
                </table>
            </div>
    """

    # Snapshot-driven sections (Jira + Confluence). If the snapshot file is
    # missing, show an empty state with instructions.
    content += _render_project_fantasy_snapshot()

    content += """
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    html = render_html(
        title=project_label_str,
        content=content,
        body_class=_PAGE_THEME["project-fantasy"],
    )
    _atomic_write(output_path, html)
    print(f"✅ {project_label_str} dashboard generated: {output_path}")


def generate_features_html(output_path: Path):
    """Generate the Features page — the two feature-list rollups.

    Reads data/project_fantasy.json (produced by sync_project_fantasy.py)
    and renders the by-Milestone + by-Launch sections that previously lived
    on the Project: Fantasy page.
    """
    init_key = _jira_initiative_key()
    init_url = f"{_jira_cloud_base()}/browse/{init_key}"
    content = f"""
        <header>
            <h1>🗂️ Features</h1>
            <div class="subtitle">{project_label()} feature rollups • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('features')}
        <div class="content">
            <div class="intro-banner">
                <p>All features under <a href="{init_url}" target="_blank" style="color: var(--accent-text);">{init_key}</a>, grouped by Launch phase. RAG and Weekly Status freshness come from PM weekly updates in Jira.</p>
            </div>
    """
    content += _render_feature_lists()
    content += """
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    html = render_html(
        title="Features",
        content=content,
        body_class=_PAGE_THEME["features"],
    )
    _atomic_write(output_path, html)
    print(f"✅ Features dashboard generated: {output_path}")


# ---------------------------------------------------------------------------
# Readiness by Phase page
# ---------------------------------------------------------------------------
# A forecast/burnup is only honest once scope is decomposed. Beta and beyond
# are still being broken down, so instead of projecting against a denominator
# we know is wrong, this page measures *how much of each launch phase is even
# knowable yet* — Features → have Epics → Epics have Stories — and gates the
# idea of forecasting on a readiness threshold.

# A phase at/above this decomposition % is considered scoped enough that a
# burnup/forecast for it would be meaningful. Below it, the page says so
# plainly rather than projecting against unknown scope.
_READINESS_FORECAST_THRESHOLD = 85.0

# Terminal/abandoned states never count toward decomposition gaps — you don't
# groom a dropped feature. Mirrors the dropped bucket the snapshot assigns.
_READINESS_LIVE = lambda bucket: bucket != 'dropped'


def _compute_phase_readiness(snap: dict) -> list:
    """Return per-launch-phase decomposition readiness.

    Walks the Feature → Epic → Story hierarchy from the project snapshot and,
    for each launch phase, counts:
      * features with no child epics at all (can't be scoped or sized yet)
      * epics with no child stories (sized container, unknown contents)
    Readiness % = share of "decomposition checkpoints" that are satisfied,
    where each live feature contributes a "has epics?" checkpoint and each
    live epic contributes a "has stories?" checkpoint. A phase with no live
    features is reported as readiness=None (nothing to scope yet).

    Returns an ordered list of dicts (one per phase) with the raw worklists
    so the renderer can show both the rollup bar and the grooming punch list.
    """
    features = [f for f in (snap.get('features') or []) if _READINESS_LIVE(f.get('status_bucket'))]
    epics = [e for e in (snap.get('epics') or []) if _READINESS_LIVE(e.get('status_bucket'))]
    stories = [s for s in (snap.get('stories') or []) if _READINESS_LIVE(s.get('status_bucket'))]

    epics_by_feature: dict[str, list] = {}
    for e in epics:
        epics_by_feature.setdefault(e.get('parent'), []).append(e)
    stories_by_epic: dict[str, list] = {}
    for s in stories:
        stories_by_epic.setdefault(s.get('parent'), []).append(s)

    launch_order = ['Alpha', 'Beta', 'Public Launch', 'Post Launch']
    groups: dict[str, list] = {name: [] for name in launch_order}
    groups['Unassigned'] = []
    for f in features:
        phase = (f.get('launch') or '').strip()
        if phase in groups:
            groups[phase].append(f)
        elif phase:
            groups.setdefault(phase, []).append(f)
        else:
            groups['Unassigned'].append(f)

    ordered_phases = (
        launch_order
        + sorted(k for k in groups if k not in launch_order and k != 'Unassigned')
        + ['Unassigned']
    )

    rows = []
    for phase in ordered_phases:
        feats = groups.get(phase) or []
        if not feats:
            continue
        features_no_epics = []
        epics_no_stories = []
        checkpoints_total = 0
        checkpoints_met = 0
        epic_count = 0
        for f in feats:
            checkpoints_total += 1  # "feature has epics?" checkpoint
            fe = epics_by_feature.get(f['key'], [])
            # A completed feature is fully scoped by definition — the work
            # shipped — so it's never a decomposition gap even if its epics
            # weren't modeled here. Same for completed epics below.
            f_done = f.get('status_bucket') == 'done'
            if fe:
                checkpoints_met += 1
                for e in fe:
                    epic_count += 1
                    checkpoints_total += 1  # "epic has stories?" checkpoint
                    if stories_by_epic.get(e['key']) or e.get('status_bucket') == 'done':
                        checkpoints_met += 1
                    else:
                        epics_no_stories.append(e)
            elif f_done:
                checkpoints_met += 1  # done feature counts as scoped
            else:
                features_no_epics.append(f)

        readiness = (checkpoints_met / checkpoints_total * 100) if checkpoints_total else None
        rows.append({
            'phase': phase,
            'feature_count': len(feats),
            'epic_count': epic_count,
            'features_no_epics': features_no_epics,
            'epics_no_stories': epics_no_stories,
            'readiness': readiness,
            'forecastable': readiness is not None and readiness >= _READINESS_FORECAST_THRESHOLD,
        })
    return rows


def _readiness_tone(pct):
    """Map a readiness % to a semantic tone class. None = not-yet-scoped."""
    if pct is None:
        return 'muted'
    if pct >= _READINESS_FORECAST_THRESHOLD:
        return 'success'
    if pct >= 50:
        return 'warning'
    return 'danger'


def _render_readiness_worklist(title: str, items: list, kind: str) -> str:
    """Render a grooming punch list (features-without-epics or epics-without-
    stories). `kind` controls the verb shown. Empty list → nothing."""
    if not items:
        return ''
    verb = 'Break down into epics' if kind == 'feature' else 'Add child stories'
    li = []
    for it in sorted(items, key=lambda x: x.get('key', '')):
        key = html.escape(it.get('key', ''))
        summary = html.escape(it.get('summary') or '')
        url = it.get('url') or '#'
        status = html.escape(it.get('status') or '')
        li.append(
            f'<li class="readiness-work-item">'
            f'<a href="{url}" target="_blank" class="ticket-key">{key}</a>'
            f'<span class="readiness-work-summary">{summary}</span>'
            f'<span class="badge status">{status}</span>'
            f'</li>'
        )
    return (
        f'<div class="readiness-worklist">'
        f'<div class="readiness-worklist-head">{html.escape(title)} '
        f'<span class="readiness-work-action">→ {verb}</span></div>'
        f'<ul>{"".join(li)}</ul>'
        f'</div>'
    )


def generate_readiness_html(output_path: Path):
    """Generate the Readiness by Phase page.

    Reads data/project_fantasy.json and reports decomposition readiness per
    launch phase, with a per-phase "ready to forecast?" gate and a grooming
    worklist of what's not yet broken down. Designed for the case where Beta+
    scope is still being defined, so a burnup/forecast would project against
    an unknown denominator.
    """
    import json as _json
    snapshot_path = Path(__file__).parent.parent / "data" / "project_fantasy.json"

    intro = (
        '<div class="intro-banner">'
        '<p><strong>Decomposition readiness</strong> measures how much of each launch '
        'phase is broken down far enough to track or forecast: '
        '<em>Features → Epics → Stories</em>. A phase only becomes forecastable once it '
        f'crosses <strong>{_READINESS_FORECAST_THRESHOLD:.0f}%</strong> — below that, scope is '
        'still being defined and any burnup would project against an unknown total. '
        'The worklists below are the grooming that closes the gap.</p>'
        '</div>'
    )

    if not snapshot_path.exists():
        body = (
            '<div class="section"><div class="intro-banner" style="border-left-color: var(--warning);">'
            '<p><strong>No project snapshot yet.</strong> Run '
            '<code>python3 scripts/sync_project_fantasy.py</code> to populate readiness.</p>'
            '</div></div>'
        )
    else:
        try:
            snap = _json.loads(snapshot_path.read_text())
        except Exception as e:
            snap = None
            body = (
                f'<div class="section"><div class="intro-banner" style="border-left-color: var(--danger);">'
                f'<p><strong>Snapshot file is malformed:</strong> {html.escape(str(e))}</p>'
                f'</div></div>'
            )
        if snap is not None:
            rows = _compute_phase_readiness(snap)
            if not rows:
                body = (
                    '<div class="section"><div class="empty-state">'
                    '<div class="icon">📦</div><div>No live features found in the snapshot.</div>'
                    '</div></div>'
                )
            else:
                # Overall readiness banner — weighted by checkpoint counts so a
                # heavily-scoped phase isn't outvoted by a tiny one.
                forecastable = [r for r in rows if r['forecastable']]
                total_feats = sum(r['feature_count'] for r in rows)
                total_gap_feats = sum(len(r['features_no_epics']) for r in rows)
                total_gap_epics = sum(len(r['epics_no_stories']) for r in rows)
                cards = []
                cards.append(
                    f'<div class="metric-card success">'
                    f'<div class="metric-label">Phases Forecastable</div>'
                    f'<div class="metric-value">{len(forecastable)}<span style="font-size:.5em;color:var(--text-muted)"> / {len(rows)}</span></div>'
                    f'<div class="metric-subtext">≥ {_READINESS_FORECAST_THRESHOLD:.0f}% decomposed</div></div>'
                )
                cards.append(
                    f'<div class="metric-card{" warning" if total_gap_feats else ""}">'
                    f'<div class="metric-label">Features Not Broken Down</div>'
                    f'<div class="metric-value">{total_gap_feats}<span style="font-size:.5em;color:var(--text-muted)"> / {total_feats}</span></div>'
                    f'<div class="metric-subtext">no child epics yet</div></div>'
                )
                cards.append(
                    f'<div class="metric-card{" warning" if total_gap_epics else ""}">'
                    f'<div class="metric-label">Epics Not Broken Down</div>'
                    f'<div class="metric-value">{total_gap_epics}</div>'
                    f'<div class="metric-subtext">no child stories yet</div></div>'
                )
                banner = f'<div class="metrics-grid">{"".join(cards)}</div>'

                phase_blocks = []
                for r in rows:
                    pct = r['readiness']
                    tone = _readiness_tone(pct)
                    pct_label = '—' if pct is None else f'{pct:.0f}%'
                    bar_w = 0 if pct is None else pct
                    gate = (
                        '<span class="readiness-gate ready">✓ Ready to forecast</span>'
                        if r['forecastable'] else
                        '<span class="readiness-gate not-ready">⏳ Scope still being defined</span>'
                    )
                    worklists = (
                        _render_readiness_worklist(
                            f'{len(r["features_no_epics"])} feature(s) with no epics',
                            r['features_no_epics'], 'feature')
                        + _render_readiness_worklist(
                            f'{len(r["epics_no_stories"])} epic(s) with no stories',
                            r['epics_no_stories'], 'epic')
                    )
                    if not worklists:
                        worklists = '<div class="readiness-clear">✓ Fully broken down — nothing to groom.</div>'

                    phase_blocks.append(
                        f'<div class="readiness-phase">'
                        f'<div class="readiness-phase-head">'
                        f'<span class="readiness-phase-name">{html.escape(r["phase"])}</span>'
                        f'<span class="readiness-phase-meta">{r["feature_count"]} features · {r["epic_count"]} epics</span>'
                        f'{gate}'
                        f'<span class="readiness-pct {tone}">{pct_label}</span>'
                        f'</div>'
                        f'<div class="readiness-bar"><div class="readiness-bar-fill {tone}" style="width:{bar_w:.1f}%"></div></div>'
                        f'<div class="readiness-worklists">{worklists}</div>'
                        f'</div>'
                    )

                body = (
                    f'<div class="section">{banner}</div>'
                    f'<div class="section"><h2 class="section-title">🧩 Decomposition by Launch Phase</h2>'
                    f'<div class="readiness-phase-list">{"".join(phase_blocks)}</div></div>'
                )

    content = f"""
        <header>
            <h1>🧩 Readiness by Phase</h1>
            <div class="subtitle">Decomposition readiness • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('readiness')}
        <div class="content">
            {intro}
            {body}
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """
    html_page = render_html(
        title="Readiness by Phase",
        content=content,
        body_class=_PAGE_THEME["readiness"],
    )
    _atomic_write(output_path, html_page)
    print(f"✅ Readiness dashboard generated: {output_path}")


# ---------------------------------------------------------------------------
# Delivery Excellence page
# ---------------------------------------------------------------------------
# Every other page asks "are we on track?" (status). This page asks "is the
# WAY we work getting better?" — flow efficiency, rework, and predictability.
# All team-level (deliberately blameless — no per-dev leaderboards) and
# trended over the closed-sprint history. Sourced from the transition log and
# sprint snapshots; see queries.get_flow_efficiency / _rework_rate /
# _predictability.

def _excellence_gauge(label: str, value, unit: str, tone: str, sub: str,
                      help_text: str = '', be=None, fe=None) -> str:
    """One headline metric tile. value=None renders an em-dash.

    When be/fe are given, a split row shows each track's value beneath the
    team number so the headline never hides a BE/FE divergence.
    """
    val = '—' if value is None else f'{value}{unit}'
    title = f' title="{html.escape(help_text)}"' if help_text else ''
    split = ''
    if be is not None or fe is not None:
        be_s = '—' if be is None else f'{be}{unit}'
        fe_s = '—' if fe is None else f'{fe}{unit}'
        split = (
            f'<div class="excellence-gauge-split">'
            f'<span class="excellence-split-chip be">BE {be_s}</span>'
            f'<span class="excellence-split-chip fe">FE {fe_s}</span>'
            f'</div>'
        )
    return (
        f'<div class="excellence-gauge {tone}"{title}>'
        f'<div class="excellence-gauge-label">{html.escape(label)}</div>'
        f'<div class="excellence-gauge-value">{val}</div>'
        f'<div class="excellence-gauge-sub">{html.escape(sub)}</div>'
        f'{split}'
        f'</div>'
    )


def generate_delivery_excellence_html(config: dict, output_path: Path):
    """Render the Delivery Excellence page (flow / rework / predictability)."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']
    window = 90

    # Assignee → role map drives the BE/FE split on flow & rework (ticket-level
    # work). Predictability splits by sprint-track role instead (BE/FE run as
    # separate sprints), handled inside get_predictability.
    name_to_role, _ = _build_role_maps(config)
    flow = get_flow_efficiency(db_path, days=window, name_to_role=name_to_role)
    rework = get_rework_rate(db_path, days=window, name_to_role=name_to_role)
    pred = get_predictability(db_path, sprint_prefix, num_sprints=12)
    flow_be, flow_fe = flow['by_role']['BE'], flow['by_role']['FE']
    rw_be, rw_fe = rework['by_role']['BE'], rework['by_role']['FE']
    pred_be, pred_fe = pred['by_role']['BE'], pred['by_role']['FE']

    # --- Headline gauges ---------------------------------------------------
    eff = flow['efficiency_pct']
    eff_tone = 'muted' if eff is None else ('success' if eff >= 40 else 'warning' if eff >= 25 else 'danger')
    rw = rework['rework_pct']
    # Lower rework is better — invert the tone thresholds.
    rw_tone = 'muted' if rw is None else ('success' if rw <= 10 else 'warning' if rw <= 20 else 'danger')
    saydo = pred['say_do_avg']
    saydo_tone = 'muted' if saydo is None else ('success' if 90 <= saydo <= 115 else 'warning' if 75 <= saydo <= 130 else 'danger')
    cov = pred['velocity_cov_pct']
    # Lower coefficient of variation = steadier = better.
    cov_tone = 'muted' if cov is None else ('success' if cov <= 20 else 'warning' if cov <= 35 else 'danger')

    gauges = ''.join([
        _excellence_gauge(
            'Flow Efficiency', eff, '%', eff_tone,
            f'{flow["active_days"]:.0f}d active · {flow["wait_days"]:.0f}d waiting',
            'Share of in-flight time spent actively worked vs. waiting in a queue. '
            'Higher is better; industry-typical knowledge work is often 15-40%.',
            be=flow_be['efficiency_pct'], fe=flow_fe['efficiency_pct']),
        _excellence_gauge(
            'Rework Rate', rw, '%', rw_tone,
            f'{rework["rework_tickets"]} of {rework["total_tickets"]} tickets bounced back',
            'Share of tickets that moved backward in the pipeline at least once '
            '(e.g. review → in-progress). Lower is better.',
            be=rw_be['rework_pct'], fe=rw_fe['rework_pct']),
        _excellence_gauge(
            'Say / Do', saydo, '%', saydo_tone,
            'avg completed ÷ committed, recent sprints',
            'Commitment reliability: completed vs committed work per sprint, averaged. '
            '~100% means the team finishes what it plans.',
            be=pred_be['say_do_avg'], fe=pred_fe['say_do_avg']),
        _excellence_gauge(
            'Velocity Stability', cov, '%', cov_tone,
            f'± around {pred["velocity_mean"] or 0:.0f} SP/sprint',
            'Coefficient of variation of completed story points across sprints. '
            'LOWER is better — a steady team is more predictable than a high-but-erratic one.',
            be=pred_be['velocity_cov_pct'], fe=pred_fe['velocity_cov_pct']),
    ])

    # --- Where the waiting happens (flow breakdown) ------------------------
    wait_rows = ''
    wbs = flow['wait_by_status']
    wait_max = max(wbs.values()) if wbs else 0
    for status, dval in wbs.items():
        pctw = (dval / wait_max * 100) if wait_max else 0
        wait_rows += (
            f'<div class="excellence-flow-row">'
            f'<span class="excellence-flow-name">{html.escape(status)}</span>'
            f'<span class="excellence-flow-track"><span class="excellence-flow-fill" style="width:{pctw:.1f}%"></span></span>'
            f'<span class="excellence-flow-val">{dval:.0f}d</span>'
            f'</div>'
        )
    if not wait_rows:
        wait_rows = '<div class="readiness-clear">No measurable queue time in the window.</div>'
    top_q = flow['top_queue']
    queue_callout = (
        f'<p class="excellence-callout">Biggest queue: <strong>{html.escape(top_q["status"])}</strong> '
        f'— {top_q["days"]:.0f} cumulative working-days of tickets waiting here. '
        f'This is the handoff to optimize first.</p>'
    ) if top_q else ''

    # BE-vs-FE flow comparison — the team number can hide a big track gap.
    def _track_line(role_label, rdata):
        e = rdata['efficiency_pct']
        tq = rdata['top_queue']
        e_txt = '—' if e is None else f'{e:.0f}%'
        tq_txt = f' · biggest queue {html.escape(tq["status"])} ({tq["days"]:.0f}d)' if tq else ''
        return (
            f'<div class="excellence-track-line">'
            f'<span class="excellence-split-chip {role_label.lower()}">{role_label}</span>'
            f'<strong>{e_txt}</strong> flow efficiency'
            f'<span class="excellence-track-detail">{rdata["active_days"]:.0f}d active · '
            f'{rdata["wait_days"]:.0f}d waiting{tq_txt}</span>'
            f'</div>'
        )
    track_compare = (
        f'<div class="excellence-track-compare">{_track_line("BE", flow_be)}{_track_line("FE", flow_fe)}</div>'
    )

    # --- Rework hotspots ---------------------------------------------------
    hop_rows = ''
    for h in rework['top_hops']:
        hop_rows += (
            f'<li class="excellence-hop"><span class="excellence-hop-name">{html.escape(h["hop"])}</span>'
            f'<span class="badge status">{h["count"]}×</span></li>'
        )
    hop_block = (
        f'<ul class="excellence-hop-list">{hop_rows}</ul>'
        if hop_rows else '<div class="readiness-clear">✓ No backward transitions in the window.</div>'
    )

    # --- Predictability trend (per-sprint say/do bars) ---------------------
    trend_rows = ''
    for s in pred['sprints']:
        acc = s['accuracy']
        if not s['planned']:
            continue
        tone = 'success' if 90 <= acc <= 115 else 'warning' if 75 <= acc <= 130 else 'danger'
        bar = min(acc, 150) / 150 * 100
        role = s.get('role')
        role_chip = (
            f'<span class="excellence-split-chip {role.lower()}">{role}</span>'
            if role in ('BE', 'FE') else ''
        )
        trend_rows += (
            f'<div class="excellence-trend-row">'
            f'<span class="excellence-trend-name">{role_chip}{html.escape(fmt_sprint_short(s["sprint_name"]))}</span>'
            f'<span class="excellence-trend-track"><span class="excellence-trend-fill {tone}" style="width:{bar:.1f}%"></span>'
            f'<span class="excellence-trend-target"></span></span>'
            f'<span class="excellence-trend-val">{acc:.0f}%</span>'
            f'<span class="excellence-trend-detail">{s["completed"]}/{s["planned"]} · {s["completed_sp"]:.0f} SP</span>'
            f'</div>'
        )
    if not trend_rows:
        trend_rows = '<div class="readiness-clear">Not enough closed-sprint history yet.</div>'

    body = f"""
        <div class="section">
            <div class="excellence-gauges">{gauges}</div>
        </div>

        <div class="section">
            <h2 class="section-title">⏳ Where the Time Goes</h2>
            <p class="section-note">In-flight time split by queue. Active work is healthy; long queues are
            waste between handoffs — the lever for faster delivery without working harder.</p>
            {queue_callout}
            {track_compare}
            <div class="excellence-flow">{wait_rows}</div>
        </div>

        <div class="section">
            <h2 class="section-title">↩️ Rework Hotspots</h2>
            <p class="section-note">Backward transitions in the last {window} days — work bouncing back a stage.
            These are the points where quality or clarity is breaking down.</p>
            {hop_block}
        </div>

        <div class="section">
            <h2 class="section-title">🎯 Predictability Trend</h2>
            <p class="section-note">Completed ÷ committed per closed sprint. The marker at 100% is the target —
            consistently near it matters more than any single high number.</p>
            <div class="excellence-trend">{trend_rows}</div>
        </div>
    """

    content = f"""
        <header>
            <h1>🏅 Delivery Excellence</h1>
            <div class="subtitle">How well we deliver — flow, rework &amp; predictability • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('delivery-excellence')}
        <div class="content">
            <div class="intro-banner">
                <p>This page measures the <strong>delivery system</strong>, not project status: how efficiently
                work flows, how often it bounces back, and how reliably the team hits its commitments.
                Flow &amp; rework are team-wide over the last {window} days, split BE/FE by assignee;
                predictability covers recent closed sprints, split by sprint track.</p>
            </div>
            {body}
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """
    html_page = render_html(
        title="Delivery Excellence",
        content=content,
        body_class=_PAGE_THEME["delivery-excellence"],
    )
    _atomic_write(output_path, html_page)
    print(f"✅ Delivery Excellence dashboard generated: {output_path}")


# ---------------------------------------------------------------------------
# Stakeholders page (driven by config/stakeholders.yaml)
# ---------------------------------------------------------------------------

_STAKEHOLDER_COLORS = {
    'indigo', 'teal', 'orange', 'purple', 'blue', 'pink', 'green', 'red', 'slate',
}


def _stakeholder_initials(name: str) -> str:
    """Two-letter avatar from a person's name. 'Mary Jo Watson' → 'MW'."""
    parts = [p for p in name.split() if p]
    if not parts:
        return '??'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _render_stakeholder_card(person: dict) -> str:
    """One name+title+role-tag chip card. Avatar color comes from the YAML."""
    name = html.escape(person.get('name', ''))
    title = html.escape(person.get('title', ''))
    role_tag = html.escape(person.get('role_tag', '')) if person.get('role_tag') else ''
    color_raw = (person.get('color') or 'indigo').strip()
    # Whitelisted palette names get a CSS class; anything else is treated as a
    # raw CSS color via inline style — keeps the YAML expressive without
    # opening an XSS vector.
    if color_raw in _STAKEHOLDER_COLORS:
        avatar_class = f'stk-avatar {color_raw}'
        avatar_style = ''
    else:
        avatar_class = 'stk-avatar'
        avatar_style = f' style="background: {html.escape(color_raw)};"'

    initials = _stakeholder_initials(person.get('name', ''))
    tag_html = f'<span class="stk-tag">{role_tag}</span>' if role_tag else ''
    return (
        f'<div class="stk-card">'
        f'<div class="{avatar_class}"{avatar_style}>{initials}</div>'
        f'<div class="stk-info">'
        f'<div class="stk-name">{name}</div>'
        f'<div class="stk-title">{title}</div>'
        f'{tag_html}'
        f'</div>'
        f'</div>'
    )


def _render_stakeholder_group(group: dict) -> str:
    """One row in the matrix — label chip on the left, members on the right.

    Supports both flat groups (members:) and nested (subgroups:).
    """
    label = html.escape(group.get('label', ''))

    if group.get('subgroups'):
        rows = []
        for sub in group['subgroups']:
            sub_label = html.escape(sub.get('label', ''))
            cards = ''.join(_render_stakeholder_card(p) for p in sub.get('members', []))
            rows.append(
                f'<div class="stk-subgroup">'
                f'<div class="stk-subgroup-label">{sub_label}</div>'
                f'<div class="stk-members">{cards}</div>'
                f'</div>'
            )
        body = f'<div class="stk-subgroups">{"".join(rows)}</div>'
    else:
        cards = ''.join(_render_stakeholder_card(p) for p in group.get('members', []))
        body = f'<div class="stk-members">{cards}</div>'

    return (
        f'<div class="stk-group">'
        f'<div class="stk-group-label">{label}</div>'
        f'{body}'
        f'</div>'
    )


def generate_stakeholders_html(config: dict, output_path: Path):
    """Render the Stakeholders matrix from config/stakeholders.yaml.

    The data lives in a separate YAML so the assistant can edit groups +
    people in conversation ("add Foo to OSB", "move Bar from DSEA to Tech
    Compliance") without touching code. Missing file → empty state.
    """
    import yaml as _yaml

    repo_root = Path(config['database']['path']).parent.parent
    stakeholders_path = repo_root / 'config' / 'stakeholders.yaml'

    groups_html = ''
    last_updated_label = ''
    if stakeholders_path.exists():
        with open(stakeholders_path) as f:
            data = _yaml.safe_load(f) or {}
        groups = data.get('groups', []) or []
        groups_html = ''.join(_render_stakeholder_group(g) for g in groups)
        try:
            mtime = datetime.fromtimestamp(stakeholders_path.stat().st_mtime)
            last_updated_label = mtime.strftime('%B %d, %Y at %H:%M')
        except OSError:
            pass
    else:
        groups_html = (
            '<div class="empty-state">'
            '<div class="icon">👥</div>'
            '<div>No stakeholders configured. Edit <code>config/stakeholders.yaml</code> to populate this page.</div>'
            '</div>'
        )

    content = f"""
        <header>
            <h1>👥 Stakeholders</h1>
            <div class="subtitle">{project_label()} · Source: config/stakeholders.yaml{(" · Last edited " + last_updated_label) if last_updated_label else ""}</div>
        </header>
{generate_nav_menu('stakeholders')}
        <div class="content stakeholders-page">
            <div class="stakeholders-card">
                <div class="stk-header">
                    <span>👥</span>
                    <span>Stakeholders</span>
                </div>
                {groups_html}
            </div>
            <footer>
                Generated by Engineering Management Dashboard · {datetime.now().strftime('%B %d, %Y at %H:%M')}
            </footer>
        </div>
    """

    page = render_html(
        title=f"Stakeholders — {project_label()}",
        content=content,
        body_class=_PAGE_THEME["stakeholders"],
    )
    _atomic_write(output_path, page)
    print(f"✅ Stakeholders dashboard generated: {output_path}")


# ---------------------------------------------------------------------------
# Dependencies page (driven by config/dependencies.yaml)
# ---------------------------------------------------------------------------

_DEP_STATUS_CLASS = {
    'to do':                'todo',
    'open':                 'open',
    'product discovery':    'todo',
    'ready for prioritization review': 'todo',
    'in progress':          'inprogress',
    'in development':       'inprogress',
    'in review':            'inprogress',
    'in code review':       'inprogress',
    'engineering unpacking':'inprogress',
    'ready for testing':    'inprogress',
    'testing in progress':  'inprogress',
    'released to test':     'inprogress',
    'blocked':              'blocked',
    'done':                 'done',
    'closed':               'closed',
    'resolved':             'done',
}


def _dep_status_class(status: str) -> str:
    if not status:
        return 'unknown'
    return _DEP_STATUS_CLASS.get(status.strip().lower(), 'unknown')


def _dep_lookup_from_db(db_path: str, key: str) -> dict:
    """Pull live ticket details for an FNTSY key from the local DB.

    Returns {} if not found. Refresh runs after every collector cycle
    (every 15 min), so this stays close to live.
    """
    if not key:
        return {}
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        # Match exact key first; epics get a `_s<sprint_id>` suffix when
        # they appear in multiple sprints, so fall back to LIKE for those.
        cursor.execute("""
            SELECT ticket_key, summary, status, assignee_display_name, ticket_url, issue_type
            FROM tickets
            WHERE ticket_key = ?
            ORDER BY last_updated_at DESC LIMIT 1
        """, (key,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
                SELECT ticket_key, summary, status, assignee_display_name, ticket_url, issue_type
                FROM tickets
                WHERE ticket_key LIKE ?
                ORDER BY last_updated_at DESC LIMIT 1
            """, (key + '_s%',))
            row = cursor.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _jira_cloud_base() -> str:
    """https://<tenant> root from jira.cloud_id. Cached for the regen pass;
    falls back to the betfanatics tenant when config is unavailable."""
    cached = getattr(_jira_cloud_base, '_cache', None)
    if cached is not None:
        return cached
    try:
        cloud_id = (load_config().get('jira') or {}).get('cloud_id') or "betfanatics.atlassian.net"
    except Exception:
        cloud_id = "betfanatics.atlassian.net"
    base = f"https://{cloud_id}"
    _jira_cloud_base._cache = base
    return base


def _structure_board_url() -> str:
    """Full URL to the Jira Structure board (jira.structure_board_url). Surfaced
    under 'Useful Links' on the Project snapshot page; empty string hides it."""
    try:
        return (load_config().get('jira') or {}).get('structure_board_url') or ''
    except Exception:
        return ''


def _dep_default_url(key: str) -> str:
    """Best-effort Jira URL for any project key — the cloud_id lives in config
    but the browse/ format is uniform, so we just prefix the configured tenant."""
    if not key:
        return ''
    return f"{_jira_cloud_base()}/browse/{key}"


def _normalize_rag(value) -> str:
    """Map free-text rag values to one of: red, amber, green, or '' (none)."""
    if not value:
        return ''
    v = str(value).strip().lower()
    if v in ('red', 'r'):
        return 'red'
    if v in ('amber', 'yellow', 'y', 'a'):
        return 'amber'
    if v in ('green', 'g'):
        return 'green'
    return ''


def _normalize_weekly_history(raw) -> list[dict]:
    """Coerce `weekly_status` YAML into a list of {date, text} dicts.

    Accepts:
      - missing/None → []
      - a string → single entry with no date
      - list of strings → entries with no date
      - list of dicts {date, text} (or {date, status}) → kept as-is
    Newest entries first. Stable string-sort on ISO dates handles ordering;
    entries without a date sort to the end.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        return [{'date': '', 'text': raw}]
    if not isinstance(raw, list):
        return []
    today_iso = datetime.now().strftime('%Y-%m-%d')
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({'date': '', 'text': item})
        elif isinstance(item, dict):
            text = item.get('text') or item.get('status') or item.get('note') or ''
            date = item.get('date') or ''
            date_str = str(date)
            if date_str and date_str > today_iso:
                date_str = today_iso
            if text or date_str:
                out.append({'date': date_str, 'text': str(text)})
    # Newest first — empty dates float to the end.
    out.sort(key=lambda e: (e.get('date') or ''), reverse=True)
    out.sort(key=lambda e: 0 if e.get('date') else 1)
    return out


# Field that holds the human-curated weekly status updates on FEAT/CAT/FNTSY
# tickets. Each paragraph in the ADF doc starts with a `date` node (Unix ms
# timestamp) followed by the entry body — see FEAT-8216 for the canonical
# example. Hit the issue REST endpoint with this single field for cheap
# fetches.
_DEP_WEEKLY_STATUS_FIELD = 'customfield_10120'
# RAG status field — single-select with options "Red" / "Amber" / "Green".
# Sample payload: {"value": "Amber", "id": "10240", ...}.
_DEP_RAG_FIELD = 'customfield_10155'


def _jira_session():
    """Return a (session, base_url) tuple authenticated for Atlassian Cloud,
    or None if creds aren't available — caller should fall back to YAML.

    Loads JIRA_EMAIL/JIRA_API_TOKEN from env, with config/.env as a backup
    so this works in local regen too. Cached on the function object to avoid
    rebuilding for every dependency on a single regen pass.
    """
    if hasattr(_jira_session, '_cache'):
        return _jira_session._cache
    try:
        import requests
        repo_root = Path(__file__).resolve().parent.parent
        env_file = repo_root / 'config' / '.env'
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())
        email = os.environ.get('JIRA_EMAIL')
        token = os.environ.get('JIRA_API_TOKEN')
        if not email or not token:
            _jira_session._cache = None
            return None
        sess = requests.Session()
        sess.auth = (email, token)
        sess.headers.update({'Accept': 'application/json'})
        base = f"{_jira_cloud_base()}/rest/api/3"
        _jira_session._cache = (sess, base)
        return _jira_session._cache
    except Exception:
        _jira_session._cache = None
        return None


def _parse_weekly_status_adf(doc: dict) -> list[dict]:
    """Parse the customfield_10120 ADF doc into [{date, text}] entries.

    Each top-level paragraph has the shape:
      [{type: "date", attrs: {timestamp: "<unix_ms>"}}, {type: "text", text: " body..."}]
    Some paragraphs have multiple text nodes; we concatenate them. Paragraphs
    with no date node still surface as entries with date='' so nothing is
    silently dropped. Newest first.

    Future-dated entries (typo'd timestamps that land after today — Jira's
    date picker makes it easy to mis-tap a month) are clamped to today so the
    dashboard never advertises a status update from the future. The QA review
    agent flags these in parallel so a human can fix the source ticket.
    """
    if not isinstance(doc, dict):
        return []
    today_iso = datetime.now().strftime('%Y-%m-%d')
    out = []
    for para in doc.get('content') or []:
        if (para.get('type') or '') != 'paragraph':
            continue
        nodes = para.get('content') or []
        date_str = ''
        text_parts = []
        for node in nodes:
            ntype = node.get('type')
            if ntype == 'date':
                ts = (node.get('attrs') or {}).get('timestamp')
                if ts:
                    try:
                        ms = int(ts)
                        date_str = datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%d')
                    except (TypeError, ValueError):
                        pass
            elif ntype == 'text':
                t = node.get('text') or ''
                if t:
                    text_parts.append(t)
        text = ''.join(text_parts).strip()
        if date_str and date_str > today_iso:
            date_str = today_iso
        if text or date_str:
            out.append({'date': date_str, 'text': text})
    # Already in newest-first order from Jira, but sort to be safe — empty
    # dates float to the end so they don't shoulder real-dated entries aside.
    out.sort(key=lambda e: (e.get('date') or ''), reverse=True)
    out.sort(key=lambda e: 0 if e.get('date') else 1)
    return out


def _fetch_dep_jira_fields(key: str) -> dict | None:
    """Fetch the dep-card extras (weekly status + RAG) for `key` in one call.

    Returns a dict with `weekly_status: list[{date,text}]` and `rag: str` (one
    of red/amber/green or '' when unset). Returns None when the request can't
    run (no creds / network / 404) so callers can fall back to YAML.
    """
    sess_info = _jira_session()
    if not sess_info:
        return None
    session, base = sess_info
    url = f"{base}/issue/{key}"
    fields = ','.join([_DEP_WEEKLY_STATUS_FIELD, _DEP_RAG_FIELD])
    try:
        resp = session.get(url, params={'fields': fields}, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    fmap = data.get('fields') or {}
    weekly_doc = fmap.get(_DEP_WEEKLY_STATUS_FIELD)
    weekly = _parse_weekly_status_adf(weekly_doc) if weekly_doc else []
    rag_field = fmap.get(_DEP_RAG_FIELD)
    rag_value = ''
    if isinstance(rag_field, dict):
        rag_value = _normalize_rag(rag_field.get('value'))
    elif isinstance(rag_field, str):
        rag_value = _normalize_rag(rag_field)
    return {'weekly_status': weekly, 'rag': rag_value}


def _render_dependency_card(dep: dict, db_path: str) -> str:
    """One dependency card. FNTSY tickets pull live; others use YAML fields."""
    key = (dep.get('key') or '').strip()
    if not key:
        return ''

    # YAML overrides win when present; otherwise fill from DB (FNTSY) or empty.
    db_row = {}
    if key.startswith('FNTSY-'):
        db_row = _dep_lookup_from_db(db_path, key)

    summary = dep.get('summary') or db_row.get('summary') or '(no summary)'
    owner = dep.get('owner') or db_row.get('assignee_display_name') or 'Unassigned'
    status = dep.get('status') or db_row.get('status') or 'Unknown'
    url = dep.get('url') or db_row.get('ticket_url') or _dep_default_url(key)
    team = dep.get('team') or ''
    notes = dep.get('notes') or ''
    # Live-fetch RAG + weekly status from Jira in a single request. On any
    # failure (no creds, network blip, 404) we fall back to YAML so the card
    # still renders something useful instead of blanking.
    live = _fetch_dep_jira_fields(key)
    if live is not None:
        weekly_entries = live['weekly_status']
        rag = live['rag'] or _normalize_rag(dep.get('rag'))
    else:
        weekly_entries = _normalize_weekly_history(dep.get('weekly_status'))
        rag = _normalize_rag(dep.get('rag'))

    status_cls = _dep_status_class(status)
    team_html = f'<span class="dep-team">{html.escape(team)}</span>' if team else ''
    rag_html = (
        f'<span class="dep-rag {rag}">{rag.upper()}</span>' if rag else ''
    )
    safe_notes = html.escape(notes)
    safe_key = html.escape(key)

    # Weekly status block: latest entry on top, the rest in a collapsible.
    if weekly_entries:
        latest = weekly_entries[0]
        latest_date = html.escape(latest.get('date') or '')
        latest_text = html.escape(latest.get('text') or '')
        latest_html = (
            f'<div class="dep-weekly-date">{latest_date}</div>' if latest_date else ''
        ) + f'<div class="dep-weekly-latest">{latest_text}</div>'
        if len(weekly_entries) > 1:
            rest = weekly_entries[1:]
            entries_html = ''.join(
                f'<div class="dep-weekly-entry">'
                + (f'<div class="dep-weekly-date">{html.escape(e.get("date") or "")}</div>' if e.get('date') else '')
                + html.escape(e.get('text') or '')
                + '</div>'
                for e in rest
            )
            history_html = (
                f'<details class="dep-weekly-history">'
                f'<summary>{len(rest)} earlier update{"s" if len(rest) != 1 else ""}</summary>'
                f'<div class="dep-weekly-history-list">{entries_html}</div>'
                f'</details>'
            )
        else:
            history_html = ''
    else:
        latest_html = '<div class="dep-weekly-empty">No weekly updates yet.</div>'
        history_html = ''

    weekly_block = (
        '<div class="dep-weekly">'
        '<div class="dep-weekly-label">Weekly Status</div>'
        f'{latest_html}'
        f'{history_html}'
        '</div>'
    )

    return f"""
        <div class="dep-card" data-key="{safe_key}">
            <div class="dep-head">
                <a class="dep-key" href="{html.escape(url)}" target="_blank" rel="noopener">{safe_key}</a>
                {team_html}
                {rag_html}
                <span class="dep-status {status_cls}">{html.escape(status)}</span>
            </div>
            <div class="dep-summary">{html.escape(summary)}</div>
            <div class="dep-meta">
                <span><strong>Owner:</strong>{html.escape(owner)}</span>
            </div>
            {weekly_block}
            <label class="dep-notes-label" for="dep-notes-{safe_key}">Status notes</label>
            <textarea id="dep-notes-{safe_key}" class="dep-notes" rows="4"
                placeholder="What's the latest? (free-text — no history kept)">{safe_notes}</textarea>
            <div class="dep-actions">
                <span class="dep-save-msg"></span>
                <button type="button" class="dep-save-btn" onclick="saveDependencyNotes('{safe_key}', this)">Save</button>
            </div>
        </div>
    """


def generate_dependencies_html(config: dict, output_path: Path):
    """Render the Dependencies page from config/dependencies.yaml.

    FNTSY tickets pull live summary/status/owner from the local tickets
    table; non-FNTSY tickets (CAT, FEAT, etc.) use whatever the YAML provides.
    The notes textarea writes back via /api/dependency-notes.
    """
    import yaml as _yaml

    db_path = config['database']['path']
    repo_root = Path(db_path).parent.parent
    deps_path = repo_root / 'config' / 'dependencies.yaml'

    deps = []
    last_updated_label = ''
    if deps_path.exists():
        with open(deps_path) as f:
            data = _yaml.safe_load(f) or {}
        deps = data.get('dependencies', []) or []
        try:
            mtime = datetime.fromtimestamp(deps_path.stat().st_mtime)
            last_updated_label = mtime.strftime('%B %d, %Y at %H:%M')
        except OSError:
            pass

    if deps:
        # Group by team while preserving each team's first-appearance order
        # in the YAML — that lets you reorder teams just by reordering the
        # first matching entry in the file. Tickets without a `team` field
        # fall into a final "Untagged" bucket so they don't disappear.
        groups: dict[str, list[dict]] = {}
        team_order: list[str] = []
        for dep in deps:
            team = (dep.get('team') or '').strip() or 'Untagged'
            if team not in groups:
                groups[team] = []
                team_order.append(team)
            groups[team].append(dep)

        # Sort each team's entries by ticket key — project prefix first
        # (alphabetical), then the numeric suffix as an int so FEAT-1000
        # doesn't sort before FEAT-99. Unparseable keys fall to the end.
        def _sort_key(dep: dict):
            key = (dep.get('key') or '').strip()
            if '-' in key:
                project, _, num = key.rpartition('-')
                try:
                    return (0, project, int(num))
                except ValueError:
                    pass
            return (1, key, 0)

        section_parts = []
        for team in team_order:
            entries = sorted(groups[team], key=_sort_key)
            cards_html = ''.join(_render_dependency_card(d, db_path) for d in entries)
            section_parts.append(
                f'<div class="dep-team-section">'
                f'<div class="dep-team-header">'
                f'<span class="dep-team-name">{html.escape(team)}</span>'
                f'<span class="dep-team-count">{len(entries)}</span>'
                f'</div>'
                f'<div class="dep-grid">{cards_html}</div>'
                f'</div>'
            )
        body = ''.join(section_parts)
    else:
        body = (
            '<div class="dep-empty">'
            '<div style="font-size:32px;margin-bottom:10px;">🔗</div>'
            '<div>No dependencies tracked yet.</div>'
            '<div style="margin-top:8px;font-size:12px;">'
            'Tell the assistant <em>"add &lt;TICKET-KEY&gt; to dependencies"</em> '
            'or edit <code>config/dependencies.yaml</code> directly.'
            '</div>'
            '</div>'
        )

    content = f"""
        <header>
            <h1>🔗 Dependencies</h1>
            <div class="subtitle">{project_label()} · {len(deps)} tracked · Source: config/dependencies.yaml{(" · Last edited " + last_updated_label) if last_updated_label else ""}</div>
        </header>
{generate_nav_menu('dependencies')}
        <div class="content">
            <div class="intro-banner">
                <p>Cross-team / cross-project tickets the Fantasy team is waiting on. Status notes are free-text — no history kept; the latest text wins. Save writes back to <code>config/dependencies.yaml</code>.</p>
            </div>
            {body}
            <footer>
                Generated by Engineering Management Dashboard · {datetime.now().strftime('%B %d, %Y at %H:%M')}
            </footer>
        </div>
    """

    page = render_html(
        title=f"Dependencies — {project_label()}",
        content=content,
        body_class=_PAGE_THEME["dependencies"],
    )
    _atomic_write(output_path, page)
    print(f"✅ Dependencies dashboard generated: {output_path}")


# ---------------------------------------------------------------------------
# Competency modal (rendered on the Team Members page)
# ---------------------------------------------------------------------------

def _render_competency_modal():
    """Modal + embedded data for the "View Competencies" button.

    Data lives in a <script type="application/json"> block; the JS in
    dashboard.js wires button clicks to open/close and populate the modal.
    """
    payload = get_competency_payload()
    payload_json = json.dumps(payload)
    return f"""
    <script id="competency-data" type="application/json">{payload_json}</script>
    <div id="competency-modal" class="competency-modal" hidden>
        <div class="competency-backdrop" data-close-competency-modal></div>
        <div class="competency-dialog" role="dialog" aria-modal="true" aria-labelledby="competency-modal-title">
            <div class="competency-dialog-header">
                <div>
                    <div id="competency-modal-title" class="competency-modal-title">Competencies</div>
                    <div id="competency-modal-subtitle" class="competency-modal-subtitle"></div>
                </div>
                <button type="button" class="competency-close" data-close-competency-modal aria-label="Close">×</button>
            </div>
            <div id="competency-modal-body" class="competency-dialog-body"></div>
            <div class="competency-dialog-footer">
                <span class="competency-note">Competency definitions are cumulative — a given level includes all preceding levels.</span>
                <button type="button" class="flat-btn" data-close-competency-modal>Close</button>
            </div>
        </div>
    </div>
    """


def _render_member_edit_modal():
    """Modal + embedded level list for the "Edit" button on member cards.

    Writes to POST /api/member on the dashboard server, which rewrites
    config/team_config.yaml and regenerates the HTML.
    """
    level_options = ['']  # blank = clear level
    level_options.extend(sorted(TITLE_TO_LEVEL.keys(), key=lambda t: TITLE_TO_LEVEL[t]))
    options_html = ''.join(
        f'<option value="{html.escape(lvl)}">{html.escape(lvl) if lvl else "— no level —"}</option>'
        for lvl in level_options
    )
    return f"""
    <div id="member-edit-modal" class="competency-modal" hidden>
        <div class="competency-backdrop" data-close-member-edit-modal></div>
        <div class="competency-dialog member-edit-dialog" role="dialog" aria-modal="true" aria-labelledby="member-edit-modal-title">
            <div class="competency-dialog-header">
                <div>
                    <div id="member-edit-modal-title" class="competency-modal-title">Edit Member</div>
                    <div id="member-edit-modal-subtitle" class="competency-modal-subtitle"></div>
                </div>
                <button type="button" class="competency-close" data-close-member-edit-modal aria-label="Close">×</button>
            </div>
            <form id="member-edit-form" class="competency-dialog-body member-edit-body" autocomplete="off">
                <input type="hidden" name="original_name" id="member-edit-original-name">
                <label class="member-edit-label" for="member-edit-github">GitHub username</label>
                <input type="text" id="member-edit-github" name="github_username" placeholder="e.g. anushri-patel">

                <label class="member-edit-label" for="member-edit-jira">Jira account ID</label>
                <input type="text" id="member-edit-jira" name="jira_account_id" placeholder="e.g. 712020:...">

                <label class="member-edit-label" for="member-edit-level">Engineering level</label>
                <select id="member-edit-level" name="level">
                    {options_html}
                </select>

                <div id="member-edit-error" class="member-edit-error" hidden></div>
            </form>
            <div class="competency-dialog-footer">
                <span class="competency-note">Saves to <code>config/team_config.yaml</code> and regenerates the dashboard.</span>
                <div>
                    <button type="button" class="flat-btn danger" data-close-member-edit-modal>Cancel</button>
                    <button type="button" class="flat-btn success" id="member-edit-save">Save</button>
                </div>
            </div>
        </div>
    </div>
    """


# ---------------------------------------------------------------------------
# Snapshot rendering helpers for Project: Fantasy
# ---------------------------------------------------------------------------

def _render_project_fantasy_snapshot():
    """Render the data-driven sections of the Project: Fantasy page.

    Reads data/project_fantasy.json (produced by scripts/sync_project_fantasy.py).
    If missing, shows an empty state explaining how to populate it.
    """
    import json as _json
    snapshot_path = Path(__file__).parent.parent / "data" / "project_fantasy.json"
    if not snapshot_path.exists():
        return """
            <div class="section">
                <div class="intro-banner" style="border-left-color: var(--warning);">
                    <p><strong>No project snapshot yet.</strong> Run the snapshot agent to pull Jira + Confluence data:</p>
                    <p><code>python3 scripts/sync_project_fantasy.py</code></p>
                </div>
            </div>
        """

    try:
        snap = _json.loads(snapshot_path.read_text())
    except Exception as e:
        return f"""
            <div class="section">
                <div class="intro-banner" style="border-left-color: var(--danger);">
                    <p><strong>Snapshot file is malformed:</strong> {e}</p>
                </div>
            </div>
        """

    generated_at = snap.get('generated_at', '')
    staleness_badge = ''
    try:
        gen_dt = parse_iso_tz(generated_at)
        gen_label = gen_dt.astimezone().strftime('%B %d, %Y at %H:%M')
        from datetime import timezone as _tz
        age_hours = (datetime.now(_tz.utc) - gen_dt.astimezone(_tz.utc)).total_seconds() / 3600
        if age_hours >= 24:
            staleness_badge = (
                f' <span style="background: var(--danger-bg); color: var(--danger-text); '
                f'padding: 2px 8px; border-radius: 4px; font-weight: 600; margin-left: 8px;" '
                f'title="Snapshot data is {age_hours:.0f} hours old">⚠ stale ({age_hours:.0f}h)</span>'
            )
        elif age_hours >= 6:
            staleness_badge = (
                f' <span style="background: var(--warning-bg); color: var(--warning-text); '
                f'padding: 2px 8px; border-radius: 4px; font-weight: 600; margin-left: 8px;" '
                f'title="Snapshot is getting old — refresh recommended">⏳ {age_hours:.0f}h old</span>'
            )
    except Exception:
        gen_label = generated_at

    parts = []

    # ---- Vision / initiative block ----------------------------------------
    init = snap.get('initiative', {}) or {}
    raw_description = init.get('description')
    # Jira v3 returns descriptions as Atlassian Document Format (ADF) — a JSON
    # doc tree. Older snapshots may have a plain string. Normalize both to text.
    description = _adf_to_text(raw_description) if raw_description else ''
    description_html = _description_to_html(description, max_chars=1200)
    parts.append(f"""
            <div class="section">
                <h2 class="section-title">🎯 Vision</h2>
                <div class="intro-banner">
                    <p style="font-weight: 600; margin-bottom: 12px; color: var(--text-primary);">
                        <a href="{init.get('url', '#')}" target="_blank" style="color: var(--accent-text); text-decoration: none;">{init.get('key', '')}</a>
                        — {init.get('summary', '')}
                        <span class="badge" style="background: var(--bg-hover); color: var(--text-secondary); margin-left: 8px;">{init.get('status', '')}</span>
                    </p>
                    <div class="vision-body">{description_html}</div>
                    <p style="margin-top: 12px; font-size: 11px; color: var(--text-faint);">Snapshot generated {gen_label}{staleness_badge}</p>
                </div>
            </div>
    """)

    # ---- Summary counts with status breakdown bars ------------------------
    summary = snap.get('summary', {}) or {}

    def _status_bar(counts, total):
        """Render a stacked bar showing done / in_flight / discovery / dropped."""
        if total <= 0:
            return '<div class="status-bar"></div>'
        order = [
            ('done', 'Done', 'var(--success)'),
            ('in_flight', 'In Flight', 'var(--info)'),
            ('discovery', 'Discovery', 'var(--text-muted)'),
            ('dropped', 'Dropped', 'var(--danger)'),
            ('other', 'Other', 'var(--text-faint)'),
        ]
        segments = []
        for bucket, label, color in order:
            c = counts.get(bucket, 0)
            if c <= 0:
                continue
            pct = (c / total) * 100
            segments.append(
                f'<div class="status-bar-seg" style="width: {pct:.1f}%; background: {color};" '
                f'title="{label}: {c} ({pct:.0f}%)"></div>'
            )
        return f'<div class="status-bar">{"".join(segments)}</div>'

    features_total = summary.get('features_total', 0)
    epics_total = summary.get('epics_total', 0)
    stories_total = summary.get('stories_total', 0)
    f_buckets = summary.get('features_by_bucket', {})
    e_buckets = summary.get('epics_by_bucket', {})
    s_buckets = summary.get('stories_by_bucket', {})
    at_risk_count = summary.get('at_risk_count', 0)

    parts.append(f"""
            <div class="section">
                <h2 class="section-title">📊 Work Rollup</h2>
                <div class="metrics-grid">
                    <div class="metric-card info">
                        <div class="metric-label">Features</div>
                        <div class="metric-value">{features_total}</div>
                        <div class="metric-subtext">{f_buckets.get('done', 0)} done · {f_buckets.get('in_flight', 0)} in flight · {f_buckets.get('discovery', 0)} discovery · {f_buckets.get('dropped', 0)} dropped</div>
                        {_status_bar(f_buckets, features_total)}
                    </div>
                    <div class="metric-card info">
                        <div class="metric-label">Epics</div>
                        <div class="metric-value">{epics_total}</div>
                        <div class="metric-subtext">{e_buckets.get('done', 0)} done · {e_buckets.get('in_flight', 0)} in flight · {e_buckets.get('discovery', 0)} discovery · {e_buckets.get('dropped', 0)} dropped</div>
                        {_status_bar(e_buckets, epics_total)}
                    </div>
                    <div class="metric-card info">
                        <div class="metric-label">Stories</div>
                        <div class="metric-value">{stories_total}</div>
                        <div class="metric-subtext">{s_buckets.get('done', 0)} done · {s_buckets.get('in_flight', 0)} in flight · {s_buckets.get('discovery', 0)} discovery · {s_buckets.get('dropped', 0)} dropped</div>
                        {_status_bar(s_buckets, stories_total)}
                    </div>
                    <div class="metric-card {'danger' if at_risk_count > 0 else 'success'}">
                        <div class="metric-label">Features At Risk</div>
                        <div class="metric-value">{at_risk_count}</div>
                        <div class="metric-subtext">{"flagged in snapshot" if at_risk_count else "nothing flagged"}</div>
                    </div>
                </div>
            </div>
    """)

    # ---- Useful Links -----------------------------------------------------
    # Hand-curated tools/environments that aren't in Confluence. Add new
    # entries here as `(label, url, description)`; description is optional.
    useful_links = [
        ('Playmaker',
         'https://playmaker-internal.dev1.fanatics.bet/fantasy/contests',
         'Internal contest admin (dev1)'),
    ]
    structure_board_url = _structure_board_url()
    if structure_board_url:
        useful_links.append(
            ('Structure Board', structure_board_url, 'Jira Structure board'))
    parts.append("""
            <div class="section">
                <h2 class="section-title">🔗 Useful Links</h2>
                <ul class="useful-links">
    """)
    for label, url, desc in useful_links:
        desc_html = f' <span style="color: var(--text-muted); font-size: 12px;">— {html.escape(desc)}</span>' if desc else ''
        parts.append(
            f'<li><a href="{html.escape(url)}" target="_blank">{html.escape(label)}</a>{desc_html}</li>'
        )
    parts.append("""
                </ul>
            </div>
    """)

    # ---- Confluence doc index ---------------------------------------------
    docs = snap.get('confluence_docs', []) or []
    if docs:
        space_url = snap.get('confluence_space_url', '#')
        parts.append(f"""
            <div class="section">
                <h2 class="section-title">📚 Confluence Docs</h2>
                <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 16px;">
                    Curated links to working docs in the
                    <a href="{space_url}" target="_blank" style="color: var(--accent-text);">DFS space</a>.
                </p>
                <div class="confluence-grid">
        """)
        for group in docs:
            parts.append(f"""
                    <div class="confluence-group">
                        <div class="confluence-group-title">{group['folder']}</div>
                        <ul class="confluence-links">
            """)
            for doc in group.get('docs', []):
                parts.append(
                    f'<li><a href="{doc["url"]}" target="_blank">{doc["title"]}</a></li>'
                )
            parts.append("""
                        </ul>
                    </div>
            """)
        parts.append("""
                </div>
            </div>
        """)

    return ''.join(parts)


def _load_feature_work_status() -> dict:
    """Return {feature_key: {'be_done': bool, 'fe_done': bool}} from
    config/feature_work_status.yaml. Missing file or unreadable YAML →
    empty dict (every feature renders as un-checked, which matches the
    schema's documented default)."""
    import yaml as _yaml
    path = Path(__file__).resolve().parent.parent / 'config' / 'feature_work_status.yaml'
    if not path.exists():
        return {}
    try:
        data = _yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    out = {}
    for entry in (data.get('features') or []):
        if not isinstance(entry, dict):
            continue
        key = (entry.get('key') or '').strip()
        if not key:
            continue
        out[key] = {
            'be_done': bool(entry.get('be_done')),
            'fe_done': bool(entry.get('fe_done')),
        }
    return out


def _feature_toggle_cell(key: str, kind: str, checked: bool) -> str:
    """Render a sortable BE/FE-done checkbox cell.

    `kind` is 'be' or 'fe'. The cell sets data-sort to '1' when checked, '0'
    otherwise so the existing sortTable(..., 'number') path works. The
    checkbox itself wires onchange to saveFeatureWorkStatus, which POSTs to
    /api/feature-work-status. On GitHub Pages the JS disables it instead.
    """
    state = '1' if checked else '0'
    checked_attr = ' checked' if checked else ''
    safe_key = html.escape(key)
    return (
        f'<td data-sort="{state}" class="feature-toggle-cell">'
        f'<input type="checkbox" class="feature-toggle" '
        f'data-key="{safe_key}" data-kind="{kind}"{checked_attr} '
        f'onchange="saveFeatureWorkStatus(this)" '
        f'aria-label="{kind.upper()} work complete for {safe_key}">'
        f'</td>'
    )


def _render_feature_lists():
    """Render the two feature-list sections (by Milestone + by Launch).

    Lives on its own page (features.html) so the Project: Fantasy page can
    stay focused on vision + rollup. Reads the same project_fantasy.json
    snapshot. Empty state mirrors _render_project_fantasy_snapshot's.
    """
    import json as _json
    initiative_key = _jira_initiative_key()
    snapshot_path = Path(__file__).parent.parent / "data" / "project_fantasy.json"
    if not snapshot_path.exists():
        return """
            <div class="section">
                <div class="intro-banner" style="border-left-color: var(--warning);">
                    <p><strong>No project snapshot yet.</strong> Run the snapshot agent to populate features:</p>
                    <p><code>python3 scripts/sync_project_fantasy.py</code></p>
                </div>
            </div>
        """

    try:
        snap = _json.loads(snapshot_path.read_text())
    except Exception as e:
        return f"""
            <div class="section">
                <div class="intro-banner" style="border-left-color: var(--danger);">
                    <p><strong>Snapshot file is malformed:</strong> {e}</p>
                </div>
            </div>
        """

    # Per-feature BE/FE work-complete checkbox state. Defaults to all-false
    # for any feature not listed in the YAML.
    work_status = _load_feature_work_status()

    # Open-epic count per feature. "Open" = an epic that still has work left,
    # i.e. its status bucket is neither 'done' nor 'dropped' (so discovery +
    # in-flight + any other live state count). Keyed by the epic's parent
    # feature key. Reads the same snapshot's epics list — no schema change.
    open_epics_by_feature: dict[str, int] = {}
    for e in snap.get('epics', []) or []:
        if e.get('status_bucket') in ('done', 'dropped'):
            continue
        parent_key = e.get('parent')
        if parent_key:
            open_epics_by_feature[parent_key] = open_epics_by_feature.get(parent_key, 0) + 1

    parts = []

    def _status_bar(counts, total):
        """Render a stacked bar showing done / in_flight / discovery / dropped."""
        if total <= 0:
            return '<div class="status-bar"></div>'
        order = [
            ('done', 'Done', 'var(--success)'),
            ('in_flight', 'In Flight', 'var(--info)'),
            ('discovery', 'Discovery', 'var(--text-muted)'),
            ('dropped', 'Dropped', 'var(--danger)'),
            ('other', 'Other', 'var(--text-faint)'),
        ]
        segments = []
        for bucket, label, color in order:
            c = counts.get(bucket, 0)
            if c <= 0:
                continue
            pct = (c / total) * 100
            segments.append(
                f'<div class="status-bar-seg" style="width: {pct:.1f}%; background: {color};" '
                f'title="{label}: {c} ({pct:.0f}%)"></div>'
            )
        return f'<div class="status-bar">{"".join(segments)}</div>'

    # ---- Feature roster grouped by Launch (collapsible) -------------------
    # Features carry a Jira "Launch" custom field with values Alpha / Beta /
    # Public Launch / Post Launch (see customfield_10441). We group features
    # under those phases so the page reads as a launch-by-launch plan.
    # Anything missing the value lands under "Unassigned".
    features = snap.get('features', []) or []
    if features:
        # Canonical phase order matches the launch sequence on the timeline above.
        launch_order = ['Alpha', 'Beta', 'Public Launch', 'Post Launch']
        groups: dict[str, list[dict]] = {name: [] for name in launch_order}
        groups['Unassigned'] = []
        for f in features:
            phase = (f.get('launch') or '').strip()
            if phase in groups:
                groups[phase].append(f)
            elif phase:
                # Unexpected option value — keep it visible rather than hiding it.
                groups.setdefault(phase, []).append(f)
            else:
                groups['Unassigned'].append(f)

        bucket_order = {'in_flight': 0, 'discovery': 1, 'done': 2, 'other': 3, 'dropped': 4}
        status_color_map = {
            'done': 'var(--success-text)',
            'in_flight': 'var(--info-text)',
            'discovery': 'var(--text-muted)',
            'dropped': 'var(--text-faint)',
            'other': 'var(--text-secondary)',
        }

        parts.append(f"""
            <div class="section">
                <h2 class="section-title">🗂️ Features ({html.escape(initiative_key)}) — by Launch</h2>
                <div class="feature-filter-bar" id="feature-filter-bar">
                    <span class="feature-filter-label">Filter:</span>
                    <label class="feature-filter-chip">
                        <input type="checkbox" data-filter="completed">
                        <span>Hide Completed</span>
                    </label>
                    <label class="feature-filter-chip">
                        <input type="checkbox" data-filter="be">
                        <span>Hide BE Done</span>
                    </label>
                    <label class="feature-filter-chip">
                        <input type="checkbox" data-filter="fe">
                        <span>Hide FE Done</span>
                    </label>
                </div>
                <div class="launch-group-list">
        """)

        first_open_used = False
        # Render the canonical phases first (in order), then any unexpected
        # phases alphabetically, then Unassigned at the end.
        ordered_phases = (
            launch_order
            + sorted(k for k in groups if k not in launch_order and k != 'Unassigned')
            + ['Unassigned']
        )
        for phase in ordered_phases:
            feats = groups.get(phase) or []
            if not feats:
                continue
            sorted_feats = sorted(
                feats,
                key=lambda f: (bucket_order.get(f.get('status_bucket', 'other'), 9), f.get('key', '')),
            )
            buckets = Counter(f.get('status_bucket', 'other') for f in feats)
            done = buckets.get('done', 0)
            in_flight = buckets.get('in_flight', 0)
            discovery = buckets.get('discovery', 0)
            total = len(feats)
            pct_done = (done / total * 100) if total else 0
            badge_cls = 'launch-badge unassigned' if phase == 'Unassigned' else f'launch-badge {phase.lower().replace(" ", "-")}'
            # Open the first non-empty phase so the page isn't entirely collapsed
            # on first load — every other phase stays collapsed.
            open_attr = ' open' if not first_open_used else ''
            first_open_used = True

            rows_html = []
            for f in sorted_feats:
                bucket = f.get('status_bucket', 'other')
                color = status_color_map.get(bucket, 'var(--text-secondary)')
                updated_days = _days_since_iso(f.get('updated'))
                updated_label = f"{updated_days}d ago" if updated_days is not None else '—'
                # Sort key: numeric days for real values, large sentinel for
                # "—" so unknowns sort to the end ascending and start descending.
                updated_sort = updated_days if updated_days is not None else 999999
                status_owner = f.get('status_owner') or ''
                owner_cell = (
                    html.escape(status_owner)
                    if status_owner
                    else '<span style="color: var(--text-faint);">unassigned</span>'
                )
                rag_html, rag_sort = _rag_cell(f.get('rag'))
                ws_days = _days_since_iso(f.get('weekly_status_updated'))
                ws_label = f"{ws_days}d ago" if ws_days is not None else '—'
                ws_sort = ws_days if ws_days is not None else 999999
                fkey = f.get('key') or ''
                open_epics = open_epics_by_feature.get(fkey, 0)
                fws = work_status.get(fkey, {})
                be_done = bool(fws.get('be_done'))
                fe_done = bool(fws.get('fe_done'))
                be_cell = _feature_toggle_cell(fkey, 'be', be_done)
                fe_cell = _feature_toggle_cell(fkey, 'fe', fe_done)
                # "Completed" for filter purposes is the union of three
                # signals: Jira status of Resolved or Abandoned, or a RAG
                # value of "Completed" (PMs sometimes set RAG=Completed
                # before the ticket is formally closed). The status_bucket
                # alone misses RAG=Completed since RAG is independent of
                # Jira workflow state.
                f_status = (f.get('status') or '').strip()
                f_rag = (f.get('rag') or '').strip().lower()
                is_completed = (
                    f_status in ('Resolved', 'Abandoned')
                    or f_rag == 'completed'
                )
                # Row-level data attrs feed the top-of-page filter bar. JS
                # walks .feature-row and toggles display based on these +
                # the active filter checkboxes; the BE/FE attrs also get
                # rewritten when a toggle flips so live filtering stays
                # consistent without a full re-render.
                rows_html.append(f"""
                            <tr class="feature-row" data-bucket="{html.escape(bucket)}" data-completed="{'1' if is_completed else '0'}" data-be-done="{'1' if be_done else '0'}" data-fe-done="{'1' if fe_done else '0'}">
                                <td><a href="{f['url']}" target="_blank" class="ticket-key">{fkey}</a></td>
                                <td>{html.escape(f.get('summary') or '')}</td>
                                <td><span style="color: {color};">{html.escape(f.get('status') or '')}</span></td>
                                <td data-sort="{rag_sort}">{rag_html}</td>
                                <td>{owner_cell}</td>
                                <td data-sort="{open_epics}" style="text-align: right;">{open_epics}</td>
                                {be_cell}
                                {fe_cell}
                                <td data-sort="{updated_sort}" style="text-align: right; color: var(--text-muted); font-size: 12px;">{updated_label}</td>
                                <td data-sort="{ws_sort}" style="text-align: right; color: var(--text-muted); font-size: 12px;">{ws_label}</td>
                            </tr>
                """)

            if phase == 'Unassigned':
                jql_launch = (
                    f'parent = {initiative_key} AND '
                    f'{_JIRA_CF_LAUNCH} is EMPTY AND '
                    'status not in (Abandoned, Duplicate) ORDER BY status, key'
                )
            else:
                jql_launch = (
                    f'parent = {initiative_key} AND '
                    f'{_JIRA_CF_LAUNCH} = {_quote_jql_value(phase)} AND '
                    'status not in (Abandoned, Duplicate) ORDER BY status, key'
                )
            launch_slug = re.sub(r'[^a-z0-9]+', '-', phase.lower()).strip('-') or 'unassigned'
            group_id_launch = f'features-launch-{launch_slug}'
            export_btns_launch = _feature_export_buttons(
                group_id=group_id_launch,
                jql=jql_launch,
                label=f'Features — {phase}',
            )
            parts.append(f"""
                    <details class="launch-group" id="{group_id_launch}"{open_attr}>
                        <summary class="launch-group-summary">
                            <span class="launch-group-caret">▸</span>
                            <span class="{badge_cls}">{html.escape(phase)}</span>
                            <span class="launch-group-counts">
                                <strong>{total}</strong> feature{'s' if total != 1 else ''}
                                · {done} done · {in_flight} in flight · {discovery} discovery
                            </span>
                            <span class="launch-group-pct">{pct_done:.0f}%</span>
                            {export_btns_launch}
                        </summary>
                        <div class="launch-group-body">
                            {_status_bar(buckets, total)}
                            <table class="launch-group-table">
                                <thead>
                                    <tr>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 0, 'string')">Key</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 1, 'string')">Summary</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 2, 'string')">Status</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 3, 'number')">RAG</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 4, 'string')">Status Owner</th>
                                        <th class="sortable" style="text-align: right;" onclick="sortTable(this.closest('table'), 5, 'number')" title="Open epics remaining in this feature (not done or dropped)">Open Epics</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 6, 'number')">BE Done</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 7, 'number')">FE Done</th>
                                        <th class="sortable" style="text-align: right;" onclick="sortTable(this.closest('table'), 8, 'number')">Updated</th>
                                        <th class="sortable" style="text-align: right;" onclick="sortTable(this.closest('table'), 9, 'number')">Weekly Status Updated</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {''.join(rows_html)}
                                </tbody>
                            </table>
                        </div>
                    </details>
            """)

        parts.append("""
                </div>
            </div>
        """)

    return ''.join(parts)


def _adf_to_text(node):
    """Flatten an Atlassian Document Format (ADF) tree to plain text.

    Jira v3 returns rich-text fields as ADF JSON. We don't need faithful
    formatting here — just readable prose with paragraph breaks preserved
    and bullet items prefixed with a bullet.
    """
    if node is None:
        return ''
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ''

    node_type = node.get('type')
    if node_type == 'text':
        return node.get('text') or ''
    if node_type == 'hardBreak':
        return '\n'

    child_text = _adf_to_text(node.get('content'))

    if node_type in ('paragraph', 'heading'):
        return child_text + '\n\n'
    if node_type == 'listItem':
        return f'• {child_text.strip()}\n'
    if node_type in ('bulletList', 'orderedList'):
        return child_text + '\n'
    if node_type == 'codeBlock':
        return child_text + '\n\n'
    # Unknown container type — just fall through to children.
    return child_text


def _description_to_html(text, max_chars=1200):
    """Convert a (possibly long) plain-text block into safe HTML paragraphs.

    Escapes HTML, splits on blank lines, and truncates after max_chars on a
    paragraph boundary so we don't cut mid-sentence.
    """
    import html as _html
    if not text:
        return ''
    clean = text.replace('\r\n', '\n').strip()
    if len(clean) > max_chars:
        clean = clean[:max_chars].rsplit(' ', 1)[0].rstrip() + '…'
    paragraphs = [p.strip() for p in clean.split('\n\n') if p.strip()]
    return ''.join(
        f'<p style="color: var(--text-secondary); margin-bottom: 10px;">{_html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in paragraphs
    )


def _days_since_iso(ts):
    """Days since an ISO-8601 timestamp. Returns None if unparseable."""
    if not ts:
        return None
    try:
        dt = parse_iso_tz(ts)
    except Exception:
        return None
    # parse_iso_tz returns None (doesn't raise) on a malformed-but-truthy
    # timestamp, so guard explicitly before .astimezone() — otherwise the
    # Features freshness columns crash on a single bad date string.
    if dt is None:
        return None
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    return max(0, (now - dt.astimezone(_tz.utc)).days)


# RAG (Red / Amber-Yellow / Green) is a single-select field on Jira. PMs
# sometimes use "Amber" and sometimes "Yellow" for the same idea — collapse
# both onto the same warning color. Unknown values fall through to the
# muted text color so a typo doesn't render as a misleading "Green".
_RAG_COLORS = {
    'red': 'var(--danger-text)',
    'amber': 'var(--warning-text)',
    'yellow': 'var(--warning-text)',
    'green': 'var(--success-text)',
    'blue': 'var(--info-text)',
}


def _rag_cell(value):
    """Return cell HTML and a sort key for a RAG value.

    Sort puts Red first, then Amber/Yellow, then Green, then unknown — so an
    ascending sort surfaces problems at the top of the table.
    """
    if not value:
        return '<span style="color: var(--text-faint);">—</span>', 9
    label = str(value).strip()
    color = _RAG_COLORS.get(label.lower(), 'var(--text-secondary)')
    sort_key = {'red': 0, 'amber': 1, 'yellow': 1, 'green': 2, 'blue': 3}.get(label.lower(), 8)
    return f'<span style="color: {color}; font-weight: 600;">{html.escape(label)}</span>', sort_key


# Custom-field IDs for the JQL we hand to Jira. Mirror sync_project_fantasy.py
# so a clone of either file alone is correct. Jira's Issue Navigator accepts
# `cf[10646]` (numeric form) for custom field references in JQL.
_JIRA_CF_LAUNCH = "cf[10441]"
_JIRA_CF_MILESTONE = "cf[10646]"


def _jira_initiative_key() -> str:
    """Top-level initiative key (jira.initiative_key), used to build the
    Feature-section JQL deep-links. Cached so repeated calls during one regen
    don't re-read config; falls back to INIT-185 when config is unavailable."""
    cached = getattr(_jira_initiative_key, '_cache', None)
    if cached is not None:
        return cached
    try:
        key = (load_config().get('jira') or {}).get('initiative_key') or "INIT-185"
    except Exception:
        key = "INIT-185"
    _jira_initiative_key._cache = key
    return key


def _quote_jql_value(value: str) -> str:
    """Quote a value for inclusion in a JQL string literal."""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _feature_export_buttons(*, group_id: str, label: str, jql: Optional[str] = None) -> str:
    """Render the per-section export buttons (Jira + Sheets + PDF).

    `group_id` is the DOM id of the `<details>` element we'll print.
    `label` is human-readable text used in the PDF print title.
    `jql` is the JQL string to open in the Jira Issue Navigator. When
    omitted, the Jira link is skipped — used for sections like sprint
    reports where there's no clean per-block JQL.
    """
    parts = ['<span class="feature-export-actions" onclick="event.stopPropagation();">']
    if jql:
        jira_url = f'{_jira_cloud_base()}/issues/?jql=' + _url_quote(jql)
        parts.append(
            f'<a class="feature-export-btn" href="{html.escape(jira_url)}" target="_blank" rel="noopener" '
            f'title="Open this section in Jira" '
            f'data-jql="{html.escape(jql)}">'
            '<span aria-hidden="true">↗</span> Jira</a>'
        )
    parts.append(
        f'<button type="button" class="feature-export-btn" '
        f'data-export-sheets="{html.escape(group_id)}" '
        f'data-export-label="{html.escape(label)}" '
        f'title="Copy this section as TSV and open a new Google Sheet to paste into">'
        '<span aria-hidden="true">⊞</span> Sheets</button>'
    )
    parts.append(
        f'<button type="button" class="feature-export-btn" '
        f'data-export-pdf="{html.escape(group_id)}" '
        f'data-export-label="{html.escape(label)}" '
        f'title="Export this section to PDF (uses your browser\'s Save as PDF)">'
        '<span aria-hidden="true">⤓</span> PDF</button>'
    )
    parts.append('</span>')
    return ''.join(parts)


def _export_table_html(headers: list, rows: list) -> str:
    """Build a hidden <table class="export-table"> with TSV-shaped data.

    The table is `hidden` so it doesn't render in the page; the Sheets
    export JS reads it first when serializing a section. PDF export
    ignores it (already isolates the section's visible content).
    Each `rows` entry is a list of plain strings; we HTML-escape on the
    way in.
    """
    th = ''.join(f'<th>{html.escape(h)}</th>' for h in headers)
    body = []
    for r in rows:
        cells = ''.join(f'<td>{html.escape(str(c))}</td>' for c in r)
        body.append(f'<tr>{cells}</tr>')
    return (
        '<table class="export-table" hidden aria-hidden="true">'
        f'<thead><tr>{th}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody>'
        '</table>'
    )


def _url_quote(value: str) -> str:
    """Percent-encode a JQL string for use in a Jira URL."""
    from urllib.parse import quote as _q
    return _q(value, safe='')


def refresh_mbr_nav(mbr_path: Path) -> None:
    """Replace the two <nav> blocks in mbr.html with the canonical nav menu.

    The MBR page is editorial — its body is hand-curated narrative for the
    previous month. But its top + sub nav must stay in sync with the rest of
    the dashboard, so we splice in the output of generate_nav_menu('mbr')
    every time we regenerate.

    Idempotent: if the file is already in canonical shape, this is a no-op.
    """
    import re
    if not mbr_path.exists():
        return
    text = mbr_path.read_text()
    # Match the two consecutive <nav>…</nav> blocks (top-nav + sub-nav).
    pattern = re.compile(
        r"        <nav class=\"top-nav\">.*?</nav>\s*<nav class=\"sub-nav\">.*?</nav>",
        re.DOTALL,
    )
    canonical = generate_nav_menu('mbr')
    new_text, n = pattern.subn(canonical.rstrip(), text, count=1)
    if n and new_text != text:
        _atomic_write(mbr_path, new_text)
        print(f"✅ MBR nav refreshed: {mbr_path}")


def main():
    """Generate all HTML reports."""
    try:
        config = load_config()
        report_dir = Path(config['database']['path']).parent.parent / "reports" / "html"
        report_dir.mkdir(exist_ok=True, parents=True)

        print("Generating HTML reports...")

        # Generate project fantasy roadmap
        generate_project_fantasy_html(report_dir / "project_fantasy.html")

        # Generate features rollup (by Milestone + by Launch)
        generate_features_html(report_dir / "features.html")

        # Generate decomposition readiness by phase
        generate_readiness_html(report_dir / "readiness_dashboard.html")

        # Generate team report
        generate_team_html(config, report_dir / "team_dashboard.html")

        # Generate story points report
        generate_story_points_html(config, report_dir / "story_points_dashboard.html")

        # Generate epics report
        generate_epics_html(config, report_dir / "epics_dashboard.html")

        # Generate past sprint reports
        generate_past_sprints_html(config, report_dir / "past_sprints_dashboard.html")

        # Generate delivery excellence (flow / rework / predictability)
        generate_delivery_excellence_html(config, report_dir / "delivery_excellence_dashboard.html")

        # Generate pull requests report
        generate_pull_requests_html(config, report_dir / "pull_requests_dashboard.html")

        # Stakeholders matrix (driven by config/stakeholders.yaml)
        generate_stakeholders_html(config, report_dir / "stakeholders.html")

        # Dependencies dashboard (driven by config/dependencies.yaml)
        generate_dependencies_html(config, report_dir / "dependencies.html")

        # MBR is hand-authored, but its nav must stay in sync with everything
        # else (incl. the dynamic "Project: {Name}" label), so splice it in.
        refresh_mbr_nav(report_dir / "mbr.html")

        print(f"\n✅ HTML reports generated in {report_dir}")
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
