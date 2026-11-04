# 0004. Declarative retrieval with per-request provenance

- Status: accepted direction; supersedes 0002
- Date: 2026-11-04
- Implementation: bounded GET expansion and complete-update publication available; retrieval-only POST pending

## Context

A project should describe repeatable downloads without maintaining its own downloader.
WISE already separates request expansion from parsing: its WDI source produces five requests and CWON produces four.
It keeps a file and provenance record for each response.
Its two other implemented sources use fixed URLs.
CML-IA uses fixed GET downloads; it supplies no POST use case.

[Decision 0002](0002-fetchers-stay-in-each-project.md) keeps templates and POST in projects.
That boundary also leaves projects responsible for retries, verification and download provenance.
Shared retrieval should simplify updates while keeping interpretation of data in project code.

## Decision

Add bounded declarative GET expansion, then retrieval-only POST with JSON or form bodies.
Keep the existing transport and its safety limits.
Do not add response-dependent pagination, submitted jobs, browser sessions or arbitrary workflows.
The [WDI example](../examples/declarative-wdi.toml) describes the first adopter's requests.
The GET syntax and publication mechanics below are implemented. POST remains the next implementation step.
[Download behavior](../fetching.md) documents the shipped commands, API and staging budget.

### Expansion and naming

The fields live under `source.request`.
`method` defaults to GET; `url` holds a template and `filename` names one response.
`values` defines scalar substitutions.
`expand` defines dimensions as ordered, nonempty lists of strings or integers, or `{ start = N, stop = M }` integer ranges.
Ranges include both ends, increase by one and reject `stop < start`.
Expand dimensions in sorted key order; the last dimension varies fastest.
Preserve list order and reject duplicate values.
Reject a plan exceeding 1,000 requests across the selected sources before accessing the network or creating response files.
This bounds request count, not practical disk use: 1,000 responses at the current 200 MiB cap can approach 195 GiB.
The update workflow enforces an aggregate staging budget covering responses, outputs and temporary files while preserving the previous complete generation.
Budget exhaustion stops the update without publication; retained historical generations can still accumulate disk usage.

Support only `{name}` substitution: no expressions, attribute access, format specifications or recursive substitution.
Unknown, unused or overlapping names are errors.
Substitution in URLs percent-encodes each value with no reserved characters left unescaped.
Keep API punctuation, such as WDI's semicolon-separated countries, literal in the URL.
Joining or mapping a list within a template is outside this first syntax; projects can supply a literal joined slice.
JSON substitutions occupy entire string values and preserve their resolved scalar type; never substitute into JSON keys.
Form values use the same rule before normal form encoding.

Filenames use the original scalar text, not URL-encoded text.
Accept only ASCII letters, digits, dots, underscores and hyphens, with a leading letter or digit.
Reject `.` and `..`, path separators, platform-reserved names and case-insensitive collisions across all planned files.
Preflight raw files, generated sidecars, built outputs and reserved package descriptors together.
For example, `x.csv` and `x.csv.fetch.json` must not overwrite a response's metadata.
Do not silently suffix or overwrite collisions.
The filename identifies an output location, not request identity.

### Request identity, credentials and checksums

Each record has a logical `source_id` and a stable `request_id`.
Compute the latter from a versioned, canonical description of the method, resolved URL, encoded body and header environment references.
Include resolved template parameters even when they do not alter those fields.
Changing any of these invalidates cache reuse and conditional validators.
Changing a filename or its position does not change the request ID.
Record the body encoding and content type; encode JSON deterministically and form fields in sorted key order.
Retain existing environment-variable headers and treat every configured header as representation-affecting.
Public request IDs include their names and references, never resolved header values.
Do not introduce another public header-value syntax in this change.

Public provenance stores only safe request descriptions, header names and environment-variable references.
Do not put credentials into URL or body templates; authenticated requests use environment-variable headers over HTTPS.
Reject environment references in URL/body values rather than silently resolving them there.
Operators must not put literal secrets in public manifest fields; arbitrary literal strings cannot be reliably classified as secrets.
Redact sensitive request material from errors and final redirect URLs as well as records.

Credential values must affect local cache eligibility without appearing in records or guessable public digests.
Use a private, randomly keyed local HMAC over actual credential values alongside the public request identity.
Keep that key and cache marker out of exported provenance; restrict their local file permissions.
Missing keys or changed credentials force a fresh retrieval, with no conditional validators from the previous identity.
Never assume two accounts return the same bytes because their public request descriptions match.

