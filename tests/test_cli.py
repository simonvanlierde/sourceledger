"""The command line's contract: per-stage help, error shape, exit codes, JSON, dry runs and progress."""

import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from support import (
    FIXTURE,
    OUT,
    RAW,
    WHEN,
    _http_error,
    _manifest,
    _Response,
    _run,
    _serve,
    _two_sources,
    fetch_mod,
    report_mod,
)

from sourceledger import cli, ledger
from sourceledger import manifest as manifest_mod
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
    upstream.write_text("year,value\n2020,1.5\n", encoding="utf-8", newline="")
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


# argparse before 3.13 aligns the help columns differently; the goldens hold the 3.13+ layout.
@pytest.mark.skipif(sys.version_info < (3, 13), reason="help layout differs before 3.13")
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
    manifest = Path("sources.toml")
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("file://", "bogus://"), encoding="utf-8")
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
    assert report["problems"] == report_mod.verify(tmp_path)
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
    manifest.write_text(manifest.read_text(encoding="utf-8") + "max_bytes = 5\n", encoding="utf-8")
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

    monkeypatch.setattr(cli, "fetch_sources", interrupted)
    assert main(["fetch"]) == 130
    assert capsys.readouterr().err == (
        "error: fetch was interrupted\nhelp: what finished before is kept; run `sourceledger fetch` to finish\n"
    )


@pytest.mark.parametrize(
    "url", ["https://example.org/a.csv?X-Amz-Signature=SECRET", "https://example.org/a.csv#SECRET"]
)
@pytest.mark.parametrize("form", [["--url", "{}"], ["--url={}"]])
def test_an_interrupted_record_prints_no_signed_url(
    url: str, form: list[str], project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`record` logs a URL without its query, so its suggested rerun must not print the signature either."""

    def interrupted(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "record_file", interrupted)
    assert main(["record", "example", str(project), *(part.format(url) for part in form), "--json"]) == 130
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    assert "https://example.org/a.csv" in out


@pytest.mark.usefixtures("project")
def test_ctrl_c_under_json_prints_the_report_so_far(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second source is interrupted; the first one's record is in the report, and the rerun repeats the options."""
    manifest = Path("sources.toml")
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text + text.replace('"example"', '"other"'), encoding="utf-8")
    fetch_one = fetch_mod._fetch_one  # noqa: SLF001
    done: list[str] = []

    def once(source: manifest_mod.Source, *args: object) -> ledger.FetchRecord:
        if done:
            raise KeyboardInterrupt
        done.append(source["id"])
        return fetch_one(source, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(fetch_mod, "_fetch_one", once)
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

        monkeypatch.setattr(cli, "fetch_sources", locked)
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
    [  # openssl from PATH
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


def test_a_missing_path_named_like_the_manifest_asks_for_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["revisions", "sources.toml"]) == 3
    assert capsys.readouterr().err.endswith("help: pass an earlier copy of data/fetches.jsonl\n")


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
        subprocess.run(  # fixed arguments
            [sys.executable, "-c", "import sys; from sourceledger.cli import main; sys.exit(main())", "report"],
            stdout=fh,
            env=env,
            check=True,
        )
    html = cli.report(manifest_mod.load_manifest(manifest))
    assert "Łódź ☃ data" in html
    assert out.read_bytes() == html.encode("utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no death by signal")
def test_the_command_dies_of_sigint_after_ctrl_c() -> None:
    """A shell loop stops on Ctrl-C only when the command died of SIGINT, not when it exited 130."""
    result = subprocess.run(
        [sys.executable, "-c", "from sourceledger import cli; cli.main = lambda: 130; cli.entry()"], check=False
    )
    assert result.returncode == -2  # SIGINT


def test_the_command_exits_with_mains_code() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from sourceledger import cli; cli.main = lambda: 3; cli.entry()"], check=False
    )
    assert result.returncode == 3


def _add_second_source() -> None:
    manifest = Path("sources.toml")
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text + text.replace('"example"', '"other"'), encoding="utf-8")


