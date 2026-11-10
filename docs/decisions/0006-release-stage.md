# 0006. A release stage that writes and checks the archive metadata

- Status: accepted
- Date: 2026-11-10

## Context

Papers 3 and 4 publish ReLab-derived datasets, and the census pilot waits on a release path. The
README roadmap names a `release` stage. Today `build` writes `datapackage.json`, and `verify`
checks a published folder against it. Nothing writes the other metadata, nothing builds the
archive, and nothing stops a raw file with `redistribute = false` from reaching it.

[0001](0001-local-builds-and-redistribution.md) is why that matters: `build` copies every file
source into `data/raw/`, restricted ones included, and says "Publication must exclude restricted
originals". `redistribute` is only recorded (`ledger.py` stamps it into the resource; `report`
shows it). No code acts on it. Passing `validate` or `verify` is not permission to publish.

Scope is the roadmap boundary: the metadata formats ReLab v1 consumes (RO-Crate, Croissant,
SHA256SUMS), each checked by its official validator, and a test that a `redistribute = false`
source never reaches the archive. The rules it automates are in the dotfiles `dataset-publishing`
skill, which points to this stage once it ships. [0005](0005-provenance-core.md) keeps
sourceledger "a small provenance core", and [0003](0003-stdlib-http.md) keeps the core free of
runtime dependencies.

## Decision

Add `sourceledger release OUT_DIR [--manifest PATH] [--dry-run] [--json]`, and
`sourceledger release --check DIR` to validate a folder that already exists. Options go after the
stage name. The exit codes are sourceledger's: 0 done, 1 problems found, 2 usage, 3 could not run,
130 interrupted. `--json` carries `schema_version`.

**Input.** A manifest that `validate` passes. `release` runs that check first and stops with 1 if
it fails. The dataset's descriptive fields (name, author, license, version, identifier or DOI,
`datePublished`) come from the project's `codemeta.json`, which `citesync` keeps right from
`CITATION.cff`. CodeMeta, RO-Crate and Croissant share schema.org terms, so the fields copy over
with no new mapping. A requested format whose field is missing stops the stage with 1, naming it.

**Archive (`OUT_DIR`).**

- `data/`: every `data/processed/` output. A file source copied by `build` goes in only when its
  `redistribute` is `true`.
- `datapackage.json`: the one `build` wrote, with a resource dropped for each excluded source.
- `ro-crate-metadata.json` (RO-Crate 1.3): only when asked for. It has the root dataset, one data
  entity per shipped file with `sha256`, and one entry per source with its URL, licence, terms and
  retrieval time from the log.
- `croissant.json`: only when asked for. File entries carry the same `sha256` as `SHA256SUMS`.
- `SHA256SUMS`: every shipped file, written last, in the `sha256sum -b` format.

A manifest key before the first `[[source]]`, `release_formats = ["ro-crate", "croissant"]`,
turns the two optional files on. Without it the default path writes only `data/`,
`datapackage.json` and `SHA256SUMS`, so the 2026-11-03 ruling "RO-Crate stays Assess until the
data-pipeline template pilot" still holds for the default. A format that is not asked for is
reported as "skipped", never silently missing.

The stage does not write `codemeta.json`, `.zenodo.json`, DataCite fields, the card, licence files
or `review/` (the exclusion list stays outside the archive). `citesync` owns citation metadata. It
will validate a project's `codemeta.json` against the vocabulary `codemeta-drift` exports, and
later read `ro-crate-metadata.json` and `croissant.json` as compare-only targets to flag drift.
Each fact has one writer.

**Validators.** `release` and `release --check` run each check on the files that exist, and fail
with 1 on any finding:

| File | Check | Runs as |
| --- | --- | --- |
| `SHA256SUMS` | re-hash every listed file; none missing, none unlisted | built in (`hashlib`), same result as `sha256sum -c` |
| `ro-crate-metadata.json` | `rocrate-validator validate` (PyPI `roc-validator` 0.13.1, Apache-2.0) | `sourceledger[release]` |
| `croissant.json` | `mlcroissant validate --jsonld` (1.1.1) | `sourceledger[release]` |

Both validators are in the `release` extra, with floor pins (at least the tested version), and
imported only by this stage. `roc-validator` brings pyshacl, rdflib, requests and rich;
`mlcroissant` brings pandas, scipy and networkx. Each runs in its own subprocess, so one failure
does not hide another. A validator that a requested format needs but is not installed exits 3 with
the install line; it never passes silently. The report records the name and version of each
validator that ran. The data-pipeline template's release recipe and CI run the same `--check`.
`datapackage.json` keeps its existing checks (`validate`, `verify`).

**The `redistribute = false` guarantee.** The stage builds its file list from the manifest, not
from a directory scan: a source is shipped only when its `redistribute` is exactly `true`. A
missing or non-boolean value is already a `validate` problem. After writing, a second pass hashes
every file in `OUT_DIR` and refuses (exit 1) when any hash matches the bytes of a restricted
source in `data/raw/` or its processed output, whatever the filename. The first test builds a
manifest with one `true` and one `false` source, runs `release`, and asserts that no byte of the
restricted file is anywhere in the folder (`data/`, RO-Crate, Croissant, datapackage and
`SHA256SUMS` included) and that the metadata names it only as an excluded source. The second flips
the flag after `build` and expects the restricted file to stay out. Each test must fail first
against a version that copies everything.

**Error classes.** `ReleaseError(ValueError)` for refused input and findings, with
`RestrictedSourceError` (a restricted file reached the folder) and `ValidatorFailed` (a validator
reported findings), and `ValidatorMissing(OSError)` (the extra is not installed, exit 3). Code
that catches the base classes still works. Other kinds wait until a second caller needs them.

## Out of scope

- Zenodo upload and DataCite fields (the Zenodo form or `citesync`), and the README roadmap line
  no longer names Zenodo.
- A CodeMeta file or check, schema.org JSON-LD, `CITATION.cff` and the dataset card.
- Parquet or CSV conversion, EXIF stripping, pseudonymisation, `reuse lint` and the ecoinvent
  text scan. These stay in the project's build script; the skill's checklist covers them.
- Zip packing and the 100-file Zenodo limit: `release` warns above 100 files and does not zip.
- Redistribution decisions: the stage obeys `redistribute`; it does not judge it.

## Consequences

- A release is one command whose output a person can check without sourceledger: `sha256sum -c`
  and the official validators work on the folder.
- The core keeps no runtime dependencies. CI for the stage installs the `release` extra.
- A project with a stale `codemeta.json` gets stale dataset metadata. `citesync` is where that is
  caught, not here.
- `citesync` and `codemeta-drift` will be aligned to these exit codes, as separate work.
- Effort: about 13 hours. Stage, file list and the manifest key 3; RO-Crate writer 2; Croissant
  writer 2.5 (built from `datapackage.json`); sums and recheck 0.5; validator subprocesses,
  `--check`, versions in the report and error classes 2; the exclusion tests 2; docs, changelog
  and the skill pointer 1. Dropping CodeMeta saved about 1.5 hours, and writing Croissant, which
  the first draft left to the author, took most of that back.
