"""Fetch or record a dataset's public files with provenance, and release it without files whose terms forbid it.

Every file keeps a provenance record, and every output row names the bytes it came from.

The names below are the public API. Until 1.0, a breaking change to them, to the keys of
sources.toml, or to the fetch-record format comes only in a minor version, listed in the
changelog. The submodules, such as `sourceledger.ledger`, may change in any release.
"""

from sourceledger.builder import Parser, build, parse_csv, parse_json, parse_source, parse_xlsx, read_output
from sourceledger.cli import entry, main
from sourceledger.comparison import align_rows
from sourceledger.fetcher import USER_AGENT, fetch
from sourceledger.ledger import FetchError, FetchRecord, record, revisions, verified
from sourceledger.manifest import Source, load_manifest, load_output_format, load_package_name, load_release_formats
from sourceledger.releaser import (
    ReleaseError,
    RestrictedSourceError,
    ValidatorCouldNotRun,
    ValidatorFailed,
    ValidatorMissing,
    check_release,
    release,
)
from sourceledger.report import attribution, report, status, validate, verify

__all__ = [
    "USER_AGENT",
    "FetchError",
    "FetchRecord",
    "Parser",
    "ReleaseError",
    "RestrictedSourceError",
    "Source",
    "ValidatorCouldNotRun",
    "ValidatorFailed",
    "ValidatorMissing",
    "align_rows",
    "attribution",
    "build",
    "check_release",
    "entry",
    "fetch",
    "load_manifest",
    "load_output_format",
    "load_package_name",
    "load_release_formats",
    "main",
    "parse_csv",
    "parse_json",
    "parse_source",
    "parse_xlsx",
    "read_output",
    "record",
    "release",
    "report",
    "revisions",
    "status",
    "validate",
    "verified",
    "verify",
]
