"""Read `sources.toml`: its source tables, the checks on them and the names derived from them."""

from __future__ import annotations

import difflib
import re
import sys
import tomllib
from collections import Counter
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Collection


MANIFEST = Path("sources.toml")


# A manifest's first line names it, for an editor to check the file. The copy at this
# version's tag, not the $id on main: main can accept or refuse keys this version doesn't.
_SCHEMA_URL = (
    f"https://raw.githubusercontent.com/simonvanlierde/sourceledger/v{version('sourceledger')}"
    "/src/sourceledger/sources.schema.json"
)


_STARTER = f"""#:schema {_SCHEMA_URL}

# One [[source]] table per input. Uncomment the example and point it at your own source.
# Every key: https://simonvanlierde.com/sourceledger/reference.html#sourcestoml

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


# One source's download budget includes attempts and waits. DNS lookup and HTTP header
# parsing can exceed it.
_BUDGET_S = 300


# A runaway or wrong URL fails the fetch instead of filling the disk. A big source sets
# its own `max_bytes` in sources.toml.
_MAX_BYTES = 200 * 1024 * 1024


_SHA256 = re.compile(r"[0-9a-f]{64}")


_ENV_REF = re.compile(r"env:([A-Za-z_][A-Za-z0-9_]*)")
# C0 except tab, LF and CR, then DEL and C1.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


# Headers a manifest may not set: the fetch's own.
_RESERVED_HEADERS = frozenset({"user-agent", "accept-encoding", "if-none-match", "if-modified-since"})


type Source = dict[str, Any]


# The formats `build._PARSERS` reads; a test keeps the two in step.
_FORMATS = ("csv", "json", "xlsx")
_MANIFEST_KEYS = frozenset({"output_format", "release_formats", "source"})
# The optional metadata files `release` writes when `release_formats` asks for them.
RELEASE_FORMATS = ("ro-crate", "croissant")


def load_manifest(path: Path = MANIFEST) -> list[Source]:
    """The `[[source]]` tables of sources.toml, after checking ids, URLs, caps, timeouts and pins."""
    with path.open("rb") as fh:
        manifest = tomllib.load(fh)
    # A typo such as [[sources]] must not read as an empty manifest: build would then write
    # an empty datapackage.json over a good one.
    for key in sorted(manifest.keys() - _MANIFEST_KEYS):
        close = difflib.get_close_matches(key.lower().replace("-", "_"), _MANIFEST_KEYS, n=1, cutoff=0.6)
        hint = f"; did you mean {close[0]}?" if close else ""
        msg = f"sources.toml: {key!r} is not a sources.toml key{hint}"
        raise ValueError(msg)
    _check_output_format(manifest.get("output_format", "csv"))
    _check_release_formats(manifest.get("release_formats", []))
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
        _check_placement(source)
        _check_text(source)
        _check_pin(source)
        _check_vintage(source)
        _check_format(source)
        _check_headers(source)
    return sources


def _check_placement(source: Source) -> None:
    """Refuse a project-wide key that an appended line put in the last [[source]] table, where it does nothing."""
    for key in ("output_format", "release_formats"):
        if key in source:
            msg = f"source {source['id']!r} in sources.toml sets {key}; put it above the first [[source]]"
            raise ValueError(msg)


def _check_text(source: Source) -> None:
    """Refuse a control character: manifest text reaches the terminal, where an ESC could rewrite the screen."""
    if any(_CONTROL.search(value) for value in source.values() if isinstance(value, str)):
        msg = f"{source['id']}: sources.toml text may not hold a control character other than a tab or line break"
        raise ValueError(msg)


def printable(text: str) -> str:
    r"""`text` with each control character `_check_text` refuses shown as an escape, such as `\x1b`."""
    return _CONTROL.sub(lambda match: match[0].encode("unicode_escape").decode(), text)


def _check_output_format(output_format: str) -> None:
    """Refuse unsupported table output formats before writing any files."""
    if output_format not in ("csv", "parquet"):
        msg = "output_format must be 'csv' or 'parquet'"
        raise ValueError(msg)


def load_output_format(path: Path = MANIFEST) -> str:
    """The project's `output_format` from sources.toml, "csv" when unset: pass it to `build` and `validate`."""
    with path.open("rb") as fh:
        output_format = tomllib.load(fh).get("output_format", "csv")
    _check_output_format(output_format)
    return output_format


def _check_release_formats(formats: object) -> None:
    """Refuse a `release_formats` that is not a list of known formats, each named once."""
    if not (
        isinstance(formats, list) and all(f in RELEASE_FORMATS for f in formats) and len(set(formats)) == len(formats)
    ):
        msg = f"release_formats in sources.toml must list each of {list(RELEASE_FORMATS)} at most once"
        raise ValueError(msg)


def load_release_formats(path: Path = MANIFEST) -> list[str]:
    """The project's `release_formats` from sources.toml, empty when unset: pass it to `release`."""
    with path.open("rb") as fh:
        formats = tomllib.load(fh).get("release_formats", [])
    _check_release_formats(formats)
    return formats


def init(path: Path = MANIFEST) -> None:
    """Write a starter sources.toml at `path`: the schema line and one commented example source.

    Raises FileExistsError when `path` exists, which is never overwritten.
    """
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(_STARTER)
    except FileExistsError as error:
        msg = f"{path.as_posix()} already exists; not overwriting it"
        raise FileExistsError(msg) from error


def select(sources: list[Source], only: Collection[str] | None) -> list[Source]:
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
    if (fmt := source.get("format")) is not None and fmt not in (*_FORMATS, FILE_FORMAT):
        msg = f"{source['id']}: format in sources.toml must be one of {sorted([*_FORMATS, FILE_FORMAT])}, not {fmt!r}"
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


def output_name(source: Source, output_format: str) -> str:
    """The built file's name; an unimplemented source may have no URL yet."""
    return (
        raw_name(source)
        if source.get("format") == FILE_FORMAT and source.get("url")
        else f"{source['id']}.{output_format}"
    )
