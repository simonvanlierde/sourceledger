"""Fetch, build and validate the public sources a dataset is built from.

Every download keeps a provenance record, and every output row names the bytes it came from.

The names below are the public API. Until 1.0, a breaking change to them, to the keys of
sources.toml, or to the fetch-record format comes only in a minor version, listed in the
changelog. `sourceledger.ledger` and `sourceledger.cli` may change in any release.
"""

from sourceledger.cli import main
from sourceledger.ledger import (
    USER_AGENT,
    FetchError,
    FetchRecord,
    Parser,
    Source,
    attribution,
    build,
    fetch,
    load_manifest,
    parse_csv,
    parse_json,
    parse_source,
    parse_xlsx,
    read_output,
    revisions,
    status,
    validate,
    verified,
    verify,
)

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
    "revisions",
    "status",
    "validate",
    "verified",
    "verify",
]
