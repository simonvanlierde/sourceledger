"""The command line: `<project> STAGE`, one subcommand per stage, with the project's own parser.

Exit codes: 0 done, 1 problems found (validate, verify, release), 2 usage error, 3 could not run
(a missing or bad file, a failed download, a held lock, a validator missing or not finishing),
130 interrupted. Errors go to stderr as `error: <what>` and, when there is a next step, `help: <what to do>`.
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
from typing import TYPE_CHECKING, Any, NoReturn

from sourceledger.builder import DATAPACKAGE, PROCESSED, Parser, build_sources, parse_source
from sourceledger.fetcher import USER_AGENT, fetch_sources
from sourceledger.ledger import (
    FETCH_LOG,
    RAW,
    FetchError,
    FetchRecord,
    current_record,
    log_fields,
    log_version,
    record_file,
    revision_records,
    revisions,
    without_query,
)
from sourceledger.manifest import (
    MANIFEST,
    Source,
    implemented,
    init,
    load_manifest,
    load_output_format,
    load_package_name,
    load_release_formats,
    output_name,
    printable,
    select,
)
from sourceledger.releaser import CODEMETA, ReleaseError, check_release, release
from sourceledger.report import attribution, one_line, report, status, status_records, validate, verify

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
    "release": "write and check a release folder: data, metadata, SHA256SUMS",
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
    # No abbreviated options, here and in each stage: `--j` for `--json` would break once
    # another option starts with j.
    parser = _ArgumentParser(
        prog=prog,
        description="Fetch, build and validate the sources in sources.toml.",
        epilog=_EPILOG.format(prog=prog),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
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
        "release": [manifest_option, dry_run_option, json_option],
    }
    sub = {
        name: stages.add_parser(
            name, help=text, description=text[0].upper() + text[1:] + ".", parents=parents[name], allow_abbrev=False
        )
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
    sub["record"].set_defaults(check_args=_check_parameters)
    sub["revisions"].add_argument("path", type=Path, metavar="OLD_LOG", help="an earlier copy of data/fetches.jsonl")
    sub["verify"].add_argument("path", type=Path, metavar="DIR", help="the folder of a published dataset")
    sub["release"].add_argument("path", type=Path, nargs="?", metavar="OUT_DIR", help="the new folder to write")
    sub["release"].add_argument(
        "--check", type=Path, metavar="DIR", help="check an existing release folder instead of writing one"
    )
    sub["release"].add_argument(
        "--codemeta",
        type=Path,
        metavar="PATH",
        help="read the dataset's name, licence and authors from this CodeMeta file (default: ./codemeta.json)",
    )
    sub["release"].set_defaults(check_args=_check_release_args)
    for name, stage_parser in sub.items():
        stage_parser.set_defaults(parser=stage_parser, run=_HANDLERS[name])
    return parser


def _time(text: str) -> str:
    """`text` as given, once it reads as an ISO 8601 time with a zone; the library checks that it is past."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        msg = f"{text!r} is not an ISO 8601 time, such as 2026-11-05T09:30:00Z"
        raise argparse.ArgumentTypeError(msg) from None
    if when.tzinfo is None:
        msg = f"{text!r} needs a time zone, such as 2026-11-05T09:30:00Z"
        raise argparse.ArgumentTypeError(msg)
    return text


def _check_release_args(args: argparse.Namespace) -> None:
    """Exit with a usage error unless exactly one of OUT_DIR and --check is given, and --check without --dry-run."""
    if (args.path is None) == (args.check is None):
        args.parser.error("pass OUT_DIR to write a release, or --check DIR to check one")
    if args.check is not None and args.dry_run:
        args.parser.error("--check writes nothing; drop --dry-run")
    if args.check is not None and args.manifest != args.parser.get_default("manifest"):
        args.parser.error("--check reads no manifest; drop --manifest")
    if args.check is not None and args.codemeta is not None:
        args.parser.error("--check reads no codemeta.json; drop --codemeta")


def _check_parameters(args: argparse.Namespace) -> None:
    """Exit with a usage error for a --parameter that is not NAME=VALUE, or a name given twice."""
    if bad := [p for p in args.parameter if not (p.partition("=")[0] and p.partition("=")[1])]:
        args.parser.error(f"--parameter takes NAME=VALUE, not {bad[0]!r}")
    names = [p.partition("=")[0] for p in args.parameter]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        args.parser.error(f"--parameter names {dupes} more than once")


