"""The command line: `<project> STAGE`, one subcommand per stage, with the project's own parser.

Exit codes: 0 done, 1 problems found (validate, verify), 2 usage error, 3 could not run
(a missing or bad file, a failed download, a held lock), 130 interrupted. Errors go to
stderr as `error: <what>` and, when there is a next step, `help: <what to do>`.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import os
import re
import shlex
import signal
import sys
import tomllib
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from sourceledger.ledger import (
    DATAPACKAGE,
    FETCH_LOG,
    MANIFEST,
    PROCESSED,
    RAW,
    USER_AGENT,
    FetchError,
    FetchRecord,
    Parser,
    Source,
    _build,
    _current,
    _fetch,
    _fields,
    _output_name,
    _record_file,
    _revision_records,
    _select,
    _status_records,
    _without_query,
    attribution,
    implemented,
    init,
    load_manifest,
    load_output_format,
    one_line,
    parse_source,
    revisions,
    status,
    validate,
    verify,
)
from sourceledger.report import report

if TYPE_CHECKING:
    from collections.abc import Callable

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2
EXIT_FAILED = 3
EXIT_INTERRUPTED = 130
# Bumped when a field of the --json reports changes meaning or goes away.
JSON_SCHEMA_VERSION = 1

_STAGES = {
    "init": "write a starter sources.toml",
    "fetch": "download every implemented source and log it",
    "record": "take in a file another tool downloaded, and log it as fetch would",
    "build": "write data/processed/ and datapackage.json",
    "validate": "check the outputs against the log and the manifest",
    "status": "print one line per source: status, vintage, last fetch, licence",
    "attribution": "print a Markdown table of the sources and their terms",
    "revisions": "list the sources whose bytes changed since an earlier log",
    "verify": "check a published dataset against its datapackage.json",
    "report": "write a standalone HTML provenance report to stdout",
}

_EPILOG = """\
a first run:
  {prog} init        write a starter sources.toml
  {prog} fetch       download every source and log it
  {prog} build       write data/processed/ and datapackage.json
  {prog} validate    check the outputs against the log and the manifest

exit codes:
  0 done, 1 problems found, 2 usage error, 3 could not run, 130 interrupted

docs: https://simonvanlierde.com/sourceledger/reference.html
"""


class _UsageError(Exception):
    """An argument the parser could not check, such as an id sources.toml does not have."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        """Usage errors in the same shape as every other error, with exit code 2."""
        self.print_usage(sys.stderr)
        _error(message, f"run `{self.prog} --help` for its options")
        sys.exit(EXIT_USAGE)


def _error(message: str, help_: str | None) -> None:
    """Print one error, rustc style: what went wrong, then what to do about it when that is known."""
    print(f"error: {message}", file=sys.stderr)
    if help_:
        print(f"help: {help_}", file=sys.stderr)


@dataclasses.dataclass
class _Run:
    """One stage's arguments, and the fields of its --json report as the stage fills them."""

    args: argparse.Namespace
    argv: list[str]
    parse: Parser
    prog: str
    user_agent: str
    result: dict[str, object] = dataclasses.field(default_factory=dict)

    def say(self, line: str) -> None:
        """Print a line of the text report; --json prints the report as one object instead."""
        if not self.args.json:
            print(line)


