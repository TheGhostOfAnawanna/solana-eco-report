#!/usr/bin/env bash
# Auto-update loop for solana-eco-report — runs pipeline, pushes if data changed.
# $0 cost: free public APIs + existing repo-scope token (push only; no workflow files touched).
set -euo pipefail
cd "$(dirname "$0")"

OUT=$(python3 pipeline.py)
echo "pipeline: $OUT"

git config user.name  "TheGhostOfAnawanna"
git config user.email "robertbobbybudnick@gmail.com"
if [[ -n "$(git status --porcelain data/)" ]]; then
  git add data/
  git commit -m "auto-refresh snapshot $(date -u '+%Y-%m-%dT%H:%MZ')"
  git push fork HEAD:main 2>&1 | grep -v 'ghp_' # never leak token in logs
  echo "PUSH_OK $(date -u '+%Y-%m-%dT%H:%MZ')"
else
  echo "NO_CHANGE $(date -u '+%Y-%m-%dT%H:%MZ')"
fi
