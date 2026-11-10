"""The release stage: what reaches the folder, what never does, and the checks on it."""

import errno
import hashlib
import importlib
import importlib.util
import json
import logging
import subprocess
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import pytest

import sourceledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main

releaser = importlib.import_module("sourceledger.releaser")

# A column name with a slash, which a Croissant field id must escape.
TABLE = b"year,ducks/ha\n2020,1.5\n2021,2.5\n"
# A restricted table, for the sources whose rows may not be republished either.
SECRET_TABLE = b"year,nests\n2020,7\n"
# The restricted file: bytes that appear in no other output, so a search for them means something.
SECRET = b"\x00restricted-payload\xff\xfe"
AUTHOR = {"@type": "Person", "@id": "https://orcid.org/0000-0002-1825-0097", "givenName": "Ada", "familyName": "Duck"}
CODEMETA: dict[str, object] = {
    "@context": "https://w3id.org/codemeta/3.0",
    "@type": "SoftwareSourceCode",
    "name": "ducks-of-the-dunes",
    "description": "Duck counts on a dune transect",
    "author": [AUTHOR],
    "license": "https://spdx.org/licenses/CC-BY-4.0",
    "version": "1.0.0",
    "datePublished": "2026-11-10",
    "identifier": "https://doi.org/10.5281/zenodo.1234567",
    "url": "https://example.org/ducks",
}
OUT = Path("release")


def _source(sid: str, url: str, *, redistribute: bool, extra: str = "") -> str:
    return (
        f'[[source]]\nid = "{sid}"\nstatus = "implemented"\nurl = "{url}"\n{extra}'
        f'terms = "terms of {sid}"\nterms_url = "https://example.org/{sid}/terms"\n'
        f'license = "CC0-1.0"\nredistribute = {str(redistribute).lower()}\n\n'
    )


def _write_manifest(
    root: Path,
    *,
    secret_shared: bool = False,
    formats: str = "",
    open_file: str = "",
    secret_table: bool = False,
    head: str = "",
) -> None:
    """`open`, a table anyone may republish, and `secret`, a file whose terms forbid it.

    `open_file` makes `open` a file source of that upstream file instead, and `secret_table`
    makes `secret` a table.
    """
    up = root / "upstream"
    secret_url = (up / ("secret.csv" if secret_table else "secret.bin")).as_uri()
    open_url = (up / (open_file or "open.csv")).as_uri()
    text = head + (f"release_formats = [{formats}]\n\n" if formats else "")
    open_extra = 'name = "Open duck counts"\n' + ('format = "file"\n' if open_file else "")
    text += _source("open", open_url, redistribute=True, extra=open_extra)
    text += _source("secret", secret_url, redistribute=secret_shared, extra="" if secret_table else 'format = "file"\n')
    text += '[[source]]\nid = "later"\nstatus = "unimplemented"\n'
    (root / "sources.toml").write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fetched and built project with one shareable and one restricted source; the test runs in it."""
    up = tmp_path / "upstream"
    up.mkdir()
    (up / "open.csv").write_bytes(TABLE)
    (up / "secret.bin").write_bytes(SECRET)
    (up / "twin.bin").write_bytes(SECRET)
    (up / "secret.csv").write_bytes(SECRET_TABLE)
    (tmp_path / "codemeta.json").write_text(json.dumps(CODEMETA), encoding="utf-8")
    _write_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert [main(["fetch"]), main(["build"])] == [0, 0]
    return tmp_path


@pytest.fixture
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Each metadata validator replaced by a script, `pass` until the test calls the fixture with another.

    A stand-in prints its findings on stdout, separated by `|`.
    """

    def use(rocrate: str = "pass", croissant: str = "pass") -> None:
        for name, script in ((releaser._ROCRATE, rocrate), (releaser._CROISSANT, croissant)):  # noqa: SLF001
            monkeypatch.setitem(releaser._VALIDATORS, name, ("sourceledger", ("-c", script)))  # noqa: SLF001

    monkeypatch.setitem(releaser._FINDINGS, "sourceledger", lambda stdout, _: stdout.split("|") if stdout else [])  # noqa: SLF001
    use()
    return use


def _all_files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _release(*args: str) -> int:
    return main(["release", *args])


