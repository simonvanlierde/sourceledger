"""The command line: `<project> fetch|build|validate` and the other stages, with the project's own parser."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from importlib.metadata import version
from pathlib import Path

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
    _build,
    _fetch,
    _load_output_format,
    _record_file,
    _revision_records,
    _select,
    _status_records,
    attribution,
    init,
    load_manifest,
    one_line,
    parse_source,
    revisions,
    status,
    validate,
    verify,
)
from sourceledger.report import report

_STAGES = ("init", "fetch", "record", "build", "validate", "status", "attribution", "revisions", "verify", "report")
# Stages that take --only.
_SELECTABLE = ("fetch", "build", "validate")
_JSON_STAGES = ("status", "validate", "revisions")
# Stages that take operands, and what they are.
_OPERANDS = {
    "revisions": ("an earlier copy of the fetch log",),
    "verify": ("the folder of a published dataset",),
    "record": ("a source id", "the file another tool downloaded"),
}
_RECORD = "record"
_RECORD_OPTIONS = ("url", "retrieved_at", "etag", "last_modified", "parameter")


def _fetch_line(record: FetchRecord, before: dict[str, str]) -> str:
    """One line per fetched source; `new` when its bytes differ from the last ones `before` logged."""
    marker = "unchanged" if before.get(record.source_id) == record.sha256 else "new"
    return (
        f"{record.source_id}  {marker:<9}  {record.bytes:>9,} B  {record.sha256[:12]}  {record.vintage}  {record.url}"
    )


_EPILOG = """\
a first run:
  {prog} init        write a starter sources.toml
  {prog} fetch       download every source and log it
  {prog} build       write data/processed/ and datapackage.json
  {prog} validate    check the outputs against the log and the manifest

docs: https://simonvanlierde.com/sourceledger/reference.html
"""


def _problems(count: int) -> str:
    return f"{count} problem{'' if count == 1 else 's'}"


def main(  # argument validation and reporting for each stage
    argv: list[str] | None = None,
    *,
    parse: Parser = parse_source,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
    manifest: Path = MANIFEST,
) -> int:
    """Run one stage: init, fetch, record, build, validate, status, attribution, revisions, verify or report.

    `report` writes standalone HTML to stdout for the current dataset.
    `--json` prints structured status, validate and revisions reports, including runtime errors.
    `revisions` takes an earlier copy of the fetch log, and `verify` the folder of a published
    dataset. `--only ID`, repeatable, restricts fetch, build and validate to those sources.
    `record ID PATH --retrieved-at TIME` takes in a file another tool downloaded, as `record()` does.

    A project's own entry point calls this with its `parse`, its name as `prog`, and that name
    as `user_agent`. `manifest` is the sources.toml to read, `./sources.toml` by default; a
    project that ships it inside its package passes that path, and `--manifest PATH` overrides
    both. Returns the exit code: 0, or 1 for any failure or validation problem.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Fetch, build and validate the sources in sources.toml.",
        epilog=_EPILOG.format(prog=prog),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sourceledger {version('sourceledger')}")
    parser.add_argument("stage", choices=_STAGES)
    parser.add_argument(
        "operands",
        nargs="*",
        metavar="ARG",
        help="revisions: an earlier copy of data/fetches.jsonl; verify: the folder of a published dataset; "
        "record: a source id and the file another tool downloaded",
    )
    parser.add_argument(
        "--only", action="append", metavar="ID", help="fetch, build and validate: only this source (repeatable)"
    )
    parser.add_argument(
        "--manifest", type=Path, default=manifest, metavar="PATH", help="the manifest to read (default: %(default)s)"
    )
    parser.add_argument("--json", action="store_true", help="status, validate and revisions: print JSON")
    parser.add_argument("--url", help="record: where the file came from (default: the source's url)")
    parser.add_argument(
        "--retrieved-at",
        metavar="TIME",
        help="record: when it was downloaded, ISO 8601 with a zone, such as 2026-11-05T09:30:00Z; "
        "without it the record states no download time",
    )
    parser.add_argument("--etag", default="", help="record: the ETag the server sent")
    parser.add_argument(
        "--last-modified", default="", metavar="HTTP_DATE", help="record: the Last-Modified the server sent"
    )
    parser.add_argument(
        "--parameter", action="append", default=[], metavar="NAME=VALUE", help="record: request metadata (repeatable)"
    )
    args = parser.parse_intermixed_args(argv)
    _check(parser, args)
    args.path = Path(args.operands[-1]) if args.operands else None
    # Retries and recovered fetch logs are reported as warnings, on stderr.
    logging.basicConfig(format=f"{prog}: %(message)s", level=logging.WARNING)
    try:
        return _main(args, parse, prog, user_agent)
    except KeyboardInterrupt:
        # A fetch moves each file into place whole, so what finished is kept and logged.
        print(f"{args.stage}: interrupted; what finished before is kept", file=sys.stderr)
        return 130


