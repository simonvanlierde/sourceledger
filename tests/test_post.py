"""Declared retrieval POSTs retain body identity and the existing transport limits."""

# ruff: noqa: SLF001  # verify private cache markers and redirect guards

import dataclasses
import http.client
import http.server
import io
import json
import threading
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Generator
from email.message import Message
from pathlib import Path
from typing import Any, Never
from urllib.parse import parse_qsl

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sourceledger import FetchError, expand_requests, fetch, ledger, published, read_output, update


def source(url: str, **request: object) -> dict[str, Any]:
    return {
        "id": "lookup",
        "status": "implemented",
        "format": "csv",
        "terms": "CC0",
        "terms_url": "https://example.org/terms",
        "license": "CC0-1.0",
        "redistribute": True,
        "request": {
            "method": "POST",
            "retrieval_only": True,
            "url": url,
            "filename": "lookup.csv",
            "json": {"year": 2024},
            **request,
        },
    }


def response(payload: bytes = b"value\n001\n", url: str = "https://example.org/lookup") -> urllib.response.addinfourl:
    headers = Message()
    headers["Content-Length"] = str(len(payload))
    headers["ETag"] = '"v1"'
    return urllib.response.addinfourl(io.BytesIO(payload), headers, url)


def test_post_encoding_identity_and_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = [
        source(
            "https://example.org/lookup",
            json={"z": "é", "year": "{year}"},
            expand={"year": [2023, 2024]},
            filename="lookup-{year}.csv",
        )
    ]
    resolved = expand_requests(sources)
    assert [s["body"] for s in resolved] == ['{"year":2023,"z":"é"}'.encode(), '{"year":2024,"z":"é"}'.encode()]
    assert len({s["request_id"] for s in resolved}) == 2
    assert resolved[0]["content_type"] == "application/json"
    assert resolved[0]["body_encoding"] == "json"
    requests = []

    def serve(request: urllib.request.Request, timeout: float) -> urllib.response.addinfourl:
        assert timeout > 0
        requests.append(request)
        return response()

    monkeypatch.setattr(ledger, "_open", serve)
    records = fetch(sources, raw=tmp_path / "raw", log=tmp_path / "log")
    assert [r.get_method() for r in requests] == ["POST", "POST"]
    assert [r.data for r in requests] == [s["body"] for s in resolved]
    assert all(r.get_header("Content-type") == "application/json" for r in requests)
    assert records[0].method == "POST"
    assert records[0].body == '{"year":2023,"z":"é"}'
    assert records[0].body_encoding == "json"
    assert records[0].content_type == "application/json"


@pytest.mark.parametrize(
    "changes",
    [
        {"retrieval_only": False},
        {"retrieval_only": 1},
        {"retrieval_only": None},
        {"form": {"year": 2024}},
        {"json": {"invalid": {1, 2}}},
        {"method": "PUT"},
        {"json": {"{year}": 1}},
        {"json": {"bad{": 1}},
        {"json": {"bad}": 1}},
        {"json": {"env:TOKEN": 1}},
        {"json": {"x": "prefix-{year}"}},
        {"json": {"x": "{year"}},
        {"json": {"x": "year}"}},
        {"json": {"x": "env:TOKEN"}},
        {"json": {"x": float("nan")}},
        {"json": {"x": "{year!s}"}},
    ],
)
def test_post_rejects_unsafe_or_ambiguous_requests(changes: dict):
    with pytest.raises(ValueError, match=r"request|POST|GET|body"):
        expand_requests([source("https://example.org/lookup", **changes)])


def test_post_rejects_archive_and_conflicting_headers():
    for extra in (
        {"archive": True},
        *({"headers": {name: "env:VALUE"}} for name in ("Content-Type", "Content-Length", "Transfer-Encoding")),
    ):
        with pytest.raises(ValueError, match=r"request|POST|GET|body"):
            expand_requests([{**source("https://example.org/lookup"), **extra}])
    with pytest.raises(ValueError, match=r"request|POST|GET|body"):
        expand_requests([source("https://example.org/lookup", method="GET")])


@given(st.dictionaries(st.text(alphabet="abcxyz", min_size=1, max_size=6), st.integers(), max_size=5))
def test_json_key_order_preserves_encoding_and_identity(body: dict):
    first = expand_requests([source("https://example.org/lookup", json=body)])[0]
    second = expand_requests([source("https://example.org/lookup", json=dict(reversed(list(body.items()))))])[0]
    assert first["body"] == second["body"]
    assert first["request_id"] == second["request_id"]
    assert json.loads(first["body"]) == body