def _json(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


# The redistribute guarantee: decision 0006's two tests, then the hash pass on its own.


@pytest.mark.usefixtures("stand_ins")
def test_a_restricted_source_never_reaches_the_release(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With every format on, no file holds the restricted bytes, and only the RO-Crate names it, as excluded."""
    _write_manifest(project, formats='"ro-crate", "croissant"')
    assert _release(str(OUT), "--json") == 0
    files = _all_files(OUT)
    built = Path("data/processed/secret.bin").read_bytes()
    assert [name for name, data in files.items() if SECRET in data or built in data] == []
    report = _json(capsys)
    assert report["excluded"] == ["secret"]
    assert report["formats"] == {"ro-crate": "written", "croissant": "written"}
    assert sorted(files) == sorted(report["files"])
    assert set(files) == {
        "data/open.csv",
        "datapackage.json",
        "ro-crate-metadata.json",
        "croissant.json",
        "SHA256SUMS",
    }
    assert [name for name, data in files.items() if b"secret" in data] == ["ro-crate-metadata.json"]
    crate = json.loads(files["ro-crate-metadata.json"])
    naming = [entity for entity in crate["@graph"] if "secret" in json.dumps(entity)]
    assert {entity["@id"] for entity in naming} == {"./", "#source-secret", "#fetch-secret"}
    (source,) = [entity for entity in naming if entity["@id"] == "#source-secret"]
    assert source["description"].startswith("Excluded from this release")
    assert {part["@id"] for part in next(e for e in naming if e["@id"] == "./")["hasPart"]} == {"data/open.csv"}


@pytest.mark.usefixtures("stand_ins")
def test_flipping_redistribute_after_build_keeps_the_file_out(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Built while shareable, then marked restricted: the stale build is refused, and the rebuild leaves it out."""
    _write_manifest(project, secret_shared=True)
    assert main(["build"]) == 0
    _write_manifest(project, secret_shared=False)
    assert _release(str(OUT)) == 1
    assert "describes an earlier build of data/processed/secret.bin" in capsys.readouterr().err
    assert not OUT.exists()
    assert main(["build"]) == 0
    assert _release(str(OUT)) == 0
    assert [name for name, data in _all_files(OUT).items() if SECRET in data] == []


@pytest.mark.usefixtures("stand_ins")
def test_the_hash_pass_refuses_restricted_bytes_under_any_name(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shareable source with the same bytes as a restricted one would carry them out under its own name."""
    _write_manifest(project, open_file="twin.bin")
    Path("data/processed/open.csv").unlink()
    assert [main(["fetch"]), main(["build"])] == [0, 0]
    assert _release(str(OUT)) == 1
    err = capsys.readouterr().err
    assert err == "error: release/data/open.bin would hold the bytes of secret, whose redistribute is not true\n"
    assert not OUT.exists()
    assert [p.name for p in project.iterdir() if p.name.startswith(".")] == []


# What the default path writes, and how it reports.


def test_the_default_release_writes_data_the_package_and_sums(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without release_formats: no RO-Crate, no Croissant, each reported skipped, and no codemeta.json needed."""
    (project / "codemeta.json").unlink()
    assert _release(str(OUT)) == 0
    out = capsys.readouterr().out
    assert out.startswith("wrote release/data/open.csv\nwrote release/datapackage.json\nwrote release/SHA256SUMS\n")
    assert "skipped ro-crate: not in release_formats\nskipped croissant: not in release_formats\n" in out
    assert "excluded secret: redistribute is not true\n" in out
    assert out.endswith("checked SHA256SUMS with sourceledger " + version("sourceledger") + "\n")
    assert sorted(_all_files(OUT)) == ["SHA256SUMS", "data/open.csv", "datapackage.json"]
    # The format `sha256sum -b` writes, in path order, so `sha256sum -c SHA256SUMS` passes.
    expected = "".join(
        f"{hashlib.sha256((OUT / name).read_bytes()).hexdigest()} *{name}\n"
        for name in ("data/open.csv", "datapackage.json")
    )
    assert (OUT / "SHA256SUMS").read_bytes() == expected.encode()
    assert sourceledger.verify(OUT) == []
    package = json.loads((OUT / "datapackage.json").read_text(encoding="utf-8"))
    built = json.loads(Path("data/processed/datapackage.json").read_text(encoding="utf-8"))
    assert package == {**built, "resources": [{**built["resources"][0], "path": "data/open.csv"}]}


@pytest.mark.usefixtures("project")
def test_the_release_json_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert _release(str(OUT), "--json") == 0
    report = _json(capsys)
    assert report == {
        "schema_version": 1,
        "stage": "release",
        "files": ["data/open.csv", "datapackage.json", "SHA256SUMS"],
        "excluded": ["secret"],
        "formats": {"ro-crate": "skipped", "croissant": "skipped"},
        "validators": [{"file": "SHA256SUMS", "name": "sourceledger", "version": report["validators"][0]["version"]}],
        "errors": [],
    }


def test_a_dry_run_writes_nothing_and_says_what_it_would(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _all_files(project)
    assert _release(str(OUT), "--dry-run") == 0
    assert capsys.readouterr().out.startswith(
        "would write release/data/open.csv\nwould write release/datapackage.json\n"
    )
    assert _release(str(OUT), "--dry-run", "--json") == 0
    report = _json(capsys)
    assert report["dry_run"] is True
    assert report["validators"] == []
    assert _all_files(project) == before


def test_an_empty_out_dir_is_used_and_a_full_one_refused(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    OUT.mkdir()
    assert _release(str(OUT)) == 0
    capsys.readouterr()
    assert _release(str(OUT)) == 3
    assert capsys.readouterr().err == (
        "error: release already exists; a release goes into a new folder\n"
        "help: pass a new OUT_DIR, or move that folder away\n"
    )
    (project / "file").write_text("x", encoding="utf-8")
    assert _release("file") == 3
    assert project.joinpath("file").read_text(encoding="utf-8") == "x"


def test_out_dir_may_be_in_a_new_folder(project: Path) -> None:
    assert _release("releases/2026/v1") == 0
    assert (project / "releases" / "2026" / "v1" / "SHA256SUMS").is_file()


@pytest.mark.usefixtures("project")
def test_a_folder_a_killed_release_left_is_refused_and_kept(capsys: pytest.CaptureFixture[str]) -> None:
    left = Path(".release.part")
    left.mkdir()
    (left / "note.txt").write_text("mine", encoding="utf-8")
    assert _release(str(OUT)) == 3
    assert capsys.readouterr().err == (
        "error: .release.part is left from a release that did not finish; delete it\n"
        "help: pass a new OUT_DIR, or move that folder away\n"
    )
    assert (left / "note.txt").read_text(encoding="utf-8") == "mine"
    assert not OUT.exists()


def test_the_command_releases_in_the_projects_output_format(project: Path) -> None:
    pytest.importorskip("pyarrow")
    _write_manifest(project, head='output_format = "parquet"\n\n')
    Path("data/processed/open.csv").unlink()
    assert main(["build"]) == 0
    assert _release(str(OUT)) == 0
    assert sorted(_all_files(OUT)) == ["SHA256SUMS", "data/open.parquet", "datapackage.json"]


def test_release_reads_the_folders_it_is_given(project: Path) -> None:
    """Other raw and processed folders, and Parquet outputs, are read where and as the caller says."""
    pytest.importorskip("pyarrow")
    _write_manifest(project, head='output_format = "parquet"\n\n')
    Path("data/processed/open.csv").unlink()
    assert main(["build"]) == 0
    moved = project / "elsewhere"
    Path("data").rename(moved)
    result = sourceledger.release(
        manifest_mod.load_manifest(), OUT, raw=moved / "raw", processed=moved / "processed", output_format="parquet"
    )
    assert result["files"] == ["data/open.parquet", "datapackage.json", "SHA256SUMS"]
    assert (OUT / "data/open.parquet").read_bytes() == (moved / "processed/open.parquet").read_bytes()


@pytest.mark.usefixtures("project")
def test_release_refuses_a_build_that_does_not_validate(capsys: pytest.CaptureFixture[str]) -> None:
    """Every problem validate finds is listed, and nothing is written."""
    Path("data/processed/open.csv").write_text("changed\n", encoding="utf-8")
    Path("data/processed/secret.bin").unlink()
    assert _release(str(OUT), "--json") == 1
    messages = [error["message"] for error in _json(capsys)["errors"]]
    assert len(messages) == 3
    assert any("secret.bin is missing" in m for m in messages)
    assert not OUT.exists()


@pytest.mark.usefixtures("project")
def test_release_warns_above_zenodos_file_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(releaser, "_ZENODO_FILES", 3)
    assert _release(str(OUT), "--dry-run") == 0
    assert caplog.messages == []
    monkeypatch.setattr(releaser, "_ZENODO_FILES", 2)
    with caplog.at_level(logging.WARNING):
        assert _release(str(OUT)) == 0
    assert caplog.messages == ["the release has 3 files; Zenodo takes at most 2, so zip them first"]
    assert sorted(_all_files(OUT)) == ["SHA256SUMS", "data/open.csv", "datapackage.json"]


def test_a_table_source_that_is_restricted_stays_out_too(project: Path) -> None:
    text = (project / "sources.toml").read_text(encoding="utf-8")
    (project / "sources.toml").write_text(text.replace("redistribute = true", "redistribute = false"), encoding="utf-8")
    assert main(["build"]) == 0
    result = sourceledger.release(manifest_mod.load_manifest(), OUT)
    assert result["excluded"] == ["open", "secret"]
    assert sorted(_all_files(OUT)) == ["SHA256SUMS", "datapackage.json"]
    assert json.loads((OUT / "datapackage.json").read_text(encoding="utf-8"))["resources"] == []


@pytest.mark.usefixtures("stand_ins")
def test_a_copy_of_a_restricted_tables_output_is_refused(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The hash pass knows a restricted table's built output, not only its raw file."""
    _write_manifest(project, secret_table=True)
    Path("data/processed/secret.bin").unlink()
    assert [main(["fetch"]), main(["build"])] == [0, 0]
    Path("upstream/copy.bin").write_bytes(Path("data/processed/secret.csv").read_bytes())
    _write_manifest(project, secret_table=True, open_file="copy.bin")
    Path("data/processed/open.csv").unlink()
    assert [main(["fetch"]), main(["build"])] == [0, 0]
    assert _release(str(OUT)) == 1
    assert capsys.readouterr().err == (
        "error: release/data/open.bin would hold the bytes of secret, whose redistribute is not true\n"
    )


def test_the_hash_pass_names_the_source_of_raw_and_built_bytes(project: Path) -> None:
    _write_manifest(project, secret_table=True)
    Path("data/processed/secret.bin").unlink()
    assert [main(["fetch"]), main(["build"])] == [0, 0]
    planted = project / "planted"
    planted.mkdir()
    (planted / "a").write_bytes(SECRET_TABLE)
    (planted / "b").write_bytes(Path("data/processed/secret.csv").read_bytes())
    (planted / "SHA256SUMS").write_bytes(b"listed\n")
    (_, secret, _) = manifest_mod.load_manifest()
    with pytest.raises(sourceledger.RestrictedSourceError) as refused:
        releaser._refuse_restricted(planted, OUT, [secret], Path("data/raw"), Path("data/processed"), "csv")  # noqa: SLF001
    assert refused.value.problems == [
        "release/a would hold the bytes of secret, whose redistribute is not true",
        "release/b would hold the bytes of secret, whose redistribute is not true",
    ]
    assert str(refused.value) == "; ".join(refused.value.problems)


@pytest.mark.parametrize("formats", ["ro-crate", ["zip"], {"ro-crate"}])
@pytest.mark.usefixtures("project")
def test_release_takes_a_list_of_known_formats(formats: object) -> None:
    with pytest.raises(ValueError, match="formats must be a list drawn from"):
        sourceledger.release(manifest_mod.load_manifest(), OUT, formats=formats)  # ty: ignore[invalid-argument-type]  # the wrong types under test


# The descriptive fields and the two optional files.


@pytest.mark.usefixtures("stand_ins")
def test_the_metadata_takes_the_codemeta_fields(project: Path) -> None:
    (project / "codemeta.json").write_text(json.dumps({**CODEMETA, "description": "Enten der Dünen"}), encoding="utf-8")
    sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["ro-crate", "croissant"])
    text = (OUT / "ro-crate-metadata.json").read_bytes()
    assert text.startswith(b'{\n  "@context": "https://w3id.org/ro/crate/1.3/context",\n  "@graph": [\n    {\n')
    assert text.endswith(b"\n  ]\n}\n")
    assert b"D\\u00fcnen" in text
    crate = json.loads(text)
    graph = {entity["@id"]: entity for entity in crate["@graph"]}
    assert graph["ro-crate-metadata.json"] == {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": "https://w3id.org/ro/crate/1.3"},
        "about": {"@id": "./"},
    }
    root = graph["./"]
    assert root["@type"] == "Dataset"
    assert root["description"] == "Enten der Dünen"
    for term in ("name", "license", "version", "identifier", "datePublished", "url"):
        assert root[term] == CODEMETA[term]
    assert root["author"] == [{"@id": "https://orcid.org/0000-0002-1825-0097"}]
    assert graph["https://orcid.org/0000-0002-1825-0097"]["name"] == "Ada Duck"
    lines = (OUT / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    sums = {name: digest for digest, name in (line.split(" *") for line in lines)}
    assert graph["data/open.csv"] == {
        "@id": "data/open.csv",
        "@type": "File",
        "name": "open.csv",
        "encodingFormat": "text/csv",
        "contentSize": str((OUT / "data/open.csv").stat().st_size),
        "sha256": sums["data/open.csv"],
        "isBasedOn": {"@id": "#source-open"},
    }
    record = json.loads(Path("data/raw/open.csv.fetch.json").read_text(encoding="utf-8"))
    assert graph["#source-open"] == {
        "@id": "#source-open",
        "@type": "CreativeWork",
        "identifier": "open",
        "name": "Open duck counts",
        "url": record["url"],
        "license": "CC0-1.0",
        "usageInfo": "https://example.org/open/terms",
        "conditionsOfAccess": "terms of open",
    }
    assert graph["#source-secret"]["name"] == "secret"
    assert graph["#fetch-open"] == {
        "@id": "#fetch-open",
        "@type": "DownloadAction",
        "object": {"@id": "#source-open"},
        "endTime": record["retrieved_at"],
    }
    assert root["isBasedOn"] == [{"@id": "#source-open"}, {"@id": "#source-secret"}]
    assert root["mentions"] == [{"@id": "#fetch-open"}, {"@id": "#fetch-secret"}]

    croissant = json.loads((OUT / "croissant.json").read_text(encoding="utf-8"))
    assert croissant["conformsTo"] == "http://mlcommons.org/croissant/1.1"
    assert croissant["@type"] == "sc:Dataset"
    for term in ("name", "license", "version", "identifier", "datePublished", "url"):
        assert croissant[term] == CODEMETA[term]
    assert croissant["description"] == "Enten der Dünen"
    assert "author" not in croissant
    assert croissant["creator"] == [{**AUTHOR, "name": "Ada Duck"}]
    (file_object,) = croissant["distribution"]
    assert file_object == {
        "@type": "cr:FileObject",
        "@id": "data/open.csv",
        "name": "open.csv",
        "contentUrl": "data/open.csv",
        "encodingFormat": "text/csv",
        "sha256": sums["data/open.csv"],
    }
    (records,) = croissant["recordSet"]
    assert {key: records[key] for key in ("@type", "@id", "name")} == {
        "@type": "cr:RecordSet",
        "@id": "open",
        "name": "open",
    }
    assert [field["name"] for field in records["field"]][-2:] == ["year", "ducks/ha"]
    assert records["field"][-1] == {
        "@type": "cr:Field",
        "@id": "open/ducks%2Fha",
        "name": "ducks/ha",
        "dataType": "sc:Text",
        "source": {"fileObject": {"@id": "data/open.csv"}, "extract": {"column": "ducks/ha"}},
    }


@pytest.mark.usefixtures("stand_ins")
def test_a_shipped_file_source_gets_a_media_type_and_no_record_set(project: Path) -> None:
    _write_manifest(project, secret_shared=True)
    assert main(["build"]) == 0
    sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["ro-crate", "croissant"])
    croissant = json.loads((OUT / "croissant.json").read_text(encoding="utf-8"))
    assert [(f["@id"], f["encodingFormat"]) for f in croissant["distribution"]] == [
        ("data/open.csv", "text/csv"),
        ("data/secret.bin", "application/octet-stream"),
    ]
    assert [r["@id"] for r in croissant["recordSet"]] == ["open"]


def test_people_get_an_id_and_a_name() -> None:
    refs, people = releaser._people(  # noqa: SLF001
        [{"givenName": "Ada"}, "Bea Duck", {"@id": "https://orcid.org/1", "name": "Cy", "@type": "Organization"}]
    )
    assert refs == [{"@id": "#author-1"}, {"@id": "#author-2"}, {"@id": "https://orcid.org/1"}]
    assert people == [
        {"givenName": "Ada", "@id": "#author-1", "@type": "Person", "name": "Ada"},
        {"@id": "#author-2", "name": "Bea Duck", "@type": "Person"},
        {"@id": "https://orcid.org/1", "name": "Cy", "@type": "Organization"},
    ]


@pytest.mark.usefixtures("stand_ins")
def test_one_author_need_not_be_a_list_and_none_is_left_out(project: Path) -> None:
    meta = {**CODEMETA, "author": AUTHOR}
    (project / "codemeta.json").write_text(json.dumps(meta), encoding="utf-8")
    sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["ro-crate", "croissant"])
    assert len(json.loads((OUT / "croissant.json").read_text(encoding="utf-8"))["creator"]) == 1
    for empty in ({k: v for k, v in CODEMETA.items() if k != "author"}, {**CODEMETA, "author": []}):
        (project / "codemeta.json").write_text(json.dumps(empty), encoding="utf-8")
        new = Path(f"r{len(empty)}")
        sourceledger.release(manifest_mod.load_manifest(), new, formats=["ro-crate", "croissant"])
        root = next(
            e for e in json.loads((new / "ro-crate-metadata.json").read_text("utf-8"))["@graph"] if e["@id"] == "./"
        )
        assert "author" not in root
        assert "creator" not in json.loads((new / "croissant.json").read_text(encoding="utf-8"))


@pytest.mark.usefixtures("project", "stand_ins")
def test_a_recorded_file_without_a_download_time_has_no_end_time() -> None:
    assert main(["record", "secret", "upstream/secret.bin"]) == 0
    assert main(["build"]) == 0
    sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["ro-crate"])
    graph = {e["@id"]: e for e in json.loads((OUT / "ro-crate-metadata.json").read_text("utf-8"))["@graph"]}
    assert "endTime" not in graph["#fetch-secret"]
    assert "endTime" in graph["#fetch-open"]


@pytest.mark.usefixtures("stand_ins")
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"datePublished": ""}, ["codemeta.json has no datePublished, which ro-crate needs"]),
        (
            {"name": None, "url": []},
            [
                "codemeta.json has no name, which ro-crate needs",
                "codemeta.json has no name, which croissant needs",
                "codemeta.json has no url, which croissant needs",
            ],
        ),
    ],
)
def test_a_field_a_format_needs_must_be_in_codemeta(
    project: Path, capsys: pytest.CaptureFixture[str], change: dict, expected: list[str]
) -> None:
    (project / "codemeta.json").write_text(json.dumps({**CODEMETA, **change}), encoding="utf-8")
    _write_manifest(project, formats='"ro-crate", "croissant"')
    assert _release(str(OUT), "--json") == 1
    assert [error["message"] for error in _json(capsys)["errors"]] == expected
    assert not OUT.exists()


