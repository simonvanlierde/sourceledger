---
title: Follow a publisher revision
description: Fetch a source, notice changed bytes, and rebuild the output.
---

## A worked example

This example follows one source through a publisher's revision. A local file stands in for
the publisher, so you can reproduce the revision. The output is from an actual run,
with the file path shortened.

### 1. Declare the source

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

### 2. Fetch, build and validate

```console
$ sourceledger fetch
co2  new               37 B  943000a3ec89  2025 release  file:///…/publisher/annual.csv
$ sourceledger build
2 rows -> data/processed/co2.csv
package -> data/processed/datapackage.json
$ sourceledger validate
0 problems
```

`fetch` saved the file to `data/raw/` with a record beside it, and logged the version in
`data/fetches.jsonl`. `build` wrote the output with its provenance columns, and the data
package that describes it.

### 3. Fetch again: nothing changed

```console
$ sourceledger fetch
co2  unchanged         37 B  943000a3ec89  2025 release  file:///…/publisher/annual.csv
```

Over HTTP, this fetch sends the ETag and Last-Modified back and the server answers that
nothing changed, so nothing is downloaded. The log stays unchanged.

### 4. The publisher revises a value

The publisher changes 2023's value from 419.31 to 419.32, without saying so. Copy the log
before you fetch, so you can compare against it:

```console
$ cp data/fetches.jsonl before.jsonl
$ sourceledger fetch
co2  new               37 B  b5e4e235da30  2025 release  file:///…/publisher/annual.csv
$ sourceledger validate
data/processed/co2.csv: rows come from an earlier fetch than co2.csv; re-run build
1 problems
$ sourceledger revisions before.jsonl
- `co2`: 943000a3ec89 -> b5e4e235da30, 37 bytes, fetched 2026-10-18T03:38:33Z from <file:///…/publisher/annual.csv>
```

The new bytes have a new checksum, so the fetch line says `new` and the log gains a second
line. `validate` now refuses the output built from the old download. `revisions`, given
`before.jsonl`, names the source that changed. It exits 0 here, since a test decides what a
revision breaks. The
[project-templates](https://github.com/simonvanlierde/project-templates) `data-pipeline`
kind runs this check weekly in CI and comments on an issue when a source changes.

### 5. Rebuild

After `sourceledger build`, each row carries the new download's provenance:

```csv
source_id,source_url,retrieved_at,vintage,raw_sha256,year,co2_ppm
co2,file:///…/publisher/annual.csv,2026-10-18T03:38:33Z,2025 release,b5e4e235da3066bccd4bf836b9f823b45709fb5e06afdb2b5328210c62c796e5,2023,419.32
```

## What to commit

See [what to commit](https://github.com/simonvanlierde/sourceledger/blob/main/README.md#what-to-commit) for the manifest, log and local data directories.

## Check a dataset you downloaded

Someone who downloads a published dataset can check it against its package, offline:

```console
$ sourceledger verify my-dataset/
0 problems
```

Each file the package lists must exist, with the size and SHA-256 it gives.

## Check a manifest as you type

The [manifest reference](reference.md#sourcestoml) shows the schema line for your editor.
