---
title: Download limits and retries
---

# How fetch behaves

- It tries four times, waiting 5, 10 and 20 s between attempts, on 429, 500, 502, 503,
  504, network errors and a body cut short. A `Retry-After` header, in seconds or as a
  date, raises the wait to at most 120 s. Any other error status fails at once.
- One source's download has a `timeout_s` budget, 300 s unless set, including retries and waits.
  It is checked before each attempt and after each body read. Each socket operation times
  out after the smaller of 30 s and the budget left at the start of that attempt.
  DNS lookup and HTTP header parsing can exceed the budget, so it is not a hard time limit.
  Redirects discard their unused response bodies. An optional archive request has the separate timeout below.
- Fetch downloads one source at a time by default. `sourceledger fetch --workers 2`
  overlaps two downloads using the existing HTTP transport. The library equivalent is
  `fetch(sources, workers=2)`. Only 1 or 2 workers are accepted, so any host receives
  at most two simultaneous source requests, including redirects and retries.
  Downloads run in pairs; a slow source delays the next pair. Files, history
  and records are written in manifest order on the calling thread. At most two
  temporary downloads are retained, each bounded by its source's `max_bytes`.
  Archive requests remain sequential and can overlap the other source's download.
- Every source is tried. When some fail, the others are still written, and the run
  exits 1 with one line per failed source.
- A second fetch in the same project, while one runs, exits at once (a lock on
  `data/raw/.lock`).
- Each line it prints says `new` or `unchanged`, against the last version in the log.
- For a source with `archive = true`, the Internet Archive request times out after 120 s
  per connect and read. When it fails, fetch prints a warning, leaves `archive_url` empty,
  and still succeeds. Save Page Now answers once the snapshot is taken.
  Its limit is five captures of one URL per day across all users. Past that it answers
  HTTP 429 with no `Retry-After`, and that fetch records no snapshot.