def entry(
    argv: list[str] | None = None,
    *,
    parse: Parser = parse_source,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
    manifest: Path = MANIFEST,
) -> NoReturn:
    """The `sourceledger` command: run `main` with these arguments, and exit as the shell expects.

    A project's own command points its console script at a function that calls this with its
    `parse`, `prog`, `user_agent` and `manifest`, as for `main`. `argv` is the arguments,
    `sys.argv[1:]` when None, so a test can run the command and catch its SystemExit.
    A shell loop stops on Ctrl-C only when the command was killed by the signal; an exit code of
    130 alone does not stop it, so after Ctrl-C this dies of SIGINT. `main` itself returns 130.
    A reader that closes the pipe early, such as `head`, ends the command quietly by SIGPIPE.
    Output is UTF-8, stdout with LF line ends, on every system; `main` leaves the streams as they are.
    """
    if hasattr(signal, "SIGPIPE"):  # not on Windows
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    # Windows writes redirected output in the locale's code page with CRLF; the HTML report says UTF-8.
    for stream, newline in ((sys.stdout, {"newline": "\n"}), (sys.stderr, {})):
        if reconfigure := getattr(stream, "reconfigure", None):
            reconfigure(encoding="utf-8", **newline)
    code = main(argv=argv, parse=parse, prog=prog, user_agent=user_agent, manifest=manifest)
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
    """Run one stage: init, fetch, record, build, validate, status, attribution, revisions, verify, report or release.

    `sourceledger STAGE --help` lists each stage's options. A project's own entry point calls
    this with its `parse`, its name as `prog`, and that name as `user_agent`. `manifest` is the
    sources.toml to read, `./sources.toml` by default; a project that ships it inside its
    package passes that path, and `--manifest PATH` overrides both.

    Returns the exit code the module docstring lists. A usage error exits through SystemExit
    with code 2, as argparse does.
    """
    argv = sys.argv[1:] if argv is None else argv
    args, extra = _parser(prog, manifest).parse_known_args(argv)
    if extra:  # reported by the stage's parser, so the usage and help name the stage
        args.parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if check_args := getattr(args, "check_args", None):
        check_args(args)
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
        argv = [a.replace(url, without_query(url)) for a in run.argv] if url else run.argv
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


# What to do about the file or folder a stage will not overwrite.
_EXISTS_HELP = {
    "init": "edit it, or pass --manifest PATH to write one elsewhere",
    "release": "pass a new OUT_DIR, or move that folder away",
}


