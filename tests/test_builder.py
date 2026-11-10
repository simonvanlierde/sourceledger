"""Parsing raw files and writing outputs and datapackage.json."""

import csv
import dataclasses
import gzip
import hashlib
import io
import json
import shutil
import sys
import zipfile
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from support import (
    FIXTURE,
    LOG,
    OUT,
    RAW,
    TERMS,
    WHEN,
    _lines,
    _manifest,
    _peak,
    _record,
    _Response,
    _run,
    _serve,
    _source,
    _two_sources,
    builder_mod,
    fetcher_mod,
    report_mod,
)

from sourceledger import ledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main


def test_the_whole_pipeline_runs_from_a_local_file(upstream: Path) -> None:
    """Fetch, build and validate succeed, and every output row names the bytes it came from."""
    assert _run("fetch", "build", "validate") == [0, 0, 0]

    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["url"] == upstream.as_uri()
    assert record["sha256"] == hashlib.sha256(FIXTURE.encode()).hexdigest()
    assert record["retrieved_at"].endswith("Z")
    assert record["vintage"] == "not stated"

    rows = list(builder_mod.read_output(OUT))
    assert [(r["year"], r["value"]) for r in rows] == [("2020", "1.5"), ("2021", "2.5")]
    assert {(r["source_url"], r["raw_sha256"]) for r in rows} == {(record["url"], record["sha256"])}


@pytest.mark.usefixtures("upstream")
def test_build_reads_a_fetch_record_from_before_the_http_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record without last_modified and etag, as older fetches wrote, still builds."""
    _serve(monkeypatch, _Response(FIXTURE.encode()))
    _run("fetch")
    path = Path("data/raw/example.csv.fetch.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["last_modified"], record["etag"], record["archive_url"]
    path.write_text(json.dumps(record), encoding="utf-8", newline="")
    assert _run("build") == [0]


def test_build_refuses_a_fetch_made_for_a_different_request(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Changing a source's URL without re-fetching must not pass the old bytes off as the new request."""
    _run("fetch")
    moved = upstream.with_name("moved.csv")
    moved.write_text(FIXTURE, encoding="utf-8", newline="")
    _manifest(tmp_path, moved.as_uri())
    assert _run("build") == [3]
    assert "re-run fetch" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_build_refuses_a_raw_file_changed_after_fetch(capsys: pytest.CaptureFixture[str]) -> None:
    """Checksums are verified before anything is parsed."""
    _run("fetch")
    RAW.write_text(FIXTURE + "2022,9.9\n", encoding="utf-8", newline="")
    assert _run("build") == [3]
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
    upstream.write_text(text, encoding="utf-8", newline="")
    _run("fetch")
    assert _run("build") == [3]
    err = capsys.readouterr().err
    assert expected in err
    assert len(err.strip().splitlines()) == 1
    assert not OUT.exists()


def test_build_keeps_the_header_of_a_source_with_no_rows(upstream: Path) -> None:
    """Columns come from the header, so a header-only source still publishes them."""
    upstream.write_text("year,value\n", encoding="utf-8", newline="")
    assert _run("fetch", "build") == [0, 0]
    assert OUT.read_text(encoding="utf-8").splitlines() == [
        "source_id,source_url,retrieved_at,vintage,raw_sha256,year,value"
    ]


