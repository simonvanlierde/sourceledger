#!/usr/bin/env bash
# Delete pull request preview Workers, named $SLUG-pr-<number>, and check that each
# address stops serving.
# Usage: SLUG=app PRS="12 13" preview-retire.sh [WORKER...]
# PRS empty runs the sweep: every $SLUG-pr-<number> Worker whose PR is not open and
# ready, plus each WORKER argument that exists.
# Needs CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, REPO and GH_TOKEN. PROBE_WAITS sets
# the seconds between address checks. Exits 1 when any step failed.
set -euo pipefail

api="https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers"
auth="Authorization: Bearer $CLOUDFLARE_API_TOKEN"
prs=${PRS:-}
names=""
failed=0

if [ -z "$prs" ]; then
  # shortcut: the sweep matches Workers by name alone, so in one Cloudflare account a repo
  # foo with a site and a repo foo-site with a web app share foo-site-pr-<n>. Rename one
  # repo's slug if that ever happens.
  if ! all=$(curl -fsS --max-time 30 -H "$auth" "$api/scripts" | jq -r '.result[].id'); then
    echo "::error::Could not list the account's Workers."
    exit 1
  fi
  for name in $(grep -E "^$SLUG-pr-[0-9]+\$" <<<"$all" || true); do
    n=${name#"$SLUG"-pr-}
    # NOTE: each PR's state is read once, here. A PR reopened while the sweep runs
    # loses its Worker until its next push deploys it again.
    # A failed read keeps the Worker: delete only on a confirmed state.
    if ! state=$(gh api "repos/$REPO/pulls/$n" --jq '.state + " " + (.draft|tostring)'); then
      echo "::warning::Could not read PR $n, so its preview stays for now."
      failed=1
      continue
    fi
    [ "$state" = "open false" ] || prs="$prs $n"
  done
  for name in "$@"; do
    if grep -qxF "$name" <<<"$all"; then names="$names $name"; fi
  done
fi
for pr in $prs; do
  if [[ $pr = *[!0-9]* ]]; then
    echo "::error::Not a PR number: $pr"
    exit 1
  fi
  names="$names $SLUG-pr-$pr"
done
if [ -z "$names" ]; then
  echo "Nothing to retire."
  exit "$failed"
fi

sub=$(curl -fsS --max-time 30 -H "$auth" "$api/subdomain" | jq -r '.result.subdomain') || sub=""
if [ -z "$sub" ] || [ "$sub" = null ]; then
  echo "::error::Could not read the account's workers.dev subdomain, so no takedown below is verified."
  failed=1
fi
body=$(mktemp)
for name in $names; do
  # The API rather than `wrangler delete`, which also lists KV namespaces the token
  # cannot read. A PR that never had a preview has no Worker: 404 is not an error.
  status=$(curl -sS --max-time 30 -o "$body" -w '%{http_code}' -X DELETE -H "$auth" "$api/scripts/$name?force=true" || true)
  case $status in
  200) echo "Deleted $name." ;;
  404) echo "::notice::$name does not exist, so there is nothing to retire." ;;
  *)
    echo "::error::Could not delete $name (HTTP $status): $(cat "$body")"
    failed=1
    continue
    ;;
  esac
  if [ -z "$sub" ] || [ "$sub" = null ]; then continue; fi
  url="https://$name.$sub.workers.dev"
  # The delete takes a while to reach every edge. Only a 404 shows the Worker is
  # gone: a 5xx or a timeout (000) may still be the old build.
  code=""
  for wait in 0 ${PROBE_WAITS-5 10 20 40 60}; do
    sleep "$wait"
    code=$(curl -sS -o /dev/null --max-time 15 -w '%{http_code}' "$url" || true)
    [ "$code" = 404 ] && break
  done
  if [ "$code" = 404 ]; then
    echo "Retired $name: $url answers HTTP 404." | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
  else
    echo "::error::$url answers HTTP $code after the delete, not 404."
    failed=1
  fi
done
exit "$failed"