def _parser(prog: str, manifest: Path) -> argparse.ArgumentParser:
    """The top-level parser, with one subparser per stage taking only that stage's options."""
    parser = _ArgumentParser(
        prog=prog,
        description="Fetch, build and validate the sources in sources.toml.",
        epilog=_EPILOG.format(prog=prog),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    release = f"sourceledger {version('sourceledger')}"
    parser.add_argument(
        "--version",
        action="version",
        version=release if prog == "sourceledger" else f"{prog} ({release})",  # noqa: PLR2004
    )
    # For the stages without these options: text output, and the manifest an error may name.
    parser.set_defaults(json=False, manifest=manifest)
    stages = parser.add_subparsers(dest="stage", required=True, metavar="STAGE", title="stages")
    manifest_option = _ArgumentParser(add_help=False)
    manifest_option.add_argument(
        "--manifest", type=Path, default=manifest, metavar="PATH", help="the manifest to read (default: %(default)s)"
    )
    json_option = _ArgumentParser(add_help=False)
    json_option.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    only_option = _ArgumentParser(add_help=False)
    only_option.add_argument("--only", action="append", metavar="ID", help="only this source (repeatable)")
    dry_run_option = _ArgumentParser(add_help=False)
    dry_run_option.add_argument("--dry-run", action="store_true", help="say what would happen, and write nothing")
    parents = {
        "init": [manifest_option],
        "fetch": [manifest_option, only_option, dry_run_option, json_option],
        "record": [manifest_option, dry_run_option, json_option],
        "build": [manifest_option, only_option, dry_run_option],
        "validate": [manifest_option, only_option, json_option],
        "status": [manifest_option, json_option],
        "attribution": [manifest_option],
        "revisions": [json_option],
        "verify": [json_option],
        "report": [manifest_option],
    }
    sub = {
        name: stages.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".", parents=parents[name])
        for name, text in _STAGES.items()
    }
    sub["record"].add_argument("id", metavar="ID", help="the source in sources.toml")
    sub["record"].add_argument("path", type=Path, metavar="PATH", help="the file another tool downloaded")
    sub["record"].add_argument("--url", help="where the file came from (default: the source's url)")
    sub["record"].add_argument(
        "--retrieved-at",
        type=_time,
        metavar="TIME",
        help="when it was downloaded, ISO 8601 with a zone, such as 2026-11-05T09:30:00Z; "
        "without it the record states no download time",
    )
    sub["record"].add_argument("--etag", default="", help="the ETag the server sent")
    sub["record"].add_argument(
        "--last-modified", default="", metavar="HTTP_DATE", help="the Last-Modified the server sent"
    )
    sub["record"].add_argument(
        "--parameter", action="append", default=[], metavar="NAME=VALUE", help="request metadata (repeatable)"
    )
    sub["record"].set_defaults(check=_check_parameters)
    sub["revisions"].add_argument("path", type=Path, metavar="OLD_LOG", help="an earlier copy of data/fetches.jsonl")
    sub["verify"].add_argument("path", type=Path, metavar="DIR", help="the folder of a published dataset")
    for name, stage_parser in sub.items():
        stage_parser.set_defaults(parser=stage_parser, run=_HANDLERS[name])
    return parser


def _time(text: str) -> str:
    """`text` as given, once it reads as an ISO 8601 time; the library checks its zone and that it is past."""
    try:
        datetime.fromisoformat(text)
    except ValueError:
        msg = f"{text!r} is not an ISO 8601 time, such as 2026-11-05T09:30:00Z"
        raise argparse.ArgumentTypeError(msg) from None
    return text


def _check_parameters(args: argparse.Namespace) -> None:
    """Exit with a usage error for a --parameter that is not NAME=VALUE, or a name given twice."""
    if bad := [p for p in args.parameter if not (p.partition("=")[0] and p.partition("=")[1])]:
        args.parser.error(f"--parameter takes NAME=VALUE, not {bad[0]!r}")
    names = [p.partition("=")[0] for p in args.parameter]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        args.parser.error(f"--parameter names {dupes} more than once")


def entry(
    *,
    parse: Parser = parse_source,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
    manifest: Path = MANIFEST,
) -> NoReturn:
    """The `sourceledger` command: run `main` with these arguments, and exit as the shell expects.

    A project's own command points its console script at a function that calls this with its
    `parse`, `prog`, `user_agent` and `manifest`, as for `main`.
    A shell loop stops on Ctrl-C only when the command was killed by the signal; an exit code of
    130 alone does not stop it, so after Ctrl-C this dies of SIGINT. `main` itself returns 130.
    A reader that closes the pipe early, such as `head`, ends the command quietly by SIGPIPE.
    """
    if hasattr(signal, "SIGPIPE"):  # not on Windows
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    code = main(parse=parse, prog=prog, user_agent=user_agent, manifest=manifest)
    if code == EXIT_INTERRUPTED and sys.platform != "win32":  # noqa: PLR2004
        sys.stdout.flush()
        sys.stderr.flush()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
    sys.exit(code)


