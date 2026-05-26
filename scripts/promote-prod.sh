#!/usr/bin/env bash
# scripts/promote-prod.sh
#
# Deploy current main checkout to Mike's prod (real-estate-map-tool).
# Computes APP_VERSION automatically from git history (count of merge commits
# on main's first-parent line) so the version pill displayed in the sidebar
# stays in sync with reality.
#
# Usage (from a checkout on the main branch, after merging develop):
#   ./scripts/promote-prod.sh
#
# Refuses to run if the working tree is dirty or if you're not on main.
# To force-deploy from a non-main branch (e.g., to test the script itself),
# set FORCE=1.

set -euo pipefail

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" && "${FORCE:-0}" != "1" ]]; then
  echo "ERROR: not on main (currently on '$CURRENT_BRANCH'). Set FORCE=1 to override."
  exit 1
fi

if [[ -n "$(git status --porcelain)" && "${FORCE:-0}" != "1" ]]; then
  echo "ERROR: working tree is dirty. Commit or stash first. Set FORCE=1 to override."
  git status --short
  exit 1
fi

APP_VERSION="0.$(git rev-list --count --merges --first-parent main)"
echo "Computed APP_VERSION = $APP_VERSION (from merge count on main)"
echo "Submitting prod build to real-estate-map-tool..."

gcloud builds submit \
  --config=cloudbuild-prod.yaml \
  --project=real-estate-map-tool \
  --substitutions=_APP_VERSION="$APP_VERSION"
