# EM Dashboard Project Review
**Date:** May 8, 2026  
**Reviewer:** Claude Code

## Executive Summary

The EM Dashboard project is functional but has significant **architectural inconsistencies**, **abandoned code**, **duplicate implementations**, and **documentation drift**. The project successfully collects hygiene metrics but has **two parallel Jira collection systems** (one unused) and **multiple orphaned database files**.

**Severity:**
- 🔴 Critical: 2 issues
- 🟡 Medium: 8 issues  
- 🟢 Low: 5 issues

---

## 🔴 Critical Issues

### 1. **Broken Scheduled Task References Non-Existent Script**
**File:** `.claude/scheduled_tasks.json`  
**Issue:** Daily cron job references `scripts/daily_burndown_agent.py` which doesn't exist (deprecated)
```json
{
  "cron": "0 0 * * *",
  "prompt": "...python3 scripts/daily_burndown_agent.py..."
}
```
**Impact:** Cron task fails daily  
**Fix:** Remove or update to reference `generate_html_report.py` which handles burndown

### 2. **Unused JiraCollector Has NotImplementedError**
**File:** `src/collectors/jira_collector.py`  
**Issue:** The `_query_jira_mcp()` method raises `NotImplementedError` - this entire 489-line module is dead code
```python
def _query_jira_mcp(self, jql: str, ...) -> Dict[str, Any]:
    raise NotImplementedError(
        "This method should be replaced with actual MCP tool call..."
    )
```
**Impact:** Confuses maintainers; suggests broken features  
**Fix:** Either delete or clearly mark as template/example

---

## 🟡 Medium Priority Issues

### 3. **Database File Proliferation**
**Location:** `data/` directory  
**Issue:** 5 database files exist, but only 1 is actively used:
- ✅ `metrics.db` (2.4 MB, actively used)
- ❌ `dashboard.db` (0 bytes, empty)
- ❌ `em_dashboard.db` (0 bytes, empty)
- ❌ `em_metrics.db` (0 bytes, empty)
- ❌ `team_metrics.db` (0 bytes, empty)

**Impact:** Confusion about which DB to use, wasted disk space  
**Fix:** Delete unused DB files

### 4. **Duplicate Jira Collection Systems**
**Files:** `jira_collector.py` vs `jira_api_collector.py`  
**Issue:** Two complete implementations of Jira data collection:
- `jira_collector.py` (489 lines, unused, has NotImplementedError)
- `jira_api_collector.py` (working, used by 3 scripts)

**Impact:** Code bloat, maintenance confusion  
**Fix:** Delete `jira_collector.py` or move to deprecated/

### 5. **13 Deprecated Scripts Not Cleaned Up**
**Location:** `scripts/deprecated/`  
**Issue:** 13 old scripts (1,000+ lines total) kept in project:
- `collect_metrics.py` (now `jira_collector_agent.py`)
- `generate_report.py` (now `generate_html_report.py`)
- `daily_burndown_agent.py` (functionality moved)
- Multiple others with no clear deprecation date

**Impact:** Git history bloat, confusing project structure  
**Fix:** Archive to separate branch or delete (already in git history)

### 6. **Documentation-Reality Mismatch**
**File:** `README.md`  
**Issues:**
- Documents `scripts/collect_metrics.py` which is deprecated
- Shows project structure that doesn't match reality
- References Python imports that don't work
- Says "12 scripts" but has more

**Example from README:**
```bash
python3 scripts/collect_metrics.py  # This file is deprecated
```

**Fix:** Update README to reflect actual working scripts

### 7. **Hardcoded Ignore List in Code**
**File:** `scripts/jira_hygiene_agent.py:142`  
**Issue:** Epic ignore list hardcoded in Python:
```python
IGNORED_EPICS = {'FNTSY-368', 'FNTSY-373', 'FNTSY-383', 'FNTSY-385'}
```
**Impact:** Requires code change to update ignore list  
**Fix:** Move to `config/team_config.yaml` under `hygiene.ignored_epics`

### 8. **Inconsistent Story Points Field Handling**
**Files:** Multiple collectors  
**Issue:** Story points field configured with fallbacks in YAML but not all collectors use them consistently
- `jira_api_collector.py` ✅ Uses fallback fields
- `jira_hygiene_agent.py` ✅ Uses fallback fields  
- `jira_collector.py` ❌ Only uses primary field

**Fix:** Ensure all collectors use fallback logic

### 9. **Empty Developer Snapshot Implementation**
**File:** `src/collectors/jira_collector.py:461-473`  
**Issue:** Method body is just `pass`
```python
def _store_developer_snapshot(self, metrics: DeveloperMetrics) -> None:
    """Store developer metrics snapshot."""
    # ...
    try:
        pass  # This does nothing!
    finally:
        conn.close()
```
**Impact:** Silent data loss if this collector were used  
**Fix:** Delete file or implement method

### 10. **Schema Version Unused**
**Files:** `database/schema.py` and `database/hygiene_schema.py`  
**Issue:** `schema.py` has version tracking (`SCHEMA_VERSION = 1`) but `hygiene_schema.py` doesn't
- No migration strategy for schema changes
- No validation that schemas match between files