def main(  # the entry point: parse, run one stage, report it
    argv: list[str] | None = None,
    *,
    parse: Parser = parse_source,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
    manifest: Path = MANIFEST,
) -> int:
    """Run one stage: init, fetch, record, build, validate, status, attribution, revisions, verify or report.

    `sourceledger STAGE --help` lists each stage's options. A project's own entry point calls
    this with its `parse`, its name as `prog`, and that name as `user_agent`. `manifest` is the
    sources.toml to read, `./sources.toml` by default; a project that ships it inside its
    package passes that path, and `--manifest PATH` overrides both.

    Returns the exit code the module docstring lists. A usage error exits through SystemExit
    with code 2, as argparse does.
    """
    # Windows writes redirected output in the locale's code page with CRLF; the HTML report says UTF-8.
    for stream, newline in ((sys.stdout, {"newline": "\n"}), (sys.stderr, {})):
        if reconfigure := getattr(stream, "reconfigure", None):
            reconfigure(encoding="utf-8", **newline)
    argv = sys.argv[1:] if argv is None else argv
    args, extra = _parser(prog, manifest).parse_known_args(argv)
    if extra:  # reported by the stage's parser, so the usage and help name the stage
        args.parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if check := getattr(args, "check", None):
        check(args)
    # Retries and recovered fetch logs are reported as warnings, on stderr.
    logging.basicConfig(format=f"{prog}: %(message)s", level=logging.WARNING)
    return _main(_Run(args, argv, parse, prog, user_agent))


def _main(run: _Run) -> int:
    """Run the stage, then print its errors (or its JSON report); the exit code."""
    errors: list[tuple[str, str | None]] = []
    try:
        code = EXIT_PROBLEMS if run.args.run(run) else EXIT_OK
    except _UsageError as error:
        errors = [(str(error), f"run `{run.prog} status` to list the source ids")]
        code = EXIT_USAGE
    except FetchError as error:
        errors = [(failure, None) for failure in error.failures]
        if error.records:  # say what was kept, after the last failure
            errors[-1] = (
                error.failures[-1],
                f"the other sources were fetched and logged; run `{run.prog} fetch` again to retry",
            )
        code = EXIT_FAILED
    # OSError: a missing file, a failed download or a held lock. ValueError: a bad manifest,
    # a download that fails its checks, or a stale fetch. csv.Error: a source that is not CSV.
    except (OSError, ValueError, csv.Error) as error:
        errors = [_explain(error, run)]
        code = EXIT_FAILED
    except KeyboardInterrupt:
        # A fetch moves each file into place whole, so what finished is kept and logged.
        url = getattr(run.args, "url", None)
        # record logs the URL without its query and fragment, which can hold a signature; so does the rerun.
        argv = [a.replace(url, _without_query(url)) for a in run.argv] if url else run.argv
        rerun = shlex.join([run.prog, *argv])
        errors = [(f"{run.args.stage} was interrupted", f"what finished before is kept; run `{rerun}` to finish")]
        code = EXIT_INTERRUPTED
    except Exception as error:  # noqa: BLE001  # such as a project's parser failing: one line, not a traceback
        errors = [(f"{type(error).__name__}: {error}", None)]
        code = EXIT_FAILED
    if run.args.json:
        document = {"schema_version": JSON_SCHEMA_VERSION, "stage": run.args.stage, **run.result}
        document["errors"] = [{"message": message, "help": help_} for message, help_ in errors]
        print(json.dumps(document))
    else:
        for message, help_ in errors:
            _error(message, help_)
    return code


