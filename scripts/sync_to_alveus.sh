#!/bin/bash
set -euo pipefail

# Sync the generated dashboards into the alveus app folder.
#
# alveus (fanatics-gaming/alveus) hosts static apps at /<gh-user>/<app>/. Our app
# is a *copy* of docs/ — the same output GitHub Pages serves — so publishing there
# is a file copy plus a folder-only PR, which auto-merges.
#
# Run deploy_to_github_pages.sh (or the dashboard's Publish button) first so docs/
# is current; this script copies docs/, it does not regenerate anything.
#
# Usage:
#   ./scripts/sync_to_alveus.sh                 # copy + show what changed, then stop
#   ./scripts/sync_to_alveus.sh --push          # also branch, commit, push, open PR
#   ALVEUS_DIR=/path/to/alveus ./scripts/sync_to_alveus.sh
#
# Exits 0 with a message when there is nothing to sync.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
SRC="$REPO_ROOT/docs"

ALVEUS_DIR="${ALVEUS_DIR:-$HOME/sync/code/alveus}"
GH_USER="${ALVEUS_GH_USER:-}"
APP_NAME="${ALVEUS_APP:-fantasy-dashboard}"

DO_PUSH=0
for arg in "$@"; do
    case "$arg" in
        --push) DO_PUSH=1 ;;
        --alveus-dir=*) ALVEUS_DIR="${arg#*=}" ;;
        # Print the header comment block (lines 4-18) as the usage text.
        -h|--help) sed -n '4,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# --- preflight ------------------------------------------------------------

[ -d "$SRC" ] || { echo "❌ No docs/ at $SRC — run deploy_to_github_pages.sh first." >&2; exit 1; }
[ -f "$SRC/index.html" ] || { echo "❌ $SRC/index.html missing — docs/ looks incomplete." >&2; exit 1; }

if [ ! -d "$ALVEUS_DIR/.git" ]; then
    echo "❌ No alveus checkout at $ALVEUS_DIR" >&2
    echo "   Clone it:  gh repo clone fanatics-gaming/alveus $ALVEUS_DIR" >&2
    echo "   Or point at an existing one:  ALVEUS_DIR=/path $0" >&2
    exit 1
fi

# Make sure it's actually alveus and not some other repo at that path — we're
# about to write into it and open a PR against it.
if ! git -C "$ALVEUS_DIR" remote get-url origin 2>/dev/null | grep -q "fanatics-gaming/alveus"; then
    echo "❌ $ALVEUS_DIR is a git repo but its origin isn't fanatics-gaming/alveus." >&2
    exit 1
fi

# The folder name must equal the GitHub login exactly — that's how alveus decides
# ownership and whether the PR may auto-merge.
if [ -z "$GH_USER" ]; then
    GH_USER="$(gh api user --jq .login 2>/dev/null || true)"
fi
[ -n "$GH_USER" ] || { echo "❌ Could not determine your GitHub login (gh api user). Set ALVEUS_GH_USER." >&2; exit 1; }

APP_DIR="$ALVEUS_DIR/apps/$GH_USER/$APP_NAME"
[ -d "$APP_DIR" ] || { echo "❌ App folder not found: $APP_DIR" >&2; exit 1; }

echo "📊 Syncing docs/ → $APP_DIR"

# --- refuse to clobber uncommitted work in the alveus checkout -------------

if [ -n "$(git -C "$ALVEUS_DIR" status --porcelain -- "apps/$GH_USER/$APP_NAME")" ]; then
    echo "❌ $APP_DIR has uncommitted changes. Commit or discard them first —" >&2
    echo "   this script overwrites those files." >&2
    git -C "$ALVEUS_DIR" status --short -- "apps/$GH_USER/$APP_NAME" >&2
    exit 1
fi

# Start from main so we don't stack an app change onto an unrelated branch.
git -C "$ALVEUS_DIR" fetch --quiet origin main
git -C "$ALVEUS_DIR" checkout --quiet main
git -C "$ALVEUS_DIR" reset --hard --quiet origin/main

# --- copy -----------------------------------------------------------------

