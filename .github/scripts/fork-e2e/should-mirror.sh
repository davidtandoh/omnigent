#!/usr/bin/env bash
# Decides whether a fork PR's head commit should be mirrored onto the trusted
# fork-e2e/pr-N branch (which lets e2e run as a `push` with the test-gateway
# secrets). Called by .github/workflows/fork-e2e-mirror.yml.
#
# Gate (any one opens it):
#   1. The fork-e2e/pr-N branch already exists -> always re-mirror, so new
#      commits on an already-opened PR re-run e2e.
#   2. Returning contributor -- author_association is OWNER / MEMBER /
#      COLLABORATOR / CONTRIBUTOR (GitHub's own "has contributed before"
#      signal; first-timers are FIRST_TIME_CONTRIBUTOR / FIRST_TIMER / NONE).
#   3. Maintainer signal -- the author is in .github/MAINTAINER, or a maintainer
#      applied the `e2e-approved` label. The label (not a PR approval) is the
#      trigger: applying it fires pull_request_target (labeled), which carries
#      the secrets the mirror needs to mint its App token. A pull_request_review
#      on a fork PR can't -- it gets only a read-only GITHUB_TOKEN, no secrets.
#
# The MAINTAINER list comes from load-maintainers.sh (same as
# maintainer-approval.yml). A drift only over-/under-opens the mirror gate
# (bounded by the rate-limited, revocable test token), it can't bypass the
# merge gate.
#
# Env in:  GH_TOKEN, REPO, PR, AUTHOR_ASSOCIATION, MAINTAINERS (space-separated,
#          from load-maintainers.sh), MIRROR_BRANCH (e.g. fork-e2e/pr-123).
# Out:     `mirror=true|false` and `reason=<text>` on $GITHUB_OUTPUT.

set -euo pipefail

APPROVE_LABEL="e2e-approved"

emit() {
  echo "mirror=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "mirror=$1 ($2)"
}

# 1. Already opened: re-mirror every subsequent push.
if gh api "repos/$REPO/branches/$MIRROR_BRANCH" >/dev/null 2>&1; then
  emit true "re-mirror: $MIRROR_BRANCH already exists"
  exit 0
fi

# 2. Returning contributor (GitHub's native author_association signal).
case "$AUTHOR_ASSOCIATION" in
  OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR)
    emit true "returning contributor (author_association=$AUTHOR_ASSOCIATION)"
    exit 0
    ;;
esac

# 3. Maintainer signal: author is a maintainer, or a maintainer applied the
#    e2e-approved label (see header note).
MAINTAINERS_LC=$(echo "${MAINTAINERS:-}" | tr '[:upper:]' '[:lower:]')
if [[ -n "${MAINTAINERS_LC// /}" ]]; then
  AUTHOR=$(gh pr view "$PR" --repo "$REPO" --json author --jq '.author.login')
  AUTHOR_LC=$(echo "$AUTHOR" | tr '[:upper:]' '[:lower:]')

  for m in $MAINTAINERS_LC; do
    if [[ "$m" == "$AUTHOR_LC" ]]; then
      emit true "author @$AUTHOR is a maintainer"
      exit 0
    fi
  done

  # e2e-approved label, but only if it is currently present AND was applied by a
  # maintainer. Label changes need triage/write access, so a fork author can't
  # self-apply; the timeline-actor check is defence in depth (matches the
  # maintainer-effective waiver pattern in should-scan.sh).
  if gh pr view "$PR" --repo "$REPO" --json labels --jq '.labels[].name' \
       | grep -qx "$APPROVE_LABEL"; then
    LABELERS=$(gh api "repos/$REPO/issues/$PR/timeline" --paginate \
      --jq "[.[] | select(.event == \"labeled\" and .label.name == \"$APPROVE_LABEL\")] | .[].actor.login" 2>/dev/null || echo "")
    for u in $LABELERS; do
      u_lc=$(echo "$u" | tr '[:upper:]' '[:lower:]')
      for m in $MAINTAINERS_LC; do
        if [[ "$m" == "$u_lc" ]]; then
          emit true "labeled '$APPROVE_LABEL' by maintainer @$u"
          exit 0
        fi
      done
    done
  fi
fi

emit false "awaiting maintainer '$APPROVE_LABEL' label (first-time contributor)"
