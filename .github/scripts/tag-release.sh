#!/usr/bin/env bash
# Sign the release tag vX.Y.Z on origin/main with your git signing key and push it.
# The release workflow refuses a tag GitHub cannot verify, so a tag pushed any other
# way publishes nothing. Run it on an up-to-date main.
# Usage: tag-release.sh X.Y.Z
set -euo pipefail

version=${1:?usage: tag-release.sh X.Y.Z}
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "not a version: $version" >&2
  exit 1
}
tag="v$version"

git fetch --quiet origin main
sha=$(git rev-parse origin/main)
# Not grep -q: it stops reading early, and under pipefail git show's SIGPIPE then fails
# the check on a changelog bigger than the pipe buffer.
git show "$sha:CHANGELOG.md" | grep "^## \[$version\] - " >/dev/null || {
  echo "CHANGELOG.md on origin/main has no $version section" >&2
  exit 1
}
[[ $(git rev-parse HEAD) == "$sha" ]] || {
  echo "HEAD is not origin/main: check out and pull main first" >&2
  exit 1
}
# The manifest version checks read the working tree, not the commit being tagged.
git diff --quiet HEAD || {
  echo "uncommitted changes to tracked files: commit or stash them first" >&2
  exit 1
}

# A tag left by an earlier run is reused only if it marks the same commit.
if git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null; then
  [[ $(git rev-parse "$tag^{commit}") == "$sha" ]] || {
    echo "tag $tag exists on another commit. If no release was published from it, delete it" >&2
    echo "(a tags-ruleset bypass is needed): git tag -d $tag && git push origin :refs/tags/$tag" >&2
    exit 1
  }
else
  git tag --sign "$tag" --message "$tag" "$sha"
fi
git verify-tag "$tag" >/dev/null 2>&1 || {
  echo "tag $tag is not signed with a key git trusts (gpg.ssh.allowedSignersFile)" >&2
  exit 1
}
git push origin "refs/tags/$tag"
