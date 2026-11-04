"""Isolated HTTP/1.1 download comparison; run with --help inside a throwaway container."""

# Benchmark adapters use separate optional environments and explicit smoke assertions.
# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D101, D102, D103, PLR2004, S101, T201, INP001, C901, PLR0912, PLR0915, PLC0415, PT017
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import gzip
import hashlib
import http.server
import importlib.metadata
import json
import os
import platform
import random
import resource
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CHUNK = 64 * 1024
SMALL = (b"sourceledger-data\n" * 256)[:4096]
LARGE = bytes(range(256)) * 32768
COMPRESSED = gzip.compress(LARGE, mtime=0)
CLIENTS = ("urllib", "urllib3", "httpx", "niquests", "curl_cffi", "curl_stream")
CASES = (
    "warm_small",
    "delayed_serial",
    "delayed_parallel",
    "large",
    "post",
    "not_modified",
    "redirect",
    "compressed",
    "truncated",
    "size_cap",
)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = SMALL
        status = 200
        if self.path == "/delay":
            time.sleep(0.1)
        elif self.path in ("/large", "/cap", "/truncated"):
            body = LARGE
        elif self.path == "/compressed":
            body = COMPRESSED
        elif self.path == "/post":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        elif self.path == "/redirect":
            status, body = 302, b""
        elif self.path == "/not_modified":
            status, body = 304, b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Connection-Id", str(self.client_address[1]))
        if self.path == "/compressed":
            self.send_header("Content-Encoding", "gzip")
        if self.path == "/redirect":
            self.send_header("Location", "/small")
        self.end_headers()
        try:
            if self.path == "/truncated":
                self.wfile.write(body[: CHUNK + 17])
                self.wfile.flush()
                self.close_connection = True
            else:
                for offset in range(0, len(body), CHUNK):
                    self.wfile.write(body[offset : offset + CHUNK])
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class SizeLimitError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Client:
    def __init__(self, name, ca_cert=None):
        self.name = name
        self.session: Any
        self.connections = set()
        context = ssl.create_default_context(cafile=ca_cert) if name in ("urllib", "httpx") else None
        if name == "urllib":
            self.session = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context)
            )
        elif name == "urllib3":
            import urllib3

            assert urllib3.__version__ == importlib.metadata.version("urllib3"), "urllib3 namespace overwritten"
            self.session = urllib3.PoolManager(retries=False, cert_reqs="CERT_REQUIRED", ca_certs=ca_cert)
        elif name == "httpx":
            import httpx  # ty: ignore[unresolved-import]

            self.session = httpx.Client(trust_env=False, http2=False, timeout=10, verify=context)
        elif name == "niquests":
            import niquests  # ty: ignore[unresolved-import]

            self.session = niquests.Session(disable_http2=True, disable_http3=True)
            self.session.trust_env = False
            self.session.verify = ca_cert or True
        else:
            from curl_cffi import CurlOpt  # ty: ignore[unresolved-import]
            from curl_cffi.requests import Session  # ty: ignore[unresolved-import]

            self.session = Session(
                trust_env=False, verify=ca_cert or True, curl_options={CurlOpt.HTTP_CONTENT_DECODING: 0}
            )

    def close(self):
        if self.name == "urllib3":
            self.session.clear()
        elif self.name != "urllib":
            self.session.close()

    def download(self, url, *, data=None, cap=None):
        method = "GET" if data is None else "POST"
        digest = hashlib.sha256()
        count = 0
        self.limit_exceeded = False
        self.received = 0
        # A real file and durability step are shared by all adapters.
        with tempfile.TemporaryFile() as target:

            def write(chunk):
                nonlocal count
                count += len(chunk)
                self.received = count
                if cap is not None and count > cap:
                    self.limit_exceeded = True
                    message = f"download exceeds {cap} bytes"
                    raise SizeLimitError(message)
                target.write(chunk)
                digest.update(chunk)
                return len(chunk)

            response: Any = None
            try:
                if self.name == "urllib":
                    request = urllib.request.Request(  # noqa: S310 - URL comes from the local test server.
                        url, data=data, headers={"Accept-Encoding": "identity"}, method=method
                    )
                    try:
                        response = self.session.open(request, timeout=10)
                    except urllib.error.HTTPError as error:
                        response = error
                    status, headers = response.code, response.headers
                    while chunk := response.read(CHUNK):
                        write(chunk)
                elif self.name == "urllib3":
                    response = self.session.request(
                        method,
                        url,
                        body=data,
                        headers={"Accept-Encoding": "identity"},
                        preload_content=False,
                        decode_content=False,
                        redirect=False,
                        timeout=10,
                        retries=False,
                    )
                    status, headers = response.status, response.headers
                    for chunk in response.stream(CHUNK, decode_content=False):
                        write(chunk)
                elif self.name == "httpx":
                    request = self.session.build_request(
                        method, url, content=data, headers={"Accept-Encoding": "identity"}
                    )
                    response = self.session.send(request, stream=True, follow_redirects=False)
                    status, headers = response.status_code, response.headers
                    for chunk in response.iter_raw(CHUNK):
                        write(chunk)
                elif self.name == "niquests":
                    response = self.session.request(
                        method,
                        url,
                        data=data,
                        headers={"Accept-Encoding": "identity"},
                        stream=True,
                        allow_redirects=False,
                        timeout=10,
                    )
                    status, headers = response.status_code, response.headers
                    for chunk in response.raw.stream(CHUNK, decode_content=False):
                        write(chunk)
                else:
                    from curl_cffi import CurlHttpVersion  # ty: ignore[unresolved-import]

                    options = {
                        "data": data,
                        "headers": {"Accept-Encoding": "identity"},
                        "allow_redirects": False,
                        "timeout": 10,
                        "http_version": CurlHttpVersion.V1_1,
                        "accept_encoding": None,
                    }
                    if self.name == "curl_stream":
                        response = self.session.request(method, url, stream=True, **options)
                        for chunk in response.iter_content():
                            write(chunk)
                    else:
                        response = self.session.request(method, url, content_callback=write, **options)
                    status, headers = response.status_code, response.headers
                self.connections.add(next(value for key, value in headers.items() if key.lower() == "x-connection-id"))
                target.flush()
                os.fsync(target.fileno())
                return status, dict(headers), count, digest.hexdigest()
            finally:
                if response is not None:
                    if self.name == "urllib3":
                        response.close()
                        response.release_conn()
                    else:
                        response.close()


