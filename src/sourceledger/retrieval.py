"""Resolve finite retrieval requests without interpreting publisher data."""

# ruff: noqa: PLR2004  # literal tokens define the manifest grammar

from __future__ import annotations

import hashlib
import itertools
import json
import re
import string
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode, urlsplit

if TYPE_CHECKING:
    from sourceledger.ledger import Source

MAX_REQUESTS = 1000
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def canonical(value: object) -> bytes:
    """Stable UTF-8 encoding for request and generation identities."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def request_key(source: Source) -> str:
    """A resource/history key distinct from every legacy source id."""
    return f"{source['id']}:{source['request_id']}" if source.get("request_id") else source["id"]


def _dimension(value: object) -> list[str | int] | range:
    if isinstance(value, dict) and value.keys() == {"start", "stop"}:
        start, stop = value["start"], value["stop"]
        if type(start) is int and type(stop) is int and start <= stop and stop - start < MAX_REQUESTS:
            return range(start, stop + 1)
    elif isinstance(value, list) and value and len(value) <= MAX_REQUESTS:
        if all(type(item) in (str, int) for item in value) and len(set(value)) == len(value):
            return value
    msg = "request.expand needs unique nonempty scalar lists or inclusive integer ranges (max 1000)"
    raise ValueError(msg)


def _template(template: object, parameters: dict[str, str | int], *, url: bool, used: set[str]) -> str:
    if not isinstance(template, str) or "env:" in template:
        msg = "request templates need literal strings without environment references"
        raise ValueError(msg)
    parts = []
    for literal, name, spec, conversion in string.Formatter().parse(template):
        # Escaped braces would introduce a second template language; require only plain substitutions.
        if "{" in literal or "}" in literal:
            msg = "request templates support only {name} substitutions"
            raise ValueError(msg)
        parts.append(literal)
        if name is not None:
            if not _NAME.fullmatch(name) or spec or conversion or name not in parameters:
                msg = "request templates support only known {name} substitutions"
                raise ValueError(msg)
            used.add(name)
            value = str(parameters[name])
            parts.append(quote(value, safe="") if url else value)
    return "".join(parts)


def _body_value(value: object, parameters: dict[str, str | int], used: set[str]) -> object:
    """Body substitutions occupy whole values, so numbers keep their type."""
    if isinstance(value, dict):
        if any(not isinstance(k, str) or any(token in k for token in ("{", "}", "env:")) for k in value):
            msg = "request body keys must be literal strings without templates or environment references"
            raise ValueError(msg)
        return {key: _body_value(item, parameters, used) for key, item in value.items()}
    if isinstance(value, list):
        return [_body_value(item, parameters, used) for item in value]
    if isinstance(value, str):
        if "env:" in value:
            msg = "request bodies cannot contain environment references"
            raise ValueError(msg)
        if "{" in value or "}" in value:
            match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
            if match is None or match[1] not in parameters:
                msg = "request body substitutions must occupy a whole value and name a known parameter"
                raise ValueError(msg)
            used.add(match[1])
            return parameters[match[1]]
    return value


def _body(request: Source, parameters: dict[str, str | int], used: set[str]) -> tuple[bytes, str, str]:
    encoding = "json" if "json" in request else "form"
    value = _body_value(request[encoding], parameters, used)
    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error:
        msg = "request body must contain finite JSON-compatible values"
        raise ValueError(msg) from error
    if encoding == "json":
        return serialized.encode(), "application/json", encoding
    if not isinstance(value, dict) or any(type(v) not in (str, int, float, bool) for v in value.values()):
        msg = "request.form needs a table of scalar values"
        raise ValueError(msg)
    return urlencode(sorted(value.items())).encode(), "application/x-www-form-urlencoded", encoding


def expand_requests(  # noqa: C901, PLR0912, PLR0915  # one bounded manifest grammar
    sources: list[Source], *, output_format: str | None = "csv"
) -> list[Source]:
    """Resolve requests in source order, sorted dimension order and explicit value order.

    Logical ids remain unchanged. Each copy adds a stable request_id, resolved parameters,
    filename and URL. Plain sources keep their existing shape. All names and the total
    request count are checked before any files or network requests are created.
    """
    resolved = []
    for source in sources:
        request = source.get("request")
        if "request" not in source or source.get("request_id") or source.get("status") != "implemented":
            resolved.append(source)
            continue
        if not isinstance(request, dict) or request.keys() - {
            "method",
            "url",
            "filename",
            "values",
            "expand",
            "sha256",
            "retrieval_only",
            "json",
            "form",
        }:
            msg = "request supports GET or retrieval-only POST with json or form bodies"
            raise ValueError(msg)
        method = request.get("method", "GET")
        if method not in ("GET", "POST") or "url" in source or "sha256" in source:
            msg = (
                "declarative retrieval uses GET or POST, request.url and per-filename request.sha256, "
                "without source.url or source.sha256"
            )
            raise ValueError(msg)
        if method == "POST":
            if request.get("retrieval_only") is not True or ("json" in request) == ("form" in request):
                msg = "request POST requires retrieval_only = true and exactly one json or form body"
                raise ValueError(msg)
            if source.get("archive") or any(
                k.lower() in {"content-type", "content-length", "transfer-encoding"} for k in source.get("headers", {})
            ):
                msg = "POST does not support archiving or configured content headers"
                raise ValueError(msg)
        elif any(k in request for k in ("json", "form", "retrieval_only")):
            msg = "GET does not accept a body or retrieval_only"
            raise ValueError(msg)
        values, dimensions = request.get("values", {}), request.get("expand", {})
        if not isinstance(values, dict) or not isinstance(dimensions, dict):
            msg = "request.values and request.expand need tables"
            raise ValueError(msg)  # noqa: TRY004  # manifest errors use ValueError
        if values.keys() & dimensions.keys() or any(not _NAME.fullmatch(k) for k in [*values, *dimensions]):
            msg = "request parameter names must be distinct identifiers"
            raise ValueError(msg)
        if any(type(v) not in (str, int) or (isinstance(v, str) and "env:" in v) for v in values.values()):
            msg = "request.values supports strings and integers without environment references"
            raise ValueError(msg)
        names = sorted(dimensions)
        axes = [_dimension(dimensions[name]) for name in names]
        count = 1
        for axis in axes:
            count *= len(axis)
        if sum(s.get("status") == "implemented" for s in resolved) + count > MAX_REQUESTS:
            msg = "request plan exceeds 1000 requests"
            raise ValueError(msg)
        files = []
        for combination in itertools.product(*axes):
            parameters = {**values, **dict(zip(names, combination, strict=True))}
            if any(isinstance(v, str) and "env:" in v for v in parameters.values()):
                msg = "request parameters cannot contain environment references"
                raise ValueError(msg)
            used: set[str] = set()
            url = _template(request.get("url"), parameters, url=True, used=used)
            filename = _template(request.get("filename"), parameters, url=False, used=used)
            body = _body(request, parameters, used) if method == "POST" else None
            if used != parameters.keys():
                msg = "request has unused parameters"
                raise ValueError(msg)
            _check_filename(filename)
            split = urlsplit(url)
            if method == "POST" and split.scheme not in ("http", "https"):
                msg = "request POST needs an HTTP or HTTPS URL"
                raise ValueError(msg)
            if split.username is not None or split.password is not None:
                msg = "request URLs cannot contain credentials; use environment headers over HTTPS"
                raise ValueError(msg)
            identity = {
                "version": 1,
                "method": method,
                "url": url,
                "parameters": parameters,
                "headers": source.get("headers", {}),
            }
            if body is not None:
                identity.update(body=body[0].decode(), content_type=body[1], body_encoding=body[2])
            item = {
                **source,
                "method": method,
                "url": url,
                "filename": filename,
                "parameters": parameters,
                "request_id": hashlib.sha256(canonical(identity)).hexdigest(),
            }
            if body is not None:
                item.update(body=body[0], content_type=body[1], body_encoding=body[2])
            pins = request.get("sha256")
            if pins is not None:
                if (
                    not isinstance(pins, dict)
                    or not isinstance(pin := pins.get(filename), str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", pin)
                ):
                    msg = "request.sha256 needs an exact filename-to-SHA256 mapping"
                    raise ValueError(msg)
                item["sha256"] = pin.lower()
            resolved.append(item)
            files.append(filename)
        if request.get("sha256") is not None and set(request["sha256"]) != set(files):
            msg = "request.sha256 must cover exactly the resolved filenames"
            raise ValueError(msg)
    if sum(s.get("status") == "implemented" for s in resolved) > MAX_REQUESTS:
        msg = "request plan exceeds 1000 requests"
        raise ValueError(msg)
    if any(s.get("request_id") for s in resolved):
        _check_names(resolved, output_format)
    return resolved


def _check_filename(filename: object) -> None:
    if (
        not isinstance(filename, str)
        or not _FILENAME.fullmatch(filename)
        or filename.endswith(".")
        or filename.split(".")[0].upper() in _RESERVED
    ):
        msg = "request filename must be a safe ASCII filename without reserved platform names"
        raise ValueError(msg)


def _check_names(sources: list[Source], output_format: str | None) -> None:
    from sourceledger.ledger import _output_name, raw_name  # noqa: PLC0415  # ledger uses expansion at entry points

    raw_names = {".lock", "datapackage.json"}
    output_names = {"datapackage.json"}
    for source in sources:
        if source.get("status") != "implemented":
            continue
        if request_id := source.get("request_id"):
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{64}", request_id):
                msg = "resolved request_id must be a SHA-256 identifier"
                raise ValueError(msg)
            _check_filename(source.get("filename"))
        raw = raw_name(source)
        output = _output_name(source, output_format) if output_format is not None else None
        names = {raw.lower(), f"{raw}.fetch.json".lower()}
        if names & raw_names or (output is not None and output.lower() in output_names):
            msg = "request filenames collide with another response, sidecar or built output"
            raise ValueError(msg)
        raw_names.update(names)
        if output is not None:
            output_names.add(output.lower())
