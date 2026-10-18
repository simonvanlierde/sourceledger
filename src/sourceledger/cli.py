"""The command line: `<project> fetch|build|validate|revisions`, with the project's own parser."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from sourceledger.ledger import (
    USER_AGENT,
    Parser,
    build,
    fetch,
    load_manifest,
    one_line,
    parse_csv,
    read_output,
    revisions,
    validate,
)


def main(
    argv: list[str] | None = None,
    *,
    parse: Parser = parse_csv,
    prog: str = "sourceledger",
    user_agent: str = USER_AGENT,
) -> int:
    """Run one stage: fetch, build, validate, or revisions against an earlier copy of the fetch log.

    A project's own entry point calls this with its `parse`, its name as `prog`, and that name as `user_agent`.
    """
    parser = argparse.ArgumentParser(prog=prog, description="Fetch, build and validate the sources in sources.toml.")
    parser.add_argument("stage", choices=["fetch", "build", "validate", "revisions"])
    parser.add_argument("before", nargs="?", type=Path, help="revisions only: an earlier copy of data/fetches.jsonl")
    args = parser.parse_args(argv)
    stage = args.stage
    problems: list[str] = []  # only validate reports any
    try:
        sources = load_manifest()
        match stage:
            case "revisions":
                if args.before is None:
                    parser.error("revisions needs an earlier copy of the fetch log")
                # Not a failure: a revised source is news, and the tripwire test decides whether it breaks anything.
                for line in revisions(args.before):
                    print(line)
            case "fetch":
                for r in fetch(sources, user_agent=user_agent):
                    print(f"{r.source_id}  {r.bytes:>9,} B  {r.sha256[:12]}  {r.vintage}  {r.url}")
            case "build":
                for path in build(sources, parse=parse):
                    print(f"{len(read_output(path))} rows -> {path}")
            case _:
                problems = validate(sources)
                print(f"{len(problems)} problems")
    # OSError: a missing file or a failed download. ValueError: a bad manifest, a download
    # that fails its checks, or a stale fetch. csv.Error: a source that is not readable CSV.
    except (OSError, ValueError, csv.Error) as error:
        print(f"{stage}: {one_line(error)}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0