**Fix:** Consolidate schemas or implement proper versioning

---

## 🟢 Low Priority Issues

### 11. **No .gitignore for Sensitive Files**
**Issue:** `config/google_credentials.json` exists in repo  
**Status:** Unclear if this is version controlled  
**Fix:** Ensure `.gitignore` includes:
```
config/.env
config/*_credentials.json
data/*.db
logs/*.log
```

### 12. **Inconsistent Logging Levels**
**Issue:** Some scripts log at INFO, others at DEBUG, no central configuration

### 13. **Multiple Markdown Doc Files with Overlapping Info**
**Files:** 7 setup/automation docs in root  
**Issue:** SETUP_STATUS.md, AUTOMATION_GUIDE.md, HYGIENE_AGENT_SETUP.md overlap

### 14. **No requirements-dev.txt**
**Issue:** Testing/dev dependencies not separated from production

### 15. **JSON Data Files in Repo**
**Files:** `data/jira_*.json` (1 MB total)  
**Issue:** Large JSON cache files checked in?

---

## Architecture Analysis

### What Works Well ✅
1. **Hygiene tracking system** - Working end-to-end
2. **HTML report generation** - Clean, functional dashboards
3. **Database schema** - Well-designed for metrics tracking
4. **Configuration system** - Good YAML-based config with env var support
5. **QA validator** - Excellent hygiene check validation

### What's Broken 🔴
1. **Original collector design** - JiraCollector never implemented
2. **Burndown agent** - Deprecated but still scheduled
3. **Documentation** - Out of sync with code

### Architecture Decision Record
**Original Design:** MCP-based Jira collection via `jira_collector.py`  
**Reality:** Direct REST API via `jira_api_collector.py`  
**Why the change:** MCP tools can't be imported into Python modules  
**Problem:** Old design left in codebase

---

## Dependency Analysis

### Active Scripts and Their Dependencies
```
jira_collector_agent.py
  └─ collectors/jira_api_collector.py ✅
  └─ database/schema.py ✅

jira_hygiene_agent.py  
  └─ collectors/jira_api_collector.py ✅
  └─ database/hygiene_schema.py ✅

generate_hygiene_dashboard.py
  └─ database/hygiene_schema.py ✅

generate_html_report.py
  └─ database/queries.py ✅

qa_hygiene_validator.py
  └─ collectors/jira_api_collector.py ✅
  └─ database/hygiene_schema.py ✅

[UNUSED]
jira_collector.py (489 lines)
  └─ Never imported by any active script
```

---

## Data Flow Analysis

### Current Working Flow
```
1. jira_collector_agent.py (every 15 min)
   ↓ queries Jira API
   ↓ stores in metrics.db → sprint_snapshots, tickets, etc.

2. jira_hygiene_agent.py (every 15 min, 6am-6pm)
   ↓ queries Jira API  
   ↓ stores in metrics.db → hygiene_issues, status_changes

3. generate_hygiene_dashboard.py
   ↓ reads from metrics.db → hygiene_issues
   ↓ writes reports/html/hygiene_dashboard.html

4. generate_html_report.py
   ↓ reads from metrics.db → all tables
   ↓ writes reports/html/*.html (individual + team dashboards)
```

### Broken/Unused Flow
```
daily_burndown_agent.py (scheduled but doesn't exist)
  └─ 404 ERROR

jira_collector.py (imports available but never called)
  └─ Would raise NotImplementedError if called
```

---

## Recommendations

### Immediate Actions (Day 1)
1. **Delete broken cron task** or update to working script
2. **Delete 4 empty database files**
3. **Move `jira_collector.py` to deprecated** with explanation
4. **Update README.md** to reflect actual working scripts

### Short-term (Week 1)
5. **Create config option** for ignored epics list
6. **Consolidate documentation** - merge 7 docs into 2
7. **Add .gitignore entries** for sensitive files
8. **Delete deprecated scripts** after confirming no usage

### Long-term (Month 1)
9. **Schema versioning strategy** - proper migrations
10. **Testing framework** - unit tests for collectors
11. **Error handling** - more graceful failures
12. **Monitoring** - alert if data collection fails

---

## Metrics

### Code Stats
- **Total Python files:** 27 (15 active, 13 deprecated)
- **Total lines of code:** ~8,000
- **Active code:** ~6,000 lines
- **Dead code:** ~2,000 lines (25%)

### Database Stats  
- **Tables:** 12 (11 in use, 1 abandoned: `hygiene_violations`)
- **Active database:** `metrics.db` (2.4 MB)
- **Empty databases:** 4 files (0 bytes each)

### Documentation Stats
- **Markdown files:** 22 (7 in root, 15 in reports/)
- **Overlap/redundancy:** High (3-4 setup guides covering same info)

---

## Conclusion

The project **successfully delivers its core functionality** (hygiene tracking and reporting) but suffers from **architectural debt** accumulated during development. The main issue is **parallel/duplicate implementations** that were never cleaned up after pivoting from MCP-based to REST API-based Jira collection.

**Priority:** Focus on cleanup (delete dead code, fix docs) before adding new features.

**Risk Assessment:** Low - the working parts are solid, but maintainability will degrade if cleanup isn't done.
