"""The whole pipeline against a fixture served from a local file: no network."""

import contextlib
import errno
import gzip
import hashlib
import http.client
import http.server
import io
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from email.message import Message
from email.utils import format_datetime
from pathlib import Path

import pytest

if sys.platform != "win32":
    import fcntl

from sourceledger import ledger
from sourceledger.cli import main

FIXTURE = "year,value\n2020,1.5\n2021,2.5\n"
RAW = Path("data/raw/example.csv")
OUT = Path("data/processed/example.csv")
LOG = Path("data/fetches.jsonl")
TERMS = 'terms = "CC0-1.0"\nterms_url = "https://example.org/terms"\nlicense = "CC0-1.0"\nredistribute = true'
SCHEMA = json.loads((Path(ledger.__file__).parent / "sources.schema.json").read_text(encoding="utf-8"))


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
    (root / "sources.toml").write_text(text, encoding="utf-8")


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture source file and a manifest that points at it. The test runs inside the project directory."""
    source = tmp_path / "upstream" / "example.csv"
    source.parent.mkdir()
    source.write_text(FIXTURE, encoding="utf-8")
    _manifest(tmp_path, source.as_uri())
    monkeypatch.chdir(tmp_path)
    return source


def _run(*stages: str) -> list[int]:
    return [main([stage]) for stage in stages]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Each wait between fetch attempts, recorded instead of slept."""
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


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

    def open_(request: urllib.request.Request, timeout: float) -> _Response:
        request.timeout = timeout
        calls.append(request)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.url = outcome.url or request.full_url
        return outcome

    monkeypatch.setattr(ledger, "_open", open_)
    return calls


def _http_error(code: int, **headers: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "error", _headers(**headers), None)


def _record(source_id: str, digit: str) -> str:
    """A log line for `source_id` whose SHA-256 is `digit` 64 times."""
    fields = {"source_id": source_id, "url": "u", "retrieved_at": "t", "sha256": digit * 64, "bytes": 1}
    return json.dumps(fields)


def test_the_whole_pipeline_runs_from_a_local_file(upstream: Path) -> None:
    """Fetch, build and validate succeed, and every output row names the bytes it came from."""
    assert _run("fetch", "build", "validate") == [0, 0, 0]

    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["url"] == upstream.as_uri()
    assert record["sha256"] == hashlib.sha256(FIXTURE.encode()).hexdigest()
    assert record["retrieved_at"].endswith("Z")
    assert record["vintage"] == "not stated"

    rows = ledger.read_output(OUT)
    assert [(r["year"], r["value"]) for r in rows] == [("2020", "1.5"), ("2021", "2.5")]
    assert {(r["source_url"], r["raw_sha256"]) for r in rows} == {(record["url"], record["sha256"])}


