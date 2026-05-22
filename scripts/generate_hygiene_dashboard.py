#!/usr/bin/env python3
"""
Generate Ticket Hygiene Dashboard HTML page.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.io import atomic_write as _atomic_write
from utils.nav import generate_nav_menu
from database.schema import get_connection


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket Hygiene Dashboard</title>
    <link rel="stylesheet" href="assets/dashboard.css">
    <script src="assets/dashboard.js?v=2.0" defer></script>
    <!-- showSection lives in assets/dashboard.js -->
</head>
<body class="page-hygiene">
    <div class="container">
        {content}
    </div>
</body>
</html>
"""


def generate_hygiene_dashboard(config: dict, output_path: Path):
    """Generate hygiene dashboard HTML."""
    db_path = config['database']['path']
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Get counts by issue type — open issues only. Resolved rows stay
        # in the table for agent memory but shouldn't count toward headline
        # numbers on the dashboard.
        cursor.execute("""
            SELECT issue_type, COUNT(*) as count
            FROM hygiene_issues
            WHERE resolved_at IS NULL
            GROUP BY issue_type
        """)

        counts = {row[0]: row[1] for row in cursor.fetchall()}

        # Shared nav — the 'hygiene' key activates both the primary (Project: Fantasy)
        # and the secondary (Ticket Hygiene) tabs automatically.
        nav_menu = generate_nav_menu('hygiene')

        # Build HTML
        content = f"""
        <header>
            <h1>🔍 Ticket Hygiene Dashboard</h1>
            <div class="subtitle">Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
        {nav_menu}
        <div class="content">
            <h2 class="section-title" style="margin-top: 0;">🎯 Feature Hygiene Rules</h2>
            <div class="metrics-grid">
                <button type="button" class="metric-card{' clean' if counts.get('features_missing_requirements', 0) == 0 else ''}" onclick="showSection('features_missing_requirements')" aria-controls="features_missing_requirements">
                    <div class="metric-label">Features Missing Requirements</div>
                    <div class="metric-value">{counts.get('features_missing_requirements', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('features_missing_designs', 0) == 0 else ''}" onclick="showSection('features_missing_designs')" aria-controls="features_missing_designs">
                    <div class="metric-label">Features Missing Designs</div>
                    <div class="metric-value">{counts.get('features_missing_designs', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('features_missing_launch_phase', 0) == 0 else ''}" onclick="showSection('features_missing_launch_phase')" aria-controls="features_missing_launch_phase">
                    <div class="metric-label">Features Missing Launch Phase</div>
                    <div class="metric-value">{counts.get('features_missing_launch_phase', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('features_missing_milestone', 0) == 0 else ''}" onclick="showSection('features_missing_milestone')" aria-controls="features_missing_milestone">
                    <div class="metric-label">Features Missing Proposed Milestone</div>
                    <div class="metric-value">{counts.get('features_missing_milestone', 0)}</div>
                </button>
            </div>

            <h2 class="section-title" style="margin-top: 40px;">📋 Epic Hygiene Rules</h2>
            <div class="metrics-grid">
                <button type="button" class="metric-card{'  clean' if counts.get('epics_no_parent', 0) == 0 else ''}" onclick="showSection('epics_no_parent')" aria-controls="epics_no_parent">
                    <div class="metric-label">Epics Without Parent</div>
                    <div class="metric-value">{counts.get('epics_no_parent', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_no_description', 0) == 0 else ''}" onclick="showSection('epics_no_description')" aria-controls="epics_no_description">
                    <div class="metric-label">Epics Missing Description</div>
                    <div class="metric-value">{counts.get('epics_no_description', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_no_prefix', 0) == 0 else ''}" onclick="showSection('epics_no_prefix')" aria-controls="epics_no_prefix">
                    <div class="metric-label">Epics Without [BE]/[FE] Prefix</div>
                    <div class="metric-value">{counts.get('epics_no_prefix', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_no_designs', 0) == 0 else ''}" onclick="showSection('epics_no_designs')" aria-controls="epics_no_designs">
                    <div class="metric-label">[FE] Epics Without Figma</div>
                    <div class="metric-value">{counts.get('epics_no_designs', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_no_work_items', 0) == 0 else ''}" onclick="showSection('epics_no_work_items')" aria-controls="epics_no_work_items">
                    <div class="metric-label">Epics Without Child Stories</div>
                    <div class="metric-value">{counts.get('epics_no_work_items', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_missing_acceptance_criteria', 0) == 0 else ''}" onclick="showSection('epics_missing_acceptance_criteria')" aria-controls="epics_missing_acceptance_criteria">
                    <div class="metric-label">Epics Missing AC</div>
                    <div class="metric-value">{counts.get('epics_missing_acceptance_criteria', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_no_sprint', 0) == 0 else ''}" onclick="showSection('epics_no_sprint')" aria-controls="epics_no_sprint">
                    <div class="metric-label">Epics Without Sprint</div>
                    <div class="metric-value">{counts.get('epics_no_sprint', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('epics_in_progress_no_assignee', 0) == 0 else ''}" onclick="showSection('epics_in_progress_no_assignee')" aria-controls="epics_in_progress_no_assignee">
                    <div class="metric-label">Epics In Progress · No Assignee</div>
                    <div class="metric-value">{counts.get('epics_in_progress_no_assignee', 0)}</div>
                </button>
            </div>

            <h2 class="section-title" style="margin-top: 40px;">📝 Story Hygiene Rules</h2>
            <div class="metrics-grid">
                <button type="button" class="metric-card{' clean' if counts.get('stories_no_parent', 0) == 0 else ''}" onclick="showSection('stories_no_parent')" aria-controls="stories_no_parent">
                    <div class="metric-label">Stories Without Epic</div>
                    <div class="metric-value">{counts.get('stories_no_parent', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('stories_no_points', 0) == 0 else ''}" onclick="showSection('stories_no_points')" aria-controls="stories_no_points">
                    <div class="metric-label">Stories Without Points</div>
                    <div class="metric-value">{counts.get('stories_no_points', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('stories_no_description', 0) == 0 else ''}" onclick="showSection('stories_no_description')" aria-controls="stories_no_description">
                    <div class="metric-label">Stories Missing Description</div>
                    <div class="metric-value">{counts.get('stories_no_description', 0)}</div>
                </button>
                <button type="button" class="metric-card{' clean' if counts.get('code_review_24h', 0) == 0 else ''}" onclick="showSection('code_review_24h')" aria-controls="code_review_24h">
                    <div class="metric-label">Code Review > 24 Hours</div>
                    <div class="metric-value">{counts.get('code_review_24h', 0)}</div>
                </button>
            </div>
        """

        # Generate sections for each issue type
        issue_types = [
            ('features_missing_requirements', 'Features Missing Requirements'),
            ('features_missing_designs', 'Features Missing Designs (No Figma/Screenshots)'),
            ('features_missing_launch_phase', 'Features Missing Launch Phase'),
            ('features_missing_milestone', 'Features Missing Proposed Milestone'),
            ('epics_no_parent', 'Epics Without Parent Feature'),
            ('epics_no_description', 'Epics Without Description'),
            ('epics_no_prefix', 'Epics Without [BE]/[FE] Prefix'),
            ('epics_no_designs', '[FE] Epics Without Figma Link'),
            ('epics_no_work_items', 'Epics Without Child Stories'),
            ('epics_missing_acceptance_criteria', 'Epics Missing Acceptance Criteria'),
            ('epics_no_sprint', 'Epics Not Assigned to a Sprint'),
            ('epics_in_progress_no_assignee', 'Epics In Progress Without Assignee'),
            ('stories_no_parent', 'Stories In Progress Without Parent Epic'),
            ('stories_no_points', 'Stories In Progress Without Story Points'),
            ('stories_no_description', 'Stories In Progress Without Description'),
            ('code_review_24h', 'Tickets In Code Review > 24 Hours')
        ]

        for issue_type, title in issue_types:
            # Pull first_seen_at so we can render an age badge. Only show
            # currently-open rows (resolved_at IS NULL) — historical rows
            # from earlier runs are kept in the table for the agent's memory
            # but shouldn't clutter the list on this page.
            cursor.execute("""
                SELECT ticket_key, ticket_summary, ticket_url, assignee_display_name,
                       status, details, first_seen_at, times_seen, times_resolved
                FROM hygiene_issues
                WHERE issue_type = ? AND resolved_at IS NULL
                ORDER BY first_seen_at ASC, ticket_key
            """, (issue_type,))

            issues = cursor.fetchall()

            content += f"""
            <div class="section" id="{issue_type}">
                <h2 class="section-title">{title} ({len(issues)})</h2>
            """

            if issues:
                content += '<div class="issue-list">'
                now = datetime.now()
                for row in issues:
                    (ticket_key, summary, url, assignee, status,
                     details, first_seen_at, times_seen, times_resolved) = row
                    # Compute age in days (defensive parse).
                    age_badge = ''
                    if first_seen_at:
                        try:
                            fs = datetime.fromisoformat(first_seen_at)
                            age_days = max(0, (now - fs).days)
                            if age_days >= 14:
                                tone = 'danger'
                            elif age_days >= 7:
                                tone = 'warning'
                            elif age_days >= 3:
                                tone = 'info'
                            else:
                                tone = 'muted'
                            age_badge = (
                                f'<span class="badge age-{tone}" '
                                f'title="First seen {first_seen_at}">'
                                f'open {age_days}d</span>'
                            )
                        except Exception:
                            pass
                    regression_badge = ''
                    if times_resolved and times_resolved > 0:
                        regression_badge = (
                            f'<span class="badge regression" '
                            f'title="Has resolved and reappeared {times_resolved}x">'
                            f'↻ regressed {times_resolved}x</span>'
                        )
                    content += f"""
                    <div class="issue-item">
                        <div>
                            <a href="{url}" target="_blank" class="issue-key">{ticket_key}</a>
                            <span class="issue-summary">{summary}</span>
                        </div>
                        <div class="issue-meta">
                            <span class="badge assignee">{assignee}</span>
                            <span class="badge status">{status}</span>
                            {age_badge}
                            {regression_badge}
                    """

                    if 'hours' in details:
                        hours = details.split('for ')[1].split(' hours')[0]
                        content += f'<span class="badge hours">{hours}h in review</span>'

                    content += """
                        </div>
                    </div>
                    """
                content += '</div>'
            else:
                content += """
                <div class="empty-state">
                    <div class="icon">✅</div>
                    <div>No issues found. Great job!</div>
                </div>
                """

            content += '</div>'

        content += """
        </div>
    </div>
    """

        # Write HTML file
        html = HTML_TEMPLATE.format(content=content)
        _atomic_write(output_path, html)
        print(f"✅ Hygiene dashboard generated: {output_path}")

    finally:
        conn.close()


def main():
    """Main entry point."""
    config = load_config()
    output_path = Path(__file__).parent.parent / "reports" / "html" / "hygiene_dashboard.html"

    generate_hygiene_dashboard(config, output_path)


if __name__ == "__main__":
    main()
