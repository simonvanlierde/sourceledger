"""Download sources over HTTP(S) with conditional requests, retries and redirect guards."""

from __future__ import annotations

import dataclasses
import functools
import http.client
import ipaddress
import logging
import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
import urllib.response
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from importlib.metadata import version
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from sourceledger.ledger import (
    _UNUSABLE,
    FETCH_LOG,
    RAW,
    FetchError,
    FetchRecord,
    _commit,
    _copy,
    _last_records,
    _lock,
    _log_new_version,
    _served_from,
    _time_left,
    _timestamp,
    current_record,
    without_query,
)
from sourceledger.manifest import _BUDGET_S, Source, _vintage, implemented, select

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable, Collection
    from email.message import Message


# Bounds each connect and read; the source's budget is checked between reads.
_SOCKET_TIMEOUT_S = 30


# Waits of 5, 10 and 20 s between four attempts ride out a brief publisher outage.
_ATTEMPTS = 4


_BACKOFF_S = 5


# A server asking for hours would otherwise stall the run.
_MAX_RETRY_AFTER_S = 120


# Statuses a publisher recovers from. 501 and 505 never will.
_RETRIED = frozenset({429, 500, 502, 503, 504})


# A network error or timeout, or a connection dropped or garbled mid-response.
_TRANSIENT = (OSError, http.client.HTTPException)


# Sent again after a redirect to another scheme, host or port. Credentials from the manifest are not.
_SAFE_ON_REDIRECT = frozenset({"user-agent", "accept-encoding"})


# The Internet Archive's Save Page Now: a GET of this plus a URL asks it to snapshot that URL.
_ARCHIVE = "https://web.archive.org/save/"


_SNAPSHOT = "https://web.archive.org/web/"


# Save Page Now answers only once the snapshot is taken, 49 s for a GitHub page on 2026-10-20, so
# this bounds each connect and read of that request.
_ARCHIVE_TIMEOUT_S = 120


# Sent with every request, so a publisher can tell who is fetching. `main` takes the project's own.
_logger = logging.getLogger("sourceledger")
USER_AGENT = f"sourceledger/{version('sourceledger')}"


# What the opener returns: an HTTP response, or a file:// one.
type _Response = http.client.HTTPResponse | urllib.response.addinfourl


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
                msg = f"refused a redirect from a public host to {without_query(newurl)}, which is not HTTP or HTTPS"
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
            msg = f"refused a redirect from a public host to {without_query(self.req.full_url)}, which is at {address}"
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
            digest, size = _copy(response, fh, source, deadline)  # ty: ignore[invalid-argument-type]  # addinfourl forwards read1 to its file
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return Path(temp), digest, size


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
        return current_record(source, raw)
    except _UNUSABLE:
        return None


def _conditional(previous: FetchRecord | None) -> dict[str, str]:
    """The headers that ask the server to send the source only if it changed since `previous`."""
    if previous is None:
        return {}
    headers = {"If-None-Match": previous.etag, "If-Modified-Since": previous.last_modified}
    return {name: value for name, value in headers.items() if value}


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
    return fetch_sources(sources, raw=raw, log=log, user_agent=user_agent, only=only)


def fetch_sources(
    sources: list[Source],
    *,
    raw: Path = RAW,
    log: Path = FETCH_LOG,
    user_agent: str = USER_AGENT,
    only: Collection[str] | None = None,
    before: dict[str, dict[str, Any]] | None = None,
    progress: Callable[[str], None] | None = None,
    done: list[FetchRecord] | None = None,
) -> list[FetchRecord]:
    """Fetch and optionally retain the pre-fetch hashes from the same locked history read.

    `progress`, when given, is called before each source with its id and place, as `example (1 of 3)`.
    `done`, when given, collects the records as they come, so a caller still has them after Ctrl-C.
    """
    sources = select(sources, only)
    raw.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    records: list[FetchRecord] = [] if done is None else done
    failures: list[str] = []
    with _lock(raw):
        logged = _last_records(log)
        if before is not None:
            before.update(logged)
        todo = implemented(sources)
        for number, source in enumerate(todo, 1):
            if progress:
                progress(f"{source['id']} ({number} of {len(todo)})")
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
