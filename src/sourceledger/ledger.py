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

import contextlib
import csv
import dataclasses
import errno
import functools
import gzip
import hashlib
import http.client
import io
import ipaddress
import itertools
import json
import logging
import math
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import urllib.response
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    import ssl
    from _csv import Reader as _CSVReader
    from email.message import Message

if sys.platform != "win32":  # noqa: PLR2004
    import fcntl

MANIFEST = Path("sources.toml")
RAW = Path("data/raw")
PROCESSED = Path("data/processed")
FETCH_LOG = Path("data/fetches.jsonl")
# Written by build beside the outputs.
DATAPACKAGE = "datapackage.json"
_DATAPACKAGE_PROFILE = "https://datapackage.org/profiles/2.0/datapackage.json"
# A manifest's first line names it, for an editor to check the file. The copy at this
# version's tag, not the $id on main: main can accept or refuse keys this version doesn't.
_SCHEMA_URL = (
    f"https://raw.githubusercontent.com/simonvanlierde/sourceledger/v{version('sourceledger')}"
    "/src/sourceledger/sources.schema.json"
)
_STARTER = f"""#:schema {_SCHEMA_URL}

# One [[source]] table per input. Uncomment the example and point it at your own source.
# Every key: https://github.com/simonvanlierde/sourceledger#sourcestoml

# [[source]]
# id = "co2"
# status = "implemented"
# url = "https://raw.githubusercontent.com/datasets/co2-ppm/main/data/co2-annmean-mlo.csv"
# vintage = "main branch, unversioned"
# terms = "ODC-PDDL-1.0 (datasets/co2-ppm package; original data from NOAA GML)"
# terms_url = "https://opendatacommons.org/licenses/pddl/"
# license = "PDDL-1.0"
# redistribute = true
"""

IMPLEMENTED = "implemented"
UNIMPLEMENTED = "unimplemented"
FILE_FORMAT = "file"
_PARQUET_SUFFIX = ".parquet"
_PARQUET_BATCH_ROWS = 10_000
# Bounds each connect and read; the source's budget is checked between reads.
_SOCKET_TIMEOUT_S = 30
# One source's download budget includes attempts and waits. DNS lookup and HTTP header
# parsing can exceed it.
_BUDGET_S = 300
# Waits of 5, 10 and 20 s between four attempts ride out a brief publisher outage.
_ATTEMPTS = 4
_BACKOFF_S = 5
# A server asking for hours would otherwise stall the run.
_MAX_RETRY_AFTER_S = 120
# Statuses a publisher recovers from. 501 and 505 never will.
_RETRIED = frozenset({429, 500, 502, 503, 504})
_CHUNK_BYTES = 1024 * 1024
# Hashing a file already on disk: small enough that its peak memory stays small, large
# enough that a small file is one read.
_HASH_BLOCK_BYTES = 64 * 1024
# A network error or timeout, or a connection dropped or garbled mid-response.
_TRANSIENT = (OSError, http.client.HTTPException)
# A raw file or fetch record that is missing, edited or unreadable.
_UNUSABLE = (OSError, ValueError)
# A runaway or wrong URL fails the fetch instead of filling the disk. A big source sets
# its own `max_bytes` in sources.toml.
_MAX_BYTES = 200 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GZIP_MAGIC = b"\x1f\x8b"
_ENV_REF = re.compile(r"env:([A-Za-z_][A-Za-z0-9_]*)")
# Headers a manifest may not set: the fetch's own.
_RESERVED_HEADERS = frozenset({"user-agent", "accept-encoding", "if-none-match", "if-modified-since"})
# Sent again after a redirect to another scheme, host or port. Credentials from the manifest are not.
_SAFE_ON_REDIRECT = frozenset({"user-agent", "accept-encoding"})
# The Internet Archive's Save Page Now: a GET of this plus a URL asks it to snapshot that URL.
_ARCHIVE = "https://web.archive.org/save/"
_SNAPSHOT = "https://web.archive.org/web/"
# Save Page Now answers only once the snapshot is taken, 49 s for a GitHub page on 2026-10-20, so
# this bounds each connect and read of that request.
_ARCHIVE_TIMEOUT_S = 120

# Sent with every request, so a publisher can tell who is fetching. `main` takes the project's own.
USER_AGENT = f"sourceledger/{version('sourceledger')}"

_logger = logging.getLogger("sourceledger")

type Source = dict[str, Any]
# What the opener returns: an HTTP response, or a file:// one.
type _Response = http.client.HTTPResponse | urllib.response.addinfourl
# Reads one raw file as its columns and rows. `parse_source` is the default.
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
    # Where the bytes came from after redirects. Empty when there were none.
    final_url: str = ""
    # The request headers taken from environment variables (names only, never values).
    env_headers: list[str] = dataclasses.field(default_factory=list)
    # The Internet Archive's snapshot of the URL, for a source with `archive = true`. Set by
    # the fetch that first brought these bytes; a later download of the same bytes takes it
    # from their line in the log. Empty when the source does not ask for one, or the archive
    # did not answer with one.
    archive_url: str = ""
    # When `record()` took these bytes from a file another tool downloaded; empty for a
    # fetch. `retrieved_at` is then the download time that tool reported, or empty when it
    # reported none. With `parameters`, left out of the log and sidecar while empty, so a
    # fetched record is written as 0.4.0 wrote it.
    recorded_at: str = ""
    # The request's own metadata, as the caller passed it to `record()`.
    parameters: dict[str, str] = dataclasses.field(default_factory=dict)


def _fields(record: FetchRecord) -> dict[str, Any]:
    """The record as written to the log and sidecar."""
    fields = dataclasses.asdict(record)
    for key in ("recorded_at", "parameters"):
        if not fields[key]:
            del fields[key]
    return fields


class FetchError(OSError):
    """One or more sources failed to fetch. The others were fetched and recorded as usual."""

    def __init__(self, records: list[FetchRecord], failures: list[str]) -> None:
        super().__init__("\n".join(failures))
        self.records = records  # the sources that succeeded
        self.failures = failures  # one line per source that did not


def load_manifest(path: Path = MANIFEST) -> list[Source]:
    """The `[[source]]` tables of sources.toml, after checking ids, URLs, caps, timeouts and pins."""
    with path.open("rb") as fh:
        manifest = tomllib.load(fh)
    _check_output_format(manifest.get("output_format", "csv"))
    sources = manifest.get("source", [])
    if not isinstance(sources, list) or not all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in sources):
        msg = "sources.toml: every [[source]] needs a string id"
        raise ValueError(msg)
    ids = [s["id"] for s in sources]
    # ASCII only: an id names files, and must mean the same file on every system.
    if bad := [i for i in ids if not re.fullmatch(r"[\w-]+", i, re.ASCII)]:
        msg = f"ids {bad} in sources.toml may hold only ASCII letters, digits, _ and -"
        raise ValueError(msg)
    if dupes := sorted(i for i, n in Counter(ids).items() if n > 1):
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
    if bad_archive := [s["id"] for s in sources if not isinstance(s.get("archive", False), bool)]:
        msg = f"sources {bad_archive} in sources.toml need archive to be true or false"
        raise ValueError(msg)
    if bad_timeout := [
        s["id"]
        for s in sources
        if type(timeout := s.get("timeout_s", _BUDGET_S)) not in (int, float) or not 0 < timeout <= sys.float_info.max
    ]:
        msg = f"sources {bad_timeout} in sources.toml need timeout_s to be a positive finite number"
        raise ValueError(msg)
    for source in sources:
        _check_pin(source)
        _check_vintage(source)
        _check_format(source)
        _check_headers(source)
    return sources


def _check_output_format(output_format: str) -> None:
    """Refuse unsupported table output formats before writing any files."""
    if output_format not in ("csv", "parquet"):
        msg = "output_format must be 'csv' or 'parquet'"
        raise ValueError(msg)


def _load_output_format(path: Path) -> str:
    """The project's table format; library callers pass it to build and validate directly."""
    with path.open("rb") as fh:
        output_format = tomllib.load(fh).get("output_format", "csv")
    _check_output_format(output_format)
    return output_format


def init(path: Path = MANIFEST) -> None:
    """Write a starter sources.toml at `path`: the schema line and one commented example source.

    Raises FileExistsError when `path` exists, which is never overwritten.
    """
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(_STARTER)
    except FileExistsError as error:
        msg = f"{path} already exists; not overwriting it"
        raise FileExistsError(msg) from error


def _select(sources: list[Source], only: Collection[str] | None) -> list[Source]:
    """The sources whose ids are in `only`, in manifest order, or all of them when `only` is None.

    Raises ValueError for an id the manifest does not have.
    """
    if only is None:
        return sources
    if isinstance(only, str):  # a str is a Collection of its characters
        msg = f"only takes a list of ids, not the string {only!r}"
        raise TypeError(msg)
    if unknown := sorted(set(only) - {s["id"] for s in sources}):
        msg = f"ids {unknown} are not in sources.toml"
        raise ValueError(msg)
    return [s for s in sources if s["id"] in only]