def expected_bytes(case):
    return {
        "large": LARGE,
        "compressed": COMPRESSED,
        "post": b'{"query":"crop data","year":2024}',
        "not_modified": b"",
    }.get(case, SMALL)


def execute_one(client, base, case):
    path = {"warm_small": "small", "delayed_serial": "delay", "delayed_parallel": "delay", "size_cap": "cap"}.get(
        case, case
    )
    kwargs = {"data": expected_bytes(case)} if case == "post" else {}
    if case == "size_cap":
        kwargs["cap"] = CHUNK
    if case in ("size_cap", "truncated"):
        try:
            result = client.download(f"{base}/{path}", **kwargs)
        except Exception as error:  # noqa: BLE001 - Record adapter failures without stopping other clients.
            assert client.received > 0, f"{case}: failed before receiving the test body: {error}"
            if case == "size_cap":
                assert client.limit_exceeded, f"{case}: failed for an unrelated reason: {error}"
            else:
                expected_error = {
                    "urllib": ("IncompleteRead",),
                    "urllib3": ("ProtocolError",),
                    "httpx": ("RemoteProtocolError",),
                    "niquests": ("IncompleteRead",),
                    "curl_cffi": ("ConnectionError", "IncompleteRead"),
                    "curl_stream": ("ConnectionError", "IncompleteRead"),
                }[client.name]
                assert type(error).__name__ in expected_error, f"{case}: unexpected exception: {error!r}"
            return {"expected_failure": type(error).__name__, "received_bytes": client.received}
        if case == "truncated" and result[2] != len(LARGE):
            # urllib permits a short read; the caller must check Content-Length.
            return {"expected_failure": "caller Content-Length check", "received_bytes": client.received}
        message = f"{case}: invalid response was accepted"
        raise AssertionError(message)
    status, headers, count, digest = client.download(f"{base}/{path}", **kwargs)
    if case == "redirect":
        assert status == 302
        location = next(value for key, value in headers.items() if key.lower() == "location")
        assert location == "/small"
        status, _, count, digest = client.download(base + location)
    assert status == (304 if case == "not_modified" else 200), (case, status)
    body = expected_bytes(case)
    assert count == len(body), (case, count, len(body))
    assert digest == hashlib.sha256(body).hexdigest(), case
    return True


