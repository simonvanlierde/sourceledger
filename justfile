# Task runner: https://just.systems. `just` lists the recipes, `just <name>` runs one.
#
# `import?` skips a recipe file you did not scaffold. Recipes are prefixed
# (`py-`, `ts-`, `rs-`, `go-`) because imports share one namespace, except the few only
# one kind has, such as `pipeline` or `up`.
import? 'just/python.just'
import? 'just/ts.just'
import? 'just/rust.just'
import? 'just/go.just'
import? 'just/docker.just'

# List the recipes (what a bare `just` runs).
default:
    @just --list

# The stack checks CI runs. `just hooks` runs the git hooks, which CI also runs.
check:
    @just py-check

# Fix and format every stack here, as each stack's `fmt` recipe does.
fmt:
    @just py-fmt

# Run every git hook over the whole repo, not just staged files. The checks workflow skips
# the hooks in its SKIP list, which the stack workflows run instead. Here they all run, so
# this can fail where the checks workflow passes.
hooks:
    prek run -a

# Rewrite codemeta.json to agree with CITATION.cff, then check. The writer
# exits 1 whenever it changes a file, so the check that follows decides the result.
citation:
    -prek run citesync-write --hook-stage manual --all-files
    prek run citesync --all-files

# Release X.Y.Z. First run: cut CHANGELOG's [Unreleased] into [X.Y.Z] and commit it; merge that to main. Second run, on up-to-date main: sign and push tag vX.Y.Z.
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    version={{ version }}
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a version: $version" >&2; exit 1; }
    tag=v$version
    repo=https://github.com/simonvanlierde/sourceledger
    # The build takes its version from the manifest and refuses a tag that differs.
    [ "$(uv version --short --no-sync --directory .)" = "$version" ] || { echo "pyproject.toml is not at $version" >&2; exit 1; }
    # citesync fails while the manifests and citation files disagree. Run it before any file changes.
    prek run citesync --all-files
    git fetch --quiet origin main
    # Not grep -q: it stops reading early, and under pipefail git show then fails on a
    # changelog bigger than the pipe buffer.
    if git show origin/main:CHANGELOG.md | grep "^## \[$version\] - " >/dev/null; then
        .github/scripts/tag-release.sh "$version"
        exit 0
    fi
    prev=$(sed -n 's/^## \[\([0-9][0-9.]*\)\].*/\1/p' CHANGELOG.md | head -1)
    if [ -n "$prev" ]; then link=$repo/compare/v$prev...$tag; else link=$repo/releases/tag/$tag; fi
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    awk -v v="$version" -v d="$(date +%F)" -v repo="$repo" -v link="$link" '
        /^## \[Unreleased\]/ && !h { print; print ""; print "## [" v "] - " d; h = 1; next }
        /^\[Unreleased\]: / { print "[Unreleased]: " repo "/compare/v" v "...HEAD"; print "[" v "]: " link; l = 1; next }
        { print }
        END { if (!h) exit 1; if (!l) { print ""; print "[Unreleased]: " repo "/compare/v" v "...HEAD"; print "[" v "]: " link } }
    ' CHANGELOG.md >"$tmp" || { echo "CHANGELOG.md has no [Unreleased] section" >&2; exit 1; }
    .github/scripts/release-notes.sh "$tag" "$tmp" >/dev/null || { echo "[Unreleased] has no entries to release" >&2; exit 1; }
    cp "$tmp" CHANGELOG.md
    git add CHANGELOG.md
    git commit -s -m "chore: release $version" -- CHANGELOG.md
    echo "Merge this commit to main, then run: just release $version"