def _check_format(source: Source) -> None:
    if (fmt := source.get("format")) is not None and fmt not in (*_PARSERS, FILE_FORMAT):
        msg = f"{source['id']}: format in sources.toml must be one of {sorted([*_PARSERS, FILE_FORMAT])}, not {fmt!r}"
        raise ValueError(msg)


def _check_headers(source: Source) -> None:
    """Each extra request header takes its value from an environment variable, never a literal, and goes over https."""
    headers = source.get("headers", {})
    if not isinstance(headers, dict):
        msg = f"{source['id']}: headers in sources.toml must be a table"
        raise ValueError(msg)  # noqa: TRY004  # a manifest error: the CLI reports ValueError as one line
    url = source.get("url")
    # The same test as the schema's pattern, so the two accept the same manifests.
    if source.get("headers") is not None and isinstance(url, str) and not url.lower().startswith("https:"):
        msg = f"{source['id']}: headers in sources.toml need an https url, so the values are never sent in clear text"
        raise ValueError(msg)
    for name, value in headers.items():
        if name.lower() in _RESERVED_HEADERS:
            msg = f"{source['id']}: sourceledger sets the {name} header itself"
            raise ValueError(msg)
        if not (isinstance(value, str) and _ENV_REF.fullmatch(value)):
            msg = (
                f'{source["id"]}: header {name} must name an environment variable, as "env:NAME"; '
                "never write a secret into sources.toml"
            )
            raise ValueError(msg)


def _check_pin(source: Source) -> None:
    """Normalize a `sha256` pin to lower case, as `sha256sum` prints it, and reject one that is not a SHA-256."""
    if (pin := source.get("sha256")) is None:
        return
    if not (isinstance(pin, str) and _SHA256.fullmatch(pin.lower())):
        msg = f"{source['id']}: sha256 in sources.toml must be 64 hexadecimal characters"
        raise ValueError(msg)
    source["sha256"] = pin.lower()


def _check_vintage(source: Source) -> None:
    """Refuse the old key `release`: kept as an unknown key, it would leave the vintage "not stated"."""
    if source.get("release") is not None:
        msg = f"{source['id']}: `release` in sources.toml is now `vintage`; rename it"
        raise ValueError(msg)


def _vintage(source: Source) -> str:
    # str(): a TOML date (`vintage = 2024-06-01`) loads as a datetime.date.
    return str(source.get("vintage", "not stated"))


def implemented(sources: list[Source]) -> list[Source]:
    """The sources the pipeline reads."""
    return [s for s in sources if s.get("status") == IMPLEMENTED]


def raw_name(source: Source) -> str:
    """The raw file's name: the source id plus the URL's extension, e.g. `example-co2.csv`."""
    return source["id"] + PurePosixPath(urlsplit(source["url"]).path).suffix


