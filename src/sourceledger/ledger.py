"""Fetch, build and validate the sources a dataset is built from, as `sources.toml` declares them.

Raw files stay in data/raw/ and outputs in data/processed/. Each fetch leaves a
`<file>.fetch.json` record beside its raw file: the URL, when it was retrieved, the SHA-256
of the bytes, the vintage the manifest declares, and the server's Last-Modified and ETag
headers. A later fetch sends those headers back, and when the server answers 304 Not
Modified it keeps the raw file and its record as they are.

Each fetch that brings bytes a source has not had before also appends its record to
data/fetches.jsonl, the history of every version of every source the pipeline has seen.

Paths are relative to the working directory, the project root.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import http.client
import io
import json
import re
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

MANIFEST = Path("sources.toml")
RAW = Path("data/raw")
PROCESSED = Path("data/processed")
FETCH_LOG = Path("data/fetches.jsonl")

IMPLEMENTED = "implemented"
UNIMPLEMENTED = "unimplemented"
_TIMEOUT_S = 120
# Waits of 5, 10 and 20 s between four attempts ride out a brief publisher outage.
_ATTEMPTS = 4
_BACKOFF_S = 5
# A server asking for hours would otherwise stall the run. Three capped waits (6 min) stay
# inside a 10-minute CI job, so the run ends on the fetch's own error, not a timeout.
_MAX_RETRY_AFTER_S = 120
# A network error or timeout, or a connection dropped or garbled mid-response.
_TRANSIENT = (OSError, http.client.HTTPException)
# A raw file or fetch record that is missing, edited or unreadable.
_UNUSABLE = (OSError, ValueError)
# A runaway or wrong URL fails the fetch instead of filling the disk. A big source sets
# its own `max_bytes` in sources.toml.
_MAX_BYTES = 200 * 1024 * 1024

# Sent with every request, so a publisher can tell who is fetching. `main` takes the project's own.
USER_AGENT = f"sourceledger/{version('sourceledger')}"

type Source = dict[str, Any]
# Reads one raw file as its columns and rows. `parse_csv` is the default.
type Parser = Callable[[Source, bytes], tuple[list[str], list[dict[str, str]]]]


@dataclasses.dataclass(frozen=True, slots=True)
class FetchRecord:
    """What one download was, so every output row traces back to the bytes it came from."""

    source_id: str
    url: str
    retrieved_at: str
    sha256: str
    bytes: int
    vintage: str
    # Empty when the server sends none. Defaults keep records written before these fields readable.
    last_modified: str = ""
    etag: str = ""


def load_manifest(path: Path = MANIFEST) -> list[Source]:
    """The `[[source]]` tables of sources.toml, after checking that each id is a safe, unique file name."""
    with path.open("rb") as fh:
        sources = tomllib.load(fh).get("source", [])
    if not isinstance(sources, list) or not all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in sources):
        msg = "sources.toml: every [[source]] needs a string id"
        raise ValueError(msg)
    ids = [s["id"] for s in sources]
    if bad := [i for i in ids if not re.fullmatch(r"[\w-]+", i)]:
        msg = f"ids {bad} in sources.toml may hold only letters, digits, _ and -"
        raise ValueError(msg)
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        msg = f"ids {dupes} appear more than once in sources.toml"
        raise ValueError(msg)
    if no_url := [s["id"] for s in implemented(sources) if not isinstance(s.get("url"), str)]:
        msg = f"implemented sources {no_url} in sources.toml have no url"
        raise ValueError(msg)
    # Not isinstance: bool is an int subclass, so `max_bytes = true` would pass as a 1-byte cap.
    caps = {s["id"]: s.get("max_bytes", _MAX_BYTES) for s in sources}
    if bad_cap := [i for i, cap in caps.items() if type(cap) is not int or cap < 1]:
        msg = f"sources {bad_cap} in sources.toml need max_bytes to be a positive whole number"
        raise ValueError(msg)
    return sources


def implemented(sources: list[Source]) -> list[Source]:
    """The sources the pipeline reads."""
    return [s for s in sources if s.get("status") == IMPLEMENTED]


def raw_name(source: Source) -> str:
    """The raw file's name: the source id plus the URL's extension, e.g. `example-co2.csv`."""
    return source["id"] + PurePosixPath(urlsplit(source["url"]).path).suffix


def _write_atomic(path: Path, data: bytes) -> None:
    """Write a sibling temp file, then rename it: a reader never sees half a file."""
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)


def _get(
    source: Source, previous: FetchRecord | None, user_agent: str
) -> tuple[bytes, http.client.HTTPMessage] | FetchRecord:
    """GET one source's URL and its headers, retrying failures a publisher recovers from.

    The request is conditional on the ETag and Last-Modified of a `previous` record, which
    is returned as it is when the server answers 304 Not Modified.

    Server errors, rate limits, network errors and a body shorter than its Content-Length
    are retried with exponential backoff. A 429 waits at least the server's Retry-After, in
    seconds, up to 120 s. Any other 4xx is raised at once: it will not fix itself.

    Raises ValueError for a body over the source's `max_bytes` (default 200 MiB).
    """
    cap = source.get("max_bytes", _MAX_BYTES)
    conditional = _conditional(previous)
    # NOTE: urlopen follows any scheme, file:// included, which the tests use for fixtures.
    # The URLs come from sources.toml, which is as trusted as the code.
    request = urllib.request.Request(source["url"], headers={"User-Agent": user_agent, **conditional})  # noqa: S310
    for attempt in range(_ATTEMPTS):
        last = attempt == _ATTEMPTS - 1
        wait = _BACKOFF_S * 2**attempt
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
                payload = response.read(cap + 1)
                if len(payload) > cap:
                    msg = f"{source['id']}: download exceeds {cap} bytes; raise max_bytes in sources.toml if expected"
                    raise ValueError(msg)
                # A connection dropped mid-body can end the read early without an error.
                expected = response.headers.get("Content-Length")
                if expected is not None and int(expected) != len(payload):
                    raise http.client.IncompleteRead(payload, int(expected) - len(payload))
                return payload, response.headers
        except urllib.error.HTTPError as error:
            # urllib raises a 304 like an error. Only a conditional request can get one.
            if error.code == HTTPStatus.NOT_MODIFIED and previous and conditional:
                return previous
            if last or (error.code < HTTPStatus.INTERNAL_SERVER_ERROR and error.code != HTTPStatus.TOO_MANY_REQUESTS):
                raise
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            if retry_after.isdigit():
                wait = max(wait, min(int(retry_after), _MAX_RETRY_AFTER_S))
        except _TRANSIENT:
            if last:
                raise
        time.sleep(wait)
    raise AssertionError  # unreachable: the last attempt returns or raises


def _previous(source: Source, raw: Path) -> FetchRecord | None:
    """The last fetch record of `source`, if its raw file is intact and still passes the manifest's `sha256`."""
    try:
        record, _ = verified(source, raw)
    except _UNUSABLE:
        return None
    return record if source.get("sha256", record.sha256) == record.sha256 else None


