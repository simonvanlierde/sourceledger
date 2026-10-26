"""Property tests for the code that reads outside input: the fetch log, response headers and parsed rows."""

import csv
import dataclasses
import io
import json
import tempfile
from email.message import Message
from pathlib import Path

from hypothesis import example, given, settings
from hypothesis import strategies as st

from sourceledger import ledger

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
)


def _last_by_id(written: list[ledger.FetchRecord]) -> dict[str, dict[str, object]]:
    return {record.source_id: dataclasses.asdict(record) for record in written}


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
    assert 0.0 <= ledger._retry_after(_message("Retry-After", value)) <= ledger._MAX_RETRY_AFTER_S  # noqa: SLF001


@given(st.text())
@example("29, 29")
@example("٢٩")
@example("²")
def test_content_length_never_raises(value: str) -> None:
    """Any Content-Length gives a byte count or None."""
    length = ledger._content_length(_message("Content-Length", value))  # noqa: SLF001
    assert length is None or length >= 0


_provenance = {"source_id", "source_url", "retrieved_at", "vintage", "raw_sha256"}
# Unique: a repeated column name is refused by design.
_columns = st.lists(
    st.text(min_size=1, max_size=8).filter(lambda c: c not in _provenance), min_size=1, max_size=4, unique=True
)


def _built(name: str, payload: bytes, parse: ledger.Parser) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The rows `parse` reads from `payload`, and the rows read back after a fetch and build of it."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        upstream = root / name
        upstream.write_bytes(payload)
        source = {"id": "s", "status": "implemented", "url": upstream.as_uri()}
        raw, processed = root / "raw", root / "processed"
        ledger.fetch([source], raw=raw, log=root / "fetches.jsonl")
        _, parsed = parse(source, payload)
        (out,) = ledger.build([source], raw=raw, processed=processed, parse=parse)
        read = [{k: v for k, v in row.items() if k not in _provenance} for row in ledger.read_output(out)]
        return parsed, read


@settings(deadline=None)
@given(
    _columns.flatmap(
        lambda cols: st.tuples(
            st.just(cols), st.lists(st.lists(st.text(), min_size=len(cols), max_size=len(cols)), max_size=4)
        )
    )
)
def test_csv_rows_survive_build_and_read_output(table: tuple[list[str], list[list[str]]]) -> None:
    """What parse_csv reads is what the published output holds, field for field."""
    columns, rows = table
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(rows)
    parsed, read = _built("s.csv", buffer.getvalue().encode(), ledger.parse_csv)
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
    parsed, read = _built("s.json", json.dumps(items).encode(), ledger.parse_json)
    assert read == parsed
