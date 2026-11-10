"""Validate, summarise and report on a built dataset: checks, status, attribution, verify and the offline HTML view."""

from __future__ import annotations

import csv
import json
import os
import re
import stat
from html import escape
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from sourceledger.builder import (
    _PARQUET_SUFFIX,
    DATAPACKAGE,
    PROCESSED,
    _by_name,
    _listed,
    _parquet_batches,
    _provenance,
    _resource,
    _stamp,
)
from sourceledger.ledger import FETCH_LOG, RAW, _digest, _last_records, current_record
from sourceledger.manifest import (
    FILE_FORMAT,
    IMPLEMENTED,
    UNIMPLEMENTED,
    Source,
    _check_output_format,
    _vintage,
    implemented,
    output_name,
    raw_name,
    select,
)

if TYPE_CHECKING:
    from collections.abc import Collection


def _terms_problems(source: Source) -> list[str]:
    """What an implemented source is missing of its reuse terms: in the publisher's words, and machine-readable."""
    sid, problems = source["id"], []
    if not (source.get("terms") and source.get("terms_url")):
        problems.append(f"{sid}: no terms or terms_url; record the publisher's reuse terms")
    license_ = source.get("license")
    if not (isinstance(license_, str) and license_.strip()):
        problems.append(f"{sid}: no license; give the SPDX id of the terms, or LicenseRef-<name> for custom ones")
    if not isinstance(source.get("redistribute"), bool):
        problems.append(f"{sid}: redistribute must be true or false: may the raw file be republished?")
    return problems


def validate(
    sources: list[Source],
    *,
    raw: Path = RAW,
    processed: Path = PROCESSED,
    only: Collection[str] | None = None,
    output_format: str = "csv",
    name: str | None = None,
) -> list[str]:
    """Check the published output against the manifest and the fetch records, and return one string per problem.

    `only` restricts the checks to those source ids, and skips the two that concern the whole
    output: files no source owns, and the package's list of sources. Raises ValueError for an
    id the manifest does not have.
    `output_format` must match the build: CSV by default, or Parquet (needs the parquet extra).
    `name`, the package name sources.toml sets, must be the one datapackage.json has; None skips
    that check, since the fallback names differ between checkouts.
    """
    _check_output_format(output_format)
    selected = select(sources, only)
    problems = []
    listed = _listed(processed / DATAPACKAGE)
    outputs = {output_name(source, output_format) for source in sources}
    # Output of a source since removed or renamed would otherwise pass as published data.
    # A `.NAME.<random>.part` is the temp file of a build that was killed or is still running.
    if only is None:
        ids = {source["id"] for source in sources}
        problems += [
            f"{out.as_posix()}: output_format is {output_format!r}; "
            "build again, or validate with the format it was built in"
            if out.stem in ids and out.suffix in (".csv", ".parquet")
            else f"{out.as_posix()}: no source {out.stem!r} in sources.toml, and {processed.as_posix()}/ holds "
            "only build outputs; delete it, or keep the file elsewhere, as data/extra/"
            for out in sorted(processed.glob("*"))
            if out.is_file() and out.name != DATAPACKAGE and out.name not in outputs and not out.match(".*.part")
        ]
    for source in selected:
        sid, status = source["id"], source.get("status")
        out = processed / output_name(source, output_format)
        if status == UNIMPLEMENTED:
            if out.name.lower() != DATAPACKAGE and out.exists():
                problems.append(f"{sid}: status is unimplemented, but {out.as_posix()} exists")
            continue
        if status != IMPLEMENTED:
            problems.append(f"{sid}: status must be {IMPLEMENTED!r} or {UNIMPLEMENTED!r}, not {status!r}")
            continue
        problems += _terms_problems(source)
        problems += _output_problems(source, raw, out, listed.get(sid))
    return problems + _package_problems(sources, processed, only, output_format, name)