def _conditional(previous: FetchRecord | None) -> dict[str, str]:
    """The headers that ask the server to send the source only if it changed since `previous`."""
    if previous is None:
        return {}
    headers = {"If-None-Match": previous.etag, "If-Modified-Since": previous.last_modified}
    return {name: value for name, value in headers.items() if value}


def _last_records(log: Path) -> dict[str, dict[str, Any]]:
    """The record most recently logged for each source id, from the fetch log."""
    if not log.exists():
        return {}
    records = (json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
    return {r["source_id"]: r for r in records}


def _last_logged(log: Path) -> dict[str, str]:
    """The SHA-256 most recently logged for each source id, from the fetch log."""
    return {sid: r["sha256"] for sid, r in _last_records(log).items()}


def revisions(before: Path, log: Path = FETCH_LOG) -> list[str]:
    """One Markdown line per source whose bytes changed between `before`, an earlier copy of the log, and `log`.

    A source that `before` never logged is new, not revised, and is left out.
    """
    old, new = _last_records(before), _last_records(log)
    return [
        f"- `{sid}`: {old[sid]['sha256'][:12]} -> {r['sha256'][:12]}, {r['bytes']:,} bytes, "
        f"fetched {r['retrieved_at']} from <{r['url']}>"
        for sid, r in new.items()
        if sid in old and old[sid]["sha256"] != r["sha256"]
    ]


def fetch(
    sources: list[Source], raw: Path = RAW, log: Path = FETCH_LOG, user_agent: str = USER_AGENT
) -> list[FetchRecord]:
    """Download each implemented source into `raw`, with a fetch record beside it.

    A source the server reports unchanged since its last intact fetch keeps that raw file
    and record, and its old record is returned. A record whose bytes differ from the last
    ones logged for its source is appended to `log`.

    Raises ValueError for a download over the source's `max_bytes` (default 200 MiB), or
    one whose SHA-256 differs from the `sha256` the manifest pins, and OSError naming the
    source for one that still fails after its retries. Nothing is written for that source.
    """
    raw.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    logged = _last_logged(log)
    records = []
    for source in implemented(sources):
        try:
            response = _get(source, _previous(source, raw), user_agent)
        except _TRANSIENT as error:
            msg = f"{source['id']}: fetching {source['url']} failed: {error}"
            raise OSError(msg) from error
        if isinstance(response, FetchRecord):
            records.append(response)
            continue
        payload, headers = response
        digest = hashlib.sha256(payload).hexdigest()
        if source.get("sha256", digest) != digest:
            msg = f"{source['id']}: downloaded bytes hash to {digest}, not the sha256 in sources.toml"
            raise ValueError(msg)
        record = FetchRecord(
            source_id=source["id"],
            url=source["url"],
            retrieved_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sha256=digest,
            bytes=len(payload),
            # str(): a TOML date (`release = 2024-06-01`) loads as a datetime.date.
            vintage=str(source.get("release", "not stated")),
            # Kept for a conditional request (If-Modified-Since, If-None-Match) on a later run.
            last_modified=headers.get("Last-Modified", ""),
            etag=headers.get("ETag", ""),
        )
        name = raw_name(source)
        _write_atomic(raw / name, payload)
        _write_atomic(raw / f"{name}.fetch.json", (json.dumps(dataclasses.asdict(record), indent=2) + "\n").encode())
        # Appended, not rewritten: the log is history. A re-download of the same bytes adds nothing.
        if logged.get(record.source_id) != record.sha256:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dataclasses.asdict(record)) + "\n")
            logged[record.source_id] = record.sha256
        records.append(record)
    return records