def _output_name(source: Source, output_format: str) -> str:
    """The built file's name; an unimplemented source may have no URL yet."""
    return (
        raw_name(source)
        if source.get("format") == FILE_FORMAT and source.get("url")
        else f"{source['id']}.{output_format}"
    )


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` as `_replacing` does."""
    with _replacing(path) as fh:
        fh.write(data)


@contextlib.contextmanager
def _replacing(path: Path) -> Generator[IO[bytes]]:
    """A new temp file beside `path` to write; on success it is flushed to disk and renamed over `path`.

    A reader never sees half a file. The temp file gets a fresh name, so two runs at once
    don't share it, and it is created rather than opened, so a symlink in its place is
    never followed. On an error the temp file is removed and `path` is left as it was.
    """
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _is_public(host: str) -> bool:
    """Whether every address `host` resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # unresolvable: the request itself will fail
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


class _InwardRedirectError(ValueError):
    """A redirect from a public host refused for where it leads: inward, or out of HTTP."""


class _NoRedirectInward(urllib.request.HTTPRedirectHandler):
    """Refuses a redirect from a public host to a loopback, private or link-local one.

    Otherwise a third-party server could have the fetch save an internal service's response
    under the manifest's URL. A source that lives on an internal host can still redirect
    within it. The redirect is only marked here; the connection checks the address it
    actually reached (`_PeerCheck`), so a name that resolves to another address the second
    time can't slip through.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Follow the redirect as urllib does, marking it public-only when it comes from a public host."""
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        # Every later hop stays public-only, so a public host can't bounce through one that looks internal.
        if getattr(req, "public_only", False) or _is_public(urlsplit(req.full_url).hostname or ""):
            # _PeerCheck sees only HTTP connections; urllib would also follow a redirect to ftp.
            if urlsplit(newurl).scheme.lower() not in ("http", "https"):
                fp.close()
                msg = f"refused a redirect from a public host to {_without_query(newurl)}, which is not HTTP or HTTPS"
                raise _InwardRedirectError(msg)
            new.public_only = True  # ty: ignore[unresolved-attribute]  # read back by _PeerCheck
        # urllib copies every header to the new request. A token meant for one host must not
        # reach another, nor go out in clear text after https -> http, so a redirect that
        # changes the scheme, host or port keeps only the harmless ones.
        if urlsplit(newurl)[:2] != urlsplit(req.full_url)[:2]:
            new.headers = {k: v for k, v in new.headers.items() if k.lower() in _SAFE_ON_REDIRECT}
        # urllib drains this body before redirecting; closing it avoids an unbounded read.
        fp.close()
        return new


class _PeerCheck(http.client.HTTPConnection):
    """Once connected, refuses a public-only request whose socket reached a non-public address.

    Checked before anything is sent. Through a proxy the peer is the proxy, which resolves
    the name itself, so the check is skipped there.
    """

    def __init__(self, host: str, *, req: urllib.request.Request, **kwargs: Any) -> None:  # noqa: ANN401  # http.client's own arguments
        super().__init__(host, **kwargs)
        self.req = req

    def connect(self) -> None:
        """Connect as usual, then check the address the socket reached."""
        super().connect()
        target = urlsplit(self.req.full_url).hostname or ""
        if not getattr(self.req, "public_only", False) or self.host.lower() != target:
            return
        address = self.sock.getpeername()[0]
        if not ipaddress.ip_address(address).is_global:
            self.close()
            msg = f"refused a redirect from a public host to {_without_query(self.req.full_url)}, which is at {address}"
            raise _InwardRedirectError(msg)


class _HTTPSPeerCheck(_PeerCheck, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(functools.partial(_PeerCheck, req=req), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        super().__init__()
        self.context = context  # https_open passes it on; the base class's own copy is never read

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(functools.partial(_HTTPSPeerCheck, req=req), req, context=self.context)


_OPENER = urllib.request.build_opener(_NoRedirectInward(), _HTTPHandler(), _HTTPSHandler())


def _open(request: urllib.request.Request, timeout: float) -> _Response:
    # NOTE: the opener follows file:// too, which the tests use for fixtures. The URLs come
    # from sources.toml, which is as trusted as the code.
    return _OPENER.open(request, timeout=timeout)


def _retry_after(headers: Message | None) -> float:
    """Seconds a 429 or 503 asks to wait, from Retry-After as seconds or an HTTP date, capped."""
    value = (headers.get("Retry-After", "") if headers else "").strip()
    if value.isascii() and value.isdigit():
        seconds = float(value)
    else:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):  # absent, malformed, or a date with no time zone
            return 0.0
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)


def _content_length(headers: Message) -> int | None:
    """The Content-Length header as a byte count, or None when it is absent or not a plain number."""
    value = headers.get("Content-Length", "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


@dataclasses.dataclass(frozen=True, slots=True)
class _Download:
    """A body streamed to a temp file, not yet moved into place."""

    path: Path
    sha256: str
    size: int
    headers: Message
    url: str


def _download(response: _Response, source: Source, deadline: float, folder: Path) -> tuple[Path, str, int]:
    """Stream the body to a new temp file in `folder`, hashing it on the way: its path, SHA-256 and size.

    Memory stays at one chunk whatever the file's size. Refused past the source's
    `max_bytes` or past the fetch's time budget; the temp file is removed then.
    """
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        msg = f"{source['id']}: served with Content-Encoding {encoding}, so its bytes are not the file itself"
        raise ValueError(msg)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=f".{source['id']}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            digest, size = _copy(response, fh, source, deadline)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return Path(temp), digest, size


def _copy(response: _Response | io.BufferedReader, fh: IO[bytes], source: Source, deadline: float) -> tuple[str, int]:
    """Copy the body into `fh` chunk by chunk, flushed to disk: its SHA-256 and size."""
    cap = source.get("max_bytes", _MAX_BYTES)
    digest = hashlib.sha256()
    size = 0
    # read1: one socket read at most, so a server trickling bytes can't hold a read past the deadline.
    while True:
        chunk = response.read1(_CHUNK_BYTES)
        _time_left(source, deadline)
        if not chunk:
            break
        size += len(chunk)
        if size > cap:
            msg = f"{source['id']}: download exceeds {cap} bytes; raise max_bytes in sources.toml if expected"
            raise ValueError(msg)
        digest.update(chunk)
        fh.write(chunk)
    fh.flush()
    os.fsync(fh.fileno())
    return digest.hexdigest(), size


def _time_left(source: Source, deadline: float) -> float:
    """The fetch budget left for this attempt or read, or a timeout when it is spent."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        msg = f"download still unfinished after {source.get('timeout_s', _BUDGET_S)} s"
        raise TimeoutError(msg)
    return remaining


def _get(source: Source, previous: FetchRecord | None, user_agent: str, folder: Path) -> _Download | FetchRecord:
    """GET one source's URL into a temp file in `folder`, retrying failures a publisher recovers from.

    The request is conditional on the ETag and Last-Modified of a `previous` record, which
    is returned as it is when the server answers 304 Not Modified.

    429, 500, 502, 503 and 504, network errors and a body shorter than its Content-Length
    are retried with exponential backoff, waiting at least the server's Retry-After (up to
    120 s). The `timeout_s` budget (default 300 s) is checked before attempts and after
    body reads. DNS lookup and HTTP header parsing can exceed it; it is not a hard
    time limit. Any other error status is raised at once.

    Raises ValueError for a body over the source's `max_bytes` (default 200 MiB), or one
    served compressed.
    """
    conditional = _conditional(previous)
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity", **_env_headers(source), **conditional}
    request = urllib.request.Request(source["url"], headers=headers)  # noqa: S310  # see _open
    deadline = time.monotonic() + source.get("timeout_s", _BUDGET_S)
    for attempt in range(1, _ATTEMPTS + 1):
        wait = _BACKOFF_S * 2 ** (attempt - 1)
        try:
            with _open(request, timeout=min(_SOCKET_TIMEOUT_S, _time_left(source, deadline))) as response:
                path, digest, size = _download(response, source, deadline, folder)
                # A connection dropped mid-body can end the read early without an error.
                expected = _content_length(response.headers)
                if expected is not None and expected != size:
                    path.unlink()
                    msg = f"body ended after {size} of the {expected} bytes its Content-Length promised"
                    raise http.client.HTTPException(msg)
                return _Download(path, digest, size, response.headers, response.url)
        except _InwardRedirectError as error:
            msg = f"{source['id']}: {error}"
            raise ValueError(msg) from error
        except urllib.error.HTTPError as error:
            # urllib raises a 304 like an error. Only a conditional request can get one.
            if error.code == HTTPStatus.NOT_MODIFIED and previous and conditional:
                return previous
            if attempt == _ATTEMPTS or error.code not in _RETRIED:
                raise
            failure: Exception = error
            wait = max(wait, _retry_after(error.headers))
        except _TRANSIENT as error:
            if attempt == _ATTEMPTS:
                raise
            failure = error
        if time.monotonic() + wait >= deadline:
            raise failure
        _logger.warning(
            "%s: attempt %d of %d failed (%s); retrying in %.0f s", source["id"], attempt, _ATTEMPTS, failure, wait
        )
        time.sleep(wait)
    raise AssertionError  # unreachable: the last attempt returns or raises


def _archive(source: Source, user_agent: str) -> str:
    """Ask the Internet Archive to snapshot the source's URL: the snapshot's URL, or "" when there is none.

    Never raises. An archive that is down, slow or answers oddly costs a warning, not the fetch.
    """
    url = source["url"]
    if urlsplit(url).scheme not in {"http", "https"}:
        _logger.warning("%s: archive needs an http or https URL, not %s; skipped", source["id"], url)
        return ""
    request = urllib.request.Request(_ARCHIVE + url, headers={"User-Agent": user_agent})  # noqa: S310  # a fixed https URL
    try:
        with _open(request, timeout=_ARCHIVE_TIMEOUT_S) as response:
            # The archive redirects to the snapshot, or names it in Content-Location.
            snapshot = urljoin(response.url, response.headers.get("Content-Location", ""))
    except (*_TRANSIENT, ValueError) as error:
        _logger.warning("%s: the Internet Archive did not snapshot %s (%s)", source["id"], url, error)
        return ""
    if not snapshot.startswith(_SNAPSHOT):
        _logger.warning("%s: the Internet Archive answered without a snapshot of %s", source["id"], url)
        return ""
    return snapshot


def _env_headers(source: Source) -> dict[str, str]:
    """The manifest's extra request headers, each value read from the environment variable it names."""
    resolved = {}
    for name, ref in source.get("headers", {}).items():
        var = ref.removeprefix("env:")
        if (value := os.environ.get(var)) is None:
            msg = f"{source['id']}: environment variable {var} for the {name} header is not set"
            raise ValueError(msg)
        resolved[name] = value
    return resolved


def _previous(source: Source, raw: Path) -> FetchRecord | None:
    """The last fetch record of `source`, if it and its raw file still match the manifest."""
    try:
        return _current(source, raw)
    except _UNUSABLE:
        return None


def _conditional(previous: FetchRecord | None) -> dict[str, str]:
    """The headers that ask the server to send the source only if it changed since `previous`."""
    if previous is None:
        return {}
    headers = {"If-None-Match": previous.etag, "If-Modified-Since": previous.last_modified}
    return {name: value for name, value in headers.items() if value}


def _is_record(fields: object) -> bool:
    """Whether `fields` holds a fetch record's id, URL, time, SHA-256 and size, each of its own type."""
    return (
        isinstance(fields, dict)
        and all(isinstance(fields.get(key), str) for key in ("source_id", "url", "retrieved_at", "sha256"))
        and _SHA256.fullmatch(fields["sha256"]) is not None
        and type(fields.get("bytes")) is int  # not isinstance: bool is an int subclass
        and isinstance(fields.get("recorded_at", ""), str)
        and isinstance(parameters := fields.get("parameters", {}), dict)
        and all(isinstance(value, str) for value in parameters.values())
    )


def _history_batches(log: Path) -> Iterator[tuple[list[str], bool]]:
    """History batches with whether their last line may have been interrupted."""
    with log.open(encoding="utf-8", errors="replace", newline="") as fh:
        physical = fh.readlines(16 * 1024)
        while physical:
            terminated = physical[-1].endswith("\n")
            # Look ahead so a CR-terminated batch is not mistaken for an interrupted file.
            following = fh.readlines(16 * 1024)
            yield "".join(physical).splitlines(), not following and not terminated
            physical = following


def _last_records(log: Path) -> dict[str, dict[str, Any]]:
    """The record most recently logged for each source id, from the fetch log.

    A last line cut off by an interrupted fetch is skipped with a warning. Any other line
    that is not a record is an error that names the line.
    """
    if not log.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    numbers: dict[str, int] = {}
    number = 0
    for lines, interrupted in _history_batches(log):
        for index, line in enumerate(lines):
            number += 1
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                if interrupted and index == len(lines) - 1:
                    _logger.warning("%s: skipping a last line cut off by an interrupted fetch", log)
                    continue
                msg = f"{log}:{number} is not a fetch record ({error})"
                raise ValueError(msg) from error
            if not (isinstance(record, dict) and isinstance(record.get("source_id"), str)):
                msg = f"{log}:{number} is not a fetch record"
                raise ValueError(msg)  # noqa: TRY004  # a damaged log: the CLI reports ValueError as one line
            records[record["source_id"]] = record
            numbers[record["source_id"]] = number
    # Only the last record per source is used, so only those get the full check: it costs
    # more than reading the line, and a log has many lines per source.
    for sid, record in records.items():
        if not _is_record(record):
            msg = f"{log}:{numbers[sid]} is not a fetch record"
            raise ValueError(msg)
    return records


def _append(log: Path, record: FetchRecord) -> None:
    """Add `record` as one line at the end of the log, in one write, flushed to disk.

    A last line cut off by an interrupted fetch is dropped first, so the new line starts
    on a line of its own. A whole record that only lacks its newline, as an editor or a
    merge can leave it, is kept. Only the end of the log is read, however long it is.

    Raises ValueError when the log is a symlink, which a cloned checkout could point at
    another file.
    """
    if log.is_symlink():
        msg = f"{log} is a symlink; the fetch log must be a file of its own"
        raise ValueError(msg)
    with log.open("a+b") as fh:
        end = fh.seek(0, os.SEEK_END)
        lead = b""
        if end and (start := _last_line_start(fh, end)) < end:
            fh.seek(start)
            try:
                json.loads(fh.read())
            except ValueError:  # not JSON, or not UTF-8
                _logger.warning("%s: dropping a last line cut off by an interrupted fetch", log)
                fh.truncate(start)
            else:
                lead = b"\n"
        fh.write(lead + json.dumps(_fields(record)).encode() + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def _last_line_start(fh: IO[bytes], end: int) -> int:
    """Where the line after the last newline starts: `end` when the file ends with one."""
    pos = end
    while pos > 0:
        step = min(pos, _CHUNK_BYTES)
        fh.seek(pos - step)
        if (newline := fh.read(step).rfind(b"\n")) != -1:
            return pos - step + newline + 1
        pos -= step
    return 0


def logged_versions(log: Path = FETCH_LOG) -> dict[str, str]:
    """The SHA-256 most recently logged for each source id."""
    return {sid: r["sha256"] for sid, r in _last_records(log).items()}


@contextlib.contextmanager
def _lock(raw: Path) -> Generator[None]:
    """Hold an exclusive lock on `raw/.lock`, so two fetches at once can't interleave their writes.

    Fails at once rather than waiting. POSIX only: on Windows no lock is taken.
    """
    if sys.platform == "win32":  # noqa: PLR2004
        yield
        return
    # Never truncated, and never through a symlink: a cloned checkout could plant one to another file.
    fd = os.open(raw / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            msg = f"another fetch is already running in {raw}"
            raise OSError(msg) from error
        yield


def revisions(before: Path, *, log: Path = FETCH_LOG) -> list[str]:
    """One Markdown line per source whose bytes changed between `before`, an earlier copy of the log, and `log`.

    A source that `before` never logged is new, not revised, and is left out. Raises
    FileNotFoundError when `before` or `log` does not exist: an empty report must mean no
    revisions, not a missing copy or a run from the wrong directory.
    """
    return [
        f"- `{r['source_id']}`: {r['before_sha256'][:12]} -> {r['after']['sha256'][:12]}, "
        f"{r['after']['bytes']:,} bytes, {_when(r['after'])} from <{r['after']['url']}>"
        for r in _revision_records(before, log=log)
    ]


def _when(fields: dict[str, Any]) -> str:
    """When a logged record's bytes arrived: fetched, or recorded from another tool's download."""
    if recorded := fields.get("recorded_at"):
        return f"recorded {recorded}"
    return f"fetched {fields['retrieved_at']}"


def _revision_records(before: Path, *, log: Path = FETCH_LOG) -> list[dict[str, Any]]:
    """Changed sources with their earlier hash and current fetch record, in log order."""
    for path in (before, log):
        if not path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    old, new = _last_records(before), _last_records(log)
    return [
        {"source_id": sid, "before_sha256": old[sid]["sha256"], "after": record}
        for sid, record in new.items()
        if sid in old and old[sid]["sha256"] != record["sha256"]
    ]


def fetch(
    sources: list[Source],
    *,
    raw: Path = RAW,
    log: Path = FETCH_LOG,
    user_agent: str = USER_AGENT,
    only: Collection[str] | None = None,
) -> list[FetchRecord]:
    """Download each implemented source into `raw`, with a fetch record beside it.

    A source the server reports unchanged since its last intact fetch keeps that raw file
    and record, and its old record is returned. A record whose bytes differ from the last
    ones logged for its source is appended to `log`.

    Every source is tried. Raises FetchError after the loop when any failed, listing each
    with its reason; nothing is written for those. A source fails for a download over its
    `max_bytes` (default 200 MiB), bytes that miss the `sha256` the manifest pins, or an
    error that outlasts its retries. Raises OSError at once when another fetch holds the
    lock on `raw`.

    `only` restricts the fetch to those source ids, and leaves the others as they are.
    Raises ValueError for an id the manifest does not have.
    """
    return _fetch(sources, raw=raw, log=log, user_agent=user_agent, only=only)


def _fetch(
    sources: list[Source],
    *,
    raw: Path = RAW,
    log: Path = FETCH_LOG,
    user_agent: str = USER_AGENT,
    only: Collection[str] | None = None,
    before: dict[str, str] | None = None,
) -> list[FetchRecord]:
    """Fetch and optionally retain the pre-fetch hashes from the same locked history read."""
    sources = _select(sources, only)
    raw.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    records: list[FetchRecord] = []
    failures: list[str] = []
    with _lock(raw):
        logged = _last_records(log)
        if before is not None:
            before.update((sid, record["sha256"]) for sid, record in logged.items())
        for source in implemented(sources):
            try:
                records.append(_fetch_one(source, raw, log, logged, user_agent))
            except ValueError as error:  # messages already name the source
                failures.append(str(error))
            except _TRANSIENT as error:
                failures.append(f"{source['id']}: fetching {source['url']} failed: {error}")
    if failures:
        raise FetchError(records, failures)
    return records


def _fetch_one(source: Source, raw: Path, log: Path, logged: dict[str, dict[str, Any]], user_agent: str) -> FetchRecord:
    """Fetch one source, write its raw file, log and record, and return the record."""
    response = _get(source, _previous(source, raw), user_agent, raw)
    if isinstance(response, FetchRecord):
        # A version written to disk by a fetch that died before logging it gets logged now.
        _log_new_version(log, logged, response)
        return response
    try:
        return _keep(source, response, raw, log, logged, user_agent)
    finally:
        response.path.unlink(missing_ok=True)  # gone already once moved into place


def _keep(
    source: Source, download: _Download, raw: Path, log: Path, logged: dict[str, dict[str, Any]], user_agent: str
) -> FetchRecord:
    """Check a finished download against the pin, move it into place, and log and record it.

    Bytes the log has not had for this source are first archived, when the source asks for it.
    """
    digest = download.sha256
    if source.get("sha256", digest) != digest:
        msg = f"{source['id']}: downloaded bytes hash to {digest}, not the sha256 in sources.toml"
        raise ValueError(msg)
    archived = ""
    if source.get("archive"):
        # The same bytes again, with no 304 to say so, keep the snapshot their log line names.
        last = logged.get(source["id"], {})
        archived = last.get("archive_url", "") if last.get("sha256") == digest else _archive(source, user_agent)
    record = FetchRecord(
        source_id=source["id"],
        url=source["url"],
        retrieved_at=_timestamp(datetime.now(UTC)),
        sha256=digest,
        bytes=download.size,
        vintage=_vintage(source),
        # Kept for a conditional request (If-Modified-Since, If-None-Match) on a later run.
        last_modified=download.headers.get("Last-Modified", ""),
        etag=download.headers.get("ETag", ""),
        final_url=_served_from(source, download.url),
        env_headers=sorted(source.get("headers", {})),
        archive_url=archived,
    )
    _commit(source, download.path, record, raw, log, logged)
    return record


def _without_query(url: str) -> str:
    """`url` without its query and fragment, which can hold a signature."""
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _served_from(source: Source, url: str) -> str:
    """Where the bytes came from when not the source's own URL, without the query and fragment.

    A redirect target or another tool's download URL can carry a signature or key there.
    """
    served = _without_query(url)
    return "" if url == source["url"] or served == source["url"] else served


def _commit(
    source: Source, temp: Path, record: FetchRecord, raw: Path, log: Path, logged: dict[str, dict[str, Any]]
) -> None:
    """Move `temp`, whose bytes `record` describes, into place as the raw file, and log and record it."""
    name = raw_name(source)
    temp.replace(raw / name)
    # Logged before the record is written: until the record matches the raw file, the next
    # run fetches in full, so a crash in between loses nothing.
    fields = _log_new_version(log, logged, record)
    _write_atomic(raw / f"{name}.fetch.json", (json.dumps(fields, indent=2) + "\n").encode())


def record(
    source: Source,
    path: Path,
    *,
    retrieved_at: datetime | str | None = None,
    url: str | None = None,
    etag: str = "",
    last_modified: str = "",
    parameters: dict[str, str | int] | None = None,
    raw: Path = RAW,
    log: Path = FETCH_LOG,
) -> FetchRecord:
    """Take a file another tool downloaded for `source` into `raw`, and log and record it as `fetch` would.

    The file is copied, never moved, so the other tool keeps its cache. The same pin, lock,
    log and sidecar rules apply as for a fetch, and nothing goes over the network. The
    record's `recorded_at` is the time of this call.

    `retrieved_at` is when the other tool downloaded the file: a timezone-aware datetime or
    an ISO 8601 string with a time zone, stored in UTC. Leave it out when that tool does not
    say; the record then states no download time rather than pass the record time off as one.
    `url` is where the file came from, the source's URL by default; another URL is kept as
    the record's `final_url`, without its query or fragment, which can hold a signature or
    key. `etag` and `last_modified` are kept only when `url` is the source's URL, so a later
    `fetch` never sends one URL's validators to another. `parameters` describes the
    request: flat, with str or int values, stored as text in name order. It is written to
    the log and to every copy of the dataset's metadata, so a name that looks like a
    credential is refused; a value is published as given.

    The same bytes recorded again for the same request, URL, download time and validators add no log
    line; anything else adds one, so the log's last line names how the sidecar's bytes came
    in. A record replaces the previous record's ETag and Last-Modified with its own.

    Raises ValueError for a source that is not implemented, a URL with a user name or
    password, a time without a time zone or in the future, other `parameters`, a path that
    is not a regular file, a file over the source's `max_bytes`, or bytes that miss the
    manifest's `sha256` pin. Raises OSError when another fetch holds the lock on `raw`.
    """
    sid = source["id"]
    if source.get("status") != IMPLEMENTED:
        msg = f"{sid}: only an implemented source can be recorded"
        raise ValueError(msg)
    url = url or source["url"]
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        msg = f"{sid}: the URL holds a user name or password; record it without them"
        raise ValueError(msg)
    foreign = url != source["url"]
    served = _served_from(source, url) if foreign else ""
    if foreign and served != url:
        # The manifest's own URL is reviewed; another one can carry a signature or key in its query.
        kept = served or source["url"]
        _logger.warning("%s: kept %s without its query and fragment; put what they asked for in parameters", sid, kept)
    if foreign and (etag or last_modified):
        _logger.warning("%s: ETag and Last-Modified from %s are not the source URL's; not kept", sid, served)
        etag = last_modified = ""
    request = _checked_parameters(sid, parameters or {})
    now = datetime.now(UTC)
    downloaded = "" if retrieved_at is None else _timestamp(_aware(sid, retrieved_at, now))
    # Checked before opening: opening a named pipe waits for a writer, here with the lock held.
    if not stat.S_ISREG(path.stat().st_mode):
        msg = f"{sid}: {path} is not a regular file"
        raise ValueError(msg)
    raw.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    with _lock(raw):
        logged = _last_records(log)
        fd, name = tempfile.mkstemp(dir=raw, prefix=f".{sid}.", suffix=".part")
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh, path.open("rb") as src:
                digest, size = _copy(src, fh, source, math.inf)  # a local copy has no time budget
            if source.get("sha256", digest) != digest:
                msg = f"{sid}: {path} hashes to {digest}, not the sha256 in sources.toml"
                raise ValueError(msg)
            result = FetchRecord(
                source_id=sid,
                url=source["url"],
                retrieved_at=downloaded,
                sha256=digest,
                bytes=size,
                vintage=_vintage(source),
                last_modified=last_modified,
                etag=etag,
                final_url=served,
                recorded_at=_timestamp(now),
                parameters=request,
            )
            _commit(source, temp, result, raw, log, logged)
        finally:
            temp.unlink(missing_ok=True)  # gone already once moved into place
    return result


# A name holding one of these, or with one of the words below as a part of it (`x-api-key`, `sig`).
_SECRET_IN = re.compile(
    r"token|secret|passw|credential|apikey|accesskey|privatekey|authkey|authcode|sessionid|sessid|userkey",
    re.IGNORECASE,
)
_SECRET_PART = frozenset(
    {"key", "auth", "oauth", "authorization", "sig", "signature", "pwd", "jwt", "session", "bearer", "cookie", "sas"}
)
# How far ahead of this host's clock a reported download time may be.
_CLOCK_SKEW = timedelta(minutes=5)


def _checked_parameters(sid: str, parameters: dict[str, str | int]) -> dict[str, str]:
    """A copy of `parameters`, refused unless flat, with str or int values, and with no name that suggests a secret."""
    # Not isinstance: bool is an int subclass.
    if not all(isinstance(k, str) and k and type(v) in (str, int) for k, v in parameters.items()):
        msg = f"{sid}: parameters must map non-empty names to str or int values"
        raise ValueError(msg)
    if secret := sorted(k for k in parameters if _secret_name(k)):
        msg = f"{sid}: parameters {secret} look like credentials; they would be published with the dataset"
        raise ValueError(msg)
    return {name: str(parameters[name]) for name in sorted(parameters)}


def _secret_name(name: str) -> bool:
    """Whether a parameter name looks like it holds a credential: `api_key`, `authKey` and `sig` do, `author` does not.

    A camelCase name is split into parts first. `session_date` is refused, as a part `session` is.
    """
    return bool(_SECRET_IN.search(name.replace("_", "").replace("-", ""))) or not _SECRET_PART.isdisjoint(
        re.split(r"[^a-z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower())
    )


def _aware(sid: str, when: datetime | str, now: datetime) -> datetime:
    """`when` as a datetime, refused unless it is a time with a time zone, and not later than `now`."""
    try:
        parsed = when if isinstance(when, datetime) else datetime.fromisoformat(when)
    except (TypeError, ValueError) as error:
        msg = f"{sid}: retrieved_at {when!r} is not an ISO 8601 time"
        raise ValueError(msg) from error
    if parsed.utcoffset() is None:  # naive, or a tzinfo that knows no offset
        msg = f"{sid}: retrieved_at {when!r} needs a time zone, such as Z"
        raise ValueError(msg)
    try:
        parsed.astimezone(UTC)
    except OverflowError as error:  # such as year 1 at +05:00, before UTC's year 1
        msg = f"{sid}: retrieved_at {when!r} is out of range"
        raise ValueError(msg) from error
    if parsed > now + _CLOCK_SKEW:
        msg = f"{sid}: retrieved_at {when!r} is in the future"
        raise ValueError(msg)
    return parsed


def _timestamp(when: datetime) -> str:
    """A record's time: UTC, to the second, as `2026-11-05T09:30:00Z`."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_new_version(log: Path, logged: dict[str, dict[str, Any]], record: FetchRecord) -> dict[str, Any]:
    """Log `record` when it is a new version; return the fields the sidecar must hold."""
    # Appended, not rewritten: the log is history. A re-download of the same bytes adds nothing,
    # but the same bytes fetched after being recorded, or recorded for another request, do: the
    # log's last line and the sidecar must agree on how the bytes came in.
    last = logged.get(record.source_id, {})
    if _version(last) == _version(_fields(record)):
        # A recorded file keeps its logged fields, so recording it again changes no byte of the
        # sidecar and the package built from it stays current.
        return last if last.get("recorded_at") else _fields(record)
    _append(log, record)
    logged[record.source_id] = _fields(record)
    return _fields(record)


def _version(fields: dict[str, Any]) -> tuple[object, ...]:
    """What makes a log line new: the bytes and how they came in; for a recorded file, also where, when, validators."""
    keys = ("final_url", "retrieved_at", "etag", "last_modified")
    stated = tuple(fields.get(k) for k in keys) if fields.get("recorded_at") else ()
    return fields.get("sha256"), bool(fields.get("recorded_at")), fields.get("parameters", {}), *stated


def verified(source: Source, *, raw: Path = RAW) -> tuple[FetchRecord, bytes]:
    """The fetch record and raw bytes for `source`, after checking that both are current.

    Raises ValueError when the record was made for a different request than the manifest
    now makes (another URL, vintage or `sha256` pin), or when the raw file no longer hashes
    to the record.
    """
    record = _record(source, raw)
    payload = (raw / raw_name(source)).read_bytes()
    _check_hash(source, record, hashlib.sha256(payload).hexdigest())
    return record, payload


def _current(source: Source, raw: Path) -> FetchRecord:
    """The fetch record for `source`, checked as `verified` checks it, without reading the raw file into memory."""
    record = _record(source, raw)
    _check_hash(source, record, _digest(raw / raw_name(source)))
    return record


def _digest(path: Path) -> str:
    """The file's SHA-256, read a block at a time."""
    with path.open("rb") as fh:
        return _sha256(fh)


def _sha256(fh: IO[bytes]) -> str:
    # Not hashlib.file_digest: its fixed 256 KiB buffer costs more than the hash on a small file.
    digest = hashlib.sha256()
    while chunk := fh.read(_HASH_BLOCK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _check_hash(source: Source, record: FetchRecord, digest: str) -> None:
    if digest != record.sha256:
        msg = f"{raw_name(source)} changed since it was fetched; re-run fetch, or record for a file from another tool"
        raise ValueError(msg)


def _record(source: Source, raw: Path) -> FetchRecord:
    """The fetch record for `source`, if it was made for the request the manifest now makes."""
    name = raw_name(source)
    fields = json.loads((raw / f"{name}.fetch.json").read_text(encoding="utf-8"))
    msg = f"{name}.fetch.json is not a fetch record; re-run fetch"
    if not _is_record(fields):
        raise ValueError(msg)
    try:
        record = FetchRecord(**fields)
    except TypeError as error:  # a key FetchRecord does not have, or lacks
        raise ValueError(msg) from error
    if (record.source_id, record.url) != (source["id"], source["url"]):
        msg = f"{name} was fetched from {record.url}, but sources.toml now requests {source['url']}; re-run fetch"
        raise ValueError(msg)
    if record.vintage != _vintage(source):
        msg = f"{name} was fetched as vintage {record.vintage!r}, not {_vintage(source)!r}; re-run fetch"
        raise ValueError(msg)
    if source.get("sha256", record.sha256) != record.sha256:  # load_manifest lowercases the pin
        msg = f"{name} hashes to {record.sha256}, not the sha256 sources.toml now pins; re-run fetch"
        raise ValueError(msg)
    return record


def parse_source(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """One raw file as its columns and rows, read by its format: the default `Parser`.

    The format is the source's `format` key, or else the URL's extension: `.json` is JSON,
    `.xlsx` is Excel, anything else is CSV (gzipped or not). Raises ValueError for
    `format = "file"`, which has no table; `build` copies these files without a parser.
    """
    if _format(source) == FILE_FORMAT:
        msg = f"{source['id']}: format file has no table to parse"
        raise ValueError(msg)
    return _PARSERS[_format(source)](source, payload)


def _format(source: Source) -> str:
    suffix = PurePosixPath(urlsplit(source.get("url", "")).path).suffix.lower()
    return source.get("format") or {".json": "json", ".xlsx": "xlsx"}.get(suffix, "csv")


def parse_csv(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """One raw CSV file as its columns and rows. A gzipped file is decompressed first.

    Raises ValueError for a repeated column name, a row with more or fewer fields than the
    header, or a gzipped file that is damaged or decompresses past the source's `max_bytes`.
    """
    payload = _maybe_gunzip(source, payload)
    # utf-8-sig drops the byte-order mark that spreadsheet exports start with. Plain UTF-8 reads the same.
    text = payload.decode("utf-8-sig")
    columns, rows = _csv_rows(source, io.StringIO(text, newline=""))
    return columns, [dict(zip(columns, row, strict=True)) for row in rows]


def _csv_rows(source: Source, text: IO[str]) -> tuple[list[str], Iterator[list[str]]]:
    """A CSV text's columns and its rows as lists, each checked as it is read: parse_csv's checks and messages."""
    reader = csv.reader(text)
    columns = next(reader, [])
    _check_columns(source, columns)
    return columns, _checked_rows(source, reader, len(columns))


def _checked_rows(source: Source, reader: _CSVReader, width: int) -> Iterator[list[str]]:
    for row in reader:
        if not row:  # a blank line, which csv.DictReader also skips
            continue
        if len(row) != width:
            more = "more" if len(row) > width else "fewer"
            msg = f"{source['id']}: line {reader.line_num} has {more} fields than the header"
            raise ValueError(msg)
        yield row


def _maybe_gunzip(source: Source, payload: bytes) -> bytes:
    """The payload decompressed when it is gzipped, else as it is.

    Refused past the source's `max_bytes`, since a small file can expand without limit,
    and when the gzip is cut off or damaged.
    """
    if payload[:2] != _GZIP_MAGIC:
        return payload
    cap = source.get("max_bytes", _MAX_BYTES)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as fh:
            data = fh.read(cap + 1)
    except (EOFError, zlib.error, gzip.BadGzipFile) as error:
        msg = f"{source['id']}: not a readable gzip file ({error})"
        raise ValueError(msg) from error
    if len(data) > cap:
        msg = f"{source['id']}: decompresses past {cap} bytes; raise max_bytes in sources.toml if expected"
        raise ValueError(msg)
    return data


class _CappedGzip(gzip.GzipFile):
    """A gzip stream for TextIOWrapper, capped before decompressed bytes reach its buffer."""

    def __init__(self, source: Source, fh: IO[bytes]) -> None:
        super().__init__(fileobj=fh)
        self.source = source
        self.cap = source.get("max_bytes", _MAX_BYTES)
        self.remaining = self.cap

    def read1(self, size: int = -1) -> bytes:
        size = min(size if size >= 0 else _CHUNK_BYTES, self.remaining + 1)
        try:
            data = super().read1(size)
        except (EOFError, zlib.error, gzip.BadGzipFile) as error:
            msg = f"{self.source['id']}: not a readable gzip file ({error})"
            raise ValueError(msg) from error
        self.remaining -= len(data)
        if self.remaining < 0:
            msg = (
                f"{self.source['id']}: decompresses past {self.cap} bytes; raise max_bytes in sources.toml if expected"
            )
            raise ValueError(msg)
        return data


def _check_columns(source: Source, columns: list[str]) -> None:
    # A dict keeps only the last of two same-named columns, silently dropping the other.
    if dupes := sorted(c for c, n in Counter(columns).items() if n > 1):
        msg = f"{source['id']}: columns {dupes} appear more than once in the header"
        raise ValueError(msg)


class _Number(str):
    """A JSON number kept as its text, so 1.10 stays 1.10 and 1e5 stays 1e5."""

    __slots__ = ()


def _cell(value: object) -> str:
    """A JSON or spreadsheet value as output text: empty for none, `true`/`false`, JSON numbers as written."""
    match value:
        case None:
            return ""
        case str():
            return value
        case datetime() | date():
            return value.isoformat()
        case _:  # booleans as true/false, nested values as JSON text
            return _json_text(value)


def _json_text(value: object) -> str:
    """`value` as JSON text, with each `_Number` written as it was read."""
    match value:
        case _Number():
            return value
        case dict():
            return (
                "{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)}: {_json_text(v)}" for k, v in value.items()) + "}"
            )
        case list():
            return "[" + ", ".join(_json_text(item) for item in value) + "]"
        case _:
            return json.dumps(value, ensure_ascii=False, default=str)


def parse_json(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """A JSON array of objects as columns and rows: one column per key, in the order keys first appear.

    A key an object lacks is an empty cell. Numbers are kept as written, and nested values
    as JSON text. Raises ValueError for anything but an array of objects, or for one
    nested too deeply to read.
    """
    payload = _maybe_gunzip(source, payload)
    # From 3.14 the reader's depth limit follows the stack size, so a deep value can load and
    # then overflow while its cell is written: both steps are covered.
    try:
        data = json.loads(payload.decode("utf-8-sig"), parse_float=_Number, parse_int=_Number)
        if not (isinstance(data, list) and all(isinstance(item, dict) for item in data)):
            msg = f"{source['id']}: expected a JSON array of objects"
            raise ValueError(msg)
        columns = list(dict.fromkeys(key for item in data for key in item))
        for index, item in enumerate(data):
            data[index] = {column: _cell(item.get(column)) for column in columns}
    except RecursionError as error:
        msg = f"{source['id']}: JSON nested too deeply to read"
        raise ValueError(msg) from error
    return columns, data


def parse_xlsx(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """One sheet of an Excel workbook as columns and rows, the first row as the header.

    The sheet is the source's `sheet` key, or else the first. Fully empty rows are skipped,
    and so are empty header cells at the end of the row. An empty cell reads as "", like an
    empty CSV field. Needs the `xlsx` extra: `uv add 'sourceledger[xlsx]'`.

    Raises ValueError for a file that is not a workbook, one that expands past the source's
    `max_bytes`, a missing sheet, a repeated column name, or a cell right of the header.
    """
    try:
        import openpyxl  # noqa: PLC0415  # an optional extra, imported only when a source needs it
        from openpyxl.utils.exceptions import InvalidFileException  # noqa: PLC0415
    except ImportError as error:
        msg = f"{source['id']}: reading .xlsx needs the xlsx extra: uv add 'sourceledger[xlsx]'"
        raise ValueError(msg) from error
    cap = source.get("max_bytes", _MAX_BYTES)
    try:
        # The sizes the zip declares bound what it can expand to: reading past one fails its check.
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            expanded = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as error:
        msg = f"{source['id']}: not a readable Excel workbook ({error})"
        raise ValueError(msg) from error
    if expanded > cap:
        msg = f"{source['id']}: decompresses past {cap} bytes; raise max_bytes in sources.toml if expected"
        raise ValueError(msg)
    try:
        rows = _sheet_rows(source, openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True))
    except (zipfile.BadZipFile, KeyError, InvalidFileException) as error:  # a missing part reads as KeyError
        msg = f"{source['id']}: not a readable Excel workbook ({error})"
        raise ValueError(msg) from error
    if not rows:
        return [], []
    header = rows[0][1]
    width = len(header)
    while width and header[width - 1] is None:
        width -= 1
    columns = [_cell(cell) for cell in header[:width]]
    _check_columns(source, columns)
    table = []
    for number, row in rows[1:]:
        if any(cell is not None for cell in row[width:]):
            msg = f"{source['id']}: row {number} has more cells than the header"
            raise ValueError(msg)
        table.append({column: _cell(cell) for column, cell in itertools.zip_longest(columns, row[:width])})
    return columns, table


def _sheet_rows(source: Source, workbook: Any) -> list[tuple[int, tuple[object, ...]]]:  # noqa: ANN401  # openpyxl is optional
    """The sheet's rows that are not fully empty, each with its row number, and the workbook closed."""
    try:
        if (name := source.get("sheet")) is not None and name not in workbook.sheetnames:
            msg = f"{source['id']}: no sheet {name!r}; the workbook has {workbook.sheetnames}"
            raise ValueError(msg)
        sheet = workbook[name] if name is not None else workbook.worksheets[0]
        return [
            (number, row)
            for number, row in enumerate(sheet.iter_rows(values_only=True), start=1)
            if any(cell is not None for cell in row)
        ]
    finally:
        workbook.close()


_PARSERS: dict[str, Parser] = {"csv": parse_csv, "json": parse_json, "xlsx": parse_xlsx}


def _stamp(record: FetchRecord) -> dict[str, str]:
    """The provenance columns `build` adds to every output row."""
    return {
        "source_id": record.source_id,
        "source_url": record.url,
        "retrieved_at": record.retrieved_at,
        "vintage": record.vintage,
        "raw_sha256": record.sha256,
    }


def build(
    sources: list[Source],
    *,
    raw: Path = RAW,
    processed: Path = PROCESSED,
    parse: Parser = parse_source,
    only: Collection[str] | None = None,
    output_format: str = "csv",
) -> list[Path]:
    """Write implemented sources to `processed`: unchanged files, or tables stamped with their fetch records.

    `format = "file"` copies the original bytes to `processed/<raw_name>`, without parsing.
    For tables, `parse` reads each raw file; the default reads CSV, JSON and Excel by format.
    `output_format` selects CSV (the default) or Parquet with zstd compression and an explicit
    string schema. Parquet needs the parquet extra and writes batches of at most 10,000 rows.
    Then writes `processed/datapackage.json`, a Frictionless Data Package (v2) describing every
    output: its columns, size and SHA-256, its source URL and licence, and its fetch record.
    The same records give the same file, byte for byte.

    `only` restricts the build to those source ids. The package keeps each other source's
    entry as the last build wrote it. Raises ValueError for an id the manifest does not have,
    or a file output named `datapackage.json`, which is reserved for the descriptor.
    """
    return [
        out
        for out, _count in _build(
            sources, raw=raw, processed=processed, parse=parse, only=only, output_format=output_format
        )
    ]


def _build(
    sources: list[Source],
    *,
    raw: Path = RAW,
    processed: Path = PROCESSED,
    parse: Parser = parse_source,
    only: Collection[str] | None = None,
    output_format: str = "csv",
) -> list[tuple[Path, int | None]]:
    """Build outputs with row counts for tables, or None for unchanged file copies."""
    _check_output_format(output_format)
    selected = {s["id"] for s in _select(sources, only)}
    earlier = {} if only is None else _listed(processed / DATAPACKAGE)
    processed.mkdir(parents=True, exist_ok=True)
    written = []
    resources = []
    for source in implemented(sources):
        if source["id"] not in selected:
            if source["id"] in earlier:
                resources.append(earlier[source["id"]])
            continue
        out = processed / _output_name(source, output_format)
        if out.name.lower() == DATAPACKAGE:
            msg = f"{DATAPACKAGE} is reserved for the package descriptor; choose another source id"
            raise ValueError(msg)
        record, fields, count = _build_one(source, raw, out, parse)
        written.append((out, count))
        resources.append(_resource(source, record, fields, out))
    package = {"$schema": _DATAPACKAGE_PROFILE, "name": _package_name(), "resources": resources}
    _write_atomic(processed / DATAPACKAGE, (json.dumps(package, indent=2, ensure_ascii=False) + "\n").encode())
    return written


def _build_one(source: Source, raw: Path, out: Path, parse: Parser) -> tuple[FetchRecord, list[str], int | None]:
    """Write one source to `out`: return its fetch record, columns and row count (None for a file).

    File sources are copied unchanged. Tables get their provenance columns first.
    With the default parser, CSV files, gzipped or not, are read a row at a time.
    CSV output writes one row at a time; Parquet writes batches of at most 10,000 rows.
    """
    record = _record(source, raw)
    stamp = _stamp(record)
    with (raw / raw_name(source)).open("rb") as fh:
        _check_hash(source, record, _sha256(fh))
        fh.seek(0)
        if source.get("format") == FILE_FORMAT:
            with _replacing(out) as target:
                shutil.copyfileobj(fh, target)
            return record, [], None
        if parse is parse_source and _PARSERS[_format(source)] is parse_csv:
            compressed = fh.read(2) == _GZIP_MAGIC
            fh.seek(0)
            stream = _CappedGzip(source, fh) if compressed else fh
            text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
            try:
                columns, rows = _csv_rows(source, text)
                count = _write_cells(source, out, stamp, columns, rows)
            except (ValueError, csv.Error):
                # parse_csv decodes the whole file before it reads a row, so it can fail on another
                # line, or name another byte offset. Reading the file whole gives exactly its error.
                fh.seek(0)
                parse(source, fh.read())
                raise
            finally:
                text.detach()  # closing the wrapper would close fh
                if compressed:
                    stream.close()
        else:
            # NOTE: other formats and custom parsers hold the whole file and its rows
            # in memory, as a Parser returns lists; stream them too once such sources outgrow memory.
            fh.seek(0)
            columns, rows = parse(source, fh.read())
            count = _write_rows(source, out, stamp, columns, rows)
    return record, [*stamp, *columns], count


def _write_rows(
    source: Source, out: Path, stamp: dict[str, str], columns: list[str], rows: Iterable[Mapping[str, object]]
) -> int:
    """Write a parser's rows, as `_write_cells` does: a missing cell is "", a provenance key is ignored."""
    known = {*stamp, *columns}

    def cells() -> Iterator[list[object]]:
        for row in rows:
            if extra := row.keys() - known:
                msg = f"{source['id']}: row has fields not in the header: {sorted(extra)}"
                raise ValueError(msg)
            yield [row.get(column) for column in columns]

    return _write_cells(source, out, stamp, columns, cells())


def _write_cells(
    source: Source, out: Path, stamp: dict[str, str], columns: list[str], rows: Iterable[Sequence[object]]
) -> int:
    """Write CSV or Parquet with provenance columns first, and return the number of rows written.

    Each row holds its cells in `columns` order. None is written as "", anything else through
    str(), as csv.writer does.
    """
    if clash := stamp.keys() & set(columns):
        msg = f"{source['id']}: source columns {sorted(clash)} collide with the provenance columns"
        raise ValueError(msg)
    fields, head = [*stamp, *columns], list(stamp.values())
    stamped = ([*head, *row] for row in rows)
    if out.suffix == _PARQUET_SUFFIX:
        pa, pq = _arrow()
        schema = pa.schema([(field, pa.string()) for field in fields])
        count = 0
        with _replacing(out) as fh, pq.ParquetWriter(fh, schema, compression="zstd") as writer:
            while batch := list(itertools.islice(stamped, _PARQUET_BATCH_ROWS)):
                arrays = [
                    pa.array(["" if cell is None else str(cell) for cell in column], pa.string())
                    for column in zip(*batch, strict=True)
                ]
                writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
                count += len(batch)
        return count
    with _replacing(out) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
        writer = csv.writer(text)
        writer.writerow(fields)
        count = 0
        for row in stamped:
            writer.writerow(row)
            count += 1
        text.detach()  # flushes; _replacing closes fh
    return count


def _listed(path: Path) -> dict[str, Any]:
    """The resources a data package lists, by name: none when it is missing or unreadable."""
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except _UNUSABLE:
        return {}
    return _by_name(package)


def _by_name(package: object) -> dict[str, Any]:
    """A parsed data package's resources, by name: none when it is not an object with a list of them."""
    resources = package.get("resources") if isinstance(package, dict) else None
    return (
        {r["name"]: r for r in resources if isinstance(r, dict) and isinstance(r.get("name"), str)}
        if isinstance(resources, list)
        else {}
    )


def _package_name() -> str:
    """The project directory's name, lower-cased, as Data Package names should be."""
    return re.sub(r"[^a-z0-9._-]+", "-", Path.cwd().name.lower()).strip("-") or "dataset"


def _resource(source: Source, record: FetchRecord, fields: list[str], out: Path) -> dict[str, Any]:
    """One output as a Data Package resource: a file, or a table whose columns hold text."""
    resource: dict[str, Any] = {
        "name": source["id"],
        "type": "table",
        "path": out.name,
        "format": "csv",
        "mediatype": "text/csv",
        "encoding": "utf-8",
        "bytes": out.stat().st_size,
        "hash": f"sha256:{_digest(out)}",
        "schema": {"fields": [{"name": field, "type": "string"} for field in fields]},
        "sources": [{"title": str(source.get("name", source["id"])), "path": record.url}],
    }
    if source.get("format") == FILE_FORMAT:
        for key in ("type", "format", "mediatype", "encoding", "schema"):
            del resource[key]
    elif out.suffix == _PARQUET_SUFFIX:
        resource.update(format="parquet", mediatype="application/vnd.apache.parquet")
        del resource["encoding"]
    if source.get("license"):
        licence = {"name": source["license"], "path": source.get("terms_url"), "title": source.get("terms")}
        resource["licenses"] = [{k: v for k, v in licence.items() if v}]
    # Custom properties are allowed in Data Package v2; these name the bytes the rows came from.
    resource["sourceledger"] = _provenance(source, record)
    return resource


def _provenance(source: Source, record: FetchRecord) -> dict[str, Any]:
    """A resource's `sourceledger` block: the fetch or record its output was built from."""
    stamp = {
        "retrieved_at": record.retrieved_at,
        "vintage": record.vintage,
        "raw_sha256": record.sha256,
        "final_url": record.final_url,
        "redistribute": source.get("redistribute"),
    }
    if record.recorded_at:
        stamp["recorded_at"] = record.recorded_at
    if record.parameters:
        stamp["parameters"] = record.parameters
    return stamp


def read_output(path: Path) -> list[dict[str, str]]:
    """A published CSV or Parquet table, as written. Parquet needs the parquet extra."""
    return list(_output_rows(path))


def _arrow() -> tuple[Any, Any]:  # optional dependency, imported only for Parquet
    """Load the Parquet backend, with an actionable error when the extra is missing."""
    try:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as error:
        msg = (
            "Parquet needs the parquet extra: uv tool install 'sourceledger[parquet]' (CLI) "
            "or uv add 'sourceledger[parquet]' (project)"
        )
        raise ValueError(msg) from error
    return pa, pq


def _parquet_batches(path: Path) -> Iterator[Any]:
    """Decode all columns in bounded row batches, including columns validation does not compare."""
    pa, pq = _arrow()
    try:
        with pq.ParquetFile(path) as reader:
            for batch in reader.iter_batches(batch_size=_PARQUET_BATCH_ROWS, use_threads=False):
                batch.validate(full=True)
                yield batch
    except (pa.ArrowException, OSError) as error:
        msg = f"{path}: not a readable Parquet table ({error})"
        raise ValueError(msg) from error


def _output_rows(path: Path) -> Iterator[dict[str, str]]:
    """Read CSV one row at a time, or Parquet in bounded batches with its optional backend."""
    if path.suffix == _PARQUET_SUFFIX:
        for batch in _parquet_batches(path):
            yield from batch.to_pylist()
        return
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


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
) -> list[str]:
    """Check the published output against the manifest and the fetch records, and return one string per problem.

    `only` restricts the checks to those source ids, and skips the two that concern the whole
    output: files no source owns, and the package's list of sources. Raises ValueError for an
    id the manifest does not have.
    `output_format` must match the build: CSV by default, or Parquet (needs the parquet extra).
    """
    _check_output_format(output_format)
    selected = _select(sources, only)
    problems = []
    listed = _listed(processed / DATAPACKAGE)
    outputs = {_output_name(source, output_format) for source in sources}
    # Output of a source since removed or renamed would otherwise pass as published data.
    if only is None:
        problems += [
            f"{out}: no source {out.stem!r} in sources.toml; delete it"
            for out in sorted(processed.glob("*"))
            if out.is_file() and out.name != DATAPACKAGE and out.name not in outputs
        ]
    for source in selected:
        sid, status = source["id"], source.get("status")
        out = processed / _output_name(source, output_format)
        if status == UNIMPLEMENTED:
            if out.name.lower() != DATAPACKAGE and out.exists():
                problems.append(f"{sid}: status is unimplemented, but {out} exists")
            continue
        if status != IMPLEMENTED:
            problems.append(f"{sid}: status must be {IMPLEMENTED!r} or {UNIMPLEMENTED!r}, not {status!r}")
            continue
        problems += _terms_problems(source)
        problems += _output_problems(source, raw, out, listed.get(sid))
    return problems + _package_problems(sources, processed, only, output_format)


def _output_problems(source: Source, raw: Path, out: Path, resource: dict[str, Any] | None) -> list[str]:
    """Whether one built file and its metadata still match the current fetch."""
    try:
        record = _current(source, raw)
        if source.get("format") == FILE_FORMAT:
            expected = _resource(source, record, [], out)
            problems = []
            if expected["hash"] != f"sha256:{record.sha256}":
                problems.append(f"{out}: differs from the fetched file; re-run build")
            if resource is not None and resource != expected:
                problems.append(f"{out.parent / DATAPACKAGE} describes an earlier build of {out}; re-run build")
            return problems
        rows, stale = _scan(out, _stamp(record))
    except (OSError, ValueError, csv.Error) as error:
        return [one_line(error)]
    problems = []
    if not rows:
        problems.append(f"{out}: no rows")
    if stale:
        problems.append(f"{out}: rows come from an earlier fetch than {raw_name(source)}; re-run build")
    # The rows' stamp misses a record of the same bytes for another request; the package's block does not.
    elif resource is not None and resource.get("sourceledger") != _provenance(source, record):
        problems.append(f"{out.parent / DATAPACKAGE} describes an earlier fetch of {raw_name(source)}; re-run build")
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
    sources: list[Source], processed: Path, only: Collection[str] | None, output_format: str
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
        return [f"{path} is missing; re-run build"] if checked else []
    except ValueError as error:
        return [f"{path} is not JSON ({error}); re-run build"]
    listed = _by_name(package)
    if only is None and listed.keys() != expected.keys():
        return [
            f"{path} lists {sorted(map(str, listed))}, not the implemented sources {sorted(expected)}; re-run build"
        ]
    problems = []
    for sid in sorted(checked):
        out = processed / _output_name(expected[sid], output_format)
        if sid not in listed:
            problems.append(f"{path} does not list {sid}; re-run build")
        # File outputs already have their full resource checked by _output_problems.
        elif (
            expected[sid].get("format") != FILE_FORMAT
            and out.exists()
            and listed[sid].get("hash") != f"sha256:{_digest(out)}"
        ):
            problems.append(f"{path} describes an earlier build of {out}; re-run build")
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
        for r in _status_records(sources, log=log)
    ]


def _status_records(sources: list[Source], *, log: Path = FETCH_LOG) -> list[dict[str, Any]]:
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


def _md_cell(value: object) -> str:
    """A value as one Markdown table cell: on one line, with its pipes escaped."""
    return " ".join(str(value).split()).replace("|", "\\|")


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


def verify(folder: Path) -> list[str]:
    """Check a published dataset against its datapackage.json, offline, and return one string per problem.

    Each resource's file must be inside `folder` and match the `bytes` (when given) and the
    `hash` (`sha256:<hex>`) the package lists. Raises OSError when the package is missing,
    and ValueError when it is not JSON.
    """
    path = folder / DATAPACKAGE
    package = json.loads(path.read_text(encoding="utf-8"))
    resources = package.get("resources") if isinstance(package, dict) else None
    if not isinstance(resources, list):
        return [f"{path} has no list of resources"]
    return [problem for r in resources for problem in _file_problems(folder, r if isinstance(r, dict) else {})]


def _file_problems(folder: Path, resource: dict[str, Any]) -> list[str]:
    """Whether one resource's file is in `folder`, with the size and SHA-256 the package lists."""
    name = resource.get("path")
    # The package comes from someone else, so a path may not point outside the folder.
    if not isinstance(name, str) or urlsplit(name).scheme or Path(name).is_absolute() or os.pardir in Path(name).parts:
        return [f"{folder / DATAPACKAGE}: resource path {name!r} is not a file in {folder}"]
    file = folder / name
    if not file.is_file():
        return [f"{file} is missing"]
    # A symlink inside the folder can still lead out of it.
    if not file.resolve().is_relative_to(folder.resolve()):
        return [f"{folder / DATAPACKAGE}: resource path {name!r} is not a file in {folder}"]
    problems = []
    size = file.stat().st_size
    # Not isinstance: bool is an int subclass, so `"bytes": true` would pass for a 1-byte file.
    if (listed := resource.get("bytes")) is not None and (type(listed) is not int or listed != size):
        shown = f"{listed:,}" if type(listed) is int else repr(listed)  # from someone else's file
        problems.append(f"{file} is {size:,} bytes, not the {shown} the package lists")
    expected = resource.get("hash")
    if not (isinstance(expected, str) and expected.lower().startswith("sha256:")):
        problems.append(f"{file}: the package lists no sha256 hash to check it against")
        return problems
    digest = _digest(file)
    if f"sha256:{digest}" != expected.lower():
        problems.append(f"{file} hashes to sha256:{digest[:12]}, not the hash the package lists")
    return problems


def one_line(error: Exception) -> str:
    """One line for an expected failure: a missing file by name, anything else as raised."""
    if isinstance(error, FileNotFoundError) and error.filename:
        return f"{error.filename} is missing"
    return str(error)
