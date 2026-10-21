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

# Benchmarks, measured as CodSpeed's CI does (needs the codspeed CLI).
py-bench:
    codspeed run --mode simulation -- uv run pytest tests/test_benchmarks.py --codspeed
