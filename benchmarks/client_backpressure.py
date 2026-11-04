"""Compare slow-disk backpressure using the download study's exact client adapters."""

# This standalone experiment prints JSON and asserts known synthetic responses.
# ruff: noqa: INP001, S101, T201, PLR2004

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING

import download_clients as study

if TYPE_CHECKING:
    from collections.abc import Generator


class SlowFile:
    """Throttle writes by bytes, independent of the client's chunk sizes."""

    def __init__(self, target: IO[bytes]) -> None:
        self.target = target

    def flush(self) -> None:
        """Flush the underlying file."""
        self.target.flush()

    def fileno(self) -> int:
        """Expose the underlying descriptor for the shared fsync step."""
        return self.target.fileno()

    def write(self, data: bytes) -> int:
        """Delay in proportion to bytes before writing."""
        time.sleep(len(data) / (32 * 1024**2))
        return self.target.write(data)


def worker(client: str, url: str) -> dict[str, object]:
    """Measure one slow-sink download in a fresh client process."""
    original = study.tempfile.TemporaryFile

    @contextlib.contextmanager
    def slow_file() -> Generator[SlowFile]:
        with original() as target:
            yield SlowFile(target)

    study.tempfile.TemporaryFile = slow_file  # ty: ignore[invalid-assignment]  # instrument the sink in this worker only
    adapter = study.Client(client)
    start = time.perf_counter()
    try:
        status, _, size, digest = adapter.download(url)
        assert status == 200
        high_water_kib = next(
            int(line.split()[1])
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
            if line.startswith("VmHWM:")
        )
        return {
            "client": client,
            "elapsed_s": time.perf_counter() - start,
            "max_rss_mib": high_water_kib / 1024,
            "bytes": size,
            "sha256": digest,
        }
    finally:
        adapter.close()


def main() -> None:
    """Run shuffled large and small controls with independent client environments."""
    if not __debug__:
        msg = "run without -O so checksum assertions remain enabled"
        raise RuntimeError(msg)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", nargs=2)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(*args.worker)))
        return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), study.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    combinations = [(name, size, trial) for size in (8, 80) for name in study.CLIENTS for trial in range(3)]
    random.Random(20261104).shuffle(combinations)  # noqa: S311  # reproducible trial order, not a secret
    result = {
        "rate_mib_s": 32,
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "python": sys.version,
        "scope": "HTTP/1.1 localhost; same adapters and per-response fsync as main study; synthetic slow disk",
        "rss_method": "Linux /proc/self/status VmHWM; ru_maxrss can inherit the parent high-water mark across exec",
        "samples": [],
    }
    output = Path("dist/client-study/backpressure.json")
    try:
        for client, size, trial in combinations:
            study.LARGE = bytes(range(256)) * (size * 4096)
            expected = hashlib.sha256(study.LARGE).hexdigest()
            name = "curl_cffi" if client == "curl_stream" else client
            interpreter = sys.executable if name == "urllib" else f"/tmp/bench-{name}/bin/python"  # noqa: S108  # private container environments
            process = subprocess.run(  # noqa: S603  # fixed experiment script and interpreter paths
                [interpreter, __file__, "--worker", client, f"http://127.0.0.1:{server.server_port}/large"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            row = json.loads(process.stdout)
            assert row["bytes"] == size * 1024**2
            assert row["sha256"] == expected
            row.update({"trial": trial + 1, "size_mib": size})
            result["samples"].append(row)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(row), flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
