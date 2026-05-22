# Jira Hygiene Agent

## Overview

The Jira Hygiene Agent automatically checks for ticket hygiene issues every 15 minutes during business hours (6am-6pm Pacific).

## What It Checks

1. **Epics without parent Feature** - Epics that have no parent
2. **Epics in progress without description** - Active epics missing descriptions
3. **Stories in progress without parent Epic** - Active stories not linked to an Epic
4. **Stories in progress without story points** - Active stories missing estimates
5. **Stories in progress without description** - Active stories missing descriptions
6. **Tickets in Code Review > 24 hours** - PRs stuck in review

## How It Works

### Status Tracking
- Tracks when tickets enter/exit statuses in the `status_changes` table
- Calculates how long tickets have been in "In Code Review" or "In code review"
- Updates automatically on every run

### Hygiene Checks
- Fetches all FNTSY project tickets
- Applies hygiene rules
- Stores issues in `hygiene_issues` table
- Generates HTML dashboard

### Schedule
- **Runs**: Every 15 minutes
- **Hours**: 6:00am - 5:45pm Pacific time
- **Days**: 7 days a week
- **Auto-expires**: After 7 days (needs renewal)

## Database Schema

### status_changes table
```sql
ticket_key      TEXT    -- Jira ticket key
status          TEXT    -- Status name
entered_at      TIMESTAMP -- When ticket entered this status
exited_at       TIMESTAMP -- When ticket left this status (NULL if still in it)
```

### hygiene_issues table
```sql
issue_type              TEXT    -- Type of hygiene issue
ticket_key              TEXT    -- Jira ticket key  
ticket_summary          TEXT    -- Ticket summary
ticket_url              TEXT    -- Link to Jira
assignee_display_name   TEXT    -- Who it's assigned to
status                  TEXT    -- Current status
details                 TEXT    -- Issue details
detected_at             TIMESTAMP -- When detected
```

## Viewing Results

### Hygiene Dashboard
Open: `reports/html/hygiene_dashboard.html`

Or click "Ticket Hygiene" in the navigation on the Team Dashboard.

### Dashboard Features
- **Metric Cards**: Click any card to see the list of issues
- **Issue Lists**: Shows all affected tickets with:
  - Ticket key (clickable link to Jira)
  - Summary
  - Assignee
  - Status
  - Time in code review (for code review issues)
- **Clean State**: Cards turn green when no issues found

## Manual Run

Test or run the hygiene check manually:

```bash
./scripts/run_hygiene_check.sh
```

Or run components separately:

```bash
# Check hygiene issues
python3 scripts/jira_hygiene_agent.py

# Generate dashboard
python3 scripts/generate_hygiene_dashboard.py
```

## Cron Schedule

The agent is scheduled with Claude Code's CronCreate:

```
Schedule: */15 6-17 * * *  (every 15 minutes, 6am-5pm)
Job ID: bce68b80
Durable: Yes (persisted across sessions)
Auto-expires: After 7 days
```

### Renew the schedule

After 7 days, the cron job auto-expires. To renew:

Ask Claude Code:
> "Renew the Jira Hygiene Agent cron job for another 7 days"

Or create manually:
```python
CronCreate(
    cron="*/15 6-17 * * *",
    prompt="Run the Jira Hygiene Agent: cd /Users/davidbaxter/sync/claude/em_dashboard && python3 scripts/jira_hygiene_agent.py && python3 scripts/generate_hygiene_dashboard.py",
    durable=True
)
```

## Logs

Check agent logs:
```bash
tail -f logs/collector.log | grep -i hygiene
```

## Troubleshooting

### No data showing
- Check that Jira credentials are set in `config/.env`
- Run manually to see errors: `python3 scripts/jira_hygiene_agent.py`

### Status tracking not working
- Status changes are tracked from the first run forward
- Historical data before first run is not available
- Give it 24+ hours to accumulate "In Code Review" time data

### Cron not running
- Check schedule: `CronList()`
- Job auto-expires after 7 days - renew it
- Only runs 6am-6pm Pacific time

### Parent field not found
- Agent auto-discovers the "Parent" custom field
- Falls back to common field IDs: customfield_10014, customfield_10008, parent
- Check logs for which field was discovered

## Architecture

```
jira_hygiene_agent.py
├── Fetches all FNTSY tickets via Jira API
├── Tracks status changes in database
├── Applies 6 hygiene rules
└── Stores issues in hygiene_issues table

generate_hygiene_dashboard.py
├── Reads hygiene_issues from database
├── Generates HTML with counts and lists
└── Saves to reports/html/hygiene_dashboard.html

Cron Job (every 15 min, 6am-6pm)
└── Runs both scripts sequentially
```

## Future Enhancements

Potential additions:
- Email/Slack notifications for critical issues
- Trend tracking (issues over time)
- Per-developer hygiene scores
- Custom hygiene rules
- Integration with team dashboard metrics
