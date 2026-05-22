# Dependency Status Editing

The Dependencies dashboard has environment-aware editing capabilities.

## 🏠 Local Environment (Editable)

When running the dashboard locally (http://localhost:8000 or file://), the dependency notes are **fully editable**:

### Features:
- ✏️ **Textareas are editable** - Type freely in the status notes field
- 💾 **Auto-save on blur** - Changes save automatically when you click away
- 🔘 **Manual save button** - Click "Save" to force an immediate save
- ✅ **Instant feedback** - "Saved" or error messages appear inline
- 🔄 **Writes to YAML** - Updates are written back to `config/dependencies.yaml`

### How It Works:
The JavaScript detects you're running locally by checking if the hostname is:
- `localhost`
- `127.0.0.1`
- Any private IP address (192.168.x.x, 10.x.x.x)
- Any `.local` domain

When local, the save function POSTs to `/api/dependency-notes` on your local Flask server.

## 🌐 GitHub Pages (Read-Only)

When hosted on GitHub Pages, the dashboard automatically switches to **read-only mode**:

### Behavior:
- 🔒 **Textareas are read-only** - Status notes cannot be edited
- 🚫 **Save buttons hidden** - No save functionality available
- 📌 **Visual indicator** - A yellow banner appears at the top explaining read-only mode
- 💡 **Helpful message** - Explains you need to run locally to edit

### Why Read-Only?
GitHub Pages serves static files only - there's no backend server to process saves. The YAML file lives in your local repository, not on GitHub Pages.

## 📝 Editing Workflow

To update dependency statuses:

1. **Run the dashboard locally:**
   ```bash
   cd /Users/davidbaxter/sync/claude/em_dashboard
   python scripts/run_server.py
   # Visit http://localhost:8000/dependencies.html
   ```

2. **Edit the dependency notes** in the textarea fields

3. **Save automatically** by clicking away, or click the "Save" button

4. **Changes are written** to `config/dependencies.yaml`

5. **Regenerate the dashboard:**
   ```bash
   python scripts/generate_html_reports.py
   ```

6. **Deploy to GitHub Pages:**
   ```bash
   ./scripts/deploy_to_github_pages.sh "Update dependency statuses"
   git add docs/ config/dependencies.yaml
   git commit -m "Update dependency statuses"
   git push
   ```

7. **GitHub Pages updates** in 1-2 minutes with the new read-only view

## 🔧 Technical Details

### Detection Logic
The `isLocalEnvironment()` function in `assets/dashboard.js`:

```javascript
function isLocalEnvironment() {
    const hostname = window.location.hostname;
    return hostname === 'localhost' ||
           hostname === '127.0.0.1' ||
           hostname.startsWith('192.168.') ||
           hostname.startsWith('10.') ||
           hostname.endsWith('.local');
}
```

### Read-Only Implementation
When not local:
- Sets `textarea.readOnly = true`
- Hides all `.dep-save-btn` buttons
- Adds visual styling (gray background, disabled cursor)
- Displays a banner message
- Blocks the `saveDependencyNotes()` function

### Local Implementation
When local:
- Textareas remain editable
- Save buttons visible and functional
- Auto-save on blur enabled
- POSTs to `/api/dependency-notes` endpoint

## 🎯 Benefits

✅ **Best of both worlds:**
- Team members can view dependencies anywhere (GitHub Pages)
- You can edit locally with instant feedback
- No risk of lost edits or conflicts

✅ **User-friendly:**
- No configuration needed - it just works
- Clear visual feedback about edit capabilities
- Graceful degradation on static hosting

✅ **Safe:**
- No broken functionality on GitHub Pages
- No confusion about why saves aren't working
- Prevents frustration with failed save attempts