def test_form_encoding_preserves_scalar_substitutions():
    s = source("https://example.org/lookup", form={"year": "{year}", "region": "a + é"}, values={"year": 2024})
    del s["request"]["json"]
    resolved = expand_requests([s])[0]
    assert resolved["body"] == b"region=a+%2B+%C3%A9&year=2024"
    assert resolved["content_type"] == "application/x-www-form-urlencoded"
    assert resolved["body_encoding"] == "form"


def test_nested_json_arrays_preserve_values_and_parameter_types():
    s = source(
        "https://example.org/lookup",
        json={"rows": ["{Year_2}", {"year": "{Year_2}"}, "literal"]},
        values={"Year_2": 2024},
    )
    resolved = expand_requests([s])[0]
    assert json.loads(resolved["body"]) == {"rows": [2024, {"year": 2024}, "literal"]}


@pytest.fixture
def publisher() -> Generator[tuple[str, list[tuple[str, str, bytes, dict[str, str]]]]]:
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.path, self.command, body, dict(self.headers)))
            if self.path.startswith("/redirect/"):
                self.send_response(int(self.path.rsplit("/", 1)[1]))
                self.send_header("Location", "/data")
                self.send_header("Content-Length", "999999999")
                self.end_headers()
                return
            if self.path.startswith("/cross/"):
                self.send_response(int(self.path.rsplit("/", 1)[1]))
                self.send_header("Location", f"http://localhost:{server.server_port}/data")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if (self.path.startswith("/retry/") and len([c for c in calls if c[0] == self.path]) == 1) or (
                self.path == "/recover" and body == b'{"year":2024}' and len(calls) == 2
            ):
                self.send_response(int(self.path.rsplit("/", 1)[1]) if self.path.startswith("/retry/") else 503)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/304" or (self.path == "/after304" and len(calls) > 1):
                self.send_response(304)
                self.end_headers()
                return
            payload = b"value\n001\n"
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            calls.append((self.path, self.command, b"", dict(self.headers)))
            self.send_error(405)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # HTTP handler API
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_post_redirects_keep_method_and_body_or_refuse(publisher: tuple[str, list], tmp_path: Path, code: int):
    base, calls = publisher
    sources = [source(f"{base}/redirect/{code}")]
    if code in (301, 302, 303):
        with pytest.raises(FetchError, match="POST redirect"):
            fetch(sources, raw=tmp_path / "raw", log=tmp_path / "log")
        assert len(calls) == 1
    else:
        fetch(sources, raw=tmp_path / "raw", log=tmp_path / "log")
        assert [(c[0], c[1], c[2]) for c in calls] == [
            (f"/redirect/{code}", "POST", b'{"year":2024}'),
            ("/data", "POST", b'{"year":2024}'),
        ]
        assert calls[-1][3]["Content-Type"] == "application/json"


@pytest.mark.parametrize("code", [307, 308])
def test_post_refuses_cross_origin_body_disclosure(publisher: tuple[str, list], tmp_path: Path, code: int):
    base, calls = publisher
    with pytest.raises(FetchError, match="cross-origin POST redirect"):
        fetch([source(f"{base}/cross/{code}")], raw=tmp_path / "raw", log=tmp_path / "log")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_post_replays_retries_without_conditionals(
    publisher: tuple[str, list], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
):
    base, calls = publisher
    monkeypatch.setattr(ledger, "_BACKOFF_S", 0)
    sources = [source(f"{base}/retry/{status}")]
    raw, log = tmp_path / "raw", tmp_path / "log"
    fetch(sources, raw=raw, log=log)
    fetch(sources, raw=raw, log=log)
    assert len(calls) == 3
    assert all(c[1:3] == ("POST", b'{"year":2024}') for c in calls)
    assert all("If-None-Match" not in c[3] and "If-Modified-Since" not in c[3] for c in calls)
    assert not list(raw.glob("*.part"))


def test_post_unsolicited_304_is_not_reused(publisher: tuple[str, list], tmp_path: Path):
    base, _calls = publisher
    with pytest.raises(FetchError, match="304"):
        fetch([source(base + "/304")], raw=tmp_path / "raw", log=tmp_path / "log")


def test_post_304_cannot_reuse_an_existing_response(publisher: tuple[str, list], tmp_path: Path):
    base, calls = publisher
    sources, raw, log = [source(base + "/after304")], tmp_path / "raw", tmp_path / "log"
    fetch(sources, raw=raw, log=log)
    with pytest.raises(FetchError, match="304"):
        fetch(sources, raw=raw, log=log)
    assert len(calls) == 2