# Only the served files. Deliberately excluded:
#   .nojekyll   — GitHub Pages only, meaningless here
#   .DS_Store   — macOS junk
#   archive/    — stale markdown, unreferenced by any page
#   README.md   — docs/ has a Pages-specific one; the app keeps its own
mkdir -p "$APP_DIR/assets"
cp "$SRC"/*.html "$APP_DIR/"
cp "$SRC"/assets/dashboard.css "$SRC"/assets/dashboard.js "$APP_DIR/assets/"

# The generator doesn't emit alveus's project-managed favicon, so re-apply it
# after every copy or new pages ship without one.
python3 - "$APP_DIR" <<'PY'
import glob, os, re, sys
app = sys.argv[1]
FAV = '    <link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
added = 0
for p in sorted(glob.glob(os.path.join(app, '*.html'))):
    t = open(p, encoding='utf-8').read()
    if 'favicon.svg' in t:
        continue
    m = re.search(r'<head[^>]*>\s*\n', t)
    if not m:
        print('  ⚠️  no <head>, favicon not added: %s' % os.path.basename(p))
        continue
    open(p, 'w', encoding='utf-8').write(t[:m.end()] + FAV + t[m.end():])
    added += 1
if added:
    print('  ✓ favicon added to %d page(s)' % added)
PY

# Absolute links would escape the /<user>/<app>/ subpath. The generator emits
# relative ones today; fail loudly if that ever changes.
STRAY=$(grep -ohE '(href|src)="/[^"]*"' "$APP_DIR"/*.html | grep -v 'favicon.svg' | sort -u || true)
if [ -n "$STRAY" ]; then
    echo "⚠️  Absolute paths found — these break under the app subpath:" >&2
    echo "$STRAY" | sed 's|^|     |' >&2
fi

# --- report ---------------------------------------------------------------

CHANGED=$(git -C "$ALVEUS_DIR" status --porcelain -- "apps/$GH_USER/$APP_NAME" | wc -l | tr -d ' ')
if [ "$CHANGED" = "0" ]; then
    echo "✅ Already up to date — nothing to sync."
    exit 0
fi

echo "  $CHANGED file(s) changed:"
git -C "$ALVEUS_DIR" status --short -- "apps/$GH_USER/$APP_NAME" | sed 's|^|     |'

if [ "$DO_PUSH" != "1" ]; then
    cat <<EOF

Next steps (or re-run with --push to do this automatically):
  cd $ALVEUS_DIR
  git checkout -b $GH_USER-$APP_NAME-refresh
  git add apps/$GH_USER/$APP_NAME
  git commit -m "Refresh $GH_USER/$APP_NAME dashboards"
  gh pr create --fill        # folder-only PR auto-merges
EOF
    exit 0
fi

# --- push + PR ------------------------------------------------------------

BRANCH="$GH_USER-$APP_NAME-refresh-$(date +%Y%m%d-%H%M%S)"
cd "$ALVEUS_DIR"
git checkout -q -b "$BRANCH"
git add "apps/$GH_USER/$APP_NAME"

# A PR touching anything outside the app folder loses auto-merge, so verify.
OUTSIDE=$(git diff --cached --name-only | grep -v "^apps/$GH_USER/$APP_NAME/" || true)
if [ -n "$OUTSIDE" ]; then
    echo "❌ Staged files outside the app folder — refusing to push:" >&2
    echo "$OUTSIDE" | sed 's|^|     |' >&2
    exit 1
fi

git commit -q -m "Refresh $GH_USER/$APP_NAME dashboards

Copied from the fantasy-domain-dashboard generator output (docs/).
Snapshot of the dashboards as of $(date '+%Y-%m-%d %H:%M %Z')."

git push -q -u origin "$BRANCH"
gh pr create --title "Refresh $GH_USER/$APP_NAME dashboards" --body "Routine refresh of the generated dashboards, copied from the \`fantasy-domain-dashboard\` generator output.

Folder-only PR — nothing outside \`apps/$GH_USER/$APP_NAME/\` is touched, so this auto-merges.

Synced by \`scripts/sync_to_alveus.sh --push\`."

echo
echo "✅ PR opened. It auto-merges; the app updates at:"
echo "   https://alveus.ai.dsea.cafe/$GH_USER/$APP_NAME/"
