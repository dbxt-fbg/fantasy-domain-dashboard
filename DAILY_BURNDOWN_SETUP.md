# Daily Burndown Agent Setup

## Step 1: Create Jira API Token

1. Go to: https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Give it a name like "EM Dashboard"
4. Copy the token (you won't be able to see it again!)

## Step 2: Configure Credentials

Create `config/.env` file with your credentials:

```bash
cd /Users/davidbaxter/sync/claude/em_dashboard
cp config/.env.example config/.env
```

Edit `config/.env` and add your real credentials:

```
JIRA_EMAIL=your.email@betfanatics.com
JIRA_API_TOKEN=your_actual_token_here
JIRA_DOMAIN=betfanatics.atlassian.net
```

**Important:** Never commit this file to git! It's already in .gitignore.

## Step 3: Install Dependencies

```bash
pip3 install -r requirements.txt
```

## Step 4: Test the Agent

Test that the agent can connect to Jira:

```bash
python3 scripts/daily_burndown_agent.py
```

You should see it fetch data from Jira and store a daily snapshot.

## Step 5: Schedule with Cron

The agent is designed to run at midnight Pacific time.

**Option A: Using Claude Code CronCreate**

Ask Claude Code:
> "Create a cron job to run scripts/daily_burndown_agent.py every day at midnight Pacific time"

**Option B: Using System Cron**

```bash
# Edit crontab
crontab -e

# Add this line (runs at midnight Pacific)
0 0 * * * cd /Users/davidbaxter/sync/claude/em_dashboard && /usr/bin/python3 scripts/daily_burndown_agent.py >> logs/burndown_agent.log 2>&1
```

## How It Works

1. **At midnight** (Pacific time), the agent runs
2. **Connects to Jira** using your API token
3. **Fetches active sprint data** (all open sprint issues)
4. **Stores daily snapshot** in the database
5. **Retries every 5 minutes** if it fails (up to 1 hour)
6. **Preserves historical data** - each day's snapshot is kept

## Verify It's Working

Check the logs:

```bash
tail -f logs/burndown_agent.log
```

Check the database:

```bash
sqlite3 data/metrics.db "SELECT snapshot_date, open_tickets, closed_tickets FROM sprint_snapshots WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE state = 'active') ORDER BY snapshot_date DESC LIMIT 7;"
```

You should see one snapshot per day.

## Troubleshooting

### "Jira credentials not found"
- Make sure `config/.env` exists with correct credentials
- Check file permissions: `ls -la config/.env`

### "Authentication failed"
- Verify your Jira email and API token are correct
- Make sure the API token hasn't expired

### "No issues found"
- Check that the sprint prefix is correct in `config/team_config.yaml`
- Verify there's an active sprint in Jira

### Agent not running
- Check cron logs: `grep CRON /var/log/system.log` (macOS)
- Make sure the path in crontab is absolute
- Check that Python can be found: `which python3`

## Security Notes

- ✅ API token is stored in `config/.env` (not in git)
- ✅ `.env` is in `.gitignore`
- ✅ File permissions should be `600` (read/write for owner only)
- ✅ Use environment variables for production deployments

Set secure permissions:
```bash
chmod 600 config/.env
```