def verified(source: Source, raw: Path = RAW) -> tuple[FetchRecord, bytes]:
    """The fetch record and raw bytes for `source`, after checking that both are current.

    Raises ValueError when the record was made for a different request than the
    manifest now makes, or when the raw file no longer hashes to the record.
    """
    name = raw_name(source)
    try:
        record = FetchRecord(**json.loads((raw / f"{name}.fetch.json").read_text(encoding="utf-8")))
    except TypeError as error:
        msg = f"{name}.fetch.json is not a fetch record; re-run fetch"
        raise ValueError(msg) from error
    if (record.source_id, record.url) != (source["id"], source["url"]):
        msg = f"{name} was fetched from {record.url}, but sources.toml now requests {source['url']}; re-run fetch"
        raise ValueError(msg)
    payload = (raw / name).read_bytes()
    if hashlib.sha256(payload).hexdigest() != record.sha256:
        msg = f"{name} changed since it was fetched; re-run fetch"
        raise ValueError(msg)
    return record, payload


def parse_csv(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """One raw CSV file as its columns and rows: the default `Parser`.

    Raises ValueError for a repeated column name, or a row with more or fewer fields than the header.
    """
    # utf-8-sig drops the byte-order mark that spreadsheet exports start with. Plain UTF-8 reads the same.
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = list(reader.fieldnames or [])
    # DictReader keeps only the last of two same-named columns, silently dropping the other.
    if dupes := sorted({c for c in columns if columns.count(c) > 1}):
        msg = f"{source['id']}: columns {dupes} appear more than once in the header"
        raise ValueError(msg)
    rows = []
    for row in reader:
        # DictReader files the surplus fields of a long row under the key None.
        if None in row:
            msg = f"{source['id']}: line {reader.line_num} has more fields than the header"
            raise ValueError(msg)
        # It fills the missing fields of a short row with None, which would publish as "".
        if None in row.values():
            msg = f"{source['id']}: line {reader.line_num} has fewer fields than the header"
            raise ValueError(msg)
        rows.append(row)
    return columns, rows


def build(sources: list[Source], raw: Path = RAW, processed: Path = PROCESSED, parse: Parser = parse_csv) -> list[Path]:
    """Write each implemented source to `processed/<id>.csv`, each row stamped with its fetch record.

    `parse` reads each raw file. Give it a function that branches on `source["id"]` for sources that are not CSV.
    """
    processed.mkdir(parents=True, exist_ok=True)
    written = []
    for source in implemented(sources):
        record, payload = verified(source, raw)
        stamp = {
            "source_id": record.source_id,
            "source_url": record.url,
            "retrieved_at": record.retrieved_at,
            "vintage": record.vintage,
            "raw_sha256": record.sha256,
        }
        columns, rows = parse(source, payload)
        if clash := stamp.keys() & set(columns):
            msg = f"{source['id']}: source columns {sorted(clash)} collide with the provenance columns"
            raise ValueError(msg)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=[*stamp, *columns])
        writer.writeheader()
        writer.writerows({**stamp, **row} for row in rows)
        out = processed / f"{source['id']}.csv"
        _write_atomic(out, buffer.getvalue().encode())
        written.append(out)
    return written


