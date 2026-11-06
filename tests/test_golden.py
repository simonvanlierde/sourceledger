"""What sourceledger writes, byte for byte: records, logs, packages, schemas and reports.

A change to any of these files is a change to a format other people read. Rerun with
`pytest --update-goldens` only when the change is meant, and say so in the changelog.
"""

import io
import json
import re
import shutil
from collections.abc import Callable
from email.message import Message
from pathlib import Path

import pytest

from sourceledger import ledger
from sourceledger.cli import main
from sourceledger.report import report

FIXTURES = Path(__file__).parent / "fixtures"
TERMS = """terms = "Free to reuse with attribution"
terms_url = "https://example.org/terms"
license = "CC-BY-4.0"
redistribute = true"""

type Golden = Callable[[str, str], None]


def _project(root: Path, monkeypatch: pytest.MonkeyPatch, manifest: str) -> None:
    """A project folder with a fixed name, since the package is named after it."""
    project = root / "golden-project"
    project.mkdir()
    (project / "sources.toml").write_text(manifest, encoding="utf-8")
    monkeypatch.chdir(project)


def _text(path: str, publisher: str) -> str:
    """A written file, with the publisher's free port replaced by a fixed one."""
    return Path(path).read_text(encoding="utf-8").replace(publisher, "http://127.0.0.1:8000")


def _report(sources: list[ledger.Source], publisher: str) -> str:
    """The HTML report, normalized as `_package` does."""
    html = report(sources).replace(publisher, "http://127.0.0.1:8000")
    return re.sub(
        r"<dt>Size in bytes</dt><dd>\d+</dd><dt>Output SHA-256</dt><dd>sha256:[0-9a-f]{64}</dd>",
        "<dt>Size in bytes</dt><dd>VARIES</dd><dt>Output SHA-256</dt><dd>VARIES</dd>",
        html,
    )


def _package(publisher: str) -> str:
    """datapackage.json, normalized as `_text` does.

    A table's size and hash cover its rows, whose URLs hold the free port. The table goldens
    pin those rows, and validate checks the hashes against them.
    """
    package = json.loads(_text("data/processed/datapackage.json", publisher))
    for resource in package["resources"]:
        if resource.get("type") == "table":
            resource.update(bytes="<varies with the port>", hash="<varies with the port>")
    return json.dumps(package, indent=2) + "\n"


@pytest.mark.usefixtures("frozen_clock")
def test_a_full_dataset_matches_its_goldens(
    publisher: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden: Golden
) -> None:
    """A redirected CSV, a file source and a recorded mirror download, each with every optional key."""
    download = tmp_path / "pooch-cache" / "mirror.csv"
    download.parent.mkdir()
    download.write_text("country,value\nNL,3\n", encoding="utf-8")
    _project(
        tmp_path,
        monkeypatch,
        f"""
[[source]]
id = "table"
name = "Example series"
status = "implemented"
url = "{publisher}/old.csv"
vintage = "2024 release"
{TERMS}

[[source]]
id = "document"
status = "implemented"
url = "{publisher}/data.bin"
format = "file"
{TERMS}

[[source]]
id = "mirror"
name = "Recorded mirror download"
status = "implemented"
url = "{publisher}/mirror.csv"
vintage = 2024-06-01
{TERMS}
""",
    )
    sources = ledger.load_manifest()
    ledger.fetch(sources, only=["table", "document"])
    ledger.record(
        sources[2],
        download,
        retrieved_at="2026-11-04T08:00:00+01:00",
        url="https://mirror.example.org/mirror.csv?country=NL",
        parameters={"country": "NL", "year": 2024},
    )
    ledger.build(sources)
    assert ledger.validate(sources) == []
    golden("full/fetches.jsonl", _text("data/fetches.jsonl", publisher))
    golden("full/table.csv.fetch.json", _text("data/raw/table.csv.fetch.json", publisher))
    golden("full/document.bin.fetch.json", _text("data/raw/document.bin.fetch.json", publisher))
    golden("full/mirror.csv.fetch.json", _text("data/raw/mirror.csv.fetch.json", publisher))
    golden("full/table.csv", _text("data/processed/table.csv", publisher))
    golden("full/mirror.csv", _text("data/processed/mirror.csv", publisher))
    golden("full/datapackage.json", _package(publisher))
    golden("full/report.html", _report(sources, publisher))


