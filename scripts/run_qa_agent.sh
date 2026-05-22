#!/bin/bash
set -o pipefail

# QA Agent - Runs data quality checks every 5 minutes
# Runs 24/7 to continuously monitor dashboard data quality

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LOG_DIR="$SCRIPT_DIR/../logs"
LOG_FILE="$LOG_DIR/qa_agent.log"

mkdir -p "$LOG_DIR"

# shellcheck source=lib/rotate_log.sh
. "$SCRIPT_DIR/lib/rotate_log.sh"

echo "=== QA Agent starting at $(date) ===" >> "$LOG_FILE"
cd "$SCRIPT_DIR/.."
/Users/davidbaxter/.pyenv/shims/python3 scripts/qa_agent.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ QA Agent completed successfully" >> "$LOG_FILE"
else
    echo "❌ QA Agent failed with exit code $EXIT_CODE" >> "$LOG_FILE"
fi

rotate_log "$LOG_FILE" 5000
rotate_log "$LOG_DIR/cron.log" 10000
