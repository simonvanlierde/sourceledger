"""Property tests for the code that reads outside input: the fetch log, response headers and parsed rows."""

import csv
import dataclasses
import gzip
import io
import json
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from email.message import Message
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from support import builder_mod, fetcher_mod, report_mod

from sourceledger import ledger
from sourceledger import manifest as manifest_mod

_ids = st.text("abcxyz_-0123456789", min_size=1, max_size=8)
_texts = st.text(max_size=20)
_records = st.builds(
    ledger.FetchRecord,
    source_id=_ids,
    url=_texts,
    retrieved_at=_texts,
    sha256=st.text("0123456789abcdef", min_size=64, max_size=64),
    bytes=st.integers(min_value=0),
    vintage=_texts,
    last_modified=_texts,
    etag=_texts,
    final_url=_texts,
    env_headers=st.lists(_texts, max_size=3),
    archive_url=_texts,
    recorded_at=_texts,
    parameters=st.dictionaries(_texts, _texts, max_size=3),
)


def _last_by_id(written: list[ledger.FetchRecord]) -> dict[str, dict[str, object]]:
    return {record.source_id: ledger.log_fields(record) for record in written}


# No deadline where an example writes and fsyncs files: a slow CI disk is not a failure.
@settings(deadline=None)
@given(st.lists(_records, max_size=5), st.data())
def test_the_log_reads_back_what_was_appended_even_after_a_torn_line(
    written: list[ledger.FetchRecord], data: st.DataObject
) -> None:
    """A crash part-way through an append leaves a prefix of a line; reading skips it, the next append drops it."""
    with tempfile.TemporaryDirectory() as folder:
        log = Path(folder) / "fetches.jsonl"
        for record in written:
            ledger._append(log, record)  # noqa: SLF001
        torn = json.dumps(dataclasses.asdict(data.draw(_records)))
        with log.open("a", encoding="utf-8") as fh:
            fh.write(torn[: data.draw(st.integers(0, len(torn) - 1))])
        assert ledger._last_records(log) == _last_by_id(written)  # noqa: SLF001
        last = data.draw(_records)
        ledger._append(log, last)  # noqa: SLF001
        assert ledger._last_records(log) == _last_by_id([*written, last])  # noqa: SLF001


@given(st.lists(st.text().map(str.encode) | st.binary(), max_size=5), st.sampled_from([b"", b"\n"]))
@example([b"\x80"], b"\n")  # not UTF-8: was a UnicodeDecodeError that named no line
def test_a_log_of_junk_reads_or_names_the_bad_line(lines: list[bytes], end: bytes) -> None:
    """Whatever the log holds, reading it gives records or a ValueError that names the line, never another error."""
    with tempfile.TemporaryDirectory() as folder:
        log = Path(folder) / "fetches.jsonl"
        log.write_bytes(b"\n".join(lines) + end)
        try:
            ledger._last_records(log)  # noqa: SLF001
        except ValueError as error:
            if "is not a fetch record" not in str(error):
                raise


def _message(name: str, value: str) -> Message:
    headers = Message()
    headers[name] = value
    return headers


@given(st.text())
@example("Wed, 21 Oct 2015 07:28:00")  # a date with no time zone
@example("Fri, 31 Dec 9999 23:59:59 -2359")  # in UTC, past the last year datetime holds
@example("٣٠")  # Arabic-Indic digits
@example("²")  # a digit to str.isdigit, not to float()
def test_retry_after_never_raises(value: str) -> None:
    """Any Retry-After gives a wait between 0 and the cap."""
    assert 0.0 <= fetcher_mod._retry_after(_message("Retry-After", value)) <= fetcher_mod._MAX_RETRY_AFTER_S  # noqa: SLF001


@given(st.text())
@example("29, 29")
@example("٢٩")
@example("²")
def test_content_length_never_raises(value: str) -> None:
    """Any Content-Length gives a byte count or None."""
    length = fetcher_mod._content_length(_message("Content-Length", value))  # noqa: SLF001
    assert length is None or length >= 0


