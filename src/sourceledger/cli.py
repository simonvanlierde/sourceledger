"""The command line: `<project> fetch|build|validate|revisions`, with the project's own parser."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from sourceledger.ledger import (
    FETCH_LOG,
    USER_AGENT,
    FetchError,
    FetchRecord,
    Parser,
    build,
    fetch,
    load_manifest,
    logged_versions,
    one_line,
    parse_source,
    read_output,
    revisions,
    validate,
)


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
) -> int:
    """Run one stage: fetch, build, validate, or revisions against an earlier copy of the fetch log.

    A project's own entry point calls this with its `parse`, its name as `prog`, and that name
    as `user_agent`. Returns the exit code: 0, or 1 for any failure or validation problem.
    """
    parser = argparse.ArgumentParser(prog=prog, description="Fetch, build and validate the sources in sources.toml.")
    parser.add_argument("stage", choices=["fetch", "build", "validate", "revisions"])
    parser.add_argument("before", nargs="?", type=Path, help="revisions only: an earlier copy of data/fetches.jsonl")
    args = parser.parse_args(argv)
    # Retries and recovered fetch logs are reported as warnings, on stderr.
    logging.basicConfig(format=f"{prog}: %(message)s", level=logging.WARNING)
    before: dict[str, str] = {}
    try:
        # What the log held before this run, so each fetch line can say new or unchanged.
        before = logged_versions(FETCH_LOG)
        problems = _run(args.stage, args.before, parse, user_agent, parser, before)
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


def _run(
    stage: str,
    earlier_log: Path | None,
    parse: Parser,
    user_agent: str,
    parser: argparse.ArgumentParser,
    logged: dict[str, str],
) -> list[str]:
    """Run `stage`, print its report, and return its problems: only validate has any."""
    match stage:
        case "revisions":
            if earlier_log is None:
                parser.error("revisions needs an earlier copy of the fetch log")
            # Not a failure: a revised source is news, and the tripwire test decides whether it breaks anything.
            for line in revisions(earlier_log):
                print(line)
        case "fetch":
            for record in fetch(load_manifest(), user_agent=user_agent):
                print(_fetch_line(record, logged))
        case "build":
            for path in build(load_manifest(), parse=parse):
                print(f"{len(read_output(path))} rows -> {path}")
        case _:
            problems = validate(load_manifest())
            print(f"{len(problems)} problems")
            return problems
    return []