def test_fetch_dry_run_json_lists_what_it_would_fetch(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add_second_source()
    assert main(["fetch", "--dry-run", "--json", "--only", "example"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["would_fetch"] == [{"source_id": "example", "url": project.as_uri()}]


@pytest.mark.usefixtures("project")
def test_build_dry_run_honours_only(capsys: pytest.CaptureFixture[str]) -> None:
    _add_second_source()
    assert main(["fetch", "--only", "example"]) == 0
    capsys.readouterr()
    assert main(["build", "--dry-run", "--only", "example"]) == 0
    assert capsys.readouterr().out == (
        "would write data/processed/example.csv\nwould write data/processed/datapackage.json\n"
    )


@pytest.mark.usefixtures("project")
def test_record_dry_run_says_unchanged_for_a_logged_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch"]) == 0
    capsys.readouterr()
    assert main(["record", "example", "data/raw/example.csv", "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("would record example from data/raw/example.csv: unchanged, 20 B, ")


def test_a_missing_stage_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main([])
    assert exit_.value.code == 2
    assert "error: the following arguments are required: STAGE" in capsys.readouterr().err


def test_version_of_the_default_project_is_the_bare_release(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out == f"sourceledger {cli.version('sourceledger')}\n"


@pytest.mark.usefixtures("project")
def test_main_reads_sys_argv_by_default(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Also checks the rerun hint, which repeats the arguments the run was given."""
    monkeypatch.setattr(sys, "argv", ["sourceledger", "status", "--json"])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "status"

    def interrupted(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "fetch_sources", interrupted)
    monkeypatch.setattr(sys, "argv", ["sourceledger", "fetch"])
    assert main() == 130
    assert "run `sourceledger fetch` to finish" in capsys.readouterr().err


class _Stream(io.StringIO):
    """A text stream that records how main reconfigures it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


@pytest.mark.usefixtures("project")
def test_main_makes_both_streams_utf8_and_stdout_lf(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _Stream(), _Stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert main(["status"]) == 0
    assert out.calls == [{"encoding": "utf-8", "newline": "\n"}]
    assert err.calls == [{"encoding": "utf-8"}]


@pytest.mark.parametrize(
    ("code", "platform", "kills"), [(130, "linux", True), (3, "linux", False), (130, "win32", False)]
)
def test_entry_re_raises_sigint_only_after_ctrl_c_off_windows(
    code: int, platform: str, monkeypatch: pytest.MonkeyPatch, *, kills: bool
) -> None:
    """In process, so mutmut sees it: the signal calls are recorded, not sent."""
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli, "main", lambda: code)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(signal, "signal", lambda *args: calls.append(("signal", *args)))
    monkeypatch.setattr(os, "kill", lambda *args: calls.append(("kill", *args)))
    with pytest.raises(SystemExit) as exit_:
        cli.entry()
    assert exit_.value.code == code
    expected = [("signal", signal.SIGINT, signal.SIG_DFL), ("kill", os.getpid(), signal.SIGINT)] if kills else []
    assert calls == expected


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
    Path(path).write_text(text, encoding="utf-8", newline="")
    capsys.readouterr()
    assert _run(stage) == [3]
    err = capsys.readouterr().err
    assert expected in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.usefixtures("upstream")
def test_a_missing_input_is_one_line_and_a_failed_exit(capsys: pytest.CaptureFixture[str]) -> None:
    """Building before fetching names the missing file instead of printing a traceback."""
    assert _run("build") == [3]
    err = capsys.readouterr().err
    assert "example.csv.fetch.json is missing" in err
    assert len(err.strip().splitlines()) == 1


def test_a_failed_download_is_one_line_and_a_failed_exit(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead URL is reported like any other expected failure, not as a traceback."""
    _manifest(tmp_path, upstream.with_name("gone.csv").as_uri())
    assert _run("fetch") == [3]
    err = capsys.readouterr().err
    assert err.startswith("error: example: fetching ")
    assert len(err.strip().splitlines()) == 1  # no help: nothing else was fetched


def test_only_fetches_builds_and_validates_the_named_sources(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other sources are left alone, and the package keeps their entries from the last build."""
    other = upstream.with_name("b.csv")
    other.write_text(FIXTURE, encoding="utf-8", newline="")
    _two_sources(tmp_path, upstream.as_uri(), other.as_uri())
    assert main(["fetch", "--only", "a"]) == 0
    assert Path("data/raw/a.csv").exists()
    assert not Path("data/raw/b.csv").exists()
    assert main(["build", "--only", "a"]) == 0
    assert main(["validate", "--only", "a"]) == 0
    capsys.readouterr()
    assert main(["validate"]) == 1
    out, err = capsys.readouterr()
    assert "not the implemented sources" in out
    assert err == ""

    assert _run("fetch", "build") == [0, 0]
    package = Path("data/processed/datapackage.json")
    b_entry = json.loads(package.read_text(encoding="utf-8"))["resources"][1]
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    assert main(["fetch", "--only", "a", "--only", "a"]) == 0
    assert main(["build", "--only", "a"]) == 0
    assert json.loads(package.read_text(encoding="utf-8"))["resources"][1] == b_entry
    assert _run("validate") == [0]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["fetch", "--only", "zz"], "error: ids ['zz'] are not in sources.toml"),
        (["build", "--only", "zz"], "error: ids ['zz'] are not in sources.toml"),
        (["validate", "--only", "zz"], "error: ids ['zz'] are not in sources.toml"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_only_refuses_an_unknown_id(argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped id is a usage error before anything is written, instead of quietly doing nothing."""
    assert main(argv) == 2
    assert capsys.readouterr().err == expected + "\nhelp: run `sourceledger status` to list the source ids\n"
    assert not Path("data").exists()


@pytest.mark.usefixtures("upstream")
def test_only_and_paths_go_with_their_stages(capsys: pytest.CaptureFixture[str]) -> None:
    """--only is for fetch, build and validate; verify, like revisions, needs its path."""
    with pytest.raises(SystemExit):
        main(["status", "--only", "example"])
    assert "error: unrecognized arguments: --only example" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["verify"])
    assert "error: the following arguments are required: DIR" in capsys.readouterr().err


def test_an_unknown_stage_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Not a silent validate: argparse names the program and the choices."""
    with pytest.raises(SystemExit) as exit_:
        main(["publish"])
    assert exit_.value.code == 2
    assert capsys.readouterr().err.startswith("usage: sourceledger ")


def test_a_cut_off_gzip_fails_build_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated download that still starts like a gzip is a source error, not a traceback."""
    upstream = tmp_path / "upstream" / "example.csv.gz"
    upstream.parent.mkdir()
    upstream.write_bytes(gzip.compress(FIXTURE.encode() * 50)[:40])
    _manifest(tmp_path, upstream.as_uri())
    monkeypatch.chdir(tmp_path)
    assert _run("fetch") == [0]
    capsys.readouterr()
    assert _run("build") == [3]
    err = capsys.readouterr().err
    assert err.startswith("error: example: not a readable gzip file (")
    assert err.count("\n") == 1


@pytest.mark.usefixtures("upstream")
def test_warnings_name_the_program(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A retry warning on stderr starts with the program's name, as the CLI's other messages do."""
    monkeypatch.setattr(logging.root, "handlers", [])
    _serve(monkeypatch, _http_error(503), _Response(FIXTURE.encode()))
    try:
        assert main(["fetch"], prog="my-dataset") == 0
    finally:
        for handler in logging.root.handlers:
            handler.close()
    assert capsys.readouterr().err.startswith("my-dataset: example: attempt 1 of 4 failed")


@pytest.mark.usefixtures("upstream")
def test_json_status_keeps_types_and_full_hashes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "schema_version": 1,
        "stage": "status",
        "errors": [],
        "sources": [
            {
                "source_id": "example",
                "status": "implemented",
                "vintage": "not stated",
                "retrieved_at": None,
                "recorded_at": None,
                "sha256": None,
                "license": "CC0-1.0",
                "redistribute": True,
            },
            {
                "source_id": "later",
                "status": "unimplemented",
                "vintage": "not stated",
                "retrieved_at": None,
                "recorded_at": None,
                "sha256": None,
                "license": None,
                "redistribute": None,
            },
        ],
    }
    assert _run("fetch") == [0]
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert report["sources"][0]["sha256"] == record["sha256"]
    assert report["sources"][0]["retrieved_at"] == record["retrieved_at"]


@pytest.mark.parametrize(
    "args",
    [
        ["revisions", "before.jsonl", "--json"],
        ["revisions", "--json", "before.jsonl"],
    ],
)
def test_json_revisions_describes_changed_bytes(
    upstream: Path, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert _run("fetch") == [0]
    before = Path("before.jsonl")
    shutil.copyfile(LOG, before)
    old = json.loads(before.read_text(encoding="utf-8"))
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8")
    assert _run("fetch") == [0]
    new = json.loads(LOG.read_text(encoding="utf-8").splitlines()[-1])
    capsys.readouterr()
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "stage": "revisions",
        "errors": [],
        "revisions": [{"source_id": "example", "before_sha256": old["sha256"], "after": new}],
    }
    assert main(["revisions", str(LOG), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["revisions"] == []


@pytest.mark.parametrize("stage", ["status", "validate", "revisions"])
def test_json_runtime_errors_are_one_document(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    args = [stage, "--json"] + (["missing.jsonl"] if stage == "revisions" else [])
    assert main(args) == 3
    captured = capsys.readouterr()
    assert captured.err == ""
    message, help_ = (
        ("missing.jsonl is missing", "pass an earlier copy of data/fetches.jsonl")
        if stage == "revisions"
        else ("sources.toml is missing", "run `sourceledger init` to write one, or pass --manifest PATH")
    )
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "stage": stage,
        "errors": [{"message": message, "help": help_}],
    }


@pytest.mark.parametrize("stage", ["init", "build", "attribution", "report"])
def test_json_is_rejected_for_unsupported_stages(stage: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        main([stage, "--json"])
    assert "error: unrecognized arguments: --json" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_json_reports_honor_manifest_and_report_bad_input(capsys: pytest.CaptureFixture[str]) -> None:
    Path("sources.toml").rename("alternate.toml")
    assert main(["status", "--json", "--manifest", "alternate.toml"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["source_id"] == "example"
    assert main(["validate", "--json", "--manifest", "alternate.toml", "--only", "missing"]) == 2
    assert "not in sources.toml" in json.loads(capsys.readouterr().out)["errors"][0]["message"]
    Path("alternate.toml").write_text("bad toml [", encoding="utf-8")
    assert main(["status", "--json", "--manifest", "alternate.toml"]) == 3
    assert json.loads(capsys.readouterr().out)["errors"]


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize("name", [[], {}, None])
def test_json_validate_reports_a_malformed_resource_name(capsys: pytest.CaptureFixture[str], name: object) -> None:
    """A damaged package remains a validation report, without an uncaught TypeError."""
    assert main(["fetch"]) == 0
    assert main(["build"]) == 0
    path = Path("data/processed/datapackage.json")
    package = json.loads(path.read_text(encoding="utf-8"))
    package["resources"][0]["name"] = name
    path.write_text(json.dumps(package), encoding="utf-8")
    capsys.readouterr()
    assert main(["validate", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["problems"]


@pytest.mark.parametrize("count", [0, 10, 100])
def test_cli_build_does_not_reopen_outputs_to_count_rows(
    count: int, upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream.write_text("value,note\n" + '1,"two\nlines"\n' * count, encoding="utf-8", newline="")
    assert main(["fetch"]) == 0
    capsys.readouterr()
    opened: list[str] = []
    original_open = Path.open

    def tracking_open(path: Path, mode: str = "r", *args: object, **kwargs: object):  # noqa: ANN202
        # Keep the probe on the real writer and descriptor paths.
        if path == OUT:
            opened.append(mode)
        return original_open(path, mode, *args, **kwargs)  # ty: ignore[no-matching-overload]

    monkeypatch.setattr(Path, "open", tracking_open)
    assert main(["build"]) == 0
    assert f"{count} rows -> {OUT.as_posix()}" in capsys.readouterr().out
    assert opened == ["rb"]  # the descriptor hashes the completed output once


def test_the_record_stage_records_one_file(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`record ID PATH` prints a fetch-style line and passes every option on."""
    argv = [
        "record", "example", str(upstream), "--retrieved-at", WHEN,
        "--parameter", "country=NL", "--parameter", "query=a=b", "--last-modified", "x",
    ]  # fmt: skip
    assert main(argv) == 0
    assert capsys.readouterr().out.startswith("example  new")
    line = json.loads(LOG.read_text(encoding="utf-8"))
    assert (line["parameters"], line["last_modified"]) == ({"country": "NL", "query": "a=b"}, "x")
    assert line["retrieved_at"] == "2026-11-05T09:30:00Z"
    assert main(["record", "example", str(upstream)]) == 0
    assert capsys.readouterr().out.startswith("example  unchanged")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["record", "example"], "error: the following arguments are required: PATH"),
        (["fetch", "extra"], "error: unrecognized arguments: extra"),
        (["fetch", "--url", "x"], "error: unrecognized arguments: --url x"),
        (["status", "--parameter", "a=b"], "error: unrecognized arguments: --parameter a=b"),
        (["record", "example", "f", "--parameter", "novalue"], "--parameter takes NAME=VALUE, not 'novalue'"),
        (["record", "example", "f", "--parameter", "=v"], "--parameter takes NAME=VALUE, not '=v'"),
        (["record", "example", "f", "--parameter", "=a=b"], "--parameter takes NAME=VALUE, not '=a=b'"),
        (["record", "example", "f", "--parameter", "q=a=b", "--parameter", "q=c"], "--parameter names ['q']"),
        (
            ["record", "example", "f", "--parameter", "a=1", "--parameter", "a=2"],
            "--parameter names ['a'] more than once",
        ),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_the_record_options_are_checked(argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    assert exit_.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "code", "message"),
    [
        (
            ["record", "missing", "f"],
            2,
            "error: ids ['missing'] are not in sources.toml\nhelp: run `sourceledger status` to list the source ids",
        ),
        (["record", "later", "f"], 3, "error: later: only an implemented source can be recorded"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_the_record_stage_reports_a_failure_in_one_line(
    argv: list[str], code: int, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == code
    assert capsys.readouterr().err == message + "\n"


def test_the_record_stage_passes_url_and_validators_on(upstream: Path) -> None:
    """--etag, --last-modified and --url reach the record; left out, they stay empty strings."""
    assert main(["record", "example", str(upstream)]) == 0
    line = json.loads(LOG.read_text(encoding="utf-8"))
    assert (line["etag"], line["last_modified"]) == ("", "")
    assert main(["record", "example", str(upstream), "--etag", '"e1"', "--last-modified", "lm"]) == 0
    assert json.loads(Path(f"{RAW}.fetch.json").read_text(encoding="utf-8"))["etag"] == '"e1"'
    assert main(["record", "example", str(upstream), "--url", "https://mirror.example.org/x.csv"]) == 0
    assert json.loads(Path(f"{RAW}.fetch.json").read_text(encoding="utf-8"))["final_url"] == (
        "https://mirror.example.org/x.csv"
    )


def test_version_names_the_package(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.startswith("sourceledger ")


@pytest.mark.parametrize(("count", "text"), [(0, "0 problems"), (1, "1 problem"), (2, "2 problems")])
def test_a_problem_count_reads_as_english(count: int, text: str) -> None:
    from sourceledger.cli import _problems  # noqa: PLC0415  # private, tested alone

    assert _problems(count) == text