def test_build_refuses_a_repeated_column_name(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two columns with one name would publish the second's values under both."""
    upstream.write_text("a,a\n1,2\n", encoding="utf-8", newline="")
    assert _run("fetch", "build") == [0, 3]
    assert "appear more than once in the header" in capsys.readouterr().err
    assert not OUT.exists()


def test_build_drops_a_byte_order_mark(upstream: Path) -> None:
    """A spreadsheet export's BOM does not end up in the first column's name."""
    upstream.write_bytes(b"\xef\xbb\xbf" + FIXTURE.encode())
    assert _run("fetch", "build") == [0, 0]
    assert [r["year"] for r in builder_mod.read_output(OUT)] == ["2020", "2021"]


@pytest.mark.usefixtures("upstream")
def test_build_refuses_a_column_named_like_a_provenance_column(
    upstream: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source column must not be silently overwritten by the provenance stamp."""
    upstream.write_text("year,source_url\n2020,x\n", encoding="utf-8", newline="")
    assert _run("fetch", "build") == [0, 3]
    assert "collide with the provenance columns" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_a_project_passes_its_own_parser_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` builds with the project's parser and fetches under the project's name."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()), _Response(FIXTURE.encode()))

    def marked(source: manifest_mod.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        columns, rows = builder_mod.parse_csv(source, payload)
        return columns, [{**row, "year": f"y{row['year']}"} for row in rows]

    assert main(["fetch"], user_agent="my-dataset") == 0
    assert calls[0].get_header("User-agent") == "my-dataset"
    RAW.unlink()  # forces a full fetch through the library function
    fetcher_mod.fetch(manifest_mod.load_manifest(), user_agent="my-library-dataset")
    assert calls[1].get_header("User-agent") == "my-library-dataset"
    assert main(["build"], parse=marked) == 0
    assert [r["year"] for r in builder_mod.read_output(OUT)] == ["y2020", "y2021"]


def test_build_checks_the_pin_too(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A pin added after the fetch applies to build and validate, not only to the next fetch."""
    _run("fetch")
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsha256 = "{"0" * 64}"')
    capsys.readouterr()
    assert _run("build", "validate") == [3, 1]
    assert "not the sha256 sources.toml now pins" in capsys.readouterr().err


def test_a_new_vintage_needs_a_new_fetch(upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Records and output never keep a vintage the manifest no longer declares."""
    _run("fetch", "build")
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nvintage = "2025"')
    assert _run("validate") == [1]
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert calls[0].get_header("If-none-match") is None
    assert {r["vintage"] for r in builder_mod.read_output(OUT)} == {"2025"}


@pytest.mark.parametrize("change", ["vintage", "url"])
def test_a_new_request_for_the_same_bytes_is_logged(
    change: str, upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log's last line and the sidecar agree after a vintage or URL change that leaves the bytes as they were."""
    _run("fetch")
    url = upstream.as_uri() if change == "vintage" else "https://example.org/moved.csv"
    vintage = '\nvintage = "2025"' if change == "vintage" else ""
    _manifest(tmp_path, url, terms=TERMS + vintage)
    _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    sidecar = json.loads(RAW.with_name(f"{RAW.name}.fetch.json").read_text(encoding="utf-8"))
    lines = _lines()
    assert len(lines) == 2
    assert (lines[-1]["vintage"], lines[-1]["url"]) == (sidecar["vintage"], sidecar["url"])
    assert report_mod.status_records([_source()])[0]["sha256"] == sidecar["sha256"]


@pytest.mark.usefixtures("upstream")
def test_a_parser_cannot_overwrite_a_provenance_column() -> None:
    """The stamp is written last, so a parser's row keys never replace it."""

    def sneaky(source: manifest_mod.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        columns, rows = builder_mod.parse_csv(source, payload)
        return columns, [{**row, "vintage": "forged"} for row in rows]

    _run("fetch")
    assert main(["build"], parse=sneaky) == 0
    assert {r["vintage"] for r in builder_mod.read_output(OUT)} == {"not stated"}


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
    rows = list(builder_mod.read_output(OUT))
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
    assert _run("fetch", "build") == [0, 3]
    assert "decompresses past 2000 bytes" in capsys.readouterr().err


def test_json_values_become_text() -> None:
    """Missing keys are empty, booleans are lower case, nested values stay JSON."""
    payload = b'[{"a": true, "b": {"c": 1}}, {"a": null, "d": "x"}]'
    columns, rows = builder_mod.parse_json({"id": "j"}, payload)
    assert columns == ["a", "b", "d"]
    assert rows == [{"a": "true", "b": '{"c": 1}', "d": ""}, {"a": "", "b": "", "d": "x"}]
    with pytest.raises(ValueError, match="array of objects"):
        builder_mod.parse_json({"id": "j"}, b'{"a": 1}')


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
    assert [(r["year"], r["value"]) for r in builder_mod.read_output(OUT)] == [("2020", "1.5"), ("2021", "2.5")]


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


def test_the_package_is_written_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """datapackage.json byte for byte: the keys, the field types, the name, non-ASCII text kept as it is."""
    upstream = tmp_path / "upstream" / "example.csv"
    upstream.parent.mkdir()
    upstream.write_text(FIXTURE, encoding="utf-8", newline="")
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
    package = {"$schema": builder_mod._DATAPACKAGE_PROFILE, "name": "my-data-set", "resources": [resource]}  # noqa: SLF001
    expected = json.dumps(package, indent=2, ensure_ascii=False) + "\n"
    assert Path("data/processed/datapackage.json").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(("folder", "name"), [("Data.Set_2", "data.set_2"), ("--x--", "x"), ("!!!", "dataset")])
def test_the_package_name_comes_from_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str, name: str
) -> None:
    """Lower case, runs of other characters as one dash, and a fallback when nothing is left."""
    (tmp_path / folder).mkdir()
    monkeypatch.chdir(tmp_path / folder)
    assert builder_mod._package_name() == name  # noqa: SLF001


def test_build_only_a_later_source_builds_it(upstream: Path, tmp_path: Path) -> None:
    """Skipping an unselected source moves on to the next one."""
    other = upstream.with_name("b.csv")
    other.write_text(FIXTURE, encoding="utf-8", newline="")
    _two_sources(tmp_path, upstream.as_uri(), other.as_uri())
    assert main(["fetch"]) == 0
    assert main(["build", "--only", "b"]) == 0
    assert Path("data/processed/b.csv").exists()


def test_a_gzip_exactly_at_max_bytes_is_read() -> None:
    """The cap is the most a file may expand to, not one byte less."""
    body = FIXTURE.encode()
    columns, _ = builder_mod.parse_csv({"id": "g", "max_bytes": len(body)}, gzip.compress(body))
    assert columns == ["year", "value"]
    _, rows = builder_mod.parse_json({"id": "g"}, gzip.compress(b'[{"a": 1}]'))
    assert rows == [{"a": "1"}]


def test_a_csv_field_keeps_its_line_break() -> None:
    """A quoted field with CR LF inside is data, not a line ending to translate."""
    _, rows = builder_mod.parse_csv({"id": "c"}, b'a,b\r\n"one\r\ntwo",2\r\n')
    assert rows == [{"a": "one\r\ntwo", "b": "2"}]


def test_a_quoted_line_break_survives_read_output(tmp_path: Path) -> None:
    out = tmp_path / "o.csv"
    out.write_bytes(b'a\r\n"one\r\ntwo"\r\n')
    assert list(builder_mod.read_output(out)) == [{"a": "one\r\ntwo"}]


def test_read_output_streams_rows(tmp_path: Path) -> None:
    """A table of millions of rows is read one row at a time, not into one list."""
    out = tmp_path / "o.csv"
    out.write_bytes(b"a\r\n1\r\n2\r\n")
    rows = builder_mod.read_output(out)
    assert next(rows) == {"a": "1"}
    assert list(rows) == [{"a": "2"}]


@pytest.mark.parametrize("payload", [b"[1]", b"{}", b'"x"'])
def test_json_that_is_not_an_array_of_objects_is_refused(payload: bytes) -> None:
    with pytest.raises(ValueError, match=r"^j: expected a JSON array of objects$"):
        builder_mod.parse_json({"id": "j"}, payload)


def test_cells_keep_false_dates_times_and_non_ascii_text() -> None:
    naive = datetime(2020, 1, 2, 3, 4)  # noqa: DTZ001  # what openpyxl returns for a date cell
    assert [
        builder_mod._cell(value)  # noqa: SLF001
        for value in (False, True, 3, 1.5, naive, date(2020, 1, 2), dt_time(1, 2), ["é"])
    ] == ["false", "true", "3", "1.5", "2020-01-02T03:04:00", "2020-01-02", '"01:02:00"', '["é"]']


def test_the_reader_needs_no_url_when_the_format_is_given() -> None:
    assert builder_mod.parse_source({"id": "j", "format": "json"}, b"[]") == ([], [])


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
    assert builder_mod.parse_xlsx({"id": "x"}, path.read_bytes()) == (
        ["year", "total"],
        [{"year": "2020", "total": ""}],
    )


def test_excel_errors_name_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.append(("a", "a"))
    path = tmp_path / "b.xlsx"
    book.save(path)
    with pytest.raises(ValueError, match=r"^x: columns \['a'\] appear more than once in the header$"):
        builder_mod.parse_xlsx({"id": "x"}, path.read_bytes())
    with pytest.raises(ValueError, match=r"^x: no sheet 'nope'; the workbook has \['Sheet'\]$"):
        builder_mod.parse_xlsx({"id": "x", "sheet": "nope"}, path.read_bytes())
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(ValueError, match=r"^x: reading .xlsx needs the xlsx extra: uv add 'sourceledger\[xlsx\]'$"):
        builder_mod.parse_xlsx({"id": "x"}, path.read_bytes())


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
        builder_mod.parse_xlsx({"id": "x", "max_bytes": 1000}, payload)
    assert len(builder_mod.parse_xlsx({"id": "x", "max_bytes": 10 * len(payload)}, payload)[1]) == 100


def test_an_excel_workbook_exactly_at_max_bytes_is_read(tmp_path: Path) -> None:
    """As for a gzip, the cap is the most a workbook may expand to, not one byte less."""
    payload = _xlsx(tmp_path, ("year", "value"), (2020, 1.5))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        expanded = sum(info.file_size for info in archive.infolist())
    assert builder_mod.parse_xlsx({"id": "x", "max_bytes": expanded}, payload) == (
        ["year", "value"],
        [{"year": "2020", "value": "1.5"}],
    )
    with pytest.raises(ValueError, match="decompresses past"):
        builder_mod.parse_xlsx({"id": "x", "max_bytes": expanded - 1}, payload)


def test_a_cell_right_of_the_excel_header_is_refused(tmp_path: Path) -> None:
    """As a CSV row with more fields than its header; an empty cell reads as "", like an empty CSV field."""
    trailing = _xlsx(tmp_path, ("a", "b", None), (1,), (1, 2))
    assert builder_mod.parse_xlsx({"id": "x"}, trailing) == (["a", "b"], [{"a": "1", "b": ""}, {"a": "1", "b": "2"}])
    stray = _xlsx(tmp_path, ("a", "b"), (1, 2), (None, None), (1, 2, None, "x", "y"))
    with pytest.raises(ValueError, match=r"^x: row 4 has more cells than the header$"):
        builder_mod.parse_xlsx({"id": "x"}, stray)


@pytest.mark.parametrize("payload", [b"not a workbook", b"PK\x05\x06" + bytes(18)])
def test_a_file_that_is_not_a_workbook_is_refused_by_name(payload: bytes) -> None:
    """A damaged download or an empty zip is a one-line error naming the source, not a zipfile traceback."""
    pytest.importorskip("openpyxl")
    with pytest.raises(ValueError, match=r"^x: not a readable Excel workbook \("):
        builder_mod.parse_xlsx({"id": "x"}, payload)


@pytest.mark.parametrize(
    "payload", [gzip.compress(b"[]")[:-8] + b"\0" * 8, b"\x1f\x8b\x08\x00" + bytes(6) + b"\x07junk"]
)
def test_a_damaged_gzip_is_refused_by_name(payload: bytes) -> None:
    """A bad checksum or a corrupt stream names the source too."""
    with pytest.raises(ValueError, match=r"^j: not a readable gzip file \("):
        builder_mod.parse_json({"id": "j"}, payload)


def test_json_nested_too_deeply_is_refused_by_name() -> None:
    """Up to 3.13 the JSON reader gives up on it; from 3.14 it reads it, and writing the cell does."""
    with pytest.raises(ValueError, match=r"^j: JSON nested too deeply to read$"):
        builder_mod.parse_json({"id": "j"}, b'[{"a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}]")


def test_json_numbers_are_kept_as_written() -> None:
    """1.10 is not 1.1, and 1e5 is not 100000.0, at the top level or nested."""
    payload = '[{"a": 1.10, "b": 1e5, "c": -0, "d": {"é": [2.50, 3E2, true, "ü"], "f": null}}]'.encode()
    _, rows = builder_mod.parse_json({"id": "j"}, payload)
    assert rows == [{"a": "1.10", "b": "1e5", "c": "-0", "d": '{"é": [2.50, 3E2, true, "ü"], "f": null}'}]


def test_a_gzip_is_read_no_further_than_one_byte_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decompression bomb is stopped after max_bytes + 1 bytes, not read to its end first."""
    asks: list[int] = []
    read = gzip.GzipFile.read

    def spy(self: gzip.GzipFile, size: int = -1) -> bytes:
        asks.append(size)
        return read(self, size)

    monkeypatch.setattr(gzip.GzipFile, "read", spy)
    builder_mod.parse_csv({"id": "g", "max_bytes": 100}, gzip.compress(FIXTURE.encode()))
    assert asks == [101]


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
    assert [row["value"] for row in builder_mod.read_output(project / OUT)] == ["1.5", "2.5"]
    assert main(["validate"], manifest=packaged) == 0
    assert main(["validate"]) == 3  # no ./sources.toml here
    assert not packaged.with_name("data").exists()


@pytest.mark.parametrize("offset", [-1, 0])
def test_streamed_gzip_stops_at_the_decompressed_cap(
    offset: int, upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"year,value\n" + b"2020,1.5\n" * 1_000
    cap = len(body) + offset
    upstream.write_bytes(gzip.compress(body))
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\nmax_bytes = {cap}")
    sources = manifest_mod.load_manifest()
    fetcher_mod.fetch(sources)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(b"previous output")
    chunks: list[int] = []
    read1 = gzip.GzipFile.read1

    def spy(self: gzip.GzipFile, size: int = -1) -> bytes:
        data = read1(self, size)
        chunks.append(len(data))
        return data

    monkeypatch.setattr(gzip.GzipFile, "read1", spy)
    if offset:
        with pytest.raises(ValueError, match=f"decompresses past {cap} bytes"):
            builder_mod.build(sources)
        assert OUT.read_bytes() == b"previous output"
        assert sorted(path.name for path in OUT.parent.iterdir()) == [OUT.name]
    else:
        builder_mod.build(sources)
        assert report_mod.validate(sources) == []
    assert chunks
    assert sum(chunks) <= cap + 1


@pytest.mark.parametrize("count", [2_000, 20_000])
def test_json_conversion_releases_each_decoded_object(count: int) -> None:
    payload = b"[" + b'{"flag":true,"value":null},' * (count - 1) + b'{"flag":true,"value":null}]'
    assert _peak(lambda: builder_mod.parse_json({"id": "j"}, payload)) < count * 350
    columns, rows = builder_mod.parse_json({"id": "j"}, payload)
    assert columns == ["flag", "value"]
    assert rows == [{"flag": "true", "value": ""}] * count


def _list_path(source: manifest_mod.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """The default parser behind another name, so build takes the path any custom parser takes."""
    return builder_mod.parse_source(source, payload)


def _both_paths(raw: bytes) -> list[bytes | str]:
    """What build writes, or the error it raises, for a CSV source with these bytes: streamed, then as a list."""
    RAW.write_bytes(raw)
    record = json.loads(Path(f"{RAW}.fetch.json").read_text(encoding="utf-8"))
    record["sha256"] = hashlib.sha256(raw).hexdigest()
    Path(f"{RAW}.fetch.json").write_text(json.dumps(record), encoding="utf-8", newline="")
    results: list[bytes | str] = []
    for parse in (builder_mod.parse_source, _list_path):
        try:
            builder_mod.build(manifest_mod.load_manifest(), parse=parse)
        except (ValueError, csv.Error) as error:
            results.append(f"{type(error).__name__}: {error}")
        else:
            results.append(OUT.read_bytes() + Path("data/processed/datapackage.json").read_bytes())
    return results


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "raw",
    [
        FIXTURE.encode(),
        b"\xef\xbb\xbfyear,value\r\n2020,1.5\r\n",  # a byte-order mark and CRLF
        b'year,note\n2020,"one\ntwo"\n2021,caf\xc3\xa9 \xe2\x82\xac\n',  # a quoted line break, non-ASCII
        b'year,note\r\n2020,"one\r\ntwo"\r\n',  # a quoted CRLF, kept as it is
        b"year,value\n\n2020,1.5\n",  # a blank line
        b"year,value\n",  # no rows
        b"",  # empty
        b"year,year\n1,2\n",
        b"year,value\n2020,1.5,extra\n",
        b"year,value\n2020\n",
        # A short row, then bytes that are not UTF-8 past the first block a stream decodes.
        pytest.param(b"year,value\n2020\n" + b"x" * 20_000 + b",\xff\n", id="short-row-then-not-utf8"),
        b"year,source_id\n2020,x\n",  # a provenance column
        b"year,value\n2020,1.5,\x00\n" + b"2021,2.5\n" * 10,
        b'year,value\n2020,"open\n',
    ],
)
def test_a_streamed_build_writes_or_refuses_what_the_list_path_does(raw: bytes, *, compressed: bool) -> None:
    assert _run("fetch") == [0]
    if compressed:
        raw = gzip.compress(raw)
    streamed, listed = _both_paths(raw)
    assert streamed == listed


def test_a_file_source_checks_its_raw_hash_before_building(upstream: Path, tmp_path: Path) -> None:
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nformat = "file"')
    assert _run("fetch") == [0]
    RAW.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed since it was fetched"):
        builder_mod.build(manifest_mod.load_manifest())
    assert not OUT.exists()


def test_parse_source_reports_that_files_have_no_table() -> None:
    with pytest.raises(ValueError, match="format file has no table"):
        builder_mod.parse_source({"id": "example", "format": "file"}, b"anything")


@pytest.mark.parametrize("suffix", [".json", ".JSON"])
@pytest.mark.usefixtures("upstream")
def test_a_file_output_cannot_overwrite_the_package(suffix: str) -> None:
    sources = manifest_mod.load_manifest()
    source = sources[0]
    source.update(id="datapackage", format="file", url=source["url"].removesuffix(".csv") + suffix)
    Path(url2pathname(urlsplit(source["url"]).path)).write_bytes(b"raw")
    fetcher_mod.fetch(sources)
    with pytest.raises(ValueError, match=r"datapackage\.json.*reserved"):
        builder_mod.build(sources)


def test_public_build_preserves_custom_parser(upstream: Path):
    sources = manifest_mod.load_manifest()
    fetcher_mod.fetch(sources)

    def parse(source: manifest_mod.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        assert source["id"] == "example"
        assert payload == upstream.read_bytes()
        return ["custom"], [{"custom": "kept"}]

    assert builder_mod.build(sources, parse=parse) == [OUT]
    row = next(builder_mod.read_output(OUT))
    assert row["custom"] == "kept"
    assert row["source_id"] == "example"


def test_a_recorded_file_builds_like_a_fetched_one(upstream: Path) -> None:
    """The same bytes, fetched or recorded, give the same rows; only the record says which happened."""
    (fetched,) = fetcher_mod.fetch(manifest_mod.load_manifest())
    builder_mod.build(manifest_mod.load_manifest())
    fetched_rows = list(builder_mod.read_output(OUT))
    shutil.rmtree("data")

    recorded = ledger.record(_source(), upstream, retrieved_at=WHEN, last_modified="Tue, 03 Nov 2026 08:00:00 GMT")
    builder_mod.build(manifest_mod.load_manifest())

    assert recorded == dataclasses.replace(
        fetched,
        retrieved_at="2026-11-05T09:30:00Z",
        last_modified="Tue, 03 Nov 2026 08:00:00 GMT",
        recorded_at=recorded.recorded_at,
    )
    assert recorded.recorded_at.endswith("Z")
    assert [{**row, "retrieved_at": ""} for row in builder_mod.read_output(OUT)] == [
        {**row, "retrieved_at": ""} for row in fetched_rows
    ]
    assert upstream.read_text(encoding="utf-8") == FIXTURE  # copied, not moved
    assert report_mod.validate(manifest_mod.load_manifest()) == []
    assert report_mod.verify(Path("data/processed")) == []


def test_record_states_no_download_time_it_was_not_given(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Without retrieved_at the rows say nothing about when; status and revisions say when it was recorded."""
    LOG.parent.mkdir()
    LOG.write_text(_record("example", "0") + "\n", encoding="utf-8", newline="")
    shutil.copy(LOG, "before.jsonl")
    result = ledger.record(_source(), upstream)
    assert (result.retrieved_at, bool(result.recorded_at)) == ("", True)
    builder_mod.build(manifest_mod.load_manifest())
    assert {row["retrieved_at"] for row in builder_mod.read_output(OUT)} == {""}
    assert f"recorded {result.recorded_at}" in report_mod.status(manifest_mod.load_manifest())[0]
    assert f"recorded {result.recorded_at} from" in ledger.revisions(Path("before.jsonl"))[0]
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["recorded_at"] == result.recorded_at


def test_parse_csv_skips_blank_lines() -> None:
    """A blank line is no row, as in csv.DictReader; a row of empty cells is one, and a short row is refused."""
    payload = b"year,value\n\n2020,1.5\r\n\r\n,\n2021,2.5\n"
    assert builder_mod.parse_csv({"id": "x"}, payload) == (
        ["year", "value"],
        [{"year": "2020", "value": "1.5"}, {"year": "", "value": ""}, {"year": "2021", "value": "2.5"}],
    )


@pytest.mark.usefixtures("upstream")
def test_a_csv_output_ends_its_lines_in_crlf() -> None:
    """RFC 4180 line ends, as 0.4.0 wrote them: readers of earlier outputs get the same bytes."""
    assert _run("fetch", "build") == [0, 0]
    out = Path("data/processed/example.csv").read_bytes()
    assert out.startswith(b"source_id,source_url,retrieved_at,vintage,raw_sha256,")
    assert out.count(b"\n") == out.count(b"\r\n") > 1
