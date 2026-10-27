"""Benchmarks of the pipeline's CPU-bound paths, measured by CodSpeed (`pytest --codspeed`).

Without `--codspeed` each benchmark runs once, as a plain test. Everything is local: the
sources are files served through file:// URLs.

CodSpeed's simulation counts CPU instructions and leaves out time spent in system calls,
so `fetch` and `verify` measure the Python around their file I/O, not the I/O itself.
"""

import io
import json
from datetime import date
from pathlib import Path

import openpyxl
import pytest
from pytest_codspeed import BenchmarkFixture

from sourceledger import ledger

ROWS = 20_000
SOURCES = 20
TERMS = 'terms = "CC0-1.0"\nterms_url = "https://example.org/terms"\nlicense = "CC0-1.0"\nredistribute = true'
SOURCE = {"id": "bench", "url": "https://example.org/bench.csv"}


def _csv(rows: int = ROWS) -> bytes:
    lines = ["year,region,value,unit,note"]
    lines += [f"{1900 + i % 125},region-{i % 50},{i * 0.25:.2f},kt,row {i}" for i in range(rows)]
    return ("\n".join(lines) + "\n").encode()


def _wide_csv(columns: int, rows: int) -> bytes:
    """Years as columns, as statistics offices often publish."""
    lines = [",".join(["region", *(f"y{1000 + c}" for c in range(columns - 1))])]
    lines += [",".join([f"region-{r}", *(f"{r * c * 0.5:.1f}" for c in range(columns - 1))]) for r in range(rows)]
    return ("\n".join(lines) + "\n").encode()


def _json(rows: int = ROWS) -> bytes:
    items = [
        {"year": 1900 + i % 125, "region": f"region-{i % 50}", "value": i * 0.25, "ok": i % 2 == 0, "tags": [i, "x"]}
        for i in range(rows)
    ]
    # Every 10th object lacks a key, so the empty-cell path is measured too.
    for item in items[::10]:
        del item["tags"]
    return json.dumps(items).encode()


def _xlsx(rows: int = 2_000) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["year", "region", "value", "unit", "published"])
    for i in range(rows):
        sheet.append([1900 + i % 125, f"region-{i % 50}", i * 0.25, "kt", date(2026, 1 + i % 12, 1)])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _write_manifest(root: Path, count: int = SOURCES, rows: int = ROWS, extra: str = "") -> None:
    """`count` implemented sources sharing `rows` rows, each a CSV under `root/upstream`, plus one unimplemented.

    `extra` is added to each implemented source's table.
    """
    upstream = root / "upstream"
    upstream.mkdir(exist_ok=True)
    tables = []
    for n in range(count):
        file = upstream / f"src{n}.csv"
        file.write_bytes(_csv(rows // count))
        tables.append(f'[[source]]\nid = "src{n}"\nstatus = "implemented"\nurl = "{file.as_uri()}"\n{TERMS}\n{extra}')
    tables.append('[[source]]\nid = "later"\nstatus = "unimplemented"\n')
    (root / "sources.toml").write_text("\n".join(tables), encoding="utf-8")


def _log(path: Path, sources: int, versions: int) -> None:
    """A fetch log with `versions` versions of each of `sources` sources, oldest first."""
    lines = [
        json.dumps(
            {
                "source_id": f"src{n}",
                "url": f"https://example.org/src{n}.csv",
                "retrieved_at": f"2026-01-{1 + v % 28:02d}T00:00:00Z",
                "sha256": f"{v:032x}{n:032x}",
                "bytes": 1000 + v,
                "vintage": "not stated",
            }
        )
        for v in range(versions)
        for n in range(sources)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with a manifest of local sources; the benchmark runs inside it."""
    _write_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fetched(project: Path) -> list[ledger.Source]:
    """The project's sources, fetched into data/raw/."""
    sources = ledger.load_manifest(project / "sources.toml")
    ledger.fetch(sources)
    return sources


@pytest.fixture
def built(fetched: list[ledger.Source]) -> list[ledger.Source]:
    """The project's sources, fetched and built into data/processed/."""
    ledger.build(fetched)
    return fetched


def test_parse_csv(benchmark: BenchmarkFixture) -> None:
    columns, rows = benchmark(ledger.parse_csv, SOURCE, _csv())
    assert len(columns) == 5
    assert len(rows) == ROWS


def test_parse_csv_wide(benchmark: BenchmarkFixture) -> None:
    # Tables with years as columns get wide: every per-column check runs once per row.
    columns, rows = benchmark(ledger.parse_csv, SOURCE, _wide_csv(1_000, 100))
    assert len(columns) == 1_000
    assert len(rows) == 100


def test_parse_json(benchmark: BenchmarkFixture) -> None:
    columns, rows = benchmark(ledger.parse_json, {**SOURCE, "format": "json"}, _json())
    assert columns == ["year", "region", "value", "ok", "tags"]
    assert len(rows) == ROWS
    assert rows[0]["tags"] == ""


def test_parse_xlsx(benchmark: BenchmarkFixture) -> None:
    columns, rows = benchmark(ledger.parse_xlsx, {**SOURCE, "format": "xlsx"}, _xlsx())
    assert columns == ["year", "region", "value", "unit", "published"]
    assert len(rows) == 2_000
    assert rows[0]["published"] == "2026-01-01T00:00:00"


def test_load_manifest(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    # A large manifest: every check runs once per source.
    _write_manifest(tmp_path, count=500, rows=500)
    sources = benchmark(ledger.load_manifest, tmp_path / "sources.toml")
    assert len(sources) == 501


def test_fetch(benchmark: BenchmarkFixture, project: Path) -> None:
    # After the first round each fetch re-downloads the same bytes: file:// has no 304.
    sources = ledger.load_manifest(project / "sources.toml")
    records = benchmark(ledger.fetch, sources)
    assert len(records) == SOURCES


def test_fetch_archived(benchmark: BenchmarkFixture, project: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Each source comes back with the same bytes and no 304, so keeps the snapshot its last log line names,
    # behind a long history of other versions.
    _write_manifest(project, extra="archive = true\n")
    sources = ledger.load_manifest(project / "sources.toml")
    with caplog.at_level("ERROR"):  # file:// URLs are never archived, which warns once per source
        ledger.fetch(sources)
    log = Path("data/fetches.jsonl")
    current = log.read_text(encoding="utf-8")
    _log(log, SOURCES, 500)
    log.write_text(log.read_text(encoding="utf-8") + current, encoding="utf-8")
    records = benchmark(ledger.fetch, sources)
    assert len(records) == SOURCES


def test_build(benchmark: BenchmarkFixture, fetched: list[ledger.Source]) -> None:
    written = benchmark(ledger.build, fetched)
    assert len(written) == SOURCES


def test_validate(benchmark: BenchmarkFixture, built: list[ledger.Source]) -> None:
    assert benchmark(ledger.validate, built) == []


@pytest.mark.usefixtures("built")
def test_verify(benchmark: BenchmarkFixture) -> None:
    assert benchmark(ledger.verify, Path("data/processed")) == []


def test_revisions(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    # Reads both logs whole; `status` and every `fetch` read the log the same way.
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    _log(before, SOURCES, 250)
    _log(after, SOURCES, 500)
    lines = benchmark(ledger.revisions, before, log=after)
    assert len(lines) == SOURCES
