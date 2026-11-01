"""An offline provenance report must be useful without exposing data or executing metadata."""

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sourceledger import ledger
from sourceledger.cli import main
from sourceledger.report import report


class Elements(HTMLParser):
    """Collect active content and links, rather than matching escaped text."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.links.extend(value for key, value in attrs if key == "href" and value is not None)


@pytest.fixture
def dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[ledger.Source]:
    monkeypatch.chdir(tmp_path)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    sources = []
    for sid, payload, fmt in (
        ("table", b"year,value\n2023,1.5\n", "csv"),
        ("document", b"PRIVATE RAW CONTENT", "file"),
    ):
        file = upstream / f"{sid}.csv"
        file.write_bytes(payload)
        sources.append(
            {
                "id": sid,
                "status": "implemented",
                "url": file.as_uri(),
                "format": fmt,
                "vintage": "2023",
                "terms": "Example terms",
                "terms_url": "https://example.org/terms",
                "license": "LicenseRef-example",
                "redistribute": False,
            }
        )
    ledger.fetch(sources)
    ledger.build(sources)
    return sources


def test_report_links_outputs_to_sources_and_checks_integrity(dataset: list[ledger.Source]) -> None:
    page = report(dataset)
    dom = Elements()
    dom.feed(page)
    assert "Integrity checks passed" in page
    assert "document.csv" in page
    assert "table.csv" in page
    assert "#source-0" in dom.links
    assert "#source-1" in dom.links
    assert hashlib.sha256(b"PRIVATE RAW CONTENT").hexdigest() in page
    assert "PRIVATE RAW CONTENT" not in page
    assert "LicenseRef-example" in page
    assert "Example terms" in page
    assert "does not grant permission" in page
    assert "script" not in dom.tags
    assert "iframe" not in dom.tags
    Path("data/processed/document.csv").write_bytes(b"changed")
    assert "Integrity problems" in report(dataset)


def test_report_escapes_metadata_and_omits_credentials(dataset: list[ledger.Source]) -> None:
    dataset[0].update(
        name='<script>alert("x")</script>',
        terms="<img src=x onerror=alert(1)>",
        terms_url="javascript:alert(1)",
        url="https://user:password@example.org/data?token=SECRET#PRIVATE",
        headers={"Authorization": "env:PRIVATE_TOKEN"},
    )
    package = Path("data/processed/datapackage.json")
    data = json.loads(package.read_text())
    data["resources"][0]["sourceledger"]["final_url"] = "https://example.org/data?key=SECRET"
    package.write_text(json.dumps(data))
    page = report(dataset)
    dom = Elements()
    dom.feed(page)
    assert "&lt;script&gt;" in page
    assert "&lt;img" in page
    assert not {"script", "img", "iframe"} & set(dom.tags)
    assert all(link.startswith("#") for link in dom.links)
    assert all(secret not in page for secret in ("password", "SECRET", "PRIVATE", "Authorization", "env:"))
    assert "https://example.org/data" in page


def test_report_marks_an_output_from_an_earlier_fetch(dataset: list[ledger.Source]) -> None:
    Path("upstream/table.csv").write_text("year,value\n2023,2.5\n")
    ledger.fetch(dataset)
    assert "built from an earlier fetch" in report(dataset)


def test_report_cli_errors_do_not_write_partial_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("sources.toml").write_text("")
    assert main(["report"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("package", [[], {}, {"resources": [None]}])
def test_report_rejects_malformed_package(dataset: list[ledger.Source], package: object) -> None:
    Path("data/processed/datapackage.json").write_text(json.dumps(package))
    with pytest.raises(ValueError, match="list of resource objects"):
        report(dataset)


@pytest.mark.usefixtures("dataset")
def test_report_describes_missing_provenance_without_claiming_success() -> None:
    Path("data/fetches.jsonl").unlink()
    page = report([])
    assert "missing from the manifest" in page
    assert "no fetch record" in page
    assert "Integrity checks passed" not in page


def test_report_handles_malformed_urls_as_text(dataset: list[ledger.Source]) -> None:
    dataset[0]["terms_url"] = "https://[invalid"
    assert "Local or non-HTTP URL omitted" in report(dataset)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://user:secret@example.org/data?key=hidden#hidden", "http://example.org/data"),
        ("https://user:secret@example.org/data?key=hidden#hidden", "https://example.org/data"),
        ("https://user:sec@ret@example.org/data?key=hidden#hidden", "https://example.org/data"),
        ("ftp://user:secret@example.org/data", "Local or non-HTTP URL omitted"),
        ("https:/missing-host", "Local or non-HTTP URL omitted"),
    ],
)
def test_report_keeps_only_the_public_part_of_a_url(dataset: list[ledger.Source], url: str, expected: str) -> None:
    dataset[0]["terms_url"] = url
    page = report(dataset)
    assert f"<dt>Terms URL</dt><dd>{expected}</dd>" in page
    assert "secret" not in page
    assert "hidden" not in page


def test_report_displays_the_built_metadata_and_security_policy(dataset: list[ledger.Source]) -> None:
    package = json.loads(Path("data/processed/datapackage.json").read_text())
    resource = package["resources"][0]
    page = report(dataset)
    expected = {
        "File": "table.csv",
        "Size in bytes": resource["bytes"],
        "Output SHA-256": resource["hash"],
        "Fetched at": resource["sourceledger"]["retrieved_at"],
        "Vintage": "2023",
        "Raw SHA-256": hashlib.sha256(b"year,value\n2023,1.5\n").hexdigest(),
        "Licence": "LicenseRef-example",
        "Terms": "Example terms",
        "Raw redistribution allowed": "False",
    }
    for key, value in expected.items():
        assert f"<dt>{key}</dt><dd>{value}</dd>" in page
    assert "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'" in page


@pytest.mark.parametrize("key", ["url", "vintage"])
def test_report_flags_manifest_changes(dataset: list[ledger.Source], key: str) -> None:
    dataset[0][key] = "https://example.org/annual.csv" if key == "url" else "2024"
    page = report(dataset)
    assert "manifest differs from the last build" in page
    assert "Integrity checks passed" not in page


@pytest.mark.parametrize(
    ("key", "value"), [("sources", None), ("sources", []), ("sources", [None]), ("sourceledger", [])]
)
def test_report_handles_damaged_provenance(dataset: list[ledger.Source], key: str, value: object) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text())
    package["resources"][0][key] = value
    path.write_text(json.dumps(package))
    page = report(dataset)
    assert "Integrity problems" in page


def test_report_cli_writes_one_complete_document(
    dataset: list[ledger.Source], capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = "".join(
        "[[source]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in source.items())
        for source in dataset
    )
    Path("sources.toml").write_text(manifest)
    capsys.readouterr()
    assert main(["report"]) == 0
    assert capsys.readouterr().out == report(dataset)


def test_report_keeps_source_urls_and_names(dataset: list[ledger.Source]) -> None:
    dataset[0].update(url="https://example.org/data", name="Example observations")
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text())
    package["resources"][0]["sourceledger"]["final_url"] = "https://example.org/final?token=secret"
    path.write_text(json.dumps(package))
    page = report(dataset)
    assert '<h3 id="source-0">Example observations</h3>' in page
    assert '<h3 id="source-1">document</h3>' in page
    assert "<dt>URL</dt><dd>https://example.org/data</dd>" in page
    assert "<dt>Final URL</dt><dd>https://example.org/final</dd>" in page


def test_report_accepts_a_source_without_a_declared_vintage(dataset: list[ledger.Source]) -> None:
    del dataset[0]["vintage"]
    ledger.fetch(dataset)
    ledger.build(dataset)
    assert "Integrity checks passed" in report(dataset)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.text())
def test_report_keeps_arbitrary_metadata_out_of_html_elements(dataset: list[ledger.Source], name: str) -> None:
    # The dataset stays fixed across examples; only the displayed name changes.
    dataset[0]["name"] = name
    dom = Elements()
    dom.feed(report(dataset))
    assert set(dom.tags) <= {
        "html",
        "head",
        "meta",
        "title",
        "style",
        "body",
        "main",
        "h1",
        "p",
        "h2",
        "ul",
        "li",
        "section",
        "dl",
        "dt",
        "dd",
        "a",
        "h3",
    }
    assert all(link.startswith("#") for link in dom.links)


def test_report_does_not_echo_credentials_from_an_invalid_file_url(dataset: list[ledger.Source]) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text())
    package["resources"][0]["path"] = "https://user:secret@example.org/data?token=PRIVATE"
    path.write_text(json.dumps(package))
    try:
        text = report(dataset)
    except ValueError as error:
        text = str(error)
    assert "secret" not in text
    assert "PRIVATE" not in text


@pytest.mark.parametrize("path_value", [None, "../outside.csv", "/outside.csv"])
def test_report_refuses_invalid_resource_paths(dataset: list[ledger.Source], path_value: object) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text())
    package["resources"][0]["path"] = path_value
    path.write_text(json.dumps(package))
    with pytest.raises(ValueError, match="relative files"):
        report(dataset)


@pytest.mark.parametrize("change", ["omit one", "omit all", "unimplement", "duplicate"])
def test_report_flags_incomplete_or_obsolete_packages(dataset: list[ledger.Source], change: str) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text())
    if change == "omit one":
        package["resources"].pop()
    elif change == "omit all":
        package["resources"] = []
    elif change == "unimplement":
        dataset[0]["status"] = "unimplemented"
    else:
        package["resources"].append(package["resources"][0])
    path.write_text(json.dumps(package))
    page = report(dataset)
    assert "Integrity checks passed" not in page
    assert "not the implemented sources" in page
    assert "re-run build" in page


def test_report_accepts_a_genuinely_empty_dataset(tmp_path: Path) -> None:
    (tmp_path / "datapackage.json").write_text('{"resources": []}')
    assert "Integrity checks passed" in report([], processed=tmp_path, log=tmp_path / "missing.jsonl")