@pytest.mark.parametrize("output_format", ["csv", "parquet"])
def test_post_update_builds_per_body_outputs(publisher: tuple[str, list], tmp_path: Path, output_format: str):
    base, calls = publisher
    sources = [
        source(base + "/data", json={"year": "{year}"}, expand={"year": [2023, 2024]}, filename="lookup-{year}.csv")
    ]
    generation = update(sources, root=tmp_path / "updates", workers=2, output_format=output_format)
    assert published(tmp_path / "updates") == generation
    assert len(calls) == 2
    package = json.loads((generation / "processed/datapackage.json").read_text())
    assert len(package["resources"]) == 2
    assert {r["format"] for r in package["resources"]} == {output_format}
    assert len({r["sourceledger"]["body"] for r in package["resources"]}) == 2
    assert read_output(generation / f"processed/lookup-2023.{output_format}")[0]["value"] == "001"


def test_post_credentials_rotate_and_stay_out_of_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = [{**source("https://example.org/lookup"), "headers": {"Authorization": "env:POST_TOKEN"}}]
    seen = []

    def serve(request: urllib.request.Request, timeout: float) -> urllib.response.addinfourl:
        assert timeout > 0
        seen.append(request)
        return response(url="https://example.org/private-first-secret?token=first-secret")

    monkeypatch.setattr(ledger, "_open", serve)
    monkeypatch.setenv("POST_TOKEN", "first-secret")
    raw, log = tmp_path / "raw", tmp_path / "log"
    record = fetch(sources, raw=raw, log=log)[0]
    resolved = expand_requests(sources)[0]
    assert ledger._previous(resolved, raw) == record
    monkeypatch.setenv("POST_TOKEN", "second-secret")
    assert ledger._previous(resolved, raw) is None
    fetch(sources, raw=raw, log=log)
    assert [r.get_header("Authorization") for r in seen] == ["first-secret", "second-secret"]
    assert all(r.get_header("If-none-match") is None for r in seen)
    assert "first-secret" not in log.read_text()
    assert "second-secret" not in log.read_text()
    assert "first-secret" not in (raw / "lookup.csv.fetch.json").read_text()

    def refuse(request: urllib.request.Request, timeout: float) -> Never:
        assert request.get_method() == "POST"
        assert timeout > 0
        msg = "server echoed second-secret"
        raise urllib.error.URLError(msg)

    monkeypatch.setattr(ledger, "_open", refuse)
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    with pytest.raises(FetchError) as failure:
        fetch(sources, raw=raw, log=log)
    assert "second-secret" not in str(failure.value)


def test_post_preserves_public_only_redirect_checks():
    req = urllib.request.Request(
        "https://example.org/a",
        data=b'{"x":1}',
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "fixture-token"},
    )
    req.public_only = True  # ty: ignore[unresolved-attribute]  # transport marks public-only requests
    body = io.BytesIO(b"unread")
    moved = ledger._NoRedirectInward().redirect_request(
        req, body, 307, "Temporary Redirect", http.client.HTTPMessage(), "https://EXAMPLE.org:443/b"
    )
    assert body.closed
    assert moved is not None
    assert getattr(moved, "public_only", False)
    assert moved.data == req.data
    assert moved.get_method() == "POST"
    assert moved.get_header("Authorization") == "fixture-token"


def test_post_port_zero_is_a_different_origin():
    req = urllib.request.Request("https://example.org/a", data=b"{}", method="POST")
    body = io.BytesIO(b"unread")
    with pytest.raises(ValueError, match="cross-origin POST redirect"):
        ledger._NoRedirectInward().redirect_request(
            req, body, 307, "Temporary Redirect", http.client.HTTPMessage(), "https://example.org:0/b"
        )
    assert body.closed


def test_post_plain_http_default_port_is_the_same_origin():
    req = urllib.request.Request("http://127.0.0.1/a", data=b"{}", method="POST")
    body = io.BytesIO(b"unread")
    moved = ledger._NoRedirectInward().redirect_request(
        req, body, 307, "Temporary Redirect", http.client.HTTPMessage(), "http://127.0.0.1:80/b"
    )
    assert moved is not None
    assert moved.get_method() == "POST"
    assert moved.data == b"{}"
    assert body.closed


def test_post_rejects_non_http_and_nested_form_values():
    with pytest.raises(ValueError, match="HTTP"):
        expand_requests([source("file:///tmp/source.csv")])
    s = source("https://example.org/lookup", form={"nested": [1, 2]})
    del s["request"]["json"]
    with pytest.raises(ValueError, match="scalar"):
        expand_requests([s])


def test_post_record_metadata_is_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = [source("https://example.org/lookup")]
    monkeypatch.setattr(ledger, "_open", lambda _request, **_kwargs: response())
    raw, log = tmp_path / "raw", tmp_path / "log"
    record = fetch(sources, raw=raw, log=log)[0]
    path = raw / "lookup.csv.fetch.json"
    fields = dataclasses.asdict(record)
    fields["body"] = '{"year":2025}'
    path.write_text(json.dumps(fields))
    assert ledger._previous(expand_requests(sources)[0], raw) is None


