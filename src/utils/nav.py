"""Single source of truth for dashboard navigation.

Keeps primary + secondary nav structure in one place so generators never
drift (e.g., a new tab was missing from the hygiene/logs pages because
both hardcoded their own nav at different times).
"""

from __future__ import annotations


PRIMARY_NAV = [
    ('project-fantasy', 'project_fantasy.html', 'Project: Fantasy'),
    ('team-members',    'team_members_dashboard.html', 'Team Members'),
    ('logs',            'logs_dashboard.html', 'Agents and Logs'),
]

# Secondary tabs live under Project: Fantasy and appear only when the active
# page belongs to that group.
SECONDARY_NAV = [
    ('stories',       'team_dashboard.html#team-metrics', 'Stories'),
    ('story-points',  'story_points_dashboard.html', 'Story Points'),
    ('epics',         'epics_dashboard.html', 'Epics'),
    ('pull-requests', 'pull_requests_dashboard.html', 'Repositories'),
    ('hygiene',       'hygiene_dashboard.html', 'Ticket Hygiene'),
    ('past-sprints',  'past_sprints_dashboard.html', 'Sprint Reports'),
    ('stakeholders',  'stakeholders.html', 'Stakeholders'),
    ('dependencies',  'dependencies.html', 'Dependencies'),
    ('mbr',           'mbr.html', 'MBR'),
]


def generate_nav_menu(active_page: str = 'stories') -> str:
    """Render the primary (+ secondary) nav, highlighting the active page."""
    secondary_keys = {k for k, _, _ in SECONDARY_NAV}
    active_primary = 'project-fantasy' if active_page in secondary_keys else active_page

    parts = ['        <nav class="top-nav">\n            <ul class="nav-menu">\n']
    for key, url, label in PRIMARY_NAV:
        active_cls = ' class="active"' if key == active_primary else ''
        parts.append(f'                <li class="nav-item"><a href="{url}"{active_cls}>{label}</a></li>\n')
    parts.append('            </ul>\n        </nav>\n')

    if active_primary == 'project-fantasy':
        parts.append('        <nav class="sub-nav">\n            <ul class="nav-menu">\n')
        for key, url, label in SECONDARY_NAV:
            active_cls = ' class="active"' if key == active_page else ''
            parts.append(f'                <li class="nav-item"><a href="{url}"{active_cls}>{label}</a></li>\n')
        parts.append('            </ul>\n        </nav>')

    return ''.join(parts)