def _built_from(stamp: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Whether a resource's `sourceledger` block names this log line: its bytes and how they came in."""
    logged = {
        "raw_sha256": fields.get("sha256"),
        "retrieved_at": fields.get("retrieved_at"),
        "final_url": fields.get("final_url", ""),
        "recorded_at": fields.get("recorded_at") or None,  # the block leaves both out when empty
        "parameters": fields.get("parameters") or None,
    }
    return all(stamp.get(key) == value for key, value in logged.items())


def _output_problems(source: Source, raw: Path, out: Path, resource: dict[str, Any] | None) -> list[str]:
    """Whether one built file and its metadata still match the current fetch."""
    try:
        record = current_record(source, raw)
        if source.get("format") == FILE_FORMAT:
            expected = _resource(source, record, [], out)
            problems = []
            if expected["hash"] != f"sha256:{record.sha256}":
                problems.append(f"{out.as_posix()}: differs from the fetched file; re-run build")
            if resource is not None and resource != expected:
                package = (out.parent / DATAPACKAGE).as_posix()
                problems.append(f"{package} describes an earlier build of {out.as_posix()}; re-run build")
            return problems
        rows, stale = _scan(out, _stamp(record))
    except (OSError, ValueError, csv.Error) as error:
        return [one_line(error)]
    problems = []
    if not rows:
        problems.append(f"{out.as_posix()}: no rows")
    if stale:
        problems.append(f"{out.as_posix()}: rows come from an earlier fetch than {raw_name(source)}; re-run build")
    # The rows' stamp misses a record of the same bytes for another request; the package's block does not.
    elif resource is not None and resource.get("sourceledger") != _provenance(source, record):
        problems.append(
            f"{(out.parent / DATAPACKAGE).as_posix()} describes an earlier fetch of {raw_name(source)}; re-run build"
        )
    return problems


def _scan(out: Path, stamp: dict[str, str]) -> tuple[bool, bool]:
    """Whether an output has rows, and whether any of them has another stamp."""
    rows = stale = False
    if out.suffix == _PARQUET_SUFFIX:
        for batch in _parquet_batches(out):
            rows = rows or bool(batch.num_rows)
            if not stale:
                stale = any(
                    column not in batch.schema.names
                    or any(value != expected for value in batch.column(column).to_pylist())
                    for column, expected in stamp.items()
                )
        return rows, stale
    with out.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        where = {name: i for i, name in enumerate(header)}  # a repeated name keeps its last column, as in DictReader
        wanted = [(where.get(column), expected) for column, expected in stamp.items()]
        # Read to the end even once stale, so damage anywhere in the table is reported.
        for row in reader:
            if not row:  # a blank line, which DictReader skips too
                continue
            rows = True
            stale = stale or any(i is None or i >= len(row) or row[i] != expected for i, expected in wanted)
    return rows, stale


def _package_problems(
    sources: list[Source], processed: Path, only: Collection[str] | None, output_format: str, name: str | None
) -> list[str]:
    """Whether datapackage.json lists exactly the implemented sources, each with its output's current hash.

    With `only`, whether it lists those of them, each with its output's current hash.
    """
    path = processed / DATAPACKAGE
    expected = {source["id"]: source for source in implemented(sources)}
    checked = expected.keys() if only is None else expected.keys() & set(only)
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path.as_posix()} is missing; re-run build"] if checked else []
    except ValueError as error:
        return [f"{path.as_posix()} is not JSON ({error}); re-run build"]
    listed = _by_name(package)
    problems = []
    if name is not None and isinstance(package, dict) and package.get("name") != name:
        problems.append(f"{path.as_posix()} names the package {package.get('name')!r}, not {name!r}; re-run build")
    if only is None and listed.keys() != expected.keys():
        names = sorted(map(str, listed))
        return [
            *problems,
            f"{path.as_posix()} lists {names}, not the implemented sources {sorted(expected)}; re-run build",
        ]
    for sid in sorted(checked):
        out = processed / output_name(expected[sid], output_format)
        if sid not in listed:
            problems.append(f"{path.as_posix()} does not list {sid}; re-run build")
        # File outputs already have their full resource checked by _output_problems.
        elif (
            expected[sid].get("format") != FILE_FORMAT
            and out.exists()
            and listed[sid].get("hash") != f"sha256:{_digest(out)}"
        ):
            problems.append(f"{path.as_posix()} describes an earlier build of {out.as_posix()}; re-run build")
    return problems


