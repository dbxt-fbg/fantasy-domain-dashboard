# GitHub Pages Setup Guide

This guide will help you deploy your Fantasy Dashboard to GitHub Pages.

## Quick Setup

### Option 1: Deploy to a New Repository (Recommended for Public Dashboard)

1. **Create a new GitHub repository:**
   ```bash
   # On GitHub.com, create a new repository called "fantasy-dashboard"
   # Don't initialize with README, .gitignore, or license
   ```

2. **Initialize and push from the docs folder:**
   ```bash
   cd docs
   git init
   git add .
   git commit -m "Initial dashboard deployment"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/fantasy-dashboard.git
   git push -u origin main
   ```

3. **Enable GitHub Pages:**
   - Go to your repository on GitHub
   - Settings → Pages
   - Source: Deploy from a branch
   - Branch: `main` / `(root)`
   - Click Save

4. **Access your dashboard:**
   - Your dashboard will be available at: `https://YOUR_USERNAME.github.io/fantasy-dashboard/`
   - Wait 1-2 minutes for the first deployment

### Option 2: Use This Repository with docs Folder

If this directory is already a git repository or you want to add Pages to it:

1. **Initialize git (if not already done):**
   ```bash
   git init
   git add .
   git commit -m "Add Fantasy Dashboard"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/em_dashboard.git
   git push -u origin main
   ```

2. **Enable GitHub Pages:**
   - Go to your repository on GitHub
   - Settings → Pages
   - Source: Deploy from a branch
   - Branch: `main` / `/docs`
   - Click Save

3. **Access your dashboard:**
   - Your dashboard will be available at: `https://YOUR_USERNAME.github.io/em_dashboard/`

## Updating the Dashboard

After making changes to your dashboard:

```bash
# Regenerate the HTML files
python scripts/generate_dashboard.py  # or whatever your generation script is

# Copy updated files
cp -r reports/html/* docs/
mv docs/project_fantasy.html docs/index.html
cd docs && find . -name "*.html" -type f -exec sed -i '' 's/project_fantasy\.html/index.html/g' {} +

# Commit and push
git add docs/
git commit -m "Update dashboard"
git push
```

## Important Considerations

### Security & Privacy
- **GitHub Pages sites are PUBLIC** by default (unless you have GitHub Pro/Enterprise)
- Make sure no sensitive data is in your HTML files:
  - ✅ Names, ticket numbers, sprint info are generally OK
  - ❌ Remove any API keys, credentials, internal URLs, or confidential metrics
  - ❌ Review any email addresses or personal contact info

### Files Included
The `docs/` folder contains:
- `index.html` - Main project dashboard (renamed from project_fantasy.html)
- `team_members_dashboard.html` - Team member overview
- `member_*.html` - Individual member dashboards
- `epics_dashboard.html` - Epic tracking
- `story_points_dashboard.html` - Story points analysis
- `pull_requests_dashboard.html` - Repository metrics
- `past_sprints_dashboard.html` - Sprint history
- `hygiene_dashboard.html` - Ticket hygiene metrics
- `logs_dashboard.html` - Agent logs
- `mbr.html` - Monthly Business Review
- `dependencies.html` - Cross-team dependencies
- `stakeholders.html` - Stakeholder list
- `assets/` - CSS and JavaScript files

### Automating Updates

You can automate dashboard updates using GitHub Actions. Create `.github/workflows/update-dashboard.yml`:

```yaml
name: Update Dashboard

on:
  schedule:
    - cron: '0 */6 * * *'  # Every 6 hours
  workflow_dispatch:  # Manual trigger

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
      
      - name: Generate dashboard
        env:
          JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python scripts/generate_dashboard.py
          cp -r reports/html/* docs/
          mv docs/project_fantasy.html docs/index.html
          cd docs && find . -name "*.html" -type f -exec sed -i 's/project_fantasy\.html/index.html/g' {} +
      
      - name: Commit and push
        run: |
          git config user.name "GitHub Actions"
          git config user.email "actions@github.com"
          git add docs/
          git diff --quiet && git diff --staged --quiet || (git commit -m "Auto-update dashboard" && git push)
```

## Troubleshooting

**Dashboard not loading?**
- Check that Pages is enabled in Settings
- Verify the branch and folder are correct
- Wait 1-2 minutes after first push
- Check Actions tab for build errors

**CSS/JS not loading?**
- Ensure `assets/` folder was copied
- Check browser console for 404 errors
- Verify relative paths in HTML files

**Links broken?**
- All internal links should use relative paths
- Main dashboard should be `index.html`
- Navigation links should work without `.html` extension

## Alternative: GitHub Private Pages

If your dashboard contains sensitive information:
1. Upgrade to GitHub Pro, Team, or Enterprise
2. Make your repository private
3. Enable Pages (will only be accessible to repository members)

Or consider:
- Hosting on internal infrastructure
- Using authentication layers (e.g., Cloudflare Access)
- Deploying to AWS S3 with authentication