@pytest.mark.usefixtures("stand_ins")
def test_codemeta_must_be_a_json_object_and_exist(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_manifest(project, formats='"croissant"')
    (project / "codemeta.json").write_text("[]", encoding="utf-8")
    assert _release(str(OUT)) == 1
    assert capsys.readouterr().err == "error: codemeta.json is not a JSON object\n"
    (project / "codemeta.json").unlink()
    assert _release(str(OUT)) == 3
    assert capsys.readouterr().err.startswith("error: codemeta.json is missing\n")


# The checks: SHA256SUMS, the validators in their own processes, and --check.


@pytest.mark.usefixtures("project")
def test_a_missing_validator_exits_3_before_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(releaser._VALIDATORS, releaser._CROISSANT, ("no-such-validator-dist", ("-c", "pass")))  # noqa: SLF001
    _write_manifest(Path.cwd(), formats='"croissant"')
    assert _release(str(OUT)) == 3
    assert capsys.readouterr().err == (
        "error: checking croissant.json needs no-such-validator-dist, from the release extra: "
        "uv tool install 'sourceledger[release]'\n"
    )
    assert not OUT.exists()
    with pytest.raises(sourceledger.ValidatorMissing):
        sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["croissant"])


@pytest.mark.usefixtures("project")
def test_each_validator_reports_its_own_findings(
    stand_ins: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both fail: the second still runs, each finding is one line, and nothing reaches OUT_DIR."""
    stand_ins(
        rocrate="import sys; print('no licence|no date', end=''); sys.exit(1)",
        croissant="import sys; sys.exit(3)",
    )
    _write_manifest(Path.cwd(), formats='"ro-crate", "croissant"')
    assert _release(str(OUT), "--json") == 1
    assert [error["message"] for error in _json(capsys)["errors"]] == [
        "ro-crate-metadata.json: no licence",
        "ro-crate-metadata.json: no date",
        "croissant.json: sourceledger exited 3: with no output",
    ]
    assert not OUT.exists()
    with pytest.raises(sourceledger.ValidatorFailed) as failed:
        sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["croissant"])
    assert isinstance(failed.value, sourceledger.ReleaseError)
    assert failed.value.problems == ["croissant.json: sourceledger exited 3: with no output"]


@pytest.mark.usefixtures("project")
def test_a_validator_that_writes_bytes_outside_utf8_still_reports(stand_ins: Callable[..., None]) -> None:
    script = "import sys; sys.stdout.buffer.write(b'bad \\xff|x'); sys.stderr.buffer.write(b'\\xfe'); sys.exit(1)"
    stand_ins(croissant=script)
    with pytest.raises(sourceledger.ValidatorFailed) as failed:
        sourceledger.release(manifest_mod.load_manifest(), OUT, formats=["croissant"])
    assert failed.value.problems == ["croissant.json: bad \ufffd", "croissant.json: x"]


def _done(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(returncode, [], stdout.encode(), stderr.encode())


def test_the_rocrate_findings_come_from_its_json_report() -> None:
    report = {"passed": False, "issues": [{"message": "No license.", "check": {"identifier": "ro-crate-1.3_13.3"}}]}
    assert releaser._findings("roc-validator", _done(1, json.dumps(report))) == [  # noqa: SLF001
        "No license. (ro-crate-1.3_13.3)"
    ]
    assert releaser._findings("roc-validator", _done(2, "[ERROR] no network\n\n", "trace\nURLError\n")) == [  # noqa: SLF001
        "roc-validator exited 2: URLError"
    ]
    assert releaser._findings("roc-validator", _done(1, '{"issues": [{"message": "m"}]}')) == [  # noqa: SLF001
        'roc-validator exited 1: {"issues": [{"message": "m"}]}'
    ]


def test_the_croissant_findings_are_its_errors_without_its_warnings() -> None:
    stderr = (
        "E1110 validate.py:55] Found the following 2 error(s) during the validation:\n"
        "  -  `license` should have type Text, but got int\n"
        "  -  no name\n"
        "Found the following 1 warning(s) during the validation:\n"
        "  -  citeAs is recommended\n"
    )
    assert releaser._findings("mlcroissant", _done(1, "", stderr)) == [  # noqa: SLF001
        "`license` should have type Text, but got int",
        "no name",
    ]
    assert releaser._findings("mlcroissant", _done(1, "", "Traceback\nKeyError: 'x'\n")) == [  # noqa: SLF001
        "mlcroissant exited 1: KeyError: 'x'"
    ]
    last = "Found the following 1 error(s) during the validation:\n  -  no url"
    assert releaser._findings("mlcroissant", _done(1, "", last)) == ["no url"]  # noqa: SLF001


@pytest.fixture
def released(project: Path, stand_ins: Callable[..., None]) -> Path:
    """A release folder with every format, checked by the stand-ins."""
    stand_ins()
    _write_manifest(project, formats='"ro-crate", "croissant"')
    assert _release(str(OUT)) == 0
    return OUT


def test_check_passes_a_good_release_and_names_each_check(released: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert _release("--check", str(released), "--json") == 0
    report = _json(capsys)
    assert [(v["file"], v["name"]) for v in report["validators"]] == [
        ("SHA256SUMS", "sourceledger"),
        ("ro-crate-metadata.json", "sourceledger"),
        ("croissant.json", "sourceledger"),
    ]
    assert report["problems"] == []
    assert _release("--check", str(released)) == 0
    assert capsys.readouterr().out.endswith(
        f"checked croissant.json with sourceledger {version('sourceledger')}\n0 problems\n"
    )


def test_check_runs_only_the_validators_of_the_files_there(
    released: Path, stand_ins: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    stand_ins(rocrate="import sys; sys.exit(1)")
    (released / "ro-crate-metadata.json").unlink()
    capsys.readouterr()
    assert _release("--check", str(released), "--json") == 1
    report = _json(capsys)
    assert [v["file"] for v in report["validators"]] == ["SHA256SUMS", "croissant.json"]
    assert report["problems"] == [f"{released.as_posix()}/ro-crate-metadata.json is listed in SHA256SUMS but missing"]


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        (
            lambda d: (d / "data/open.csv").write_bytes(b"x"),
            "release/data/open.csv does not match its SHA-256 in SHA256SUMS",
        ),
        (lambda d: (d / "data/extra.csv").write_bytes(b"x"), "release/data/extra.csv is not listed in SHA256SUMS"),
        (lambda d: (d / "SHA256SUMS").unlink(), "release/SHA256SUMS is missing"),
        (
            lambda d: (d / "SHA256SUMS").write_text("abc *x\n", encoding="utf-8"),
            "release/SHA256SUMS: line 1 is not `<sha256> *<path>`",
        ),
        (
            lambda d: (d / "SHA256SUMS").write_text("0" * 64 + " *../sources.toml\n", encoding="utf-8"),
            "release/SHA256SUMS: '../sources.toml' is not a file in release",
        ),
        # A Windows drive path, refused on every system even where it names a file inside.
        (
            lambda d: (d / "SHA256SUMS").write_text("0" * 64 + " *C:x\n", encoding="utf-8"),
            "release/SHA256SUMS: 'C:x' is not a file in release",
        ),
    ],
)
def test_check_finds_each_kind_of_damage(
    released: Path, capsys: pytest.CaptureFixture[str], tamper: object, problem: str
) -> None:
    tamper(released)  # ty: ignore[call-non-callable]
    capsys.readouterr()
    assert _release("--check", str(released), "--json") == 1
    assert problem in _json(capsys)["problems"]


def test_check_reads_on_past_a_bad_line(released: Path) -> None:
    sums = released / "SHA256SUMS"
    sums.write_text("junk\n" + sums.read_text(encoding="utf-8").replace("*data/open.csv", "*data/gone.csv"), "utf-8")
    assert releaser._sums_problems(released) == [  # noqa: SLF001
        "release/SHA256SUMS: line 1 is not `<sha256> *<path>`",
        "release/data/gone.csv is listed in SHA256SUMS but missing",
        "release/data/open.csv is not listed in SHA256SUMS",
    ]


def test_check_reads_text_mode_sums_too(released: Path) -> None:
    """`sha256sum` without -b marks a path with a space instead of `*`."""
    sums = released / "SHA256SUMS"
    sums.write_text(sums.read_text(encoding="utf-8").replace(" *", "  "), encoding="utf-8")
    assert releaser._sums_problems(released) == []  # noqa: SLF001


def test_check_of_a_missing_folder_says_what_to_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nowhere"
    with pytest.raises(FileNotFoundError) as error:
        releaser.check_release(missing)
    assert (error.value.errno, str(error.value)) == (errno.ENOENT, f"[Errno 2] no such folder: {str(missing)!r}")
    assert _release("--check", str(missing)) == 3
    assert (
        capsys.readouterr().err == f"error: {missing.as_posix()} is missing\nhelp: pass the release folder to check\n"
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "pass OUT_DIR to write a release, or --check DIR to check one"),
        (["out", "--check", "out"], "pass OUT_DIR to write a release, or --check DIR to check one"),
        (["--check", "out", "--dry-run"], "--check writes nothing; drop --dry-run"),
    ],
)
def test_release_needs_one_folder(argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        _release(*argv)
    assert exit_.value.code == 2
    assert f"error: {message}\n" in capsys.readouterr().err


# The real validators. mlcroissant runs offline; rocrate-validator fetches the RO-Crate
# JSON-LD context from w3id.org, so its test is a live one.

_HAS = {
    dist: importlib.util.find_spec(module) is not None
    for dist, module in [("mlcroissant", "mlcroissant"), ("roc-validator", "rocrate_validator")]
}


@pytest.mark.skipif(not _HAS["mlcroissant"], reason="needs the release extra")
def test_mlcroissant_passes_the_croissant_and_finds_a_broken_one(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_manifest(project, formats='"croissant"')
    assert _release(str(OUT), "--json") == 0
    (ran,) = [v for v in _json(capsys)["validators"] if v["file"] == "croissant.json"]
    assert ran == {
        "file": "croissant.json",
        "name": "mlcroissant",
        "version": version("mlcroissant"),
    }
    croissant = json.loads((OUT / "croissant.json").read_text(encoding="utf-8"))
    (OUT / "croissant.json").write_text(json.dumps({**croissant, "license": 7}), encoding="utf-8")
    assert _release("--check", str(OUT), "--json") == 1
    problems = _json(capsys)["problems"]
    # mlcroissant words the finding differently on Windows; that it reports one is the contract.
    assert any(p.startswith("croissant.json: ") and "license" in p for p in problems)


@pytest.mark.live
@pytest.mark.skipif(not _HAS["roc-validator"], reason="needs the release extra")
def test_rocrate_validator_passes_the_crate_and_finds_a_broken_one(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_manifest(project, formats='"ro-crate"')
    assert _release(str(OUT), "--json") == 0
    (ran,) = [v for v in _json(capsys)["validators"] if v["file"] == "ro-crate-metadata.json"]
    assert ran["name"] == "roc-validator"
    crate = json.loads((OUT / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    for entity in crate["@graph"]:
        if entity["@id"] == "./":
            del entity["license"]
    (OUT / "ro-crate-metadata.json").write_text(json.dumps(crate), encoding="utf-8")
    assert _release("--check", str(OUT), "--json") == 1
    assert any("`license`" in p and "ro-crate-1.3_" in p for p in _json(capsys)["problems"])