# What to pass instead of a stage's PATH operand that is missing.
_MISSING_PATH_HELP = {
    "record": "pass the file another tool downloaded",
    "revisions": f"pass an earlier copy of {FETCH_LOG.as_posix()}",
    "verify": f"pass the folder that holds the dataset's {DATAPACKAGE}",
}


def _explain(error: Exception, run: _Run) -> tuple[str, str | None]:
    """The error line and its help; a TOML syntax error, which names only a line, also names the manifest."""
    if isinstance(error, tomllib.TOMLDecodeError):
        manifest = run.args.manifest.as_posix()
        # The line number is in the message; the error's lineno attribute is Python 3.14+.
        line = re.search(r"at line (\d+)", str(error))
        return f"{manifest}: {error}", line and f"fix the TOML syntax at line {line[1]} of {manifest}"
    return one_line(error), _help(error, run)


def _help(error: Exception, run: _Run) -> str | None:
    """What to do about an error the CLI knows the cause of; None for the rest."""
    missing = Path(error.filename) if isinstance(error, FileNotFoundError) and error.filename else None
    # The stage's own path first: one named like the manifest is still the path the user passed.
    if missing and (path := getattr(run.args, "path", None)) and missing in (path, path / DATAPACKAGE):
        return _MISSING_PATH_HELP[run.args.stage]
    if missing == run.args.manifest:
        return f"run `{run.prog} init` to write one, or pass --manifest PATH"
    if missing == FETCH_LOG:
        return f"run `{run.prog} fetch` first; it writes the log"
    if isinstance(error, FileExistsError):
        return "edit it, or pass --manifest PATH to write one elsewhere"
    # A held lock: BlockingIOError on POSIX, PermissionError on Windows.
    if isinstance(error.__cause__, (BlockingIOError, PermissionError)):
        return "wait for the other fetch or record to finish, then run this again"
    return None


def _sources(run: _Run, ids: list[str] | None = None) -> list[Source]:
    """The manifest's sources; a usage error for an id in `ids` (or --only) it does not have."""
    sources = load_manifest(run.args.manifest)
    try:
        _select(sources, ids or run.args.only)
    except ValueError as error:
        raise _UsageError(str(error)) from error
    return sources


def _new(record: FetchRecord, before: dict[str, str]) -> bool:
    """Whether the record's bytes differ from the last ones `before` logged for its source."""
    return before.get(record.source_id) != record.sha256


def _fetch_line(record: FetchRecord, before: dict[str, str]) -> str:
    """One line per fetched source; `new` when its bytes differ from the last ones `before` logged."""
    marker = "new" if _new(record, before) else "unchanged"
    return (
        f"{record.source_id}  {marker:<9}  {record.bytes:>9,} B  {record.sha256[:12]}  {record.vintage}  {record.url}"
    )


def _report_records(run: _Run, records: list[FetchRecord], before: dict[str, str]) -> None:
    run.result["records"] = [{**_fields(r), "new": _new(r, before)} for r in records]
    for record in records:
        run.say(_fetch_line(record, before))


def _problems(count: int) -> str:
    return f"{count} problem{'' if count == 1 else 's'}"


def _report_problems(run: _Run, problems: list[str]) -> list[str]:
    """The problems and their count, on stdout: they are the stage's result."""
    run.result["problems"] = problems
    for problem in problems:
        run.say(problem)
    run.say(_problems(len(problems)))
    return problems


def _progress(run: _Run) -> Callable[[str], None] | None:
    """Progress lines on stderr, only for a person watching a terminal."""
    if run.args.json or not sys.stderr.isatty():
        return None
    return lambda where: print(f"fetching {where}", file=sys.stderr)


def _init_stage(run: _Run) -> list[str]:
    init(run.args.manifest)
    run.say(f"wrote {run.args.manifest.as_posix()}; fill in your sources, then run: {run.prog} fetch")
    return []


