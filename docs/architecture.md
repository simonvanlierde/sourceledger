---
title: How sourceledger works
description: The four main stages, the files each one reads and writes, and where the code lives.
---

# How sourceledger works

sourceledger turns a list of public sources into a dataset in four stages: `fetch`,
`build`, `validate` and `release`. Each stage is a function in its own module (see
[Where the code lives](#where-the-code-lives)) and a command in `sourceledger.cli`. The
stages talk to each other only through files on disk, so you can run them in separate CI
jobs, or days apart. `record` takes the place of `fetch` for a file another tool downloaded.

```text
sources.toml ──fetch──> data/raw/        ──build──> data/processed/ ──validate──> problems
other tool ──record──>  data/fetches.jsonl
```

The [reference](reference.md) lists the manifest keys and command options. This page explains
how the parts fit together.

## The files on disk

| Path | Written by | Read by | In git |
| --- | --- | --- | --- |
| `sources.toml` | you (`init` writes a starter) | stages that use a manifest | yes |
| `data/raw/<id>.<ext>` | `fetch`, `record` | `build`, `validate` | no |
| `data/raw/<id>.<ext>.fetch.json` | `fetch`, `record` | `fetch`, `build`, `validate` | no |
| `data/raw/.lock` | `fetch`, `record` | `fetch`, `record` | no |
| `data/fetches.jsonl` | `fetch`, `record` | `fetch`, `record`, `status`, `revisions`, `report` | yes |
| `data/processed/<id>.csv` | `build` (tables) | `validate`, `verify`, `report` | no |
| `data/processed/<id>.parquet` | `build` (Parquet tables) | `validate`, `verify`, `report` | no |
| `data/processed/<id>.<ext>` | `build` (`format = "file"`) | `validate`, `verify`, `report` | no |
| `data/processed/datapackage.json` | `build` | `build --only`, `validate`, `verify`, `report` | no |
| `codemeta.json` | you | `release`, only when `release_formats` asks for a format | yes |
| `OUT_DIR` | `release` | `verify`, `release --check` | your choice |

`<ext>` is the extension of the source's URL. The README says
[what to commit](https://github.com/simonvanlierde/sourceledger#what-to-commit).

Each raw file, record and output is written to a temporary file in the same folder and
then renamed into place, so a crash never leaves half a file. The next `fetch` or `record`
removes a temp file that a killed one left in `data/raw/`, once it holds the lock.

The log is appended to instead, one line per new version. The next fetch drops a last line
that a crash cut off. It keeps a complete record that only lacks its final newline.

## fetch

`fetch` reads `sources.toml` and downloads each implemented source:

1. It reads the source's last `.fetch.json`. When that record still matches the manifest
   and the raw file, the request asks the server to send the file only if it changed
   (`If-None-Match` with the ETag, `If-Modified-Since` with Last-Modified). A `304 Not Modified` keeps the old file and
   record.
2. Otherwise it streams the body to a temporary file, hashing it on the way, within the
   source's size cap and the time budget.
3. It checks the SHA-256 against the source's `sha256` pin, if there is one.
4. It moves the file into place, appends a line to the log when the bytes are new for that
   source, and then writes the `.fetch.json` record.

Reading the log keeps only the latest record for each source, and reads it in small batches of
complete lines. The CLI uses that read, under the fetch lock, to label downloads as new or
unchanged.

The log line goes in before the record. If a crash comes between the two, the next run
sees no matching record and fetches in full, so no version is lost. A lock on
`data/raw/.lock` stops a second fetch in the same project while one runs (Linux, macOS and
Windows). [Use sourceledger safely](secure-use.md#limit-what-a-download-can-cost) lists what
`fetch` refuses at that path.

## record

`record` writes what `fetch` writes, for a file it did not download. It copies the file
into a temporary file in `data/raw/`, within the source's size cap, then takes the same
steps 3 and 4: the pin check, the move into place, the log line, and the record. It holds
the same lock and makes no request.

The record keeps `recorded_at`, the time of the call. `retrieved_at` is the download time
the other tool reported, or empty.

`record` adds a log line for new bytes. It also adds one when the same bytes come under
another `url` or `vintage`, switch between fetched and recorded, or are recorded for other
`parameters`. So the log's last line always describes the current record.

A later `fetch` sends the recorded ETag or Last-Modified only when they came from the
source's own URL.

## build

`build` makes the outputs from the raw files. It never uses the network.

1. For each implemented source, it re-reads the `.fetch.json` record and re-hashes the raw
   file. A record made for another URL, vintage or pin, or a raw file changed since, is
   refused.
2. It reads the raw file with the project's reader, a `Parser`, into columns and rows. The
   default reader reads CSV files, gzipped or not, one row at a time. JSON, Excel and custom
   readers hold the whole source in memory. JSON converts its decoded objects in place, to
   avoid keeping a second list of rows. CSV output writes one row at a time, and Parquet
   writes batches of at most 10,000 rows.
3. It writes `data/processed/<id>.csv` with five provenance columns in front of the source's
   own: `source_id`, `source_url`, `retrieved_at`, `vintage` and `raw_sha256`.
   `retrieved_at` is empty for a recorded file whose tool stated no download time.
   With `output_format = "parquet"`, it writes `<id>.parquet` with zstd compression and an explicit string schema.
4. It writes `datapackage.json`, a Frictionless Data Package that lists every output with
   its size, SHA-256, source and licence.

The same records give the same files, byte for byte: nothing in an output depends on the
time of the build.

For `format = "file"`, build copies the checked raw file into `data/processed/`,
without calling a reader. Its package resource has its size and hash, and the same
source, licence and fetch metadata. It omits `type` to describe a generic file,
as the [Data Resource standard](https://datapackage.org/standard/data-resource/#type) defines.
It has no table schema.

## validate

`validate` checks the outputs against the manifest and the records, and lists every
problem instead of stopping at the first. It re-hashes each raw file in blocks and reads
each CSV output one row at a time. Parquet tables are decoded in batches of at most 10,000 rows.
It compares each row's provenance
columns with the record. For a file source, it compares the output's hash with the fetch
record and checks that its package entry has the current provenance and reuse terms.

It also reports a source without reuse terms, an output that no source owns, and a
`datapackage.json` that no longer matches the outputs, or whose `name` is not the one
`sources.toml` sets. Every file in `data/processed/`,
including non-CSV files, must belong to a source or be the package descriptor.
`validate` exits 1 if it finds any problem.

## The other commands

- `status`, `attribution` and `revisions` read the manifest and the log only.
- `verify` reads only a published `datapackage.json` and the files it lists. It is for
  someone who downloaded your dataset and has neither your manifest nor your log.
- `report` reads the manifest, fetch log, package and outputs to create a
  [standalone HTML provenance report](reference.md#html-provenance-report).
- `init` writes a starter `sources.toml`.
- `release` copies the outputs of the sources that may be republished into a new folder,
  with `datapackage.json`, the metadata asked for and `SHA256SUMS`, then checks it. It reads
  the manifest, the records and the outputs, and `codemeta.json` when a metadata format is
  asked for. [Release a dataset](reference.md#release-a-dataset) has the details.

## Where the code lives

- `src/sourceledger/manifest.py`: `sources.toml` and the checks on it.
- `src/sourceledger/ledger.py`: the fetch log, the sidecar records and `record()`.
- `src/sourceledger/fetcher.py`: the HTTP(S) fetcher. It is the only module of sourceledger's own
  that opens a network connection. The release validators run in a subprocess and may fetch.
- `src/sourceledger/builder.py`: the readers, the outputs and `datapackage.json`.
- `src/sourceledger/report.py`: `validate`, `status`, `attribution`, `verify` and the
  standalone HTML provenance report.
- `src/sourceledger/releaser.py`: `release` and its checks. It runs the RO-Crate and
  Croissant validators, from the release extra, each in its own process.
- `src/sourceledger/cli.py`: the command line. One argparse subcommand per stage; it runs
  that stage and reports expected errors in one shape, with the
  [exit codes](reference.md#options-output-and-exit-codes) the reference lists. JSON
  reports put errors in the document on stdout.
- `src/sourceledger/sources.schema.json`: the JSON Schema for `sources.toml`, for editors.
  A test checks that it accepts exactly what `load_manifest` accepts.
- `src/sourceledger/__init__.py`: the public API, listed in `__all__`.

HTTP is the standard library's `urllib`, so the core has no runtime dependencies. sourceledger
adds the retries, the size cap, the time budget, the redirect check and the conditional
requests itself.

## Extending it

A project that needs another file format writes its own reader: a function that takes the
source's manifest table and the raw bytes, and returns columns and rows. It passes the
reader to `build`, or to `entry` from its own entry point. Everything else, including the
provenance columns, stays the same. A project that downloads with another tool, or its own
code, calls `record` for each file. [Recipes](recipes.md) has examples of both.

[Use sourceledger safely](secure-use.md) covers what each stage trusts and checks.
