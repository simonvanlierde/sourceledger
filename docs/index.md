---
title: sourceledger
description: Build a dataset from public sources where every row says which download it came from.
---

## sourceledger

Build a dataset from public sources where every row says which download it came from.

sourceledger is a small Python library and command, with no dependencies, for projects that
turn other people's published data into a dataset of their own. It makes four promises:

1. **Every download is recorded.** The URL, the time, the SHA-256 and size of the bytes, the
   edition you expected, and the server's ETag and Last-Modified.
2. **Every output row names its download.** Five provenance columns lead each row, and a
   Frictionless `datapackage.json` describes every output.
3. **Every source states its reuse terms.** In the publisher's words, as an SPDX licence id,
   and as a yes or no on republishing. `validate` fails without them.
4. **A publisher's revision is noticed.** Each new version of a source is logged, and
   `revisions` lists what changed since a copy of that log.

[Install and reference](https://github.com/simonvanlierde/sourceledger#readme) ·
[PyPI](https://pypi.org/project/sourceledger/) ·
[Changelog](https://github.com/simonvanlierde/sourceledger/blob/main/CHANGELOG.md) ·
[How it works](architecture.md) ·
[Use it safely](secure-use.md)

### A worked example

This example follows one source through a publisher's revision. A local file stands in for
the publisher, so the revision can be made on purpose. The output is from an actual run,
with the file path shortened.

#### 1. Declare the source

`sources.toml`, in the project root:

```toml
[[source]]
id = "co2"
status = "implemented"
url = "file:///…/publisher/annual.csv"
vintage = "2025 release"
terms = "Public domain (US government work)"
terms_url = "https://gml.noaa.gov/about/disclaimer.html"
license = "LicenseRef-us-government-public-domain"
redistribute = true
```

#### 2. Fetch, build and validate

```console
$ sourceledger fetch
co2  new               37 B  38fe14e8b0ed  2025 release  file:///…/publisher/annual.csv
$ sourceledger build
2 rows -> data/processed/co2.csv
package -> data/processed/datapackage.json
$ sourceledger validate
0 problems
```

`fetch` saved the file to `data/raw/` with a record beside it, and logged the version in
`data/fetches.jsonl`. `build` wrote the output with its provenance columns, and the data
package that describes it.

#### 3. Fetch again: nothing changed

```console
$ sourceledger fetch
co2  unchanged         37 B  38fe14e8b0ed  2025 release  file:///…/publisher/annual.csv
```

Over HTTP, this fetch sends the ETag and Last-Modified back and the server answers that
nothing changed, so nothing is downloaded. The log gains no line.

#### 4. The publisher revises a value

The publisher changes 2023's value from 419.31 to 419.32, without saying so. Copy the log
before you fetch, so you can compare against it:

```console
$ cp data/fetches.jsonl before.jsonl
$ sourceledger fetch
co2  new               37 B  16f81da8a5c1  2025 release  file:///…/publisher/annual.csv
$ sourceledger validate
data/processed/co2.csv: rows come from an earlier fetch than co2.csv; re-run build
1 problems
$ sourceledger revisions before.jsonl
- `co2`: 38fe14e8b0ed -> 16f81da8a5c1, 37 bytes, fetched 2026-10-18T03:38:33Z from <file:///…/publisher/annual.csv>
```

The new bytes have a new checksum, so the fetch line says `new` and the log gains a second
line. `validate` now refuses the output built from the old download. `revisions`, given
`before.jsonl`, names the source that changed. It exits 0 here, since a test decides what a
revision breaks. The
[project-templates](https://github.com/simonvanlierde/project-templates) `data-pipeline`
kind runs this check weekly in CI and comments on an issue when a source changes.

#### 5. Rebuild

After `sourceledger build`, each row carries the new download's provenance:

```csv
source_id,source_url,retrieved_at,vintage,raw_sha256,year,co2_ppm
co2,file:///…/publisher/annual.csv,2026-10-18T03:38:33Z,2025 release,16f81da8a5c14a281dc0d85d59275de78ee39b38d4fd9ea5a0b3e85fb3282333,2023,419.32
```

### What to commit

Commit `sources.toml` and `data/fetches.jsonl`, and keep `data/raw/` and `data/processed/`
out of git. The [README](https://github.com/simonvanlierde/sourceledger#what-to-commit)
says why.

### Check a dataset you downloaded

Someone who downloads a published dataset can check it against its package, offline:

```console
$ sourceledger verify my-dataset/
0 problems
```

Each file the package lists must exist, with the size and SHA-256 it gives.

### Check a manifest as you type

For checks in an editor with a TOML language server (Tombi, Taplo), put this line at the
top of `sources.toml`. `init` writes it, with the version you have installed in place of
`v0.3.1`:

```toml
#:schema https://raw.githubusercontent.com/simonvanlierde/sourceledger/v0.3.1/src/sourceledger/sources.schema.json
```
