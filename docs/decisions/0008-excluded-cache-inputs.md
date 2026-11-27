# 0008. Keep excluded file inputs in their cache

- Status: accepted
- Date: 2026-11-28

## Context

The census keeps a 169 GB cache of files it cannot redistribute. The release only needs their fingerprints.
Copying the cache through both `raw` and `processed` takes disk space without adding release data.

## Decision

For a restricted file, `record(copy=False)` writes a log entry and a separate checksum sidecar. Callers
pass their own cache paths by source ID to `release`. The release stages the other outputs and records
each opted-in input's byte count and SHA-256 in `datapackage.json` under `excluded_inputs`. It checks the
cache against those fingerprints immediately before moving the release into place.

## Consequences

The cache owner keeps the input stable while producing and releasing the outputs. A recipient can verify
the shipped files offline. A recipient who has the cached inputs can pass a path for every excluded ID
to `verify`. Received metadata supplies fingerprints and IDs. Only the caller supplies paths.

The independent redistribution check from decision 0006 still refuses a shipped file with the same bytes
as a restricted input, including an old raw copy kept during migration. When switching an existing project
to hash-only recording, use a fresh processed folder so older build outputs stay out of the release.
