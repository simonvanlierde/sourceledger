# Install the venv, writing uv.lock if it is missing or stale. CI uses --locked.
py-sync:
    uv sync

# What CI runs: lint, format check, types, tests, dependency audit.
py-check:
    uv run ruff check
    uv run ruff format --check
    uv run ty check
    uv run pytest
    uv audit --locked --preview-features audit-command

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

# Mutation testing over src/, about 2 minutes. Mutant globs narrow it:
# `just py-mutation 'sourceledger.ledger.x_parse*'`. Fails when a function has more
# survivors than tests/mutation-baseline.txt lists for it.
py-mutation *mutants:
    #!/usr/bin/env bash
    set -euo pipefail
    # Mutant globs pass through to mutmut, not the shell.
    set -f
    # Low priority on half the cores by default, so the machine stays usable.
    nice -n 10 uv run --group mutation mutmut run --max-children "${MUTMUT_JOBS:-$(( ($(nproc) + 1) / 2 ))}" {{ mutants }}
    mkdir -p reports/mutation
    uv run --group mutation mutmut results | sed -n 's/^ *\(.*\): survived$/\1/p' | LC_ALL=C sort > reports/mutation/survivors.txt
    # Mutant numbers shift whenever a function changes, so compare survivor counts per function.
    sed 's/__mutmut_[0-9]*$//' reports/mutation/survivors.txt | LC_ALL=C sort | uniq -c \
        | awk 'FILENAME == ARGV[1] { base[$2] = $1; next } $1 > base[$2] { print $2, base[$2] + 0, "->", $1 }' \
            tests/mutation-baseline.txt - > reports/mutation/new-survivors.txt
    echo "$(wc -l < reports/mutation/survivors.txt) survivors; functions above baseline:"
    cat reports/mutation/new-survivors.txt
    test ! -s reports/mutation/new-survivors.txt

# Rebaseline after triaging survivors. Only after a full run: it replaces the whole
# file, so a narrowed run would drop every other function.
py-mutation-baseline:
    sed 's/__mutmut_[0-9]*$//' reports/mutation/survivors.txt | LC_ALL=C sort | uniq -c | awk '{ print $1, $2 }' > tests/mutation-baseline.txt
