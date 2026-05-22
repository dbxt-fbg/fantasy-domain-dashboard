# Engineering Management Dashboard

Automated dashboard for tracking team metrics, ticket hygiene, and sprint progress from Jira and GitHub.

## Features

### Hygiene Tracking
- Epic hygiene rules (missing descriptions, no child stories, missing acceptance criteria)
- Feature hygiene (missing requirements, designs, launch phase)
- Story hygiene (missing points, descriptions, parent epics)
- Code review time tracking
- HTML dashboard with drill-down details

### Team Metrics (Jira)
- Sprint progress and burndown
- Team velocity
- Ticket status distribution
- Individual developer metrics

### GitHub Metrics
- Open PR count per developer
- Average time to merge
- PR activity tracking

### Automation
- Hygiene checks every 15 minutes (6am-6pm)
- Historical data stored for trend analysis
- Automated HTML report generation

## Quick Start

### 1. Install Dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Configure Credentials

Set up Jira API credentials:
```bash
export JIRA_EMAIL="your-email@company.com"
export JIRA_API_TOKEN="your-api-token"
```

Or create `config/.env`:
```
JIRA_EMAIL=your-email@company.com
JIRA_API_TOKEN=your-api-token
```

**Getting a Jira API Token:**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Give it a name and copy the token

### 3. Configure Team Members

Edit `config/team_config.yaml` to add your team:

```yaml
team_members:
  - name: "Alice Johnson"
    jira_account_id: "557058:f1234567-89ab-cdef-0123-456789abcdef"
    github_username: "alice-johnson"
```

**Finding Jira Account IDs:**
- Query any ticket and look at the assignee field in the API response
- Or use: `https://betfanatics.atlassian.net/rest/api/3/user/search?query=<name>`

### 4. Initialize Database

```bash
python3 scripts/init_database.py
```

## Usage

### Run Hygiene Checks

```bash
# Collect hygiene data
python3 scripts/jira_hygiene_agent.py

# Generate dashboard
python3 scripts/generate_hygiene_dashboard.py

# View report
open reports/html/hygiene_dashboard.html
```

### Run Sprint Metrics Collection

```bash
# Collect sprint data
python3 scripts/jira_collector_agent.py

# Generate reports
python3 scripts/generate_html_report.py

# View reports
open reports/html/epics_dashboard.html
```

### Query Data Directly

```bash
# View hygiene issues
sqlite3 data/metrics.db "SELECT * FROM hygiene_issues ORDER BY issue_type, ticket_key;"

# View sprint snapshots
sqlite3 data/metrics.db "SELECT * FROM sprint_snapshots ORDER BY snapshot_date DESC LIMIT 10;"

# View tickets
sqlite3 data/metrics.db "SELECT ticket_key, status, summary FROM tickets ORDER BY updated_at DESC LIMIT 20;"
```

## Project Structure

```
em_dashboard/
├── config/
│   ├── team_config.yaml          # Team configuration
│   └── .env                       # Credentials (not in git)
├── data/
│   └── metrics.db                 # SQLite database
├── src/
│   ├── database/
│   │   ├── schema.py              # Main schema (sprints, tickets, etc.)
│   │   ├── hygiene_schema.py      # Hygiene tracking tables
│   │   └── queries.py             # Query helpers
│   ├── collectors/
│   │   ├── jira_api_collector.py  # Jira REST API client
│   │   ├── github_collector.py    # GitHub metrics
│   │   └── calendar_collector.py  # Google Calendar 1:1s
│   ├── models/
│   │   └── metrics.py             # Data models
│   └── utils/
│       ├── config.py              # Config loading
│       └── logging_config.py      # Logging setup
├── scripts/
│   ├── init_database.py           # Initialize schema
│   ├── jira_hygiene_agent.py      # Hygiene checker
│   ├── jira_collector_agent.py    # Sprint data collector
│   ├── generate_hygiene_dashboard.py  # Hygiene HTML reports
│   ├── generate_html_report.py    # Sprint HTML reports
│   ├── qa_hygiene_validator.py    # Validate hygiene results
│   └── deprecated/                # Old scripts (for reference)
├── reports/
│   ├── html/                      # Generated HTML dashboards
│   └── *.md                       # Markdown reports (legacy)
└── logs/                          # Log files
```

## Active Scripts

