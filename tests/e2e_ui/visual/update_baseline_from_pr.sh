#!/usr/bin/env bash
# Update the committed UI-snapshot baseline from a PR's CI-rendered artifact.
#
# Visual baselines must be rendered on the pinned CI runner (screenshots drift
# across OSes), so you can't regenerate them locally. When the UI Snapshot gate
# fails on a PR -- typically a fork PR, which CI can't push to -- run this to
# pull the runner-rendered image and update the baseline locally for review +
# commit.
#
# Usage:
#   tests/e2e_ui/visual/update_baseline_from_pr.sh <pr-number> [--repo owner/name]
#
# Requires: gh (authenticated), git. The artifact is kept for 7 days.
set -euo pipefail

BASELINE="tests/e2e_ui/visual/snapshots/test_landing_snapshot/test_empty_landing_matches_baseline/test_empty_landing_matches_baseline[chromium][linux].png"

PR=""
REPO="${REPO:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "error: unknown flag $1" >&2; exit 2 ;;
    *) PR="$1"; shift ;;
  esac
done

if [ -z "$PR" ]; then
  echo "error: PR number required. Usage: $0 <pr-number> [--repo owner/name]" >&2
  exit 2
fi

# Run from the repo root so the baseline path resolves regardless of CWD.
cd "$(git rev-parse --show-toplevel)"
[ -z "$REPO" ] && REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)

echo "Resolving PR #$PR in $REPO ..."
HEAD_SHA=$(gh pr view "$PR" --repo "$REPO" --json headRefOid -q .headRefOid)
echo "  head SHA: $HEAD_SHA"

# pull_request runs (incl. fork PRs) live in the base repo, keyed by head SHA.
RUN_ID=$(gh api "repos/$REPO/actions/runs?head_sha=$HEAD_SHA&per_page=100" \
  --jq '[.workflow_runs[] | select(.name=="UI Snapshot")] | sort_by(.created_at) | last | .id // empty')
if [ -z "$RUN_ID" ]; then
  echo "error: no 'UI Snapshot' run found for $HEAD_SHA -- has CI run on this PR's head?" >&2
  exit 1
fi
echo "  UI Snapshot run: $RUN_ID"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Downloading artifact ui-snapshot-$RUN_ID ..."
if ! gh run download "$RUN_ID" --repo "$REPO" -n "ui-snapshot-$RUN_ID" -D "$TMP" 2>/dev/null; then
  gh run download "$RUN_ID" --repo "$REPO" -D "$TMP"  # fall back to all artifacts
fi

# On a mismatch the runner-rendered image is snapshot_failures/.../actual_*.png.
# On an --update-snapshots run it's the regenerated file under snapshots/.
SRC=$(find "$TMP" -type f -name 'actual_*.png' | head -n1)
[ -z "$SRC" ] && SRC=$(find "$TMP" -type f -path '*/snapshots/*' -name 'test_empty_landing_matches_baseline*.png' | head -n1)
if [ -z "$SRC" ]; then
  echo "No rendered image in the artifact -- the gate likely PASSED, so the baseline already matches. Nothing to update." >&2
  exit 0
fi
echo "  rendered image: ${SRC#"$TMP"/}"

cp "$SRC" "$BASELINE"
echo
echo "Updated baseline: $BASELINE"
git --no-pager diff --stat -- "$BASELINE" || true
echo
echo "Next: review the image, then commit + push:"
echo "  git add \"$BASELINE\" && git commit -m 'test(ui-snapshot): update landing baseline' && git push"
