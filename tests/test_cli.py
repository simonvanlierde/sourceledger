"""The command line's contract: per-stage help, error shape, exit codes, JSON, dry runs and progress."""

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from sourceledger import cli, ledger
from sourceledger.cli import main

TERMS = 'terms = "CC0-1.0"\nterms_url = "https://example.org/terms"\nlicense = "CC0-1.0"\nredistribute = true'
LOG = Path("data/fetches.jsonl")


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch retries return at once instead of sleeping between attempts."""
    monkeypatch.setattr(time, "sleep", lambda _: None)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with one implemented source served from a local file; the test runs inside it."""
    upstream = tmp_path / "upstream" / "example.csv"
    upstream.parent.mkdir()
    upstream.write_text("year,value\n2020,1.5\n", encoding="utf-8")
    (tmp_path / "sources.toml").write_text(
        f'[[source]]\nid = "example"\nstatus = "implemented"\nurl = "{upstream.as_uri()}"\n{TERMS}\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return upstream


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("stage", ["", *cli._STAGES])  # noqa: SLF001
def test_each_stage_has_its_own_help(
    stage: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], golden: Callable[[str, str], None]
) -> None:
    """`sourceledger STAGE --help` lists only that stage's options; the text is part of the contract."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit) as exit_:
        main([stage, "--help"] if stage else ["--help"])
    assert exit_.value.code == 0
    golden(f"help/{stage or 'sourceledger'}.txt", capsys.readouterr().out)


def test_an_error_names_what_failed_and_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 3
    assert capsys.readouterr().err == (
        "error: sources.toml is missing\nhelp: run `sourceledger init` to write one, or pass --manifest PATH\n"
    )


def test_a_usage_error_has_the_same_shape_and_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["fetch", "extra"])
    assert exit_.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: sourceledger fetch ")
    assert err.endswith("error: unrecognized arguments: extra\nhelp: run `sourceledger fetch --help` for its options\n")


@pytest.mark.usefixtures("project")
def test_an_unknown_only_id_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", "--only", "zz"]) == 2
    assert capsys.readouterr().err == (
        "error: ids ['zz'] are not in sources.toml\nhelp: run `sourceledger status` to list the source ids\n"
    )
    assert main(["validate", "--json", "--only", "zz"]) == 2
    assert json.loads(capsys.readouterr().out)["errors"] == [
        {"message": "ids ['zz'] are not in sources.toml", "help": "run `sourceledger status` to list the source ids"}
    ]
    assert not Path("data").exists()


@pytest.mark.usefixtures("project")
def test_problems_and_their_count_go_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 1
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines()[-1].endswith("problem") or out.splitlines()[-1].endswith("problems")
    assert len(out.splitlines()) > 1


@pytest.mark.usefixtures("project")
def test_every_json_report_has_a_schema_version(capsys: pytest.CaptureFixture[str]) -> None:
    """fetch, record, verify, status, validate and revisions share one envelope."""
    assert main(["fetch", "--json"]) == 0
    fetched = json.loads(capsys.readouterr().out)
    assert fetched["schema_version"] == 1
    assert (fetched["stage"], fetched["errors"]) == ("fetch", [])
    (record,) = fetched["records"]
    assert (record["source_id"], record["new"], len(record["sha256"])) == ("example", True, 64)
    assert json.loads(LOG.read_text(encoding="utf-8"))["sha256"] == record["sha256"]

    assert main(["record", "example", "data/raw/example.csv", "--json"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert (recorded["stage"], recorded["records"][0]["new"]) == ("record", False)
    assert main(["build"]) == 0
    capsys.readouterr()
    for argv, field in (
        (["verify", "data/processed", "--json"], "problems"),
        (["status", "--json"], "sources"),
        (["validate", "--json"], "problems"),
        (["revisions", str(LOG), "--json"], "revisions"),
    ):
        assert main(argv) == 0, argv
        report = json.loads(capsys.readouterr().out)
        assert report == {"schema_version": 1, "stage": argv[0], "errors": [], field: report[field]}


@pytest.mark.usefixtures("project")
def test_a_failed_json_run_reports_its_errors_and_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    Path("sources.toml").write_text(Path("sources.toml").read_text().replace("file://", "bogus://"))
    assert main(["fetch", "--json"]) == 3
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["records"] == []
    assert report["errors"][-1]["help"] is None  # nothing else was fetched
    assert captured.err == ""


def test_verify_json_lists_its_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "datapackage.json").write_text(
        json.dumps({"resources": [{"name": "x", "path": "x.csv", "bytes": 1, "hash": "sha256:" + "0" * 64}]})
    )
    assert main(["verify", str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["problems"] == ledger.verify(tmp_path)
    assert report["problems"]


@pytest.mark.usefixtures("project")
def test_progress_shows_on_a_terminal_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch"]) == 0
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    assert main(["fetch"]) == 0
    assert capsys.readouterr().err == "fetching example (1 of 1)\n"
    assert main(["fetch", "--json"]) == 0
    assert capsys.readouterr().err == ""


def test_fetch_dry_run_writes_nothing(project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _files(tmp_path)
    assert main(["fetch", "--dry-run"]) == 0
    assert capsys.readouterr().out == f"would fetch example from {project.as_uri()}\n"
    assert _files(tmp_path) == before


def test_record_dry_run_checks_everything_and_writes_nothing(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _files(tmp_path)
    assert main(["record", "example", str(project), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith(f"would record example from {project}: new, 20 B, sha256 ")
    assert _files(tmp_path) == before
    assert main(["record", "example", str(project), "--dry-run", "--retrieved-at", "yesterday"]) == 3
    assert "is not an ISO 8601 time" in capsys.readouterr().err
    assert main(["record", "example", str(project), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["dry_run"], report["records"][0]["new"]) == (True, True)
    assert _files(tmp_path) == before


def test_record_dry_run_keeps_the_size_cap(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The dry run refuses an over-cap file with the same message as the real record."""
    manifest = Path("sources.toml")
    manifest.write_text(manifest.read_text() + "max_bytes = 5\n")
    assert main(["record", "example", str(project), "--dry-run"]) == 3
    dry = capsys.readouterr().err
    assert not Path("data").exists()
    assert main(["record", "example", str(project)]) == 3
    assert (
        capsys.readouterr().err
        == dry
        == ("error: example: download exceeds 5 bytes; raise max_bytes in sources.toml if expected\n")
    )


@pytest.mark.usefixtures("project")
def test_build_dry_run_names_the_outputs_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", "--dry-run"]) == 3
    assert "example.csv.fetch.json is missing" in capsys.readouterr().err
    assert main(["fetch"]) == 0
    before = _files(tmp_path)
    capsys.readouterr()
    assert main(["build", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "would write data/processed/example.csv\nwould write data/processed/datapackage.json\n"
    )
    assert _files(tmp_path) == before


@pytest.mark.usefixtures("project")
def test_ctrl_c_exits_130_with_one_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def interrupted(*_: object, **__: object) -> list[ledger.FetchRecord]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_fetch", interrupted)
    assert main(["fetch"]) == 130
    assert capsys.readouterr().err == (
        "error: fetch was interrupted\nhelp: what finished before is kept; run `sourceledger fetch` to finish\n"
    )
