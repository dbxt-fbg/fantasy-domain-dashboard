# Dashboard Automation Guide

## Current Automation Setup

Your dashboard has **two types of data** that refresh differently:

### ✅ Automated via Cron (GitHub Data)
- GitHub PR metrics
- PR merge times
- Open PR counts

### 🔧 Manual via Claude Code (Jira Data)
- Sprint tickets
- Team member assignments
- Ticket statuses
- Hygiene violations

## Why Jira Can't Be Fully Automated

The Jira integration uses **MCP (Model Context Protocol) tools** that are specific to Claude Code and require authentication/session context. Standard cron jobs can't access these tools.

## Recommended Setup: Hybrid Automation

### Step 1: Set Up Cron for GitHub Data (Every 15 Minutes)

```bash
# Edit your crontab
crontab -e

# Add this line (updates GitHub data every 15 minutes):
*/15 * * * * /Users/davidbaxter/sync/claude/em_dashboard/scripts/refresh_dashboard.sh >> /Users/davidbaxter/sync/claude/em_dashboard/logs/cron.log 2>&1
```

**What this does:**
- ✅ Refreshes GitHub PR metrics automatically
- ✅ Regenerates HTML reports with latest data
- ✅ Runs every 15 minutes
- ✅ Logs output for debugging

### Step 2: Manually Refresh Jira Data (As Needed)

When you want fresh Jira data, ask Claude Code:

```
"Collect latest Jira data"
```

**Claude will:**
1. Query Jira for active sprint tickets
2. Query for hygiene violations
3. Process and store in database
4. Regenerate reports automatically

**Recommended frequency:**
- **Daily** for active sprints
- **2-3 times per week** during quieter periods
- **Before important meetings** (standups, reviews)

## Alternative Options

### Option A: Full Manual Refresh (No Cron)

If you prefer full control, skip the cron job and manually refresh everything:

```bash
# When you want updated data:
./scripts/refresh_dashboard.sh

# Then ask Claude: "Collect latest Jira data"
```

### Option B: Claude Code Schedule (If Available)

If Claude Code supports scheduled prompts in the future:

```python
# This would be the ideal automation
CronCreate(
    cron="*/15 * * * *",
    prompt="Collect latest Jira data and refresh GitHub metrics",
    durable=True
)
```

**Note:** Currently, this would require Claude Code to be running continuously.

## Current Status Check

Verify your setup:

```bash
# Check if cron job is installed
crontab -l | grep refresh_dashboard

# Test manual refresh
./scripts/refresh_dashboard.sh

# Check last update time
sqlite3 data/metrics.db "SELECT MAX(snapshot_timestamp) FROM sprint_snapshots;"
sqlite3 data/metrics.db "SELECT MAX(snapshot_timestamp) FROM github_pr_snapshots;"
```

## Data Freshness Indicators

Your dashboard shows generation timestamps:
- **Top of page:** "Generated April 24, 2026 at 15:21"
- This tells you when reports were last created
- Compare with current time to know data age

### What Gets Updated When

| Data Type | Updated By | Frequency | Notes |
|-----------|------------|-----------|-------|
| GitHub PRs | Cron | Every 15 min | Automatic if cron enabled |
| Sprint tickets | Manual | On demand | Ask Claude Code |
| Burndown chart | Manual | Daily recommended | Needs Jira data |
| Hygiene violations | Manual | Weekly recommended | Needs Jira data |
| Projections | Automatic | When reports regenerate | Uses cached data |
| Team metrics | Manual | Daily recommended | Needs Jira data |

## Recommended Workflow

### Daily Routine (5 minutes)
1. Morning: Ask Claude Code to "Collect latest Jira data"
2. Dashboard auto-refreshes throughout the day (GitHub data via cron)
3. Check Projections tab before standup

### Weekly Routine (10 minutes)
1. Monday morning: Full refresh (Jira + hygiene)
2. Check Ticket Hygiene tab
3. Review team projections
4. Plan 1-on-1s based on concerns

### Before Important Meetings
Ask Claude Code: "Collect latest Jira data and regenerate dashboard"

## Troubleshooting

### Cron Job Not Running?

```bash
# Check cron service (macOS)
sudo launchctl list | grep cron

# View recent cron logs
tail -f logs/cron.log
```

### Dashboard Shows Old Data?

```bash
# Check when data was last collected
ls -la data/metrics.db

# Manually trigger full refresh
./scripts/refresh_dashboard.sh
# Then ask Claude: "Collect latest Jira data"
```

### GitHub CLI Not Working in Cron?

```bash
# Ensure gh is in PATH for cron
which gh

# Add to crontab if needed:
PATH=/usr/local/bin:/usr/bin:/bin
*/15 * * * * /Users/davidbaxter/sync/claude/em_dashboard/scripts/refresh_dashboard.sh
```

## Future Improvements

Once we solve the MCP automation challenge, we could:
1. Build a lightweight Jira API wrapper (no MCP dependency)
2. Use Jira webhooks for real-time updates
3. Create a background service that runs continuously
4. Integrate with Jira REST API directly using tokens

## Summary

**Right now:**
- ✅ GitHub data: Automated every 15 minutes (set up cron)
- 🔧 Jira data: Manual via Claude Code (daily recommended)
- ✅ Reports: Auto-regenerate when data updates

**This hybrid approach gives you:**
- Fresh PR metrics throughout the day
- Control over when to query Jira
- Updated dashboards without manual report generation
- Minimal maintenance (just ask Claude for Jira updates)

Want me to set up the cron job for you now? 🚀
