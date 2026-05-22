#!/usr/bin/env python3
"""Discover and auto-append new team members to config/team_config.yaml.

Signals scanned:
  1. Jira sprint-ticket assignees (high signal)
  2. Recurring 1:1 meeting attendees (high signal)
  3. GitHub PR authors in fantasy-* repos (medium signal)
  4. GitHub PR reviewers of team PRs (low signal — catches cross-team)

New entries are silently appended to the team_members list. Existing
entries are never modified. A safety rail (--max, default 20) prevents
runaway additions from the reviewer signal.

Usage:
    python3 scripts/discover_team_members.py            # auto-append
    python3 scripts/discover_team_members.py --dry-run  # preview only
    python3 scripts/discover_team_members.py --max 100  # override rail
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from database.schema import get_connection

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "team_config.yaml"


def _gh_display_name(login: str) -> str:
    """Fetch a GitHub user's display name. Empty string on failure."""
    try:
        result = subprocess.run(
            ['gh', 'api', f'/users/{login}'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        data = json.loads(result.stdout)
        return data.get('name') or ''
    except Exception:
        return ''


def _load_known(config):
    members = config.get('team_members', [])
    return {
        'jira': {m.get('jira_account_id') for m in members if m.get('jira_account_id')},
        'github': {(m.get('github_username') or '').lower() for m in members if m.get('github_username')},
    }


def _discover_jira(db_path, known):
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT DISTINCT assignee_account_id, assignee_display_name
            FROM tickets
            WHERE assignee_account_id IS NOT NULL AND assignee_account_id != ''
        """).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        aid = r['assignee_account_id']
        if aid in known['jira']:
            continue
        out.append({
            'jira_account_id': aid,
            'name': (r['assignee_display_name'] or '').strip(),
            'github_username': '',
            'source': 'jira_assignee',
        })
    return out


def _discover_github_authors(db_path, known):
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT DISTINCT author_github_username
            FROM github_prs
            WHERE lower(repository) LIKE '%fantasy%'
              AND author_github_username IS NOT NULL
        """).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        login = r['author_github_username']
        if not login or login.lower() in known['github']:
            continue
        out.append({
            'jira_account_id': '',
            'name': '',
            'github_username': login,
            'source': 'github_author',
        })
    return out


def _discover_github_reviewers(db_path, known):
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT DISTINCT reviewer_github_username
            FROM github_reviews
        """).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        login = r['reviewer_github_username']
        if not login or login.lower() in known['github']:
            continue
        out.append({
            'jira_account_id': '',
            'name': '',
            'github_username': login,
            'source': 'github_reviewer',
        })
    return out


def _discover_calendar(db_path, known):
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT DISTINCT developer_name, jira_account_id, github_username
            FROM one_on_one_meetings
        """).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        name = (r['developer_name'] or '').strip()
        if not name:
            continue
        aid = r['jira_account_id'] or ''
        login = r['github_username'] or ''
        if aid and aid in known['jira']:
            continue
        if login and login.lower() in known['github']:
            continue
        out.append({
            'jira_account_id': aid,
            'name': name,
            'github_username': login,
            'source': 'calendar_1on1',
        })
    return out


def _merge(candidates):
    """Merge candidates that clearly represent the same person (same jira_id
    or same github login). Name-based fuzzy matching intentionally skipped —
    exact ID matches only, to avoid wrong merges.
    """
    by_jira = {}
    by_github = {}
    merged = []
    for c in candidates:
        aid = c.get('jira_account_id') or ''
        login = (c.get('github_username') or '').lower()
        target = None
        if aid and aid in by_jira:
            target = by_jira[aid]
        elif login and login in by_github:
            target = by_github[login]
        if target is None:
            entry = dict(c)
            entry['sources'] = [c['source']]
            merged.append(entry)
            if aid:
                by_jira[aid] = entry
            if login:
                by_github[login] = entry
        else:
            if aid and not target['jira_account_id']:
                target['jira_account_id'] = aid
                by_jira[aid] = target
            if c.get('github_username') and not target['github_username']:
                target['github_username'] = c['github_username']
                by_github[c['github_username'].lower()] = target
            if c.get('name') and not target['name']:
                target['name'] = c['name']
            target['sources'].append(c['source'])
    return merged


def _enrich_names(merged):
    """Fill in display names from GitHub for github-only entries."""
    for m in merged:
        if not m.get('name') and m.get('github_username'):
            m['name'] = _gh_display_name(m['github_username']) or m['github_username']


def _append_entries(new_members):
    """Append new entries into team_config.yaml, preserving existing formatting.

    We insert after the last indented line of the team_members: block (and
    before the next top-level section) so downstream sections stay intact.
    """
    content = CONFIG_PATH.read_text()
    lines = content.splitlines(keepends=True)

    team_idx = None
    for i, line in enumerate(lines):
        if line.startswith('team_members:'):
            team_idx = i
            break
    if team_idx is None:
        raise RuntimeError("Couldn't find team_members: section in config")

    last_member_line = team_idx
    for i in range(team_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if line[0] in (' ', '\t'):
            last_member_line = i
        else:
            break

    blocks = []
    for m in new_members:
        parts = [f'  - name: "{m["name"]}"']
        if m.get('jira_account_id'):
            parts.append(f'    jira_account_id: "{m["jira_account_id"]}"')
        if m.get('github_username'):
            parts.append(f'    github_username: "{m["github_username"]}"')
        sources = ', '.join(sorted(set(m.get('sources', []))))
        parts.append(f'    # auto-added via {sources}')
        blocks.append('\n'.join(parts))

    insertion = '\n\n' + '\n\n'.join(blocks) + '\n'
    new_content = (
        ''.join(lines[:last_member_line + 1])
        + insertion
        + ''.join(lines[last_member_line + 1:])
    )
    CONFIG_PATH.write_text(new_content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview additions without writing.')
    parser.add_argument('--max', type=int, default=20,
                        help='Safety rail: refuse to add more than N at once.')
    args = parser.parse_args()

    config = load_config()
    setup_logging(config)
    db_path = config['database']['path']

    known = _load_known(config)
    logger.info("Known: %d jira_ids, %d github_logins",
                len(known['jira']), len(known['github']))

    candidates = (
        _discover_jira(db_path, known)
        + _discover_calendar(db_path, known)
        + _discover_github_authors(db_path, known)
        + _discover_github_reviewers(db_path, known)
    )
    logger.info("Raw candidates across all signals: %d", len(candidates))

    merged = _merge(candidates)
    logger.info("Unique new members (after merging by id/login): %d", len(merged))

    if not merged:
        logger.info("No new members to add.")
        return 0

    _enrich_names(merged)

    for m in sorted(merged, key=lambda x: (x.get('name') or x.get('github_username') or '').lower()):
        logger.info(
            "  + %s  jira=%s  github=%s  sources=%s",
            m.get('name') or '(unknown)',
            m.get('jira_account_id') or '—',
            m.get('github_username') or '—',
            ', '.join(sorted(set(m['sources']))),
        )

    if len(merged) > args.max:
        logger.error(
            "Would add %d members (--max=%d). Refusing. Re-run with --max %d to override.",
            len(merged), args.max, len(merged),
        )
        return 2

    if args.dry_run:
        logger.info("DRY RUN: not writing.")
        return 0

    _append_entries(merged)
    logger.info("Appended %d new members to %s", len(merged), CONFIG_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
