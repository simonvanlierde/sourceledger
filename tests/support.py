"""Helpers and fixtures the test modules share."""

import importlib
import io
import json
import tracemalloc
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path

import pytest

from sourceledger import manifest as manifest_mod
from sourceledger.cli import main

build_mod = importlib.import_module("sourceledger.build")
fetch_mod = importlib.import_module("sourceledger.fetch")
report_mod = importlib.import_module("sourceledger.report")


FIXTURE = "year,value\n2020,1.5\n2021,2.5\n"


RAW = Path("data/raw/example.csv")


OUT = Path("data/processed/example.csv")


LOG = Path("data/fetches.jsonl")


TERMS = 'terms = "CC0-1.0"\nterms_url = "https://example.org/terms"\nlicense = "CC0-1.0"\nredistribute = true'


def _manifest(root: Path, url: str, terms: str = TERMS) -> None:
    """One implemented source at `url`, and one unimplemented source."""
    text = f"""
[[source]]
id = "example"
status = "implemented"
url = "{url}"
{terms}

[[source]]
id = "later"
status = "unimplemented"
reason = "No reader written yet."
"""
    (root / "sources.toml").write_text(text, encoding="utf-8", newline="")


def _run(*stages: str) -> list[int]:
    return [main([stage]) for stage in stages]


def _headers(**fields: str) -> Message:
    """Response headers; `Retry_After="30"` sets Retry-After."""
    headers = Message()
    for name, value in fields.items():
        headers[name.replace("_", "-")] = value
    return headers


class _Response(io.BytesIO):
    """What the opener returns: a body, and headers whose Content-Length is `length` (default: the true length).

    `url` is where the opener ended after redirects; empty means the request's own URL.
    """

    def __init__(self, body: bytes, length: int | None = None, url: str = "", **headers: str) -> None:
        super().__init__(body)
        self.headers = _headers(Content_Length=str(len(body) if length is None else length), **headers)
        self.url = url


def _serve(monkeypatch: pytest.MonkeyPatch, *outcomes: _Response | Exception) -> list[urllib.request.Request]:
    """Answer each request with the next outcome, raising it if it is an exception. Returns the requests made.

    Each request's socket timeout is kept as its `timeout` attribute.
    """
    calls: list[urllib.request.Request] = []
    queue = list(outcomes)

    def open_(request: urllib.request.Request, timeout: float) -> _Response:  # the stand-in ignores the timeout
        request.timeout = timeout
        calls.append(request)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.url = outcome.url or request.full_url
        return outcome

    monkeypatch.setattr(fetch_mod, "_open", open_)
    return calls


def _http_error(code: int, **headers: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "error", _headers(**headers), None)


def _record(source_id: str, digit: str) -> str:
    """A log line for `source_id` whose SHA-256 is `digit` 64 times."""
    fields = {"source_id": source_id, "url": "u", "retrieved_at": "t", "sha256": digit * 64, "bytes": 1}
    return json.dumps(fields)


def _two_sources(root: Path, first: str, second: str) -> None:
    """Two implemented sources, `a` at `first` and `b` at `second`."""
    text = "".join(
        f'[[source]]\nid = "{sid}"\nstatus = "implemented"\nurl = "{url}"\n{TERMS}\n\n'
        for sid, url in (("a", first), ("b", second))
    )
    (root / "sources.toml").write_text(text, encoding="utf-8", newline="")


_DATA = "https://example.org/data.csv"


_SNAPSHOT = f"https://web.archive.org/web/20261018033833/{_DATA}"


def _peak(run: Callable[[], object]) -> int:
    """The most memory Python held at once while `run` ran, in bytes."""
    tracemalloc.start()
    try:
        run()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# record(): files another tool downloaded.

WHEN = "2026-11-05T10:30:00+01:00"


def _source() -> manifest_mod.Source:
    return manifest_mod.load_manifest()[0]


def _lines() -> list[dict[str, object]]:
    return [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