def _fetch_stage(run: _Run) -> list[str]:
    sources = _sources(run)
    if run.args.dry_run:
        todo = implemented(_select(sources, run.args.only))
        run.result["dry_run"] = True
        run.result["would_fetch"] = [{"source_id": s["id"], "url": s["url"]} for s in todo]
        for source in todo:
            run.say(f"would fetch {source['id']} from {source['url']}")
        return []
    before: dict[str, str] = {}
    done: list[FetchRecord] = []
    try:
        _fetch(
            sources, user_agent=run.user_agent, only=run.args.only, before=before, progress=_progress(run), done=done
        )
    finally:  # on a failure or Ctrl-C too: the sources that did fetch
        _report_records(run, done, before)
    return []


def _record_stage(run: _Run) -> list[str]:
    args = run.args
    (source,) = _select(_sources(run, [args.id]), [args.id])
    before: dict[str, str] = {}
    result = _record_file(
        source,
        args.path,
        retrieved_at=args.retrieved_at,
        url=args.url,
        etag=args.etag,
        last_modified=args.last_modified,
        parameters=dict(p.split("=", 1) for p in args.parameter),
        raw=RAW,
        log=FETCH_LOG,
        before=before,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        run.result["dry_run"] = True
        run.result["records"] = [{**dataclasses.asdict(result), "new": _new(result, before)}]
        state = "new" if _new(result, before) else "unchanged"
        what = f"{state}, {result.bytes:,} B, sha256 {result.sha256}"
        run.say(f"would record {source['id']} from {args.path.as_posix()}: {what}")
    else:
        _report_records(run, [result], before)
    return []


def _build_stage(run: _Run) -> list[str]:
    sources = _sources(run)
    output_format = load_output_format(run.args.manifest)
    if run.args.dry_run:
        for source in implemented(_select(sources, run.args.only)):
            _current(source, RAW)  # the same refusal build gives for a missing or changed raw file
            run.say(f"would write {(PROCESSED / _output_name(source, output_format)).as_posix()}")
        run.say(f"would write {(PROCESSED / DATAPACKAGE).as_posix()}")
        return []
    for out, count in _build(sources, parse=run.parse, only=run.args.only, output_format=output_format):
        size = f"{out.stat().st_size} bytes" if count is None else f"{count} row{'' if count == 1 else 's'}"
        run.say(f"{size} -> {out.as_posix()}")
    run.say(f"package -> {(PROCESSED / DATAPACKAGE).as_posix()}")
    return []


def _validate_stage(run: _Run) -> list[str]:
    sources = _sources(run)
    output_format = load_output_format(run.args.manifest)
    return _report_problems(run, validate(sources, only=run.args.only, output_format=output_format))


def _status_stage(run: _Run) -> list[str]:
    sources = load_manifest(run.args.manifest)
    if run.args.json:
        run.result["sources"] = _status_records(sources)
    else:
        for line in status(sources):
            print(line)
    return []


def _attribution_stage(run: _Run) -> list[str]:
    for line in attribution(load_manifest(run.args.manifest)):
        run.say(line)
    return []


def _revisions_stage(run: _Run) -> list[str]:
    # Not a problem: a revised source is news, and the tripwire test decides whether it breaks anything.
    if run.args.json:
        run.result["revisions"] = _revision_records(run.args.path)
    else:
        for line in revisions(run.args.path) or ["no revisions"]:
            print(line)
    return []


def _verify_stage(run: _Run) -> list[str]:
    return _report_problems(run, verify(run.args.path))


def _report_stage(run: _Run) -> list[str]:
    print(report(load_manifest(run.args.manifest)), end="")
    return []


_HANDLERS: dict[str, Callable[[_Run], list[str]]] = {
    "init": _init_stage,
    "fetch": _fetch_stage,
    "record": _record_stage,
    "build": _build_stage,
    "validate": _validate_stage,
    "status": _status_stage,
    "attribution": _attribution_stage,
    "revisions": _revisions_stage,
    "verify": _verify_stage,
    "report": _report_stage,
}
