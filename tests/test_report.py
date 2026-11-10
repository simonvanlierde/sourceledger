"""An offline provenance report must be useful without exposing data or executing metadata."""

import gzip
import hashlib
import json
import sys
import time
from datetime import datetime, tzinfo
from html.parser import HTMLParser
from pathlib import Path
from typing import Self

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from support import (
    FIXTURE,
    LOG,
    OUT,
    RAW,
    TERMS,
    WHEN,
    _http_error,
    _manifest,
    _peak,
    _run,
    _serve,
    _symlink,
    _two_sources,
    builder_mod,
    fetcher_mod,
    report_mod,
)

from sourceledger import ledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main
from sourceledger.manifest import load_manifest
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
def dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[manifest_mod.Source]:
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
    fetcher_mod.fetch(sources)
    builder_mod.build(sources)
    return sources


def test_report_links_outputs_to_sources_and_checks_integrity(dataset: list[manifest_mod.Source]) -> None:
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


def test_report_escapes_metadata_and_omits_credentials(dataset: list[manifest_mod.Source]) -> None:
    dataset[0].update(
        name='<script>alert("x")</script>',
        terms="<img src=x onerror=alert(1)>",
        terms_url="javascript:alert(1)",
        url="https://user:password@example.org/data?token=SECRET#PRIVATE",
        headers={"Authorization": "env:PRIVATE_TOKEN"},
    )
    package = Path("data/processed/datapackage.json")
    data = json.loads(package.read_text(encoding="utf-8"))
    data["resources"][0]["sourceledger"]["final_url"] = "https://example.org/data?key=SECRET"
    package.write_text(json.dumps(data), encoding="utf-8")
    page = report(dataset)
    dom = Elements()
    dom.feed(page)
    assert "&lt;script&gt;" in page
    assert "&lt;img" in page
    assert not {"script", "img", "iframe"} & set(dom.tags)
    assert all(link.startswith("#") for link in dom.links)
    assert all(secret not in page for secret in ("password", "SECRET", "PRIVATE", "Authorization", "env:"))
    assert "https://example.org/data" in page


def test_report_marks_an_output_from_an_earlier_fetch(dataset: list[manifest_mod.Source]) -> None:
    Path("upstream/table.csv").write_text("year,value\n2023,2.5\n", encoding="utf-8")
    fetcher_mod.fetch(dataset)
    assert "built from an earlier fetch" in report(dataset)


def test_report_marks_an_output_built_before_a_relog(upstream: Path) -> None:
    """Same bytes recorded again with another time: validate fails, so the report must not pass."""
    assert main(["record", "example", str(upstream)]) == 0
    assert main(["build"]) == 0
    assert main(["record", "example", str(upstream), "--retrieved-at", WHEN]) == 0
    assert main(["validate"]) == 1
    page = report(load_manifest())
    assert "built from an earlier fetch" in page
    assert "Integrity checks passed" not in page


def test_report_passes_a_build_from_a_log_line_older_than_final_url(dataset: list[manifest_mod.Source]) -> None:
    """Lines logged before `final_url` existed lack it; the build stamped the empty default."""
    log = Path("data/fetches.jsonl")
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert all(line.pop("final_url") == "" for line in lines)
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    assert "Integrity checks passed" in report(dataset)


def test_report_cli_errors_do_not_write_partial_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("sources.toml").write_text("", encoding="utf-8")
    assert main(["report"]) == 3
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("package", [[], {}, {"resources": [None]}])
def test_report_rejects_malformed_package(dataset: list[manifest_mod.Source], package: object) -> None:
    Path("data/processed/datapackage.json").write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(ValueError, match=r"^datapackage.json needs a list of resource objects; re-run build$"):
        report(dataset)


@pytest.mark.usefixtures("dataset")
def test_report_describes_missing_provenance_without_claiming_success() -> None:
    Path("data/fetches.jsonl").unlink()
    page = report([])
    assert "missing from the manifest" in page
    assert "no fetch record" in page
    assert "Integrity checks passed" not in page


def test_report_handles_malformed_urls_as_text(dataset: list[manifest_mod.Source]) -> None:
    dataset[0]["terms_url"] = "https://[invalid"
    assert "omitted: not an HTTP URL" in report(dataset)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://user:secret@example.org/data?key=hidden#hidden", "http://example.org/data"),
        ("https://user:secret@example.org/data?key=hidden#hidden", "https://example.org/data"),
        ("https://user:sec@ret@example.org/data?key=hidden#hidden", "https://example.org/data"),
        ("ftp://user:secret@example.org/data", "omitted: not an HTTP URL"),
        ("https:/missing-host", "omitted: not an HTTP URL"),
        ("", "not stated in sources.toml"),  # nothing was hidden, so the report must not say it was
    ],
)
def test_report_keeps_only_the_public_part_of_a_url(
    dataset: list[manifest_mod.Source], url: str, expected: str
) -> None:
    dataset[0]["terms_url"] = url
    page = report(dataset)
    assert f"<dt>Terms URL</dt><dd>{expected}</dd>" in page
    assert "secret" not in page
    assert "hidden" not in page


