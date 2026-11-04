# Compare download clients

Run `download_clients.py` inside a credential-free throwaway container. Mount only this checkout.
Use CPython 3.12 and a separate environment for each client:
`/tmp/bench-{urllib3,httpx,niquests,curl_cffi}/bin/python`.
Niquests installs a different implementation into the `urllib3` namespace, so a shared environment gives misleading results.

From the mounted checkout inside that container:

```sh
uv venv --python python3.12 /tmp/bench-urllib3
uv pip install --python /tmp/bench-urllib3/bin/python --only-binary :all: -r benchmarks/requirements/urllib3.txt
uv venv --python python3.12 /tmp/bench-httpx
uv pip install --python /tmp/bench-httpx/bin/python --only-binary :all: -r benchmarks/requirements/httpx.txt
uv venv --python python3.12 /tmp/bench-niquests
uv pip install --python /tmp/bench-niquests/bin/python --only-binary :all: -r benchmarks/requirements/niquests.txt
uv venv --python python3.12 /tmp/bench-curl_cffi
uv pip install --python /tmp/bench-curl_cffi/bin/python --only-binary :all: -r benchmarks/requirements/curl_cffi.txt
python3.12 benchmarks/download_clients.py --env-root /tmp --self-check --output dist/client-study/self-check.json
python3.12 benchmarks/download_clients.py --env-root /tmp --trials 5 --output dist/client-study/http.json
python3.12 benchmarks/download_clients.py --env-root /tmp --trials 5 --tls --output dist/client-study/tls.json
python3.12 benchmarks/client_backpressure.py
python3.12 benchmarks/client_footprint.py
```

`--tls` needs OpenSSL. It creates a temporary local certificate and makes each client verify it.
The private key contains no project or user credentials and disappears after the run.

Each trial uses a fresh process. Each worker creates and warms its own session on its actual thread before timing starts.
Every response goes to a temporary file with a SHA-256 check and `flush`/`fsync`.
The server disables Nagle's algorithm to avoid a local delayed-ACK artifact.
HTTP/1.1 is explicit; browser impersonation and automatic redirects are off.
The curl callback adapter provides backpressure; `curl_stream` measures its normal queue-backed stream separately.

Raw results include wall time, client CPU time, peak process memory, observed TCP connections, versions and errors.
Wall times cover complete batches, not individual requests. Imports, session creation, warmup and close are outside the timed interval.
The primary `ru_maxrss` values can inherit a parent high-water mark across exec on Linux; do not rank memory with them.
The separate backpressure experiment uses worker `VmHWM`, which resets across exec.
Its peaks include the worker's fixture/import overhead. The footprint probe additionally includes metadata/file inspection.
Recorded package sizes exclude generated caches and environment layout.
Contract cases check POST bodies, 304, manual redirects, compressed bytes, truncated bodies and download-size cancellation.
These local smoke tests do not establish sourceledger's full SSRF, proxy, deadline or retry guarantees.

Five samples can show a large effect. Their tail statistics cannot estimate production request latency.
Local HTTP/1.1 results cannot establish the benefit of HTTP/2 or HTTP/3 on a remote publisher.

Run the peer-guard proof separately in each environment, for example:

```sh
/tmp/bench-niquests/bin/python benchmarks/client_peer_guard.py niquests
/tmp/bench-urllib3/bin/python benchmarks/client_peer_guard.py urllib3-per-request
/tmp/bench-httpx/bin/python benchmarks/client_peer_guard.py httpx
/tmp/bench-curl_cffi/bin/python benchmarks/client_peer_guard.py curl_cffi
```

Some minimal guard prototypes deliberately fail or report an unsupported binding.
Those outcomes are evidence about the tested approach, not a production security verdict for the library.

The [study and implementation plan](../docs/performance-plan.md) explains the results and adoption gates.
The [recorded results](results/) retain individual samples rather than only rounded headline values.