@pytest.mark.usefixtures("frozen_clock")
def test_a_minimal_dataset_matches_its_goldens(
    publisher: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden: Golden
) -> None:
    """One source with only the keys a fetch needs: the report says what is not stated."""
    _project(tmp_path, monkeypatch, f'[[source]]\nid = "only"\nstatus = "implemented"\nurl = "{publisher}/data.csv"\n')
    sources = ledger.load_manifest()
    ledger.fetch(sources)
    ledger.build(sources)
    golden("minimal/datapackage.json", _package(publisher))
    golden("minimal/report.html", _report(sources, publisher))


@pytest.mark.usefixtures("frozen_clock")
def test_the_parquet_schema_matches_its_golden(
    publisher: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden: Golden
) -> None:
    """Provenance columns first, every column a string, zstd: what other readers of the table rely on."""
    pq = pytest.importorskip("pyarrow.parquet")
    manifest = (
        f'output_format = "parquet"\n\n[[source]]\nid = "table"\nstatus = "implemented"\nurl = "{publisher}/data.csv"\n'
    )
    _project(tmp_path, monkeypatch, manifest)
    sources = ledger.load_manifest()
    ledger.fetch(sources)
    ledger.build(sources, output_format="parquet")
    metadata = pq.ParquetFile("data/processed/table.parquet").metadata
    codecs = {metadata.row_group(0).column(i).compression for i in range(metadata.num_columns)}
    golden("parquet/schema.txt", f"{pq.read_schema('data/processed/table.parquet')}\ncompression: {sorted(codecs)}\n")


class _Response(io.BytesIO):
    def __init__(self, body: bytes, url: str, **headers: str) -> None:
        super().__init__(body)
        self.headers = Message()
        for name, value in {"Content-Length": str(len(body)), **headers}.items():
            self.headers[name] = value
        self.url = url


@pytest.fixture
def v040(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project as sourceledger 0.4.0 left it after `fetch` and `build`, from a local HTTP server.

    Made with `uvx --from sourceledger==0.4.0 sourceledger fetch` and `build`, in a folder
    named `proj` against `python -m http.server 8731`. The server is not running now.
    """
    project = tmp_path / "proj"
    shutil.copytree(FIXTURES / "v0.4.0", project)
    monkeypatch.chdir(project)
    return project


@pytest.mark.usefixtures("v040")
def test_a_0_4_0_project_validates_and_rebuilds_unchanged() -> None:
    """Records, log and outputs from 0.4.0 read as they are; a rebuild writes the same bytes."""
    sources = ledger.load_manifest()
    assert ledger.validate(sources) == []
    assert ledger.verify(Path("data/processed")) == []
    assert "Integrity checks passed" in report(sources)
    assert main(["status"]) == 0
    before = {p: p.read_bytes() for p in Path("data/processed").iterdir()}
    ledger.build(sources)
    assert {p: p.read_bytes() for p in Path("data/processed").iterdir()} == before


@pytest.mark.usefixtures("frozen_clock", "v040")
def test_a_fetch_over_a_0_4_0_project_writes_the_0_4_0_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same bytes again add no log line, and the new sidecar has exactly the keys 0.4.0 wrote."""
    old = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    log = Path("data/fetches.jsonl").read_bytes()
    body = Path("data/raw/example.csv").read_bytes()

    def open_(request: object, timeout: float) -> _Response:  # noqa: ARG001  # the stand-in ignores the timeout
        return _Response(body, old["url"], **{"Last-Modified": old["last_modified"]})

    monkeypatch.setattr(ledger, "_open", open_)
    (record,) = ledger.fetch(ledger.load_manifest())
    assert record.retrieved_at == "2026-11-05T09:30:00Z"
    new = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert list(new) == list(old)
    assert {k: v for k, v in new.items() if k != "retrieved_at"} == {
        k: v for k, v in old.items() if k != "retrieved_at"
    }
    assert Path("data/fetches.jsonl").read_bytes() == log