def test_report_displays_the_built_metadata_and_security_policy(dataset: list[manifest_mod.Source]) -> None:
    package = json.loads(Path("data/processed/datapackage.json").read_text(encoding="utf-8"))
    resource = package["resources"][0]
    page = report(dataset)
    expected = {
        "File": "table.csv",
        "Size in bytes": resource["bytes"],
        "SHA-256 of the output": resource["hash"],
        "Fetched at": resource["sourceledger"]["retrieved_at"],
        "Vintage": "2023",
        "SHA-256 of the downloaded file": hashlib.sha256(b"year,value\n2023,1.5\n").hexdigest(),
        "Licence": "LicenseRef-example",
        "Terms": "Example terms",
        "Raw redistribution allowed": "no",
    }
    for key, value in expected.items():
        assert f"<dt>{key}</dt><dd>{value}</dd>" in page
    assert "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'" in page


@pytest.mark.parametrize("key", ["url", "vintage"])
def test_report_flags_manifest_changes(dataset: list[manifest_mod.Source], key: str) -> None:
    dataset[0][key] = "https://example.org/annual.csv" if key == "url" else "2024"
    page = report(dataset)
    assert "manifest differs from the last build" in page
    assert "Integrity checks passed" not in page


def test_report_says_not_stated_for_what_a_damaged_package_leaves_out(dataset: list[manifest_mod.Source]) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    for key in ("bytes", "hash"):
        del package["resources"][0][key]
    for key in ("raw_sha256", "vintage"):
        del package["resources"][0]["sourceledger"][key]
    path.write_text(json.dumps(package), encoding="utf-8")
    page = report(dataset)
    for label in ("Size in bytes", "SHA-256 of the output", "SHA-256 of the downloaded file", "Vintage"):
        assert f"<dt>{label}</dt><dd>not stated</dd>" in page


@pytest.mark.parametrize("value", [["yes"], {"a": 1}, 1])
def test_report_says_not_stated_for_a_redistribute_that_is_not_true_or_false(
    dataset: list[manifest_mod.Source], value: object
) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["sourceledger"]["redistribute"] = value
    path.write_text(json.dumps(package), encoding="utf-8")
    assert "<dt>Raw redistribution allowed</dt><dd>not stated in sources.toml</dd>" in report(dataset)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sources", None),
        ("sources", []),
        ("sources", [None]),
        ("sourceledger", []),
        ("sourceledger", {"parameters": "x"}),
    ],
)
def test_report_handles_damaged_provenance(dataset: list[manifest_mod.Source], key: str, value: object) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0][key] = value
    path.write_text(json.dumps(package), encoding="utf-8")
    page = report(dataset)
    assert "Integrity problems" in page


def test_report_cli_writes_one_complete_document(
    dataset: list[manifest_mod.Source], capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = "".join(
        "[[source]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in source.items())
        for source in dataset
    )
    Path("sources.toml").write_text(manifest, encoding="utf-8")
    capsys.readouterr()
    assert main(["report"]) == 0
    assert capsys.readouterr().out == report(dataset)


def test_report_keeps_source_urls_and_names(dataset: list[manifest_mod.Source]) -> None:
    dataset[0].update(url="https://example.org/data", name="Example observations")
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["sourceledger"]["final_url"] = "https://example.org/final?token=secret"
    path.write_text(json.dumps(package), encoding="utf-8")
    page = report(dataset)
    assert '<h4 id="source-0">Example observations</h4>' in page
    assert '<h4 id="source-1">document</h4>' in page
    assert "<dt>URL</dt><dd>https://example.org/data</dd>" in page
    assert "<dt>Served from</dt><dd>https://example.org/final</dd>" in page


def test_report_accepts_a_source_without_a_declared_vintage(dataset: list[manifest_mod.Source]) -> None:
    del dataset[0]["vintage"]
    fetcher_mod.fetch(dataset)
    builder_mod.build(dataset)
    assert "Integrity checks passed" in report(dataset)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.text())
def test_report_keeps_arbitrary_metadata_out_of_html_elements(dataset: list[manifest_mod.Source], name: str) -> None:
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
        "h4",
        "div",
    }
    assert all(link.startswith("#") for link in dom.links)


