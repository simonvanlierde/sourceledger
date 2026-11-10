"""Fetching over HTTP(S): conditional requests, retries, redirects, size and time limits."""

import contextlib
import hashlib
import http.client
import http.server
import io
import itertools
import json
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from pathlib import Path
from typing import ClassVar

import pytest
from support import (
    _DATA,
    _SNAPSHOT,
    FIXTURE,
    LOG,
    RAW,
    TERMS,
    WHEN,
    _headers,
    _http_error,
    _manifest,
    _peak,
    _Response,
    _run,
    _serve,
    _source,
    _symlink,
    _two_sources,
    builder_mod,
    fetcher_mod,
    report_mod,
)

from sourceledger import ledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main


@pytest.mark.usefixtures("upstream")
def test_fetch_retries_a_server_error_with_backoff(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A brief publisher outage costs a wait, not a failed run."""
    calls = _serve(monkeypatch, _http_error(503), _http_error(502), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert len(calls) == 3
    assert sleeps == [5, 10]
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.usefixtures("upstream")
def test_fetch_gives_up_after_four_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A publisher that stays down fails the run after four tries, not a fifth."""
    calls = _serve(monkeypatch, *(_http_error(503) for _ in range(5)))
    assert _run("fetch") == [3]
    assert len(calls) == 4


@pytest.mark.usefixtures("upstream")
def test_fetch_waits_for_retry_after(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A 429 waits as long as the server asks, when that is longer than the backoff."""
    _serve(monkeypatch, _http_error(429, Retry_After="30"), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert sleeps == [30]


@pytest.mark.usefixtures("upstream")
def test_fetch_caps_the_retry_after_wait(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A server asking for a day's wait cannot stall the run for a day."""
    _serve(monkeypatch, _http_error(429, Retry_After="86400"), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert sleeps == [120]


@pytest.mark.usefixtures("upstream")
def test_a_refetch_is_conditional_and_a_304_keeps_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged source is not downloaded again, and its fetch record stays as it was."""
    modified = "Wed, 13 Mar 2024 15:21:06 GMT"
    calls = _serve(monkeypatch, _Response(FIXTURE.encode(), Last_Modified=modified, ETag='"abc123"'), _http_error(304))
    assert _run("fetch") == [0]
    record = Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8")
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert calls[0].get_header("If-none-match") is None
    assert (calls[1].get_header("If-none-match"), calls[1].get_header("If-modified-since")) == ('"abc123"', modified)
    assert Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8") == record
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 1
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.usefixtures("upstream")
def test_a_raw_file_changed_since_its_fetch_is_fetched_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edited bytes are not kept on a 304: the request is unconditional."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc123"'), _Response(FIXTURE.encode()))
    _run("fetch")
    RAW.write_text("edited\n", encoding="utf-8", newline="")
    assert _run("fetch") == [0]
    assert calls[1].get_header("If-none-match") is None
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.usefixtures("upstream")
def test_fetch_does_not_retry_a_client_error(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """A 404 will not fix itself, so it fails at once."""
    calls = _serve(monkeypatch, _http_error(404))
    assert _run("fetch") == [3]
    assert (len(calls), sleeps) == (1, [])


@pytest.mark.usefixtures("upstream")
def test_fetch_does_not_retry_a_certificate_it_cannot_verify(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    """A certificate that fails to verify fails the same way in 35 s, so it fails at once."""
    refused = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
    calls = _serve(monkeypatch, urllib.error.URLError(refused), _Response(FIXTURE.encode()))
    assert _run("fetch") == [3]
    assert (len(calls), sleeps) == (1, [])
    assert "certificate verify failed" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_fetch_rejects_a_body_shorter_than_its_content_length(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated body is retried, and never saved as if it were the whole file."""
    calls = _serve(monkeypatch, *[_Response(FIXTURE.encode()[:5], length=len(FIXTURE)) for _ in range(4)])
    assert _run("fetch") == [3]
    assert len(calls) == 4
    assert "body ended after 5 of the 29 bytes" in capsys.readouterr().err
    assert not RAW.exists()


@pytest.mark.usefixtures("upstream")
def test_fetch_records_last_modified_and_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validators a later run needs for a conditional request are kept in the fetch record."""
    modified = "Wed, 13 Mar 2024 15:21:06 GMT"
    _serve(monkeypatch, _Response(FIXTURE.encode(), Last_Modified=modified, ETag='"abc123"'))
    assert _run("fetch") == [0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert (record["last_modified"], record["etag"]) == (modified, '"abc123"')
    assert _run("build") == [0]


@pytest.mark.usefixtures("upstream")
def test_fetch_refuses_a_download_over_the_size_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A URL that serves far more than a small cut fails before anything is written."""
    monkeypatch.setattr(ledger, "_MAX_BYTES", len(FIXTURE) - 1)
    assert _run("fetch") == [3]
    assert "exceeds" in capsys.readouterr().err
    assert not RAW.exists()


def test_a_source_over_the_default_cap_fetches_with_its_own_max_bytes(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source bigger than the default on purpose raises its own cap in sources.toml."""
    monkeypatch.setattr(ledger, "_MAX_BYTES", len(FIXTURE) - 1)
    _manifest(tmp_path, upstream.as_uri(), terms=f"max_bytes = {len(FIXTURE)}")
    assert _run("fetch") == [0]
    assert RAW.read_text(encoding="utf-8") == FIXTURE


def test_the_fetch_log_keeps_each_new_version_once(upstream: Path) -> None:
    """A re-download of the same bytes adds nothing; a publisher revision adds one line."""
    _run("fetch", "fetch")
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    _run("fetch")
    lines = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
    assert [r["sha256"] for r in lines] == [
        hashlib.sha256(FIXTURE.encode()).hexdigest(),
        hashlib.sha256((FIXTURE + "2022,3.5\n").encode()).hexdigest(),
    ]
    assert lines[-1] == json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))


def test_the_default_user_agent_names_the_library() -> None:
    """A fetch without a project name still says who is asking."""
    assert fetcher_mod.USER_AGENT.startswith("sourceledger/")


def test_one_failing_source_does_not_stop_the_others(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every source is tried; the run fails, names the failed one, and keeps what the others fetched."""
    _two_sources(tmp_path, upstream.with_name("gone.csv").as_uri(), upstream.as_uri())
    assert _run("fetch") == [3]
    out, err = capsys.readouterr()
    assert err.startswith("error: a: fetching ")
    assert out.startswith("b ")
    assert Path("data/raw/b.csv").read_text(encoding="utf-8") == FIXTURE
    assert not Path("data/raw/a.csv").exists()


@pytest.mark.usefixtures("upstream")
def test_a_version_the_log_missed_is_logged_on_the_next_304(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fetch that died after writing the record but before logging it loses no history."""
    _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc"'), _http_error(304))
    _run("fetch")
    LOG.unlink()
    assert _run("fetch") == [0]
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(("key", "value"), [("sha256", 123), ("bytes", "29"), ("url", None), ("surplus", 1)])
def test_a_fetch_record_of_the_wrong_type_is_fetched_again(
    upstream: Path, key: str, value: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validate reports it as a problem and fetch replaces it, rather than either stopping on a traceback."""
    _run("fetch", "build")
    record_path = Path("data/raw/example.csv.fetch.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record_path.write_text(json.dumps({**record, key: value}), encoding="utf-8", newline="")
    capsys.readouterr()
    assert _run("validate") == [1]
    assert "example.csv.fetch.json is not a fetch record; re-run fetch" in capsys.readouterr().out
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert json.loads(record_path.read_text(encoding="utf-8"))["url"] == upstream.as_uri()


def test_a_pin_is_compared_as_given_by_a_library_caller(upstream: Path, tmp_path: Path) -> None:
    """A pin that skipped load_manifest is compared as written, by fetch and verified alike."""
    digest = hashlib.sha256(FIXTURE.encode()).hexdigest()
    source = {"id": "example", "status": "implemented", "url": upstream.as_uri(), "sha256": digest.upper()}
    raw = tmp_path / "raw"
    with pytest.raises(ledger.FetchError, match=r"not the sha256 in sources\.toml$"):
        fetcher_mod.fetch([source], raw=raw, log=tmp_path / "log.jsonl")
    fetcher_mod.fetch([{**source, "sha256": digest}], raw=raw, log=tmp_path / "log.jsonl")
    with pytest.raises(ValueError, match=r"not the sha256 sources\.toml now pins; re-run fetch$"):
        ledger.verified(source, raw=raw)


@pytest.mark.parametrize("line", ["[1]", "{}"])
@pytest.mark.usefixtures("upstream")
def test_a_log_line_that_is_not_a_record_breaks_only_fetch(line: str, capsys: pytest.CaptureFixture[str]) -> None:
    """JSON that is not a record is reported as one; build, validate and verify never read the log."""
    _run("fetch", "build")
    LOG.write_text(line + "\n", encoding="utf-8", newline="")
    capsys.readouterr()
    assert _run("fetch") == [3]
    assert capsys.readouterr().err == "error: data/fetches.jsonl:1 is not a fetch record\n"
    assert _run("build", "validate") == [0, 0]
    assert main(["verify", "data/processed"]) == 0


@pytest.mark.usefixtures("upstream")
def test_a_content_length_that_is_not_a_number_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed header can't verify the length, so it doesn't fail the fetch either."""
    response = _Response(FIXTURE.encode())
    response.headers.replace_header("Content-Length", "29, 29")
    _serve(monkeypatch, response)
    assert _run("fetch") == [0]


@pytest.mark.usefixtures("upstream")
def test_a_compressed_body_is_refused(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Hashing gzip bytes would record something that is not the file the publisher serves."""
    _serve(monkeypatch, _Response(FIXTURE.encode(), Content_Encoding="gzip"))
    assert _run("fetch") == [3]
    assert "Content-Encoding gzip" in capsys.readouterr().err
    assert not RAW.exists()


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("1", [5]), ("²", [5]), ("soon", [5])],
)
@pytest.mark.usefixtures("upstream")
def test_a_short_or_unreadable_retry_after_waits_the_backoff(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], retry_after: str, expected: list[float]
) -> None:
    """Retry-After raises the wait above the backoff, never below it, and junk is ignored."""
    _serve(monkeypatch, _http_error(429, Retry_After=retry_after), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert sleeps == expected


@pytest.mark.usefixtures("upstream")
def test_retry_after_as_an_http_date_is_honoured(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """RFC 9110 lets Retry-After be a date; a minute from now waits about a minute."""
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    _serve(monkeypatch, _http_error(503, Retry_After=when), _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert 55 <= sleeps[0] <= 60


@pytest.mark.usefixtures("upstream")
def test_each_request_has_a_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that never answers can't hold the run forever."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    _run("fetch")
    assert calls[0].timeout == fetcher_mod._SOCKET_TIMEOUT_S  # noqa: SLF001


@pytest.mark.usefixtures("upstream")
def test_a_304_to_an_unconditional_request_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without validators there is no earlier copy for a 304 to point at."""
    _serve(monkeypatch, _http_error(304))
    assert _run("fetch") == [3]


@pytest.mark.usefixtures("upstream")
def test_a_download_that_outlasts_its_budget_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server trickling bytes inside every socket timeout is still stopped."""
    # 150 s pass at each look at the clock: the start, the attempt, then the first read is past 300 s.
    clock = itertools.count(0, 150)
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch") == [3]
    assert "unfinished after" in capsys.readouterr().err


def test_a_server_trickling_bytes_is_stopped_near_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A byte every 0.2 s never trips the socket timeout, yet the fetch stops at the budget, not the body's end."""
    monkeypatch.setattr(fetcher_mod, "_BUDGET_S", 0.5)
    monkeypatch.setattr(ledger, "_BUDGET_S", 0.5)
    monkeypatch.setattr(fetcher_mod, "_SOCKET_TIMEOUT_S", 1)
    listener = socket.create_server(("127.0.0.1", 0))

    def trickle() -> None:
        connection, _ = listener.accept()
        with connection, contextlib.suppress(OSError):  # the client hangs up once it gives up
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n")
            pause = threading.Event()  # time.sleep is stubbed out for the fetch's own waits
            for _ in range(30):
                pause.wait(0.2)
                connection.sendall(b"x")

    threading.Thread(target=trickle, daemon=True).start()
    monkeypatch.chdir(tmp_path)
    _manifest(tmp_path, f"http://127.0.0.1:{listener.getsockname()[1]}/data.csv")
    try:
        start = time.monotonic()
        assert _run("fetch") == [3]
        # Within the budget plus one socket timeout; the whole body takes 6 s.
        assert time.monotonic() - start < 1.5 + 0.5
        assert "unfinished after 0.5 s" in capsys.readouterr().err
    finally:
        listener.close()


@pytest.mark.parametrize("timeout", [1, 0.5, 600])
@pytest.mark.usefixtures("upstream")
def test_source_timeout_bounds_the_socket(monkeypatch: pytest.MonkeyPatch, timeout: float) -> None:
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    sources = manifest_mod.load_manifest()
    sources[0]["timeout_s"] = timeout
    fetcher_mod.fetch(sources)
    assert calls[0].timeout is not None
    assert 0 < calls[0].timeout <= min(timeout, 30)


@pytest.mark.parametrize("timeout", [1, 5])
@pytest.mark.usefixtures("upstream")
def test_source_timeout_includes_retry_waits(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], timeout: int
) -> None:
    calls = _serve(monkeypatch, _http_error(503, Retry_After="2"), _Response(FIXTURE.encode()))
    sources = manifest_mod.load_manifest()
    sources[0]["timeout_s"] = timeout
    monkeypatch.setattr(time, "monotonic", lambda: 0)
    with pytest.raises(ledger.FetchError, match="503"):
        fetcher_mod.fetch(sources)
    assert len(calls) == 1
    assert sleeps == []


def test_a_redirect_from_a_public_host_is_marked_public_only() -> None:
    """The mark carries on to every later hop; a redirect within an internal host gets none."""
    handler = fetcher_mod._NoRedirectInward()  # noqa: SLF001
    public = urllib.request.Request("http://93.184.215.14/data.csv")
    new = handler.redirect_request(public, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://10.0.0.1/a")
    assert new is not None
    assert new.public_only  # ty: ignore[unresolved-attribute]
    again = handler.redirect_request(new, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://10.0.0.2/b")
    assert again is not None
    assert again.public_only  # ty: ignore[unresolved-attribute]
    internal = urllib.request.Request("http://127.0.0.1/data.csv")
    moved = handler.redirect_request(
        internal, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://127.0.0.1/moved"
    )
    assert moved is not None
    assert not hasattr(moved, "public_only")


def test_a_redirect_from_a_public_host_must_stay_http() -> None:
    """The connected-peer check sees only HTTP, so a public host can't send a fetch to internal ftp."""
    handler = fetcher_mod._NoRedirectInward()  # noqa: SLF001
    public = urllib.request.Request("http://93.184.215.14/data.csv")
    with pytest.raises(fetcher_mod._InwardRedirectError, match="not HTTP or HTTPS"):  # noqa: SLF001
        handler.redirect_request(public, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "ftp://10.0.0.1/a")
    # A source that already lives on an internal host keeps urllib's own redirect rules.
    internal = urllib.request.Request("http://127.0.0.1/data.csv")
    assert handler.redirect_request(internal, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "ftp://10.0.0.1/a")


class _Rebinder(http.server.BaseHTTPRequestHandler):
    """/start.csv redirects to rebind.test on the same port; anything else is the internal secret."""

    def do_GET(self) -> None:
        if self.path == "/start.csv":
            self.send_response(302)
            self.send_header("Location", f"http://rebind.test:{self.connection.getsockname()[1]}/secret")
            self.end_headers()
            return
        body = b"secret\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # the base class names it
        """Keep test output quiet."""


def test_a_redirect_to_a_name_that_rebinds_inward_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A .test name answers public to the redirect check's lookup and loopback to the connect's one."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rebinder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = socket.getaddrinfo

    def rebinding(host: str, port: object, *args: object, **kwargs: object) -> object:
        if host.endswith(".test"):
            host = "93.184.215.14" if port is None else "127.0.0.1"
        return real(host, port, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    for proxy in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(proxy, raising=False)
    monkeypatch.chdir(tmp_path)
    _manifest(tmp_path, f"http://origin.test:{server.server_address[1]}/start.csv")
    try:
        assert _run("fetch") == [3]
    finally:
        server.shutdown()
    assert "example: refused a redirect from a public host to http://rebind.test" in capsys.readouterr().err
    assert not RAW.exists()


def test_a_refused_redirect_names_no_query_or_fragment() -> None:
    """A presigned URL's signature must not reach a log or the CLI's output."""
    handler = fetcher_mod._NoRedirectInward()  # noqa: SLF001
    public = urllib.request.Request("http://93.184.215.14/data.csv")
    with pytest.raises(fetcher_mod._InwardRedirectError) as refused:  # noqa: SLF001
        handler.redirect_request(
            public, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "ftp://10.0.0.1/a?sig=s3cr3t#frag"
        )
    assert "ftp://10.0.0.1/a," in str(refused.value)
    assert "s3cr3t" not in str(refused.value)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rebinder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/a.csv?sig=s3cr3t")
        request.public_only = True  # ty: ignore[unresolved-attribute]
        connection = fetcher_mod._PeerCheck("127.0.0.1", port=server.server_address[1], req=request)  # noqa: SLF001
        with pytest.raises(ValueError, match=r"/a\.csv, which is at") as inward:
            connection.connect()
        assert "s3cr3t" not in str(inward.value)
    finally:
        server.shutdown()
        server.server_close()


def test_the_peer_check_is_skipped_through_a_proxy() -> None:
    """Through a proxy the socket reaches the proxy, not the host the URL names."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rebinder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        for url, refused in ((f"http://127.0.0.1:{port}/a.csv", True), ("http://example.org/a.csv", False)):
            request = urllib.request.Request(url)  # noqa: S310  # http only
            request.public_only = True  # ty: ignore[unresolved-attribute]
            connection = fetcher_mod._PeerCheck("127.0.0.1", port=port, req=request)  # noqa: SLF001
            if refused:
                with pytest.raises(
                    ValueError, match=rf"^refused a redirect from a public host to {url}, which is at 127\.0\.0\.1$"
                ):
                    connection.connect()
            else:
                connection.connect()
            connection.close()
    finally:
        server.shutdown()


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        """The refused client hangs up mid-request; that is the point, so print nothing."""


class _TLSEcho(http.server.BaseHTTPRequestHandler):
    """Answers every GET with a body and records the request line."""

    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.seen.append(self.path)
        body = b"tls ok\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # the base class names it
        """Keep test output quiet."""


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI to make a throwaway test cert")
def test_the_peer_check_refuses_a_loopback_peer_over_tls(tmp_path: Path) -> None:
    """A public-only https request is refused after the handshake and before any request bytes."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(  # noqa: S603  # fixed arguments
        [  # noqa: S607  # openssl from PATH
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(cert, key)
    _TLSEcho.seen.clear()
    server = _QuietServer(("127.0.0.1", 0), _TLSEcho)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{server.server_address[1]}/a.csv"
    opener = urllib.request.build_opener(
        fetcher_mod._HTTPSHandler(context=ssl.create_default_context(cafile=str(cert)))  # noqa: SLF001
    )
    try:
        plain = urllib.request.Request(url)  # https only
        with opener.open(plain, timeout=5) as response:
            assert response.read() == b"tls ok\n"
        assert _TLSEcho.seen == ["/a.csv"]
        _TLSEcho.seen.clear()
        marked = urllib.request.Request(url)  # https only
        marked.public_only = True  # ty: ignore[unresolved-attribute]
        with pytest.raises(
            ValueError, match=rf"^refused a redirect from a public host to {url}, which is at 127\.0\.0\.1$"
        ):
            opener.open(marked, timeout=5)
        assert _TLSEcho.seen == []
    finally:
        server.shutdown()


def test_real_http_fetches_redirects_and_304s(publisher: str, tmp_path: Path) -> None:
    """Against a real server: the final URL is recorded, and the second fetch is a 304 that keeps the record."""
    _manifest(tmp_path, f"{publisher}/old.csv")
    assert _run("fetch") == [0]
    record_path = Path("data/raw/example.csv.fetch.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert (record["etag"], record["final_url"]) == ('"v1"', f"{publisher}/data.csv")
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert json.loads(record_path.read_text(encoding="utf-8")) == record


def test_a_redirect_to_a_signed_url_keeps_no_signature(publisher: str, tmp_path: Path) -> None:
    """A redirect's query can be a signature; the published records keep where, not the key."""
    _manifest(tmp_path, f"{publisher}/signed.csv")
    assert _run("fetch") == [0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["final_url"] == f"{publisher}/data.csv"
    assert "s3cr3t" not in Path("data/fetches.jsonl").read_text(encoding="utf-8")


@pytest.mark.usefixtures("upstream")
def test_a_symlinked_fetch_log_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Appending through a planted symlink would write into another file."""
    victim = tmp_path / "victim.txt"
    victim.write_text("", encoding="utf-8", newline="")
    LOG.parent.mkdir(parents=True)
    _symlink(LOG, victim)
    assert _run("fetch") == [3]
    assert (
        capsys.readouterr().err == "error: data/fetches.jsonl is a symlink; the fetch log must be a file of its own\n"
    )
    assert victim.read_text(encoding="utf-8") == ""


@pytest.mark.usefixtures("upstream")
def test_an_unremovable_temp_file_does_not_stop_a_fetch() -> None:
    """A virus scanner holding a temp file, or a directory named like one, must not abort the run."""
    RAW.parent.mkdir(parents=True)
    (RAW.parent / ".x.part").mkdir()
    assert _run("fetch") == [0]


@pytest.mark.usefixtures("upstream")
def test_a_download_streams_to_disk_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than one chunk arrives whole, and a refused one leaves no temp file behind."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", 4)
    assert _run("fetch") == [0]
    assert RAW.read_text(encoding="utf-8") == FIXTURE
    monkeypatch.setattr(ledger, "_MAX_BYTES", 10)
    RAW.write_text("edited\n", encoding="utf-8", newline="")  # forces a full fetch
    assert _run("fetch") == [3]
    assert not list(RAW.parent.glob("*.part"))


def test_each_fetch_line_says_new_or_unchanged(upstream: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The first fetch and a publisher revision are new; the same bytes again are unchanged."""
    _run("fetch")
    assert capsys.readouterr().out.split()[1] == "new"
    _run("fetch")
    assert capsys.readouterr().out.split()[1] == "unchanged"
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    _run("fetch")
    assert capsys.readouterr().out.split()[1] == "new"


@pytest.mark.usefixtures("upstream")
def test_a_header_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The token reaches the request, its variable's name reaches the record, its value doesn't."""
    _manifest(
        tmp_path, "https://example.invalid/data.csv", terms=f'{TERMS}\nheaders = {{ Authorization = "env:SL_TOKEN" }}'
    )
    monkeypatch.setenv("SL_TOKEN", "Bearer s3cret")
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert calls[0].get_header("Authorization") == "Bearer s3cret"
    record = Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8")
    assert json.loads(record)["env_headers"] == ["Authorization"]
    assert "s3cret" not in record
    assert "s3cret" not in LOG.read_text(encoding="utf-8")


@pytest.mark.usefixtures("upstream")
def test_a_missing_variable_fails_that_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unset token is named, and nothing is fetched without it."""
    _manifest(
        tmp_path, "https://example.invalid/data.csv", terms=f'{TERMS}\nheaders = {{ Authorization = "env:SL_UNSET" }}'
    )
    assert _run("fetch") == [3]
    assert "environment variable SL_UNSET for the Authorization header is not set" in capsys.readouterr().err


def test_a_redirect_to_another_host_drops_the_credentials() -> None:
    """Urllib copies every header to a redirect; a token for one host must not reach another."""
    handler = fetcher_mod._NoRedirectInward()  # noqa: SLF001
    req = urllib.request.Request("https://93.184.215.14/data.csv", headers={"Authorization": "x", "User-Agent": "ua"})
    other = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "https://8.8.8.8/data.csv"
    )
    same = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "https://93.184.215.14/v2.csv"
    )
    assert other is not None
    assert same is not None
    assert (other.get_header("Authorization"), other.get_header("User-agent")) == (None, "ua")
    assert same.get_header("Authorization") == "x"


def test_a_redirect_to_plain_http_on_the_same_host_drops_the_credentials() -> None:
    """A token sent over https must not go out again in clear text after a downgrade."""
    handler = fetcher_mod._NoRedirectInward()  # noqa: SLF001
    req = urllib.request.Request("https://93.184.215.14/data.csv", headers={"Authorization": "x", "User-Agent": "ua"})
    new = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://93.184.215.14/data.csv"
    )
    assert new is not None
    assert (new.get_header("Authorization"), new.get_header("User-agent")) == (None, "ua")


@pytest.mark.usefixtures("upstream")
def test_the_stages_take_their_folders_as_arguments(tmp_path: Path) -> None:
    """A library caller can keep raw files elsewhere; build and validate then read them there."""
    sources = manifest_mod.load_manifest()
    raw, processed, log = tmp_path / "elsewhere", tmp_path / "out", tmp_path / "logs" / "deep" / "log.jsonl"
    fetcher_mod.fetch(sources, raw=raw, log=log)
    fetcher_mod.fetch(sources, raw=raw, log=log)  # the second asks conditionally, from the record in `raw`
    builder_mod.build(sources, raw=raw, processed=processed)
    assert report_mod.validate(sources, raw=raw, processed=processed) == []
    assert not RAW.exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 0.0),
        (_headers(), 0.0),
        (_headers(ETag="x"), 0.0),
        (_headers(Retry_After="junk"), 0.0),
        (_headers(Retry_After="Wed, 21 Oct 2015 07:28:00 GMT"), 0.0),
        (_headers(Retry_After="7"), 7.0),
    ],
)
def test_retry_after_reads_seconds_dates_and_nothing(headers: Message | None, expected: float) -> None:
    """No header, junk and a date in the past all mean no wait of the server's own."""
    assert fetcher_mod._retry_after(headers) == expected  # noqa: SLF001


def test_content_length_is_none_when_absent() -> None:
    assert fetcher_mod._content_length(_headers(ETag="x")) is None  # noqa: SLF001
    assert fetcher_mod._content_length(_headers(Content_Length=" 12 ")) == 12  # noqa: SLF001


@pytest.mark.usefixtures("upstream")
def test_a_failed_conditional_request_is_not_a_304(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a 304 keeps the earlier record; a 404 to a conditional request fails the fetch."""
    _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc"'), _http_error(404))
    assert _run("fetch", "fetch") == [0, 3]


def test_the_opener_gets_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr(fetcher_mod._OPENER, "open", lambda _request, timeout: seen.append(timeout))  # noqa: SLF001
    fetcher_mod._open(urllib.request.Request("https://example.invalid/"), timeout=7)  # noqa: SLF001
    assert seen == [7]


def test_an_unresolvable_host_counts_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redirect check leaves it to the request, which fails on its own."""

    def fail(*_args: object) -> None:
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert fetcher_mod._is_public("no-such-host.invalid")  # noqa: SLF001


class _Spy(_Response):
    """A response that records how much each read asks for."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.asks: list[int | None] = []

    def read1(self, size: int | None = -1, /) -> bytes:
        self.asks.append(size)
        return super().read1(size)


@pytest.mark.usefixtures("upstream")
def test_a_download_reads_one_chunk_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory stays at one chunk: no read asks for the whole body."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", 4)
    response = _Spy(FIXTURE.encode())
    _serve(monkeypatch, response)
    assert _run("fetch") == [0]
    assert set(response.asks) == {4}


@pytest.mark.usefixtures("upstream")
def test_an_empty_content_encoding_is_no_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _Response(FIXTURE.encode(), Content_Encoding=""))
    assert _run("fetch") == [0]


def test_archive_asks_for_http_urls_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://example.org/data.csv"
    _manifest(tmp_path, url, terms=f"{TERMS}\narchive = true")
    monkeypatch.chdir(tmp_path)
    snapshot = f"https://web.archive.org/web/20261018033833/{url}"
    _serve(monkeypatch, _Response(FIXTURE.encode()), _Response(b"", url=snapshot))
    assert _run("fetch") == [0]
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["archive_url"] == snapshot


def test_a_log_line_from_before_archive_url_reads_as_no_snapshot(upstream: Path, tmp_path: Path) -> None:
    """The same bytes again take the snapshot from their log line, and an old line without one gives none."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\narchive = true")
    assert _run("fetch") == [0]
    line = json.loads(LOG.read_text(encoding="utf-8"))
    del line["archive_url"]
    LOG.write_text(json.dumps(line) + "\n", encoding="utf-8", newline="")
    RAW.write_text("edited\n", encoding="utf-8", newline="")  # forces a full fetch of the same bytes
    assert _run("fetch") == [0]
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["archive_url"] == ""


@pytest.mark.usefixtures("upstream")
def test_archive_asks_for_a_snapshot_of_new_bytes_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot's URL lands in the record and the log; the same bytes keep it, and new bytes get their own."""
    _manifest(tmp_path, _DATA, terms=f"{TERMS}\narchive = true")
    newer = f"https://web.archive.org/web/20261019033833/{_DATA}"
    calls = _serve(
        monkeypatch,
        _Response(FIXTURE.encode(), ETag='"v1"'),
        _Response(b"", url=_SNAPSHOT),
        _http_error(304),
        _Response(FIXTURE.encode()),
        _Response((FIXTURE + "2022,3.5\n").encode()),
        _Response(b"", url=newer),
    )
    assert _run("fetch") == [0]
    assert (calls[1].full_url, calls[1].timeout) == (
        f"https://web.archive.org/save/{_DATA}",
        fetcher_mod._ARCHIVE_TIMEOUT_S,  # noqa: SLF001
    )
    assert calls[1].get_header("User-agent") == fetcher_mod.USER_AGENT
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["archive_url"] == _SNAPSHOT
    assert json.loads(LOG.read_text(encoding="utf-8"))["archive_url"] == _SNAPSHOT
    assert _run("fetch") == [0]  # a 304
    RAW.write_text("edited\n", encoding="utf-8", newline="")
    assert _run("fetch") == [0]  # the same bytes again, downloaded in full
    assert len(calls) == 4
    assert json.loads(LOG.read_text(encoding="utf-8"))["archive_url"] == _SNAPSHOT
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["archive_url"] == _SNAPSHOT
    assert _run("fetch") == [0]  # new bytes
    assert len(calls) == 6
    assert [json.loads(line)["archive_url"] for line in LOG.read_text(encoding="utf-8").splitlines()] == [
        _SNAPSHOT,
        newer,
    ]


@pytest.mark.parametrize(
    ("answer", "snapshot"),
    [
        # Factories, not responses: a response is closed after one run, and pytest may run a test twice.
        (
            lambda: _Response(
                b"", url=f"https://web.archive.org/save/{_DATA}", Content_Location=f"/web/20261018033833/{_DATA}"
            ),
            _SNAPSHOT,
        ),
        (lambda: _Response(b"", url=f"https://web.archive.org/save/{_DATA}"), ""),
        (lambda: _http_error(503), ""),
        (lambda: TimeoutError("timed out"), ""),
        (lambda: urllib.error.URLError("x"), ""),
        (lambda: ValueError("bad"), ""),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_archive_never_fails_the_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[], _Response | Exception],
    snapshot: str,
) -> None:
    """A snapshot named in Content-Location is kept; no snapshot, an error or a timeout is a warning."""
    _manifest(tmp_path, _DATA, terms=f"{TERMS}\narchive = true")
    _serve(monkeypatch, _Response(FIXTURE.encode()), answer())
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["archive_url"] == snapshot
    assert ("Internet Archive" in caplog.text) == (not snapshot)


def test_archive_skips_a_url_that_is_not_http(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Save Page Now can only reach the web, so a file:// source is fetched and not archived."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\narchive = true")
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    assert _run("fetch") == [0]
    assert len(calls) == 1
    assert "archive needs an http or https URL" in caplog.text
    assert json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))["archive_url"] == ""


def test_archive_must_be_true_or_false(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A string such as "yes" is a manifest error, not a silent yes."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\narchive = "yes"')
    assert _run("fetch") == [3]
    assert "need archive to be true or false" in capsys.readouterr().err


_SNAPSHOT_FORM = r"https://web\.archive\.org/web/\d{14}/(?P<url>.+)"


class _SavePageNow:
    """A fake of Save Page Now, standing in for `fetcher_mod._open`, as far as `_archive` relies on it.

    It redirects to a snapshot, or answers 429 with no Retry-After, which the live archive sends
    once a URL has had its 5 captures of the day. `test_save_page_now_contract` checks it against
    the live service.
    """

    def __init__(self, *, rate_limited: bool = False) -> None:
        self.rate_limited = rate_limited

    def __call__(self, request: urllib.request.Request, timeout: float) -> _Response:
        assert request.full_url.startswith(fetcher_mod._ARCHIVE)  # noqa: SLF001
        assert timeout == fetcher_mod._ARCHIVE_TIMEOUT_S  # noqa: SLF001
        if self.rate_limited:
            raise urllib.error.HTTPError(request.full_url, 429, "TOO MANY REQUESTS", _headers(), None)
        target = request.full_url.removeprefix(fetcher_mod._ARCHIVE)  # noqa: SLF001
        return _Response(b"", url=f"{fetcher_mod._SNAPSHOT}20261019161808/{target}")  # noqa: SLF001


@pytest.fixture(params=["fake", "fake-rate-limited", pytest.param("live", marks=pytest.mark.live)])
def save_page_now(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Save Page Now: the fake on every run, the live service under `pytest -m live`."""
    if request.param != "live":
        monkeypatch.setattr(fetcher_mod, "_open", _SavePageNow(rate_limited=request.param == "fake-rate-limited"))
    return request.param


def test_save_page_now_contract(save_page_now: str, caplog: pytest.LogCaptureFixture) -> None:
    """The archive either redirects to a snapshot of the URL that opens, or rate-limits with a 429."""
    # A page few others save: example.com reaches the archive's 5 captures a day by morning.
    target = "https://github.com/simonvanlierde/sourceledger/blob/main/README.md"
    snapshot = fetcher_mod._archive({"id": "contract", "url": target}, fetcher_mod.USER_AGENT)  # noqa: SLF001
    if not snapshot:
        assert "HTTP Error 429" in caplog.text
        if save_page_now == "live":
            pytest.skip("the live archive had captured this URL 5 times today, so only the 429 was checked")
        return
    match = re.fullmatch(_SNAPSHOT_FORM, snapshot)
    assert match
    assert match["url"].rstrip("/") == target.rstrip("/")
    if save_page_now == "live":
        request = urllib.request.Request(snapshot, headers={"User-Agent": fetcher_mod.USER_AGENT})  # noqa: S310
        with fetcher_mod._open(request, timeout=60) as response:  # noqa: SLF001
            assert response.status == 200


def test_a_conditional_fetch_hashes_the_raw_file_without_holding_it(big: int, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = manifest_mod.load_manifest()
    _serve(monkeypatch, _http_error(304))
    assert _peak(lambda: fetcher_mod.fetch(sources)) < big // 4


@pytest.mark.usefixtures("upstream")
def test_a_fetch_reads_the_log_once_however_many_sources_keep_their_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(tmp_path, _DATA, terms=f"{TERMS}\narchive = true")
    _serve(monkeypatch, _Response(FIXTURE.encode()), _Response(b"", url=_SNAPSHOT), _Response(FIXTURE.encode()))
    sources = manifest_mod.load_manifest()
    fetcher_mod.fetch(sources)
    reads: list[Path] = []
    last_records = ledger._last_records  # noqa: SLF001
    monkeypatch.setattr(fetcher_mod, "_last_records", lambda log: reads.append(log) or last_records(log))
    assert [r.archive_url for r in fetcher_mod.fetch(sources)] == [_SNAPSHOT]
    assert reads == [LOG]


@pytest.mark.parametrize(
    ("budget", "elapsed", "success"), [(1, 0.5, True), (1, 1, False), (1, 2, False), (600, 301, True)]
)
@pytest.mark.usefixtures("upstream")
def test_the_download_uses_the_source_budget(
    monkeypatch: pytest.MonkeyPatch, budget: int, elapsed: float, *, success: bool
) -> None:
    _serve(monkeypatch, _Response(FIXTURE.encode()))
    sources = manifest_mod.load_manifest()
    sources[0]["timeout_s"] = budget
    clock = iter([0, 0, elapsed, elapsed])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    if success:
        assert len(fetcher_mod.fetch(sources)) == 1
    else:
        with pytest.raises(ledger.FetchError, match=f"unfinished after {budget} s"):
            fetcher_mod.fetch(sources)


def test_redirects_do_not_need_the_redirect_response_body(publisher: str, tmp_path: Path) -> None:
    _manifest(tmp_path, f"{publisher}/unreadable-redirect.csv", terms=f"{TERMS}\nmax_bytes = {len(FIXTURE)}")
    assert _run("fetch", "build", "validate") == [0, 0, 0]
    assert RAW.read_text(encoding="utf-8") == FIXTURE


def test_default_fetch_stays_on_calling_thread(upstream: Path, monkeypatch: pytest.MonkeyPatch):
    caller = threading.get_ident()
    original_open = fetcher_mod._open  # noqa: SLF001

    def open_(request: urllib.request.Request, timeout: float) -> fetcher_mod._Response:
        assert threading.get_ident() == caller
        return original_open(request, timeout)

    monkeypatch.setattr(fetcher_mod, "_open", open_)
    assert fetcher_mod.fetch(manifest_mod.load_manifest())[0].url == upstream.as_uri()


def test_a_later_fetch_sends_the_validators_record_kept(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 304 for the recorded ETag keeps the recorded file and its record."""
    _manifest(tmp_path, "https://example.org/example.csv")
    recorded = ledger.record(_source(), upstream, retrieved_at=WHEN, etag='"abc"')
    calls = _serve(monkeypatch, _http_error(304))
    assert fetcher_mod.fetch(manifest_mod.load_manifest()) == [recorded]
    assert calls[0].get_header("If-none-match") == '"abc"'


@pytest.mark.usefixtures("upstream")
def test_a_failed_source_leaves_the_others_on_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A library caller still gets the records of the sources that were fetched."""
    first = manifest_mod.load_manifest()[0]
    sources = [{**first, "id": "first"}, {**first, "id": "second"}]
    _serve(monkeypatch, _Response(FIXTURE.encode()), _http_error(404))
    with pytest.raises(ledger.FetchError) as error:
        fetcher_mod.fetch(sources)
    assert [r.source_id for r in error.value.records] == ["first"]
    assert len(error.value.failures) == 1