### Data Collection
- **jira_hygiene_agent.py** - Check ticket hygiene issues
- **jira_collector_agent.py** - Collect sprint metrics
- **github_pr_agent.py** - Collect GitHub PR data
- **sync_calendar.py** - Sync 1:1 meetings from Google Calendar

### Report Generation
- **generate_hygiene_dashboard.py** - Create hygiene HTML dashboard
- **generate_html_report.py** - Create sprint/team HTML dashboards
- **generate_logs_dashboard.py** - Create logs analysis dashboard

### Utilities
- **init_database.py** - Initialize database schema
- **backfill_snapshots.py** - Backfill historical data
- **qa_hygiene_validator.py** - Validate hygiene checks for false positives
- **refresh_jira_data.py** - Refresh cached Jira data

## Database Schema

### Main Tables
- **sprints** - Active and historical sprints
- **sprint_snapshots** - Daily sprint metrics (for burndown)
- **tickets** - Individual ticket details with URLs
- **ticket_status_history** - Status change tracking
- **developer_snapshots** - Daily developer metrics
- **developer_velocity** - Velocity per sprint per developer

### Hygiene Tables
- **hygiene_issues** - Current hygiene violations
- **status_changes** - Track time in specific statuses (e.g., Code Review)

### GitHub Tables
- **github_pr_snapshots** - Point-in-time PR counts
- **github_prs** - Individual PR records

### Calendar Tables
- **one_on_one_meetings** - Recurring 1:1 schedules

## Configuration

### Hygiene Settings

Edit `config/team_config.yaml` to customize hygiene checks:

```yaml
hygiene:
  # Epics to ignore (e.g., onboarding tickets)
  ignored_epics:
    - FNTSY-368
    - FNTSY-373
```

### Story Points Fields

If your Jira uses different story points fields:

```yaml
jira:
  story_points_field: "customfield_10016"
  story_points_fallback_fields:
    - "customfield_10026"
    - "customfield_10028"
```

## Automation

The project uses Claude Code's scheduled tasks for automation.

**Current Schedule:**
- Hygiene checks: Every 15 minutes, 6am-6pm Pacific
- Located in `.claude/scheduled_tasks.json`

To modify schedules, edit the JSON file or recreate tasks via Claude Code.

## Troubleshooting

### Jira Authentication Failed

```bash
# Check environment variables
echo $JIRA_EMAIL
echo $JIRA_API_TOKEN

# Test API access
curl -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "https://betfanatics.atlassian.net/rest/api/3/myself"
```

### No Data Collected

Check logs:
```bash
tail -f logs/collector.log
```

Verify config:
```bash
python3 -c "import sys; sys.path.insert(0, 'src'); from utils.config import load_config; c=load_config(); print(f'Team: {len(c[\"team_members\"])} members')"
```

### Wrong Story Points

Query a ticket to find the correct field:
```bash
curl -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "https://betfanatics.atlassian.net/rest/api/3/issue/FNTSY-123" | jq .fields
```

Update `story_points_field` in `config/team_config.yaml`.

### GitHub CLI Not Authenticated

```bash
gh auth status
gh auth login
```

## Development

### Running Tests

```bash
# Test hygiene validator
python3 scripts/qa_hygiene_validator.py

# Check for false positives
sqlite3 data/metrics.db "SELECT COUNT(*) FROM hygiene_issues WHERE issue_type = 'epics_no_work_items';"
```

### Adding New Hygiene Rules

1. Add rule check in `scripts/jira_hygiene_agent.py` → `check_hygiene_issues()`
2. Add to `hygiene_data` dict
3. Store issues with `_store_hygiene_issue()`
4. Update dashboard template in `scripts/generate_hygiene_dashboard.py`

## Recent Changes

- **v2.1** - Moved ignored epics to config file
- **v2.0** - Fixed "Epics without Child Stories" false positives
- **v1.9** - Removed deprecated burndown agent
- **v1.8** - Cleaned up duplicate Jira collectors

## Documentation

- **HYGIENE_AGENT_SETUP.md** - Hygiene tracking details
- **CALENDAR_INTEGRATION.md** - Google Calendar setup
- **AUTOMATION_GUIDE.md** - Scheduling and automation
- **PROJECT_REVIEW.md** - Architecture review and issues

## License

MIT