def test_report_does_not_echo_credentials_from_an_invalid_file_url(dataset: list[manifest_mod.Source]) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["path"] = "https://user:secret@example.org/data?token=PRIVATE"
    path.write_text(json.dumps(package), encoding="utf-8")
    try:
        text = report(dataset)
    except ValueError as error:
        text = str(error)
    assert "secret" not in text
    assert "PRIVATE" not in text


@pytest.mark.parametrize(
    "path_value",
    [
        None,
        "../outside.csv",
        "/outside.csv",
        "..\\outside.csv",
        "C:\\x.csv",
        "\\\\srv\\share\\x.csv",
        "\\x.csv",
        "C:x.csv",
    ],
)
def test_report_refuses_invalid_resource_paths(dataset: list[manifest_mod.Source], path_value: object) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["path"] = path_value
    path.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(ValueError, match="relative files"):
        report(dataset)


@pytest.mark.parametrize("change", ["omit one", "omit all", "unimplement", "duplicate"])
def test_report_flags_incomplete_or_obsolete_packages(dataset: list[manifest_mod.Source], change: str) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    if change == "omit one":
        package["resources"].pop()
    elif change == "omit all":
        package["resources"] = []
    elif change == "unimplement":
        dataset[0]["status"] = "unimplemented"
    else:
        package["resources"].append(package["resources"][0])
    path.write_text(json.dumps(package), encoding="utf-8")
    page = report(dataset)
    assert "Integrity checks passed" not in page
    assert "not the implemented sources" in page
    assert "re-run build" in page


def test_report_accepts_a_genuinely_empty_dataset(tmp_path: Path) -> None:
    (tmp_path / "datapackage.json").write_text('{"resources": []}')
    assert "Integrity checks passed" in report([], processed=tmp_path, log=tmp_path / "missing.jsonl")


def test_report_says_what_it_does_not_know_about_times(dataset: list[manifest_mod.Source]) -> None:
    """A stamp without a fetch time, or a recorded file without a download time, says "not stated"."""
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    del package["resources"][0]["sourceledger"]["retrieved_at"]
    package["resources"][1]["sourceledger"].update(retrieved_at="", recorded_at="2026-11-05T09:30:00Z")
    path.write_text(json.dumps(package), encoding="utf-8")
    page = report(dataset)
    assert "<dt>Fetched at</dt><dd>not stated</dd>" in page
    assert "<dt>Downloaded by another tool at</dt><dd>not stated by that tool</dd>" in page
    assert "<dt>Recorded at</dt><dd>2026-11-05T09:30:00Z</dd>" in page


def test_report_names_a_resource_without_a_name(dataset: list[manifest_mod.Source]) -> None:
    """A damaged package still gets a report, with the nameless output called "unnamed"."""
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    del package["resources"][0]["name"]
    path.write_text(json.dumps(package), encoding="utf-8")
    page = report(dataset)
    assert ">unnamed</h" in page
    assert "unnamed: no fetch record in the log" in page


def test_report_says_when_the_manifest_or_stamp_gives_no_url(dataset: list[manifest_mod.Source]) -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["sourceledger"].pop("final_url", None)
    path.write_text(json.dumps(package), encoding="utf-8")
    del dataset[0]["url"]
    page = report(dataset)
    assert "<dt>URL</dt><dd>not stated in sources.toml</dd>" in page
    # Both outputs: the second one never had a final_url, so only a count tells them apart.
    assert page.count("<dt>Served from</dt><dd>the URL above</dd>") == 2


@pytest.mark.usefixtures("dataset")
def test_report_lists_each_integrity_problem_in_one_failed_block() -> None:
    Path("data/fetches.jsonl").unlink()
    page = report([])
    block = page[page.index('<div class="result failed">') : page.index('<h2 id="outputs">')]
    assert block.startswith('<div class="result failed"><h2>Integrity problems</h2><ul><li>')
    assert block.endswith("</li></ul></div>")
    assert block.count("</li><li>") >= 2


@pytest.mark.usefixtures("dataset")
def test_report_names_the_rule_a_resource_path_breaks() -> None:
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["path"] = "../escape.csv"
    path.write_text(json.dumps(package), encoding="utf-8")
    message = r"^datapackage\.json resource paths must be relative files inside the dataset$"
    with pytest.raises(ValueError, match=message):
        report([])


