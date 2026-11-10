---
parent: /getting-started.html
title: Recipes
description: Record files that pooch, DVC, dlt or your own code downloaded, write a custom reader, and publish only a build that validates.
redirect_from:
  - /readers.html
---

# Recipes

sourceledger's own `fetch` downloads one URL per source. When another tool already does
the downloading, keep it, and hand each file to `record`. The file gets the same log line,
record and provenance columns as a fetched one, marked as recorded rather than fetched.

`record` copies the file into `data/raw/` and leaves the original where it is. It checks
the source's `sha256` pin and `max_bytes`, refuses a URL that holds a user name or
password, and keeps another URL without its query, where a signed download link keeps its
secret. Pass `retrieved_at` only when the tool tells you when it downloaded the file:
without it, the record states no download time instead of guessing one.

## Record a file another tool downloaded

Each recipe assumes a `sources.toml` with an implemented source whose `url` is the file's
address. The [reference](reference.md#other-commands) lists every `record` option.

### pooch

[pooch](https://www.fatiando.org/pooch/) returns the path of its cached copy. It keeps no
response headers or download time, so record only the URL:

```python
from pathlib import Path

import pooch
import sourceledger

url = "https://example.org/annual.csv"
path = pooch.retrieve(url, known_hash=None)
source = next(s for s in sourceledger.load_manifest() if s["id"] == "annual")
sourceledger.record(source, Path(path), url=url)
```

A file pooch already had in its cache was downloaded earlier, so passing the current
time as `retrieved_at` would be wrong.

### DVC

After `dvc import-url https://example.org/annual.csv data/import/annual.csv`, the
`annual.csv.dvc` file holds the URL and, for most HTTP servers, its ETag:

```sh
sourceledger record annual data/import/annual.csv \
  --url https://example.org/annual.csv --etag '<deps[0].etag from annual.csv.dvc, double quotes included>'
```

The ETag is kept only when the URL is the source's own, so a later `sourceledger fetch`
can ask the server whether the file changed. Copy it with its double quotes (`'"f432…-5"'`):
without them the server cannot match it.

### dlt

[dlt](https://dlthub.com)'s `RESTClient` follows pagination and authentication. It yields
parsed pages, not files, so write the items you keep to one file and record that. The
provenance then points at this file, not at the bytes the API sent, so pass the request
in `parameters`. The items must be a JSON array of objects, and the source needs
`format = "json"`, since an API URL has no `.json` ending to tell the reader:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import sourceledger
from dlt.sources.helpers.rest_client import RESTClient

started = datetime.now(UTC)
client = RESTClient(base_url="https://api.example.org")
items = [item for page in client.paginate("/indicators", params={"country": "NL"}) for item in page]
path = Path("downloads/indicators-nl.json")
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(items), encoding="utf-8")
source = next(s for s in sourceledger.load_manifest() if s["id"] == "indicators-nl")
sourceledger.record(source, path, retrieved_at=started, parameters={"endpoint": "/indicators", "country": "NL"})
```

Each page also carries its `requests` response as `page.response`, with the headers and
final URL, if you would rather record every page as a source of its own.

### Your own fetcher

A project that builds its own requests keeps that code, and calls `record` once per file.
This loop downloads one file per country:

```python
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import sourceledger

sources = {s["id"]: s for s in sourceledger.load_manifest()}
for country in ("nl", "be"):
    source = sources[f"population-{country}"]
    started = datetime.now(UTC)
    with urlopen(source["url"]) as response:
        body, etag = response.read(), response.headers.get("ETag", "")
    path = Path("downloads") / f"population-{country}.csv"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(body)
    sourceledger.record(source, path, retrieved_at=started, etag=etag, parameters={"country": country})
```

`parameters` keeps what the request asked for, in the log and in every copy of the
dataset's records. Never put a token or password in it.

## Write a custom reader

For a file you want to keep unchanged, set `format = "file"`; you do not need a reader.
For tables, `parse_source` chooses `parse_csv`, `parse_json` or `parse_xlsx`.

To read another table format, write a function that takes a source's manifest table and raw bytes.
Return the column names and rows: any iterable of mappings, whose cells may be numbers or None as well as text
(None is written as an empty cell). Pass the function to your project's command:

```python
# src/my_dataset/pipeline.py
import sourceledger
from sourceledger import Source


def parse(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Add a branch on source["id"] for a format the default reader cannot read."""
    return sourceledger.parse_source(source, payload)


def main() -> None:
    sourceledger.entry(parse=parse, prog="my-dataset", user_agent="my-dataset")
```

```toml
# pyproject.toml
[project.scripts]
my-dataset = "my_dataset.pipeline:main"
```

`my-dataset build` uses your reader. The other stages keep their usual behavior.
Publishers see `my-dataset` as the User-Agent when you fetch.

If your project ships `sources.toml` inside its package, pass the path as
`manifest=Path(__file__).with_name("sources.toml")`. The
[project-templates](https://github.com/simonvanlierde/project-templates) `data-pipeline`
kind sets this up, with CI and a weekly revision report.

The stages are also functions: `load_manifest`, `fetch`, `record`, `build`, `validate`,
`revisions`, `status`, `attribution`, `verify`, `release`, `check_release`, `report` and
`entry`. `verified(source)` returns a source's fetch record and raw bytes after checking that
both are current, for a reader that needs them.

## Publish a build only when it validates

[`release`](reference.md#release-a-dataset) does this for you: it validates, writes the folder
beside `OUT_DIR` and moves it into place only after its checks pass. The code below is the
manual version.

`build` replaces each output in place. To publish a folder that is either the old dataset
or a complete new one, build into a fresh folder, validate it, and only then swap it in:

```python
import shutil
from pathlib import Path

import sourceledger

sources = sourceledger.load_manifest()
output_format = sourceledger.load_output_format()
published, staged = Path("data/published"), Path("data/staged")
shutil.rmtree(staged, ignore_errors=True)
shutil.rmtree("data/previous", ignore_errors=True)  # left by a run that stopped between the renames
sourceledger.build(sources, processed=staged, output_format=output_format)
if problems := sourceledger.validate(sources, processed=staged, output_format=output_format):
    raise SystemExit("\n".join(problems))
if published.exists():
    published.rename("data/previous")
staged.rename(published)
shutil.rmtree("data/previous", ignore_errors=True)
```

Each rename is atomic. Between the two, `data/published` is briefly missing, never half
written.

## Check for revisions every week

A scheduled CI job can copy the log, fetch, and report what changed:

```sh
cp data/fetches.jsonl before.jsonl
sourceledger fetch
sourceledger revisions before.jsonl
```

`revisions` exits 0 when sources changed; your own tests decide whether a revision breaks
the dataset. Commit the new log so the next run compares against it.

## Follow a publisher's revision across two releases

A publisher that issues numbered releases changes the file and the URL together. The
[Public Utility Data Liberation Project](https://github.com/catalyst-cooperative/pudl)
(PUDL) publishes its tables under CC-BY-4.0. This recipe fetches one table from the
v2026.9.0 and v2026.10.0 releases, lets sourceledger report that the file changed and
which release each version came from, and then uses a short script to find what changed.
It downloads about 23 MB.

Save this as `sources.toml`. The Parquet file is kept as it is, so the source needs
`format = "file"`:

```toml
[[source]]
id = "pudl-generators"
name = "PUDL yearly generators"
status = "implemented"
format = "file"
url = "https://s3.us-west-2.amazonaws.com/pudl.catalyst.coop/v2026.9.0/out_eia__yearly_generators.parquet"
vintage = "PUDL v2026.9.0, DOI 10.5281/zenodo.22720286"
terms = "The PUDL data and documentation are published under the Creative Commons Attribution License v4.0 (CC-BY-4.0)."
terms_url = "https://github.com/catalyst-cooperative/pudl#licensing"
license = "CC-BY-4.0"
redistribute = true
```

Fetch the first release. `data/raw/` holds only the newest file, so keep a copy of this
one, and copy the log as well:

```sh
sourceledger fetch
cp data/raw/pudl-generators.parquet v2026.9.0.parquet
cp data/fetches.jsonl before.jsonl
```

```text
pudl-generators  new        12,979,286 B  048faf5314ba  PUDL v2026.9.0, DOI 10.5281/zenodo.22720286  https://s3.us-west-2.amazonaws.com/pudl.catalyst.coop/v2026.9.0/out_eia__yearly_generators.parquet
```

In `sources.toml`, change the version in `url` to `v2026.10.0` and `vintage` to
`"PUDL v2026.10.0, DOI 10.5281/zenodo.23375698"`. Fetch again and compare:

```sh
sourceledger fetch
cp data/raw/pudl-generators.parquet v2026.10.0.parquet
sourceledger revisions before.jsonl
```

```text
pudl-generators  new        10,643,130 B  30160046dfa2  PUDL v2026.10.0, DOI 10.5281/zenodo.23375698  https://s3.us-west-2.amazonaws.com/pudl.catalyst.coop/v2026.10.0/out_eia__yearly_generators.parquet
- `pudl-generators`: 048faf5314ba -> 30160046dfa2, 10,643,130 bytes, fetched 2026-11-10T17:06:49Z from <https://s3.us-west-2.amazonaws.com/pudl.catalyst.coop/v2026.10.0/out_eia__yearly_generators.parquet>
```

That is what sourceledger knows: the bytes differ, and the log ties each version to a
release and its DOI. It does not read rows. [`compare.py`](https://github.com/simonvanlierde/sourceledger/blob/main/docs/examples/pudl-revisions/compare.py)
is a plain script, outside sourceledger, that diffs the two files and carries the change
into one number:

```sh
uv run --no-project --with pyarrow python compare.py v2026.9.0.parquet v2026.10.0.parquet
```

```text
columns 112 -> 114 | added: ['county_id_fips', 'state_id_fips']
keys added 534 | removed 30
net_generation_mwh: 2897 changed, in report years 2001-2025
total_mmbtu: 137 changed, in report years 2008-2025
old 2025 data_maturity: {'provisional': 36118, None: 1}
new 2025 data_maturity: {'final': 36585}

year  old kt      new kt      change
2023   1,323,691   1,323,691  +0.00%
2024   1,322,734   1,322,734  +0.00%
2025   1,352,820   1,364,631  +0.87%
```

Rows are matched on `plant_id_eia`, `generator_id` and `report_date`, which are unique in
both files. `plant_id_pudl` is not a stable key: it differs on most overlapping rows.

The last table is the direct operational CO2 of US plants, in kilotonnes: fuel burned
(`total_mmbtu`) times a fixed emission factor per fuel, from 40 CFR 98 Table C-1, rounded.
It counts coal, gas and oil only. It is not a life-cycle figure: it leaves out fuel
supply, construction and any other fuel. Report years 2023 and 2024 are identical in both
releases, and the 2025 estimate rises by 0.87%.

This recipe does not say why. The 2025 rows moved from `provisional` to `final` data, and
the [release notes](https://github.com/catalyst-cooperative/pudl/releases/tag/v2026.10.0)
also name a fix to PUDL's generator fuel allocation, so the two releases differ in
more than one way. Separating those causes takes the release notes and the
upstream code, not this comparison.
