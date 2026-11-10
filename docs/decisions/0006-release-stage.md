# 0006. A release stage that writes and checks the archive metadata

- Status: proposed
- Date: 2026-11-10

## Context

Papers 3 and 4 publish ReLab-derived datasets, and the census pilot waits on a release path. The
README roadmap names a `release` stage. Today `build` writes `datapackage.json`, and `verify`
checks a published folder against it. Nothing writes the other metadata, nothing builds the
archive, and nothing stops a raw file with `redistribute = false` from reaching it.

[0001](0001-local-builds-and-redistribution.md) is why that matters: `build` copies every file
source into `data/raw/`, restricted ones included, and says "Publication must exclude restricted
originals". `redistribute` is only recorded (`ledger.py` stamps it into the resource;
`report` shows it). No code acts on it. Passing `validate` or `verify` is not permission to publish.

Scope is the roadmap boundary: the metadata formats ReLab v1 consumes (RO-Crate, Croissant,
CodeMeta, SHA256SUMS), each checked by its official validator, and a test that a
`redistribute = false` source never reaches the archive. The rules it automates are in the
dotfiles `dataset-publishing` skill, which points to this stage once it ships.
[0005](0005-provenance-core.md) keeps sourceledger "a small provenance core": the stage must
not grow into a packaging tool, and [0003](0003-stdlib-http.md) keeps the core free of runtime
dependencies.

## Decision

Add `sourceledger release OUT_DIR [--manifest PATH] [--dry-run] [--json]`. It reads the manifest,
the fetch log, `datapackage.json` and `data/processed/`, and writes a self-contained folder.
Options go after the stage name; exit codes are the CLI's: 0 done, 1 problems found, 2 usage,
3 could not run, 130 interrupted. `--json` carries `schema_version`.

**Input.** A manifest that `validate` passes. `release` runs that check first and stops with 1 if
it fails.

**Archive (`OUT_DIR`).**

- `data/`: every `data/processed/` output. A file source copied by `build` is included only when
  its `redistribute` is `true`.
- `datapackage.json`: the one `build` wrote, with resource paths under `data/`, and a resource
  dropped for each excluded source.
- `ro-crate-metadata.json` (RO-Crate 1.3): the root dataset, one data entity per shipped file with
  `sha256`, and one entry per source with its URL, licence, terms and retrieval time from the log.
- `codemeta.json`: describes the code that built the data (sourceledger version and the project's
  own fields), not the data.
- `SHA256SUMS`: every shipped file, written last, in the `sha256sum -b` format.
- Not written: `croissant.json` (see question 3), DataCite fields (Zenodo form or API), the card
  and licence files (the author's), `review/` (the exclusion list stays outside the archive).

**Validators.** After writing, `release` runs each check and fails with 1 on any finding:

| File | Check | Runs as |
| --- | --- | --- |
| `SHA256SUMS` | re-hash every listed file; none missing, none unlisted | built in (`hashlib`), same result as `sha256sum -c` |
| `datapackage.json` | `frictionless validate` | `release` extra (already a dev dependency) |
| `ro-crate-metadata.json` | `rocrate-validator validate` (PyPI `roc-validator` 0.13.1, Apache-2.0) | `release` extra |
| `croissant.json` | `mlcroissant validate --jsonld` (1.1.1, Apache-2.0) | `release` extra, only if question 3 says yes |
| `codemeta.json` | no official validator exists (see question 2) | context check |

The validators go in an optional `release` extra, imported only by this stage, never in the core.
`roc-validator` brings pyshacl, rdflib, requests and rich; `mlcroissant` brings 12 packages
including pandas, scipy and networkx; `frictionless` is heavier still. Each runs in its own
subprocess so one failure does not hide the others. A validator that is not installed exits 3 with
the install line; it never passes silently. The report names each validator and its version.

**The `redistribute = false` guarantee.** The stage builds the file list from the manifest, not
from a directory scan: a source is shipped only when its `redistribute` is exactly `true`. A
missing or non-boolean value is already a `validate` problem. After writing, a second pass hashes
every file in `OUT_DIR` and refuses (exit 1) when any hash matches a restricted source's
`data/raw/` bytes or its processed output, whatever the filename. The test builds a manifest with
one `true` and one `false` source, runs `release`, and asserts that no byte of the restricted file
is anywhere in the folder (RO-Crate, datapackage, SHA256SUMS and `data/` included) and that
metadata names it only as an excluded source. A second test flips the flag after `build` and
expects the restricted file to stay out. Each test must fail first against a version that
copies everything.

**Error classes.** Add `ReleaseError(ValueError)` for refused input and findings, with
`RestrictedSourceError` (a restricted file reached the folder), `ValidatorFailed` (a validator
reported findings) and `ValidatorMissing(OSError)` (extra not installed, exit 3). Base classes
still catch them. Other kinds wait until a second caller needs them.

## Out of scope

- Zenodo upload, DataCite files, schema.org JSON-LD, card and `CITATION.cff` writing.
- Parquet/CSV conversion, EXIF stripping, pseudonymisation, `reuse lint`, the ecoinvent text scan.
  These stay in the project's build script; the skill's checklist covers them.
- Zip packing and the 100-file Zenodo limit: `release` warns above 100 files and does not zip.
- Redistribution decisions: the stage obeys `redistribute`; it does not judge it.

## Consequences

- A release is one command whose output a person can check without sourceledger: `sha256sum -c`
  and the official validators work on the folder.
- The core still has no runtime dependencies; CI for the stage needs the `release` extra.
- The roadmap row says "Zenodo" too; this record leaves Zenodo to the deposit step. See question 4.
- Effort: about 14 hours. Stage and file list 4, RO-Crate writer 3 (about 40 lines per the
  2026-11-03 estimate, plus tests), CodeMeta and sums 1.5, validator subprocesses and error
  classes 2, the exclusion tests 2, docs, skill pointer and changelog 1.5.

## Open questions for Simon

1. **RO-Crate scope.** The 2026-11-03 ruling says "RO-Crate stays Assess until the data-pipeline
   template pilot"; the 2026-11-04 task note puts it in this stage. I did not resolve it. The
   record designs it in. Recommendation: ship it, since ReLab v1 consumes it and the writer is
   about 40 lines, and say in the changelog that the pilot is not its gate.
2. **CodeMeta checker.** No official validator exists: the `codemeta` repository ships a JSON-LD
   context and examples, no schema. CodeMetaPy 3.0.4 generates, is GPL-3.0 and does not validate.
   Recommendation: expand `codemeta.json` with `pyld` 3.3.0 (BSD) against the pinned context and
   fail when a key is dropped. Treat it as a test-only check, and say "context-checked" in docs.
3. **Croissant.** The task scope lists it, but the skill gives Croissant to ML datasets only, and
   Papers 3 and 4 may not be. Recommendation: write it only when `sources.toml` sets an opt-in
   key; otherwise skip it and report "skipped".
4. **Zenodo.** The README roadmap lists "Zenodo" metadata; the task note does not.
   Recommendation: drop it from the roadmap line and keep the DataCite fields in the Zenodo form.
5. **Validators in the `release` extra or dev only?** Recommendation: the extra, so users can run
   the checks on their own machine; the 3-exit keeps a missing validator visible.
6. **Trust the extra's own version pins?** The validators' major versions move fast.
   Recommendation: floor-pin them in the extra and record the versions in the report.