Expanded sources cannot use one source-level `sha256` pin.
Instead, `request.sha256` maps each resolved filename to its expected response digest.
When present, require exact coverage of the resolved request set; reject missing or extra entries before fetching.
Each reused response must pass its recorded hash and any current pin.
Unexpanded existing sources retain their current pin syntax.

### Provenance and project parsers

One resolved request produces one response file, record and built resource under its logical source.
Record the resolved parameters and request ID with the response's hash, vintage and retrieval metadata.
Add `request_id` to row provenance and resource metadata; keep `source_id` as the logical source.
History and revision comparisons key by both IDs, never just the logical source.
Changed and removed request IDs must remain distinguishable in revision reports.

Keep the per-response parser shape `parse(source, payload)`.
For expanded sources, pass a copy of the logical source with `request_id`, resolved `parameters` and the resolved `filename`.
Invoke it once for each response in expansion order.
Do not combine unrelated responses into one payload or introduce a project plugin registry.
Project parsers retain units, vintage extraction and domain interpretation.
The default JSON reader cannot parse WDI's metadata-plus-observations envelope.

HTTP success does not establish that an API returned the complete result.
WISE must retain its check that response metadata reports a single page before a build can complete.
A high `per_page` value is not proof of completeness.
Raw completion means every planned response passed transport and hash checks; dataset completion also requires parsing and validation.
No generic pagination detector or silent first-page acceptance is added.

### Staging, publication and recovery

Represent each update as a generation containing its resolved request set, verified response records and completed outputs.
Write successes into private staging and report failures with an unsuccessful command exit.
Keep successes after failure; a retry with the same plan reuses only hash-verified responses.
A changed plan creates a new generation; it may reuse individually verified matching requests, but never inherit a completion marker.
Removed requests must disappear from the new package.

Add an update orchestration entry point that owns staging, validation and publication.
It reuses fetching and building with explicit generation-local directories, then validates the completed outputs.
Preserve public `fetch() -> list[FetchRecord]`, `build() -> list[Path]` and caller-supplied raw/processed path semantics.
The existing `fetch` and `build` commands remain lower-level operations; only the new update workflow promises complete-update publication.
Write a durable completion marker only after those checks succeed.
Publish by atomically replacing one small pointer to an immutable generation on the same local filesystem.
Readers resolve that pointer once per operation and use both raw and built files from that generation.
Do not replace raw files, processed files and the package descriptor separately and call the result atomic.
An incomplete first update has no published dataset; report that state explicitly.

Use the project lock for recovery and publication.
On restart, treat unmarked generations as incomplete and verify staged files before reuse.
Sync generation files and the completion marker before replacing the pointer; sync the pointer's directory afterwards.
Retain the previous complete generation through publication and reader use; do not add automatic generation deletion in this change.
Direct filesystem consumers must use a resolved generation path for a consistent snapshot.
Adopters move to generation paths explicitly; do not silently redirect existing raw/processed path APIs.
A mutable directory alias alone cannot guarantee a multi-file snapshot.
Network filesystems and cross-filesystem staging are outside the initial publication guarantee.

For the update workflow, `--only` selects logical sources, including every request they expand into.
A partial update starts from the previous complete generation and replaces only selected sources as complete groups.
Require unselected source definitions to match that generation; otherwise refuse and request a full update.
With no previous complete generation, a partial update cannot publish a complete dataset.
Never publish a subset under a full-dataset completion marker.

### POST execution

Require `retrieval_only = true` for POST; that is the operator's explicit contract, not a property sourceledger can infer.
Permit one body encoding, JSON or form, with a matching content type.
Apply the existing byte caps, deadlines, connected-peer checks and bounded concurrency to every resolved request.
Retry only declared retrieval requests within the existing budget.
Reject POST redirects that would switch to GET (301, 302 or 303).
For 307/308, preserve the method and body and apply all redirect and destination checks.
Refuse cross-origin POST redirects initially: dropping headers does not prevent a body from carrying private data.
Use POST conditional requests only after method-specific tests establish correct reuse behavior; start without them.
Reject `archive = true` for POST: archiving the URL with GET does not capture a POST response.

## Consequences

Projects can update finite source slices through shared retrieval and provenance.
Implementation must change manifest validation/schema, fetching, cache identity, history, parser metadata, build publication, reports and CLI behavior together.
The current per-file publication mechanism cannot provide the agreed complete-update guarantee.
The generation workflow is additive; low-level callers continue to own their raw and processed paths.
Implement GET expansion/publication first, POST second, and adopt WISE separately.
No live POST adopter has been identified; use local retrieval fixtures rather than invent a domain workflow.
