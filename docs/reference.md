---
title: Manifest and command reference
redirect_from:
  - /reports.html
  - /provenance-report.html
---

# Manifest and commands

This page lists the manifest keys, the commands, their [JSON reports](#json-reports) and the
[HTML provenance report](#html-provenance-report). [Recipes](recipes.md) covers custom readers and
other download tools, and [download behavior](fetching.md) the retries and limits.

## sources.toml

A source is one file you download. A vintage is the edition you expect to use, such as a release year.
The manifest records its reuse terms; it does not decide whether those terms permit your intended use.

Set `output_format` before the first `[[source]]` table to choose the project's table outputs:

```toml
output_format = "parquet"
```

Install the CLI with `uv tool install 'sourceledger[parquet]'` first.
For a Python project, use `uv add 'sourceledger[parquet]'` and run commands with `uv run sourceledger`.
`build` writes `<id>.parquet` with zstd compression
and an explicit string schema, including the five provenance columns. Values such as `001`, `1.10`
and empty strings keep their spelling. `validate` and `read_output` read these tables with the same extra.
The Parquet extra uses PyArrow. Writing and validation process at most 10,000 rows per batch.
Validation decodes every column, including ordinary data columns, to detect unreadable pages.
Memory also depends on row width, file metadata and encoded column dictionaries; this is not a fixed byte cap.
JSON, Excel and custom readers still hold their input and parsed rows in memory.
`read_output` yields one row at a time, so a large table is never held in memory; wrap it in `list()` to hold it all.
`datapackage.json` records the format and media type;
`verify` checks the bytes without needing the extra.

The default is `output_format = "csv"`. `format = "file"` sources keep their original bytes in either mode.
When switching formats, move old outputs out of `data/processed/` before rebuilding; `validate` reports them as unclaimed files.
Library callers pass `output_format="parquet"` to both `build` and `validate`; `load_manifest` returns only source tables.

Each `[[source]]` table takes these keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | ASCII letters, digits, `_` and `-`. Names the raw file (`id` plus the URL's extension) and the output. |
| `name` | no | A title for people. The data package and `attribution` show it in place of the id. |
| `status` | yes | `"implemented"`, read by every stage, or `"unimplemented"`: a source you know about but don't read yet. `validate` fails if one of those has output. |
| `url` | for implemented sources | What `fetch` downloads. Change it and `build` refuses the old download until you fetch again. |
| `terms`, `terms_url` | for implemented sources | The publisher's reuse terms in their words, and where they are stated. |
| `license` | for implemented sources | The same terms as an [SPDX id](https://spdx.org/licenses/), such as `"CC-BY-4.0"`, or `"LicenseRef-<name>"` for terms with none. |
| `redistribute` | for implemented sources | `true` if the terms let you republish the raw file, `false` if not. |
| `vintage` | no | The edition you expect, such as `"2024 release"` or a TOML date. Recorded with each fetch and stamped on each row. `"not stated"` when absent. The old name `release` is refused. |
| `sha256` | no | The SHA-256 the download must have. Pin it for a fixed release, and `fetch` refuses bytes that changed upstream. |
| `max_bytes` | no | The largest download `fetch` accepts. 200 MiB unless set. A gzipped file or Excel workbook that unpacks past it is refused. |
| `timeout_s` | no | A positive finite number of seconds for one source's download, retries and waits included. 300 unless set. |
| `format` | no | `"file"` copies bytes unchanged. Table formats are `"csv"`, `"json"` and `"xlsx"`; see the details below. |
| `sheet` | no | The Excel sheet to read. The first unless set. A cell right of the last header cell is refused, and a missing cell in a row reads as `""`. |
| `headers` | no | Extra request headers, such as an API key, each value naming an environment variable: `{ Authorization = "env:EIA_TOKEN" }`. A literal value is refused, and so is a `url` that is not https. The record keeps the header names, not the values. A redirect to another scheme, host or port drops them. |
| `archive` | no | `true` to ask the Internet Archive's [Save Page Now](https://web.archive.org/save) to snapshot the URL after each fetch that brings new bytes. The record keeps the snapshot's address as `archive_url`. Only for http and https URLs, and not for a source that needs `headers`: the archive's request sends none. |
| `reason` | no | A note for people, such as why a source is unimplemented. sourceledger doesn't read it. |

Without `format`, the URL extension selects JSON (`.json`) or Excel (`.xlsx`); other extensions select CSV.
CSV and JSON can be gzipped; decompression stays within `max_bytes`.
JSON input must be an array of objects. Numbers keep their spelling: `1e5` stays `1e5`, and `1.10` stays `1.10`.
`format = "file"` keeps archives, rasters and other files unchanged, without unpacking or parsing them.

`validate` reports a missing `terms`, `terms_url`, `license` or `redistribute`. The other
keys are checked when the manifest loads. Keys not listed here are kept for your own reader.

For checks in an editor with a TOML language server (Tombi, Taplo), put this line at the
top of `sources.toml`. `init` writes it, with the version you have installed in place of
`v0.4.0`:

```toml
#:schema https://raw.githubusercontent.com/simonvanlierde/sourceledger/v0.4.0/src/sourceledger/sources.schema.json
```

The schema accepts exactly what sourceledger accepts. A test keeps the two in step.

## Other commands

- `init` writes a starter `sources.toml` in the current directory, or at `--manifest PATH`:
  the schema line and one example source, commented out. It won't overwrite an existing one.
- `record ID PATH` takes in a file that another tool, such as pooch, DVC or your own
  script, downloaded for source `ID`. It copies the file into `data/raw/`, checks the
  `sha256` pin, and writes the same log line and record as `fetch`, with `recorded_at` set
  to now. `--retrieved-at TIME` is when the file was downloaded, in ISO 8601 with a time
  zone (`2026-11-05T09:30:00Z`). Without it the record and the rows' `retrieved_at` stay
  empty rather than claim a download time. `--url URL` names where it came from if that
  is not the source's `url`; its query and fragment are dropped, since a signed download
  URL keeps its secret there. `--etag` and `--last-modified` keep the server's validators
  for a later conditional `fetch`, but only when the URL is the source's own.
  `--parameter NAME=VALUE`, repeatable, keeps request metadata such as a query's country
  code; it is published with the dataset, as text, so a name such as `token`, `x-api-key` or
  `sig` is refused ([safe use](secure-use.md) lists the rule). The file must be a regular
  file within the source's `max_bytes`. The library function is `record()`. The report and
  `datapackage.json` show the output as recorded, not fetched, and `status` shows the record
  time when there is no download time.
- `revisions OLD_LOG` lists the sources whose bytes changed since an earlier copy of the
  log. Copy the log before you fetch (`cp data/fetches.jsonl before.jsonl`), then run
  `sourceledger revisions before.jsonl`. A weekly CI run can use it to report a
  publisher's revision.
- `status` prints one line per source: its id, status and vintage, when its last logged
  version was fetched (or `never`), the start of its SHA-256, its licence, and whether
  the raw file may be republished. It reads the manifest and the log, never the network.
- `attribution` prints a Markdown table of the implemented sources, in manifest order:
  name, terms in the publisher's words, licence, terms URL, and whether the raw file may be
  republished. Paste it into a dataset's README or release notes.
- `report` writes a [standalone HTML provenance report](#html-provenance-report) to stdout.
  Save it with `sourceledger report > provenance.html`. It works offline and returns 0
  when it produces a report, including one that lists integrity problems.
- `verify DIR` is for someone who downloaded a published dataset. It reads
  `DIR/datapackage.json` and checks that each file it lists exists, with the size and
  SHA-256 it gives. It prints one line per problem and exits 1 if there are any. It needs
  no `sources.toml` and no network.

Commands exit 0 on success and 1 on a runtime failure or validation problem.
Invalid arguments exit 2. `revisions` exits 0 when sources changed: your downstream checks decide what a revision breaks.

`fetch`, `build` and `validate` take `--only ID`, as often as needed, to work on just those
sources. An id that is not in `sources.toml` is an error. `build --only` keeps the other
sources' entries in `datapackage.json` as the last build wrote them, and
`validate --only` skips the checks that cover the whole output.

Every command that reads the manifest reads `./sources.toml` unless given `--manifest PATH`.
The data folder stays `data/` in the current directory either way.

`status`, `validate` and `revisions` accept `--json`; see [JSON reports](#json-reports) for fields and exit codes.

## JSON reports

Use `--json` when a script needs to read a report:

```sh
sourceledger status --json
sourceledger validate --json --only co2
sourceledger revisions before.jsonl --json
```

These commands print one JSON object on stdout. They do not print the usual text report.
Every object has `stage` (the command name) and `errors` (a list of runtime error messages).
A successful run has an empty `errors` list. Existing warnings can still appear on stderr.
Other commands do not accept `--json`.

| Command | Result field | Meaning |
| --- | --- | --- |
| `status` | `sources` | One object per source, in manifest order. |
| `validate` | `problems` | The same list of problem messages the Python function returns. |
| `revisions` | `revisions` | One object per changed source, in current-log order. New sources are excluded, as in text output. |

### Source status

Each `sources` object has `source_id`, `status`, `vintage`, `retrieved_at`, `recorded_at`,
`sha256`, `license` and `redistribute`. Hashes have all 64 characters. Missing fetch information
is `null`; an absent licence or unknown redistribution flag is also `null`. `recorded_at` is
`null` for a fetched file and the record time for a file `record` took in; its
`retrieved_at` is then `""` when the other tool reported no download time.
`redistribute` is otherwise a JSON boolean. The `status` field is text, or `null` when absent.
The command reads no raw files and makes no network requests.

### Revisions

Each revision has `source_id`, `before_sha256` and `after`.
`before_sha256` is the earlier full hash. `after` is the complete latest fetch record,
with the same fields as a line in `data/fetches.jsonl`.
The two logs must exist. An empty `revisions` list means no existing source changed bytes.

### Problems, failures and exit codes

A validation run with no problems prints:

```json
{"stage": "validate", "errors": [], "problems": []}
```

Validation problems go in `problems` and cause exit code 1. Runtime failures, such as a
missing manifest or log, go in `errors` and cause exit code 1. A failed run omits its
result field. For example:

```json
{"stage": "status", "errors": ["sources.toml is missing"]}
```

Other successful reports exit 0. Finding revisions also exits 0: your downstream checks
decide whether a revision breaks the dataset. Invalid command arguments, such as a
missing `revisions` path, use argparse's text error on stderr and exit code 2.

These JSON structures are part of the CLI contract. Read fields by name; JSON object key
order and whitespace are not significant. The [stability policy](https://github.com/simonvanlierde/sourceledger/blob/main/README.md#stability) applies.

## HTML provenance report

After building a dataset, create a standalone HTML report for someone who receives it:

```sh
sourceledger report > provenance.html
```

Keep the report outside `data/processed/`: validation expects that folder to hold only
outputs described by the data package. Open `provenance.html` in a browser. It uses no
scripts, remote styles, fonts or network requests.

The command reads `sources.toml`, `data/fetches.jsonl` and the built package in
`data/processed/`. It checks that the listed outputs match the implemented manifest
sources and that their files match the package. It also flags a build that predates the
last logged revision. Each output links to its source details: URL, vintage, fetch
timestamp, raw and output hashes, licence and reuse terms. For a file that `record` took
in from another tool, the report gives the download time that tool reported, or "not
stated", and the time it was recorded, instead of a fetch time.

Source details come from the current manifest; the fetch stamps describe the last build.
If a file or its provenance is stale, the report lists the problem. The command returns 0
when it produces a report, including one with integrity problems. A fatal input error
returns 1 and writes no HTML. A missing fetch log produces a provenance problem in the report. Run `validate` to gate a pipeline on those problems.

The report includes no raw values or header names and values. Displayed HTTP URLs omit
user information, query parameters and fragments. URLs that are not HTTP or HTTPS are omitted.
Review free-text names and reuse terms before sharing: sourceledger cannot recognize
secrets someone wrote into those fields or into a URL path. Displayed metadata is escaped
so it cannot become executable HTML.

Passing integrity checks does not authenticate the package, establish scientific
correctness or grant publication permission. Recipients must obtain the package and
report from a source they trust, and review the source terms.

In Python, `sourceledger.report(sources, processed=folder, log=fetch_log)` returns the HTML
as a string. It reads the same files and does not write the report itself.
