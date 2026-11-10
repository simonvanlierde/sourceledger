"""Parse raw files and write the outputs and datapackage.json, each row carrying its provenance."""

from __future__ import annotations

import csv
import gzip
import io
import itertools
import json
import re
import shutil
import tomllib
import unicodedata
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sourceledger.ledger import (
    _CHUNK_BYTES,
    _UNUSABLE,
    RAW,
    FetchRecord,
    _check_hash,
    _digest,
    _record,
    _replacing,
    _sha256,
    _write_atomic,
)
from sourceledger.manifest import (
    _MAX_BYTES,
    FILE_FORMAT,
    Source,
    _check_name,
    _check_output_format,
    implemented,
    output_name,
    raw_name,
    select,
)

if TYPE_CHECKING:
    from _csv import Reader as _CSVReader


PROCESSED = Path("data/processed")


# Written by build beside the outputs.
DATAPACKAGE = "datapackage.json"


_DATAPACKAGE_PROFILE = "https://datapackage.org/profiles/2.0/datapackage.json"


_PARQUET_SUFFIX = ".parquet"


_PARQUET_BATCH_ROWS = 10_000


_GZIP_MAGIC = b"\x1f\x8b"


# Reads one raw file as its columns and rows. `parse_source` is the default. A cell may be any
# value: None is written as "", anything else through str().
type Parser = Callable[[Source, bytes], tuple[list[str], Iterable[Mapping[str, object]]]]


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

    Raises ValueError for a file that is not UTF-8, a repeated column name, a row with more or
    fewer fields than the header, or a gzipped file that is damaged or decompresses past the
    source's `max_bytes`.
    """
    text = _text(source, _maybe_gunzip(source, payload))
    columns, rows = _csv_rows(source, io.StringIO(text, newline=""))
    return columns, [dict(zip(columns, row, strict=True)) for row in rows]


def _text(source: Source, payload: bytes) -> str:
    """The payload decoded as UTF-8. Raises ValueError naming the source and the first byte that is not UTF-8."""
    # utf-8-sig drops the byte-order mark that spreadsheet exports start with. Plain UTF-8 reads the same.
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        where = f"byte 0x{error.object[error.start]:02x} at offset {error.start}"
        msg = f"{source['id']}: the raw file is not UTF-8 ({where}); re-save it as UTF-8"
        raise ValueError(msg) from error


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
    as JSON text. Raises ValueError for a file that is not UTF-8, anything but an array of
    objects, or one nested too deeply to read.
    """
    payload = _maybe_gunzip(source, payload)
    # From 3.14 the reader's depth limit follows the stack size, so a deep value can load and
    # then overflow while its cell is written: both steps are covered.
    try:
        data = json.loads(_text(source, payload), parse_float=_Number, parse_int=_Number)
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


_PARSERS: dict[str, Callable[[Source, bytes], tuple[list[str], list[dict[str, str]]]]] = {
    "csv": parse_csv,
    "json": parse_json,
    "xlsx": parse_xlsx,
}


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
    name: str | None = None,
) -> list[Path]:
    """Write implemented sources to `processed`: unchanged files, or tables stamped with their fetch records.

    `format = "file"` copies the original bytes to `processed/<raw_name>`, without parsing.
    For tables, `parse` reads each raw file; the default reads CSV, JSON and Excel by format.
    `output_format` selects CSV (the default) or Parquet with zstd compression and an explicit
    string schema. Parquet needs the parquet extra and writes batches of at most 10,000 rows.
    Then writes `processed/datapackage.json`, a Frictionless Data Package (v2) describing every
    output: its columns, size and SHA-256, its source URL and licence, and its fetch record.
    The package is called `name`; without one, the `[project]` name in ./pyproject.toml, or else
    the current folder's name, either made lower case. The same records and name give the same
    file, byte for byte.

    `only` restricts the build to those source ids. The package keeps each other source's
    entry as the last build wrote it. Raises ValueError for an id the manifest does not have,
    or a file output named `datapackage.json`, which is reserved for the descriptor.
    """
    return [
        out
        for out, _count in build_sources(
            sources, raw=raw, processed=processed, parse=parse, only=only, output_format=output_format, name=name
        )
    ]


