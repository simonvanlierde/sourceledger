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
    MANIFEST,
    PROCESSED,
    USER_AGENT,
    FetchError,
    FetchRecord,
    Parser,
    _build,
    _fetch,
    _load_output_format,
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
from sourceledger.retrieval import expand_requests, request_key
from sourceledger.update import update

_STAGES = ("init", "fetch", "build", "update", "validate", "status", "attribution", "revisions", "verify", "report")
# Stages that take --only.
_SELECTABLE = ("fetch", "build", "update", "validate")
_JSON_STAGES = ("status", "validate", "revisions")
# Stages that take a path, and what it is.
_NEEDS_PATH = {"revisions": "an earlier copy of the fetch log", "verify": "the folder of a published dataset"}


def _fetch_line(record: FetchRecord, before: dict[str, str]) -> str:
    """One line per fetched source; `new` when its bytes differ from the last ones `before` logged."""
    key = request_key({"id": record.source_id, "request_id": record.request_id})
    marker = "unchanged" if before.get(key) == record.sha256 else "new"
    return f"{key}  {marker:<9}  {record.bytes:>9,} B  {record.sha256[:12]}  {record.vintage}  {record.url}"


def main(  # noqa: C901  # argument validation and reporting for each stage
    argv: list[str] | None = None,
    *,
    parse: Parser = parse_source,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
    manifest: Path = MANIFEST,
) -> int:
    """Run init, fetch, build, update, validate, status, attribution, revisions, verify or report.

    `report` writes standalone HTML to stdout for the current dataset.
    `--json` prints structured status, validate and revisions reports, including runtime errors.
    `revisions` takes an earlier copy of the fetch log, and `verify` the folder of a published
    dataset. `--only ID`, repeatable, restricts fetch, build, update and validate to those sources.

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
        "--only", action="append", metavar="ID", help="fetch, build, update and validate: only this source (repeatable)"
    )
    parser.add_argument(
        "--manifest", type=Path, default=manifest, metavar="PATH", help="the manifest to read (default: %(default)s)"
    )
    parser.add_argument("--json", action="store_true", help="status, validate and revisions: print JSON")
    parser.add_argument("--workers", type=int, choices=(1, 2), help="fetch/update: simultaneous downloads (default: 1)")
    parser.add_argument("--root", type=Path, help="update: generation store (default: data/updates)")
    parser.add_argument("--max-bytes", type=int, help="update: aggregate staging budget (default: 1073741824)")
    args = parser.parse_intermixed_args(argv)
    if args.workers is not None and args.stage not in ("fetch", "update"):
        parser.error("--workers works with fetch and update only")
    if (args.root is not None or args.max_bytes is not None) and args.stage != "update":  # noqa: PLR2004
        parser.error("--root and --max-bytes work with update only")
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
        problems = _run(
            args.stage,
            args.path,
            args.only,
            parse,
            user_agent,
            before,
            args.manifest,
            args.workers or 1,
            (
                args.root if args.root is not None else Path("data/updates"),
                args.max_bytes if args.max_bytes is not None else 1024**3,
            ),
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
    workers: int = 1,
    update_options: tuple[Path, int] = (Path("data/updates"), 1024**3),
) -> list[str]:
    """Run `stage`, print its report, and return its problems: only validate and verify have any.

    fetch first fills `logged` with what the log held before this run, so each fetch line can
    say new or unchanged. Fetch, status, revisions and report read the log; a damaged log
    breaks those stages.
    """
    match stage, path:
        case "init", _:
            init(manifest)
            print(f"wrote {manifest}")
        case "revisions", Path():
            # Not a failure: a revised source is news, and the tripwire test decides whether it breaks anything.
            for line in revisions(path):
                print(line)
        case "report", _:
            print(report(load_manifest(manifest)), end="")
        case "verify", Path():
            problems = verify(path)
            print(f"{len(problems)} problems")
            return problems
        case "fetch", _:
            for record in _fetch(
                expand_requests(_select(load_manifest(manifest), only), output_format=_load_output_format(manifest)),
                user_agent=user_agent,
                before=logged,
                workers=workers,
            ):
                print(_fetch_line(record, logged))
        case "update", _:
            generation = update(
                load_manifest(manifest),
                root=update_options[0],
                max_bytes=update_options[1],
                parse=parse,
                user_agent=user_agent,
                only=only,
                workers=workers,
                output_format=_load_output_format(manifest),
            )
            print(f"published -> {generation}")
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
            print(f"{len(problems)} problems")
            return problems
    return []
