# 0005. A provenance core that records files other tools fetch

- Status: accepted; supersedes 0004
- Date: 2026-11-05

## Context

[Decision 0004](0004-declarative-retrieval.md) moved templated GET and retrieval-only POST into
sourceledger, with per-request provenance and complete-update publication. The GET half and the
`update` command were built on main; neither shipped in a release.

Building them showed the cost. The source grew from about 1,900 lines in 0.4.0 to about 3,000,
and a full mutation run left about 250 surviving mutants in the new modules alone. The adopter it
was meant for, wise-pipeline, keeps its own fetcher for those sources, and no project asked for
POST. Tools such as [dlt](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic)
already handle templated, paginated and authenticated requests far better.

What no other tool we checked combines is the part sourceledger already had in 0.4.0: reuse
terms for each source, provenance in every output row, detection of a publisher's revision, and
an offline check for the people who receive the dataset. We compared pooch, DVC, DataLad, dlt,
Frictionless and the Croissant tools on 2026-11-05.

We weighed three options: keep the declarative fetcher; drop it and leave interop to each project;
or drop it and add one generic way to hand sourceledger a file another tool downloaded.

## Decision

sourceledger stays a small provenance core with a plain built-in fetcher: one URL per source,
conditional requests, retries and the redirect and credential guards. Templated, paginated or
parallel retrieval stays in other tools or in each project's own fetcher, as
[0002](0002-fetchers-stay-in-each-project.md) first said.

Files from those tools enter through `record()`, which writes the same log line, sidecar and
provenance the built-in fetcher writes, marked as recorded rather than fetched. Integration is
documented as recipes, not built as plugins, adapters or optional dependencies for each tool.

The declarative request expansion, the `update` command, the credential markers they needed and
`fetch --workers` are removed without a deprecation release: none of them shipped, and no outside
project uses sourceledger yet.

## Consequences

- The core gets smaller and easier to review. Its guarantees cover one writer for the log and the
  sidecars, whichever tool downloaded the bytes.
- A project with many requests writes a short loop around its fetch tool and calls `record()`
  once per file. It can pass the request's parameters as plain metadata.
- `record()` is a trust boundary. It refuses credentials in URLs, checks the manifest's pin, and
  copies the file into `data/raw/` so `validate` and `verify` read the bytes it hashed.
- A downloaded file's conditional-request validators only come along when the other tool exposes
  them. Revision detection still works from the hash.
- The [WDI example](../examples/declarative-wdi.toml) stays as the record of 0004's proposal. It
  no longer works.