def worker(name, base, case, ca_cert=None):
    workers = 4 if case == "delayed_parallel" else 1
    requests = {"warm_small": 24, "delayed_serial": 12, "delayed_parallel": 12, "large": 4}.get(case, 1)
    clients: list[Client] = []
    clocks = {}

    def mark_start():
        clocks["start"] = time.perf_counter()
        clocks["cpu_start"] = time.process_time()

    def mark_end():
        clocks["elapsed_s"] = time.perf_counter() - clocks["start"]
        clocks["cpu_s"] = time.process_time() - clocks["cpu_start"]

    start_barrier = threading.Barrier(workers + 1, action=mark_start, timeout=20)
    end_barrier = threading.Barrier(workers, action=mark_end, timeout=20)

    def run(index):
        client = None
        try:
            # Curl sessions cache handles per thread: warm on the actual worker.
            client = Client(name, ca_cert)
            clients.append(client)
            execute_one(client, base, "warm_small")
            client.connections.clear()
            start_barrier.wait()
            values = [execute_one(client, base, case) for _ in range(index, requests, workers)]
            end_barrier.wait()
        except BaseException:
            start_barrier.abort()
            end_barrier.abort()
            raise
        else:
            return values
        finally:
            if client is not None:
                client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(run, index) for index in range(workers)]
        # Read the original worker exception below if its warmup failed.
        with contextlib.suppress(threading.BrokenBarrierError):
            start_barrier.wait()
        outcomes = [future.result() for future in futures]
    return {
        "client": name,
        "case": case,
        "elapsed_s": clocks["elapsed_s"],
        "cpu_s": clocks["cpu_s"],
        "max_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "requests": requests,
        "workers": workers,
        "contract_observations": [value for group in outcomes for value in group if isinstance(value, dict)],
        "connections_observed": len(set().union(*(client.connections for client in clients))),
        "versions": {
            package: importlib.metadata.version(package)
            for package in ("curl_cffi" if name == "curl_stream" else name,)
            if name != "urllib"
        },
        "python": platform.python_version(),
        "ok": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", nargs="+", choices=CLIENTS, default=list(CLIENTS))
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--output", type=Path, default=Path("dist/client-study/results.json"))
    parser.add_argument(
        "--env-root", type=Path, help="use isolated ENV_ROOT/bench-CLIENT/bin/python for each third-party client"
    )
    parser.add_argument("--tls", action="store_true", help="use a generated local certificate, verified by each client")
    parser.add_argument("--ca-cert", help=argparse.SUPPRESS)
    parser.add_argument("--self-check", action="store_true", help="one trial of every selected contract and workload")
    parser.add_argument("--worker", nargs=3, metavar=("CLIENT", "BASE_URL", "CASE"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not __debug__:
        parser.error("assertions must be enabled")
    if args.worker:
        try:
            result = worker(*args.worker, ca_cert=args.ca_cert)
        except Exception as error:  # noqa: BLE001 - Record adapter failures without stopping other clients.
            result = {
                "client": args.worker[0],
                "case": args.worker[2],
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }
        print(json.dumps(result))
        return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if args.tls:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        cert_dir = tempfile.TemporaryDirectory(prefix="sourceledger-study-tls-")
        cert, key = Path(cert_dir.name) / "cert.pem", Path(cert_dir.name) / "key.pem"
        subprocess.run(  # noqa: S603 - Fixed local OpenSSL command.
            [  # noqa: S607 - OpenSSL is supplied by the benchmark container.
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=sourceledger-benchmark",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            check=True,
            capture_output=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        args.ca_cert = str(cert)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"{'https' if args.tls else 'http'}://127.0.0.1:{server.server_port}"
    versions = {}
    for package in ("httpx", "urllib3", "niquests", "curl_cffi", "urllib3-future"):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[package] = importlib.metadata.version(package)
    result = {
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "versions": versions,
            "seed": args.seed,
            "protocol": "HTTP/1.1 localhost, verified TLS" if args.tls else "HTTP/1.1 localhost, no TLS",
            "timing": "warm pool, batch elapsed; imports/session creation excluded; one fresh process per trial",
            "rss": "process lifetime peak, includes imports/warmup",
            "storage": "TemporaryFile plus flush/fsync per response",
            "limits": "local smoke tests, not SSRF/security conformance; no retry tests",
        },
        "samples": [],
    }
    rng = random.Random(args.seed)  # noqa: S311 - Reproducible trial ordering, not secrets.
    try:
        for trial in range(1 if args.self_check else args.trials):
            combinations = [(client, case) for client in args.clients for case in args.cases]
            rng.shuffle(combinations)
            for client, case in combinations:
                interpreter = sys.executable
                if args.env_root and client != "urllib":
                    package = "curl_cffi" if client == "curl_stream" else client
                    interpreter = str(args.env_root / f"bench-{package}" / "bin/python")
                command = [interpreter, str(Path(__file__).resolve()), "--worker", client, base, case]
                if args.ca_cert:
                    command += ["--ca-cert", args.ca_cert]
                try:
                    process = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)  # noqa: S603 - Selected local interpreter and this file.
                    sample = json.loads(process.stdout)
                    if process.stderr:
                        sample["stderr"] = process.stderr[-2000:]
                except Exception as error:  # noqa: BLE001 - Record adapter failures without stopping other clients.
                    sample = {"client": client, "case": case, "ok": False, "error": str(error)}
                sample["trial"] = trial + 1
                result["samples"].append(sample)
                print(json.dumps(sample), flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2) + "\n")
    finally:
        server.shutdown()
        server.server_close()
        if args.tls:
            cert_dir.cleanup()
    if any(not sample["ok"] for sample in result["samples"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
