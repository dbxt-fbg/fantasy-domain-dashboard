#!/usr/bin/env python3
"""
Generate HTML dashboard reports with better formatting.
"""

import html
import json
import sys
import os
from collections import Counter
from pathlib import Path
from datetime import datetime


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
    get_one_on_one_meeting,
    get_one_on_one_meetings_bulk,
)


def render_html(*, title: str, content: str, body_class: str = "page-project") -> str:
    """Render a full page. body_class drives the page-specific theme."""
    return HTML_TEMPLATE.format(title=title, content=content, body_class=body_class)


# Which body class to use per active_page key — mirrors nav.PRIMARY_NAV.
_PAGE_THEME = {
    "project-fantasy": "page-project",
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
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700;900&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="assets/dashboard.css?v=ff-logo-1">
    <script src="assets/dashboard.js?v=2.0" defer></script>
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
        f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#94a3b8" font-size="11">{lbl}</text>'
        for y, lbl in y_ticks
    )
    x_label_svg = ''.join(
        f'<text x="{x:.1f}" y="{svg_h - pad_b + 18}" text-anchor="middle" fill="#94a3b8" font-size="11">{lbl}</text>'
        for x, lbl in x_ticks
    )

    today_marker_svg = ''
    if today_in_sprint:
        tx = x_at(wd_elapsed)
        today_marker_svg = (
            f'<line x1="{tx:.1f}" y1="{pad_t}" x2="{tx:.1f}" y2="{svg_h - pad_b}" '
            f'stroke="#60a5fa" stroke-width="1" stroke-dasharray="3,3" opacity="0.6" />'
            f'<text x="{tx:.1f}" y="{pad_t - 4}" text-anchor="middle" fill="#60a5fa" font-size="10">Today</text>'
        )

    projection_svg = ''
    if projection_points:
        projection_svg = (
            f'<polyline fill="none" stroke="#f59e0b" stroke-width="2" stroke-dasharray="4,3" '
            f'points="{" ".join(projection_points)}" />'
        )

    series_svg_parts = []
    for s in series:
        pts = s.get('points', [])
        color = s.get('color', '#10b981')
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
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{color}" stroke="#1e293b" stroke-width="1.5" />'
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
                        <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{svg_h - pad_b}" stroke="#475569" stroke-width="1" />
                        <line x1="{pad_l}" y1="{svg_h - pad_b}" x2="{svg_w - pad_r}" y2="{svg_h - pad_b}" stroke="#475569" stroke-width="1" />
                        <polyline fill="none" stroke="#64748b" stroke-width="2" stroke-dasharray="4,4" points="{" ".join(ideal_points)}" />
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


def _role_metrics(tickets_by_role: dict[str, list[dict]]) -> dict[str, dict]:
    """Return per-role counts for closed/in_progress/open ticket lists.

    tickets_by_role maps 'closed' | 'in_progress' | 'open' → role partition dict.
    Returns {'BE': {total, closed, in_progress, open, completion}, 'FE': ...}
    """
    result = {}
    for role in ('BE', 'FE'):
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


def _role_sp_metrics(closed, in_prog, open_) -> dict:
    """Sum story_points per role partition, return per-role SP dict."""
    def _sum(lst):
        return sum((t.get('story_points') or 0) for t in lst)

    result = {}
    for role in ('BE', 'FE'):
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