def _main(args: argparse.Namespace, parse: Parser, prog: str, user_agent: str) -> int:
    """Run the checked stage and report it; the exit code."""
    if args.json:
        return _json_run(args.stage, args.path, args.only, args.manifest)
    # Filled by fetch, for the lines of a run that fails.
    before: dict[str, str] = {}
    try:
        problems = (
            _record(args, before)
            if args.stage == _RECORD
            else _run(args.stage, args.path, args.only, parse, user_agent, before, args.manifest, prog)
        )
    except FetchError as error:
        for record in error.records:
            print(_fetch_line(record, before))
        for failure in error.failures:
            print(f"{args.stage}: {failure}", file=sys.stderr)
        return 1
    # OSError: a missing file or a failed download. ValueError: a bad manifest, a download
    # that fails its checks, or a stale fetch. csv.Error: a source that is not readable CSV.
    except (OSError, ValueError, csv.Error) as error:
        print(f"{args.stage}: {one_line(error)}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def _check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Exit with a usage error for an option or operand the stage does not take."""
    if args.json and args.stage not in _JSON_STAGES:
        parser.error(f"--json works with {', '.join(_JSON_STAGES)}, not {args.stage}")
    if args.only is not None and args.stage not in _SELECTABLE:
        parser.error(f"--only works with {', '.join(_SELECTABLE)}, not {args.stage}")
    wanted = _OPERANDS.get(args.stage, ())
    if len(args.operands) != len(wanted):
        parser.error(f"{args.stage} needs {' and '.join(wanted)}" if wanted else f"{args.stage} takes no arguments")
    if args.stage != _RECORD and any(getattr(args, name) for name in _RECORD_OPTIONS):
        parser.error(
            f"--url, --retrieved-at, --etag, --last-modified and --parameter work with record, not {args.stage}"
        )
    if bad := [p for p in args.parameter if not (p.partition("=")[0] and p.partition("=")[1])]:
        parser.error(f"--parameter takes NAME=VALUE, not {bad[0]!r}")
    names = [p.partition("=")[0] for p in args.parameter]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        parser.error(f"--parameter names {dupes} more than once")


def _record(args: argparse.Namespace, before: dict[str, str]) -> list[str]:
    """Record one file and print its line, as fetch prints one. Never has problems to return."""
    (source,) = _select(load_manifest(args.manifest), [args.operands[0]])
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
    )
    print(_fetch_line(result, before))
    return []


def _json_run(stage: str, path: Path | None, only: list[str] | None, manifest: Path) -> int:
    """Write one JSON report, including expected runtime failures, and return the stage's exit code."""
    report: dict[str, object] = {"stage": stage, "errors": []}
    failed = False
    try:
        match stage, path:
            case "status", _:
                report["sources"] = _status_records(load_manifest(manifest))
            case "revisions", Path():
                report["revisions"] = _revision_records(path)
            case _:
                problems = validate(load_manifest(manifest), only=only, output_format=_load_output_format(manifest))
                report["problems"] = problems
                failed = bool(problems)
    except (OSError, ValueError) as error:
        report["errors"] = [one_line(error)]
        failed = True
    print(json.dumps(report))
    return int(failed)


def _run(  # noqa: C901, PLR0912  # one case per stage
    stage: str,
    path: Path | None,
    only: list[str] | None,
    parse: Parser,
    user_agent: str,
    logged: dict[str, str],
    manifest: Path,
    prog: str,
) -> list[str]:
    """Run `stage`, print its report, and return its problems: only validate and verify have any.

    fetch first fills `logged` with what the log held before this run, so each fetch line can
    say new or unchanged. Fetch, status, revisions and report read the log; a damaged log
    breaks those stages.
    """
    match stage, path:
        case "init", _:
            init(manifest)
            print(f"wrote {manifest}; fill in your sources, then run: {prog} fetch")
        case "revisions", Path():
            # Not a failure: a revised source is news, and the tripwire test decides whether it breaks anything.
            for line in revisions(path):
                print(line)
        case "report", _:
            print(report(load_manifest(manifest)), end="")
        case "verify", Path():
            problems = verify(path)
            print(_problems(len(problems)))
            return problems
        case "fetch", _:
            for record in _fetch(load_manifest(manifest), user_agent=user_agent, only=only, before=logged):
                print(_fetch_line(record, logged))
        case "build", _:
            sources = load_manifest(manifest)
            for out, count in _build(sources, parse=parse, only=only, output_format=_load_output_format(manifest)):
                if count is None:
                    print(f"{out.stat().st_size} bytes -> {out}")
                else:
                    print(f"{count} rows -> {out}")
            print(f"package -> {PROCESSED / DATAPACKAGE}")
        case "status", _:
            for line in status(load_manifest(manifest)):
                print(line)
        case "attribution", _:
            for line in attribution(load_manifest(manifest)):
                print(line)
        case _:
            problems = validate(load_manifest(manifest), only=only, output_format=_load_output_format(manifest))
            print(_problems(len(problems)))
            return problems
    return []