def test_a_toml_date_vintage_is_recorded_as_a_string(upstream: Path, tmp_path: Path) -> None:
    """An unquoted `vintage = 2024-06-01` loads as a date; the fetch record still holds text."""
    _manifest(tmp_path, upstream.as_uri(), terms="vintage = 2024-06-01")
    assert _run("fetch") == [0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["vintage"] == "2024-06-01"


def test_fetch_refuses_bytes_that_miss_the_pinned_sha256(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source changed upstream is refused at the door, not recorded as a new fetch."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{"0" * 64}"')
    assert _run("fetch") == [1]
    assert "not the sha256 in sources.toml" in capsys.readouterr().err
    assert not RAW.exists()
    good = hashlib.sha256(FIXTURE.encode()).hexdigest()
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{good}"')
    assert _run("fetch") == [0]


@pytest.mark.usefixtures("upstream")
def test_fetch_retries_a_server_error_with_backoff(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A brief publisher outage costs a wait, not a failed run."""
    calls = _serve(monkeypatch, _http_error(503), _http_error(502), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert len(calls) == 3
    assert sleeps == [5, 10]
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.usefixtures("upstream")
def test_fetch_waits_for_retry_after(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A 429 waits as long as the server asks, when that is longer than the backoff."""
    _serve(monkeypatch, _http_error(429, Retry_After="30"), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert sleeps == [30]


@pytest.mark.usefixtures("upstream")
def test_fetch_caps_the_retry_after_wait(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A server asking for a day's wait cannot stall the run for a day."""
    _serve(monkeypatch, _http_error(429, Retry_After="86400"), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert sleeps == [120]


@pytest.mark.usefixtures("upstream")
def test_a_refetch_is_conditional_and_a_304_keeps_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged source is not downloaded again, and its fetch record stays as it was."""
    modified = "Wed, 13 Mar 2024 15:21:06 GMT"
    calls = _serve(monkeypatch, _Response(FIXTURE.encode(), Last_Modified=modified, ETag='"abc123"'), _http_error(304))
    assert _run("fetch") == [0]
    record = Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8")
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert calls[0].get_header("If-none-match") is None
    assert (calls[1].get_header("If-none-match"), calls[1].get_header("If-modified-since")) == ('"abc123"', modified)
    assert Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8") == record
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 1
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.usefixtures("upstream")
def test_a_raw_file_changed_since_its_fetch_is_fetched_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edited bytes are not kept on a 304: the request is unconditional."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc123"'), _Response(FIXTURE.encode()))
    _run("fetch")
    RAW.write_text("edited\n", encoding="utf-8")
    assert _run("fetch") == [0]
    assert calls[1].get_header("If-none-match") is None
    assert RAW.read_text(encoding="utf-8") == FIXTURE


def test_a_new_sha256_pin_makes_the_refetch_unconditional(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 304 cannot vouch for bytes the manifest no longer pins, so they are fetched and checked again."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc123"'), _Response(FIXTURE.encode()))
    _run("fetch")
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{"0" * 64}"')
    assert _run("fetch") == [1]
    assert calls[1].get_header("If-none-match") is None


@pytest.mark.usefixtures("upstream")
def test_fetch_does_not_retry_a_client_error(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A 404 will not fix itself, so it fails at once."""
    calls = _serve(monkeypatch, _http_error(404))
    assert _run("fetch") == [1]
    assert (len(calls), sleeps) == (1, [])


@pytest.mark.usefixtures("upstream")
def test_fetch_rejects_a_body_shorter_than_its_content_length(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated body is retried, and never saved as if it were the whole file."""
    calls = _serve(monkeypatch, *[_Response(FIXTURE.encode()[:5], length=len(FIXTURE)) for _ in range(4)])
    assert _run("fetch") == [1]
    assert len(calls) == 4
    assert "body ended after 5 of the 29 bytes" in capsys.readouterr().err
    assert not RAW.exists()


@pytest.mark.usefixtures("upstream")
def test_fetch_records_last_modified_and_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validators a later run needs for a conditional request are kept in the fetch record."""
    modified = "Wed, 13 Mar 2024 15:21:06 GMT"
    _serve(monkeypatch, _Response(FIXTURE.encode(), Last_Modified=modified, ETag='"abc123"'))
    assert _run("fetch") == [0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert (record["last_modified"], record["etag"]) == (modified, '"abc123"')
    assert _run("build") == [0]


@pytest.mark.usefixtures("upstream")
def test_build_reads_a_fetch_record_from_before_the_http_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record without last_modified and etag, as older fetches wrote, still builds."""
    _serve(monkeypatch, _Response(FIXTURE.encode()))
    _run("fetch")
    path = Path("data/raw/example.csv.fetch.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["last_modified"], record["etag"], record["archive_url"]
    path.write_text(json.dumps(record), encoding="utf-8")
    assert _run("build") == [0]


@pytest.mark.usefixtures("upstream")
def test_fetch_refuses_a_download_over_the_size_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A URL that serves far more than a small cut fails before anything is written."""
    monkeypatch.setattr(ledger, "_MAX_BYTES", len(FIXTURE) - 1)
    assert _run("fetch") == [1]
    assert "exceeds" in capsys.readouterr().err
    assert not RAW.exists()


def test_a_source_over_the_default_cap_fetches_with_its_own_max_bytes(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source bigger than the default on purpose raises its own cap in sources.toml."""
    monkeypatch.setattr(ledger, "_MAX_BYTES", len(FIXTURE) - 1)
    _manifest(tmp_path, upstream.as_uri(), terms=f"max_bytes = {len(FIXTURE)}")
    assert _run("fetch") == [0]
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.parametrize("cap", ["0", "true", '"1 GB"'])
def test_max_bytes_must_be_a_positive_whole_number(
    upstream: Path, tmp_path: Path, cap: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cap that is not a byte count is a manifest error, not a silent 0- or 1-byte limit."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"max_bytes = {cap}")
    assert _run("fetch") == [1]
    assert "positive whole number" in capsys.readouterr().err


def test_an_empty_manifest_is_no_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest with no `[[source]]` yet is valid: nothing to fetch, build or check."""
    (tmp_path / "sources.toml").write_text("# no sources yet\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert ledger.load_manifest() == []
    assert _run("fetch", "build", "validate") == [0, 0, 0]


def test_build_refuses_a_fetch_made_for_a_different_request(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Changing a source's URL without re-fetching must not pass the old bytes off as the new request."""
    _run("fetch")
    moved = upstream.with_name("moved.csv")
    moved.write_text(FIXTURE, encoding="utf-8")
    _manifest(tmp_path, moved.as_uri())
    assert _run("build") == [1]
    assert "re-run fetch" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_build_refuses_a_raw_file_changed_after_fetch(capsys: pytest.CaptureFixture[str]) -> None:
    """Checksums are verified before anything is parsed."""
    _run("fetch")
    RAW.write_text(FIXTURE + "2022,9.9\n", encoding="utf-8")
    assert _run("build") == [1]
    assert "changed since it was fetched" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("year,value\n2020,1.5,extra\n2021,2.5\n", "more fields than the header"),
        ("year,value\n2020\n2021,2.5\n", "fewer fields than the header"),
    ],
)
def test_build_refuses_a_row_that_does_not_fit_the_header(
    upstream: Path, text: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A long row would add a column named None; a short one would publish empty values."""
    upstream.write_text(text, encoding="utf-8")
    _run("fetch")
    assert _run("build") == [1]
    err = capsys.readouterr().err
    assert expected in err
    assert len(err.strip().splitlines()) == 1
    assert not OUT.exists()


def test_build_keeps_the_header_of_a_source_with_no_rows(upstream: Path) -> None:
    """Columns come from the header, so a header-only source still publishes them."""
    upstream.write_text("year,value\n", encoding="utf-8")
    assert _run("fetch", "build") == [0, 0]
    assert OUT.read_text(encoding="utf-8").splitlines() == [
        "source_id,source_url,retrieved_at,vintage,raw_sha256,year,value"
    ]


def test_validate_reports_an_output_with_no_rows(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A source that builds to nothing is a problem, not a quietly empty publication."""
    upstream.write_text("year,value\n", encoding="utf-8")
    assert _run("fetch", "build", "validate") == [0, 0, 1]
    assert "no rows" in capsys.readouterr().err


def test_build_refuses_a_repeated_column_name(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two columns with one name would publish the second's values under both."""
    upstream.write_text("a,a\n1,2\n", encoding="utf-8")
    assert _run("fetch", "build") == [0, 1]
    assert "appear more than once in the header" in capsys.readouterr().err
    assert not OUT.exists()


def test_build_drops_a_byte_order_mark(upstream: Path) -> None:
    """A spreadsheet export's BOM does not end up in the first column's name."""
    upstream.write_bytes(b"\xef\xbb\xbf" + FIXTURE.encode())
    assert _run("fetch", "build") == [0, 0]
    assert [r["year"] for r in ledger.read_output(OUT)] == ["2020", "2021"]


@pytest.mark.parametrize(
    ("stage", "path", "text", "expected"),
    [
        ("fetch", "sources.toml", '[[source]]\nid = "x"\nstatus = "implemented"\n', "no url"),
        ("fetch", "sources.toml", "[[source]]\nid = 1\n", "needs a string id"),
        ("build", "data/raw/example.csv.fetch.json", "{}", "not a fetch record"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_a_hand_edited_file_is_one_line_and_a_failed_exit(
    stage: str, path: str, text: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expected mistakes in the manifest or a fetch record are reported, not raised as a traceback."""
    _run("fetch")
    Path(path).write_text(text, encoding="utf-8")
    capsys.readouterr()
    assert _run(stage) == [1]
    err = capsys.readouterr().err
    assert expected in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.usefixtures("upstream")
def test_validate_rejects_an_unknown_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped status is named as such, not reported as a missing file."""
    manifest = tmp_path / "sources.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace('"unimplemented"', '"unimplmented"'), encoding="utf-8")
    assert _run("validate") == [1]
    assert "status must be" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_a_missing_input_is_one_line_and_a_failed_exit(capsys: pytest.CaptureFixture[str]) -> None:
    """Building before fetching names the missing file instead of printing a traceback."""
    assert _run("build") == [1]
    err = capsys.readouterr().err
    assert "example.csv.fetch.json is missing" in err
    assert len(err.strip().splitlines()) == 1


def test_a_failed_download_is_one_line_and_a_failed_exit(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead URL is reported like any other expected failure, not as a traceback."""
    _manifest(tmp_path, upstream.with_name("gone.csv").as_uri())
    assert _run("fetch") == [1]
    err = capsys.readouterr().err
    assert err.startswith("fetch: example: fetching ")
    assert len(err.strip().splitlines()) == 1


@pytest.mark.usefixtures("upstream")
def test_the_manifest_rejects_a_repeated_id(capsys: pytest.CaptureFixture[str]) -> None:
    """Ids name the raw and output files, so a repeat would overwrite the first source's."""
    Path("sources.toml").write_text('[[source]]\nid = "a"\n' * 2, encoding="utf-8")
    assert _run("fetch") == [1]
    assert "appear more than once" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_build_refuses_a_column_named_like_a_provenance_column(
    upstream: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source column must not be silently overwritten by the provenance stamp."""
    upstream.write_text("year,source_url\n2020,x\n", encoding="utf-8")
    assert _run("fetch", "build") == [0, 1]
    assert "collide with the provenance columns" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_validate_reports_missing_output_as_a_problem(capsys: pytest.CaptureFixture[str]) -> None:
    """Validate lists a source it cannot check instead of stopping at it."""
    _run("fetch")
    assert _run("validate") == [1]
    out, err = capsys.readouterr()
    assert "2 problems" in out
    assert "data/processed/example.csv is missing" in err
    assert "datapackage.json is missing; re-run build" in err


@pytest.mark.usefixtures("upstream")
def test_validate_rehashes_the_raw_file(capsys: pytest.CaptureFixture[str]) -> None:
    """A raw file edited after the build fails validation."""
    _run("fetch", "build")
    RAW.write_text("edited\n", encoding="utf-8")
    assert _run("validate") == [1]
    assert "changed since it was fetched" in capsys.readouterr().err


def test_validate_rereads_the_published_output(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Output built from an earlier fetch fails once the source is fetched again with new bytes."""
    _run("fetch", "build")
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    _run("fetch")
    assert _run("validate") == [1]
    assert "re-run build" in capsys.readouterr().err


def test_validate_rejects_output_of_a_source_no_longer_in_the_manifest(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Output left behind by a removed or renamed source is stale, not published data."""
    _run("fetch", "build")
    _manifest(tmp_path, upstream.as_uri())
    manifest = tmp_path / "sources.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"example"', '"renamed"'), encoding="utf-8")
    _run("fetch", "build")
    assert _run("validate") == [1]
    assert "example.csv: no source" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_validate_rejects_output_from_an_unimplemented_source(capsys: pytest.CaptureFixture[str]) -> None:
    """An `unimplemented` source is absent from the output by design, so output from one is an error."""
    _run("fetch", "build")
    OUT.with_name("later.csv").write_text("year\n2020\n", encoding="utf-8")
    assert _run("validate") == [1]
    assert "later: status is unimplemented" in capsys.readouterr().err


def test_validate_requires_reuse_terms(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every implemented source states its publisher's terms before its data is used."""
    _manifest(tmp_path, upstream.as_uri(), terms="")
    _run("fetch", "build")
    assert _run("validate") == [1]
    assert "example: no terms" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("license", '""', "example: no license"),
        ("redistribute", '"yes"', "example: redistribute must be true or false"),
    ],
)
def test_validate_requires_machine_readable_terms(
    upstream: Path, tmp_path: Path, key: str, value: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A release step can only check terms it can read: an SPDX id and a yes-or-no on republishing."""
    kept = "\n".join(t for t in TERMS.splitlines() if not t.startswith(key))
    _manifest(tmp_path, upstream.as_uri(), terms=f"{kept}\n{key} = {value}")
    _run("fetch", "build")
    assert _run("validate") == [1]
    assert expected in capsys.readouterr().err


def test_the_fetch_log_keeps_each_new_version_once(upstream: Path) -> None:
    """A re-download of the same bytes adds nothing; a publisher revision adds one line."""
    _run("fetch", "fetch")
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    _run("fetch")
    lines = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
    assert [r["sha256"] for r in lines] == [
        hashlib.sha256(FIXTURE.encode()).hexdigest(),
        hashlib.sha256((FIXTURE + "2022,3.5\n").encode()).hexdigest(),
    ]
    assert lines[-1] == json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))


def test_revisions_names_a_source_whose_bytes_changed(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The weekly report lists a revised source, and neither an unchanged one nor one new to the log."""
    _run("fetch")
    before = tmp_path / "before.jsonl"
    before.write_text(LOG.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["revisions"])
    capsys.readouterr()
    assert main(["revisions", str(before)]) == 0
    assert capsys.readouterr().out == ""

    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    _run("fetch")
    capsys.readouterr()
    assert main(["revisions", str(before)]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    old, new = (hashlib.sha256(t.encode()).hexdigest()[:12] for t in (FIXTURE, FIXTURE + "2022,3.5\n"))
    assert line.startswith(f"- `example`: {old} -> {new}, ")

    before.write_text("", encoding="utf-8")
    capsys.readouterr()
    assert main(["revisions", str(before)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("upstream")
def test_a_source_id_is_a_plain_file_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An id names files under data/, so one that is a path is refused before anything is written."""
    manifest = tmp_path / "sources.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"example"', '"../escaped"'), encoding="utf-8")
    assert _run("fetch") == [1]
    assert "../escaped" in capsys.readouterr().err
    assert not Path("data").exists()


@pytest.mark.usefixtures("upstream")
def test_a_project_passes_its_own_parser_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` builds with the project's parser and fetches under the project's name."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))

    def marked(source: ledger.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        columns, rows = ledger.parse_csv(source, payload)
        return columns, [{**row, "year": f"y{row['year']}"} for row in rows]

    assert main(["fetch"], user_agent="my-dataset") == 0
    assert calls[0].get_header("User-agent") == "my-dataset"
    assert main(["build"], parse=marked) == 0
    assert [r["year"] for r in ledger.read_output(OUT)] == ["y2020", "y2021"]


def test_the_default_user_agent_names_the_library() -> None:
    """A fetch without a project name still says who is asking."""
    assert ledger.USER_AGENT.startswith("sourceledger/")


def _two_sources(root: Path, first: str, second: str) -> None:
    """Two implemented sources, `a` at `first` and `b` at `second`."""
    text = "".join(
        f'[[source]]\nid = "{sid}"\nstatus = "implemented"\nurl = "{url}"\n{TERMS}\n\n'
        for sid, url in (("a", first), ("b", second))
    )
    (root / "sources.toml").write_text(text, encoding="utf-8")


def test_one_failing_source_does_not_stop_the_others(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every source is tried; the run fails, names the failed one, and keeps what the others fetched."""
    _two_sources(tmp_path, upstream.with_name("gone.csv").as_uri(), upstream.as_uri())
    assert _run("fetch") == [1]
    out, err = capsys.readouterr()
    assert err.startswith("fetch: a: fetching ")
    assert out.startswith("b ")
    assert Path("data/raw/b.csv").read_text(encoding="utf-8") == FIXTURE
    assert not Path("data/raw/a.csv").exists()


@pytest.mark.usefixtures("upstream")
def test_a_version_the_log_missed_is_logged_on_the_next_304(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fetch that died after writing the record but before logging it loses no history."""
    _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc"'), _http_error(304))
    _run("fetch")
    LOG.unlink()
    assert _run("fetch") == [0]
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 1


def test_a_cut_off_last_log_line_is_dropped_not_fatal(upstream: Path, tmp_path: Path) -> None:
    """An interrupted append leaves half a line; the next fetch drops it and carries on."""
    _run("fetch")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write('{"source_id": "exa')
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    before = tmp_path / "before.jsonl"
    before.write_text(LOG.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["revisions", str(before)]) == 0
    assert _run("fetch") == [0]
    lines = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2


def test_a_whole_last_log_line_without_its_newline_is_kept(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An editor or a merge can drop the final newline; the record is history, not a cut-off append."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", 4)  # find the line start across several reads
    _run("fetch")
    first = LOG.read_text(encoding="utf-8")
    LOG.write_text(first.removesuffix("\n"), encoding="utf-8")
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    assert _run("fetch") == [0]
    lines = LOG.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0] == first
    assert "dropping" not in caplog.text


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": 1},
        {"source_id": "a", "retrieved_at": "t", "sha256": "0" * 64, "bytes": 1},
        {"source_id": "a", "url": "u", "sha256": "0" * 64, "bytes": 1},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0", "bytes": 1},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": 123, "bytes": 1},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0" * 64},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": True},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": "1"},
    ],
)
def test_a_log_line_missing_a_field_or_of_the_wrong_type_is_not_a_record(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    """Revisions print the size, time and URL, so a line without them is reported, not a KeyError."""
    before, log = tmp_path / "before.jsonl", tmp_path / "log.jsonl"
    before.write_text(_record("a", "1") + "\n", encoding="utf-8")
    log.write_text(json.dumps(fields) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"log\.jsonl:1 is not a fetch record$"):
        ledger.revisions(before, log=log)


@pytest.mark.parametrize(("key", "value"), [("sha256", 123), ("bytes", "29"), ("url", None)])
def test_a_fetch_record_of_the_wrong_type_is_fetched_again(
    upstream: Path, key: str, value: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validate reports it as a problem and fetch replaces it, rather than either stopping on a traceback."""
    _run("fetch", "build")
    record_path = Path("data/raw/example.csv.fetch.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record_path.write_text(json.dumps({**record, key: value}), encoding="utf-8")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert "example.csv.fetch.json is not a fetch record; re-run fetch" in capsys.readouterr().err
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert json.loads(record_path.read_text(encoding="utf-8"))["url"] == upstream.as_uri()


def test_a_pin_is_compared_as_given_by_a_library_caller(upstream: Path, tmp_path: Path) -> None:
    """A pin that skipped load_manifest is compared as written, by fetch and verified alike."""
    digest = hashlib.sha256(FIXTURE.encode()).hexdigest()
    source = {"id": "example", "status": "implemented", "url": upstream.as_uri(), "sha256": digest.upper()}
    raw = tmp_path / "raw"
    with pytest.raises(ledger.FetchError, match=r"not the sha256 in sources\.toml$"):
        ledger.fetch([source], raw=raw, log=tmp_path / "log.jsonl")
    ledger.fetch([{**source, "sha256": digest}], raw=raw, log=tmp_path / "log.jsonl")
    with pytest.raises(ValueError, match=r"not the sha256 sources\.toml now pins; re-run fetch$"):
        ledger.verified(source, raw=raw)


@pytest.mark.usefixtures("upstream")
def test_a_bad_line_inside_the_log_is_an_error_that_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    """Only a cut-off last line is forgiven: damage earlier in the history is reported."""
    LOG.parent.mkdir(parents=True)
    LOG.write_text('not json\n{"source_id": "x", "sha256": "0"}\n', encoding="utf-8")
    assert _run("fetch") == [1]
    assert "fetches.jsonl:1 is not a fetch record" in capsys.readouterr().err


@pytest.mark.parametrize("line", ["[1]", "{}"])
@pytest.mark.usefixtures("upstream")
def test_a_log_line_that_is_not_a_record_breaks_only_fetch(line: str, capsys: pytest.CaptureFixture[str]) -> None:
    """JSON that is not a record is reported as one; build, validate and verify never read the log."""
    _run("fetch", "build")
    LOG.write_text(line + "\n", encoding="utf-8")
    capsys.readouterr()
    assert _run("fetch") == [1]
    assert capsys.readouterr().err == "fetch: data/fetches.jsonl:1 is not a fetch record\n"
    assert _run("build", "validate") == [0, 0]
    assert main(["verify", "data/processed"]) == 0


def test_a_sha256_pin_in_upper_case_matches(upstream: Path, tmp_path: Path) -> None:
    """Windows tools print hashes in upper case; the pin still matches the bytes."""
    pin = hashlib.sha256(FIXTURE.encode()).hexdigest().upper()
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsha256 = "{pin}"')
    assert _run("fetch", "build", "validate") == [0, 0, 0]


def test_a_sha256_pin_must_be_a_sha256(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A truncated or mistyped pin is a manifest error, not a download that never matches."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsha256 = "abc123"')
    assert _run("fetch") == [1]
    assert "64 hexadecimal characters" in capsys.readouterr().err


def test_build_checks_the_pin_too(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A pin added after the fetch applies to build and validate, not only to the next fetch."""
    _run("fetch")
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsha256 = "{"0" * 64}"')
    capsys.readouterr()
    assert _run("build", "validate") == [1, 1]
    assert "not the sha256 sources.toml now pins" in capsys.readouterr().err


def test_a_new_vintage_needs_a_new_fetch(upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Records and output never keep a vintage the manifest no longer declares."""
    _run("fetch", "build")
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nvintage = "2025"')
    assert _run("validate") == [1]
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert calls[0].get_header("If-none-match") is None
    assert {r["vintage"] for r in ledger.read_output(OUT)} == {"2025"}


def test_the_old_key_release_is_refused_with_its_new_name(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kept as an unknown key, `release` would silently leave the vintage "not stated"."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nrelease = "2024"')
    assert _run("fetch") == [1]
    assert "`release` in sources.toml is now `vintage`" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_validate_compares_every_provenance_column(capsys: pytest.CaptureFixture[str]) -> None:
    """Output whose retrieval time or vintage no longer matches the record is stale."""
    _run("fetch", "build")
    OUT.write_text(OUT.read_text(encoding="utf-8").replace("Z,", "Z0,"), encoding="utf-8")
    assert _run("validate") == [1]
    assert "re-run build" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_a_parser_cannot_overwrite_a_provenance_column() -> None:
    """The stamp is written last, so a parser's row keys never replace it."""

    def sneaky(source: ledger.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        columns, rows = ledger.parse_csv(source, payload)
        return columns, [{**row, "vintage": "forged"} for row in rows]

    _run("fetch")
    assert main(["build"], parse=sneaky) == 0
    assert {r["vintage"] for r in ledger.read_output(OUT)} == {"not stated"}


def test_revisions_needs_the_earlier_log_to_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing copy is an error, not an empty report; and revisions needs no sources.toml."""
    monkeypatch.chdir(tmp_path)
    assert main(["revisions", "missing.jsonl"]) == 1
    assert "missing.jsonl is missing" in capsys.readouterr().err
    (tmp_path / "before.jsonl").write_text("", encoding="utf-8")
    assert main(["revisions", "before.jsonl"]) == 1
    assert "data/fetches.jsonl is missing" in capsys.readouterr().err
    LOG.parent.mkdir()
    LOG.write_text("", encoding="utf-8")
    assert main(["revisions", "before.jsonl"]) == 0


@pytest.mark.usefixtures("upstream")
def test_a_content_length_that_is_not_a_number_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed header can't verify the length, so it doesn't fail the fetch either."""
    response = _Response(FIXTURE.encode())
    response.headers.replace_header("Content-Length", "29, 29")
    _serve(monkeypatch, response)
    assert _run("fetch") == [0]


@pytest.mark.usefixtures("upstream")
def test_a_compressed_body_is_refused(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Hashing gzip bytes would record something that is not the file the publisher serves."""
    _serve(monkeypatch, _Response(FIXTURE.encode(), Content_Encoding="gzip"))
    assert _run("fetch") == [1]
    assert "Content-Encoding gzip" in capsys.readouterr().err
    assert not RAW.exists()


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("1", [5]), ("²", [5]), ("soon", [5])],
)
@pytest.mark.usefixtures("upstream")
def test_a_short_or_unreadable_retry_after_waits_the_backoff(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], retry_after: str, expected: list[float]
) -> None:
    """Retry-After raises the wait above the backoff, never below it, and junk is ignored."""
    _serve(monkeypatch, _http_error(429, Retry_After=retry_after), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert sleeps == expected


@pytest.mark.usefixtures("upstream")
def test_retry_after_as_an_http_date_is_honoured(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """RFC 9110 lets Retry-After be a date; a minute from now waits about a minute."""
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    _serve(monkeypatch, _http_error(503, Retry_After=when), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert 55 <= sleeps[0] <= 60


@pytest.mark.usefixtures("upstream")
def test_a_status_that_never_recovers_is_not_retried(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """501 Not Implemented will say the same thing in 20 seconds."""
    calls = _serve(monkeypatch, _http_error(501))
    assert _run("fetch") == [1]
    assert (len(calls), sleeps) == (1, [])


@pytest.mark.usefixtures("upstream")
def test_each_request_has_a_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that never answers can't hold the run forever."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    _run("fetch")
    assert calls[0].timeout == ledger._SOCKET_TIMEOUT_S  # noqa: SLF001


@pytest.mark.usefixtures("upstream")
def test_a_304_to_an_unconditional_request_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without validators there is no earlier copy for a 304 to point at."""
    _serve(monkeypatch, _http_error(304))
    assert _run("fetch") == [1]


@pytest.mark.usefixtures("upstream")
def test_a_download_that_outlasts_its_budget_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server trickling bytes inside every socket timeout is still stopped."""
    monkeypatch.setattr(ledger, "_BUDGET_S", -1)
    _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch") == [1]
    assert "unfinished after" in capsys.readouterr().err


def test_a_server_trickling_bytes_is_stopped_near_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A byte every 0.2 s never trips the socket timeout, yet the fetch stops at the budget, not the body's end."""
    monkeypatch.setattr(ledger, "_BUDGET_S", 0.5)
    monkeypatch.setattr(ledger, "_SOCKET_TIMEOUT_S", 1)
    listener = socket.create_server(("127.0.0.1", 0))

    def trickle() -> None:
        connection, _ = listener.accept()
        with connection, contextlib.suppress(OSError):  # the client hangs up once it gives up
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n")
            pause = threading.Event()  # time.sleep is stubbed out for the fetch's own waits
            for _ in range(30):
                pause.wait(0.2)
                connection.sendall(b"x")

    threading.Thread(target=trickle, daemon=True).start()
    monkeypatch.chdir(tmp_path)
    _manifest(tmp_path, f"http://127.0.0.1:{listener.getsockname()[1]}/data.csv")
    start = time.monotonic()
    assert _run("fetch") == [1]
    # Within the budget plus one socket timeout; the whole body takes 6 s.
    assert time.monotonic() - start < 1.5 + 0.5
    assert "unfinished after 0.5 s" in capsys.readouterr().err
    listener.close()


def test_a_redirect_from_a_public_host_is_marked_public_only() -> None:
    """The mark carries on to every later hop; a redirect within an internal host gets none."""
    handler = ledger._NoRedirectInward()  # noqa: SLF001
    public = urllib.request.Request("http://93.184.215.14/data.csv")
    new = handler.redirect_request(public, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://10.0.0.1/a")
    assert new is not None
    assert new.public_only  # ty: ignore[unresolved-attribute]
    again = handler.redirect_request(new, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://10.0.0.2/b")
    assert again is not None
    assert again.public_only  # ty: ignore[unresolved-attribute]
    internal = urllib.request.Request("http://127.0.0.1/data.csv")
    moved = handler.redirect_request(
        internal, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://127.0.0.1/moved"
    )
    assert moved is not None
    assert not hasattr(moved, "public_only")


class _Rebinder(http.server.BaseHTTPRequestHandler):
    """/start.csv redirects to rebind.test on the same port; anything else is the internal secret."""

    def do_GET(self) -> None:
        if self.path == "/start.csv":
            self.send_response(302)
            port = self.headers["Host"].rsplit(":", 1)[1]
            self.send_header("Location", f"http://rebind.test:{port}/secret")
            self.end_headers()
            return
        body = b"secret\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # the base class names it
        """Keep test output quiet."""


def test_a_redirect_to_a_name_that_rebinds_inward_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A .test name answers public to the redirect check's lookup and loopback to the connect's one."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rebinder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = socket.getaddrinfo

    def rebinding(host: str, port: object, *args: object, **kwargs: object) -> object:
        if host.endswith(".test"):
            host = "93.184.215.14" if port is None else "127.0.0.1"
        return real(host, port, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    for proxy in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(proxy, raising=False)
    monkeypatch.chdir(tmp_path)
    _manifest(tmp_path, f"http://origin.test:{server.server_address[1]}/start.csv")
    try:
        assert _run("fetch") == [1]
    finally:
        server.shutdown()
    assert "example: refused a redirect from a public host to http://rebind.test" in capsys.readouterr().err
    assert not RAW.exists()


def test_the_peer_check_is_skipped_through_a_proxy() -> None:
    """Through a proxy the socket reaches the proxy, not the host the URL names."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rebinder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        for url, refused in ((f"http://127.0.0.1:{port}/a.csv", True), ("http://example.org/a.csv", False)):
            request = urllib.request.Request(url)  # noqa: S310  # http only
            request.public_only = True  # ty: ignore[unresolved-attribute]
            connection = ledger._PeerCheck("127.0.0.1", port=port, req=request)  # noqa: SLF001
            if refused:
                with pytest.raises(
                    ValueError, match=rf"^refused a redirect from a public host to {url}, which is at 127\.0\.0\.1$"
                ):
                    connection.connect()
            else:
                connection.connect()
            connection.close()
    finally:
        server.shutdown()


@pytest.mark.usefixtures("upstream")
def test_a_symlink_where_the_temp_file_would_go_is_not_followed(tmp_path: Path) -> None:
    """Temp files get fresh names, so a planted link can't redirect a write."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    RAW.parent.mkdir(parents=True)
    RAW.with_name(RAW.name + ".part").symlink_to(victim)
    assert _run("fetch") == [0]
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert RAW.read_text(encoding="utf-8") == FIXTURE


class _Publisher(http.server.BaseHTTPRequestHandler):
    """A real HTTP server: /data.csv with an ETag and 304s, and /old.csv redirecting to it."""

    def do_GET(self) -> None:
        if self.path == "/old.csv":
            self.send_response(302)
            self.send_header("Location", "/data.csv")
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.end_headers()
            return
        body = FIXTURE.encode()
        self.send_response(200)
        self.send_header("ETag", '"v1"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # the base class names it
        """Keep test output quiet."""


@pytest.fixture
def publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The base URL of a `_Publisher` on a free local port; the test runs in a project directory."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Publisher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.chdir(tmp_path)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_real_http_fetches_redirects_and_304s(publisher: str, tmp_path: Path) -> None:
    """Against a real server: the final URL is recorded, and the second fetch is a 304 that keeps the record."""
    _manifest(tmp_path, f"{publisher}/old.csv")
    assert _run("fetch") == [0]
    record_path = Path("data/raw/example.csv.fetch.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert (record["etag"], record["final_url"]) == ('"v1"', f"{publisher}/data.csv")
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert json.loads(record_path.read_text(encoding="utf-8")) == record


@pytest.mark.skipif(sys.platform == "win32", reason="the lock is POSIX only")
@pytest.mark.usefixtures("upstream")
def test_a_symlinked_lock_file_leaves_its_target_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A checkout can carry a symlink at data/raw/.lock; taking the lock must not empty the file it points at."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    RAW.parent.mkdir(parents=True)
    (RAW.parent / ".lock").symlink_to(victim)
    assert _run("fetch") == [1]
    assert "data/raw/.lock" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "keep me"
    (RAW.parent / ".lock").unlink()
    (RAW.parent / ".lock").write_text("held before", encoding="utf-8")
    assert _run("fetch") == [0]
    assert (RAW.parent / ".lock").read_text(encoding="utf-8") == "held before"


@pytest.mark.usefixtures("upstream")
def test_a_symlinked_fetch_log_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Appending through a planted symlink would write into another file."""
    victim = tmp_path / "victim.txt"
    victim.write_text("", encoding="utf-8")
    LOG.parent.mkdir(parents=True)
    LOG.symlink_to(victim)
    assert _run("fetch") == [1]
    assert (
        capsys.readouterr().err == "fetch: data/fetches.jsonl is a symlink; the fetch log must be a file of its own\n"
    )
    assert victim.read_text(encoding="utf-8") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="the lock is POSIX only")
@pytest.mark.usefixtures("upstream")
def test_a_second_fetch_at_once_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """Two runs interleaving their writes could pair one run's raw file with the other's record."""
    RAW.parent.mkdir(parents=True)
    with (RAW.parent / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert _run("fetch") == [1]
    assert "another fetch is already running" in capsys.readouterr().err
    assert _run("fetch") == [0]


@pytest.mark.usefixtures("upstream")
def test_a_download_streams_to_disk_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than one chunk arrives whole, and a refused one leaves no temp file behind."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", 4)
    assert _run("fetch") == [0]
    assert RAW.read_text(encoding="utf-8") == FIXTURE
    monkeypatch.setattr(ledger, "_MAX_BYTES", 10)
    RAW.write_text("edited\n", encoding="utf-8")  # forces a full fetch
    assert _run("fetch") == [1]
    assert not list(RAW.parent.glob("*.part"))


def test_each_fetch_line_says_new_or_unchanged(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The first fetch and a publisher revision are new; the same bytes again are unchanged."""
    _run("fetch")
    assert capsys.readouterr().out.split()[1] == "new"
    _run("fetch")
    assert capsys.readouterr().out.split()[1] == "unchanged"
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    _run("fetch")
    assert capsys.readouterr().out.split()[1] == "new"


@pytest.mark.parametrize(
    ("name", "body", "extra"),
    [
        ("example.json", b'[{"year": 2020, "value": 1.5}, {"year": 2021, "value": 2.5, "note": null}]', ""),
        ("example.csv.gz", gzip.compress(FIXTURE.encode()), ""),
        ("example.data", b'[{"year": 2020, "value": 1.5}, {"year": 2021, "value": 2.5}]', 'format = "json"'),
    ],
)
def test_the_default_reader_follows_the_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: bytes, extra: str
) -> None:
    """JSON and gzipped CSV need no code of the project's own; `format` overrides the extension."""
    upstream = tmp_path / "upstream" / name
    upstream.parent.mkdir()
    upstream.write_bytes(body)
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\n{extra}")
    monkeypatch.chdir(tmp_path)
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    rows = ledger.read_output(OUT)
    assert [(r["year"], r["value"]) for r in rows] == [("2020", "1.5"), ("2021", "2.5")]


def test_a_gzip_that_expands_past_max_bytes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A small compressed file can decompress without limit; the cap applies to what it expands to."""
    upstream = tmp_path / "upstream" / "example.csv.gz"
    upstream.parent.mkdir()
    upstream.write_bytes(gzip.compress(b"year,value\n" + b"2020,1.5\n" * 1000))
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\nmax_bytes = 2000")
    monkeypatch.chdir(tmp_path)
    assert _run("fetch", "build") == [0, 1]
    assert "decompresses past 2000 bytes" in capsys.readouterr().err


def test_json_values_become_text() -> None:
    """Missing keys are empty, booleans are lower case, nested values stay JSON."""
    payload = b'[{"a": true, "b": {"c": 1}}, {"a": null, "d": "x"}]'
    columns, rows = ledger.parse_json({"id": "j"}, payload)
    assert columns == ["a", "b", "d"]
    assert rows == [{"a": "true", "b": '{"c": 1}', "d": ""}, {"a": "", "b": "", "d": "x"}]
    with pytest.raises(ValueError, match="array of objects"):
        ledger.parse_json({"id": "j"}, b'{"a": 1}')


def test_an_excel_sheet_reads_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The xlsx extra reads the named sheet, header first, skipping empty rows."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.title = "notes"
    sheet = book.create_sheet("data")
    for row in (("year", "value"), (2020, 1.5), (None, None), (2021, 2.5)):
        sheet.append(row)
    upstream = tmp_path / "upstream" / "example.xlsx"
    upstream.parent.mkdir()
    book.save(upstream)
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsheet = "data"')
    monkeypatch.chdir(tmp_path)
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert [(r["year"], r["value"]) for r in ledger.read_output(OUT)] == [("2020", "1.5"), ("2021", "2.5")]


@pytest.mark.usefixtures("upstream")
def test_a_header_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The token reaches the request, its variable's name reaches the record, its value doesn't."""
    _manifest(
        tmp_path, "https://example.invalid/data.csv", terms=f'{TERMS}\nheaders = {{ Authorization = "env:SL_TOKEN" }}'
    )
    monkeypatch.setenv("SL_TOKEN", "Bearer s3cret")
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert calls[0].get_header("Authorization") == "Bearer s3cret"
    record = Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8")
    assert json.loads(record)["env_headers"] == ["Authorization"]
    assert "s3cret" not in record
    assert "s3cret" not in LOG.read_text(encoding="utf-8")


@pytest.mark.usefixtures("upstream")
def test_a_missing_variable_fails_that_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unset token is named, and nothing is fetched without it."""
    _manifest(
        tmp_path, "https://example.invalid/data.csv", terms=f'{TERMS}\nheaders = {{ Authorization = "env:SL_UNSET" }}'
    )
    assert _run("fetch") == [1]
    assert "environment variable SL_UNSET for the Authorization header is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ('{ Authorization = "Bearer s3cret" }', "never write a secret"),
        ('{ User-Agent = "env:UA" }', "sets the User-Agent header itself"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_a_literal_or_reserved_header_is_a_manifest_error(
    tmp_path: Path, headers: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Secrets stay out of the manifest, and the fetch's own headers stay the fetch's."""
    _manifest(tmp_path, "https://example.invalid/data.csv", terms=f"{TERMS}\nheaders = {headers}")
    assert _run("fetch") == [1]
    assert expected in capsys.readouterr().err


def test_a_redirect_to_another_host_drops_the_credentials() -> None:
    """Urllib copies every header to a redirect; a token for one host must not reach another."""
    handler = ledger._NoRedirectInward()  # noqa: SLF001
    req = urllib.request.Request("https://93.184.215.14/data.csv", headers={"Authorization": "x", "User-Agent": "ua"})
    other = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "https://8.8.8.8/data.csv"
    )
    same = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "https://93.184.215.14/v2.csv"
    )
    assert other is not None
    assert same is not None
    assert (other.get_header("Authorization"), other.get_header("User-agent")) == (None, "ua")
    assert same.get_header("Authorization") == "x"


def test_a_redirect_to_plain_http_on_the_same_host_drops_the_credentials() -> None:
    """A token sent over https must not go out again in clear text after a downgrade."""
    handler = ledger._NoRedirectInward()  # noqa: SLF001
    req = urllib.request.Request("https://93.184.215.14/data.csv", headers={"Authorization": "x", "User-Agent": "ua"})
    new = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://93.184.215.14/data.csv"
    )
    assert new is not None
    assert (new.get_header("Authorization"), new.get_header("User-agent")) == (None, "ua")


@pytest.mark.usefixtures("upstream")
def test_headers_on_a_plain_http_url_are_a_manifest_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A token would cross the network in clear text from the first request."""
    _manifest(
        tmp_path, "http://example.invalid/data.csv", terms=f'{TERMS}\nheaders = {{ Authorization = "env:SL_TOKEN" }}'
    )
    assert _run("fetch") == [1]
    assert "example: headers in sources.toml need an https url" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_an_id_must_be_ascii(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An id names files, which must mean the same file on every system."""
    manifest = tmp_path / "sources.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"example"', '"exämple"'), encoding="utf-8")
    assert _run("fetch") == [1]
    assert "ASCII" in capsys.readouterr().err


_GOOD = f'[[source]]\nid = "a"\nstatus = "implemented"\nurl = "https://example.org/a.csv"\n{TERMS}\n'


@pytest.mark.parametrize(
    "text",
    [
        _GOOD,
        _GOOD + 'sha256 = "' + "A" * 64 + '"\nmax_bytes = 10\nformat = "json"\nsheet = "x"\nvintage = "2024"\n',
        _GOOD + 'headers = { Authorization = "env:TOKEN" }\ncolumn_map = "kept for the project\'s own reader"\n',
        _GOOD + "archive = true\n",
        _GOOD + 'archive = "yes"\n',
        '[[source]]\nid = "b"\nstatus = "unimplemented"\nreason = "later"\n',
        _GOOD.replace('id = "a"', 'id = "a/b"'),
        _GOOD.replace('status = "implemented"', 'status = "done"'),
        _GOOD.replace('license = "CC0-1.0"\n', ""),
        _GOOD.replace("redistribute = true", 'redistribute = "yes"'),
        _GOOD + 'sha256 = "abc"\n',
        _GOOD + "max_bytes = 0\n",
        _GOOD + 'format = "parquet"\n',
        _GOOD + 'headers = { Authorization = "Bearer x" }\n',
        _GOOD.replace("https://", "http://") + 'headers = { Authorization = "env:TOKEN" }\n',
        _GOOD.replace("https://", "HTTPS://") + 'headers = { Authorization = "env:TOKEN" }\n',
        _GOOD.replace('url = "https://example.org/a.csv"\n', ""),
        _GOOD + 'release = "2024"\n',
    ],
)
def test_the_schema_and_the_loader_agree(tmp_path: Path, text: str) -> None:
    """An editor using the published schema accepts exactly the manifests sourceledger accepts."""
    jsonschema = pytest.importorskip("jsonschema")
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    data = tomllib.loads(text)
    schema_ok = jsonschema.Draft202012Validator(SCHEMA).is_valid(data)
    try:
        sources = ledger.load_manifest(path)
    except ValueError:
        loader_ok = False
    else:
        bad_status = [s for s in sources if s["status"] not in (ledger.IMPLEMENTED, ledger.UNIMPLEMENTED)]
        terms = [p for s in ledger.implemented(sources) for p in ledger._terms_problems(s)]  # noqa: SLF001
        loader_ok = not bad_status and not terms
    assert schema_ok == loader_ok


def test_build_describes_its_outputs_as_a_data_package(upstream: Path) -> None:
    """datapackage.json is a valid Frictionless Data Package, with each output's hash, source and licence."""
    frictionless = pytest.importorskip("frictionless")
    _run("fetch", "build")
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    (resource,) = package["resources"]
    assert resource["hash"] == f"sha256:{hashlib.sha256(OUT.read_bytes()).hexdigest()}"
    assert resource["sources"] == [{"title": "example", "path": upstream.as_uri()}]
    assert resource["licenses"] == [{"name": "CC0-1.0", "path": "https://example.org/terms", "title": "CC0-1.0"}]
    assert resource["sourceledger"]["raw_sha256"] == hashlib.sha256(FIXTURE.encode()).hexdigest()
    report = frictionless.validate(str(path))
    assert report.valid, report.flatten(["type", "message"])


@pytest.mark.usefixtures("upstream")
def test_the_same_records_build_the_same_package() -> None:
    """No timestamps of its own: two builds from one fetch give the same bytes."""
    _run("fetch", "build")
    first = Path("data/processed/datapackage.json").read_bytes()
    _run("build")
    assert Path("data/processed/datapackage.json").read_bytes() == first


@pytest.mark.usefixtures("upstream")
def test_validate_catches_a_stale_data_package(capsys: pytest.CaptureFixture[str]) -> None:
    """An output edited after its build no longer matches the hash the package gives it."""
    _run("fetch", "build")
    OUT.write_text(OUT.read_text(encoding="utf-8").replace(",1.5", ",9.9"), encoding="utf-8")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert "describes an earlier build" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("not json", "is not JSON"),
        ('{"resources": []}', "not the implemented sources"),
        ("[]", "not the implemented sources"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_validate_reports_a_damaged_data_package(
    damage: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package that is unreadable or lists other sources is reported, not trusted."""
    _run("fetch", "build")
    Path("data/processed/datapackage.json").write_text(damage, encoding="utf-8")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert expected in capsys.readouterr().err


def test_init_writes_a_starter_manifest_and_never_overwrites_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The starter names the schema and loads as no sources. Its example is the README's, which test_docs runs."""
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    manifest = Path("sources.toml").read_text(encoding="utf-8")
    assert manifest.splitlines()[0] == f"#:schema {SCHEMA['$id']}"
    assert ledger.load_manifest() == []
    capsys.readouterr()
    assert main(["init"]) == 1
    assert capsys.readouterr().err == "init: sources.toml already exists; not overwriting it\n"
    assert Path("sources.toml").read_text(encoding="utf-8") == manifest


@pytest.mark.usefixtures("upstream")
def test_status_lists_every_source_without_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fetched, never-fetched and unimplemented sources each get a line from the manifest and the log."""
    text = (tmp_path / "sources.toml").read_text(encoding="utf-8")
    fresh = '[[source]]\nid = "fresh"\nstatus = "implemented"\nurl = "https://example.invalid/f"\nvintage = "2025"\n'
    (tmp_path / "sources.toml").write_text(text + "\n" + fresh, encoding="utf-8")
    _run("fetch")
    capsys.readouterr()
    _serve(monkeypatch)  # any request would fail the test
    assert main(["status"]) == 0
    record = json.loads(LOG.read_text(encoding="utf-8").splitlines()[0])
    assert capsys.readouterr().out.splitlines() == [
        (
            f"example  implemented    not stated  {record['retrieved_at']}  {record['sha256'][:12]}  CC0-1.0  "
            "redistribute=yes"
        ),
        "later    unimplemented  not stated  never  -  -  redistribute=?",
        "fresh    implemented    2025  never  -  -  redistribute=?",
    ]


def test_only_fetches_builds_and_validates_the_named_sources(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other sources are left alone, and the package keeps their entries from the last build."""
    other = upstream.with_name("b.csv")
    other.write_text(FIXTURE, encoding="utf-8")
    _two_sources(tmp_path, upstream.as_uri(), other.as_uri())
    assert main(["fetch", "--only", "a"]) == 0
    assert Path("data/raw/a.csv").exists()
    assert not Path("data/raw/b.csv").exists()
    assert main(["build", "--only", "a"]) == 0
    assert main(["validate", "--only", "a"]) == 0
    capsys.readouterr()
    assert main(["validate"]) == 1
    assert "not the implemented sources" in capsys.readouterr().err

    assert _run("fetch", "build") == [0, 0]
    package = Path("data/processed/datapackage.json")
    b_entry = json.loads(package.read_text(encoding="utf-8"))["resources"][1]
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    assert main(["fetch", "--only", "a", "--only", "a"]) == 0
    assert main(["build", "--only", "a"]) == 0
    assert json.loads(package.read_text(encoding="utf-8"))["resources"][1] == b_entry
    assert _run("validate") == [0]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["fetch", "--only", "zz"], "fetch: ids ['zz'] are not in sources.toml"),
        (["build", "--only", "zz"], "build: ids ['zz'] are not in sources.toml"),
        (["validate", "--only", "zz"], "validate: ids ['zz'] are not in sources.toml"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_only_refuses_an_unknown_id(argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped id fails before anything is written, instead of quietly doing nothing."""
    assert main(argv) == 1
    assert capsys.readouterr().err == expected + "\n"
    assert not Path("data").exists()


@pytest.mark.usefixtures("upstream")
def test_only_and_paths_go_with_their_stages(capsys: pytest.CaptureFixture[str]) -> None:
    """--only is for fetch, build and validate; verify, like revisions, needs its path."""
    with pytest.raises(SystemExit):
        main(["status", "--only", "example"])
    assert "--only works with fetch, build, validate, not status" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["verify"])
    assert "verify needs the folder of a published dataset" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_validate_only_skips_the_check_for_files_no_source_owns(capsys: pytest.CaptureFixture[str]) -> None:
    """An orphan output fails a full validate, but not one narrowed to a source."""
    _run("fetch", "build")
    Path("data/processed/old.csv").write_text("x\n", encoding="utf-8")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert "data/processed/old.csv: no source 'old' in sources.toml; delete it" in capsys.readouterr().err
    assert main(["validate", "--only", "example"]) == 0


def test_only_runs_in_manifest_order(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The order of --only flags does not reorder the sources; a lone string is refused, not split into letters."""
    _two_sources(tmp_path, upstream.as_uri(), upstream.as_uri())
    assert main(["fetch", "--only", "b", "--only", "a"]) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["a", "b"]
    with pytest.raises(TypeError, match="not the string 'ab'"):
        ledger.validate(ledger.load_manifest(), only="ab")


@pytest.mark.usefixtures("upstream")
def test_validate_only_reports_a_package_that_does_not_list_the_source(capsys: pytest.CaptureFixture[str]) -> None:
    """The package checks narrow to the named source too: missing, or without its entry."""
    _run("fetch", "build")
    package = Path("data/processed/datapackage.json")
    package.write_text('{"resources": []}', encoding="utf-8")
    capsys.readouterr()
    assert main(["validate", "--only", "example"]) == 1
    assert "datapackage.json does not list example; re-run build" in capsys.readouterr().err
    package.unlink()
    assert main(["validate", "--only", "example"]) == 1
    assert "datapackage.json is missing" in capsys.readouterr().err
    assert main(["validate", "--only", "later"]) == 0


def test_attribution_prints_a_markdown_table_in_manifest_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One row per implemented source, its name or id first, pipes escaped; unimplemented ones are left out."""
    (tmp_path / "sources.toml").write_text(
        """
[[source]]
id = "co2"
name = "Mauna Loa CO2"
status = "implemented"
url = "https://example.org/co2.csv"
terms = "Public domain | US government work"
terms_url = "https://gml.noaa.gov/about/disclaimer.html"
license = "LicenseRef-us-government-public-domain"
redistribute = true

[[source]]
id = "later"
status = "unimplemented"

[[source]]
id = "eurostat"
status = "implemented"
url = "https://example.org/e.csv"
terms = "Reuse authorised,\\nsource acknowledged"
terms_url = "https://example.org/terms"
license = "CC-BY-4.0"
redistribute = false
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["attribution"]) == 0
    assert capsys.readouterr().out == (
        "| Source | Terms | Licence | Terms URL | Redistribute |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Mauna Loa CO2 | Public domain \\| US government work | LicenseRef-us-government-public-domain"
        " | https://gml.noaa.gov/about/disclaimer.html | yes |\n"
        "| eurostat | Reuse authorised, source acknowledged | CC-BY-4.0 | https://example.org/terms | no |\n"
    )


@pytest.mark.usefixtures("upstream")
def test_verify_checks_a_published_dataset_against_its_package(capsys: pytest.CaptureFixture[str]) -> None:
    """A downloaded copy passes as built, and a tampered file is named with what no longer matches."""
    _run("fetch", "build")
    capsys.readouterr()
    assert main(["verify", "data/processed"]) == 0
    assert capsys.readouterr().out == "0 problems\n"
    built = OUT.stat().st_size
    OUT.write_text(OUT.read_text(encoding="utf-8") + "extra,row\n", encoding="utf-8")
    assert main(["verify", "data/processed"]) == 1
    out, err = capsys.readouterr()
    assert out == "2 problems\n"
    size_line, hash_line = err.splitlines()
    assert (
        size_line == f"data/processed/example.csv is {OUT.stat().st_size:,} bytes, not the {built:,} the package lists"
    )
    assert hash_line.startswith("data/processed/example.csv hashes to sha256:")


# The hash of "x\n", the file the verify tests list.
_X_HASH = f"sha256:{hashlib.sha256(b'x\n').hexdigest()}"


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        ([{"path": "gone.csv", "hash": "sha256:00"}], ["d/gone.csv is missing"]),
        ([{"path": "a.csv"}], ["d/a.csv: the package lists no sha256 hash to check it against"]),
        ([{"path": "../a.csv"}], ["d/datapackage.json: resource path '../a.csv' is not a file in d"]),
        ([{"path": "/etc/hostname"}], ["d/datapackage.json: resource path '/etc/hostname' is not a file in d"]),
        ([{"path": "https://example.org/a.csv"}], ["d/datapackage.json: resource path 'https://example.org/a.csv'"]),
        (["a.csv"], ["d/datapackage.json: resource path None is not a file in d"]),
        ({"a": 1}, ["d/datapackage.json has no list of resources"]),
        (
            [{"path": "a.csv", "bytes": "many", "hash": "sha256:00"}],
            ["d/a.csv is 2 bytes, not the 'many' the package lists", "d/a.csv hashes to sha256:"],
        ),
        ([{"path": "out.csv", "hash": _X_HASH}], ["d/datapackage.json: resource path 'out.csv' is not a file in d"]),
        ([{"path": "a.csv", "bytes": True, "hash": _X_HASH}], ["d/a.csv is 2 bytes, not the True the package lists"]),
        ([{"path": "a.csv", "bytes": 2, "hash": _X_HASH.upper()}], []),
    ],
)
def test_verify_reports_a_package_it_cannot_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resources: object, expected: list[str]
) -> None:
    """Missing files, missing hashes and paths that leave the folder are problems, not passes."""
    monkeypatch.chdir(tmp_path)
    Path("d").mkdir()
    Path("d/a.csv").write_text("x\n", encoding="utf-8")
    Path("outside.csv").write_text("x\n", encoding="utf-8")
    Path("d/out.csv").symlink_to(Path("../outside.csv"))
    Path("d/datapackage.json").write_text(json.dumps({"resources": resources}), encoding="utf-8")
    problems = ledger.verify(Path("d"))
    assert len(problems) == len(expected)
    assert all(p.startswith(e) for p, e in zip(problems, expected, strict=True))


def test_verify_needs_a_readable_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder without a package, or with a broken one, is one line and a failed exit."""
    monkeypatch.chdir(tmp_path)
    assert main(["verify", "."]) == 1
    assert capsys.readouterr().err == "verify: datapackage.json is missing\n"
    Path("datapackage.json").write_text("not json", encoding="utf-8")
    assert main(["verify", "."]) == 1
    assert capsys.readouterr().err.startswith("verify: Expecting value")


@pytest.mark.usefixtures("upstream")
def test_a_fetch_record_is_written_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every field of a plain fetch, in UTC, and the record file as indented JSON."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    zone = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-14"  # far from UTC, so a local timestamp shows
    time.tzset()
    try:
        assert main(["fetch"], user_agent="my-dataset") == 0
    finally:
        if zone is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = zone
        time.tzset()
    text = Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8")
    record = json.loads(text)
    assert text == json.dumps(record, indent=2) + "\n"
    retrieved = datetime.strptime(record.pop("retrieved_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - retrieved) < timedelta(minutes=1)
    assert record == {
        "source_id": "example",
        "url": ledger.load_manifest()[0]["url"],
        "sha256": hashlib.sha256(FIXTURE.encode()).hexdigest(),
        "bytes": len(FIXTURE),
        "vintage": "not stated",
        "last_modified": "",
        "etag": "",
        "final_url": "",
        "env_headers": [],
        "archive_url": "",
    }
    assert (calls[0].get_header("User-agent"), calls[0].get_header("Accept-encoding")) == ("my-dataset", "identity")


@pytest.mark.usefixtures("upstream")
def test_the_fetch_build_and_init_lines_are_exact(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What each stage prints is part of the command line's contract."""
    assert _run("fetch", "build") == [0, 0]
    sha = hashlib.sha256(FIXTURE.encode()).hexdigest()
    url = ledger.load_manifest()[0]["url"]
    assert capsys.readouterr().out.splitlines() == [
        f"example  new               29 B  {sha[:12]}  not stated  {url}",
        "2 rows -> data/processed/example.csv",
        "package -> data/processed/datapackage.json",
    ]
    (tmp_path / "fresh").mkdir()
    monkeypatch.chdir(tmp_path / "fresh")
    assert main(["init"]) == 0
    assert capsys.readouterr().out == "wrote sources.toml\n"


def test_an_unknown_stage_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Not a silent validate: argparse names the program and the choices."""
    with pytest.raises(SystemExit) as exit_:
        main(["publish"])
    assert exit_.value.code == 2
    assert capsys.readouterr().err.startswith("usage: sourceledger ")


def test_fetch_error_reads_as_its_failures() -> None:
    """A library caller that prints the error sees one line per failed source."""
    assert str(ledger.FetchError([], ["a: down", "b: gone"])) == "a: down\nb: gone"


def test_the_package_is_written_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """datapackage.json byte for byte: the keys, the field types, the name, non-ASCII text kept as it is."""
    upstream = tmp_path / "upstream" / "example.csv"
    upstream.parent.mkdir()
    upstream.write_text(FIXTURE, encoding="utf-8")
    project = tmp_path / "My Data Set!"
    project.mkdir()
    _manifest(project, upstream.as_uri(), terms=f'{TERMS}\nname = "Données"')
    monkeypatch.chdir(project)
    assert _run("fetch", "build") == [0, 0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    data = OUT.read_bytes()
    fields = ["source_id", "source_url", "retrieved_at", "vintage", "raw_sha256", "year", "value"]
    resource = {
        "name": "example",
        "type": "table",
        "path": "example.csv",
        "format": "csv",
        "mediatype": "text/csv",
        "encoding": "utf-8",
        "bytes": len(data),
        "hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "schema": {"fields": [{"name": field, "type": "string"} for field in fields]},
        "sources": [{"title": "Données", "path": upstream.as_uri()}],
        "licenses": [{"name": "CC0-1.0", "path": "https://example.org/terms", "title": "CC0-1.0"}],
        "sourceledger": {
            "retrieved_at": record["retrieved_at"],
            "vintage": "not stated",
            "raw_sha256": record["sha256"],
            "final_url": "",
            "redistribute": True,
        },
    }
    package = {"$schema": ledger._DATAPACKAGE_PROFILE, "name": "my-data-set", "resources": [resource]}  # noqa: SLF001
    expected = json.dumps(package, indent=2, ensure_ascii=False) + "\n"
    assert Path("data/processed/datapackage.json").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(("folder", "name"), [("Data.Set_2", "data.set_2"), ("--x--", "x"), ("!!!", "dataset")])
def test_the_package_name_comes_from_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str, name: str
) -> None:
    """Lower case, runs of other characters as one dash, and a fallback when nothing is left."""
    (tmp_path / folder).mkdir()
    monkeypatch.chdir(tmp_path / folder)
    assert ledger._package_name() == name  # noqa: SLF001


def test_build_only_a_later_source_builds_it(upstream: Path, tmp_path: Path) -> None:
    """Skipping an unselected source moves on to the next one."""
    other = upstream.with_name("b.csv")
    other.write_text(FIXTURE, encoding="utf-8")
    _two_sources(tmp_path, upstream.as_uri(), other.as_uri())
    assert main(["fetch"]) == 0
    assert main(["build", "--only", "b"]) == 0
    assert Path("data/processed/b.csv").exists()


@pytest.mark.usefixtures("upstream")
def test_the_stages_take_their_folders_as_arguments(tmp_path: Path) -> None:
    """A library caller can keep raw files elsewhere; build and validate then read them there."""
    sources = ledger.load_manifest()
    raw, processed, log = tmp_path / "elsewhere", tmp_path / "out", tmp_path / "logs" / "deep" / "log.jsonl"
    ledger.fetch(sources, raw=raw, log=log)
    ledger.fetch(sources, raw=raw, log=log)  # the second asks conditionally, from the record in `raw`
    ledger.build(sources, raw=raw, processed=processed)
    assert ledger.validate(sources, raw=raw, processed=processed) == []
    assert not RAW.exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.usefixtures("upstream")
def test_a_cut_off_line_after_several_records_drops_only_itself(caplog: pytest.LogCaptureFixture) -> None:
    """The log is truncated at its last newline, so every complete record before it stays."""
    LOG.parent.mkdir(parents=True)
    old = [_record(s, "0") for s in ("x", "y")]
    LOG.write_text("\n".join(old) + '\n{"source_id": "exa', encoding="utf-8")
    assert _run("fetch") == [0]
    lines = LOG.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == old
    assert len(lines) == 3
    assert "dropping a last line" in caplog.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{_record('a', '1')}\n\n{_record('b', '2')}\n", {"a": "1" * 64, "b": "2" * 64}),
        (f'{_record("a", "1")}\n{{"source_id": "a"', {"a": "1" * 64}),
        (f"not json\n{_record('a', '1')}", "log.jsonl:1 is not a fetch record"),
        (f"{_record('a', '1')}\nnot json\n", "log.jsonl:2 is not a fetch record"),
    ],
)
def test_the_log_reader_skips_blank_lines_and_forgives_only_a_cut_off_end(
    tmp_path: Path, text: str, expected: dict[str, str] | str
) -> None:
    """A blank line is skipped, a cut-off last line is dropped, and junk anywhere else is an error."""
    log = tmp_path / "log.jsonl"
    log.write_text(text, encoding="utf-8")
    if isinstance(expected, dict):
        assert ledger.logged_versions(log) == expected
    else:
        with pytest.raises(ValueError, match=expected):
            ledger.logged_versions(log)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 0.0),
        (_headers(), 0.0),
        (_headers(ETag="x"), 0.0),
        (_headers(Retry_After="junk"), 0.0),
        (_headers(Retry_After="Wed, 21 Oct 2015 07:28:00 GMT"), 0.0),
        (_headers(Retry_After="7"), 7.0),
    ],
)
def test_retry_after_reads_seconds_dates_and_nothing(headers: Message | None, expected: float) -> None:
    """No header, junk and a date in the past all mean no wait of the server's own."""
    assert ledger._retry_after(headers) == expected  # noqa: SLF001


def test_content_length_is_none_when_absent() -> None:
    assert ledger._content_length(_headers(ETag="x")) is None  # noqa: SLF001
    assert ledger._content_length(_headers(Content_Length=" 12 ")) == 12  # noqa: SLF001


@pytest.mark.usefixtures("upstream")
def test_a_failed_conditional_request_is_not_a_304(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a 304 keeps the earlier record; a 404 to a conditional request fails the fetch."""
    _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc"'), _http_error(404))
    assert _run("fetch", "fetch") == [0, 1]


@pytest.mark.usefixtures("upstream")
def test_a_retry_after_past_the_budget_fails_with_the_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the wait would outlast the fetch, the run stops with the server's own error."""
    monkeypatch.setattr(ledger, "_BUDGET_S", 60)
    _serve(monkeypatch, _http_error(503, Retry_After="90"))
    assert _run("fetch") == [1]
    assert "HTTP Error 503" in capsys.readouterr().err


def test_the_opener_gets_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr(ledger._OPENER, "open", lambda _request, timeout: seen.append(timeout))  # noqa: SLF001
    ledger._open(urllib.request.Request("https://example.invalid/"), timeout=7)  # noqa: SLF001
    assert seen == [7]


def test_an_unresolvable_host_counts_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redirect check leaves it to the request, which fails on its own."""

    def fail(*_args: object) -> None:
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert ledger._is_public("no-such-host.invalid")  # noqa: SLF001


def test_a_gzip_exactly_at_max_bytes_is_read() -> None:
    """The cap is the most a file may expand to, not one byte less."""
    body = FIXTURE.encode()
    columns, _ = ledger.parse_csv({"id": "g", "max_bytes": len(body)}, gzip.compress(body))
    assert columns == ["year", "value"]
    _, rows = ledger.parse_json({"id": "g"}, gzip.compress(b'[{"a": 1}]'))
    assert rows == [{"a": "1"}]


def test_a_csv_field_keeps_its_line_break() -> None:
    """A quoted field with CR LF inside is data, not a line ending to translate."""
    _, rows = ledger.parse_csv({"id": "c"}, b'a,b\r\n"one\r\ntwo",2\r\n')
    assert rows == [{"a": "one\r\ntwo", "b": "2"}]


def test_a_quoted_line_break_survives_read_output(tmp_path: Path) -> None:
    out = tmp_path / "o.csv"
    out.write_bytes(b'a\r\n"one\r\ntwo"\r\n')
    assert ledger.read_output(out) == [{"a": "one\r\ntwo"}]


@pytest.mark.parametrize("payload", [b"[1]", b"{}", b'"x"'])
def test_json_that_is_not_an_array_of_objects_is_refused(payload: bytes) -> None:
    with pytest.raises(ValueError, match=r"^j: expected a JSON array of objects$"):
        ledger.parse_json({"id": "j"}, payload)


def test_cells_keep_false_dates_times_and_non_ascii_text() -> None:
    naive = datetime(2020, 1, 2, 3, 4)  # noqa: DTZ001  # what openpyxl returns for a date cell
    assert [
        ledger._cell(value)  # noqa: SLF001
        for value in (False, True, 3, 1.5, naive, date(2020, 1, 2), dt_time(1, 2), ["é"])
    ] == ["false", "true", "3", "1.5", "2020-01-02T03:04:00", "2020-01-02", '"01:02:00"', '["é"]']


def test_the_reader_needs_no_url_when_the_format_is_given() -> None:
    assert ledger.parse_source({"id": "j", "format": "json"}, b"[]") == ([], [])


def test_an_excel_workbook_reads_its_first_sheet_and_cached_values(tmp_path: Path) -> None:
    """Without `sheet` the first sheet is read; a formula is its saved value, never its text."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.append(("year", "total"))
    book.active.append((2020, "=1+1"))
    book.create_sheet("other").append(("x",))
    path = tmp_path / "b.xlsx"
    book.save(path)
    # openpyxl saves no computed values, so the formula cell is empty when read as data.
    assert ledger.parse_xlsx({"id": "x"}, path.read_bytes()) == (["year", "total"], [{"year": "2020", "total": ""}])


def test_excel_errors_name_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.append(("a", "a"))
    path = tmp_path / "b.xlsx"
    book.save(path)
    with pytest.raises(ValueError, match=r"^x: columns \['a'\] appear more than once in the header$"):
        ledger.parse_xlsx({"id": "x"}, path.read_bytes())
    with pytest.raises(ValueError, match=r"^x: no sheet 'nope'; the workbook has \['Sheet'\]$"):
        ledger.parse_xlsx({"id": "x", "sheet": "nope"}, path.read_bytes())
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(ValueError, match=r"^x: reading .xlsx needs the xlsx extra: uv add 'sourceledger\[xlsx\]'$"):
        ledger.parse_xlsx({"id": "x"}, path.read_bytes())


def _xlsx(tmp_path: Path, *rows: tuple[object, ...]) -> bytes:
    """A workbook whose first sheet holds `rows`."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    path = tmp_path / "b.xlsx"
    book.save(path)
    return path.read_bytes()


def test_an_excel_workbook_that_expands_past_max_bytes_is_refused(tmp_path: Path) -> None:
    """Like a gzip, a small workbook can unzip without limit; the cap applies to what it expands to."""
    payload = _xlsx(tmp_path, ("year", "value"), *((2020, 1.5),) * 100)
    with pytest.raises(
        ValueError, match=r"^x: decompresses past 1000 bytes; raise max_bytes in sources\.toml if expected$"
    ):
        ledger.parse_xlsx({"id": "x", "max_bytes": 1000}, payload)
    assert len(ledger.parse_xlsx({"id": "x", "max_bytes": 10 * len(payload)}, payload)[1]) == 100


def test_a_cell_right_of_the_excel_header_is_refused(tmp_path: Path) -> None:
    """As a CSV row with more fields than its header; an empty cell reads as "", like an empty CSV field."""
    trailing = _xlsx(tmp_path, ("a", "b", None), (1,), (1, 2))
    assert ledger.parse_xlsx({"id": "x"}, trailing) == (["a", "b"], [{"a": "1", "b": ""}, {"a": "1", "b": "2"}])
    stray = _xlsx(tmp_path, ("a", "b"), (1, 2), (None, None), (1, 2, None, "x", "y"))
    with pytest.raises(ValueError, match=r"^x: row 4 has more cells than the header$"):
        ledger.parse_xlsx({"id": "x"}, stray)


@pytest.mark.parametrize("payload", [b"not a workbook", b"PK\x05\x06" + bytes(18)])
def test_a_file_that_is_not_a_workbook_is_refused_by_name(payload: bytes) -> None:
    """A damaged download or an empty zip is a one-line error naming the source, not a zipfile traceback."""
    pytest.importorskip("openpyxl")
    with pytest.raises(ValueError, match=r"^x: not a readable Excel workbook \("):
        ledger.parse_xlsx({"id": "x"}, payload)


def test_a_cut_off_gzip_fails_build_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated download that still starts like a gzip is a source error, not a traceback."""
    upstream = tmp_path / "upstream" / "example.csv.gz"
    upstream.parent.mkdir()
    upstream.write_bytes(gzip.compress(FIXTURE.encode() * 50)[:40])
    _manifest(tmp_path, upstream.as_uri())
    monkeypatch.chdir(tmp_path)
    assert _run("fetch") == [0]
    capsys.readouterr()
    assert _run("build") == [1]
    err = capsys.readouterr().err
    assert err.startswith("build: example: not a readable gzip file (")
    assert err.count("\n") == 1


@pytest.mark.parametrize(
    "payload", [gzip.compress(b"[]")[:-8] + b"\0" * 8, b"\x1f\x8b\x08\x00" + bytes(6) + b"\x07junk"]
)
def test_a_damaged_gzip_is_refused_by_name(payload: bytes) -> None:
    """A bad checksum or a corrupt stream names the source too."""
    with pytest.raises(ValueError, match=r"^j: not a readable gzip file \("):
        ledger.parse_json({"id": "j"}, payload)


def test_json_nested_too_deeply_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match=r"^j: JSON nested too deeply to read$"):
        ledger.parse_json({"id": "j"}, b"[" * 100_000 + b"]" * 100_000)


def test_json_numbers_are_kept_as_written() -> None:
    """1.10 is not 1.1, and 1e5 is not 100000.0, at the top level or nested."""
    payload = b'[{"a": 1.10, "b": 1e5, "c": -0, "d": {"e": [2.50, 3E2, true, "x"]}}]'
    _, rows = ledger.parse_json({"id": "j"}, payload)
    assert rows == [{"a": "1.10", "b": "1e5", "c": "-0", "d": '{"e": [2.50, 3E2, true, "x"]}'}]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ('format = "parquet"', "example: format in sources.toml must be one of ['csv', 'json', 'xlsx'], not 'parquet'"),
        ('headers = "x"', "example: headers in sources.toml must be a table"),
        (
            'headers = { Authorization = "Bearer x" }',
            (
                'example: header Authorization must name an environment variable, as "env:NAME"; '
                "never write a secret into sources.toml"
            ),
        ),
    ],
)
def test_manifest_errors_are_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, expected: str) -> None:
    _manifest(tmp_path, "https://example.org/a.csv", terms=f"{TERMS}\n{extra}")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        ledger.load_manifest()


def test_a_source_without_a_string_id_is_named_exactly(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.toml"
    manifest.write_text("[[source]]\nid = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^sources.toml: every \[\[source\]\] needs a string id$"):
        ledger.load_manifest(manifest)


def test_max_bytes_of_one_is_allowed(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.toml"
    manifest.write_text('[[source]]\nid = "a"\nstatus = "unimplemented"\nmax_bytes = 1\n', encoding="utf-8")
    assert ledger.load_manifest(manifest)[0]["max_bytes"] == 1


@pytest.mark.usefixtures("upstream")
def test_a_new_vintage_error_is_exact(tmp_path: Path) -> None:
    _run("fetch")
    _manifest(tmp_path, ledger.load_manifest()[0]["url"], terms=f'{TERMS}\nvintage = "2025"')
    with pytest.raises(
        ValueError, match=r"^example\.csv was fetched as vintage 'not stated', not '2025'; re-run fetch$"
    ):
        ledger.verified(ledger.load_manifest()[0])


def test_status_of_no_sources_is_no_lines(tmp_path: Path) -> None:
    assert ledger.status([], log=tmp_path / "none.jsonl") == []


def test_terms_need_both_the_words_and_the_url() -> None:
    source = {"id": "a", "terms": "CC0", "license": "CC0-1.0", "redistribute": True}
    assert ledger._terms_problems(source) == ["a: no terms or terms_url; record the publisher's reuse terms"]  # noqa: SLF001


def test_validate_checks_sources_after_an_unimplemented_or_unknown_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source needing no checks does not end the checks of the ones after it."""
    sources = [
        {"id": "later", "status": "unimplemented"},
        {"id": "odd", "status": "maybe"},
        {"id": "x", "status": "implemented", "url": "https://example.org/x.csv"},
    ]
    monkeypatch.chdir(tmp_path)
    problems = ledger.validate(sources)
    assert any(p.startswith("x: no terms") for p in problems)


def test_attribution_leaves_missing_terms_empty() -> None:
    source = {"id": "a", "status": "implemented", "url": "https://example.org/a.csv"}
    assert ledger.attribution([source])[2] == "| a |  |  |  | ? |"


def test_verify_shows_a_large_listed_size_with_separators(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("x\n", encoding="utf-8")
    package = {"resources": [{"path": "a.csv", "bytes": 1234, "hash": _X_HASH}]}
    (tmp_path / "datapackage.json").write_text(json.dumps(package), encoding="utf-8")
    assert ledger.verify(tmp_path) == [f"{tmp_path}/a.csv is 2 bytes, not the 1,234 the package lists"]
    (tmp_path / "datapackage.json").write_text("[]", encoding="utf-8")
    assert ledger.verify(tmp_path) == [f"{tmp_path}/datapackage.json has no list of resources"]


def test_verify_names_the_hash_it_found(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("y\n", encoding="utf-8")
    package = {"resources": [{"path": "a.csv", "hash": _X_HASH}]}
    (tmp_path / "datapackage.json").write_text(json.dumps(package), encoding="utf-8")
    digest = hashlib.sha256(b"y\n").hexdigest()
    assert ledger.verify(tmp_path) == [
        f"{tmp_path}/a.csv hashes to sha256:{digest[:12]}, not the hash the package lists"
    ]


class _Spy(_Response):
    """A response that records how much each read asks for."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.asks: list[int | None] = []

    def read1(self, size: int | None = -1, /) -> bytes:
        self.asks.append(size)
        return super().read1(size)


@pytest.mark.usefixtures("upstream")
def test_a_download_reads_one_chunk_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory stays at one chunk: no read asks for the whole body."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", 4)
    response = _Spy(FIXTURE.encode())
    _serve(monkeypatch, response)
    assert _run("fetch") == [0]
    assert set(response.asks) == {4}


def test_a_gzip_is_read_no_further_than_one_byte_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decompression bomb is stopped after max_bytes + 1 bytes, not read to its end first."""
    asks: list[int] = []
    read = gzip.GzipFile.read

    def spy(self: gzip.GzipFile, size: int = -1) -> bytes:
        asks.append(size)
        return read(self, size)

    monkeypatch.setattr(gzip.GzipFile, "read", spy)
    ledger.parse_csv({"id": "g", "max_bytes": 100}, gzip.compress(FIXTURE.encode()))
    assert asks == [101]


@pytest.mark.usefixtures("upstream")
def test_temp_files_go_beside_their_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never in the system temp folder: a rename from another disk is not atomic, or fails."""
    monkeypatch.setattr(tempfile, "tempdir", "/nonexistent-temp-folder")
    assert _run("fetch", "build", "validate") == [0, 0, 0]


def test_a_failed_write_raises_its_own_error_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="No space left"):
        ledger._write_atomic(tmp_path / "out.csv", b"x")  # noqa: SLF001
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("upstream")
def test_an_empty_content_encoding_is_no_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _Response(FIXTURE.encode(), Content_Encoding=""))
    assert _run("fetch") == [0]


def test_archive_asks_for_http_urls_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://example.org/data.csv"
    _manifest(tmp_path, url, terms=f"{TERMS}\narchive = true")
    monkeypatch.chdir(tmp_path)
    snapshot = f"https://web.archive.org/web/20261018033833/{url}"
    _serve(monkeypatch, _Response(FIXTURE.encode()), _Response(b"", url=snapshot))
    assert _run("fetch") == [0]
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["archive_url"] == snapshot


def test_a_log_line_from_before_archive_url_reads_as_no_snapshot(upstream: Path, tmp_path: Path) -> None:
    """The same bytes again take the snapshot from their log line, and an old line without one gives none."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\narchive = true")
    assert _run("fetch") == [0]
    line = json.loads(LOG.read_text(encoding="utf-8"))
    del line["archive_url"]
    LOG.write_text(json.dumps(line) + "\n", encoding="utf-8")
    RAW.write_text("edited\n", encoding="utf-8")  # forces a full fetch of the same bytes
    assert _run("fetch") == [0]
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["archive_url"] == ""


def test_revisions_raises_a_standard_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as error:
        ledger.revisions(tmp_path / "missing.jsonl")
    assert (error.value.errno, error.value.strerror) == (errno.ENOENT, os.strerror(errno.ENOENT))


@pytest.mark.usefixtures("upstream")
def test_warnings_name_the_program(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A retry warning on stderr starts with the program's name, as the CLI's other messages do."""
    monkeypatch.setattr(logging.root, "handlers", [])
    _serve(monkeypatch, _http_error(503), _Response(FIXTURE.encode()))
    try:
        assert main(["fetch"], prog="my-dataset") == 0
    finally:
        for handler in logging.root.handlers:
            handler.close()
    assert capsys.readouterr().err.startswith("my-dataset: example: attempt 1 of 4 failed")


_DATA = "https://example.org/data.csv"
_SNAPSHOT = f"https://web.archive.org/web/20261018033833/{_DATA}"


@pytest.mark.usefixtures("upstream")
def test_archive_asks_for_a_snapshot_of_new_bytes_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot's URL lands in the record and the log; the same bytes keep it, and new bytes get their own."""
    _manifest(tmp_path, _DATA, terms=f"{TERMS}\narchive = true")
    newer = f"https://web.archive.org/web/20261019033833/{_DATA}"
    calls = _serve(
        monkeypatch,
        _Response(FIXTURE.encode(), ETag='"v1"'),
        _Response(b"", url=_SNAPSHOT),
        _http_error(304),
        _Response(FIXTURE.encode()),
        _Response((FIXTURE + "2022,3.5\n").encode()),
        _Response(b"", url=newer),
    )
    assert _run("fetch") == [0]
    assert (calls[1].full_url, calls[1].timeout) == (f"https://web.archive.org/save/{_DATA}", ledger._ARCHIVE_TIMEOUT_S)  # noqa: SLF001
    assert calls[1].get_header("User-agent") == ledger.USER_AGENT
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["archive_url"] == _SNAPSHOT
    assert json.loads(LOG.read_text(encoding="utf-8"))["archive_url"] == _SNAPSHOT
    assert _run("fetch") == [0]  # a 304
    RAW.write_text("edited\n", encoding="utf-8")
    assert _run("fetch") == [0]  # the same bytes again, downloaded in full
    assert len(calls) == 4
    assert json.loads(LOG.read_text(encoding="utf-8"))["archive_url"] == _SNAPSHOT
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["archive_url"] == _SNAPSHOT
    assert _run("fetch") == [0]  # new bytes
    assert len(calls) == 6
    assert [json.loads(line)["archive_url"] for line in LOG.read_text(encoding="utf-8").splitlines()] == [
        _SNAPSHOT,
        newer,
    ]


@pytest.mark.parametrize(
    ("answer", "snapshot"),
    [
        # Factories, not responses: a response is closed after one run, and pytest may run a test twice.
        (
            lambda: _Response(
                b"", url=f"https://web.archive.org/save/{_DATA}", Content_Location=f"/web/20261018033833/{_DATA}"
            ),
            _SNAPSHOT,
        ),
        (lambda: _Response(b"", url=f"https://web.archive.org/save/{_DATA}"), ""),
        (lambda: _http_error(503), ""),
        (lambda: TimeoutError("timed out"), ""),
        (lambda: urllib.error.URLError("x"), ""),
        (lambda: ValueError("bad"), ""),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_archive_never_fails_the_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[], _Response | Exception],
    snapshot: str,
) -> None:
    """A snapshot named in Content-Location is kept; no snapshot, an error or a timeout is a warning."""
    _manifest(tmp_path, _DATA, terms=f"{TERMS}\narchive = true")
    _serve(monkeypatch, _Response(FIXTURE.encode()), answer())
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["archive_url"] == snapshot
    assert ("Internet Archive" in caplog.text) == (not snapshot)


def test_archive_skips_a_url_that_is_not_http(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Save Page Now can only reach the web, so a file:// source is fetched and not archived."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\narchive = true")
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert len(calls) == 1
    assert "archive needs an http or https URL" in caplog.text
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["archive_url"] == ""


def test_archive_must_be_true_or_false(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A string such as "yes" is a manifest error, not a silent yes."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\narchive = "yes"')
    assert _run("fetch") == [1]
    assert "need archive to be true or false" in capsys.readouterr().err


_SNAPSHOT_FORM = r"https://web\.archive\.org/web/\d{14}/(?P<url>.+)"


class _SavePageNow:
    """A fake of Save Page Now, standing in for `ledger._open`, as far as `_archive` relies on it.

    It redirects to a snapshot, or answers 429 with no Retry-After, which the live archive sends
    once a URL has had its 5 captures of the day. `test_save_page_now_contract` checks it against
    the live service.
    """

    def __init__(self, *, rate_limited: bool = False) -> None:
        self.rate_limited = rate_limited

    def __call__(self, request: urllib.request.Request, timeout: float) -> _Response:
        assert request.full_url.startswith(ledger._ARCHIVE)  # noqa: SLF001
        assert timeout == ledger._ARCHIVE_TIMEOUT_S  # noqa: SLF001
        if self.rate_limited:
            raise urllib.error.HTTPError(request.full_url, 429, "TOO MANY REQUESTS", _headers(), None)
        target = request.full_url.removeprefix(ledger._ARCHIVE)  # noqa: SLF001
        return _Response(b"", url=f"{ledger._SNAPSHOT}20261019161808/{target}")  # noqa: SLF001


@pytest.fixture(params=["fake", "fake-rate-limited", pytest.param("live", marks=pytest.mark.live)])
def save_page_now(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Save Page Now: the fake on every run, the live service under `pytest -m live`."""
    if request.param != "live":
        monkeypatch.setattr(ledger, "_open", _SavePageNow(rate_limited=request.param == "fake-rate-limited"))
    return request.param


def test_save_page_now_contract(save_page_now: str, caplog: pytest.LogCaptureFixture) -> None:
    """The archive either redirects to a snapshot of the URL that opens, or rate-limits with a 429."""
    # A page few others save: example.com reaches the archive's 5 captures a day by morning.
    target = "https://github.com/simonvanlierde/sourceledger/blob/main/README.md"
    snapshot = ledger._archive({"id": "contract", "url": target}, ledger.USER_AGENT)  # noqa: SLF001
    if not snapshot:
        assert "HTTP Error 429" in caplog.text
        if save_page_now == "live":
            pytest.skip("the live archive had captured this URL 5 times today, so only the 429 was checked")
        return
    match = re.fullmatch(_SNAPSHOT_FORM, snapshot)
    assert match
    assert match["url"].rstrip("/") == target.rstrip("/")
    if save_page_now == "live":
        request = urllib.request.Request(snapshot, headers={"User-Agent": ledger.USER_AGENT})  # noqa: S310
        with ledger._open(request, timeout=60) as response:  # noqa: SLF001
            assert response.status == 200


@pytest.mark.usefixtures("upstream")
def test_a_manifest_outside_the_project_is_read_from_the_option_or_the_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package that ships sources.toml passes its path; the data still lands in the working directory."""
    packaged = tmp_path / "package" / "sources.toml"
    packaged.parent.mkdir()
    Path("sources.toml").rename(packaged)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert main(["fetch", "--manifest", str(packaged)]) == 0
    assert main(["build", "--manifest", str(packaged)]) == 0
    assert main(["validate", "--manifest", str(packaged)]) == 0
    assert [row["value"] for row in ledger.read_output(project / OUT)] == ["1.5", "2.5"]
    assert main(["validate"], manifest=packaged) == 0
    assert main(["validate"]) == 1  # no ./sources.toml here
    assert not packaged.with_name("data").exists()
