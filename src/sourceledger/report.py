"""A self-contained, offline HTML view of a built dataset's provenance."""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sourceledger.ledger import DATAPACKAGE, FETCH_LOG, PROCESSED, Source, _last_records, implemented, verify


def _url(value: object, empty: str) -> str:
    """Display HTTP URLs without user information, query parameters or fragments; `empty` when there is none."""
    if value in ("", None):
        return empty
    try:
        url = urlsplit(str(value))
        if url.scheme in ("http", "https") and url.hostname:
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
    except ValueError:
        pass
    return "omitted: not an HTTP URL"


def _times(stamp: dict[str, object]) -> dict[str, object]:
    """When the bytes were fetched; for a file another tool downloaded, when it says it did and when it was recorded."""
    if not stamp.get("recorded_at"):
        return {"Fetched at": stamp.get("retrieved_at", "not stated")}
    return {
        "Downloaded by another tool at": stamp.get("retrieved_at") or "not stated by that tool",
        "Recorded at": stamp["recorded_at"],
    }


def _parameters(stamp: dict[str, object]) -> dict[str, object]:
    """The request a recorded file answered, one `name=value` each; nothing for a fetched file."""
    parameters = stamp.get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        return {}
    return {
        "Request parameters": _Markup(" ".join(f"<code>{escape(f'{k}={v}')}</code>" for k, v in parameters.items()))
    }


class _Markup(str):
    """A value already escaped into HTML."""

    __slots__ = ()


def _allowed(value: object) -> str:
    """A damaged package can hold any JSON here; only true and false are answers."""
    return ("yes" if value else "no") if isinstance(value, bool) else _UNSTATED


# For what the manifest leaves out: says where to add it.
_UNSTATED = "not stated in sources.toml"


def _fields(values: dict[str, object]) -> str:
    return (
        "<dl>"
        + "".join(
            f"<dt>{escape(key)}</dt><dd>{value if isinstance(value, _Markup) else escape(str(value))}</dd>"
            for key, value in values.items()
        )
        + "</dl>"
    )


def report(sources: list[Source], *, processed: Path = PROCESSED, log: Path = FETCH_LOG) -> str:
    """Return standalone HTML describing built outputs, their sources and integrity checks.

    Reads the package, its files and the fetch log without network access or including raw
    data. Metadata is escaped; URLs omit user information, queries and fragments. Header
    names and values are omitted. Free-text names, request parameters and terms must be reviewed before sharing.
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
    listed = sorted(str(resource.get("name")) for resource in resources)
    expected = sorted(source["id"] for source in implemented(sources))
    if listed != expected:
        problems.append(f"The package lists {listed}, not the implemented sources {expected}; re-run build")
    by_id = {source["id"]: source for source in sources}
    sections, index = [], []
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
            "SHA-256 of the output": resource.get("hash", "not stated"),
        }
        source_fields = {
            "URL": _url(source.get("url", ""), _UNSTATED),
            **_times(stamp),
            **_parameters(stamp),
            "Vintage": stamp.get("vintage", "not stated"),
            "SHA-256 of the downloaded file": stamp.get("raw_sha256", "not stated"),
            "Served from": _url(stamp.get("final_url", ""), "the URL above"),
            "Licence": source.get("license", _UNSTATED),
            "Terms": source.get("terms", _UNSTATED),
            "Terms URL": _url(source.get("terms_url", ""), _UNSTATED),
            "Raw redistribution allowed": _allowed(stamp.get("redistribute")),
        }
        sections.append(
            f'<section><h3 id="output-{number}">{escape(sid)}</h3>{_fields(file_fields)}'
            f'<p><a href="#source-{number}">Source of this output</a></p>'
            f'<h4 id="source-{number}">{escape(str(source.get("name", sid)))}</h4>'
            f'{_fields(source_fields)}<p><a href="#outputs">Back to the outputs</a></p></section>'
        )
        index.append(f'<li><a href="#output-{number}">{escape(sid)}</a></li>')
    result = (
        '<div class="result failed"><h2>Integrity problems</h2><ul>'
        + "".join(f"<li>{escape(p)}</li>" for p in problems)
        + "</ul></div>"
        if problems
        else '<div class="result"><h2>Integrity checks passed</h2>'
        "<p>Listed files match their package hashes and sizes.</p></div>"
    )
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Dataset provenance</title>
<style>
:root { color-scheme: light dark; --ink: #243346; --page: #fcfbf8; --link: #215e96; --rule: #ccc;
--ok: #2f6b3a; --bad: #a3281e; }
@media (prefers-color-scheme: dark) { :root { --ink: #e4e8ee; --page: #182231; --link: #9cc7f0; --rule: #4a5a70;
--ok: #8fd19e; --bad: #f2a39b; } }
body { max-width: 72ch; margin: 2rem auto; padding: 0 1rem; color: var(--ink); background: var(--page);
font: 1rem/1.6 Georgia, serif; }
a { color: var(--link); } section { border-top: 1px solid var(--rule); margin-top: 2rem; }
.result { border: 2px solid var(--ok); padding: 0 1rem; } .result.failed { border-color: var(--bad); }
dt { font-weight: bold; } dd { margin: 0 0 1rem; } h1, h2, h3, h4, li, dd { overflow-wrap: anywhere; }
@media (min-width: 40rem) { dl { display: grid; grid-template-columns: 14rem 1fr; gap: 0 1rem; } }
:focus-visible { outline: 2px solid var(--link); outline-offset: 3px; }
@media print { :root { color-scheme: light; --ink: #000; --page: #fff; } section { break-inside: avoid; }
a[href^="#"] { display: none; } }
</style></head><body><main><h1>Dataset provenance</h1>
<p>This report checks file integrity and records where outputs came from.
It does not grant permission to publish data,
prove scientific correctness, or authenticate the package.
Obtain the package and this report from a source you trust.</p>
<p>Source details come from the current manifest; fetch stamps describe the last build.
No raw values or header credentials are included. URLs omit user information, query parameters and fragments.
Review names, request parameters and reuse terms before sharing this report.
URLs that are not HTTP or HTTPS are omitted.</p>
"""
        + result
        + '<h2 id="outputs">Outputs</h2><ul class="outputs">'
        + "".join(index)
        + "</ul>"
        + "".join(sections)
        + "</main></body></html>\n"
    )
