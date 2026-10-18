"""The whole pipeline against a fixture served from a local file: no network."""

import gzip
import hashlib
import http.client
import http.server
import io
import json
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
    """What the opener returns: a body, and headers whose Content-Length is `length` (default: the true length)."""

    def __init__(self, body: bytes, length: int | None = None, **headers: str) -> None:
        super().__init__(body)
        self.headers = _headers(Content_Length=str(len(body) if length is None else length), **headers)
        self.url = ""  # set to the request's URL when served


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


def test_a_toml_date_release_is_recorded_as_a_string(upstream: Path, tmp_path: Path) -> None:
    """An unquoted `release = 2024-06-01` loads as a date; the fetch record still holds text."""
    _manifest(tmp_path, upstream.as_uri(), terms="release = 2024-06-01")
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
    del record["last_modified"], record["etag"]
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
    assert "1 problems" in out
    assert "data/processed/example.csv is missing" in err


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


@pytest.mark.usefixtures("upstream")
def test_a_bad_line_inside_the_log_is_an_error_that_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    """Only a cut-off last line is forgiven: damage earlier in the history is reported."""
    LOG.parent.mkdir(parents=True)
    LOG.write_text('not json\n{"source_id": "x", "sha256": "0"}\n', encoding="utf-8")
    assert _run("fetch") == [1]
    assert "fetches.jsonl:1 is not a fetch record" in capsys.readouterr().err


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


def test_release_is_read_as_vintage_with_a_warning(
    upstream: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Manifests written before the rename keep working."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nrelease = "2024"')
    assert _run("fetch") == [0]
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["vintage"] == "2024"
    assert "is now `vintage`" in caplog.text
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nrelease = "2024"\nvintage = "2024"')
    assert _run("fetch") == [1]


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


def test_a_redirect_from_a_public_host_to_an_internal_one_is_refused() -> None:
    """A third-party server can't make the fetch save an internal service's response."""
    handler = ledger._NoRedirectInward()  # noqa: SLF001
    public = urllib.request.Request("http://93.184.215.14/data.csv")
    with pytest.raises(urllib.error.HTTPError, match="refused a redirect"):
        handler.redirect_request(
            public, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://127.0.0.1/secret"
        )
    internal = urllib.request.Request("http://127.0.0.1/data.csv")
    assert handler.redirect_request(
        internal, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://127.0.0.1/moved"
    )


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
        '[[source]]\nid = "b"\nstatus = "unimplemented"\nreason = "later"\n',
        _GOOD.replace('id = "a"', 'id = "a/b"'),
        _GOOD.replace('status = "implemented"', 'status = "done"'),
        _GOOD.replace('license = "CC0-1.0"\n', ""),
        _GOOD.replace("redistribute = true", 'redistribute = "yes"'),
        _GOOD + 'sha256 = "abc"\n',
        _GOOD + "max_bytes = 0\n",
        _GOOD + 'format = "parquet"\n',
        _GOOD + 'headers = { Authorization = "Bearer x" }\n',
        _GOOD.replace('url = "https://example.org/a.csv"\n', ""),
    ],
)
def test_the_schema_and_the_loader_agree(tmp_path: Path, text: str) -> None:
    """An editor using the published schema accepts exactly the manifests sourceledger accepts."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(ledger.__file__).parent / "sources.schema.json").read_text(encoding="utf-8"))
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    data = tomllib.loads(text)
    schema_ok = jsonschema.Draft202012Validator(schema).is_valid(data)
    try:
        sources = ledger.load_manifest(path)
    except ValueError:
        loader_ok = False
    else:
        bad_status = [s for s in sources if s["status"] not in (ledger.IMPLEMENTED, ledger.UNIMPLEMENTED)]
        terms = [p for s in ledger.implemented(sources) for p in ledger._terms_problems(s)]  # noqa: SLF001
        loader_ok = not bad_status and not terms
    assert schema_ok == loader_ok