_provenance = {"source_id", "source_url", "retrieved_at", "vintage", "raw_sha256"}
# Unique: a repeated column name is refused by design.
_columns = st.lists(
    st.text(min_size=1, max_size=8).filter(lambda c: c not in _provenance), min_size=1, max_size=4, unique=True
)


def _built(
    name: str, payload: bytes, parse: Callable[[manifest_mod.Source, bytes], tuple[list[str], list[dict[str, str]]]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The rows `parse` reads from `payload`, and the rows read back after a fetch and build of it."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        upstream = root / name
        upstream.write_bytes(payload)
        source = {"id": "s", "status": "implemented", "url": upstream.as_uri()}
        raw, processed = root / "raw", root / "processed"
        fetcher_mod.fetch([source], raw=raw, log=root / "fetches.jsonl")
        _, parsed = parse(source, payload)
        build_parser = builder_mod.parse_source if parse is builder_mod.parse_csv else parse
        (out,) = builder_mod.build([source], raw=raw, processed=processed, parse=build_parser)
        read = [{k: v for k, v in row.items() if k not in _provenance} for row in builder_mod.read_output(out)]
        return parsed, read


@settings(deadline=None)
@given(
    table=_columns.flatmap(
        lambda cols: st.tuples(
            st.just(cols), st.lists(st.lists(st.text(), min_size=len(cols), max_size=len(cols)), max_size=4)
        )
    ),
    compressed=st.booleans(),
)
def test_csv_rows_survive_build_and_read_output(table: tuple[list[str], list[list[str]]], *, compressed: bool) -> None:
    """What parse_csv reads is what the published output holds, field for field."""
    columns, rows = table
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(rows)
    payload = buffer.getvalue().encode()
    name = "s.csv.gz" if compressed else "s.csv"
    parsed, read = _built(name, gzip.compress(payload) if compressed else payload, builder_mod.parse_csv)
    assert read == parsed


_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=5,
)


@settings(deadline=None)
@given(
    st.lists(
        st.dictionaries(st.text(max_size=8).filter(lambda c: c not in _provenance), _json_values, max_size=4),
        max_size=4,
    )
)
def test_json_rows_survive_build_and_read_output(items: list[dict[str, object]]) -> None:
    """What parse_json reads is what the published output holds, field for field."""
    parsed, read = _built("s.json", json.dumps(items).encode(), builder_mod.parse_json)
    assert read == parsed


@settings(deadline=None)
@given(st.binary(max_size=1024))
def test_file_sources_keep_every_byte(payload: bytes) -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        upstream = root / "input.bin"
        upstream.write_bytes(payload)
        source = {
            "id": "example",
            "status": "implemented",
            "url": upstream.as_uri(),
            "format": "file",
            "redistribute": True,
            "license": "CC0-1.0",
            "terms": "CC0-1.0",
            "terms_url": "https://example.org/terms",
        }
        raw, processed = root / "raw", root / "processed"
        fetcher_mod.fetch([source], raw=raw, log=root / "fetches.jsonl")
        assert builder_mod.build([source], raw=raw, processed=processed) == [processed / "example.bin"]
        assert (processed / "example.bin").read_bytes() == payload
        assert report_mod.validate([source], raw=raw, processed=processed) == []
        assert report_mod.verify(processed) == []


@settings(deadline=None)
@given(value=st.integers() | st.floats() | st.booleans() | st.text(alphabet="abcXYZ012_-", max_size=10))
@example(value=10**400)
@example(value=sys.float_info.max)
def test_timeout_values_are_positive_finite_numbers(*, value: float | bool | str) -> None:
    encoded = json.dumps(value, ensure_ascii=False) if isinstance(value, str) else str(value).lower()
    with tempfile.TemporaryDirectory() as folder:
        manifest = Path(folder) / "sources.toml"
        manifest.write_text(f'[[source]]\nid = "example"\nstatus = "unimplemented"\ntimeout_s = {encoded}\n')
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= sys.float_info.max
        if valid:
            assert manifest_mod.load_manifest(manifest)[0]["timeout_s"] == value
        else:
            with pytest.raises(ValueError, match="timeout_s"):
                manifest_mod.load_manifest(manifest)


@given(
    status=st.none() | st.text() | st.dates() | st.times() | st.datetimes(),
    vintage=st.dates(),
    licence=st.text(),
    redistribute=st.booleans(),
)
def test_status_metadata_is_json_serializable(
    status: object, vintage: object, licence: str, *, redistribute: bool
) -> None:
    with tempfile.TemporaryDirectory() as folder:
        rows = report_mod.status_records(
            [
                {
                    "id": "example",
                    "status": status,
                    "vintage": vintage,
                    "license": licence,
                    "redistribute": redistribute,
                }
            ],
            log=Path(folder) / "missing.jsonl",
        )
    (row,) = json.loads(json.dumps(rows, ensure_ascii=False))
    assert row["status"] == (str(status) if status is not None else None)
    assert row["vintage"] == str(vintage)
    assert row["license"] == (licence or None)
    assert row["redistribute"] is redistribute


@settings(deadline=None)
@given(
    written=st.lists(_records, max_size=8),
    separator=st.sampled_from(["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]),
    terminated=st.booleans(),
)
def test_streamed_history_preserves_splitlines_records(
    written: list[ledger.FetchRecord], separator: str, *, terminated: bool
) -> None:
    with tempfile.TemporaryDirectory() as folder:
        log = Path(folder) / "history.jsonl"
        text = separator.join(json.dumps(ledger.log_fields(record)) for record in written)
        log.write_bytes((text + (separator if terminated else "")).encode())
        assert ledger._last_records(log) == _last_by_id(written)  # noqa: SLF001


@settings(deadline=None, max_examples=50)
@given(
    payload=st.binary(max_size=2048),
    fmt=st.sampled_from(["file", "csv"]),
    parameters=st.dictionaries(
        st.text("xyz_09", min_size=1, max_size=8), st.text(max_size=8) | st.integers(), max_size=3
    ),
    # Naive bounds, as hypothesis wants; the timezones strategy makes each value aware.
    retrieved_at=st.none() | st.datetimes(max_value=datetime(2026, 1, 1), timezones=st.timezones()),  # noqa: DTZ001
)
def test_a_recorded_file_verifies_and_reads_back(
    payload: bytes, fmt: str, parameters: dict[str, str | int], retrieved_at: object
) -> None:
    """Whatever bytes and metadata record() takes, the sidecar reads back as its record and verify passes."""
    if fmt == "csv":
        payload = b"value\n" + payload.replace(b"\r", b"").replace(b"\n", b"").replace(b",", b"").replace(b'"', b"")
        payload = payload.decode("utf-8", "replace").encode() + b"\n"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        file = root / "download.bin"
        file.write_bytes(payload)
        source = {
            "id": "s",
            "status": "implemented",
            "url": "https://example.org/s.csv",
            "format": fmt,
            "terms": "t",
            "terms_url": "https://example.org/t",
            "license": "CC0-1.0",
            "redistribute": True,
        }
        raw, processed, log = root / "raw", root / "processed", root / "fetches.jsonl"
        result = ledger.record(source, file, retrieved_at=retrieved_at, parameters=parameters, raw=raw, log=log)  # ty: ignore[invalid-argument-type]
        assert ledger._record(source, raw) == result  # noqa: SLF001
        assert ledger._last_records(log)["s"] == ledger.log_fields(result)  # noqa: SLF001
        assert ledger.verified(source, raw=raw) == (result, payload)
        builder_mod.build([source], raw=raw, processed=processed)
        assert report_mod.verify(processed) == []
