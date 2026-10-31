# 0001. Local builds and redistribution

- Status: accepted
- Date: 2026-10-31

## Decision

`build` copies file sources into the local working dataset, including those with
`redistribute = false`. It preserves the flag in the package metadata.
Publication must exclude restricted originals; the current commands do not publish data.

## Reason

Local copies let `verify` work offline and keep restricted sources usable in private pipelines.
The [Data Resource standard](https://datapackage.org/standard/data-resource/) permits local files
and remote URLs. It describes a generic file by omitting `type`.
[Package paths](https://datapackage.org/standard/glossary/#url-or-path) cannot point outside
the package, so `data/processed/datapackage.json` cannot refer to `../raw/`.

URL-only references would avoid copies, but could not provide complete offline verification.
See also [RO-Crate external data entities](https://www.researchobject.org/ro-crate/specification/1.3/data-entities.html).

## Consequences

Each file copy uses additional disk space. Passing `validate` or `verify` checks integrity,
not permission to publish. Check the [reuse terms](../secure-use.md#check-redistribute-before-you-publish-raw-files)
before publication.