# What to pass instead of a stage's PATH operand that is missing.
_MISSING_PATH_HELP = {
    "record": "pass the file another tool downloaded",
    "revisions": f"pass an earlier copy of {FETCH_LOG.as_posix()}",
    "verify": f"pass the folder that holds the dataset's {DATAPACKAGE}",
    "release": "pass the release folder to check",
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
    path = getattr(run.args, "check", None) or getattr(run.args, "path", None)
    if missing and path and missing in (path, path / DATAPACKAGE):
        return _MISSING_PATH_HELP[run.args.stage]
    if missing == run.args.manifest:
        return f"run `{run.prog} init` to write one, or pass --manifest PATH"
    if missing and (missing == FETCH_LOG or missing.name.endswith(".fetch.json")):
        writes = (
            "; it writes the log" if missing == FETCH_LOG else f", or `{run.prog} record` for a file you fetch yourself"
        )
        return f"run `{run.prog} fetch` first{writes}"
    if isinstance(error, FileExistsError):
        return _EXISTS_HELP.get(run.args.stage)
    # A held lock: BlockingIOError on POSIX, PermissionError on Windows.
    if isinstance(error.__cause__, (BlockingIOError, PermissionError)):
        return "wait for the other fetch or record to finish, then run this again"
    return None


def _sources(run: _Run, ids: list[str] | None = None) -> list[Source]:
    """The manifest's sources; a usage error for an id in `ids` (or --only) it does not have."""
    sources = load_manifest(run.args.manifest)
    try:
        select(sources, ids or run.args.only)
    except ValueError as error:
        raise _UsageError(str(error)) from error
    return sources


def _new(record: FetchRecord, before: dict[str, dict[str, Any]]) -> bool:
    """Whether the record's bytes differ from the last ones `before` logged for its source."""
    return before.get(record.source_id, {}).get("sha256") != record.sha256


def _relogged(record: FetchRecord, before: dict[str, dict[str, Any]]) -> bool:
    """Same bytes, logged again because they came in another way, such as fetched after a record."""
    last = before.get(record.source_id)
    return last is not None and not _new(record, before) and log_version(last) != log_version(log_fields(record))


def _json_record(record: FetchRecord, before: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A record under --json: the log line's fields, `new` and `relogged`."""
    return {**log_fields(record), "new": _new(record, before), "relogged": _relogged(record, before)}


def _marker(record: FetchRecord, before: dict[str, dict[str, Any]]) -> str:
    """`new` bytes, the same bytes `relogged` another way, or `unchanged`."""
    return "new" if _new(record, before) else "relogged" if _relogged(record, before) else "unchanged"


def _fetch_line(record: FetchRecord, before: dict[str, dict[str, Any]]) -> str:
    """One line per source, with its `_marker`."""
    marker = _marker(record, before)
    return (
        f"{record.source_id}  {marker:<9}  {record.bytes:>9,} B  {record.sha256[:12]}  {record.vintage}  {record.url}"
    )


def _report_records(run: _Run, records: list[FetchRecord], before: dict[str, dict[str, Any]]) -> None:
    run.result["records"] = [_json_record(r, before) for r in records]
    for record in records:
        run.say(_fetch_line(record, before))
    if relogged := [r.source_id for r in records if _relogged(r, before)]:
        run.say(f"run build: the outputs of {', '.join(relogged)} name the earlier log line")


def _problems(count: int) -> str:
    return f"{count} problem{'' if count == 1 else 's'}"


def _report_problems(run: _Run, problems: list[str]) -> list[str]:
    """The problems and their count, on stdout: they are the stage's result."""
    run.result["problems"] = problems
    for problem in problems:
        # Names from a received folder; --json escapes them itself, and keeps them exact.
        run.say(printable(problem))
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
        todo = implemented(select(sources, run.args.only))
        run.result["dry_run"] = True
        run.result["would_fetch"] = [{"source_id": s["id"], "url": s["url"]} for s in todo]
        for source in todo:
            run.say(f"would fetch {source['id']} from {source['url']}")
        return []
    before: dict[str, dict[str, Any]] = {}
    done: list[FetchRecord] = []
    try:
        fetch_sources(
            sources, user_agent=run.user_agent, only=run.args.only, before=before, progress=_progress(run), done=done
        )
    finally:  # on a failure or Ctrl-C too: the sources that did fetch
        _report_records(run, done, before)
    return []


def _record_stage(run: _Run) -> list[str]:
    args = run.args
    (source,) = select(_sources(run, [args.id]), [args.id])
    before: dict[str, dict[str, Any]] = {}
    result = record_file(
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
        run.result["records"] = [_json_record(result, before)]
        what = f"{_marker(result, before)}, {result.bytes:,} B, sha256 {result.sha256}"
        run.say(f"would record {source['id']} from {args.path.as_posix()}: {what}")
    else:
        _report_records(run, [result], before)
    return []


def _build_stage(run: _Run) -> list[str]:
    sources = _sources(run)
    output_format = load_output_format(run.args.manifest)
    if run.args.dry_run:
        for source in implemented(select(sources, run.args.only)):
            current_record(source, RAW)  # the same refusal build gives for a missing or changed raw file
            run.say(f"would write {(PROCESSED / output_name(source, output_format)).as_posix()}")
        run.say(f"would write {(PROCESSED / DATAPACKAGE).as_posix()}")
        return []
    name = load_package_name(run.args.manifest)
    for out, count in build_sources(
        sources, parse=run.parse, only=run.args.only, output_format=output_format, name=name
    ):
        size = f"{out.stat().st_size} bytes" if count is None else f"{count} row{'' if count == 1 else 's'}"
        run.say(f"{size} -> {out.as_posix()}")
    run.say(f"package -> {(PROCESSED / DATAPACKAGE).as_posix()}")
    return []


def _validate_stage(run: _Run) -> list[str]:
    sources = _sources(run)
    output_format = load_output_format(run.args.manifest)
    name = load_package_name(run.args.manifest)
    return _report_problems(run, validate(sources, only=run.args.only, output_format=output_format, name=name))


def _status_stage(run: _Run) -> list[str]:
    sources = load_manifest(run.args.manifest)
    if run.args.json:
        run.result["sources"] = status_records(sources)
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
        run.result["revisions"] = revision_records(run.args.path)
    else:
        for line in revisions(run.args.path) or ["no revisions"]:
            print(line)
    return []


def _verify_stage(run: _Run) -> list[str]:
    return _report_problems(run, verify(run.args.path))


def _release_stage(run: _Run) -> list[str]:
    if run.args.check is not None:
        problems, run.result["validators"] = check_release(run.args.check)
        for ran in run.result["validators"]:
            run.say(f"checked {ran['file']} with {ran['name']} {ran['version']}")
        return _report_problems(run, problems)
    sources = load_manifest(run.args.manifest)
    if run.args.dry_run:
        run.result["dry_run"] = True
    try:
        result = release(
            sources,
            run.args.path,
            formats=load_release_formats(run.args.manifest),
            output_format=load_output_format(run.args.manifest),
            codemeta=run.args.codemeta or CODEMETA,
            name=load_package_name(run.args.manifest),
            dry_run=run.args.dry_run,
        )
    except ReleaseError as error:  # refused input or findings: the stage's result, as for --check
        run.result["validators"] = getattr(error, "ran", [])
        return _report_problems(run, error.problems)
    run.result.update(result)
    _say_release(run, result)
    return []


def _say_release(run: _Run, result: dict[str, Any]) -> None:
    verb = "would write" if run.args.dry_run else "wrote"
    out = run.args.path.as_posix()
    for name in result["files"]:
        run.say(f"{verb} {out}/{name}")
    for sid in result["excluded"]:
        run.say(f"excluded {sid}: redistribute is not true")
    if not any(name.startswith("data/") for name in result["files"]):
        run.say("nothing to ship: no implemented source has redistribute = true, so data/ is empty")
    for fmt, state in result["formats"].items():
        if state == "skipped":  # noqa: PLR2004
            run.say(f"skipped {fmt}: not in release_formats")
    for ran in result["validators"]:
        run.say(f"checked {ran['file']} with {ran['name']} {ran['version']}")


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
    "release": _release_stage,
}
