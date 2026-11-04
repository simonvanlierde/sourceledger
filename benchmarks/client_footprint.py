"""Measure imports in separate client environments; run inside the study container."""

# Standalone container experiment with JSON output.
# ruff: noqa: INP001, T201, PLR2004

from __future__ import annotations

import importlib.metadata
import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

CLIENTS = ("urllib3", "httpx", "niquests", "curl_cffi")


def worker(name: str) -> None:
    """Import one client, measuring its import and installed environment."""
    started = time.perf_counter()
    __import__(name)
    elapsed = time.perf_counter() - started
    distributions = list(importlib.metadata.distributions())
    total = 0
    for distribution in distributions:
        for file in distribution.files or []:
            path = Path(str(distribution.locate_file(file)))
            if path.is_file():
                total += path.stat().st_size
    print(
        json.dumps(
            {
                "client": name,
                "import_seconds": elapsed,
                "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                "installed_mib": total / 1024**2,
                "distributions": {d.metadata["Name"]: d.version for d in distributions},
                "python": sys.version,
            }
        )
    )


def main() -> None:
    """Collect five fresh imports per client in alternating order."""
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
        return
    samples = []
    for trial in range(5):
        for name in CLIENTS[:: -1 if trial % 2 else 1]:
            process = subprocess.run(  # noqa: S603  # fixed interpreter names and this script
                [f"/tmp/bench-{name}/bin/python", __file__, "--worker", name],  # noqa: S108  # private container environments
                check=True,
                capture_output=True,
                text=True,
            )
            samples.append(json.loads(process.stdout))
    out = Path("dist/client-study/footprint.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(samples, indent=2) + "\n", encoding="utf-8")
    for name in CLIENTS:
        rows = [row for row in samples if row["client"] == name]
        print(
            name,
            "median import",
            statistics.median(row["import_seconds"] for row in rows),
            "installed MiB",
            rows[0]["installed_mib"],
        )


if __name__ == "__main__":
    main()
