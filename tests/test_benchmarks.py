"""Benchmarks of the pipeline's CPU-bound paths, measured by CodSpeed (`pytest --codspeed`).

Without `--codspeed` each benchmark runs once, as a plain test. Everything is local: the
sources are files served through file:// URLs.
"""

import gzip
import io
import json
from pathlib import Path

import openpyxl
import pytest
from pytest_codspeed import BenchmarkFixture

from sourceledger import ledger

ROWS = 5_000
SOURCES = 20
TERMS = 'terms = "CC0-1.0"\nterms_url = "https://example.org/terms"\nlicense = "CC0-1.0"\nredistribute = true'
SOURCE = {"id": "bench", "url": "https://example.org/bench.csv"}


def _csv(rows: int = ROWS) -> bytes:
    lines = ["year,region,value,unit,note"]
    lines += [f"{1900 + i % 125},region-{i % 50},{i * 0.25:.2f},kt,row {i}" for i in range(rows)]
    return ("\n".join(lines) + "\n").encode()


def _json(rows: int = ROWS) -> bytes:
    items = [
        {"year": 1900 + i % 125, "region": f"region-{i % 50}", "value": i * 0.25, "ok": i % 2 == 0, "tags": [i, "x"]}
        for i in range(rows)
    ]
    return json.dumps(items).encode()


def _xlsx(rows: int = 1_000) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["year", "region", "value", "unit"])
    for i in range(rows):
        sheet.append([1900 + i % 125, f"region-{i % 50}", i * 0.25, "kt"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _write_manifest(root: Path, upstream: Path, count: int = SOURCES) -> None:
    """`count` implemented sources, each a CSV under `upstream`, plus one unimplemented source."""
    upstream.mkdir(exist_ok=True)
    tables = []
    for n in range(count):
        file = upstream / f"src{n}.csv"
        file.write_bytes(_csv(ROWS // count))
        tables.append(f'[[source]]\nid = "src{n}"\nstatus = "implemented"\nurl = "{file.as_uri()}"\n{TERMS}\n')
    tables.append('[[source]]\nid = "later"\nstatus = "unimplemented"\n')
    (root / "sources.toml").write_text("\n".join(tables), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with a manifest of local sources; the benchmark runs inside it."""
    _write_manifest(tmp_path, tmp_path / "upstream")
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
    payload = _csv()
    columns, rows = benchmark(ledger.parse_csv, SOURCE, payload)
    assert len(columns) == 5
    assert len(rows) == ROWS


def test_parse_csv_gzip(benchmark: BenchmarkFixture) -> None:
    payload = gzip.compress(_csv(), mtime=0)
    _, rows = benchmark(ledger.parse_csv, SOURCE, payload)
    assert len(rows) == ROWS


def test_parse_json(benchmark: BenchmarkFixture) -> None:
    payload = _json()
    columns, rows = benchmark(ledger.parse_json, {**SOURCE, "format": "json"}, payload)
    assert columns == ["year", "region", "value", "ok", "tags"]
    assert len(rows) == ROWS


def test_parse_xlsx(benchmark: BenchmarkFixture) -> None:
    payload = _xlsx()
    _, rows = benchmark(ledger.parse_xlsx, {**SOURCE, "format": "xlsx"}, payload)
    assert len(rows) == 1_000


def test_load_manifest(benchmark: BenchmarkFixture, project: Path) -> None:
    sources = benchmark(ledger.load_manifest, project / "sources.toml")
    assert len(sources) == SOURCES + 1


def test_fetch(benchmark: BenchmarkFixture, project: Path) -> None:
    sources = ledger.load_manifest(project / "sources.toml")
    records = benchmark(ledger.fetch, sources)
    assert len(records) == SOURCES


def test_build(benchmark: BenchmarkFixture, fetched: list[ledger.Source]) -> None:
    written = benchmark(ledger.build, fetched)
    assert len(written) == SOURCES


def test_validate(benchmark: BenchmarkFixture, built: list[ledger.Source]) -> None:
    assert benchmark(ledger.validate, built) == []


def test_verify(benchmark: BenchmarkFixture, built: list[ledger.Source]) -> None:
    assert len(built) == SOURCES + 1
    assert benchmark(ledger.verify, Path("data/processed")) == []


def _log(path: Path, versions: int) -> None:
    """A fetch log with `versions` versions of each of SOURCES sources."""
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
        for n in range(SOURCES)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_revisions(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    _log(before, 50)
    _log(after, 100)
    lines = benchmark(ledger.revisions, before, log=after)
    assert len(lines) == SOURCES


def test_status(benchmark: BenchmarkFixture, project: Path, tmp_path: Path) -> None:
    sources = ledger.load_manifest(project / "sources.toml")
    log = tmp_path / "fetches.jsonl"
    _log(log, 100)
    lines = benchmark(ledger.status, sources, log=log)
    assert len(lines) == SOURCES + 1
