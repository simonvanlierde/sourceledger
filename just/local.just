# Benchmarks, measured as CodSpeed's CI does (needs the codspeed CLI).
py-bench:
    codspeed run --mode simulation -- uv run pytest tests/test_benchmarks.py --codspeed
