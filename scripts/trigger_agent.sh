#!/bin/bash
#
# Manual Agent Trigger Script
# Usage: ./trigger_agent.sh <agent_id>
#
# Agent IDs match the `trigger` field on each card in the Agents & Logs page.
#

cd "$(dirname "$0")/.." || exit 1

AGENT=$1
PYTHON=/Users/davidbaxter/.pyenv/shims/python3

case "$AGENT" in
    jira-collector)
        echo "Running Jira Collector..."
        "$PYTHON" scripts/jira_collector_agent.py
        "$PYTHON" scripts/generate_html_report.py
        "$PYTHON" scripts/generate_hygiene_dashboard.py
        ;;
    hygiene|jira-hygiene)
        echo "Running Jira Hygiene Agent..."
        "$PYTHON" scripts/jira_hygiene_agent.py
        "$PYTHON" scripts/generate_hygiene_dashboard.py
        ;;
    qa)
        echo "Running QA Agent..."
        "$PYTHON" scripts/qa_agent.py
        ;;
    team-member|github-pr)
        echo "Running GitHub PR Agent..."
        "$PYTHON" scripts/github_pr_agent.py
        "$PYTHON" scripts/generate_html_report.py
        ;;
    calendar-sync)
        echo "Running Calendar Sync..."
        "$PYTHON" scripts/sync_calendar.py
        "$PYTHON" scripts/generate_html_report.py
        ;;
    project-fantasy)
        echo "Running Project: Fantasy Snapshot..."
        "$PYTHON" scripts/sync_project_fantasy.py
        "$PYTHON" scripts/generate_html_report.py
        ;;
    logs)
        echo "Regenerating Agents & Logs page..."
        ;;
    *)
        echo "Unknown agent id: $AGENT"
        echo "Valid ids: jira-collector, hygiene, qa, github-pr, calendar-sync, project-fantasy, logs"
        exit 1
        ;;
esac

# Always regenerate the logs dashboard so last-run times update
"$PYTHON" scripts/generate_logs_dashboard.py

echo "Agent run complete!"
