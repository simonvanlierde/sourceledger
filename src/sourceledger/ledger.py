"""The fetch log and sidecar records, and the checks that a raw file still matches its record."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from sourceledger.manifest import _BUDGET_S, _MAX_BYTES, _SHA256, IMPLEMENTED, Source, _vintage, raw_name

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

if sys.platform == "win32":  # noqa: PLR2004
    import msvcrt
else:
    import fcntl


RAW = Path("data/raw")


FETCH_LOG = Path("data/fetches.jsonl")


_CHUNK_BYTES = 1024 * 1024


# Hashing a file already on disk: small enough that its peak memory stays small, large
# enough that a small file is one read.
_HASH_BLOCK_BYTES = 64 * 1024


# A raw file or fetch record that is missing, edited or unreadable.
_UNUSABLE = (OSError, ValueError)


_logger = logging.getLogger("sourceledger")


class _Reader(Protocol):
    """What `_copy` reads: a response or an open file."""

    def read1(self, size: int, /) -> bytes: ...


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


def log_fields(record: FetchRecord) -> dict[str, Any]:
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


def _copy(response: _Reader, fh: IO[bytes] | None, source: Source, deadline: float) -> tuple[str, int]:
    """Copy the body into `fh` chunk by chunk, flushed to disk: its SHA-256 and size; with no `fh`, only hash it."""
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
            raise ValueError(msg)  # a manifest error: the CLI reports ValueError as one line
        digest.update(chunk)
        if fh:
            fh.write(chunk)
    if fh:
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
                msg = f"{log.as_posix()}:{number} is not a fetch record ({error})"
                raise ValueError(msg) from error
            if not (isinstance(record, dict) and isinstance(record.get("source_id"), str)):
                msg = f"{log.as_posix()}:{number} is not a fetch record"
                raise ValueError(msg)  # noqa: TRY004  # a damaged log: the CLI reports ValueError as one line
            records[record["source_id"]] = record
            numbers[record["source_id"]] = number
    # Only the last record per source is used, so only those get the full check: it costs
    # more than reading the line, and a log has many lines per source.
    for sid, record in records.items():
        if not _is_record(record):
            msg = f"{log.as_posix()}:{numbers[sid]} is not a fetch record"
            raise ValueError(msg)  # a damaged log: the CLI reports ValueError as one line
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
        msg = f"{log.as_posix()} is a symlink; the fetch log must be a file of its own"
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
        fh.write(lead + json.dumps(log_fields(record)).encode() + b"\n")
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


@contextlib.contextmanager
def _lock(raw: Path) -> Generator[None]:
    """Hold an exclusive lock on `raw/.lock`, so two fetches at once can't interleave their writes.

    Once it holds the lock it removes the `.*.part` temp files a killed run left in `raw`: every
    writer there holds the same lock, so none of them is still being written. Fails at once
    rather than waiting.
    """
    # Never truncated, and never through a symlink: a cloned checkout could plant one to another file.
    # Windows has no O_NOFOLLOW, and Git there makes symlinks under Developer Mode: check first.
    if (raw / ".lock").is_symlink():
        msg = f"{(raw / '.lock').as_posix()} is a symlink; the lock must be a file of its own"
        raise OSError(msg)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(raw / ".lock", flags, 0o644)
    with os.fdopen(fd, "wb") as fh:
        try:
            if sys.platform == "win32":  # noqa: PLR2004
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # locks byte 0, even past the end
            else:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as error:  # held elsewhere; other OSErrors propagate
            msg = f"another fetch is already running in {raw.as_posix()}"
            raise OSError(msg) from error
        try:
            for stale in raw.glob(".*.part"):
                with contextlib.suppress(OSError):  # a scanner holding it, or a directory of that name
                    stale.unlink(missing_ok=True)
            yield
        finally:
            if sys.platform == "win32":  # noqa: PLR2004
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # before the file closes


def revisions(before: Path, *, log: Path = FETCH_LOG) -> list[str]:
    """One Markdown line per source whose bytes changed between `before`, an earlier copy of the log, and `log`.

    A source that `before` never logged is new, not revised, and is left out. Raises
    FileNotFoundError when `before` or `log` does not exist: an empty report must mean no
    revisions, not a missing copy or a run from the wrong directory.
    """
    return [
        f"- `{r['source_id']}`: {r['before_sha256'][:12]} -> {r['after']['sha256'][:12]}, "
        f"{r['after']['bytes']:,} bytes, {_when(r['after'])} from <{r['after']['url']}>"
        for r in revision_records(before, log=log)
    ]


def _when(fields: dict[str, Any]) -> str:
    """When a logged record's bytes arrived: fetched, or recorded from another tool's download."""
    if recorded := fields.get("recorded_at"):
        return f"recorded {recorded}"
    return f"fetched {fields['retrieved_at']}"


def revision_records(before: Path, *, log: Path = FETCH_LOG) -> list[dict[str, Any]]:
    """Changed sources with their earlier hash and current fetch record, in log order."""
    for path in (before, log):
        if not path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path.as_posix())
    old, new = _last_records(before), _last_records(log)
    return [
        {"source_id": sid, "before_sha256": old[sid]["sha256"], "after": record}
        for sid, record in new.items()
        if sid in old and old[sid]["sha256"] != record["sha256"]
    ]


def without_query(url: str) -> str:
    """`url` without its query and fragment, which can hold a signature."""
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _served_from(source: Source, url: str) -> str:
    """Where the bytes came from when not the source's own URL, without the query and fragment.

    A redirect target or another tool's download URL can carry a signature or key there.
    """
    served = without_query(url)
    return "" if url == source["url"] or served == source["url"] else served


def _commit(
    source: Source, temp: Path, record: FetchRecord, raw: Path, log: Path, logged: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Move `temp`, whose bytes `record` describes, into place as the raw file, and log and record it.

    Returns the fields the sidecar now holds: an earlier log line's when it was the same version.
    """
    name = raw_name(source)
    temp.replace(raw / name)
    # Logged before the record is written: until the record matches the raw file, the next
    # run fetches in full, so a crash in between loses nothing.
    fields = _log_new_version(log, logged, record)
    _write_atomic(raw / f"{name}.fetch.json", (json.dumps(fields, indent=2) + "\n").encode())
    return fields


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
    in. A record replaces the previous record's ETag and Last-Modified with its own. For a
    source with `archive = true` it asks for no snapshot: it keeps the one the log holds for
    the same bytes, and otherwise leaves `archive_url` empty.

    Raises ValueError for a source that is not implemented, a URL with a user name or
    password, a time without a time zone or in the future, other `parameters`, a path that
    is not a regular file, a file over the source's `max_bytes`, or bytes that miss the
    manifest's `sha256` pin. Raises OSError when another fetch holds the lock on `raw`.
    """
    return record_file(
        source,
        path,
        retrieved_at=retrieved_at,
        url=url,
        etag=etag,
        last_modified=last_modified,
        parameters=parameters,
        raw=raw,
        log=log,
    )


def record_file(  # noqa: PLR0913  # record()'s arguments, and the CLI's before and dry_run
    source: Source,
    path: Path,
    *,
    retrieved_at: datetime | str | None,
    url: str | None,
    etag: str,
    last_modified: str,
    parameters: dict[str, str | int] | None,
    raw: Path,
    log: Path,
    before: dict[str, dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> FetchRecord:
    """`record()`, filling `before` with the log lines logged before it, read under the lock as `fetch_sources` does.

    `dry_run` makes every check and returns the record without writing anything or taking the lock.
    """
    sid = source["id"]
    if source.get("status") != IMPLEMENTED:
        msg = f"{sid}: only an implemented source can be recorded"
        raise ValueError(msg)
    served, etag, last_modified = _recorded_url(source, url or source["url"], etag, last_modified)
    request = _checked_parameters(sid, parameters or {})
    now = datetime.now(UTC)
    downloaded = "" if retrieved_at is None else _timestamp(_aware(sid, retrieved_at, now))
    # Checked before opening: opening a named pipe waits for a writer, here with the lock held.
    if not stat.S_ISREG(path.stat().st_mode):
        msg = f"{sid}: {path.as_posix()} is not a regular file"
        raise ValueError(msg)
    if not dry_run:
        raw.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.nullcontext() if dry_run else _lock(raw):
        logged = _last_records(log)
        if before is not None:
            before.update(logged)
        temp = None
        try:
            digest, size, temp = _take_in(source, path, None if dry_run else raw)
            if source.get("sha256", digest) != digest:
                msg = f"{sid}: {path.as_posix()} hashes to {digest}, not the sha256 in sources.toml"
                raise ValueError(msg)
            # No request: only the snapshot a fetch already logged for these bytes is kept, as fetch keeps it.
            last = logged.get(sid, {})
            archived = last.get("archive_url", "") if source.get("archive") and last.get("sha256") == digest else ""
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
                archive_url=archived,
            )
            if temp:
                result = FetchRecord(**_commit(source, temp, result, raw, log, logged))
        finally:
            if temp:
                temp.unlink(missing_ok=True)  # gone already once moved into place
    return result


def _recorded_url(source: Source, url: str, etag: str, last_modified: str) -> tuple[str, str, str]:
    """The `final_url`, ETag and Last-Modified to record for a file from `url`.

    Another URL than the source's loses its query and fragment, and its validators.
    """
    sid = source["id"]
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
    return served, etag, last_modified


def _take_in(source: Source, path: Path, raw: Path | None) -> tuple[str, int, Path | None]:
    """The SHA-256 and size of `path`, and a copy of it in a temp file in `raw`; with no `raw`, only hashed."""
    if raw is None:
        with path.open("rb") as src:
            return (*_copy(src, None, source, math.inf), None)
    fd, name = tempfile.mkstemp(dir=raw, prefix=f".{source['id']}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh, path.open("rb") as src:
            digest, size = _copy(src, fh, source, math.inf)  # a local copy has no time budget
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return digest, size, Path(name)


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
    if log_version(last) == log_version(log_fields(record)):
        # A recorded file keeps its logged fields, so recording it again changes no byte of the
        # sidecar and the package built from it stays current. A fetched one keeps the logged
        # time and final URL, which the report checks the build against, and takes the new
        # validators for the next conditional request.
        if last.get("recorded_at"):
            return last
        return {**log_fields(record), "retrieved_at": last["retrieved_at"], "final_url": last.get("final_url", "")}
    _append(log, record)
    logged[record.source_id] = log_fields(record)
    return log_fields(record)


def log_version(fields: dict[str, Any]) -> tuple[object, ...]:
    """What makes a log line new: the bytes, the request (URL, vintage) and how they came in.

    For a recorded file, also where, when and with which validators the other tool fetched it.
    """
    keys = ("final_url", "retrieved_at", "etag", "last_modified")
    stated = tuple(fields.get(k) for k in keys) if fields.get("recorded_at") else ()
    request = fields.get("url"), fields.get("vintage")
    return fields.get("sha256"), *request, bool(fields.get("recorded_at")), fields.get("parameters", {}), *stated


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


def current_record(source: Source, raw: Path) -> FetchRecord:
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
