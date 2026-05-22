# Google Calendar Integration

## Overview

The dashboard automatically syncs recurring 1-on-1 meetings from your Google Calendar and displays the meeting schedule for each team member in their performance card.

## Setup Complete ✅

Your Google Calendar integration is fully configured with:
- OAuth authentication (credentials stored in `config/google_credentials.json`)
- Token stored in `config/token.pickle` (refreshes automatically)
- Daily sync at midnight via cron job
- Meeting info displayed on each team member's dashboard card

## What Gets Synced

The system automatically identifies recurring 1-on-1 meetings by:
1. Looking for recurring events (not one-time meetings)
2. Matching team member names in event titles
3. Looking for keywords like "1-on-1", "1:1", "sync", "check-in"
4. Checking attendee lists for team members

## Current Meeting Schedule

Based on your calendar, the following 1-on-1s were found:

| Team Member | Meeting | Day | Time | Duration |
|-------------|---------|-----|------|----------|
| Brooks Taylor | Brooks / Bax | Wednesday | 12:00 PM | 30 min |
| Kevin Paquette | Kevin / David | Monday | 11:00 AM | 30 min |
| Liam Butler | Liam / Bax | Thursday | 12:30 PM | 30 min |
| Michael Goodwin | Michael / Bax | Tuesday | 12:00 PM | 30 min |
| Nigel Young | Nigel / Bax | Thursday | 11:30 AM | 30 min |
| Patrick Kilburn | Patrick / Bax | Wednesday | 11:30 AM | 30 min |
| Ryan Taylor | Ryan / David | Thursday | 11:00 AM | 30 min |
| Tanya Phanich | Tanya / Bax | Monday | 12:00 PM | 30 min |

## Dashboard Display

On each team member's performance card in the Projections section, you'll see:

```
📅 Next 1-on-1
Wednesday at 11:30 AM          Duration: 30 min
Next: May 06
```

This shows:
- Day of week and time
- Meeting duration
- Date of next scheduled occurrence

## Manual Sync

To manually sync calendar data at any time:

```bash
python3 scripts/sync_calendar.py
```

Or ask Claude Code: "Sync Google Calendar meetings"

## Automated Sync

The calendar syncs automatically every day at midnight via cron:

```
0 0 * * * /Users/davidbaxter/.pyenv/shims/python3 /Users/davidbaxter/sync/claude/em_dashboard/scripts/sync_calendar.py >> /Users/davidbaxter/sync/claude/em_dashboard/logs/calendar_sync.log 2>&1
```

Check sync logs:
```bash
tail -f /Users/davidbaxter/sync/claude/em_dashboard/logs/calendar_sync.log
```

## Database Schema

Meeting data is stored in the `one_on_one_meetings` table:

```sql
CREATE TABLE one_on_one_meetings (
    meeting_id INTEGER PRIMARY KEY,
    developer_name TEXT NOT NULL,
    jira_account_id TEXT,
    github_username TEXT,
    event_id TEXT,
    summary TEXT NOT NULL,
    recurrence_rule TEXT,
    day_of_week TEXT,
    time_of_day TEXT,
    duration_minutes INTEGER,
    next_occurrence TEXT,
    last_synced_at TEXT NOT NULL,
    UNIQUE(developer_name)
);
```

## Troubleshooting

### Re-authenticate with Google

If you need to re-authenticate (e.g., token expired):

```bash
rm /Users/davidbaxter/sync/claude/em_dashboard/config/token.pickle
python3 scripts/sync_calendar.py
```

This will open a browser window for OAuth.

### Check What Was Synced

```bash
sqlite3 /Users/davidbaxter/sync/claude/em_dashboard/data/metrics.db "
SELECT developer_name, summary, day_of_week, time_of_day, duration_minutes 
FROM one_on_one_meetings 
ORDER BY developer_name;
"
```

### Meeting Not Detected

If a 1-on-1 meeting isn't being detected:

1. **Ensure it's recurring**: One-time meetings are ignored
2. **Check the title**: Should contain team member's name or keywords like "1-on-1"
3. **Verify it's in the next 30 days**: Events further out aren't scanned
4. **Check attendees**: For keyword-based detection, verify there's exactly 1 attendee

### Manual Database Update

To manually add a meeting:

```bash
sqlite3 /Users/davidbaxter/sync/claude/em_dashboard/data/metrics.db "
INSERT INTO one_on_one_meetings (developer_name, summary, day_of_week, time_of_day, duration_minutes, last_synced_at)
VALUES ('Developer Name', 'Weekly 1-on-1', 'Monday', '10:00 AM', 30, datetime('now'));
"
```

## Privacy & Permissions

The integration:
- Only requests **read-only** access to your calendar (`calendar.readonly` scope)
- Only looks at events in the next 30 days
- Only syncs recurring meetings that match team member patterns
- Stores minimal data: meeting title, schedule, and timing
- Does not sync meeting notes, attendees (other than matching), or other details

## Updating Team Members

If you add/remove team members in `config/team_config.yaml`, the next calendar sync will automatically update the matching logic. No additional configuration needed.
