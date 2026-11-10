"""Fetch, verify and validate the public sources a dataset is built from.

Every download keeps a provenance record, and every output row names the bytes it came from.

The names below are the public API. Until 1.0, a breaking change to them, to the keys of
sources.toml, or to the fetch-record format comes only in a minor version, listed in the
changelog. `sourceledger.ledger` and `sourceledger.cli` may change in any release.
"""

from sourceledger.builder import Parser, build, parse_csv, parse_json, parse_source, parse_xlsx, read_output
from sourceledger.cli import main
from sourceledger.fetcher import USER_AGENT, fetch
from sourceledger.ledger import FetchError, FetchRecord, record, revisions, verified
from sourceledger.manifest import Source, load_manifest
from sourceledger.report import attribution, report, status, validate, verify

__all__ = [
    "USER_AGENT",
    "FetchError",
    "FetchRecord",
    "Parser",
    "Source",
    "attribution",
    "build",
    "fetch",
    "load_manifest",
    "main",
    "parse_csv",
    "parse_json",
    "parse_source",
    "parse_xlsx",
    "read_output",
    "record",
    "report",
    "revisions",
    "status",
    "validate",
    "verified",
    "verify",
]