def test_validate_reports_an_output_with_no_rows(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A source that builds to nothing is a problem, not a quietly empty publication."""
    upstream.write_text("year,value\n", encoding="utf-8", newline="")
    assert _run("fetch", "build", "validate") == [0, 0, 1]
    assert "no rows" in capsys.readouterr().out


@pytest.mark.usefixtures("upstream")
def test_validate_rejects_an_unknown_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped status is named as such, not reported as a missing file."""
    manifest = tmp_path / "sources.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace('"unimplemented"', '"unimplmented"'), encoding="utf-8", newline="")
    assert _run("validate") == [1]
    assert "status must be" in capsys.readouterr().out


@pytest.mark.usefixtures("upstream")
def test_validate_reports_missing_output_as_a_problem(capsys: pytest.CaptureFixture[str]) -> None:
    """Validate lists a source it cannot check instead of stopping at it."""
    _run("fetch")
    assert _run("validate") == [1]
    out, err = capsys.readouterr()
    assert err == ""
    assert "2 problems" in out
    assert "data/processed/example.csv is missing" in out
    assert "datapackage.json is missing; re-run build" in out


@pytest.mark.usefixtures("upstream")
def test_validate_rehashes_the_raw_file(capsys: pytest.CaptureFixture[str]) -> None:
    """A raw file edited after the build fails validation."""
    _run("fetch", "build")
    RAW.write_text("edited\n", encoding="utf-8", newline="")
    assert _run("validate") == [1]
    assert "changed since it was fetched" in capsys.readouterr().out


def test_validate_rereads_the_published_output(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Output built from an earlier fetch fails once the source is fetched again with new bytes."""
    _run("fetch", "build")
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    _run("fetch")
    assert _run("validate") == [1]
    assert "re-run build" in capsys.readouterr().out


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
    assert "example.csv: no source" in capsys.readouterr().out


@pytest.mark.usefixtures("upstream")
def test_validate_rejects_output_from_an_unimplemented_source(capsys: pytest.CaptureFixture[str]) -> None:
    """An `unimplemented` source is absent from the output by design, so output from one is an error."""
    _run("fetch", "build")
    OUT.with_name("later.csv").write_text("year\n2020\n", encoding="utf-8", newline="")
    assert _run("validate") == [1]
    assert "later: status is unimplemented" in capsys.readouterr().out


def test_validate_requires_reuse_terms(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every implemented source states its publisher's terms before its data is used."""
    _manifest(tmp_path, upstream.as_uri(), terms="")
    _run("fetch", "build")
    assert _run("validate") == [1]
    assert "example: no terms" in capsys.readouterr().out


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
    assert expected in capsys.readouterr().out


@pytest.mark.usefixtures("upstream")
def test_validate_compares_every_provenance_column(capsys: pytest.CaptureFixture[str]) -> None:
    """Output whose retrieval time or vintage no longer matches the record is stale."""
    _run("fetch", "build")
    OUT.write_text(OUT.read_text(encoding="utf-8").replace("Z,", "Z0,"), encoding="utf-8")
    assert _run("validate") == [1]
    assert "re-run build" in capsys.readouterr().out


@pytest.mark.usefixtures("upstream")
def test_a_status_that_never_recovers_is_not_retried(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """501 Not Implemented will say the same thing in 20 seconds."""
    calls = _serve(monkeypatch, _http_error(501))
    assert _run("fetch") == [3]
    assert (len(calls), sleeps) == (1, [])


@pytest.mark.usefixtures("upstream")
def test_validate_catches_a_stale_data_package(capsys: pytest.CaptureFixture[str]) -> None:
    """An output edited after its build no longer matches the hash the package gives it."""
    _run("fetch", "build")
    OUT.write_text(OUT.read_text(encoding="utf-8").replace(",1.5", ",9.9"), encoding="utf-8")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert "describes an earlier build" in capsys.readouterr().out


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
    Path("data/processed/datapackage.json").write_text(damage, encoding="utf-8", newline="")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert expected in capsys.readouterr().out


@pytest.mark.usefixtures("upstream")
def test_status_lists_every_source_without_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fetched, never-fetched and unimplemented sources each get a line from the manifest and the log."""
    text = (tmp_path / "sources.toml").read_text(encoding="utf-8")
    fresh = '[[source]]\nid = "fresh"\nstatus = "implemented"\nurl = "https://example.invalid/f"\nvintage = "2025"\n'
    (tmp_path / "sources.toml").write_text(text + "\n" + fresh, encoding="utf-8", newline="")
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


@pytest.mark.usefixtures("upstream")
def test_validate_only_skips_the_check_for_files_no_source_owns(capsys: pytest.CaptureFixture[str]) -> None:
    """An orphan output fails a full validate, but not one narrowed to a source."""
    _run("fetch", "build")
    Path("data/processed/old.csv").write_text("x\n", encoding="utf-8", newline="")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert "data/processed/old.csv: no source 'old' in sources.toml; delete it" in capsys.readouterr().out
    assert main(["validate", "--only", "example"]) == 0


@pytest.mark.usefixtures("upstream")
def test_validate_passes_the_temp_file_of_a_killed_build() -> None:
    """A killed build leaves `.NAME.<random>.part`: not published data, and build takes no lock to sweep it."""
    _run("fetch", "build")
    Path("data/processed/.example.csv.k3x9.part").write_text("year,va", encoding="utf-8", newline="")
    assert report_mod.validate(manifest_mod.load_manifest()) == []


def test_only_runs_in_manifest_order(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The order of --only flags does not reorder the sources; a lone string is refused, not split into letters."""
    _two_sources(tmp_path, upstream.as_uri(), upstream.as_uri())
    assert main(["fetch", "--only", "b", "--only", "a"]) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["a", "b"]
    with pytest.raises(TypeError, match="not the string 'ab'"):
        report_mod.validate(manifest_mod.load_manifest(), only="ab")


@pytest.mark.usefixtures("upstream")
def test_validate_only_reports_a_package_that_does_not_list_the_source(capsys: pytest.CaptureFixture[str]) -> None:
    """The package checks narrow to the named source too: missing, or without its entry."""
    _run("fetch", "build")
    package = Path("data/processed/datapackage.json")
    package.write_text('{"resources": []}', encoding="utf-8", newline="")
    capsys.readouterr()
    assert main(["validate", "--only", "example"]) == 1
    out, err = capsys.readouterr()
    assert "datapackage.json does not list example; re-run build" in out
    assert err == ""
    package.unlink()
    assert main(["validate", "--only", "example"]) == 1
    assert "datapackage.json is missing" in capsys.readouterr().out
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
    assert err == ""
    size_line, hash_line, count = out.splitlines()
    assert count == "2 problems"
    assert (
        size_line == f"data/processed/example.csv is {OUT.stat().st_size:,} bytes, not the {built:,} the package lists"
    )
    assert hash_line.startswith("data/processed/example.csv hashes to sha256:")


# The hash of "x\n", the file the verify tests list.
_X_HASH = f"sha256:{hashlib.sha256(b'x\n').hexdigest()}"


# Paths Windows reads as absolute or climbing out, which POSIX reads as plain names.
WINDOWS_ESCAPES = ["..\\a.csv", "C:\\x.csv", "\\\\srv\\share\\x.csv", "\\x.csv", "C:x.csv"]


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        ([{"path": "gone.csv", "hash": "sha256:00"}], ["d: resource 'gone.csv' is missing"]),
        ([{"path": "a.csv"}], ["d/a.csv: the package lists no sha256 hash to check it against"]),
        ([{"path": "../a.csv"}], ["d/datapackage.json: resource path '../a.csv' is not a file in d"]),
        ([{"path": "/etc/hostname"}], ["d/datapackage.json: resource path '/etc/hostname' is not a file in d"]),
        *[
            ([{"path": name}], [f"d/datapackage.json: resource path {name!r} is not a file in d"])
            for name in WINDOWS_ESCAPES
        ],
        ([{"path": "gone\x1b[2J.csv", "hash": "sha256:00"}], ["d: resource 'gone\\x1b[2J.csv' is missing"]),
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
    Path("d/a.csv").write_text("x\n", encoding="utf-8", newline="")
    Path("outside.csv").write_text("x\n", encoding="utf-8", newline="")
    _symlink(Path("d/out.csv"), Path("../outside.csv"))
    Path("d/datapackage.json").write_text(json.dumps({"resources": resources}), encoding="utf-8", newline="")
    problems = report_mod.verify(Path("d"))
    assert len(problems) == len(expected)
    assert all(p.startswith(e) for p, e in zip(problems, expected, strict=True))


def test_verify_needs_a_readable_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder without a package, or with a broken one, is one line and a failed exit."""
    monkeypatch.chdir(tmp_path)
    assert main(["verify", "."]) == 3
    assert capsys.readouterr().err == (
        "error: datapackage.json is missing\nhelp: pass the folder that holds the dataset's datapackage.json\n"
    )
    Path("datapackage.json").write_text("not json", encoding="utf-8", newline="")
    assert main(["verify", "."]) == 3
    assert capsys.readouterr().err.startswith("error: Expecting value")


@pytest.mark.usefixtures("upstream")
def test_a_retry_after_past_the_budget_fails_with_the_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the wait would outlast the fetch, the run stops with the server's own error."""
    monkeypatch.setattr(fetcher_mod, "_BUDGET_S", 60)
    monkeypatch.setattr(ledger, "_BUDGET_S", 60)
    _serve(monkeypatch, _http_error(503, Retry_After="90"))
    assert _run("fetch") == [3]
    assert "HTTP Error 503" in capsys.readouterr().err


def test_status_of_no_sources_is_no_lines(tmp_path: Path) -> None:
    assert report_mod.status([], log=tmp_path / "none.jsonl") == []


def test_terms_need_both_the_words_and_the_url() -> None:
    source = {"id": "a", "terms": "CC0", "license": "CC0-1.0", "redistribute": True}
    assert report_mod._terms_problems(source) == ["a: no terms or terms_url; record the publisher's reuse terms"]  # noqa: SLF001


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
    problems = report_mod.validate(sources)
    assert any(p.startswith("x: no terms") for p in problems)


def test_attribution_leaves_missing_terms_empty() -> None:
    source = {"id": "a", "status": "implemented", "url": "https://example.org/a.csv"}
    assert report_mod.attribution([source])[2] == "| a |  |  |  | ? |"


def test_verify_shows_a_large_listed_size_with_separators(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("x\n", encoding="utf-8", newline="")
    package = {"resources": [{"path": "a.csv", "bytes": 1234, "hash": _X_HASH}]}
    (tmp_path / "datapackage.json").write_text(json.dumps(package), encoding="utf-8", newline="")
    assert report_mod.verify(tmp_path) == [f"{tmp_path.as_posix()}/a.csv is 2 bytes, not the 1,234 the package lists"]
    (tmp_path / "datapackage.json").write_text("[]", encoding="utf-8", newline="")
    assert report_mod.verify(tmp_path) == [f"{tmp_path.as_posix()}/datapackage.json has no list of resources"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold control characters")
def test_verify_shows_a_control_character_in_a_name_as_an_escape(tmp_path: Path) -> None:
    """A received folder names its own files; an ESC in one must not reach the terminal raw."""
    name = "a\x1b[2J.csv"
    (tmp_path / name).write_text("x\n", encoding="utf-8", newline="")
    package = {"resources": [{"path": name, "bytes": 3, "hash": _X_HASH}]}
    (tmp_path / "datapackage.json").write_text(json.dumps(package), encoding="utf-8", newline="")
    assert (
        report_mod.verify(tmp_path)[0] == f"{tmp_path.as_posix()}/a\\x1b[2J.csv is 2 bytes, not the 3 the package lists"
    )


def test_verify_names_the_hash_it_found(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("y\n", encoding="utf-8", newline="")
    package = {"resources": [{"path": "a.csv", "hash": _X_HASH}]}
    (tmp_path / "datapackage.json").write_text(json.dumps(package), encoding="utf-8", newline="")
    digest = hashlib.sha256(b"y\n").hexdigest()
    assert report_mod.verify(tmp_path) == [
        f"{tmp_path.as_posix()}/a.csv hashes to sha256:{digest[:12]}, not the hash the package lists"
    ]


def test_validate_reads_an_output_one_row_at_a_time(big: int) -> None:
    sources = manifest_mod.load_manifest()
    builder_mod.build(sources)
    report_mod.validate(sources)  # unmeasured, as in the build test below
    assert _peak(lambda: report_mod.validate(sources)) < big // 4
    assert report_mod.validate(sources) == []


def test_build_streams_a_csv_source_one_row_at_a_time(big: int) -> None:
    sources = manifest_mod.load_manifest()
    # Built once unmeasured: under coverage, the first run of a code path allocates the tracer's
    # own records, about 150 kB here, which a fresh container counted against the limit.
    builder_mod.build(sources)
    assert _peak(lambda: builder_mod.build(sources)) < big // 4
    assert report_mod.validate(sources) == []


@pytest.mark.parametrize("rows", [10_000, 100_000])
def test_build_streams_a_gzipped_csv_source(rows: int, upstream: Path) -> None:
    body = "year,value,note\n" + "".join(f"{i},{i * 0.5},row {i}\n" for i in range(rows))
    upstream.write_bytes(gzip.compress(body.encode()))
    sources = manifest_mod.load_manifest()
    fetcher_mod.fetch(sources)
    builder_mod.build(sources)  # unmeasured, as in the CSV test above
    # gzip has fixed decompression buffers in addition to the CSV reader's row-sized memory.
    assert _peak(lambda: builder_mod.build(sources)) < len(body) // 4 + 1024 * 1024
    assert report_mod.validate(sources) == []


@pytest.mark.parametrize("suffix", [".bin", ".csv", ""])
@pytest.mark.parametrize("redistribute", [True, False])
def test_file_sources_build_without_a_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    capsys: pytest.CaptureFixture[str],
    *,
    redistribute: bool,
) -> None:
    payload = b"\x00\xff\x80binary\n"
    upstream = tmp_path / f"input{suffix}"
    upstream.write_bytes(payload)
    monkeypatch.chdir(tmp_path)
    terms = TERMS.replace("redistribute = true", f"redistribute = {str(redistribute).lower()}")
    _manifest(tmp_path, upstream.as_uri(), terms=f'{terms}\nformat = "file"')
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    out = Path("data/processed") / f"example{suffix}"
    assert out.read_bytes() == payload
    if suffix != ".csv":
        assert not OUT.exists()
    assert f"{len(payload)} bytes -> {out.as_posix()}" in capsys.readouterr().out
    package = json.loads((out.parent / builder_mod.DATAPACKAGE).read_text())
    (resource,) = package["resources"]
    assert "type" not in resource
    assert resource["path"] == out.name
    assert resource["sourceledger"]["redistribute"] is redistribute
    assert "schema" not in resource
    assert "encoding" not in resource
    assert resource["hash"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert report_mod.verify(out.parent) == []
    out.write_bytes(b"edited")
    assert any("earlier build" in p for p in report_mod.validate(manifest_mod.load_manifest()))
    resource["hash"] = "sha256:" + hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / builder_mod.DATAPACKAGE).write_text(json.dumps(package))
    assert any("differs from the fetched file" in p for p in report_mod.validate(manifest_mod.load_manifest()))
    out.unlink()
    assert any("missing" in p for p in report_mod.validate(manifest_mod.load_manifest()))


def test_a_two_chunk_file_uses_its_source_timeout(publisher: str, tmp_path: Path) -> None:
    _manifest(tmp_path, f"{publisher}/data.bin", terms=f'{TERMS}\nformat = "file"\ntimeout_s = 1')
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert Path("data/processed/example.bin").read_bytes() == b"\x00\xfffirst\x80second"
    assert not OUT.exists()
    assert report_mod.verify(Path("data/processed")) == []


def test_partial_build_keeps_file_resources(upstream: Path, tmp_path: Path) -> None:
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nformat = "file"')
    assert _run("fetch", "build") == [0, 0]
    sources = manifest_mod.load_manifest()
    assert builder_mod.build(sources, only=[]) == []
    assert report_mod.validate(sources) == []
    assert report_mod.verify(Path("data/processed")) == []
    sources[0]["status"] = "unimplemented"
    assert any("unimplemented" in problem for problem in report_mod.validate(sources))
    assert any("no source" in problem for problem in report_mod.validate([]))


@pytest.mark.parametrize("field", ["vintage", "url", "redistribute"])
def test_validate_requires_current_file_metadata(upstream: Path, field: str) -> None:
    sources = manifest_mod.load_manifest()
    source = sources[0]
    source.update(format="file", vintage="2024")
    fetcher_mod.fetch(sources)
    builder_mod.build(sources)
    assert report_mod.validate(sources) == []
    original = OUT.read_bytes()
    if field == "url":
        mirror = upstream.with_name("mirror.csv")
        mirror.write_bytes(upstream.read_bytes())
        source["url"] = mirror.as_uri()
    elif field == "vintage":
        source["vintage"] = "2025"
    else:
        source["redistribute"] = False
    if field != "redistribute":
        fetcher_mod.fetch(sources)
    assert OUT.read_bytes() == original
    assert any("re-run build" in problem for problem in report_mod.validate(sources))
    assert any("re-run build" in problem for problem in report_mod.validate(sources, only=["example"]))
    builder_mod.build(sources)
    assert report_mod.validate(sources) == []
    assert OUT.read_bytes() == original


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize("status", ["implemented", "unimplemented"])
def test_unselected_reserved_file_names_do_not_block_validation(status: str) -> None:
    sources = manifest_mod.load_manifest()
    sources.append({"id": "datapackage", "status": status, "format": "file", "url": "https://example.org/a.json"})
    fetcher_mod.fetch(sources, only=["example"])
    builder_mod.build(sources, only=["example"])
    assert report_mod.validate(sources, only=["example"]) == []
    if status == "unimplemented":
        assert report_mod.validate(sources) == []


@pytest.mark.usefixtures("upstream")
def test_json_validation_preserves_problems_and_selection(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("fetch", "build") == [0, 0]
    capsys.readouterr()
    assert main(["validate", "--json", "--only", "example"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "stage": "validate",
        "errors": [],
        "problems": [],
    }
    OUT.write_text("changed", encoding="utf-8")
    expected = report_mod.validate(manifest_mod.load_manifest())
    assert main(["validate", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "stage": "validate",
        "errors": [],
        "problems": expected,
    }


@pytest.mark.usefixtures("upstream")
def test_status_uses_a_custom_log_and_marks_invalid_reuse_terms(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("fetch") == [0]
    capsys.readouterr()
    record = json.loads(LOG.read_text(encoding="utf-8"))
    other = Path("other.jsonl")
    LOG.rename(other)
    lines = report_mod.status(load_manifest(), log=other)
    assert record["sha256"][:12] in lines[0]
    assert record["retrieved_at"] in lines[0]
    manifest = Path("sources.toml")
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("redistribute = true", 'redistribute = "unknown"'),
        encoding="utf-8",
    )
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["redistribute"] is None


@pytest.mark.parametrize("count", [1, 10])
def test_validate_hashes_each_file_output_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    monkeypatch.chdir(tmp_path)
    upstream = tmp_path / "input.bin"
    upstream.write_bytes(b"binary fixture")
    sources = [
        {
            "id": f"source-{number}",
            "status": "implemented",
            "url": upstream.as_uri(),
            "format": "file",
            "terms": "CC0-1.0",
            "terms_url": "https://example.org/terms",
            "license": "CC0-1.0",
            "redistribute": True,
        }
        for number in range(count)
    ]
    fetcher_mod.fetch(sources)
    builder_mod.build(sources)
    hashed = []
    original = ledger._digest  # noqa: SLF001

    def digest(path: Path) -> str:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(report_mod, "_digest", digest)
    monkeypatch.setattr(ledger, "_digest", digest)
    monkeypatch.setattr(builder_mod, "_digest", digest)
    assert report_mod.validate(sources) == []
    assert len([p for p in hashed if p.parent == Path("data/processed")]) == count


def test_validate_names_the_package_that_describes_an_earlier_file_build(upstream: Path, tmp_path: Path) -> None:
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nformat = "file"')
    assert _run("fetch", "build") == [0, 0]
    package = Path("data/processed/datapackage.json")
    described = json.loads(package.read_text(encoding="utf-8"))
    described["resources"][0]["bytes"] += 1
    package.write_text(json.dumps(described), encoding="utf-8", newline="")
    assert report_mod.validate(manifest_mod.load_manifest()) == [
        "data/processed/datapackage.json describes an earlier build of data/processed/example.csv; re-run build"
    ]


@pytest.mark.usefixtures("upstream")
def test_validate_names_the_resources_a_package_lists_in_place_of_the_sources() -> None:
    _run("fetch", "build")
    Path("data/processed/datapackage.json").write_text('{"resources": [{"name": "zzz"}]}', encoding="utf-8", newline="")
    assert (
        "data/processed/datapackage.json lists ['zzz'], not the implemented sources ['example']; re-run build"
        in report_mod.validate(manifest_mod.load_manifest())
    )


@pytest.mark.parametrize(
    ("text", "stamp", "scanned"),
    [
        ("", "example", (False, False)),
        ("source_id\n\nexample\n", "example", (True, False)),
        ('source_id\r\n"a\r\nb"\r\n', "a\r\nb", (True, False)),
        ("other\nexample\n", "example", (True, True)),
        ("other,source_id\nx\n", "example", (True, True)),
    ],
)
def test_scan_reports_rows_and_a_stale_stamp(tmp_path: Path, text: str, stamp: str, scanned: tuple[bool, bool]) -> None:
    """An empty file, blank lines, a line break inside the stamp, a missing column and a short row."""
    out = tmp_path / "out.csv"
    out.write_bytes(text.encode())
    assert report_mod._scan(out, {"source_id": stamp}) == scanned  # noqa: SLF001


class _AnHourLater(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(time.time() + 3600, tz=tz)


@pytest.mark.usefixtures("upstream")
def test_report_passes_a_build_after_a_refetch_of_the_same_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with the bytes the log has (a file:// URL, or a server without ETag) keeps the logged time."""
    assert _run("fetch", "build") == [0, 0]
    monkeypatch.setattr(fetcher_mod, "datetime", _AnHourLater)
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    logged = json.loads(LOG.read_text(encoding="utf-8"))
    sidecar = json.loads(RAW.with_name(RAW.name + ".fetch.json").read_text(encoding="utf-8"))
    assert sidecar["retrieved_at"] == logged["retrieved_at"]
    page = report(load_manifest())
    assert "Integrity checks passed" in page
    assert "built from an earlier fetch" not in page