def _yes_no(value: object) -> str:
    """`redistribute` as yes or no, or `?` when it is neither true nor false."""
    if not isinstance(value, bool):
        return "?"
    return "yes" if value else "no"


def status(sources: list[Source], *, log: Path = FETCH_LOG) -> list[str]:
    """One line per source, from the manifest and the fetch log only: no network, no raw files.

    Each line gives the id, the status, the vintage, when the version last logged was fetched
    (or `never`; `recorded <time>` for a file `record()` took in with no download time), the
    start of its SHA-256, the licence, and whether the raw file may be republished.
    """
    width = max((len(s["id"]) for s in sources), default=0)
    return [
        "  ".join(
            [
                f"{r['source_id']:<{width}}",
                f"{r['status']!s:<13}",
                r["vintage"],
                "never" if r["retrieved_at"] is None else r["retrieved_at"] or f"recorded {r['recorded_at']}",
                (r["sha256"] or "-")[:12],
                str(r["license"] or "-"),
                f"redistribute={_yes_no(r['redistribute'])}",
            ]
        )
        for r in status_records(sources, log=log)
    ]


def status_records(sources: list[Source], *, log: Path = FETCH_LOG) -> list[dict[str, Any]]:
    """Manifest status and the last logged fetch, with nulls for missing information."""
    last = _last_records(log)
    return [
        {
            "source_id": source["id"],
            "status": str(source["status"]) if source.get("status") is not None else None,
            "vintage": _vintage(source),
            "retrieved_at": last.get(source["id"], {}).get("retrieved_at"),
            "recorded_at": last.get(source["id"], {}).get("recorded_at"),
            "sha256": last.get(source["id"], {}).get("sha256"),
            "license": str(source["license"]) if source.get("license") else None,
            "redistribute": source.get("redistribute") if isinstance(source.get("redistribute"), bool) else None,
        }
        for source in sources
    ]


# Only ASCII whitespace: a no-break space, as in "10 000 €", is part of the publisher's words.
_MD_SPACE = re.compile(r"[\t\n\v\f\r ]+")


def _md_cell(value: object) -> str:
    """A value as one Markdown table cell: on one line, with its pipes escaped."""
    return _MD_SPACE.sub(" ", str(value)).strip(" ").replace("|", "\\|")


def attribution(sources: list[Source]) -> list[str]:
    """The implemented sources as the lines of a Markdown table, in manifest order.

    One row per source: its name (or id), the terms in the publisher's words, the licence,
    where the terms are stated, and whether the raw file may be republished. For a
    dataset's README or release notes.
    """
    lines = ["| Source | Terms | Licence | Terms URL | Redistribute |", "| --- | --- | --- | --- | --- |"]
    for source in implemented(sources):
        values = [
            source.get("name", source["id"]),
            source.get("terms", ""),
            source.get("license", ""),
            source.get("terms_url", ""),
            _yes_no(source.get("redistribute")),
        ]
        lines.append("| " + " | ".join(map(_md_cell, values)) + " |")
    return lines


# A received folder's SHA256SUMS or datapackage.json is read whole; a larger one is refused, not loaded.
RECEIVED_MAX_BYTES = 64 * 1024 * 1024


def read_received(path: Path) -> bytes:
    """The bytes of a metadata file in someone else's folder; ValueError when it is not a regular file of bounded size.

    Checked on the open file, not the name, so a pipe swapped in after a check cannot hang the read,
    and a link to /dev/zero cannot fill memory. FileNotFoundError when it is missing.
    """
    # O_NONBLOCK: opening a pipe would otherwise wait for a writer, before the check could refuse it.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            msg = f"{path.as_posix()} is not a regular file"
            raise ValueError(msg)
        data = fh.read(RECEIVED_MAX_BYTES + 1)
    if len(data) > RECEIVED_MAX_BYTES:
        msg = f"{path.as_posix()} is larger than {RECEIVED_MAX_BYTES:,} bytes"
        raise ValueError(msg)
    return data


