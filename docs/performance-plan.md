---
title: Download client comparison and optimization plan
---

# Download client comparison and optimization plan

## Recommendation

Prototype **urllib3** as the pooled transport for the current synchronous fetcher.
Keep **Niquests** as the alternative if its public pre-send hook substantially simplifies the security checks.
Both passed the focused connected-peer proof. Neither is approved for adoption until the full fetch contracts pass.

This study changes no production transport, manifest field or download concurrency.
The existing proposed standard-library decision should remain unaccepted while this comparison informs the choice.

## Controlled experiment

Measured on 2026-11-04 against sourceledger `861899c`, with the standard-library client as a control.
Candidates: urllib3 2.8.0, HTTPX 0.28.1, Niquests 3.21.2 and curl_cffi 0.17.0.
Dependencies were resolved with a 2026-10-28 cutoff and installed only inside throwaway containers.
Each client had its own environment: Niquests' `urllib3-future` uses the `urllib3` import namespace.
The urllib3 adapter checks its imported version against its package metadata.

Timing used CPython 3.12.12 on Linux, four pinned logical CPUs and a 2 GiB container limit on a shared host.
Five shuffled trials ran in fresh worker processes for each client and workload.
Each worker created and warmed its client on the thread that performed the measured requests.
Wall time excludes imports, session creation, warmup and close.
The primary runs recorded `ru_maxrss`, which can inherit the parent's pre-exec high-water mark on Linux.
Those raw RSS values are not used to rank clients. The memory comparison below uses worker `VmHWM` instead.
All adapters hash their downloads and write temporary files with a flush and fsync.

The server used HTTP/1.1 with TCP_NODELAY. HTTPS used a generated local certificate that every client verified.
Automatic redirects and retries were disabled. The primary curl adapter used a synchronous content callback;
its ordinary queue-backed streaming API was measured separately.
This tests a streaming transport shape, not sourceledger's complete fetch implementation.

### Verified HTTPS results

Times are medians of five complete batches. Parentheses show the minimum and maximum batch times.
They are not individual-request percentiles or estimates of remote publisher latency.

| Client | 24 × 4 KiB responses, ms | 4 × 8 MiB files, ms | TCP connections for small batch |
| --- | ---: | ---: | ---: |
| urllib control | 34.7 (30.6–36.3) | 71.9 (71.5–73.8) | 24 |
| urllib3 | 10.3 (8.65–12.2) | 67.0 (65.4–68.7) | 1 |
| HTTPX | 12.8 (11.2–14.0) | 87.4 (86.0–87.7) | 1 |
| Niquests | 15.0 (14.2–16.8) | 116 (112–124) | 1 |
| curl_cffi callback | 10.4 (8.93–11.2) | 72.3 (70.8–75.6) | 1 |
| curl_cffi ordinary stream | 30.7 (29.5–32.5) | 70.1 (64.9–74.3) | 24 |

Pooling helped repeated small HTTPS downloads. Large-file results did not justify a native-client migration by themselves.
The four-worker experiment reduced twelve responses with an artificial 100 ms header delay from about 1.22 s to 0.31 s.
That effect occurred across all clients, including the standard library.
It shows latency overlap; it does not establish a fourfold speedup for real manifests or a saturated connection.
The experiment allowed four requests to the same host; a proposed two-per-host cap deliberately limits that overlap.

The HTTP and HTTPS runs each passed 300 samples, including checksum, POST body, 304, manual redirect,
raw compressed-byte, truncated-body and size-cap checks. A short body accepted by urllib requires the caller's
Content-Length check, as in the existing fetcher. These are smoke checks, not a complete retry or security suite.

### Startup and installed footprint

Five fresh imports per client, measured separately from the download fixture.
Installed sizes count distribution-listed files and required dependencies in the isolated CPython 3.12 environment.
They exclude generated caches, environment layout and the interpreter; optional extras change the total.
The process peak includes import and subsequent metadata/file inspection, not import alone.

| Client | Median import, ms | Median process RSS after measurement, MiB | Installed files, MiB |
| --- | ---: | ---: | ---: |
| urllib3 | 42.6 | 24.7 | 0.430 |
| HTTPX | 56.9 | 26.5 | 1.91 |
| Niquests | 160 | 35.2 | 13.1 |
| curl_cffi | 116 | 31.1 | 40.8 |

Import timing is informational, not a proposed startup optimization. The download benchmark's RSS also includes its synthetic fixture arrays.

### Slow-consumer memory

Three fresh worker processes per client and size downloaded through a synthetic 32 MiB/s sink.
The delay was proportional to bytes rather than chunk count. Every body still passed its hash and byte-count checks.
The figures below are median worker peaks from Linux `/proc/self/status` `VmHWM`.
They include the same small fixture/import overhead in each worker, but not the server's large fixture.

