"""A self-contained, offline HTML view of a built dataset's provenance."""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sourceledger.ledger import DATAPACKAGE, FETCH_LOG, PROCESSED, Source, _last_records, verify


def _url(value: object) -> str:
    """Display HTTP URLs without user information, query parameters or fragments."""
    try:
        url = urlsplit(str(value))
        if url.scheme in ("http", "https") and url.hostname:
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
    except ValueError:
        pass
    return "Local or non-HTTP URL omitted"


def _fields(values: dict[str, object]) -> str:
    return (
        "<dl>"
        + "".join(f"<dt>{escape(key)}</dt><dd>{escape(str(value))}</dd>" for key, value in values.items())
        + "</dl>"
    )


def report(sources: list[Source], *, processed: Path = PROCESSED, log: Path = FETCH_LOG) -> str:
    """Return standalone HTML describing built outputs, their sources and integrity checks.

    Reads the package, its files and the fetch log without network access or including raw
    data. Metadata is escaped; URLs omit user information, queries and fragments. Header
    names and values are omitted. Free-text names and terms must be reviewed before sharing.
    Expected input errors raise OSError or ValueError before any HTML is returned.
    Missing fetch records and stale or changed outputs are described in the report.
    """
    package = json.loads((processed / DATAPACKAGE).read_text(encoding="utf-8"))
    resources = package.get("resources") if isinstance(package, dict) else None
    if not isinstance(resources, list) or not all(isinstance(r, dict) for r in resources):
        msg = "datapackage.json needs a list of resource objects; re-run build"
        raise ValueError(msg)
    for resource in resources:
        name = resource.get("path")
        if (
            not isinstance(name, str)
            or urlsplit(name).scheme
            or Path(name).is_absolute()
            or os.pardir in Path(name).parts
        ):
            msg = "datapackage.json resource paths must be relative files inside the dataset"
            raise ValueError(msg)
    records = _last_records(log)
    problems = verify(processed)
    by_id = {source["id"]: source for source in sources}
    sections = []
    for number, resource in enumerate(resources):
        sid = str(resource.get("name", "unnamed"))
        source = by_id.get(sid, {})
        stamp = resource.get("sourceledger")
        stamp = stamp if isinstance(stamp, dict) else {}
        latest = records.get(sid)
        if not source:
            problems.append(f"{sid}: source is missing from the manifest")
        if latest is None:
            problems.append(f"{sid}: no fetch record in the log")
        elif stamp.get("raw_sha256") != latest["sha256"]:
            problems.append(f"{sid}: output was built from an earlier fetch; re-run build")
        origins = resource.get("sources")
        built_url = (
            origins[0].get("path") if isinstance(origins, list) and origins and isinstance(origins[0], dict) else None
        )
        if source and (
            built_url != source.get("url") or stamp.get("vintage") != str(source.get("vintage", "not stated"))
        ):
            problems.append(f"{sid}: manifest differs from the last build; re-run fetch and build")
        file_fields = {
            "File": resource.get("path", "not stated"),
            "Size in bytes": resource.get("bytes", "not stated"),
            "Output SHA-256": resource.get("hash", "not stated"),
        }
        source_fields = {
            "URL": _url(source.get("url", "")),
            "Fetched at": stamp.get("retrieved_at", "not stated"),
            "Vintage": stamp.get("vintage", "not stated"),
            "Raw SHA-256": stamp.get("raw_sha256", "not stated"),
            "Final URL": _url(stamp.get("final_url", "")),
            "Licence": source.get("license", "not stated"),
            "Terms": source.get("terms", "not stated"),
            "Terms URL": _url(source.get("terms_url", "")),
            "Raw redistribution allowed": str(stamp.get("redistribute", "not stated")),
        }
        sections.append(
            f"<section><h2>{escape(sid)}</h2>{_fields(file_fields)}"
            f'<p><a href="#source-{number}">Source of this output</a></p>'
            f'<h3 id="source-{number}">{escape(str(source.get("name", sid)))}</h3>'
            f"{_fields(source_fields)}</section>"
        )
    result = (
        "<h2>Integrity problems</h2><ul>" + "".join(f"<li>{escape(p)}</li>" for p in problems) + "</ul>"
        if problems
        else "<h2>Integrity checks passed</h2><p>Listed files match their package hashes and sizes.</p>"
    )
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Dataset provenance</title>
<style>
body { max-width: 72ch; margin: 2rem auto; padding: 0 1rem; color: #243346; background: #fcfbf8;
font: 1rem/1.6 Georgia, serif; }
a { color: #215e96; } section { border-top: 1px solid #ccc; margin-top: 2rem; }
dt { font-weight: bold; } dd { margin: 0 0 1rem; overflow-wrap: anywhere; }
:focus-visible { outline: 2px solid #215e96; outline-offset: 3px; }
</style></head><body><main><h1>Dataset provenance</h1>
<p>This report checks file integrity and records where outputs came from.
It does not grant permission to publish data,
prove scientific correctness, or authenticate the package.
Obtain the package and this report from a source you trust.</p>
<p>Source details come from the current manifest; fetch stamps describe the last build.
No raw values or header credentials are included. URLs omit user information, query parameters and fragments.
Review names and reuse terms before sharing this report. Local and non-HTTP URLs are omitted.</p>
"""
        + result
        + "".join(sections)
        + "</main></body></html>\n"
    )