The checks on redirects, compression and keys are in [Use sourceledger
safely](secure-use.md). [How it works](architecture.md#fetch) has the steps and
the crash safety.

## Describe a finite set of downloads

Use `source.request` instead of `source.url` when a logical source needs several GETs:

```toml
[[source]]
id = "measurements"
status = "implemented"
format = "csv"
terms = "CC0"
terms_url = "https://example.org/terms"
license = "CC0-1.0"
redistribute = true

[source.request]
url = "https://example.org/measurements/{region}/{year}.csv"
filename = "measurements-{region}-{year}.csv"

[source.request.expand]
region = ["north", "south"]
year = { start = 2022, stop = 2024 }
```

This plans six files in region order, then year order. Dimension names sort alphabetically;
list order is preserved and ranges include both ends. `request.values` supplies fixed string or integer substitutions.
Only `{name}` substitution is supported. Unknown, unused, duplicate and overlapping parameters are errors.
URL substitutions escape all reserved characters. Write API separators literally in the URL;
list joining and expressions are outside this syntax. [The WDI example](examples/declarative-wdi.toml)
uses literal country separators and needs a custom reader for the response envelope.

The selected plan cannot exceed 1,000 requests. Filenames must start with an ASCII letter or digit
and contain only ASCII letters, digits, dots, underscores and hyphens. Platform-reserved names,
case-insensitive collisions and sidecar collisions are refused before fetching.
`update` also preflights built filenames; standalone library `fetch` checks only raw filenames.
A table named `slice.json` builds to `slice.csv`, or `slice.parquet` with Parquet output.
To pin responses, `request.sha256` maps every resolved filename to its SHA-256; missing or extra pins fail preflight.
A declarative source cannot use the source-level `sha256` field.

Each response has its own record, `request_id` and resolved `parameters`.
Its logical `source_id` stays the manifest id. Table rows add `request_id` before the usual provenance columns;
resource metadata keeps both ids and parameters. Resource names and history keys combine the ids with `:`.
A renamed file or reordered dimension leaves request identity unchanged. Changes to the resolved URL,
parameters or header references invalidate it. `expand_requests(sources)` returns the resolved source copies
for callers that need to select an individual response; `verified` accepts one such copy.
`status` and `revisions` distinguish requests; revision comparisons also list added and removed requests.

Use environment-variable headers over HTTPS for authentication. Never place secrets in URLs or parameter values.
The request record stores header names and environment references, with no resolved credentials.
A private local HMAC marker prevents reusing a response after credentials change; a missing key forces retrieval.
Keep `raw/.credentials` private and out of exports. Declarative final redirect URLs omit queries, fragments and user information.
For authenticated requests, final redirect URLs, conditional validators and detailed failure diagnostics are omitted
to avoid reflecting credentials. Those requests retrieve full responses instead of sending conditional headers. Literal secrets in a public manifest cannot be identified reliably.

## Publish only complete updates

```sh
sourceledger update --workers 2 --max-bytes 1073741824
```

`update` fetches, builds and validates a generation under `data/updates`, then publishes one atomic pointer.
It prints the generation path. `--root PATH` chooses another local generation store.
Existing `fetch` and `build` commands keep their caller-supplied paths and per-file publication behavior.
They do not promise a complete dataset after a failure.

A failed update exits 1 and preserves its successful responses privately. A retry verifies each response's
hash, current pin and credential identity before reuse. A changed plan starts another generation;
removed requests disappear from its package. After publication, the next update retrieves selected responses afresh.
The low-level `fetch` API continues to support conditional GETs.

`--max-bytes` bounds aggregate staging writes, including raw responses, outputs, metadata and temporary files.
The default is 1 GiB. Retained incomplete generations count against the limit.
Deleted or replaced bytes remain charged during an attempt, so a large retry can hit the limit early.
Complete historical generations are retained outside this allowance and can accumulate disk use.
Per-response `max_bytes` and `timeout_s` still apply. This is a disk limit, not a parser-memory limit.

Read a consistent snapshot by resolving the published generation once:

```python
from sourceledger import published, read_output, verify

generation = published()  # raises when no complete update has been published
assert verify(generation / "processed") == []
rows = read_output(generation / "processed" / "measurements-north-2022.csv")
```

Use that same path for raw files, outputs and `fetches.jsonl` throughout an operation.
Existing direct consumers of `data/raw` and `data/processed` must adopt generation paths explicitly.
The other CLI stages keep those paths and `data/fetches.jsonl`. For a generation, pass its explicit
raw, processed or log paths to the Python validation and report functions.
Files and the completion marker are synced before the pointer changes, then its parent directory is synced.
The previous generation remains available to existing readers. This guarantee requires one local filesystem;
network filesystems and cross-filesystem staging are outside it. Update takes the store's project lock.

`update --only ID` replaces a logical source as a complete group, including every expanded request.
It requires an existing complete generation and unchanged definitions for all unselected sources.
Use a full update after changing another source. An incomplete first update publishes no dataset.

A successful HTTP response proves transport completion only. A project reader must reject incomplete or
multi-page responses before dataset publication. Pagination, submitted jobs and browser sessions
remain in project code.

## Retrieve data with POST

Declare `retrieval_only = true` only when repeating the POST retrieves data without submitting a job or changing server state.
Sourceledger uses that declaration to permit retries; it cannot infer whether a server operation is safe to repeat.

```toml
[[source]]
id = "lookup"
status = "implemented"
format = "csv"
terms = "CC0"
terms_url = "https://example.org/terms"
license = "CC0-1.0"
redistribute = true

[source.request]
method = "POST"
retrieval_only = true
url = "https://example.org/lookup"
filename = "lookup-{year}.csv"
json = { year = "{year}", regions = ["north", "south"] }

[source.request.expand]
year = { start = 2022, stop = 2024 }
```

Choose exactly one body: `json` for JSON values, or `form` for a table of scalar fields.
For example, replace `json` above with `form = { year = "{year}", region = "north" }`.
Body substitutions must occupy entire string values. An integer parameter stays an integer in JSON.
Keys are literal strings; key templates, embedded substitutions and environment references are refused.
JSON supports nested objects and arrays. Form fields support strings, integers, finite numbers and booleans;
booleans encode as `True` and `False`. Both encodings sort keys, and sourceledger sets the matching content type.
Do not configure `Content-Type`, `Content-Length` or `Transfer-Encoding` headers for POST.

The method, encoded body, encoding and content type affect request identity.
Two different bodies at one URL produce distinct records and resources.
Provenance stores the encoded body, so never put credentials or other private values in it.
Use environment-variable headers over HTTPS for authentication; credential rotation prevents reuse of previous responses.
The same response byte caps, deadlines, retry budget and worker bound apply to POST.
Failed generations retain verified POST successes for retry, as they do GET successes.

POST never sends conditional validators. HTTP 301, 302 and 303 redirects fail rather than switch to GET.
HTTP 307 and 308 preserve the method and complete body only within the same origin: scheme, hostname and effective port.
Cross-origin POST redirects fail before sending the body, even without authentication headers.
POST cannot use `archive = true`: a GET snapshot of its URL would not capture the POST response.
