# 0001. Local builds and redistribution

- Status: accepted
- Date: 2026-10-31

## Context

File sources need a place in the data package without turning them into tables.
The package must work with offline `verify`, and private pipelines must be able to
use sources whose terms prohibit redistribution.

We considered copying only when redistribution is allowed, referring to restricted
files by URL, and copying all files into the local working dataset.

The [Data Resource standard](https://datapackage.org/standard/data-resource/) allows
local files and remote URLs. It describes a generic file by omitting `type`.
[Package paths](https://datapackage.org/standard/glossary/#url-or-path) cannot point
outside the package, so a descriptor in `data/processed/` cannot refer to `../raw/`.
[RO-Crate](https://www.researchobject.org/ro-crate/specification/1.3/data-entities.html)
also supports external references when files cannot be bundled.

## Decision

`build` copies each file source into the local working dataset, regardless of
`redistribute`, and preserves that flag in its metadata. Publication is a separate
action: a release stage must exclude restricted originals from its payload.

## Consequences

Local packages are self-contained and can be verified offline. Restricted sources
remain usable in private pipelines. Copying costs another file's worth of storage;
sourceledger does not unpack archives or rewrite these files.

Build output is private working data until its reuse terms have been checked.
Passing `validate` or `verify` establishes integrity, not permission to publish.
The current commands do not publish data or create a release archive.

A future release can describe restricted inputs by URL while excluding their bytes.
Such a package cannot claim complete offline verification of those inputs.
