#!/usr/bin/env bash
# Deploy the built site to a Worker and report its workers.dev address as the step
# output "url" and in the job summary.
# Usage: deploy-worker.sh LABEL [WORKER [ARG...]]
# LABEL heads the summary line. WORKER overrides the name in the wrangler config, and each
# ARG goes to `wrangler deploy` as it is. WRANGLER is the command that runs wrangler
# (default: pnpm exec wrangler). Run it with CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID
# set. Exits 1 when wrangler fails or prints no address.
set -euo pipefail

label=$1
shift
args=()
if [ -n "${1:-}" ]; then args=(--name "$1"); fi
if [ $# -gt 0 ]; then shift; fi
read -ra wrangler <<<"${WRANGLER:-pnpm exec wrangler}"

# pipefail (set above): the pipe would take tee's status, so a refused deploy would pass.
"${wrangler[@]}" deploy ${args[@]+"${args[@]}"} ${1+"$@"} 2>&1 | tee deploy.log
url=$(grep -Eo 'https://[^ ]+\.workers\.dev' deploy.log | head -n 1 || true)
if [ -z "$url" ]; then
  echo "::error::Worker deployed, but wrangler printed no workers.dev address. Check workers_dev in the wrangler config."
  exit 1
fi
echo "url=$url" >>"$GITHUB_OUTPUT"
echo "### $label: $url" >>"$GITHUB_STEP_SUMMARY"
