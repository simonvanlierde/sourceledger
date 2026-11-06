"""Fixtures shared across test modules: a local publisher, a fixed clock and golden files."""

import http.server
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Self

import pytest
from hypothesis import settings

from sourceledger import ledger

# What the publisher serves at /data.csv; test_ledger's FIXTURE holds the same text.
PUBLISHED = b"year,value\n2020,1.5\n2021,2.5\n"
LAST_MODIFIED = "Wed, 04 Nov 2026 12:00:00 GMT"
NOW = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
GOLDEN = Path(__file__).parent / "golden"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--update-goldens", action="store_true", help="rewrite tests/golden/ from this run's output")


class _Publisher(http.server.BaseHTTPRequestHandler):
    """A real HTTP server: /data.csv with an ETag, Last-Modified and 304s.

    /old.csv redirects to it, and /signed.csv to a signed URL of it.

    The body comes in two writes 50 ms apart, so a download reads it in more than one chunk.
    """

    def do_GET(self) -> None:
        if self.path == "/unreadable-redirect.csv":
            self.send_response(302)
            self.send_header("Location", "/data.csv")
            self.send_header("Content-Length", "1024")
            self.end_headers()
            return
        if self.path in ("/old.csv", "/signed.csv"):
            self.send_response(302)
            self.send_header("Location", "/data.csv" if self.path == "/old.csv" else "/data.csv?sig=s3cr3t#part")
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.end_headers()
            return
        body = b"\x00\xfffirst\x80second" if self.path == "/data.bin" else PUBLISHED
        self.send_response(200)
        self.send_header("ETag", '"v1"')
        self.send_header("Last-Modified", LAST_MODIFIED)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:7])
        self.wfile.flush()
        threading.Event().wait(0.05)
        self.wfile.write(body[7:])

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # the base class names it
        """Keep test output quiet."""


@pytest.fixture
def publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The base URL of a `_Publisher` on a free local port; the test runs in a project directory."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Publisher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.chdir(tmp_path)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class _AnyDatetime(type):
    def __instancecheck__(cls, instance: object) -> bool:
        """A real datetime passed in still counts as one where `ledger.datetime` is this class."""
        return isinstance(instance, datetime)


class _FrozenDatetime(datetime, metaclass=_AnyDatetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:  # noqa: ARG003  # sourceledger only asks for UTC
        return cls.fromtimestamp(NOW.timestamp(), tz=UTC)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Fetch and record times are `NOW`, so their records compare byte for byte."""
    monkeypatch.setattr(ledger, "datetime", _FrozenDatetime)
    return NOW


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> Callable[[str, str], None]:
    """Compare text with `tests/golden/<name>`; `pytest --update-goldens` rewrites the file instead."""

    def check(name: str, text: str) -> None:
        path = GOLDEN / name
        if request.config.getoption("--update-goldens"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode())
        hint = "rerun with --update-goldens, then read git diff tests/golden"
        assert path.exists(), f"{name} has no golden file; {hint}"
        # Bytes, not read_text: CSV outputs end lines in CRLF, and that is part of the format.
        assert text == path.read_bytes().decode(), f"{name} changed; {hint} if intended"

    return check


# `just py-property` runs the tests under this profile; each run draws new inputs.
settings.register_profile("long", max_examples=10_000, derandomize=False, print_blob=True)
