# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a breaking change comes only in a minor version (see Stability in the README).

## [Unreleased]

Upgrade steps:

- Rename `release` to `vintage` in `sources.toml`. The old key is now refused, with a
  message naming the new one (see Removed below).
- A source with `headers` needs an https `url` now (see Security below). Switch an
  `http://` URL to `https://`, which most APIs serve.

### Added

- `--manifest PATH` on every command, and `manifest=` on `main`, read a `sources.toml`
  other than the one in the current directory, such as one a project ships inside its
  package. The data still goes to `data/` in the current directory.
- A weekly CI job runs the Save Page Now contract test against the live archive, so the
  stand-in the other tests use is checked against the real service. Run it locally with
  `uv run pytest -m live`.
- Two docs pages: [How sourceledger works](docs/architecture.md), on the stages and the
  files each one writes, and [Use sourceledger safely](docs/secure-use.md). The README
  shows how to verify a release with `gh attestation verify` or `pypi-attestations`.

### Changed

- sourceledger is for Linux and macOS (POSIX), and its package metadata now says so. On
  Windows it runs, but `fetch` takes no lock against a second run.
- Reading a table with thousands of columns, or a `sources.toml` with thousands of sources,
  is faster: the check for repeated names no longer grows with the square of their number.
- `build` reads a CSV source and writes its output one row at a time, with the default
  parser, so memory use no longer grows with the size of the file. For a 161 MiB source it
  peaked at 2.5 GiB before, and it is about a quarter faster. The output and
  datapackage.json are the same, byte for byte. JSON, Excel, gzipped CSV and a custom
  `parse` are still read whole.
- `validate` reads each output one row at a time, and checks the raw file's hash without
  reading it into memory, so memory use no longer grows with the size of the data. For a
  161 MiB source it peaked at 2.5 GiB before. `build` on the command line counts the rows
  it prints the same way.
- `fetch` checks a raw file's hash before a conditional request without reading the whole
  file into memory, and reads the fetch log once per run. Before, each source with
  `archive = true` that came back with the same bytes read the whole log again.

### Removed

- **Breaking:** the old key `release` is no longer read as `vintage`. A manifest that
  still uses it fails to load and names the new key, rather than being kept as an unknown
  key and leaving the vintage "not stated".

### Fixed

- Bytes in the fetch log that are not UTF-8 no longer stop fetch, status and revisions
  with a bare decoding error. They read as U+FFFD, so a line they break is reported by
  its number, as any other damaged line is.
- `archive = true` now waits up to 120 s for Save Page Now, up from 15 s. The archive
  answers only once the snapshot is taken, 49 s for a GitHub page in one test, so a fetch
  whose snapshot took longer than 15 s recorded an empty `archive_url` although the
  archive had the page.

### Security

- **Breaking:** `headers` on a source whose `url` is not https is a manifest error, since
  the API key would cross the network in clear text.
- A redirect that changes the scheme, such as https to http on the same host, now drops
  the `headers` too, as a redirect to another host already did. Before, the key went out
  in clear text after the downgrade.

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