def verify(folder: Path) -> list[str]:
    """Check a published dataset against its datapackage.json, offline, and return one string per problem.

    Each resource's file must be inside `folder` and match the `bytes` (when given) and the
    `hash` (`sha256:<hex>`) the package lists. Raises OSError when the package is missing,
    and ValueError when it is not JSON. The problems hold names from the folder as they are:
    pass each through `printable` before it goes to a terminal.
    """
    path = folder / DATAPACKAGE
    try:
        data = read_received(path)
    except ValueError as refused:
        return [str(refused)]
    package = json.loads(data.decode("utf-8"))
    resources = package.get("resources") if isinstance(package, dict) else None
    if not isinstance(resources, list):
        return [f"{path.as_posix()} has no list of resources"]
    return [problem for r in resources for problem in _file_problems(folder, r if isinstance(r, dict) else {})]


def leaves_folder(name: str) -> bool:
    """Whether a package's resource path is a URL, absolute, or climbs out, on any platform's reading of it."""
    windows = PureWindowsPath(name)  # also splits on "/"
    absolute = PurePosixPath(name).is_absolute() or bool(windows.anchor)  # also "C:x" and "\\x"
    return bool(urlsplit(name).scheme) or absolute or os.pardir in windows.parts


def _file_problems(folder: Path, resource: dict[str, Any]) -> list[str]:
    """Whether one resource's file is in `folder`, with the size and SHA-256 the package lists."""
    name = resource.get("path")
    # The package comes from someone else, so a path may not point outside the folder.
    if not isinstance(name, str) or leaves_folder(name):
        return [f"{(folder / DATAPACKAGE).as_posix()}: resource path {name!r} is not a file in {folder.as_posix()}"]
    file = folder / name
    if not file.is_file():
        return [f"{folder.as_posix()}: resource {name!r} is missing"]
    # A symlink inside the folder can still lead out of it.
    if not file.resolve().is_relative_to(folder.resolve()):
        return [f"{(folder / DATAPACKAGE).as_posix()}: resource path {name!r} is not a file in {folder.as_posix()}"]
    problems = []
    size = file.stat().st_size
    # Not isinstance: bool is an int subclass, so `"bytes": true` would pass for a 1-byte file.
    if (listed := resource.get("bytes")) is not None and (type(listed) is not int or listed != size):
        shown = f"{listed:,}" if type(listed) is int else repr(listed)  # from someone else's file
        problems.append(f"{file.as_posix()} is {size:,} bytes, not the {shown} the package lists")
    expected = resource.get("hash")
    if not (isinstance(expected, str) and expected.lower().startswith("sha256:")):
        problems.append(f"{file.as_posix()}: the package lists no sha256 hash to check it against")
        return problems
    digest = _digest(file)
    if f"sha256:{digest}" != expected.lower():
        problems.append(f"{file.as_posix()} hashes to sha256:{digest[:12]}, not the hash the package lists")
    return problems


def one_line(error: Exception) -> str:
    """One line for an expected failure: a missing file by name, anything else as raised."""
    if isinstance(error, FileNotFoundError) and error.filename:
        return f"{Path(error.filename).as_posix()} is missing"
    return str(error)


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


# Fields that hold free text from the manifest, which may be right-to-left or hold a bidi control.
_FREE_TEXT = frozenset({"Vintage", "Licence", "Terms"})


def _fields(values: dict[str, object]) -> str:
    return (
        "<dl>"
        + "".join(
            f"<dt>{escape(key)}</dt><dd{' dir="auto"' if key in _FREE_TEXT else ''}>"
            f"{value if isinstance(value, _Markup) else escape(str(value))}</dd>"
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
        if not isinstance(name, str) or leaves_folder(name):
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
        elif not _built_from(stamp, latest):
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
            f'<h4 id="source-{number}" dir="auto">{escape(str(source.get("name", sid)))}</h4>'
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