def read_output(path: Path) -> list[dict[str, str]]:
    """A published output file, as written."""
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


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


def validate(sources: list[Source], raw: Path = RAW, processed: Path = PROCESSED) -> list[str]:
    """Check the published output against the manifest and the fetch records, and return one string per problem."""
    problems = []
    ids = {source["id"] for source in sources}
    # Output of a source since removed or renamed would otherwise pass as published data.
    problems += [
        f"{out}: no source {out.stem!r} in sources.toml; delete it"
        for out in sorted(processed.glob("*.csv"))
        if out.stem not in ids
    ]
    for source in sources:
        sid, status = source["id"], source.get("status")
        out = processed / f"{sid}.csv"
        if status == UNIMPLEMENTED:
            if out.exists():
                problems.append(f"{sid}: status is unimplemented, but {out} exists")
            continue
        if status != IMPLEMENTED:
            problems.append(f"{sid}: status must be {IMPLEMENTED!r} or {UNIMPLEMENTED!r}, not {status!r}")
            continue
        problems += _terms_problems(source)
        try:
            record, _ = verified(source, raw)
            rows = read_output(out)
        except (OSError, ValueError, csv.Error) as error:
            problems.append(one_line(error))
            continue
        if not rows:
            problems.append(f"{out}: no rows")
        if {(r.get("source_url"), r.get("raw_sha256")) for r in rows} - {(record.url, record.sha256)}:
            problems.append(f"{out}: rows come from an earlier fetch than {raw_name(source)}; re-run build")
    return problems


def one_line(error: Exception) -> str:
    """One line for an expected failure: a missing file by name, anything else as raised."""
    if isinstance(error, FileNotFoundError) and error.filename:
        return f"{error.filename} is missing"
    return str(error)
