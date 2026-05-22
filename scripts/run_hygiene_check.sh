#!/bin/bash
set -e
set -o pipefail

# Combined script to run hygiene check and generate dashboard

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LOG_DIR="$SCRIPT_DIR/../logs"

cd "$SCRIPT_DIR/.."

# shellcheck source=lib/rotate_log.sh
. "$SCRIPT_DIR/lib/rotate_log.sh"

echo "Running Jira Hygiene Check..."
/Users/davidbaxter/.pyenv/shims/python3 scripts/jira_hygiene_agent.py

echo "Generating Hygiene Dashboard..."
/Users/davidbaxter/.pyenv/shims/python3 scripts/generate_hygiene_dashboard.py

# Rotate the shared collector log (hygiene logs here) and our own log.
rotate_log "$LOG_DIR/collector.log" 5000
rotate_log "$LOG_DIR/jira_hygiene_agent.log" 5000

echo "✅ Hygiene check complete!"
echo "View dashboard: reports/html/hygiene_dashboard.html"
