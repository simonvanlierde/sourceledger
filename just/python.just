# Install the venv, writing uv.lock if it is missing or stale. CI uses --locked.
py-sync:
    uv sync

# What CI runs: lint, format check, types, tests, dependency audit and licenses.
py-check:
    uv run --locked ruff check
    uv run --locked ruff format --check
    uv run --locked ty check
    uv run --locked pytest --cov
    uv audit --locked --preview-features audit-command
    d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT && uv export --locked --no-default-groups --no-hashes --no-annotate --no-header --no-emit-project -q -o "$d/requirements.txt" && { test ! -s "$d/requirements.txt" || uv run --locked --with licensecheck==2026.0.8 licensecheck -r "$d/requirements.txt" --zero; }

# Fix what ruff can fix, then format.
py-fmt:
    uv run ruff check --fix
    uv run ruff format

# Tests with a coverage summary.
py-cov:
    uv run pytest --cov --cov-report=term-missing

# Tests only; extra arguments reach pytest (`just py-test -k name`).
py-test *args:
    uv run pytest {{ args }}

# The tests on every Python version python.yml tests, each in a throwaway environment.
py-matrix:
    for v in 3.12 3.13 3.14 3.15; do uv run --isolated --locked --python "$v" pytest || exit 1; done

# A mutant that no test reaches counts as missed, unless tests/mutation-baseline.txt
# lists it. A listed one that no longer survives fails too. Slow: CI runs it weekly
# (mutation.yml); run it by hand after writing tests. Give mutmut patterns
# (`just py-mutation 'pkg.mod.x_name__mutmut_*'`) to mutate only those; that run skips
# the check for listed ones. `mutmut browse` shows survivors.

# Plant small bugs in the package; fail on a mutant the tests miss.
py-mutation *names:
    #!/usr/bin/env bash
    set -euo pipefail
    # Byte order, so sort and comm agree on the ǁ in class method mutant names.
    export LC_ALL=C
    # mutmut reuses results from mutants/ even after the tests change, so start clean.
    rm -rf mutants
    # The patterns hold *, so keep the shell from expanding them as file names.
    set -f
    uv run mutmut run {{ names }}
    survivors=$(uv run mutmut results | sed -nE 's/^ *(.*): (survived|no tests)$/\1/p' | sort)
    listed=$(grep -vE '^(#|$)' tests/mutation-baseline.txt | awk '{ print $1 }' | sort || true)
    new=$(comm -23 <(echo "$survivors" | sed '/^$/d') <(echo "$listed" | sed '/^$/d'))
    echo "$(echo "$survivors" | grep -c . || true) survivors; not in tests/mutation-baseline.txt:"
    echo "${new:-none}"
    gone=
    if [ -z "{{ names }}" ]; then
        gone=$(comm -13 <(echo "$survivors") <(echo "$listed" | sed '/^$/d'))
        echo "listed but no longer surviving (drop them):"
        echo "${gone:-none}"
    fi
    [ -z "$new$gone" ]

# The ref is origin/main unless you give one: fast enough to run on each change. Fails
# on a survivor not in the baseline. Run `just py-mutation` before a release, since it
# also finds the listed ones to drop.

# The same check for the functions this branch changed since it split off a git ref.
py-mutation-diff base='origin/main':
    #!/usr/bin/env bash
    set -euo pipefail
    patterns=$(uv run python {{ justfile_directory() }}/just/mutation_diff.py "$(git merge-base {{ base }} HEAD)")
    if [ -z "$patterns" ]; then echo "no changed function in a mutated module"; exit 0; fi
    set -f
    {{ just_executable() }} --justfile {{ justfile() }} py-mutation $patterns

# The profile is in tests/conftest.py. CI runs it weekly (property.yml).

# The tests under Hypothesis's "long" profile: 10,000 examples a property instead of 100.
py-property:
    uv run pytest --hypothesis-profile=long
