"""The whole pipeline against a fixture served from a local file: no network."""

import hashlib
import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

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
    """What urlopen returns: a body, and headers whose Content-Length is `length` (default: the true length)."""

    def __init__(self, body: bytes, length: int | None = None, **headers: str) -> None:
        super().__init__(body)
        self.headers = _headers(Content_Length=str(len(body) if length is None else length), **headers)


def _serve(monkeypatch: pytest.MonkeyPatch, *outcomes: _Response | Exception) -> list[urllib.request.Request]:
    """Answer each urlopen call with the next outcome, raising it if it is an exception. Returns the requests made."""
    calls: list[urllib.request.Request] = []
    queue = list(outcomes)

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:  # noqa: ARG001
        calls.append(request)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
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
    assert "IncompleteRead" in capsys.readouterr().err
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
    assert "max_bytes" in capsys.readouterr().err


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
