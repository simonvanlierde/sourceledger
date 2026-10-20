# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a breaking change comes only in a minor version (see Stability in the README).

## [Unreleased]

### Added

- A weekly CI job runs the Save Page Now contract test against the live archive, so the
  stand-in the other tests use is checked against the real service. Run it locally with
  `uv run pytest -m live`.

## [0.2.0] - 2026-10-18

Upgrade steps:

- Rename any source id that uses a non-ASCII letter: ids must be ASCII letters, digits,
  `_` and `-` (see the Breaking change below). Fetch again afterwards, since the id names
  the raw file and the output.

### Added

- Python 3.15 is supported and tested in CI.
- JSON (an array of objects), Excel (the `xlsx` extra) and gzipped CSV and JSON sources,
  read by the new default reader `parse_source` from a source's `format` or its URL's
  extension. A gzipped file is refused when it expands past `max_bytes`.
- `headers` in `sources.toml` for API keys: each value names an environment variable
  (`"env:NAME"`), literal values are refused, the record keeps the header names only, and a
  redirect to another host drops them.
- `build` writes `data/processed/datapackage.json`, a Frictionless Data Package (v2) for
  the outputs: columns, size, SHA-256, source URL, licence and fetch record per output.
  It has no timestamp of its own, so the same records build the same bytes. `validate`
  reports a package that no longer matches the outputs.
- A JSON Schema for `sources.toml`, shipped in the package, for editors to check a manifest
  as it is typed. A test keeps it in step with what `load_manifest` accepts.
- `sourceledger fetch` marks each line `new` or `unchanged`, against the fetch log.
- `sourceledger init` writes a starter `sources.toml` with the schema line and a commented
  example source. It never overwrites an existing one.
- `sourceledger status` lists every source with its status, vintage, last fetch, checksum,
  licence and whether it may be republished, from the manifest and the log, offline.
- `sourceledger attribution` prints the implemented sources' reuse terms as a Markdown
  table, for a dataset's README or release notes.
- `sourceledger verify DIR` checks a downloaded dataset against its `datapackage.json`:
  every file it lists must exist inside `DIR`, symlinks included, with the size and
  SHA-256 it gives.
- `--only ID` for `fetch`, `build` and `validate`, and an `only` argument for the same
  functions, to work on some sources only. An unknown id is an error.
- `status`, `attribution` and `verify` are also functions in the public API.
- `archive = true` in `sources.toml` asks the Internet Archive's Save Page Now to snapshot
  a source's URL after a fetch that brings new bytes. The snapshot's address is kept in the
  fetch record and the log as `archive_url`, and kept when the same bytes are downloaded
  again. Each connect and read times out after 15 s,
  and a failure is a warning, never a failed fetch. Records written before this field
  still load.

### Changed

- **Breaking:** source ids must be ASCII (letters, digits, `_`, `-`). They name files,
  which must mean the same file on every system. Before, any Unicode letter passed.

### Fixed

- A line in `data/fetches.jsonl` that is JSON but not a fetch record is reported by line
  number instead of crashing `fetch` and `revisions`.
- A second `fetch` in the same project while one is running exits at once instead of
  interleaving writes with it (Linux and macOS).
- Downloads stream to disk in 1 MiB chunks, so a 200 MiB source no longer needs 200 MiB
  of memory, plus copies, to fetch.

### Security

- CI gives the read-only GitHub token to the zizmor hook only, not to every hook.
- Each release file on PyPI has a signed GitHub build-provenance attestation:
  `gh attestation verify <file> --repo simonvanlierde/sourceledger` shows where it was built.
- CI installs a set prek version, 0.5.5, instead of the newest one on each run.

## [0.1.0] - 2026-10-18

### Added

- `fetch`, `build`, `validate` and `revisions` over a `sources.toml` manifest, as
  functions and as the `sourceledger` command. They move here from the project-templates
  `data-pipeline` kind, which copied them into every project.
- A fetch record per download, conditional re-fetches with the recorded ETag and
  Last-Modified, and a committed log, `data/fetches.jsonl`, of every new version of each
  source.
- `build` takes the project's own reader, and `main` its name and User-Agent.
- Fetches retry 429, 500, 502, 503, 504, network errors and cut-short bodies, honour
  `Retry-After` in seconds or as a date up to 120 s, and stop one source after 300 s.
- Every source is tried: when some fail, the others are written, and `FetchError` lists
  each failure.
- Refused: a download over `max_bytes`, bytes that miss a pinned `sha256`, a compressed
  reply, and a redirect from a public host to a private address. A followed redirect is
  recorded as `final_url`.
- `validate` checks every provenance column of the output, the reuse terms (`terms`,
  `terms_url`, SPDX `license`, `redistribute`), and records made for another URL,
  vintage or pin.
- Manifest key `vintage`. The key `release` is still read, with a warning.

[Unreleased]: https://github.com/simonvanlierde/sourceledger/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/simonvanlierde/sourceledger/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/simonvanlierde/sourceledger/releases/tag/v0.1.0
