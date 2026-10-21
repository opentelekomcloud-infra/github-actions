#!/usr/bin/env bash
# Roll the Strix caller workflow out to organisation repositories, one PR each.
#
#   tools/rollout-strix.sh                     # dry run over active repos
#   tools/rollout-strix.sh --apply             # open the PRs
#   tools/rollout-strix.sh --repos a,b --apply # only these repos
#   tools/rollout-strix.sh --set-secrets ...   # also set LITELLM_* from the environment
#
# Needs gh authenticated with repo scope. Secret values are read from the
# environment (LITELLM_MODEL, LITELLM_KEY, LITELLM_API_URL, STRIX_REPORT_KEY)
# and passed to gh on stdin; they are never printed.
set -euo pipefail

ORG="opentelekomcloud-infra"
# An exact release, not the moving v1 tag: callers get workflow changes only via a reviewed bump.
REF="v1.2.0"
ACTIVE_DAYS=180
APPLY=false
SET_SECRETS=false
REPOS=""
EXCLUDE='^$'
BRANCH="ci/strix-scan"
TEMPLATE="$(cd "$(dirname "$0")/.." && pwd)/templates/strix-caller.yml"
WORKFLOW_PATH=".github/workflows/strix.yml"
LEGACY_PATH=".github/workflows/ai-security-scan.yml"

usage() { sed -n '2,12p' "$0"; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=true ;;
    --set-secrets) SET_SECRETS=true ;;
    --repos) REPOS="$2"; shift ;;
    --exclude) EXCLUDE="$2"; shift ;;
    --ref) REF="$2"; shift ;;
    --org) ORG="$2"; shift ;;
    --active-days) ACTIVE_DAYS="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument $1" >&2; usage 2 ;;
  esac
  shift
done
export GH_PAGER=cat

# Stagger nightly scans over 00:00-05:59 UTC: the ubuntu-dind scale set has at most 5 runners for the org.
cron_for() {
  local h; h=$(printf '%s' "$1" | cksum | cut -d' ' -f1)
  echo "$(( h % 60 )) $(( (h / 60) % 6 )) * * *"
}

if [ -n "$REPOS" ]; then
  candidates=$(tr ',' '\n' <<< "$REPOS")
else
  since=$(date -u -d "-${ACTIVE_DAYS} days" +%Y-%m-%d 2>/dev/null || date -u -v-"${ACTIVE_DAYS}"d +%Y-%m-%d)
  candidates=$(gh repo list "$ORG" --limit 500 --no-archived --source \
    --json name,pushedAt,primaryLanguage \
    --jq ".[] | select(.pushedAt >= \"${since}\" and .primaryLanguage != null) | .name" | sort)
fi
candidates=$(grep -vE "$EXCLUDE" <<< "$candidates" || true)

printf '%-40s %-8s %-9s %-12s %s\n' REPO VIS SECRETS CRON ACTION
for repo in $candidates; do
  full="$ORG/$repo"
  meta=$(gh api "repos/$full" --jq '[.visibility, .default_branch, .archived] | @tsv')
  IFS=$'\t' read -r vis default_branch archived <<< "$meta"
  if [ "$archived" = "true" ]; then continue; fi
  cron=$(cron_for "$repo")

  if gh api "repos/$full/contents/$WORKFLOW_PATH?ref=$default_branch" >/dev/null 2>&1; then
    printf '%-40s %-8s %-9s %-12s %s\n' "$repo" "$vis" "-" "$cron" "already has $WORKFLOW_PATH"
    continue
  fi
  if gh pr list -R "$full" --head "$BRANCH" --state open --json number --jq '.[0].number' | grep -q .; then
    printf '%-40s %-8s %-9s %-12s %s\n' "$repo" "$vis" "-" "$cron" "rollout PR already open"
    continue
  fi

  have=$(gh secret list -R "$full" --json name --jq '[.[].name] | join(",")' 2>/dev/null || echo "")
  secrets_ok=yes
  for s in LITELLM_MODEL LITELLM_KEY LITELLM_API_URL; do
    grep -qw "$s" <<< "$have" || secrets_ok=no
  done
  action="open PR"
  if [ "$secrets_ok" = no ]; then
    action="open PR; secrets missing$($SET_SECRETS && echo ', will set' || true)"
  fi
  printf '%-40s %-8s %-9s %-12s %s\n' "$repo" "$vis" "$secrets_ok" "$cron" "$action"
  $APPLY || continue

  if $SET_SECRETS; then
    for s in LITELLM_MODEL LITELLM_KEY LITELLM_API_URL STRIX_REPORT_KEY; do
      [ -n "${!s:-}" ] || continue
      if [ "$s" != STRIX_REPORT_KEY ] || [ "$vis" = "public" ]; then
        printf '%s' "${!s}" | gh secret set "$s" -R "$full" >/dev/null
      fi
    done
  fi

  gh label create strix-scan -R "$full" --color B60205 \
    --description "Request a Strix security scan of this PR" --force >/dev/null
  gh label create strix-nightly -R "$full" --color 5319E7 \
    --description "Strix nightly scan tracking issue" --force >/dev/null

  base_sha=$(gh api "repos/$full/git/ref/heads/$default_branch" --jq .object.sha)
  gh api -X POST "repos/$full/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$base_sha" >/dev/null
  content=$(sed -e "s|__CRON__|$cron|" -e "s|strix.yaml@[^[:space:]]*|strix.yaml@$REF|" "$TEMPLATE" | base64 | tr -d '\n')
  gh api -X PUT "repos/$full/contents/$WORKFLOW_PATH" -f branch="$BRANCH" \
    -f message="ci: run the Strix security scan before merge and nightly" -f content="$content" >/dev/null
  if legacy_sha=$(gh api "repos/$full/contents/$LEGACY_PATH?ref=$BRANCH" --jq .sha 2>/dev/null); then
    gh api -X DELETE "repos/$full/contents/$LEGACY_PATH" -f branch="$BRANCH" -f sha="$legacy_sha" \
      -f message="ci: drop the per-push Strix workflow, replaced by strix.yml" >/dev/null
  fi
  gh pr create -R "$full" --base "$default_branch" --head "$BRANCH" \
    --title "ci: Strix security scan before merge and nightly" \
    --body "Adds the org-wide Strix caller ($WORKFLOW_PATH, reusable workflow \`opentelekomcloud-infra/github-actions/.github/workflows/strix.yaml@$REF\`).

- Pull requests are scanned once, when approved, or on demand with the \`strix-scan\` label; only the changed files, \`--fail-on high\`, \$5 cap. Fork PRs are never scanned.
- The default branch is scanned nightly at \`$cron\` UTC, covering the commits since the previous scan; results go to the \`strix-nightly\` issue.
- Needs the repository secrets \`LITELLM_MODEL\`, \`LITELLM_KEY\`, \`LITELLM_API_URL\` (present: $secrets_ok)$([ "$vis" = public ] && echo ' and, to keep an encrypted report on this public repository, `STRIX_REPORT_KEY`')." >/dev/null
done
$APPLY || echo "dry run; re-run with --apply to open the PRs"
