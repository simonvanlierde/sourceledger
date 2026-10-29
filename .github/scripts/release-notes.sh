#!/usr/bin/env bash
# Print the CHANGELOG.md section for one version, without its heading.
# Usage: release-notes.sh 1.2.3 [CHANGELOG.md]
# Exits 1 when the section is missing or empty.
set -euo pipefail

version="${1:?usage: release-notes.sh VERSION [CHANGELOG.md]}"
changelog="${2:-CHANGELOG.md}"

# index() rather than a regex, so the dots in the version match only dots. The section
# ends at the next heading or, for the oldest version, at the link references below it.
notes="$(awk -v head="## [${version#v}]" '
  index($0, head) == 1 { found = 1; next }
  found && (/^## \[/ || /^\[[^]]+\]: /) { exit }
  found { print }
' "$changelog")"

if ! grep -q '[^[:space:]]' <<<"$notes"; then
  echo "no '## [${version#v}]' section in $changelog" >&2
  exit 1
fi
printf '%s\n' "$notes"
