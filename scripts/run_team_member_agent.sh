#!/bin/bash
set -o pipefail

# Team Member / GitHub PR Agent - refreshes PRs, reviews, comments.
# Wired to crontab every 15 min.

# cron runs with a minimal PATH that omits Homebrew, so the `gh` CLI
# (/opt/homebrew/bin/gh) isn't found and every PR fetch + the stale-open
# reconcile pass fails silently (the agent catches the error and still
# exits 0). Prepend the dirs where gh actually lives.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LOG_DIR="$SCRIPT_DIR/../logs"
LOG_FILE="$LOG_DIR/github_pr_agent.log"

mkdir -p "$LOG_DIR"

# shellcheck source=lib/rotate_log.sh
. "$SCRIPT_DIR/lib/rotate_log.sh"

echo "=== GitHub PR Agent starting at $(date) ===" >> "$LOG_FILE"
cd "$SCRIPT_DIR/.."
/Users/davidbaxter/.pyenv/shims/python3 scripts/github_pr_agent.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ GitHub PR Agent completed successfully" >> "$LOG_FILE"
else
    echo "❌ GitHub PR Agent failed with exit code $EXIT_CODE" >> "$LOG_FILE"
fi

rotate_log "$LOG_FILE" 5000
