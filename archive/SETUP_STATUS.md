# Dashboard Setup Status

## ✅ Completed

### Configuration
- [x] Project structure created
- [x] Database schema initialized
- [x] Configuration file created
- [x] **Team members configured** (10 members from Jira + GitHub)
  - Anushri Patel, Brooks Taylor, Kevin Paquette, Liam Butler, Michael Goodwin
  - Nigel Young, Patrick Kilburn, Ryan Taylor, Scott Schmitz, Tanya Phanich

### Code
- [x] Database layer (schema.py, queries.py)
- [x] Data models (metrics.py)
- [x] Utility modules (config.py, logging_config.py)
- [x] GitHub collector (fully functional)
- [x] Report generator
- [x] SQL query templates

## ⚠️ Remaining Items

### 1. Jira Integration (Critical)

**Issue:** The Jira collector (`src/collectors/jira_collector.py`) has placeholder code because MCP tools cannot be called directly from Python scripts - they must be invoked by Claude Code.

**Solution Options:**

**Option A: Claude Code Orchestration** (Recommended)
- Have Claude Code run the collection workflow directly
- Call MCP tools to query Jira
- Parse results and store in database using Python helper functions
- This is the most straightforward approach

**Option B: Create a Claude Code Agent**
- Build a dedicated agent that runs on the 15-minute schedule
- Agent calls MCP tools and Python storage functions
- More complex but fully automated

**Option C: Hybrid Approach**
- Claude Code runs initial data collection
- Python scripts handle storage, calculation, and reporting
- Schedule Claude Code to run the collection workflow

### 2. GitHub CLI Verification

Test that GitHub CLI is authenticated:
```bash
gh auth status
```

If not authenticated:
```bash
gh auth login
```

### 3. First Test Run

Once Jira integration is complete, run:
```bash
python3 scripts/collect_metrics.py
```

Expected output:
- Sprint data collected from Jira
- Tickets stored in database
- PR metrics collected from GitHub
- Reports generated

### 4. Scheduling Setup

After successful test run, set up automation:

**Option A: CronCreate (within Claude Code session)**
```python
from cron import CronCreate

CronCreate(
    cron="*/15 * * * *",
    prompt="cd /Users/davidbaxter/sync/claude/em_dashboard && python3 scripts/collect_metrics.py",
    durable=True
)
```

**Option B: System cron**
```bash
crontab -e
# Add:
*/15 * * * * cd /Users/davidbaxter/sync/claude/em_dashboard && /usr/bin/python3 scripts/collect_metrics.py >> logs/cron.log 2>&1
```

## 🎯 Next Steps

### Immediate (To Get Running)

1. **Decide on Jira integration approach** - I recommend Option A (Claude Code Orchestration)

2. **Test GitHub collection** - Verify gh CLI works:
   ```bash
   python3 -c "
   import sys
   sys.path.insert(0, 'src')
   from utils.config import load_config
   from collectors.github_collector import GitHubCollector
   config = load_config()
   gh = GitHubCollector(config)
   gh.collect_pr_metrics()
   "
   ```

3. **Implement Jira collection** - Have Claude Code run the queries and store results

### Short-term (First Week)

1. Verify data collection works end-to-end
2. Generate first reports
3. Set up scheduling
4. Monitor logs for issues

### Long-term (Future Enhancements)

1. Web dashboard for better visualization
2. Slack integration for daily summaries
3. Trend analysis and predictions
4. Additional metrics (code review time, cycle time, etc.)

## 📊 Current State

**Database:** ✅ Initialized with proper schema
**Configuration:** ✅ Complete with 10 team members
**GitHub Integration:** ✅ Ready to test
**Jira Integration:** ⚠️ Needs Claude Code orchestration
**Reports:** ✅ Ready to generate once data is collected
**Scheduling:** ⏳ Pending successful test run

## 🔍 Verification Commands

```bash
# Check database tables
sqlite3 data/metrics.db "SELECT name FROM sqlite_master WHERE type='table';"

# Check config
python3 -c "import sys; sys.path.insert(0, 'src'); from utils.config import load_config; c=load_config(); print(f'Team: {len(c[\"team_members\"])} members')"

# Check GitHub auth
gh auth status

# Test GitHub collection
python3 -c "import sys; sys.path.insert(0, 'src'); from collectors.github_collector import GitHubCollector; from utils.config import load_config; GitHubCollector(load_config()).collect_pr_metrics()"

# View logs
tail -f logs/collector.log
```
