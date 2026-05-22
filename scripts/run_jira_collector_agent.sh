#!/bin/bash
set -o pipefail

# Jira Collector Agent - fetches all FNTSY tickets, sprints, snapshots.
# Wired to crontab every 15 min during work hours.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LOG_DIR="$SCRIPT_DIR/../logs"
LOG_FILE="$LOG_DIR/jira_collector_agent.log"

mkdir -p "$LOG_DIR"

# shellcheck source=lib/rotate_log.sh
. "$SCRIPT_DIR/lib/rotate_log.sh"

echo "=== Jira Collector starting at $(date) ===" >> "$LOG_FILE"
cd "$SCRIPT_DIR/.."
/Users/davidbaxter/.pyenv/shims/python3 scripts/jira_collector_agent.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Jira Collector completed successfully" >> "$LOG_FILE"
    echo "Regenerating HTML reports..." >> "$LOG_FILE"
    /Users/davidbaxter/.pyenv/shims/python3 scripts/generate_html_report.py >> "$LOG_FILE" 2>&1
    if [ $? -eq 0 ]; then
        echo "✅ HTML reports regenerated" >> "$LOG_FILE"
    else
        echo "❌ HTML report generation failed" >> "$LOG_FILE"
    fi
else
    echo "❌ Jira Collector failed with exit code $EXIT_CODE" >> "$LOG_FILE"
fi

rotate_log "$LOG_FILE" 5000
