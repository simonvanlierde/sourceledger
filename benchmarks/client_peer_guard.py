"""Probe pre-request peer guards; run each client in its own dependency environment."""

# This experiment keeps optional clients isolated and uses assertions as its checks.
# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D101, D102, D103, PLR2004, S101, T201, INP001, C901, PLC0415

import argparse
import contextlib
import ipaddress
import json
import socketserver
import threading
from typing import cast


class RefusedError(ValueError):
    pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.received = 0
        self.connections: set[tuple[str, int]] = set()
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = cast("Server", self.server)
        while self.rfile.read(1):
            server.received += 1
            server.connections.add(self.client_address)
            self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            self.wfile.flush()


def check(address, public_only):
    assert address is not None, "missing connection peer metadata"
    assert address[0] == "127.0.0.1", address
    if public_only and not ipaddress.ip_address(address[0]).is_global:
        msg = "connected peer is non-public"
        raise RefusedError(msg)


@contextlib.contextmanager
def client(name, url):
    if name == "niquests":
        import niquests  # ty: ignore[unresolved-import]  # installed in its isolated container environment

        with niquests.Session(revocation_configuration=None, allow_incoming_cookies=False) as session:
            session.trust_env = False

            def get(block):
                def guard(request, **_kwargs):
                    check(request.conn_info.destination_address, block)

                with session.get(url, hooks={"pre_send": guard}, timeout=2) as response:
                    assert response.content == b"OK"

            yield get, niquests.__version__
    elif name == "httpx":
        import httpx  # ty: ignore[unresolved-import]  # installed in its isolated container environment

        with httpx.Client(trust_env=False, timeout=2) as session:

            def get(block):
                def trace(event, info):
                    if event == "connection.connect_tcp.complete":
                        check(info["return_value"].get_extra_info("server_addr"), block)

                response = session.get(url, extensions={"trace": trace})
                assert response.content == b"OK"

            yield get, httpx.__version__
    elif name in {"urllib3", "urllib3-per-request"}:
        import urllib3
        import urllib3.connection

        assert urllib3.__version__ == "2.8.0", "urllib3-future shadows urllib3; use an isolated environment"
        state = {"block": False}

        class Connection(urllib3.connection.HTTPConnection):
            def connect(self):
                super().connect()
                assert self.sock is not None
                check(self.sock.getpeername(), state["block"])

            def request(self, *args, **kwargs):
                if name == "urllib3-per-request" and self.sock is not None:
                    check(self.sock.getpeername(), state["block"])
                return super().request(*args, **kwargs)

        class Pool(urllib3.HTTPConnectionPool):
            ConnectionCls = Connection  # ty: ignore[invalid-assignment]  # urllib3 protocol rejects its concrete subclass default_socket_options

        with Pool("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=2, retries=False) as pool:

            def get(block):
                state["block"] = block
                response = pool.request("GET", "/", redirect=False)
                assert response.data == b"OK"

            yield get, urllib3.__version__
    else:
        import curl_cffi  # ty: ignore[unresolved-import]  # installed in its isolated container environment

        handle = curl_cffi.Curl()
        try:
            handle.setopt(curl_cffi.CurlOpt.PREREQFUNCTION, lambda *_args: 1)
        except NotImplementedError as error:
            msg = "Python pre-request callback unsupported"
            raise RuntimeError(msg) from error
        finally:
            handle.close()
        msg = "callback binding requires a separate prototype"
        raise RuntimeError(msg)


def probe(name, warm):
    with Server() as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with client(name, f"http://127.0.0.1:{server.server_address[1]}") as (get, version):
                if warm:
                    get(False)
                    get(False)
                    assert server.received == 2
                    assert len(server.connections) == 1, "warm control did not reuse its connection"
                before = server.received
                refused = False
                try:
                    get(True)
                except RefusedError:
                    refused = True
                return {
                    "client": name,
                    "version": version,
                    "case": "pooled" if warm else "fresh",
                    "refused": refused,
                    "request_bytes_observed": server.received - before,
                    "pass": refused and server.received == before,
                }
        except RuntimeError as error:
            return {"client": name, "case": "pooled" if warm else "fresh", "unsupported": str(error)}
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["niquests", "httpx", "urllib3", "urllib3-per-request", "curl_cffi"])
    args = parser.parse_args()
    print(json.dumps([probe(args.client, warm) for warm in (False, True)], indent=2))
