"""The command line: `<project> fetch|build|validate` and the other stages, with the project's own parser."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from sourceledger.ledger import (
    DATAPACKAGE,
    FETCH_LOG,
    FILE_FORMAT,
    MANIFEST,
    PROCESSED,
    USER_AGENT,
    FetchError,
    FetchRecord,
    Parser,
    _revision_records,
    _status_records,
    attribution,
    build,
    fetch,
    init,
    load_manifest,
    logged_versions,
    one_line,
    parse_source,
    raw_name,
    revisions,
    status,
    validate,
    verify,
)

_STAGES = ("init", "fetch", "build", "validate", "status", "attribution", "revisions", "verify")
# Stages that take --only.
_SELECTABLE = ("fetch", "build", "validate")
_JSON_STAGES = ("status", "validate", "revisions")
# Stages that take a path, and what it is.
_NEEDS_PATH = {"revisions": "an earlier copy of the fetch log", "verify": "the folder of a published dataset"}


def _fetch_line(record: FetchRecord, before: dict[str, str]) -> str:
    """One line per fetched source; `new` when its bytes differ from the last ones `before` logged."""
    marker = "unchanged" if before.get(record.source_id) == record.sha256 else "new"
    return (
        f"{record.source_id}  {marker:<9}  {record.bytes:>9,} B  {record.sha256[:12]}  {record.vintage}  {record.url}"
    )


def main(
    argv: list[str] | None = None,
    *,
    parse: Parser = parse_source,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
    manifest: Path = MANIFEST,
) -> int:
    """Run one stage: init, fetch, build, validate, status, attribution, revisions or verify.

    `--json` prints structured status, validate and revisions reports, including runtime errors.
    `revisions` takes an earlier copy of the fetch log, and `verify` the folder of a published
    dataset. `--only ID`, repeatable, restricts fetch, build and validate to those sources.

    A project's own entry point calls this with its `parse`, its name as `prog`, and that name
    as `user_agent`. `manifest` is the sources.toml to read, `./sources.toml` by default; a
    project that ships it inside its package passes that path, and `--manifest PATH` overrides
    both. Returns the exit code: 0, or 1 for any failure or validation problem.
    """
    parser = argparse.ArgumentParser(prog=prog, description="Fetch, build and validate the sources in sources.toml.")
    parser.add_argument("stage", choices=_STAGES)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="revisions: an earlier copy of data/fetches.jsonl; verify: the folder of a published dataset",
    )
    parser.add_argument(
        "--only", action="append", metavar="ID", help="fetch, build and validate: only this source (repeatable)"
    )
    parser.add_argument(
        "--manifest", type=Path, default=manifest, metavar="PATH", help="the manifest to read (default: %(default)s)"
    )
    parser.add_argument("--json", action="store_true", help="status, validate and revisions: print JSON")
    args = parser.parse_args(argv)
    if args.json and args.stage not in _JSON_STAGES:
        parser.error(f"--json works with {', '.join(_JSON_STAGES)}, not {args.stage}")
    if args.only is not None and args.stage not in _SELECTABLE:
        parser.error(f"--only works with {', '.join(_SELECTABLE)}, not {args.stage}")
    if args.stage in _NEEDS_PATH and args.path is None:
        parser.error(f"{args.stage} needs {_NEEDS_PATH[args.stage]}")
    # Retries and recovered fetch logs are reported as warnings, on stderr.
    logging.basicConfig(format=f"{prog}: %(message)s", level=logging.WARNING)
    if args.json:
        return _json_run(args.stage, args.path, args.only, args.manifest)
    # Filled by fetch, for the lines of a run that fails.
    before: dict[str, str] = {}
    try:
        problems = _run(args.stage, args.path, args.only, parse, user_agent, before, args.manifest)
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
                problems = validate(load_manifest(manifest), only=only)
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
) -> list[str]:
    """Run `stage`, print its report, and return its problems: only validate and verify have any.

    fetch first fills `logged` with what the log held before this run, so each fetch line can
    say new or unchanged. Only fetch, status and revisions read the log, so a damaged one
    breaks only those.
    """
    match stage, path:
        case "init", _:
            init(manifest)
            print(f"wrote {manifest}")
        case "revisions", Path():
            # Not a failure: a revised source is news, and the tripwire test decides whether it breaks anything.
            for line in revisions(path):
                print(line)
        case "verify", Path():
            problems = verify(path)
            print(f"{len(problems)} problems")
            return problems
        case "fetch", _:
            logged.update(logged_versions(FETCH_LOG))
            for record in fetch(load_manifest(manifest), user_agent=user_agent, only=only):
                print(_fetch_line(record, logged))
        case "build", _:
            sources = load_manifest(manifest)
            files = {
                PROCESSED / raw_name(source)
                for source in sources
                if source.get("format") == FILE_FORMAT and source.get("url")
            }
            for out in build(sources, parse=parse, only=only):
                if out in files:
                    print(f"{out.stat().st_size} bytes -> {out}")
                else:
                    with out.open(newline="", encoding="utf-8") as fh:
                        rows = sum(1 for _ in csv.DictReader(fh))
                    print(f"{rows} rows -> {out}")
            print(f"package -> {PROCESSED / DATAPACKAGE}")
        case "status", _:
            for line in status(load_manifest(manifest)):
                print(line)
        case "attribution", _:
            for line in attribution(load_manifest(manifest)):
                print(line)
        case _:
            problems = validate(load_manifest(manifest), only=only)
            print(f"{len(problems)} problems")
            return problems
    return []