| Client | 8 MiB file, peak MiB | 80 MiB file, peak MiB |
| --- | ---: | ---: |
| urllib control | 37.4 | 37.3 |
| urllib3 | 36.2 | 36.2 |
| HTTPX | 38.4 | 38.3 |
| Niquests | 43.9 | 43.9 |
| curl_cffi callback | 40.2 | 40.2 |
| curl_cffi ordinary stream | 48.4 | 119 |

The callback curl path stayed bounded in this experiment. Its ordinary stream buffered much of the larger file behind the slow sink.
That API also closed its duplicated curl handle after streaming, explaining its connection counts in the small-file test.
These results describe curl_cffi 0.17.0's tested APIs, not a limitation of libcurl itself.
See the [curl_cffi streaming guidance](https://curl-cffi.readthedocs.io/en/stable/quick_start.html#streaming-response).

### Connected-peer proof

The existing policy checks the address actually connected to before sending a public-only redirected request.
A DNS preflight or a response hook cannot replace that check.
The loopback proof counts the first request byte and verifies that warm controls reuse the same connection.

| Minimal implementation tested | Fresh connection | Reused connection |
| --- | --- | --- |
| Niquests public `pre_send` hook | Pass | Pass |
| urllib3 connection hook only | Pass | Fails |
| urllib3 connection plus per-request check | Pass | Pass |
| HTTPX connection-complete trace only | Pass | Fails |
| curl_cffi Python pre-request callback binding | Unsupported | Unsupported |

A failed prototype is not proof that a client cannot be secured. In particular, HTTPX needs another approach for pooled requests.
The urllib3 prototype's mutable policy state is sequential test scaffolding, not a thread-safe production adapter.
The curl test checks the Python binding; libcurl capabilities alone do not establish that binding's support.

Niquests' hook exposes peer metadata before transmission. Its backend populates that address using `getpeername()`.
Its public hook is a real advantage, but the measured dependency and throughput costs also matter.
See [Niquests advanced usage](https://niquests.readthedocs.io/en/latest/user/advanced.html) and
the [backend implementation](https://github.com/jawah/urllib3.future/blob/main/src/urllib3/backend/hface.py).

## Ranked implementation plan

### 1. Add pooled transport, then bounded download workers

**Estimate:** 2–4 days for the transport gate and worker implementation, excluding publisher-specific issues.
**First change:** introduce one private urllib3 adapter with library redirects and retries disabled.
Keep hashing, source budgets, byte caps, conditional requests, policy and publication in sourceledger.
Keep an explicit local-file path for the existing trusted fixtures. Do not ship four selectable runtime backends.

Before replacing urllib, test fresh and pooled connections, DNS rebinding, IPv4/IPv6, HTTPS, proxies,
credential stripping and redirect bodies closed unread. The disallowed target must receive no request bytes.
Verify raw undecoded reads, truncation, size caps and slow-trickle deadlines. Disable implicit cookie persistence.
Use `read1(..., decode_content=False)` so deadline checks do not wait for a full requested chunk.
If preserving these rules needs substantial private internals, run the same gate with Niquests before choosing.

**Second change:** add an opt-in worker count, initially serial by default. Compare one, two and four workers.
Workers stage network results; one coordinator publishes raw files, history and sidecars in manifest order.
Retain the existing outer project lock. Bound both active and submitted-but-uncommitted downloads.
Start with a global limit of four and a per-origin limit of two, including redirected destinations.
Keep archive submissions separate and serialized initially. Running threads cannot be forcibly cancelled;
cleanup must wait for their bounded operations and remove every staged temporary file.

**Acceptance:** existing fetch tests pass, one TCP connection serves repeated eligible requests,
all sources still get attempted, errors remain ordered, interruption leaves no partial replacement,
and real-manifest wall time improves without multiplying peak memory or staged disk usage.
Measure mixed publishers, same-host requests, 304s, 429/Retry-After, failed downloads and a slow first source.

### 2. Remove avoidable output and history scans

**Estimate:** two small PRs, about half a day each.
At 100,000 rows, three interleaved fresh processes measured library build at 0.381–0.386 s and CLI build at 0.542–0.573 s.
The CLI parses each newly written output again solely to print a row count.
Profiling attributes the extra work to that parse. Output hashing was only about 0.012 s in the profiled library build.

Carry the count from writing to the CLI through a private result while preserving public `build() -> list[Path]`.
Compare that plumbing with an explicit choice to report bytes instead, before changing the CLI contract.
For Parquet, avoid decoding the whole table for a progress count.

Separately, stream `_last_records` and use one locked history snapshot for CLI fetch status and fetching.
A 19.7 MB, 100,000-line history retaining one source peaked at 44.2 MB of traced Python allocations.
The CLI currently parses history twice. Keep latest records in memory, not the whole decoded history.
Preserve line-numbered errors, interrupted-final-line recovery and validation of the latest record.

**Acceptance:** no post-build row parse; exact counts for empty tables and quoted newlines;
unchanged data and package bytes; one history parse per CLI fetch;
history allocations scale with distinct sources and the longest line.
Benchmark 10 and 100,000 table rows, one and twenty sources, and 10,000 and 100,000 history entries.
Keep hash verification intact; do not introduce a persistent digest cache.

### 3. Bound table memory before parallelizing builds

**Estimate:** 1–3 days for Parquet experiments and a bounded path; 1–2 days for default Excel streaming.
The completed generator improvement removes a duplicate Python list, but the complete Polars frame remains resident.
Validation also loads the frame and creates Python row dictionaries to check provenance.

Compare stable row-group writing and batch reading against the existing Polars path.
Test PyArrow if stable bounded APIs require a backend change; make that choice from measured install size, RSS and throughput.
Begin by removing unnecessary Python row construction in validation.
Then add bounded writing and reading with explicit schemas and zstd, preserving string spellings and provenance precedence.
Do not validate only provenance columns if that would hide corruption in other columns.

For Excel, preserve the public list-returning parser but stream the default build while the read-only workbook remains open.
Keep ZIP expansion guards, date conversion, sheet selection, row errors and cleanup on failure.
The already completed gzip CSV, JSON slot replacement and Parquet generator work are prerequisites, not new tasks here.

**Acceptance:** fresh-process RSS at 10,000, 100,000 and 1,000,000 rows stays proportional to a documented batch bound;
values and ordering match; damaged non-provenance pages are still detected; repeated builds remain deterministic;
workbook and output handles close after every error. Do not add parallel builds while full-table memory multiplies per worker.
Further JSON streaming requires a separate two-pass or spooling design to discover columns without losing numeric text.

### 4. Design POST provenance as a separate capability

**Estimate:** half a day for the contract and decision, then 1–2 days for implementation and tests.
All four clients can send POST. Sourceledger needs a request identity, not merely a different request call.
Define method, reproducible body identity and content type; a changed query must invalidate a prior conditional fetch.
Keep credentials out of manifests, logs and reports. Decide what body material may be recorded rather than assuming a digest is always private.

Define 301/302/303 versus 307/308 handling and refuse unsafe cross-origin body forwarding.
Do not replay POST automatically unless the source declares a read-only, safely repeatable query or supplies an appropriate idempotency contract.
POST responses are not ordinary GET archive captures. Disable that path until its semantics are explicit.
The accepted project-fetcher boundary needs a new decision before implementation.

**Acceptance:** two bodies at one URL cannot share cached provenance;
query changes trigger a fetch; failures cannot expose secrets or silently replay a state-changing request;
GET behavior remains unchanged. Keep pagination, browser sessions and a request-template language out of the first version.

### 5. Measure CI duplication and preserve performance evidence

**Estimate:** half a day after collecting main-versus-candidate CI timings.
Keep tests on Python 3.12–3.15. Inspect duplicated lint, formatting, typing and audit steps before moving checks out of the matrix.
Keep API checks, cold installation, CodeQL, DCO and benchmark coverage. Shared checks must still run for every relevant PR.

Add wall-time, RSS and request/connection counts to the existing instruction-based benchmarks for network and memory changes.
CodSpeed's current simulation excludes syscall waiting, so it cannot establish download-speed improvements.
Only change CI scheduling if runner minutes fall enough to justify the additional workflow conditions.

Resumable GET downloads remain conditional on a real large, unreliable source.
Require Range/If-Range validator tests, unchanged-server controls and full-byte hash verification before adopting them.
Do not prioritize async conversion, browser impersonation, HTTP/3 tuning or hash micro-optimizations without a workload showing their benefit.

## Reproduction and limits

The [benchmark instructions](../benchmarks/README.md) describe isolated environments and workload commands.
Raw HTTP, TLS, footprint, backpressure and interpreter smoke results accompany the experiment.
The measurement code is separate from `src/`; candidates are not added to runtime or development dependencies.

The local HTTP/1.1 results do not measure remote DNS, HTTP/2, HTTP/3, publisher limits or proxy performance.
Small five-sample timings on a shared host cannot rank near-equal results reliably.
The security proof does not certify a production adapter, and the proposed optimization savings remain targets until implemented.
All sixty HTTPS workload/contract smoke samples also passed on CPython 3.15.0 in separate client environments.
Both interpreter endpoints reproduced the connected-peer proof matrix.
The existing project suite passed 383 tests with 99.19% coverage; its locked development dependency audit passed.
