"""The command line's contract: per-stage help, error shape, exit codes, JSON, dry runs and progress."""

import hashlib
import json
import os
import subprocess
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


def _files(root: Path) -> dict[str, str]:
    """Every file under `root` with its SHA-256, and every folder, so a dry run that writes shows."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "folder"
        for p in root.rglob("*")
    }


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
    assert out.splitlines()[-1] == f"{len(out.splitlines()) - 1} problems"


@pytest.mark.parametrize(("resources", "count"), [(1, "1 problem"), (2, "2 problems")])
def test_the_count_reads_problem_or_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], resources: int, count: str
) -> None:
    listed = [{"name": f"x{i}", "path": f"x{i}.csv", "hash": "sha256:" + "0" * 64} for i in range(resources)]
    (tmp_path / "datapackage.json").write_text(json.dumps({"resources": listed}))
    assert main(["verify", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == count


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
    assert capsys.readouterr().out.startswith(f"would record example from {project.as_posix()}: new, 20 B, sha256 ")
    assert _files(tmp_path) == before
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
    expected = "error: example: download exceeds 5 bytes; raise max_bytes in sources.toml if expected\n"
    assert capsys.readouterr().err == dry == expected


def test_dry_runs_write_nothing_after_a_full_run(project: Path, tmp_path: Path) -> None:
    assert main(["fetch"]) == 0
    assert main(["build"]) == 0
    before = _files(tmp_path)
    assert main(["fetch", "--dry-run"]) == 0
    assert main(["record", "example", str(project), "--dry-run"]) == 0
    assert main(["build", "--dry-run"]) == 0
    assert _files(tmp_path) == before


def test_a_bad_retrieved_at_is_a_usage_error(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["record", "example", str(project), "--retrieved-at", "yesterday"])
    assert exit_.value.code == 2
    assert capsys.readouterr().err.endswith(
        "error: argument --retrieved-at: 'yesterday' is not an ISO 8601 time, such as 2026-11-05T09:30:00Z\n"
        "help: run `sourceledger record --help` for its options\n"
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


@pytest.mark.usefixtures("project")
def test_ctrl_c_under_json_prints_the_report_so_far(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second source is interrupted; the first one's record is in the report, and the rerun repeats the options."""
    manifest = Path("sources.toml")
    manifest.write_text(manifest.read_text() + manifest.read_text().replace('"example"', '"other"'))
    fetch_one = ledger._fetch_one  # noqa: SLF001
    done: list[str] = []

    def once(source: ledger.Source, *args: object) -> ledger.FetchRecord:
        if done:
            raise KeyboardInterrupt
        done.append(source["id"])
        return fetch_one(source, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(ledger, "_fetch_one", once)
    argv = ["fetch", "--json", "--manifest", "sources.toml", "--only", "example", "--only", "other"]
    assert main(argv) == 130
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert [r["source_id"] for r in report["records"]] == ["example"]
    assert report["errors"] == [
        {
            "message": "fetch was interrupted",
            "help": f"what finished before is kept; run `sourceledger {' '.join(argv)}` to finish",
        }
    ]
    assert captured.err == ""


@pytest.mark.usefixtures("project")
def test_an_unexpected_error_exits_3_without_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    def parse(*_: object) -> tuple[list[str], list[dict[str, str]]]:
        raise KeyError("year")  # noqa: EM101

    assert main(["fetch"]) == 0
    capsys.readouterr()
    assert main(["build"], parse=parse) == 3
    assert capsys.readouterr().err == "error: KeyError: 'year'\n"


@pytest.mark.usefixtures("project")
def test_a_held_lock_has_help_whatever_the_cause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for cause in (BlockingIOError, PermissionError):

        def locked(*_: object, cause: type[OSError] = cause, **__: object) -> list[ledger.FetchRecord]:
            msg = "another fetch is already running in data/raw"
            raise OSError(msg) from cause()

        monkeypatch.setattr(cli, "_fetch", locked)
        assert main(["fetch"]) == 3
        assert capsys.readouterr().err.endswith(
            "help: wait for the other fetch or record to finish, then run this again\n"
        ), cause


@pytest.mark.usefixtures("project")
def test_a_toml_syntax_error_names_the_manifest_and_line(capsys: pytest.CaptureFixture[str]) -> None:
    Path("sub").mkdir()
    Path("sub/sources.toml").write_text("[[source]]\nid = \n", encoding="utf-8")
    assert main(["status", "--manifest", "sub/sources.toml"]) == 3
    err = capsys.readouterr().err
    assert err.startswith("error: sub/sources.toml: ")
    assert err.endswith("(at line 2, column 6)\nhelp: fix the TOML syntax at line 2 of sub/sources.toml\n")


@pytest.mark.usefixtures("project")
@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (
            ["verify", "."],
            "error: datapackage.json is missing\nhelp: pass the folder that holds the dataset's datapackage.json\n",
        ),
        (["revisions", "old.jsonl"], "error: old.jsonl is missing\nhelp: pass an earlier copy of data/fetches.jsonl\n"),
        (
            ["record", "example", "gone.csv"],
            "error: gone.csv is missing\nhelp: pass the file another tool downloaded\n",
        ),
    ],
)
def test_a_missing_path_says_what_to_pass(argv: list[str], error: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch"]) == 0  # so revisions finds the current log
    capsys.readouterr()
    assert main(argv) == 3
    assert capsys.readouterr().err == error


@pytest.mark.usefixtures("project")
def test_build_and_revisions_say_what_they_did(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch"]) == 0
    Path("before.jsonl").write_bytes(LOG.read_bytes())
    capsys.readouterr()
    assert main(["build"]) == 0
    assert capsys.readouterr().out == (
        "1 row -> data/processed/example.csv\npackage -> data/processed/datapackage.json\n"
    )
    assert main(["revisions", "before.jsonl"]) == 0
    assert capsys.readouterr().out == "no revisions\n"


def test_version_names_the_project(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["--version"], prog="example-data")
    assert exit_.value.code == 0
    assert capsys.readouterr().out == f"example-data (sourceledger {cli.version('sourceledger')})\n"


@pytest.mark.usefixtures("project")
def test_output_is_utf8_with_lf_in_any_locale(tmp_path: Path) -> None:
    """Guards Windows, where redirected stdout is in the locale's code page with CRLF.

    On Linux the C locale with coercion and UTF-8 mode off gives ASCII stdout, the same failure.
    """
    manifest = Path("sources.toml")
    manifest.write_text(manifest.read_text(encoding="utf-8") + 'name = "Łódź ☃ data"\n', encoding="utf-8")
    assert main(["fetch"]) == 0
    assert main(["build"]) == 0
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONIOENCODING", "LANG", "LC_CTYPE"}}
    env |= {"PYTHONUTF8": "0", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0"}
    out = tmp_path / "provenance.html"
    with out.open("wb") as fh:
        subprocess.run(
            [sys.executable, "-c", "import sys; from sourceledger.cli import main; sys.exit(main())", "report"],
            stdout=fh,
            env=env,
            check=True,
        )
    html = cli.report(ledger.load_manifest(manifest))
    assert "Łódź ☃ data" in html
    assert out.read_bytes() == html.encode("utf-8")
