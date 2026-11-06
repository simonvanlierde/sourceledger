#!/usr/bin/env bash
# Print the CHANGELOG.md section for one version, without its heading.
# Usage: release-notes.sh 1.2.3 [CHANGELOG.md]
# Exits 1 when the section is missing or empty.
set -euo pipefail

version="${1:?usage: release-notes.sh VERSION [CHANGELOG.md]}"
changelog="${2:-CHANGELOG.md}"

# index() rather than a regex, so the dots in the version match only dots. The section
# ends at the next heading; trailing version comparison links are omitted.
notes="$(awk -v head="## [${version#v}]" '
  index($0, head) == 1 { found = 1; next }
  found && /^## \[/ { exit }
  found { lines[++n] = $0 }
  END {
    tail = n
    while (tail > 0 && (lines[tail] ~ /^[[:space:]]*$/ || lines[tail] ~ /^\[[^]]+\]: /)) tail--
    for (i = 1; i <= n; i++) {
      if (i > tail && lines[i] ~ /^\[(Unreleased|v?[0-9]+\.[0-9]+\.[0-9]+[^]]*)\]: /) continue
      print lines[i]
    }
  }
' "$changelog")"

if ! grep -q '[^[:space:]]' <<<"$notes"; then
  echo "no '## [${version#v}]' section in $changelog" >&2
  exit 1
fi
printf '%s\n' "$notes"