def test_post_retry_reuses_only_verified_successful_bodies(
    publisher: tuple[str, list], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    base, calls = publisher
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    sources = [
        source(base + "/recover", json={"year": "{year}"}, expand={"year": [2023, 2024]}, filename="lookup-{year}.csv")
    ]
    root = tmp_path / "updates"
    with pytest.raises(FetchError):
        update(sources, root=root)
    with pytest.raises(ValueError, match="no complete dataset"):
        published(root)
    generation = update(sources, root=root)
    assert [c[2] for c in calls] == [b'{"year":2023}', b'{"year":2024}', b'{"year":2024}']
    assert published(root) == generation


@given(st.dictionaries(st.text(alphabet="abcxyz", min_size=1, max_size=6), st.text(alphabet="abcxyz +é"), max_size=5))
def test_form_round_trip_preserves_values(body: dict[str, str]):
    s = source("https://example.org/lookup", form=body)
    del s["request"]["json"]
    resolved = expand_requests([s])[0]
    assert parse_qsl(resolved["body"].decode(), keep_blank_values=True) == sorted(body.items())


@pytest.mark.parametrize("encoding", ["json", "form"])
def test_schema_accepts_post_and_rejects_undeclared_or_ambiguous_bodies(encoding: str):
    from jsonschema import Draft202012Validator  # noqa: PLC0415  # schema contract

    schema = json.loads(Path(ledger.__file__).with_name("sources.schema.json").read_text())
    validator = Draft202012Validator(schema)
    s = source("https://example.org/lookup", **{encoding: {"year": "{year}"}}, values={"year": 2024})
    if encoding == "form":
        del s["request"]["json"]
    assert validator.is_valid({"source": [s]})
    s["request"]["retrieval_only"] = False
    assert not validator.is_valid({"source": [s]})
    s["request"]["retrieval_only"] = True
    s["request"]["form" if encoding == "json" else "json"] = {}
    assert not validator.is_valid({"source": [s]})


def test_post_byte_cap_and_truncation_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = [{**source("https://example.org/lookup"), "max_bytes": 2}]
    monkeypatch.setattr(ledger, "_open", lambda _request, **_kwargs: response())
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    raw, log = tmp_path / "raw", tmp_path / "log"
    with pytest.raises(FetchError, match="exceeds"):
        fetch(sources, raw=raw, log=log)
    assert not list(raw.glob("*.part"))

    def truncated(_request: urllib.request.Request, timeout: float) -> urllib.response.addinfourl:
        assert timeout > 0
        short = response()
        short.headers.replace_header("Content-Length", "100")
        return short

    sources[0]["max_bytes"] = 1000
    monkeypatch.setattr(ledger, "_open", truncated)
    with pytest.raises(FetchError, match="body ended"):
        fetch(sources, raw=raw, log=log)
    assert not list(raw.glob("*.part"))


def test_post_deadline_uses_the_shared_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = [{**source("https://example.org/lookup"), "timeout_s": 1}]
    moments = iter([0, 0, 2])
    monkeypatch.setattr(ledger.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(ledger, "_open", lambda _request, **_kwargs: response())
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    raw = tmp_path / "raw"
    with pytest.raises(FetchError, match="unfinished after"):
        fetch(sources, raw=raw, log=tmp_path / "log")
    assert not list(raw.glob("*.part"))


def test_source_metadata_cannot_override_declared_get_method(publisher: tuple[str, list], tmp_path: Path):
    base, calls = publisher
    s = source(base + "/data", method="GET")
    del s["request"]["json"]
    del s["request"]["retrieval_only"]
    s["method"] = "DELETE"
    with pytest.raises(FetchError, match="405"):
        fetch([s], raw=tmp_path / "raw", log=tmp_path / "log")
    assert [c[1] for c in calls] == ["GET"]


@pytest.mark.parametrize("change", ["method", "retrieval_only", "declared_method", "archive", "content_header"])
def test_resolved_requests_cannot_bypass_post_declaration(change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolved = expand_requests([source("https://example.org/lookup")])[0]
    if change == "method":
        resolved["method"] = "DELETE"
    elif change == "retrieval_only":
        resolved["request"]["retrieval_only"] = False
    elif change == "declared_method":
        resolved["request"]["method"] = "GET"
    elif change == "archive":
        resolved["archive"] = True
    else:
        resolved["headers"] = {"Content-Type": "env:POST_TYPE"}

    def unopened(_request: urllib.request.Request, timeout: float) -> Never:
        assert timeout > 0
        msg = "unsafe request reached the transport"
        raise AssertionError(msg)

    monkeypatch.setattr(ledger, "_open", unopened)
    with pytest.raises((ValueError, FetchError), match=r"request|sourceledger"):
        fetch([resolved], raw=tmp_path / "raw", log=tmp_path / "log")