def generate_team_html(config: dict, output_path: Path):
    """Generate HTML team dashboard."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    sprint = get_current_sprint(db_path, sprint_prefix)
    if not sprint:
        print("No active sprint found")
        return

    metrics = get_sprint_metrics(db_path, sprint['sprint_id'])
    burndown = get_sprint_burndown(db_path, sprint['sprint_id'])
    developers = get_all_developers_metrics(db_path, sprint['sprint_id'])

    # Velocity on the Stories page is counted in stories completed, not story points.
    # Story points live on the Story Points page.
    velocity = metrics['closed_tickets'] if metrics else 0
    velocity_label = "Sprint Velocity (Stories Completed)"

    # Get new metrics
    team_cycle_time = get_team_cycle_time(db_path, sprint['sprint_id'])
    team_throughput = get_team_throughput(db_path, sprint['sprint_id'], days=7)
    team_pr_review_time = get_team_pr_review_time(db_path, days=30)
    pr_approvals = get_pr_approvals_by_developer(db_path, days=30)
    commitment_accuracy = get_sprint_commitment_accuracy(db_path, sprint['sprint_id'])
    pr_size_dist = get_pr_size_distribution(db_path, days=30)

    # Calculate completion percentage
    completion = 0
    if metrics and metrics['total_tickets'] > 0:
        completion = (metrics['closed_tickets'] / metrics['total_tickets']) * 100

    # Header + nav go first; the main `content` block gets the metrics and
    # accordions, and `burndown_html` is built separately so we can splice it
    # in at the very top of <div class="content"> at render time.
    content = ""
    burndown_html = ""

    if metrics:
        # Get tickets by status for accordion panels — single batched query
        # rather than one round-trip per status.
        from database.queries import get_tickets_for_sprint

        closed_set = set(CLOSED_STATUSES)
        inprog_set = set(IN_PROGRESS_STATUSES)
        open_set = set(OPEN_STATUSES)
        all_statuses = list(closed_set | inprog_set | open_set)

        all_tickets = get_tickets_for_sprint(
            db_path, sprint['sprint_id'], statuses=all_statuses, issue_type='Story'
        )

        closed_tickets_list = [t for t in all_tickets if t['status'] in closed_set]
        in_progress_tickets_list = [t for t in all_tickets if t['status'] in inprog_set]
        open_tickets_list = [t for t in all_tickets if t['status'] in open_set]

        # Build role maps for BE/FE split
        name_to_role, _ = _build_role_maps(config)
        closed_by_role = _partition_tickets_by_role(closed_tickets_list, name_to_role)
        inprog_by_role = _partition_tickets_by_role(in_progress_tickets_list, name_to_role)
        open_by_role = _partition_tickets_by_role(open_tickets_list, name_to_role)
        role_m = _role_metrics({'closed': closed_by_role, 'in_progress': inprog_by_role, 'open': open_by_role})

        # Combined team-level metrics row at the top
        content += f"""
            <div class="section" id="team-metrics">

                <!-- Additional Team Metrics -->
                <div class="metrics-grid">
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
                    <div class="metric-card {'success' if commitment_accuracy['accuracy'] >= 80 else 'warning' if commitment_accuracy['accuracy'] >= 60 else 'info'}">
                        <div class="metric-label">Sprint Commitment</div>
                        <div class="metric-value">{commitment_accuracy['accuracy']}%</div>
                        <div class="metric-subtext">{commitment_accuracy['completed']}/{commitment_accuracy['planned']} tickets</div>
                    </div>
                </div>
        """

        # Render one section per role
        role_config = [
            ('BE', 'be', '⚙️ Backend'),
            ('FE', 'fe', '🎨 Frontend'),
        ]
        for role, rid, role_label in role_config:
            rm = role_m[role]
            r_closed = closed_by_role[role]
            r_inprog = inprog_by_role[role]
            r_open = open_by_role[role]
            r_velocity = rm['closed']

            content += f"""
                <div class="section-title" style="margin-top: 32px;">{role_label}</div>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-label">Total Tickets</div>
                        <div class="metric-value">{rm['total']}</div>
                    </div>
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
                <div style="margin-top: 16px;">
                    <div class="velocity-card">
                        <div class="velocity-value">{r_velocity}</div>
                        <div class="velocity-label">{velocity_label}</div>
                    </div>
                </div>

                <div id="{rid}-closed-panel" class="accordion-panel">
                    <div class="accordion-content">
                        <div class="accordion-header">✅ Closed Tickets ({len(r_closed)})</div>
                        <div class="ticket-grid">
            """

            for ticket in r_closed:
                content += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    <span style="color: #6b7280; font-size: 12px;"> • {ticket['assignee_display_name'] or 'Unassigned'}</span>
                                </div>
                """

            content += f"""
                        </div>
                    </div>
                </div>

                <div id="{rid}-inprogress-panel" class="accordion-panel">
                    <div class="accordion-content">
                        <div class="accordion-header">🔄 In Progress Tickets ({len(r_inprog)})</div>
                        <div class="ticket-grid">
            """

            for ticket in r_inprog:
                content += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    <span style="color: #6b7280; font-size: 12px;"> • {ticket['assignee_display_name'] or 'Unassigned'} • {ticket['status']}</span>
                                </div>
                """

            content += f"""
                        </div>
                    </div>
                </div>

                <div id="{rid}-open-panel" class="accordion-panel">
                    <div class="accordion-content">
                        <div class="accordion-header">📋 Open / To Do Tickets ({len(r_open)})</div>
                        <div class="ticket-grid">
            """

            for ticket in r_open:
                content += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    <span style="color: #6b7280; font-size: 12px;"> • {ticket['assignee_display_name'] or 'Unassigned'} • {ticket['status']}</span>
                                </div>
                """

            content += """
                        </div>
                    </div>
                </div>
            """

        content += """
            </div>
        """

    # Burndown Chart — working-days only (weekends excluded from axis + math).
    if burndown and len(burndown) > 0:
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

        # Filter pre-sprint snapshot rows so the ideal line anchors at sprint start.
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
        ideal_remaining_today = start_remaining - (start_remaining / wd_total) * wd_elapsed
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

        # Coordinate transforms — pre-pixel, used by the renderer for axis ticks.
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
            return pad_l + (off / wd_total) * inner_w

        def y_at(v):
            return pad_t + (1 - v / max_remaining) * inner_h

        # Y-axis ticks
        axes['y_ticks'] = [
            (y_at(round(max_remaining * (4 - i) / 4)), round(max_remaining * (4 - i) / 4))
            for i in range(5)
        ]
        # X-axis ticks
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

        # Build single 'actual' series with weekend dedupe.
        actual_points = []
        actual_dots = []
        seen = set()
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
            pace_label = f"<span style='color: #6ee7b7;'>↑ {ahead_behind:.0f} ahead of ideal</span>"
        elif ahead_behind < -0.5:
            pace_label = f"<span style='color: #fca5a5;'>↓ {abs(ahead_behind):.0f} behind ideal</span>"
        else:
            pace_label = "<span style='color: #cbd5e1;'>on pace</span>"

        forecast_text = projected_finish_date.strftime('%b %d') if projected_finish_date else '—'
        forecast_delta = ''
        if projected_finish_date:
            delta_days = (projected_finish_date - end_date).days
            forecast_delta = (
                f" ({abs(delta_days)}d early)" if delta_days < 0
                else f" ({delta_days}d late)" if delta_days > 0
                else ' (on time)'
            )

        burndown_html += _render_burndown_chart(
            title='📈 Sprint Burndown Chart',
            section_id='burndown-chart',
            axes=axes,
            series=[{'name': 'Actual', 'points': actual_points, 'dots': actual_dots, 'color': '#10b981'}],
            summary_cards=[
                {'label': 'Remaining',  'value': str(current_remaining), 'sub': f'of {start_remaining} tickets'},
                {'label': 'Pace',       'value': pace_label, 'sub': f'ideal: {ideal_remaining_today:.0f} remaining'},
                {'label': 'Time Left',  'value': f'{days_remaining}d', 'sub': f'working day {wd_elapsed + 1} of {wd_total + 1}'},
                {'label': 'Forecast Finish', 'value': forecast_text, 'sub': f'at current pace{forecast_delta}'},
            ],
            legend=[
                {'kind': 'solid', 'color': '#10b981', 'label': 'Actual'},
                {'kind': 'dashed', 'color': '#64748b', 'label': 'Ideal'},
                {'kind': 'dashed', 'color': '#f59e0b', 'label': 'Projected'},
                {'kind': 'dashed', 'color': '#60a5fa', 'label': 'Today'},
            ],
            ideal_points=ideal_points,
            projection_points=projection_points,
            today_in_sprint=(start_date <= today <= end_date),
        )


    # Final page layout: header → nav → burndown (top of page) → metrics/accordions → footer
    page = f"""
        <header>
            <h1>📊 Team Dashboard</h1>
            <div class="subtitle">{fmt_sprint_long(sprint['sprint_name'])} • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('stories')}
        <div class="content">
{burndown_html}
{content}
            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    html = render_html(
        title=f"Team Dashboard - {fmt_sprint_long(sprint['sprint_name'])}",
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
                                <div class="perf-metric-value" style="color: #10b981;">{completed_sp:g}</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">In Progress SP</div>
                                <div class="perf-metric-value" style="color: #f59e0b;">{in_progress_sp:g}</div>
                            </div>
                            <div class="perf-metric">
                                <div class="perf-metric-label">To Do SP</div>
                                <div class="perf-metric-value" style="color: #3b82f6;">{todo_sp:g}</div>
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
                type_color = '#fbbf24' if t['issue_type'] == 'Bug' else '#94a3b8'
                rows.append(f"""
                                <div style="background: #1e293b; border-left: 3px solid #475569; border-radius: 6px; padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; gap: 12px;">
                                    <div style="flex: 1; min-width: 0;">
                                        <a href="{html.escape(t['ticket_url'] or '')}" target="_blank" style="color: #60a5fa; text-decoration: none; font-weight: 600; font-size: 13px;">{html.escape(t['ticket_key'])}</a>
                                        <span style="color: {type_color}; font-size: 10px; font-weight: 600; margin-left: 6px;">{html.escape(t['issue_type'] or '')}</span>
                                        <span style="color: #e2e8f0; margin-left: 8px;">{html.escape(t['summary'] or '')}</span>
                                    </div>
                                    <div style="display: flex; gap: 10px; align-items: center; flex-shrink: 0;">
                                        <span style="color: #cbd5e1; font-size: 12px; font-variant-numeric: tabular-nums;">{sp} SP</span>
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
                                    <div class="perf-metric-value" style="color: #10b981;">{_format_sp(completed_sp)}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">In Progress SP</div>
                                    <div class="perf-metric-value" style="color: #f59e0b;">{_format_sp(in_progress_sp)}</div>
                                </div>
                                <div class="perf-metric">
                                    <div class="perf-metric-label">To Do SP</div>
                                    <div class="perf-metric-value" style="color: #3b82f6;">{_format_sp(todo_sp)}</div>
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
            details.member-current-sprint {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 12px 16px; margin: 14px 0; }}
            details.member-current-sprint > summary {{ list-style: none; cursor: pointer; outline: none; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
            details.member-current-sprint > summary::-webkit-details-marker {{ display: none; }}
            details.member-current-sprint .member-current-sprint-chevron {{ display: inline-block; width: 10px; color: #94a3b8; transition: transform 0.15s; }}
            details.member-current-sprint[open] .member-current-sprint-chevron {{ transform: rotate(90deg); }}
            details.member-current-sprint > summary:hover .member-current-sprint-chevron {{ color: #cbd5e1; }}
            details.member-current-sprint .member-current-sprint-title {{ font-size: 15px; font-weight: 600; color: #f1f5f9; }}
            details.member-current-sprint .member-current-sprint-meta {{ color: #94a3b8; font-size: 12px; margin-left: auto; }}
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



def generate_story_points_html(config: dict, output_path: Path):
    """Generate HTML story points dashboard."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    sprint = get_current_sprint(db_path, sprint_prefix)
    if not sprint:
        print("No active sprint found")
        return

    metrics = get_sprint_metrics(db_path, sprint['sprint_id'])
    if not metrics:
        print("No metrics found for sprint")
        return

    # Get burndown data
    burndown = get_sprint_burndown(db_path, sprint['sprint_id'])

    # Get tickets grouped by status with story points
    conn = get_connection(db_path)
    cursor = conn.cursor()

    closed_statuses = list(CLOSED_STATUSES)
    in_progress_statuses = list(IN_PROGRESS_STATUSES)
    open_statuses = list(OPEN_STATUSES)

    def _sum_sp_in(statuses):
        cursor.execute(
            "SELECT COALESCE(SUM(story_points), 0) FROM tickets "
            "WHERE sprint_id = ? AND issue_type = 'Story' AND status IN ({})".format(
                ','.join(['?' for _ in statuses])
            ),
            [sprint['sprint_id']] + statuses,
        )
        return cursor.fetchone()[0] or 0

    # Recompute story-point totals directly from tickets (excluding Abandoned/Duplicate)
    # so the dashboard's Total matches the sum of its Completed + In Progress + Open buckets.
    completed_sp = _sum_sp_in(closed_statuses)
    in_progress_sp = _sum_sp_in(in_progress_statuses)
    open_sp = _sum_sp_in(open_statuses)
    total_sp = completed_sp + in_progress_sp + open_sp
    remaining_sp = in_progress_sp + open_sp
    completion = (completed_sp / total_sp * 100) if total_sp > 0 else 0

    # Build content. The burndown block is accumulated separately and spliced
    # in via the <!--BURNDOWN_PLACEHOLDER--> marker below, so it lands at the
    # top of the content area regardless of where it's built.
    nav_menu = generate_nav_menu('story-points')
    burndown_html = ""
    content = f"""
        <header>
            <h1>📊 Story Points Dashboard</h1>
            <div class="subtitle">{fmt_sprint_long(sprint['sprint_name'])} • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
        {nav_menu}
        <div class="content">
            <div class="intro-banner">
                <p>Sprint progress measured by story points instead of ticket count. Story points provide a more accurate measure of work complexity and effort.</p>
            </div>
            <!--BURNDOWN_PLACEHOLDER-->

            <div class="section">
    """

    # Story-points burndown chart — working-days only, with BE and FE series.
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

        start_remaining_sp = _remaining(burndown_in_sprint[0]) if _remaining(burndown_in_sprint[0]) > 0 else total_sp
        current_remaining_sp = _remaining(burndown_in_sprint[-1])
        ideal_remaining_today = start_remaining_sp - (start_remaining_sp / wd_total) * wd_elapsed
        ahead_behind = ideal_remaining_today - current_remaining_sp

        # Per-role daily remaining SP.
        _, id_to_role = _build_role_maps(config)
        conn_dev = get_connection(db_path)
        c_dev = conn_dev.cursor()
        c_dev.execute(
            "SELECT developer_id, snapshot_date, assigned_story_points, remaining_story_points "
            "FROM developer_snapshots WHERE sprint_id = ? AND snapshot_date >= ? ORDER BY snapshot_date",
            (sprint['sprint_id'], start_date.isoformat()),
        )
        role_sp_by_date: dict[str, dict[str, float]] = defaultdict(lambda: {'BE': 0.0, 'FE': 0.0})
        # Track assigned (committed) SP per role per day too so the summary
        # cards can show "remaining of currently-committed" — without this
        # the sub-line uses the day-1 total and reads as "66 of 48 SP" the
        # moment scope is added mid-sprint.
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
        # Total currently committed by role (remaining + done). This is the
        # honest denominator for the summary card — always ≥ remaining.
        total_be = be_assigned_by_date.get(max(be_assigned_by_date, default=start_date), 0) if be_assigned_by_date else 0
        total_fe = fe_assigned_by_date.get(max(fe_assigned_by_date, default=start_date), 0) if fe_assigned_by_date else 0
        # Day-1 commitment for scope-creep callout.
        start_total_be = be_assigned_by_date.get(min(be_assigned_by_date, default=start_date), 0) if be_assigned_by_date else 0
        start_total_fe = fe_assigned_by_date.get(min(fe_assigned_by_date, default=start_date), 0) if fe_assigned_by_date else 0

        # Coordinate transforms.
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
            return pad_l + (off / wd_total) * inner_w

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

        # Build series with weekend dedupe.
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

        # Per-role ideal lines + combined (dimmed) actual become extra series.
        extra_series = []
        if start_be > 0:
            extra_series.append({
                'name': 'BE Ideal',
                'points': [f"{x_at(0):.1f},{y_at(start_be):.1f}", f"{x_at(wd_total):.1f},{y_at(0):.1f}"],
                'color': '#10b981', 'stroke_width': 1.5, 'dasharray': '4,4', 'opacity': 0.4,
            })
        if start_fe > 0:
            extra_series.append({
                'name': 'FE Ideal',
                'points': [f"{x_at(0):.1f},{y_at(start_fe):.1f}", f"{x_at(wd_total):.1f},{y_at(0):.1f}"],
                'color': '#818cf8', 'stroke_width': 1.5, 'dasharray': '4,4', 'opacity': 0.4,
            })
        if combined_points:
            extra_series.append({
                'name': 'Combined Actual',
                'points': combined_points,
                'color': '#475569', 'stroke_width': 1.5, 'dasharray': '2,3', 'opacity': 0.5,
            })

        be_series = {'name': 'BE Actual', 'points': be_points, 'dots': be_dots, 'color': '#10b981'}
        fe_series = {'name': 'FE Actual', 'points': fe_points, 'dots': fe_dots, 'color': '#818cf8'}

        if ahead_behind > 0.5:
            pace_label = f"<span style='color: #6ee7b7;'>↑ {ahead_behind:.1f} SP ahead</span>"
        elif ahead_behind < -0.5:
            pace_label = f"<span style='color: #fca5a5;'>↓ {abs(ahead_behind):.1f} SP behind</span>"
        else:
            pace_label = "<span style='color: #cbd5e1;'>on pace</span>"

        # The headline pace number conflates two effects: (a) execution
        # slipping behind the original plan and (b) scope added mid-sprint
        # inflating "remaining". Split them so the sub-line tells the reader
        # how much of the gap is each. `pace_vs_original` adds back scope
        # so it isolates execution against the day-1 commitment.
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

        burndown_html += _render_burndown_chart(
            title='📈 Story Points Burndown — BE &amp; FE',
            section_id='sp-burndown-chart',
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
                    'color': '#10b981',
                },
                {
                    'label': '🎨 FE Remaining',
                    'value': f'{current_fe:.0f} SP',
                    'sub': (
                        f'of {total_fe:.0f} SP committed'
                        + (f' (+{total_fe - start_total_fe:.0f} added)' if total_fe - start_total_fe > 0.5 else '')
                    ),
                    'color': '#818cf8',
                },
                {'label': 'Overall Pace', 'value': pace_label, 'sub': pace_sub},
                {'label': 'Time Left', 'value': f'{days_remaining}d', 'sub': f'working day {wd_elapsed + 1} of {wd_total + 1}'},
            ],
            legend=[
                {'kind': 'solid', 'color': '#10b981', 'label': 'BE Actual'},
                {'kind': 'dashed', 'color': '#10b981', 'label': 'BE Ideal'},
                {'kind': 'solid', 'color': '#818cf8', 'label': 'FE Actual'},
                {'kind': 'dashed', 'color': '#818cf8', 'label': 'FE Ideal'},
                {'kind': 'dashed', 'color': '#475569', 'label': 'Combined'},
                {'kind': 'dashed', 'color': '#60a5fa', 'label': 'Today'},
            ],
            ideal_points=ideal_points,
            today_in_sprint=(start_date <= today <= end_date),
        )

    # Collect tickets by status with story points — single batched query.
    closed_set = set(CLOSED_STATUSES)
    inprog_set = set(IN_PROGRESS_STATUSES)
    open_set = set(OPEN_STATUSES)
    all_statuses = list(closed_set | inprog_set | open_set)
    placeholders = ",".join("?" for _ in all_statuses)
    cursor.execute(
        f"""
        SELECT ticket_key, summary, ticket_url, assignee_display_name, status, story_points
        FROM tickets
        WHERE sprint_id = ? AND status IN ({placeholders})
        ORDER BY story_points DESC, ticket_key
        """,
        [sprint['sprint_id']] + all_statuses,
    )
    sp_rows = [
        {
            'ticket_key': r[0],
            'summary': r[1],
            'ticket_url': r[2],
            'assignee': r[3],
            'status': r[4],
            'story_points': r[5] or 0,
        }
        for r in cursor.fetchall()
    ]

    closed_tickets = [t for t in sp_rows if t['status'] in closed_set]
    in_progress_tickets = [t for t in sp_rows if t['status'] in inprog_set]
    open_tickets = [t for t in sp_rows if t['status'] in open_set]

    # Build role maps and partition by BE/FE
    name_to_role, _ = _build_role_maps(config)
    closed_by_role = _partition_tickets_by_role(closed_tickets, name_to_role, assignee_key='assignee')
    inprog_by_role = _partition_tickets_by_role(in_progress_tickets, name_to_role, assignee_key='assignee')
    open_by_role = _partition_tickets_by_role(open_tickets, name_to_role, assignee_key='assignee')
    sp_role_m = _role_sp_metrics(closed_by_role, inprog_by_role, open_by_role)

    # Render per-role sections stacked vertically
    sp_role_config = [
        ('BE', 'sp-be', '⚙️ Backend'),
        ('FE', 'sp-fe', '🎨 Frontend'),
    ]

    for role, rid, role_label in sp_role_config:
        rm = sp_role_m[role]
        r_closed = closed_by_role[role]
        r_inprog = inprog_by_role[role]
        r_open = open_by_role[role]

        content += f"""
                <div class="section-title" style="margin-top: 32px;">{role_label}</div>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-label">Total Story Points</div>
                        <div class="metric-value">{rm['total']:.1f}</div>
                    </div>
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
                <div style="margin-top: 16px;">
                    <div class="velocity-card">
                        <div class="velocity-value">{rm['completed']:.1f} SP</div>
                        <div class="velocity-label">Sprint Velocity (Story Points)</div>
                    </div>
                </div>

                <div id="{rid}-closed-panel" class="accordion-panel">
                    <div class="accordion-content">
                        <div class="accordion-header">✅ Completed Tickets ({len(r_closed)} tickets, {rm['completed']:.1f} SP)</div>
                        <div class="ticket-grid">
        """

        for ticket in r_closed:
            sp_badge = f"<span style='color: #10b981; font-weight: 600; margin-left: 8px;'>{ticket['story_points']:.1f} SP</span>" if ticket['story_points'] > 0 else ""
            content += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    {sp_badge}
                                    <span style="color: #6b7280; font-size: 12px;"> • {ticket['assignee'] or 'Unassigned'}</span>
                                </div>
            """

        content += f"""
                        </div>
                    </div>
                </div>

                <div id="{rid}-inprogress-panel" class="accordion-panel">
                    <div class="accordion-content">
                        <div class="accordion-header">🔄 In Progress Tickets ({len(r_inprog)} tickets, {rm['in_progress']:.1f} SP)</div>
                        <div class="ticket-grid">
        """

        for ticket in r_inprog:
            sp_badge = f"<span style='color: #f59e0b; font-weight: 600; margin-left: 8px;'>{ticket['story_points']:.1f} SP</span>" if ticket['story_points'] > 0 else ""
            content += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    {sp_badge}
                                    <span style="color: #6b7280; font-size: 12px;"> • {ticket['assignee'] or 'Unassigned'} • {ticket['status']}</span>
                                </div>
            """

        content += f"""
                        </div>
                    </div>
                </div>

                <div id="{rid}-open-panel" class="accordion-panel">
                    <div class="accordion-content">
                        <div class="accordion-header">📋 Open / To Do Tickets ({len(r_open)} tickets, {rm['open']:.1f} SP)</div>
                        <div class="ticket-grid">
        """

        for ticket in r_open:
            sp_badge = f"<span style='color: #3b82f6; font-weight: 600; margin-left: 8px;'>{ticket['story_points']:.1f} SP</span>" if ticket['story_points'] > 0 else ""
            content += f"""
                                <div class="ticket-item">
                                    <a href="{ticket['ticket_url']}" class="ticket-key" target="_blank">{ticket['ticket_key']}</a>
                                    {ticket['summary']}
                                    {sp_badge}
                                    <span style="color: #6b7280; font-size: 12px;"> • {ticket['assignee'] or 'Unassigned'} • {ticket['status']}</span>
                                </div>
            """

        content += """
                        </div>
                    </div>
                </div>
        """

    content += """
            </div>

            <footer>
                Generated by Engineering Management Dashboard
            </footer>
        </div>
    """

    conn.close()

    # Splice the burndown block into its placeholder so it appears at the
    # top of the content area.
    content = content.replace('<!--BURNDOWN_PLACEHOLDER-->', burndown_html)

    html = render_html(
        title=f"Story Points - {fmt_sprint_long(sprint['sprint_name'])}",
        content=content,
        body_class=_PAGE_THEME["story-points"],
    )

    _atomic_write(output_path, html)
    print(f"✅ Story points dashboard generated: {output_path}")


def _build_full_sprint_sequence(sprints, target_milestone=31, target_sprint_in_milestone=4):
    """Return a list of sprint dicts covering every M<n>.<sp> in the range,
    mixing real DB rows with synthesized placeholders.

    From M30.3 onward, each milestone slot has two concurrent sprints: one FE
    and one BE. Both are returned as separate entries (same date range, different
    role). When only one of the pair exists in Jira, a placeholder is synthesised
    for the missing counterpart so charts stay symmetric.

    Each returned dict has:
        sprint_id       — real DB id, or None for placeholders
        sprint_name     — display name
        short_label     — e.g. "M30.3 FE" / "M30.3 BE" / "M31.3"
        start_date      — ISO string (UTC)
        end_date        — ISO string (UTC)
        placeholder     — bool
        role            — 'FE' | 'BE' | None  (None for pre-split sprints)
    """
    from datetime import timedelta as _td
    import re as _re

    SPRINT_LEN_DAYS = 14

    def parse_label(short):
        try:
            mpart = short.split(' ', 1)[0]
            mnum, spnum = mpart.lstrip('M').split('.')
            return int(mnum), int(spnum)
        except Exception:
            return None

    def parse_role(sprint_name):
        """Extract FE/BE suffix from sprint name, or None if absent."""
        m = _re.search(r'\b(FE|BE)\s*$', sprint_name)
        return m.group(1) if m else None

    def next_label(m, sp):
        if sp >= 4:
            return m + 1, 1
        return m, sp + 1

    def iso_date(d):
        # Match the time-of-day that real Jira sprints use (08:00 UTC) so
        # synthesized placeholders compare cleanly against neighbours and
        # don't drift 8 hours when the in-memory entry sits next to a
        # real DB row in the same chart column.
        return d.isoformat() + 'T08:00:00.000Z'

    if not sprints:
        return []

    # Sort input chronologically, then FE before BE within same start date.
    ordered = sorted(
        sprints,
        key=lambda s: (
            parse_iso_tz(s['start_date']).date(),
            {'FE': 0, 'BE': 1}.get(parse_role(s['sprint_name']), 2),
        ),
    )

    # Build an anchor: nearest real sprint's slot (m, sp) → year.week token.
    # The counter ("2026.13") increments by 1 each slot regardless of calendar
    # (the user sets this in Jira sequentially, not by ISO week-of-year). We
    # use this to extrapolate week tokens for synthesised placeholders so they
    # follow the canonical "FNTSY M30.4 Sprint 2026.13 FE" naming.
    _week_re = _re.compile(r'\bSprint\s+(\d{4})\.(\d+)\b')
    slot_to_week: dict[tuple[int, int], tuple[int, int]] = {}
    for s in ordered:
        lbl = parse_label(s['sprint_name'].replace('FNTSY ', '').replace(' Sprint', ''))
        m = _week_re.search(s['sprint_name'])
        if lbl and m:
            slot_to_week[lbl] = (int(m.group(1)), int(m.group(2)))

    def slot_week_for(m_target: int, sp_target: int) -> tuple[int, int]:
        """Return (year, week) for an arbitrary (m, sp) slot.

        Linear extrapolation from the nearest real anchor: each slot adds 1
        to the week counter, rolling year over at week 53 (no team has hit
        this yet but it's correct for long-range projection). If we have no
        anchor at all (DB completely empty), fall back to (year_now, 1).
        """
        if (m_target, sp_target) in slot_to_week:
            return slot_to_week[(m_target, sp_target)]
        if not slot_to_week:
            return (datetime.now().year, 1)
        # Pick the closest known anchor by linear distance in slot space
        # (each milestone holds 4 sprint slots).
        def slot_distance(a: tuple[int, int]) -> int:
            return abs((m_target * 4 + sp_target) - (a[0] * 4 + a[1]))
        anchor_slot = min(slot_to_week.keys(), key=slot_distance)
        anchor_year, anchor_week = slot_to_week[anchor_slot]
        # Walk slots forward or backward, incrementing/decrementing the week.
        slot_delta = (m_target * 4 + sp_target) - (anchor_slot[0] * 4 + anchor_slot[1])
        year = anchor_year
        week = anchor_week + slot_delta
        # Roll over Jira's "53 weeks per year" convention.
        while week > 53:
            week -= 53
            year += 1
        while week < 1:
            week += 53
            year -= 1
        return (year, week)

    out = []
    prev_label = None
    prev_end = None
    # Track which roles are present at each (m, sp) slot so we can synthesise
    # the missing counterpart.
    label_roles_seen: dict = {}  # (m, sp) -> set of roles

    def make_sprint_entry(sprint_id, sprint_name, short_label, start_date_iso,
                          end_date_iso, placeholder, role):
        return {
            'sprint_id': sprint_id,
            'sprint_name': sprint_name,
            'short_label': short_label,
            'start_date': start_date_iso,
            'end_date': end_date_iso,
            'placeholder': placeholder,
            'role': role,
        }

    def emit_placeholder(m, sp, start_date, role=None):
        # Canonical placeholder name follows the same shape as real Jira
        # sprints: "FNTSY M30.4 Sprint 2026.13 FE". Week token is computed
        # by extending from the nearest real sprint's anchor (slot_to_week).
        end_date = start_date + _td(days=SPRINT_LEN_DAYS)
        role_suffix = f' {role}' if role else ''
        year, week = slot_week_for(m, sp)
        sprint_name = f"FNTSY M{m}.{sp} Sprint {year}.{week:02d}{role_suffix}"
        short_label = f"M{m}.{sp} {year}.{week:02d}{role_suffix}".strip()
        out.append(make_sprint_entry(
            sprint_id=None,
            sprint_name=sprint_name,
            short_label=short_label,
            start_date_iso=iso_date(start_date),
            end_date_iso=iso_date(end_date),
            placeholder=True,
            role=role,
        ))
        return end_date

    SPLIT_FROM = (30, 3)  # M30.3 onward is FE/BE-split

    for s in ordered:
        short = s['sprint_name'].replace('FNTSY ', '').replace(' Sprint', '')
        this_label = parse_label(short)
        this_role = parse_role(s['sprint_name'])
        this_start = parse_iso_tz(s['start_date']).date()
        this_end = parse_iso_tz(s['end_date']).date()

        # Pair-split slots (M30.3+) require a role on every row. If a real
        # Jira sprint at one of those slots is missing the suffix (e.g.
        # "FNTSY M31.2 Sprint 2026.15"), treat it as BE so the FE counterpart
        # gets synthesised below. Doesn't change the Jira ticket; only how
        # the dashboard slots it into pair-rendering displays.
        if this_role is None and this_label is not None and this_label >= SPLIT_FROM:
            this_role = 'BE'

        # Fill mid-sequence gaps (skip for same-slot concurrent sprints).
        # FE + BE placeholders share the same start_date/end_date — they
        # represent two concurrent role-split sprints, not consecutive ones,
        # so we capture the FE end-date once and reuse it as the BE start.
        if (prev_label is not None and this_label is not None
                and prev_end is not None and this_label != prev_label):
            m, sp = prev_label
            cursor_start = prev_end
            seen = set()
            while True:
                m, sp = next_label(m, sp)
                if (m, sp) == this_label or (m, sp) in seen:
                    break
                seen.add((m, sp))
                # FE + BE for the same slot: same start, same end.
                slot_start = cursor_start
                fe_end = emit_placeholder(m, sp, slot_start, 'FE')
                emit_placeholder(m, sp, slot_start, 'BE')
                cursor_start = fe_end

        # Track roles seen at this label
        if this_label is not None:
            if this_label not in label_roles_seen:
                label_roles_seen[this_label] = set()
            if this_role:
                label_roles_seen[this_label].add(this_role)

        out.append(make_sprint_entry(
            sprint_id=s['sprint_id'],
            sprint_name=s['sprint_name'],
            short_label=short,
            start_date_iso=s['start_date'],
            end_date_iso=s['end_date'],
            placeholder=False,
            role=this_role,
        ))
        if this_label is not None:
            prev_label = this_label
        prev_end = this_end

    # After all real sprints are placed, synthesise missing FE/BE counterparts
    # for split slots. From M30.3 onward every milestone-slot is FE+BE; if
    # only one role appeared in Jira, add the other; if a role-less sprint
    # appeared (e.g. legacy "M31.2 Sprint 2026.15" with no suffix), add both.
    to_add = []
    for (m, sp), roles in label_roles_seen.items():
        if (m, sp) < SPLIT_FROM:
            continue  # M30.1 / M30.2 are pre-split — no pairing needed
        # Find any existing entry at this slot to clone dates from.
        existing = next(
            (e for e in out
             if not e['placeholder'] and parse_label(e['short_label']) == (m, sp)),
            None,
        )
        if not existing:
            continue
        # Use the existing real sibling's week token so counterparts share
        # the same canonical name shape as the Jira ticket they pair with.
        existing_match = _week_re.search(existing['sprint_name'])
        if existing_match:
            year, week = int(existing_match.group(1)), int(existing_match.group(2))
        else:
            year, week = slot_week_for(m, sp)
        for role in ('FE', 'BE'):
            if role in roles:
                continue
            to_add.append(make_sprint_entry(
                sprint_id=None,
                sprint_name=f"FNTSY M{m}.{sp} Sprint {year}.{week:02d} {role}",
                short_label=f"M{m}.{sp} {year}.{week:02d} {role}",
                start_date_iso=existing['start_date'],
                end_date_iso=existing['end_date'],
                placeholder=True,
                role=role,
            ))

    # Insert missing-counterpart placeholders right after the last existing
    # entry at the same slot. Handles both partial pairs (one role present,
    # the other missing) and fully-roleless legacy sprints (M31.2 etc.) that
    # need both FE and BE placeholders inserted alongside.
    if to_add:
        # Group placeholders by (m, sp) so we know what to drop into each slot.
        pending: dict = {}
        for ph in to_add:
            lbl = parse_label(ph['short_label'])
            pending.setdefault(lbl, []).append(ph)

        out_with_pairs = []
        for i, entry in enumerate(out):
            out_with_pairs.append(entry)
            lbl = parse_label(entry['short_label'])
            if lbl in pending:
                # Inject after the last entry at this slot — peek ahead.
                next_lbl = parse_label(out[i + 1]['short_label']) if i + 1 < len(out) else None
                if next_lbl != lbl:
                    # Sort FE before BE for consistent rendering.
                    for ph in sorted(pending[lbl], key=lambda p: 0 if p['role'] == 'FE' else 1):
                        out_with_pairs.append(ph)
                    del pending[lbl]
        out = out_with_pairs

    # Extend forward through the target milestone with paired FE+BE
    # placeholders. FE and BE share the slot's start/end (concurrent), so
    # advance the cursor by the FE end-date once per slot.
    if prev_label is not None and prev_end is not None:
        m, sp = prev_label
        cursor_start = prev_end
        for _ in range(100):
            if (m, sp) == (target_milestone, target_sprint_in_milestone):
                break
            m, sp = next_label(m, sp)
            fe_end = emit_placeholder(m, sp, cursor_start, 'FE')
            emit_placeholder(m, sp, cursor_start, 'BE')
            cursor_start = fe_end

    return out


def _placeholder_synthetic_id(milestone: int, slot: int, role) -> int:
    """Stable, deterministic synthetic jira_sprint_id for a placeholder.

    Real Jira IDs are positive auto-increments; we use negative numbers
    that encode (milestone, slot, role) so the same placeholder always
    upserts to the same DB row across runs and never collides with a
    real Jira sprint that lands later.
    """
    role_code = {'FE': 1, 'BE': 2, None: 0}.get(role, 0)
    return -(milestone * 1000 + slot * 10 + role_code)


def persist_placeholder_sprints(db_path: str, sequence) -> int:
    """Upsert placeholder rows from a `_build_full_sprint_sequence` result.

    Real Jira sprints (`placeholder=False`) are skipped — the unified
    Jira collector owns those. Placeholders (`placeholder=True`) get a
    deterministic synthetic jira_sprint_id from `_placeholder_synthetic_id`,
    so re-running this function is idempotent.

    Returns the number of placeholder rows touched (insert + update).
    """
    from utils.sprint_names import parse_sprint_name
    placeholders = [s for s in sequence if s.get('placeholder')]
    if not placeholders:
        return 0
    now_iso = datetime.now().isoformat()
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        touched = 0
        for ph in placeholders:
            parsed = parse_sprint_name(ph['sprint_name'])
            if parsed is None:
                # Pre-split (M30.1/M30.2) shouldn't appear here, but be safe.
                continue
            jira_id = _placeholder_synthetic_id(
                parsed.milestone, parsed.slot, ph.get('role')
            )
            cur.execute(
                "SELECT sprint_id FROM sprints WHERE jira_sprint_id = ?",
                (jira_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO sprints (
                        jira_sprint_id, sprint_name, state,
                        start_date, end_date, goal,
                        is_placeholder, first_seen_at, last_updated_at
                    ) VALUES (?, ?, 'future', ?, ?, '', 1, ?, ?)
                    """,
                    (jira_id, ph['sprint_name'], ph['start_date'], ph['end_date'],
                     now_iso, now_iso),
                )
            else:
                cur.execute(
                    """
                    UPDATE sprints
                       SET sprint_name = ?,
                           start_date = ?,
                           end_date = ?,
                           is_placeholder = 1,
                           last_updated_at = ?
                     WHERE jira_sprint_id = ?
                    """,
                    (ph['sprint_name'], ph['start_date'], ph['end_date'],
                     now_iso, jira_id),
                )
            touched += 1
        conn.commit()
        return touched
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


def _render_epics_per_sprint_line_chart(sprints, epics_by_sprint):
    """Render three stacked SVG line charts — BE, FE, and missing-prefix — each with
    its own y-axis scale and a shared x-axis so columns align with the Gantt below.
    """
    if not sprints:
        return ''

    full_seq = _build_full_sprint_sequence(sprints)
    if not full_seq:
        return ''

    import re as _re
    from datetime import timedelta as _td

    def _milestone_key(short_label):
        """Extract the slot label (e.g. "M30.3") so FE/BE pairs merge into
        a single x-axis point. The function name is historical — what we
        actually want here is `format_slot`, not `format_milestone` (which
        would collapse all four slots in M30 to a single "M30" point)."""
        return fmt_sprint_slot(short_label)

    # Group full_seq entries by milestone key, preserving order of first occurrence.
    # Concurrent FE/BE sprints share dates — collapse them into one x-axis point
    # and sum their epic counts so no zigzag appears on the line chart.
    seen_keys = {}
    ordered_keys = []
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
        # Accumulate epic counts across all sprints in this milestone
        sprint_epics = epics_by_sprint.get(s['sprint_id'], []) if s['sprint_id'] is not None else []
        for epic in sprint_epics:
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

    return f"""
            <div class="section">
                <h2 class="section-title">📈 Epics per Sprint</h2>
                <div class="chart-container">
                    <div style="background:#1e293b; border-radius:8px; padding:20px; border:1px solid #475569; overflow-x:auto;">
                        <div style="min-width:800px;">
{rows_html}
                        </div>
                        <p style="margin-top:var(--space-3); color:var(--text-muted); font-size:var(--fs-sm);">
                            Epic-type tickets per sprint split by
                            <strong style="color:var(--info);">[BE] backend</strong> /
                            <strong style="color:var(--success);">[FE] frontend</strong>{
                              ' / <strong style="color:var(--danger);">missing prefix</strong>'
                              if any(s['key'] == 'none' for s in SERIES) else ''
                            }.
                            Dashed tails = projected sprints.{
                              ' Red signal = Ticket Hygiene will flag under <code>epics_no_prefix</code>.'
                              if any(s['key'] == 'none' for s in SERIES) else ''
                            }
                        </p>
                    </div>
                </div>
            </div>
    """


def generate_epics_html(config: dict, output_path: Path):
    """Generate HTML epics dashboard with Gantt chart."""
    db_path = config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']

    # Get all sprints (current and next 7 sprints)
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Get recent, current and future REAL sprints (excluding the synthesized
    # placeholders we persist via persist_placeholder_sprints — those are an
    # output of _build_full_sprint_sequence, not an input. Including them as
    # input would re-trigger placeholder synthesis on top of the persisted
    # ones and double the sequence each run).
    cursor.execute("""
        SELECT sprint_id, sprint_name, start_date, end_date, state
        FROM (
            SELECT sprint_id, sprint_name, start_date, end_date, state
            FROM sprints
            WHERE is_placeholder = 0
              AND (sprint_name LIKE ? || '%'
                   OR jira_sprint_id IN (21197, 21198, 21199, 21200))
            ORDER BY start_date DESC
            LIMIT 8
        )
        ORDER BY start_date ASC
    """, (sprint_prefix,))

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
            all_dates.append(parse_iso_tz(sprint['start_date']).date())
            all_dates.append(parse_iso_tz(sprint['end_date']).date())

        chart_start = min(all_dates)
        chart_end = max(all_dates)
        total_days = (chart_end - chart_start).days
    else:
        chart_start = datetime.now().date()
        chart_end = chart_start
        total_days = 1

    # Epic-count-per-sprint line chart (shown above the Gantt).
    line_chart_html = _render_epics_per_sprint_line_chart(sprints, epics_by_sprint)

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
                    <div class="chart-title">📊 Epic Timeline (Gantt Chart)</div>
                    <div style="background: #1e293b; border-radius: 8px; padding: 12px; border: 1px solid #475569; overflow-x: auto;">
                        <div class="gantt-wrapper" style="min-width: 1500px; position: relative;">
                            <!-- Sortable column headers for the epic-label area.
                                 Widths must mirror the per-row sub-cells below so
                                 the columns line up with the cells underneath. -->
                            <div style="display: flex; align-items: center; height: 28px; border-bottom: 1px solid #334155; font-size: 11px; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em;">
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'key')" style="width: 80px; padding: 0 8px 0 0;">Key</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'status')" style="width: 110px; padding: 0 8px 0 0;">Status</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'summary')" style="width: 190px; padding: 0 8px 0 0;">Summary</div>
                                <div class="gantt-col-header" onclick="sortGanttRows(this, 'assignee')" style="width: 60px; padding: 0 8px 0 0;">Assignee</div>
                                <div style="flex: 1;"></div>
                            </div>
                            <!-- Header with dates -->
                            <div style="display: flex; margin-bottom: 4px;">
                                <div style="width: 440px; flex-shrink: 0;"></div>
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
            role_color = {'FE': '#38bdf8', 'BE': '#a78bfa'}.get(role, '#cbd5e1')
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
            f'<div style="height:14px; min-width:0; font-size:10px; color:#94a3b8; '
            f'text-align:center; white-space:nowrap; overflow:hidden; text-overflow:clip; '
            f'border-top:1px solid #334155; margin-top:1px; padding-top:1px;">'
            f'{col_start.strftime("%b %d")} – {col_end.strftime("%b %d")}</div>'
        )
        content += (
            f'<div style="position:absolute; left:{left_pct:.2f}%; width:{width_pct:.2f}%; '
            f'height:50px; border-left:1px solid #475569; box-sizing:border-box; padding:1px 2px; '
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
        # Absolute % is relative to the timeline area only (the 440px first
        # column lives outside this overlay), so we offset by 440px on the
        # outer wrapper. Easiest: nest the overlay inside a sibling that
        # mirrors the same layout the row uses (440px label slot + flex bar).
        grid_lines.append(
            f'<div style="position:absolute; left:{col_left:.2f}%; '
            f'top:0; bottom:0; width:1px; background:#334155; '
            f'pointer-events:none;"></div>'
        )
    # Also draw the right edge of the last column for visual closure.
    grid_lines.append(
        '<div style="position:absolute; left:100%; top:0; bottom:0; '
        'width:1px; background:#334155; pointer-events:none;"></div>'
    )
    content += f"""
                            <!-- Vertical sprint dividers spanning the full chart height.
                                 The overlay sits on top of the timeline area only;
                                 the 440px epic-label column stays untouched.
                                 top:83px = 29px col-header + 50px date row + 4px margin. -->
                            <div style="position:absolute; left:440px; right:0;
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

    # Render each epic as a row
    for epic in all_epics:
        col_bounds = col_bounds_by_sprint_id.get(epic.get('sprint_id'))
        if col_bounds:
            epic_sprint_start, epic_sprint_end = col_bounds
        else:
            # Fallback for any epic whose sprint isn't in the visible
            # full_sprint_sequence — keep its raw date math.
            epic_sprint_start = parse_iso_tz(epic['start_date']).date()
            epic_sprint_end = parse_iso_tz(epic['end_date']).date()

        days_from_chart_start = (epic_sprint_start - chart_start).days
        days_in_epic_sprint = (epic_sprint_end - epic_sprint_start).days

        left_pct = (days_from_chart_start / total_days * 100) if total_days > 0 else 0
        width_pct = (days_in_epic_sprint / total_days * 100) if total_days > 0 else 0

        # Color based on status (uses canonical buckets so new in-progress
        # statuses like 'Testing in progress' don't fall through to grey).
        epic_bucket = bucket_for(epic['status'])
        if epic_bucket == 'closed':
            bar_color = '#10b981'
            status_badge = 'background: #064e3b; color: #6ee7b7;'
        elif epic['status'] == 'Blocked':
            bar_color = '#ef4444'
            status_badge = 'background: #7f1d1d; color: #fca5a5;'
        elif epic_bucket == 'in_progress':
            bar_color = '#3b82f6'
            status_badge = 'background: #1e3a8a; color: #93c5fd;'
        else:
            bar_color = '#6b7280'
            status_badge = 'background: #374151; color: #9ca3af;'

        sp_chip = (
            f"<span style='display:inline-block; padding:2px 6px; border-radius:3px; "
            f"background:#374151; color:#fcd34d; font-weight:600;'>"
            f"{epic['story_points']:.0f} SP</span>"
            if epic['story_points'] > 0 else ""
        )
        assignee_chip = (
            f"<span style='color:#94a3b8;'>{epic['assignee']}</span>"
            if epic['assignee'] else
            "<span style='color:#64748b; font-style:italic;'>Unassigned</span>"
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
        sort_attrs = (
            f'data-sort-key="{html.escape(display_key)}" '
            f'data-sort-status="{html.escape(epic["status"] or "")}" '
            f'data-sort-summary="{html.escape(epic["summary"] or "")}" '
            f'data-sort-assignee="{html.escape(assignee_sort)}"'
        )
        # Initials for the narrow Assignee column — full name appears in tooltip.
        if epic['assignee']:
            parts = epic['assignee'].split()
            initials = (parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else '')).upper()
            assignee_cell = (
                f"<span style='color:#cbd5e1;' title='{html.escape(epic['assignee'])}'>{html.escape(initials)}</span>"
            )
        else:
            assignee_cell = "<span style='color:#64748b; font-style:italic;' title='Unassigned'>—</span>"
        content += f"""
                            <div class="gantt-row" {sort_attrs} style="display: flex; align-items: stretch; min-height: 30px; border-bottom: 1px solid #334155;">
                                <!-- Four sortable sub-cells (Key/Status/Summary/Assignee).
                                     Widths total 440px to match the column headers above. -->
                                <div style="width: 80px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; overflow: hidden; white-space: nowrap;">
                                    <a href="{epic['ticket_url']}" target="_blank" style="color: #60a5fa; text-decoration: none; font-weight: 600;">{display_key}</a>
                                </div>
                                <div style="width: 110px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; gap: 4px; overflow: hidden; white-space: nowrap;">
                                    <span style="display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; {status_badge};">{epic['status']}</span>
                                    {sp_chip}
                                </div>
                                <div style="width: 190px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 13px; display: flex; align-items: center; color: #cbd5e1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;" title="{html.escape(epic['summary'] or '')}">
                                    <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{html.escape(epic['summary'] or '')}</span>
                                </div>
                                <div style="width: 60px; flex-shrink: 0; padding: 4px 8px 4px 0; font-size: 12px; display: flex; align-items: center; overflow: hidden; white-space: nowrap;">
                                    {assignee_cell}
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
            '#10b981' if _eb == 'closed'
            else '#ef4444' if epic['status'] == 'Blocked'
            else '#3b82f6' if _eb == 'in_progress'
            else '#6b7280'
        )
        sp_html = (
            f"<span style='color: #f59e0b; font-weight: 600;'>{epic['story_points']:.0f} SP</span>"
            if epic['story_points'] > 0 else ""
        )
        # Strip the cross-sprint "_s<jira_sprint_id>" suffix from the display
        # key — Jira itself only knows it as the bare key, and the URL is
        # already correct.
        display_key = epic['ticket_key'].split('_s', 1)[0]
        return f"""
                        <div style="background: #1e293b; border-left: 3px solid {status_color}; border-radius: 6px; padding: 12px; display: flex; justify-content: space-between; align-items: center;">
                            <div style="flex: 1;">
                                <a href="{epic['ticket_url']}" target="_blank" style="color: #60a5fa; text-decoration: none; font-weight: 600; font-size: 14px;">{display_key}</a>
                                <span style="color: #e2e8f0; margin-left: 8px;">{epic['summary']}</span>
                            </div>
                            <div style="display: flex; gap: 10px; align-items: center;">
                                {sp_html}
                                <span style="color: #94a3b8; font-size: 12px;">{epic['assignee'] or 'Unassigned'}</span>
                                <span style="padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; background: #475569; color: #e2e8f0;">{epic['status']}</span>
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
                            <span class="epic-sprint-stat" style="color: #10b981;">{completed_count} <small>done</small></span>
                            <span class="epic-sprint-stat" style="color: #3b82f6;">{in_progress_count} <small>in progress</small></span>
                            <span class="epic-sprint-stat" style="color: #6366f1;">{len(sprint_epics)} <small>total</small></span>
                            <span class="epic-sprint-stat" style="color: #f59e0b;">{total_sp:.0f} <small>SP</small></span>
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
                                    <span class="epic-role-stat" style="color: #10b981;">{role_done} <small>done</small></span>
                                    <span class="epic-role-stat" style="color: #3b82f6;">{role_in_prog} <small>in progress</small></span>
                                    <span class="epic-role-stat" style="color: #6366f1;">{len(role_epics)} <small>total</small></span>
                                    <span class="epic-role-stat" style="color: #f59e0b;">{role_total_sp:.0f} <small>SP</small></span>
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
    Story+Bug roster grouped by engineer. Closed sprints render solid +
    expanded; active/future/empty sprints render dimmed (italic + 55%
    opacity) and collapsed — same convention as the Epics page so a glance
    tells you what's settled vs still in flight.
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
            details.past-sprint-engineer summary:hover .past-sprint-chevron {{ color: #cbd5e1; }}
            /* Top-level sprint disclosure */
            details.sprint-report-block summary::-webkit-details-marker {{ display: none; }}
            details.sprint-report-block summary::marker {{ content: ''; }}
            details.sprint-report-block[open] > summary .sprint-report-chevron {{ transform: rotate(90deg); }}
            details.sprint-report-block summary {{ cursor: pointer; user-select: none; }}
            details.sprint-report-block summary:hover .sprint-report-chevron {{ color: #cbd5e1; }}
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
            .sprint-report-state.closed {{ background: #064e3b; color: #6ee7b7; }}
            .sprint-report-state.active {{ background: #78350f; color: #fcd34d; }}
            .sprint-report-state.future {{ background: #334155; color: #94a3b8; }}
            .sprint-report-state.placeholder {{ background: #1e1b4b; color: #c7d2fe; }}
        </style>
        <header>
            <h1>📜 Sprint Reports</h1>
            <div class="subtitle">Stories &amp; Bugs grouped by engineer • Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
{generate_nav_menu('past-sprints')}
        <div class="content">
            <div class="intro-banner">
                <p>One section per FNTSY sprint — closed sprints first, then active and future. Closed sprints stay expanded; incomplete sprints (active/future/empty) appear dimmed and collapsed. Engineers are sorted by completed story points (Done / Closed / Resolved). Click any sprint header to collapse or expand it.</p>
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
    sprints that simply have nothing assigned yet. Both render dimmed +
    collapsed.
    """
    name = html.escape(fmt_sprint_long(sprint['sprint_name']))
    is_placeholder = bool(sprint.get('is_placeholder'))
    state = (sprint.get('state') or '').lower()
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
            <details class="sprint-report-block is-incomplete" style="margin-bottom: 28px;">
                <summary style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; padding: 8px 0; border-bottom: 2px solid var(--border);">
                    <h2 class="section-title sprint-report-name" style="margin: 0; border-bottom: none; padding-bottom: 0;">
                        <span class="sprint-report-chevron" aria-hidden="true" style="display: inline-block; width: 12px; color: #94a3b8; transition: transform 0.15s; margin-right: 6px;">▶</span>
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
        bg, fg = '#064e3b', '#6ee7b7'
    elif status in IN_PROGRESS_STATUSES:
        bg, fg = '#1e3a8a', '#93c5fd'
    elif status == 'Blocked':
        bg, fg = '#7f1d1d', '#fca5a5'
    else:
        bg, fg = '#374151', '#9ca3af'
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

    Closed sprints render solid + expanded by default. Active / future
    sprints render dimmed (italic + 60% opacity on the title and meta) +
    collapsed by default — they have data to inspect, but at a glance the
    page leads with what's already settled.
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
    open_attr = ' open' if is_complete else ''
    block_class = 'sprint-report-block' + ('' if is_complete else ' is-incomplete')

    engineer_html = []
    for e in engineers:
        rows = []
        for t in e['tickets']:
            sp = _format_sp(t['story_points'] or 0.0)
            type_label = t['issue_type']
            type_color = '#fbbf24' if type_label == 'Bug' else '#94a3b8'
            rows.append(f"""
                            <div style="background: #1e293b; border-left: 3px solid #475569; border-radius: 6px; padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; gap: 12px;">
                                <div style="flex: 1; min-width: 0;">
                                    <a href="{html.escape(t['ticket_url'] or '')}" target="_blank" style="color: #60a5fa; text-decoration: none; font-weight: 600; font-size: 13px;">{html.escape(t['ticket_key'])}</a>
                                    <span style="color: {type_color}; font-size: 10px; font-weight: 600; margin-left: 6px;">{html.escape(type_label)}</span>
                                    <span style="color: #e2e8f0; margin-left: 8px;">{html.escape(t['summary'] or '')}</span>
                                </div>
                                <div style="display: flex; gap: 10px; align-items: center; flex-shrink: 0;">
                                    <span style="color: #cbd5e1; font-size: 12px; font-variant-numeric: tabular-nums;">{sp} SP</span>
                                    {_status_badge(t.get('sprint_end_status') or t['status'])}
                                </div>
                            </div>
            """)

        engineer_html.append(f"""
                <details class="past-sprint-engineer" style="background: #334155; border-radius: 8px; padding: 16px; margin-bottom: 14px;">
                    <summary style="list-style: none; cursor: pointer; outline: none;">
                        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
                            <h3 style="font-size: 16px; color: #f1f5f9; margin: 0; display: flex; align-items: center; gap: 8px;">
                                <span class="past-sprint-chevron" aria-hidden="true" style="display: inline-block; width: 10px; color: #94a3b8; transition: transform 0.15s;">▶</span>
                                {html.escape(e['name'])}
                            </h3>
                            <div style="display: flex; gap: 14px;">
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #38bdf8;">{_format_sp(e['in_review_plus_sp'])}</div>
                                    <div style="font-size: 10px; color: #94a3b8;">In Code Review+</div>
                                </div>
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #10b981;">{_format_sp(e['completed_sp'])}</div>
                                    <div style="font-size: 10px; color: #94a3b8;">Completed SP</div>
                                </div>
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #6366f1;">{_format_sp(e['total_sp'])}</div>
                                    <div style="font-size: 10px; color: #94a3b8;">Total SP</div>
                                </div>
                                <div style="text-align: center;">
                                    <div style="font-size: 18px; font-weight: 700; color: #cbd5e1;">{e['count']}</div>
                                    <div style="font-size: 10px; color: #94a3b8;">Stories</div>
                                </div>
                            </div>
                        </div>
                    </summary>
                    <div style="display: grid; gap: 6px; margin-top: 12px;">
                        {''.join(rows)}
                    </div>
                </details>
        """)

    return f"""
            <details class="{block_class}" style="margin-bottom: 28px;"{open_attr}>
                <summary style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; padding: 8px 0; margin-bottom: 12px; border-bottom: 2px solid var(--border);">
                    <h2 class="section-title sprint-report-name" style="margin: 0; border-bottom: none; padding-bottom: 0;">
                        <span class="sprint-report-chevron" aria-hidden="true" style="display: inline-block; width: 12px; color: #94a3b8; transition: transform 0.15s; margin-right: 6px;">▶</span>
                        {name}
                        <span class="sprint-report-state {state_class}">{state_label}</span>
                    </h2>
                    <div class="sprint-report-meta" style="color: var(--text-muted); font-size: 13px;">{start} → {end} · {total_count} tickets · {_format_sp(total_completed_sp)} completed · {_format_sp(total_review_plus_sp)} in code review+ · {_format_sp(total_sp)} total SP</div>
                </summary>
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
        ('XS', pr_size_dist['xs'], '<50 lines', '#10b981'),
        ('S', pr_size_dist['s'], '50-200', '#3b82f6'),
        ('M', pr_size_dist['m'], '200-400', '#f59e0b'),
        ('L', pr_size_dist['l'], '400-800', '#ef4444'),
        ('XL', pr_size_dist['xl'], '>800', '#7f1d1d')
    ]

    for label, count, range_text, color in sizes:
        percentage = (count / total_prs * 100) if total_prs > 0 else 0
        content += f"""
                        <div style="text-align: center; background: #1e293b; padding: 16px; border-radius: 8px;">
                            <div style="font-size: 12px; color: #94a3b8; margin-bottom: 8px;">{label}</div>
                            <div style="font-size: 28px; font-weight: 700; color: {color}; margin-bottom: 4px;">{count}</div>
                            <div style="font-size: 11px; color: #64748b;">{range_text}</div>
                            <div style="font-size: 11px; color: #64748b; margin-top: 4px;">{percentage:.0f}%</div>
                        </div>
        """

    avg_review_time_str = f"{team_pr_review_time:.0f}h" if team_pr_review_time else "N/A"

    content += f"""
                    </div>
                </div>
            </div>

            <!-- PR Review Metrics -->
            <div class="section">
                <h2 class="section-title">⏱️ Review Metrics</h2>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-label">Avg Review Time</div>
                        <div class="metric-value">{avg_review_time_str}</div>
                        <div class="metric-subtext">time to merge</div>
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
            f'<line x1="{tx:.1f}" y1="{baseline_y - 6}" x2="{tx:.1f}" y2="{baseline_y + 6}" stroke="#475569" stroke-width="1" />'
            f'<text x="{tx:.1f}" y="{baseline_y + 22}" text-anchor="middle" fill="#94a3b8" font-size="10">{month_label}</text>'
        )
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    # Deliverable vertical lines + labels (stagger label Y to avoid overlap on dense clusters)
    phases_svg = ''
    # Colors cycle through brand + accents so adjacent markers are distinguishable
    colors = ['#6366f1', '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6']
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
            f'<circle cx="{px:.1f}" cy="{baseline_y}" r="5" fill="{color}" stroke="#1e293b" stroke-width="2" />'
            # Phase name above the line
            f'<text x="{px:.1f}" y="{label_y}" text-anchor="middle" fill="{color}" font-size="11" font-weight="600">{phase["name"]}</text>'
            # Date label above the dot
            f'<text x="{px:.1f}" y="{baseline_y - 12}" text-anchor="middle" fill="#cbd5e1" font-size="10">{marker}</text>'
        )

    # Today marker (only if today sits in the visible range)
    today_marker_svg = ''
    if timeline_start <= today <= timeline_end:
        tx = x_at(today)
        today_marker_svg = (
            f'<line x1="{tx:.1f}" y1="{pad_t}" x2="{tx:.1f}" y2="{svg_h - pad_b}" stroke="#60a5fa" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.8" />'
            f'<text x="{tx:.1f}" y="{svg_h - pad_b + 40}" text-anchor="middle" fill="#60a5fa" font-size="11" font-weight="600">Today · {today.strftime("%b %d, %Y")}</text>'
        )

    # Baseline bar (filled portion = progress to today)
    baseline_svg = (
        f'<line x1="{pad_l}" y1="{baseline_y}" x2="{svg_w - pad_r}" y2="{baseline_y}" stroke="#475569" stroke-width="3" />'
    )
    if timeline_start <= today <= timeline_end:
        baseline_svg += (
            f'<line x1="{pad_l}" y1="{baseline_y}" x2="{x_at(today):.1f}" y2="{baseline_y}" stroke="#6366f1" stroke-width="3" />'
        )

    content = f"""
        <header>
            <h1>🎯 Project: Fantasy</h1>
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
        title="Project: Fantasy",
        content=content,
        body_class=_PAGE_THEME["project-fantasy"],
    )
    _atomic_write(output_path, html)
    print(f"✅ Project Fantasy dashboard generated: {output_path}")


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
            <div class="subtitle">Project: Fantasy · Source: config/stakeholders.yaml{(" · Last edited " + last_updated_label) if last_updated_label else ""}</div>
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
        title="Stakeholders — Project: Fantasy",
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


def _dep_default_url(key: str) -> str:
    """Best-effort Jira URL for any project key — the cloud_id lives in config
    but we don't need it here since the format is uniform. We hit the standard
    Atlassian Cloud URL for the betfanatics tenant."""
    if not key:
        return ''
    return f"https://betfanatics.atlassian.net/browse/{key}"


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

    status_cls = _dep_status_class(status)
    team_html = f'<span class="dep-team">{html.escape(team)}</span>' if team else ''
    safe_notes = html.escape(notes)
    safe_key = html.escape(key)

    return f"""
        <div class="dep-card" data-key="{safe_key}">
            <div class="dep-head">
                <a class="dep-key" href="{html.escape(url)}" target="_blank" rel="noopener">{safe_key}</a>
                {team_html}
                <span class="dep-status {status_cls}">{html.escape(status)}</span>
            </div>
            <div class="dep-summary">{html.escape(summary)}</div>
            <div class="dep-meta">
                <span><strong>Owner:</strong>{html.escape(owner)}</span>
            </div>
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
            <div class="subtitle">Project: Fantasy · {len(deps)} tracked · Source: config/dependencies.yaml{(" · Last edited " + last_updated_label) if last_updated_label else ""}</div>
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
        title="Dependencies — Project: Fantasy",
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

    # ---- By Target Milestone ----------------------------------------------
    # Group features by their Proposed Milestone (Jira customfield_10646),
    # not by fixVersion. fixVersion is a release-train concept that doesn't
    # match how DFS plans work; Proposed Milestone is the single-select the
    # PMs actually fill in.  Features without a milestone fall under
    # "Unassigned". Progress bar per milestone is driven by status buckets.
    features_list = snap.get('features', []) or []
    if features_list:
        by_release: dict[str, list[dict]] = {}
        for feat in features_list:
            if feat.get('status_bucket') == 'dropped':
                continue
            milestone = (feat.get('proposed_milestone') or '').strip()
            if not milestone:
                by_release.setdefault('Unassigned', []).append(feat)
            else:
                by_release.setdefault(milestone, []).append(feat)

        if by_release:
            # Milestone option values come prefixed with a number ("30. Milestone
            # 30 - …"), so a plain alphabetical sort already gives chronological
            # order. "Unassigned" is pushed to the end.
            release_names = sorted(
                by_release.keys(),
                key=lambda n: (n == 'Unassigned', n),
            )

            rows_html = []
            bucket_order_ms = {'in_flight': 0, 'discovery': 1, 'done': 2, 'other': 3, 'dropped': 4}
            status_color_map_ms = {
                'done': 'var(--success-text)',
                'in_flight': 'var(--info-text)',
                'discovery': 'var(--text-muted)',
                'dropped': 'var(--text-faint)',
                'other': 'var(--text-secondary)',
            }
            first_open_ms = False
            for name in release_names:
                feats = by_release[name]
                total = len(feats)
                buckets = Counter(f['status_bucket'] for f in feats)
                done = buckets.get('done', 0)
                in_flight = buckets.get('in_flight', 0)
                discovery = buckets.get('discovery', 0)
                pct_done = (done / total * 100) if total else 0

                # Sort features within milestone by status bucket then key.
                sorted_feats = sorted(
                    feats,
                    key=lambda f: (bucket_order_ms.get(f.get('status_bucket', 'other'), 9), f.get('key', '')),
                )

                # Render each feature as a table row.
                feat_rows_html = []
                for f in sorted_feats:
                    bucket = f.get('status_bucket', 'other')
                    color = status_color_map_ms.get(bucket, 'var(--text-secondary)')
                    updated_days = _days_since_iso(f.get('updated'))
                    updated_label = f"{updated_days}d ago" if updated_days is not None else '—'
                    updated_sort = updated_days if updated_days is not None else 999999
                    status_owner = f.get('status_owner') or ''
                    owner_cell = (
                        html.escape(status_owner)
                        if status_owner
                        else '<span style="color: var(--text-faint);">unassigned</span>'
                    )
                    feat_rows_html.append(f"""
                                <tr>
                                    <td><a href="{html.escape(f.get('url') or '#')}" target="_blank" class="ticket-key">{html.escape(f.get('key') or '')}</a></td>
                                    <td>{html.escape(f.get('summary') or '')}</td>
                                    <td><span style="color: {color};">{html.escape(f.get('status') or '')}</span></td>
                                    <td>{owner_cell}</td>
                                    <td data-sort="{updated_sort}" style="text-align: right; color: var(--text-muted); font-size: 12px;">{updated_label}</td>
                                </tr>
                    """)

                badge_cls = 'release-badge unscheduled' if name == 'Unassigned' else 'release-badge'
                # Open the first milestone so the section isn't entirely collapsed.
                open_attr = ' open' if not first_open_ms else ''
                first_open_ms = True
                rows_html.append(f"""
                    <details class="release-group"{open_attr}>
                        <summary class="release-group-summary">
                            <span class="release-group-caret">▸</span>
                            <span class="{badge_cls}">{html.escape(name)}</span>
                            <span class="release-group-counts">
                                <strong>{total}</strong> feature{'s' if total != 1 else ''}
                                · {done} done · {in_flight} in flight · {discovery} discovery
                            </span>
                            <span class="release-group-pct">{pct_done:.0f}%</span>
                        </summary>
                        <div class="release-group-body">
                            {_status_bar(buckets, total)}
                            <table class="release-group-table">
                                <thead>
                                    <tr>
                                        <th>Key</th>
                                        <th>Summary</th>
                                        <th>Status</th>
                                        <th>Owner</th>
                                        <th style="text-align: right;">Updated</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {''.join(feat_rows_html)}
                                </tbody>
                            </table>
                        </div>
                    </details>
                """)

            parts.append(f"""
            <div class="section">
                <h2 class="section-title">🎯 By Target Milestone</h2>
                <div class="release-group-list">
                    {''.join(rows_html)}
                </div>
            </div>
            """)

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

        parts.append("""
            <div class="section">
                <h2 class="section-title">🗂️ Features (INIT-185) — by Launch</h2>
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
                rows_html.append(f"""
                            <tr>
                                <td><a href="{f['url']}" target="_blank" class="ticket-key">{f['key']}</a></td>
                                <td>{html.escape(f.get('summary') or '')}</td>
                                <td><span style="color: {color};">{html.escape(f.get('status') or '')}</span></td>
                                <td>{owner_cell}</td>
                                <td data-sort="{updated_sort}" style="text-align: right; color: var(--text-muted); font-size: 12px;">{updated_label}</td>
                            </tr>
                """)

            parts.append(f"""
                    <details class="launch-group"{open_attr}>
                        <summary class="launch-group-summary">
                            <span class="launch-group-caret">▸</span>
                            <span class="{badge_cls}">{html.escape(phase)}</span>
                            <span class="launch-group-counts">
                                <strong>{total}</strong> feature{'s' if total != 1 else ''}
                                · {done} done · {in_flight} in flight · {discovery} discovery
                            </span>
                            <span class="launch-group-pct">{pct_done:.0f}%</span>
                        </summary>
                        <div class="launch-group-body">
                            {_status_bar(buckets, total)}
                            <table class="launch-group-table">
                                <thead>
                                    <tr>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 0, 'string')">Key</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 1, 'string')">Summary</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 2, 'string')">Status</th>
                                        <th class="sortable" onclick="sortTable(this.closest('table'), 3, 'string')">Status Owner</th>
                                        <th class="sortable" style="text-align: right;" onclick="sortTable(this.closest('table'), 4, 'number')">Updated</th>
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

    # ---- Useful Links -----------------------------------------------------
    # Hand-curated tools/environments that aren't in Confluence. Add new
    # entries here as `(label, url, description)`; description is optional.
    useful_links = [
        ('Playmaker',
         'https://playmaker-internal.dev1.fanatics.bet/fantasy/contests',
         'Internal contest admin (dev1)'),
        ('Fantasy Structure Board',
         'https://betfanatics.atlassian.net/jira/apps/94d5de1a-112d-4549-bd03-5f910d5fd27b/880424a7-af14-4c77-b446-5ef9feee797a/structure/board/6464',
         'Jira Structure board'),
    ]
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
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    return max(0, (now - dt.astimezone(_tz.utc)).days)


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

        # Generate team report
        generate_team_html(config, report_dir / "team_dashboard.html")

        # Generate story points report
        generate_story_points_html(config, report_dir / "story_points_dashboard.html")

        # Generate epics report
        generate_epics_html(config, report_dir / "epics_dashboard.html")

        # Generate past sprint reports
        generate_past_sprints_html(config, report_dir / "past_sprints_dashboard.html")

        # Generate team members pages (individual member pages only, no dashboard)
        generate_team_members_html(config, report_dir / "team_members_dashboard.html")

        # Generate pull requests report
        generate_pull_requests_html(config, report_dir / "pull_requests_dashboard.html")

        # Stakeholders matrix (driven by config/stakeholders.yaml)
        generate_stakeholders_html(config, report_dir / "stakeholders.html")

        # Dependencies dashboard (driven by config/dependencies.yaml)
        generate_dependencies_html(config, report_dir / "dependencies.html")

        print(f"\n✅ HTML reports generated in {report_dir}")
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
