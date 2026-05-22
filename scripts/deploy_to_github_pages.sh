#!/bin/bash
set -e

# Script to update GitHub Pages deployment
# Usage: ./scripts/deploy_to_github_pages.sh [commit-message]

COMMIT_MSG="${1:-Update dashboard}"

echo "📊 Updating GitHub Pages deployment..."

# Create docs directory if it doesn't exist
mkdir -p docs

# Copy all HTML files from reports/html to docs
echo "📁 Copying dashboard files..."
cp -r reports/html/* docs/

# Rename main dashboard to index.html
if [ -f "docs/project_fantasy.html" ]; then
    mv docs/project_fantasy.html docs/index.html
    echo "✓ Renamed project_fantasy.html to index.html"
fi

# Update all links to point to index.html instead of project_fantasy.html
echo "🔗 Updating internal links..."
cd docs
find . -name "*.html" -type f -exec sed -i '' 's/project_fantasy\.html/index.html/g' {} +

# Add version parameter to JavaScript includes for cache busting
echo "🔧 Adding cache-busting version to JS includes..."
find . -name "*.html" -type f -exec sed -i '' 's|assets/dashboard\.js"|assets/dashboard.js?v=2.0"|g' {} +
cd ..

echo "✓ Dashboard files prepared in docs/"
echo ""
echo "Next steps:"
echo "1. Review the files in docs/ to ensure no sensitive data is exposed"
echo "2. If not already done, initialize git and create a GitHub repository"
echo "3. Add and commit the changes:"
echo "   git add docs/"
echo "   git commit -m '$COMMIT_MSG'"
echo "   git push"
echo "4. Enable GitHub Pages in repository Settings → Pages"
echo "   - Source: Deploy from a branch"
echo "   - Branch: main / /docs"
echo ""
echo "📖 See GITHUB_PAGES_SETUP.md for detailed instructions"