# Protect the default branch (no deletion, no force-push, linear history, PRs squash-merged after the always-on checks pass and one approval) and the v* tags. Turn on private vulnerability reporting, CodeQL default setup and Dependabot alerts. Set up release environments with required review. Safe to rerun.
repo-settings:
    #!/usr/bin/env bash
    set -euo pipefail
    err=$(mktemp)
    trap 'rm -f "$err"' EXIT
    fail() {
        cat "$err" >&2
        grep -qE 'HTTP (403|404)' "$err" && echo "Rulesets on a private repo need a paid GitHub plan (or the repo does not exist, or you lack admin rights)." >&2
        grep -q 'must be part of the ruleset source' "$err" && echo "The main ruleset lets the Renovate app bypass the review, and GitHub accepts that only once the app is installed on this repo: add it at https://github.com/apps/renovate, then rerun." >&2
        exit 1
    }
    # Merge-method switches live on the repo itself; the ruleset also limits the PR button.
    gh api -X PATCH repos/{owner}/{repo} -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false -F delete_branch_on_merge=true -F web_commit_signoff_required=true >/dev/null 2>"$err" || fail
    # Adopt an existing ruleset by name, else one with the same target that protects the same
    # refs as the file, updating it in place. The list has no conditions, so each candidate is read.
    for name in main main-checks tags; do
        file=.github/rulesets/$name.json
        case "$name" in
            main) want='["~DEFAULT_BRANCH"]' filter='.[] | select(.target == "branch" and (.name | test("checks?") | not)) | .id' ;;
            main-checks) want='["~DEFAULT_BRANCH"]' filter='.[] | select(.target == "branch" and (.name | test("checks?"))) | .id' ;;
            tags) want='["refs/tags/v*"]' filter='.[] | select(.target == "tag") | .id' ;;
        esac
        id=$(gh api "repos/{owner}/{repo}/rulesets?includes_parents=false" --jq "first(.[] | select(.name == \"$name\") | .id) // empty" 2>"$err") || fail
        if [ -z "$id" ]; then
            candidates=$(gh api "repos/{owner}/{repo}/rulesets?includes_parents=false" --jq "$filter" 2>"$err") || fail
            for candidate in $candidates; do
                same=$(gh api "repos/{owner}/{repo}/rulesets/$candidate" --jq ".conditions.ref_name.include == $want" 2>"$err") || fail
                if [ "$same" = true ]; then
                    id=$candidate
                    break
                fi
            done
        fi
        if [ -n "$id" ]; then
            gh api -X PUT "repos/{owner}/{repo}/rulesets/$id" --input "$file" >"$err" 2>&1 || fail
            echo "Updated ruleset '$name' ($id)."
        else
            # gh prints GitHub's reason for a refusal on stdout, so fail() needs both.
            gh api -X POST repos/{owner}/{repo}/rulesets --input "$file" >"$err" 2>&1 || fail
            echo "Created ruleset '$name'."
        fi
    done
    # SECURITY.md sends reports to a private security advisory, which this turns on.
    gh api -X PUT repos/{owner}/{repo}/private-vulnerability-reporting >/dev/null 2>"$err" || fail
    echo "Turned on private vulnerability reporting."
    # CodeQL default setup scans every PR and push to main.
    if gh api -X PATCH repos/{owner}/{repo}/code-scanning/default-setup -f state=configured >"$err" 2>&1; then
        echo "Turned on CodeQL default setup."
    else
        echo "Warning: CodeQL default setup could not be turned on, so code scanning stays off: $(cat "$err")" >&2
    fi
    # Dependabot alerts let Renovate open security PRs and turn on the dependency graph.
    gh api -X PUT repos/{owner}/{repo}/vulnerability-alerts >/dev/null 2>"$err" || fail
    echo "Turned on Dependabot alerts."
    # Environments limit publishing to version tags and require review before release.
    user_id=$(gh api user --jq .id 2>"$err") || fail
    for env in pypi; do
        gh api -X PUT "repos/{owner}/{repo}/environments/$env" \
            -F "reviewers[][type]=User" \
            -F "reviewers[][id]=$user_id" \
            -F "deployment_branch_policy[protected_branches]=false" \
            -F "deployment_branch_policy[custom_branch_policies]=true" >/dev/null 2>"$err" || fail
        policies=$(gh api "repos/{owner}/{repo}/environments/$env/deployment-branch-policies" --jq '.branch_policies[].name' 2>/dev/null || true)
        if ! echo "$policies" | grep -qxF 'v*'; then
            gh api -X POST "repos/{owner}/{repo}/environments/$env/deployment-branch-policies" -f name="v*" -f type="tag" >/dev/null 2>"$err" || fail
        fi
        if [ "$env" = "ghcr" ] && ! echo "$policies" | grep -qxF 'main'; then
            gh api -X POST "repos/{owner}/{repo}/environments/$env/deployment-branch-policies" -f name="main" -f type="branch" >/dev/null 2>"$err" || fail
        fi
        echo "Configured environment '$env'."
    done
    repo_full_name=$(gh api repos/{owner}/{repo} --jq .full_name 2>"$err") || fail
    owner=${repo_full_name%%/*}
    repo=${repo_full_name##*/}
    echo "PyPI pending publisher: https://pypi.org/manage/account/publishing/?provider=github&project_name=sourceledger&owner=$owner&repository=$repo&workflow_filename=python-release.yml&environment=pypi"
    echo "Zenodo: https://zenodo.org/account/settings/github/"

# Set the GitHub About box from your answers: description and a topic per stack. Adds topics, never removes yours. Safe to rerun.
repo-about:
    gh repo edit --description 'Fetch, verify and validate the public sources a dataset is built from, with a provenance record for every download' --add-topic python

# A repo's own recipes that copier updates never touch:
import? 'just/local.just'
