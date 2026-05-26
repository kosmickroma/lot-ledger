#!/usr/bin/env bash
# scripts/promote-preview.sh
#
# Deploy current checkout to the preview Cloud Run service (lot-ledger-preview
# in lot-ledger project). Works from any branch.
#
# APP_VERSION for preview is suffixed with the branch name + "-pre" so the
# pill clearly shows it's a preview build, not a real prod version.
#
# Usage:
#   ./scripts/promote-preview.sh

set -euo pipefail

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
BRANCH_SAFE=$(echo "$CURRENT_BRANCH" | tr -c 'a-zA-Z0-9' '-' | tr -s '-' | sed 's/^-//;s/-$//')
APP_VERSION="0.$(git rev-list --count --merges --first-parent main)-${BRANCH_SAFE}-pre"

echo "Computed APP_VERSION = $APP_VERSION"
echo "Submitting preview build to lot-ledger..."

gcloud builds submit \
  --config=cloudbuild-preview.yaml \
  --project=lot-ledger \
  --substitutions=_APP_VERSION="$APP_VERSION"
