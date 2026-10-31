---
title: How sourceledger works
description: The three main stages, the files each one reads and writes, and where the code lives.
---

## How sourceledger works

sourceledger turns a list of public sources into a dataset in three stages: `fetch`,
`build` and `validate`. Each stage is a function in `sourceledger.ledger` and a command
in `sourceledger.cli`. The stages talk to each other only through files on disk, so you
can run them in separate CI jobs, or days apart.

```text
sources.toml ──fetch──> data/raw/        ──build──> data/processed/ ──validate──> problems
                        data/fetches.jsonl
```

The [README](https://github.com/simonvanlierde/sourceledger#readme) has every manifest key
and command option. This page explains how the parts fit together.

### The files on disk

| Path | Written by | Read by | In git |
| --- | --- | --- | --- |
| `sources.toml` | you (`init` writes a starter) | every stage | yes |
| `data/raw/<id>.<ext>` | `fetch` | `build`, `validate` | no |
| `data/raw/<id>.<ext>.fetch.json` | `fetch` | `fetch`, `build`, `validate` | no |
| `data/raw/.lock` | `fetch` | `fetch` | no |
| `data/fetches.jsonl` | `fetch` | `fetch`, `status`, `revisions` | yes |
| `data/processed/<id>.csv` | `build` (tables) | `validate` | no |
| `data/processed/<id>.<ext>` | `build` (`format = "file"`) | `validate`, `verify` | no |
| `data/processed/datapackage.json` | `build` | `build --only`, `validate`, `verify` | no |

`<ext>` is the extension of the source's URL. The README says
[what to commit](https://github.com/simonvanlierde/sourceledger#what-to-commit).

Each raw file, record and output is written to a temporary file in the same folder and
then renamed into place, so a crash never leaves half a file. The log is appended to
instead, one line per new version. A last line cut off by a crash is dropped on the next fetch. A complete record that only
lacks its final newline is kept.

### fetch

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

The log line goes in before the record. If a crash comes between the two, the next run
sees no matching record and fetches in full, so no version is lost. A lock on
`data/raw/.lock` stops a second fetch in the same project while one runs (Linux and macOS
only). `fetch` refuses a symlink at `data/raw/.lock` or `data/fetches.jsonl`.

### build

`build` makes the outputs from the raw files. It never uses the network.

1. For each implemented source, it re-reads the `.fetch.json` record and re-hashes the raw
   file. A record made for another URL, vintage or pin, or a raw file changed since, is
   refused.
2. It reads the raw file with the project's reader, a `Parser`, into columns and rows. With
   the default reader, a plain CSV is read and written one row at a time, so memory stays
   small whatever the file's size. JSON, Excel, gzipped CSV and a custom `Parser` are read
   whole.
3. It writes `data/processed/<id>.csv` with five provenance columns in front of the source's
   own: `source_id`, `source_url`, `retrieved_at`, `vintage` and `raw_sha256`.
4. It writes `datapackage.json`, a Frictionless Data Package that lists every output with
   its size, SHA-256, source and licence.

The same records give the same files, byte for byte: nothing in an output depends on the
time of the build.

For `format = "file"`, build copies the checked raw file into `data/processed/`,
without calling a reader. Its package resource has its size and hash, and the same
source, licence and fetch metadata. It omits `type` to describe a generic file,
as the [Data Resource standard](https://datapackage.org/standard/data-resource/#type) defines.
It has no table schema.

### validate

`validate` checks the outputs against the manifest and the records, and lists every
problem instead of stopping at the first. It re-hashes each raw file in blocks and reads
each output one row at a time, so memory stays small. It compares each row's provenance
columns with the record. For a file source, it compares the output's hash with the fetch
record and checks that its package entry has the current provenance and reuse terms.
It also reports a source without reuse terms, an output that no source owns, and a
`datapackage.json` that no longer matches the outputs. Every file in `data/processed/`,
including non-CSV files, must belong to a source or be the package descriptor.
It exits 1 if it finds any problem.

### The other commands

- `status`, `attribution` and `revisions` read the manifest and the log only.
- `verify` reads only a published `datapackage.json` and the files it lists. It is for
  someone who downloaded your dataset and has neither your manifest nor your log.
- `init` writes a starter `sources.toml`.

### Where the code lives

- `src/sourceledger/ledger.py`: every stage, the readers, and the fetch logic. One module,
  read top to bottom: manifest checks, HTTP, the log, fetch, the readers, build, validate,
  and the reports.
- `src/sourceledger/cli.py`: the command line. It parses the arguments, runs one stage,
  and turns an expected error into one line on stderr and exit code 1.
- `src/sourceledger/sources.schema.json`: the JSON Schema for `sources.toml`, for editors.
  A test checks that it accepts exactly what `load_manifest` accepts.
- `src/sourceledger/__init__.py`: the public API, listed in `__all__`.

HTTP is the standard library's `urllib`, so there are no runtime dependencies. sourceledger
adds the retries, the size cap, the time budget, the redirect check and the conditional
requests itself.

### Extending it

A project that needs another file format writes its own reader: a function that takes the
source's manifest table and the raw bytes, and returns columns and rows. It passes the
reader to `build`, or to `main` from its own entry point. Everything else, including the
provenance columns, stays the same. The README's
[Your own readers](https://github.com/simonvanlierde/sourceledger#your-own-readers) has an
example.

[Use sourceledger safely](secure-use.md) covers what each stage trusts and checks.