def build_sources(
    sources: list[Source],
    *,
    raw: Path = RAW,
    processed: Path = PROCESSED,
    parse: Parser = parse_source,
    only: Collection[str] | None = None,
    output_format: str = "csv",
    name: str | None = None,
) -> list[tuple[Path, int | None]]:
    """Build outputs with row counts for tables, or None for unchanged file copies."""
    _check_output_format(output_format)
    _check_name(name)
    # Before any output is written: a broken pyproject.toml must not cost a whole build.
    name = name or _package_name()
    selected = {s["id"] for s in select(sources, only)}
    earlier = {} if only is None else _listed(processed / DATAPACKAGE)
    processed.mkdir(parents=True, exist_ok=True)
    written = []
    resources = []
    for source in implemented(sources):
        if source["id"] not in selected:
            if source["id"] in earlier:
                resources.append(earlier[source["id"]])
            continue
        out = processed / output_name(source, output_format)
        if out.name.lower() == DATAPACKAGE:
            msg = f"{DATAPACKAGE} is reserved for the package descriptor; choose another source id"
            raise ValueError(msg)
        record, fields, count = _build_one(source, raw, out, parse)
        written.append((out, count))
        resources.append(_resource(source, record, fields, out))
    package = {"$schema": _DATAPACKAGE_PROFILE, "name": name, "resources": resources}
    _write_atomic(processed / DATAPACKAGE, json_bytes(package))
    return written


def json_bytes(document: object) -> bytes:
    """`document` as `build` writes datapackage.json: UTF-8, indented by 2, ending in a line break."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


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
            # NOTE: other formats and custom parsers hold the whole file in memory, and the
            # built-in ones their rows too; stream them once such sources outgrow memory.
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
    str(), as csv.writer does. Raises ValueError for text UTF-8 cannot hold: an unpaired surrogate.
    """
    if clash := stamp.keys() & set(columns):
        msg = f"{source['id']}: source columns {sorted(clash)} collide with the provenance columns"
        raise ValueError(msg)
    fields, head = [*stamp, *columns], list(stamp.values())
    stamped = ([*head, *row] for row in rows)
    try:
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
    except UnicodeEncodeError as error:
        # JSON can escape half of a UTF-16 pair, such as \ud800, which UTF-8 cannot write.
        msg = (
            f"{source['id']}: the raw file holds an unpaired surrogate escape, {error.object[error.start]!r}, "
            "which is not text; remove it or write the whole character"
        )
        raise ValueError(msg) from error
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
    """The project's name, from ./pyproject.toml or else the folder, lower-cased and without accents."""
    try:
        with Path("pyproject.toml").open("rb") as fh:
            project = tomllib.load(fh).get("project")
    except FileNotFoundError:
        project = None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        msg = f"pyproject.toml: {error}; fix it, or set name in sources.toml"
        raise ValueError(msg) from None
    named = project.get("name") if isinstance(project, dict) else None
    text = named if isinstance(named, str) and named.strip() else Path.cwd().name
    letters = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9._-]+", "-", letters.lower()).strip("-") or "dataset"


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
        msg = f"{path.as_posix()}: not a readable Parquet table ({error})"
        raise ValueError(msg) from error


def read_output(path: Path) -> Iterator[dict[str, str]]:
    """The rows of a published CSV or Parquet table, as written, one at a time.

    CSV is read row by row, Parquet in batches of at most 10,000 rows; Parquet needs the
    parquet extra. Wrap the call in `list()` to hold the whole table. This is a generator, so
    nothing runs until the first `next()`: every error, a missing file included (OSError), and
    ValueError for a Parquet file that cannot be read, is raised there. An iterator left
    unfinished keeps the file open; on Windows that blocks replacing the output, so exhaust it
    or call `close()`.
    """
    if path.suffix == _PARQUET_SUFFIX:
        for batch in _parquet_batches(path):
            yield from batch.to_pylist()
        return
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)
