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
It is a generator: a missing or unreadable file raises on the first `next()`, not at the call,
and an iterator left unfinished keeps the file open, which on Windows blocks replacing the output.
`datapackage.json` records the format and media type;
`verify` checks the bytes without needing the extra.

The default is `output_format = "csv"`. `format = "file"` sources keep their original bytes in either mode.
Every CSV row repeats the five provenance columns, so a CSV output of a narrow table is many times the size of
its raw file: a two-column table of 200,000 rows came out 14 times larger. Parquet stores those repeated values
once per column chunk; the same table was smaller than its raw file. Use Parquet for large tables.
When switching formats, move old outputs out of `data/processed/` before rebuilding; `validate` reports them as unclaimed files.
Library callers pass `output_format="parquet"` to both `build` and `validate`; `load_manifest` returns only source tables.

Set `release_formats` there too to have `release` write the optional metadata files:

```toml
release_formats = ["ro-crate", "croissant"]
```

Without it, `release` writes neither; see [release a dataset](#release-a-dataset).

`output_format`, `release_formats` and `[[source]]` are the only top-level keys. Any other, such as a misspelt
`[[sources]]`, is refused, with the intended name when one is close, so a typo never reads as
an empty manifest.
A source table may hold extra keys for your own reader.

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
| `archive` | no | `true` to ask the Internet Archive's [Save Page Now](https://web.archive.org/save) to snapshot the URL after each fetch that brings new bytes. The record keeps the snapshot's address as `archive_url`. Only for http and https URLs, and not for a source that needs `headers`: the archive's request sends none. `record` asks for no snapshot: it keeps the one logged for the same bytes, and otherwise leaves `archive_url` empty. |
| `reason` | no | A note for people, such as why a source is unimplemented. sourceledger doesn't read it. |

Without `format`, the URL extension selects JSON (`.json`) or Excel (`.xlsx`); other extensions select CSV.
CSV and JSON can be gzipped; decompression stays within `max_bytes`.
JSON input must be an array of objects. Numbers keep their spelling: `1e5` stays `1e5`, and `1.10` stays `1.10`.
`format = "file"` keeps archives, rasters and other files unchanged, without unpacking or parsing them.

`validate` reports a missing `terms`, `terms_url`, `license` or `redistribute`. The other
keys are checked when the manifest loads. Keys not listed here are kept for your own reader.

For checks in an editor with a TOML language server (Tombi, Taplo), put this line at the
top of `sources.toml`. `init` writes it, with the version you have installed in place of
`v0.5.0`:

```toml
#:schema https://raw.githubusercontent.com/simonvanlierde/sourceledger/v0.5.0/src/sourceledger/sources.schema.json
```

The schema accepts exactly what sourceledger accepts. A test keeps the two in step.

## Other commands

- `init` writes a starter `sources.toml` in the current directory, or at `--manifest PATH`:
  the schema line and one example source, commented out. It won't overwrite an existing one.
- `record ID PATH` takes in a file that another tool, such as pooch, DVC or your own
  script, downloaded for source `ID`. It copies the file into `data/raw/`, checks the
  `sha256` pin, and writes the same log line and record as `fetch`, with `recorded_at` set
  to now. `--retrieved-at TIME` is when the file was downloaded, in ISO 8601 with a time
  zone (`2026-11-05T09:30:00Z`); a value that is not ISO 8601 is a usage error. Without it the record and the rows' `retrieved_at` stay
  empty rather than claim a download time. `--url URL` names where it came from if that
  is not the source's `url`; its query and fragment are dropped, since a signed download
  URL keeps its secret there. `--etag` and `--last-modified` keep the server's validators
  for a later conditional `fetch`, but only when the URL is the source's own.
  `--parameter NAME=VALUE`, repeatable, keeps request metadata such as a query's country
  code; it is published with the dataset, as text, so a name such as `token`, `x-api-key` or
  `sig` is refused ([safe use](secure-use.md) lists the rule). The file must be a regular
  file within the source's `max_bytes`. The library function is `record()`. The report and
  `datapackage.json` show the output as recorded, not fetched, and `status` shows the record
  time when there is no download time. A later `fetch` of the same bytes logs a fetch over
  the record and prints `relogged` with a reminder to run `build`, since the outputs still
  name the record. `--dry-run` makes every check and hashes the file,
  then prints what it would record and writes nothing.
- `revisions OLD_LOG` lists the sources whose bytes changed since an earlier copy of the
  log. Copy the log before you fetch (`cp data/fetches.jsonl before.jsonl`), then run
  `sourceledger revisions before.jsonl`. It prints `no revisions` when no source changed.
  A weekly CI run can use it to report a publisher's revision.
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

## Options, output and exit codes

`sourceledger STAGE --help` lists the options of one stage; options go after the stage name.

| Option | Stages | Effect |
| --- | --- | --- |
| `--manifest PATH` | all but `revisions` and `verify` | Read this manifest instead of `./sources.toml`. The data folder stays `data/` in the current directory. |
| `--only ID` | `fetch`, `build`, `validate` | Work on just this source; repeat it for more. `build --only` keeps the other sources' entries in `datapackage.json` as the last build wrote them, and `validate --only` skips the checks that cover the whole output. |
| `--dry-run` | `fetch`, `record`, `build`, `release` | Say what would happen and write nothing: no download, no log line, no output. `build --dry-run` still checks each raw file against its fetch record, `record --dry-run` still checks and hashes the file, and `release --dry-run` still runs `validate`. |
| `--json` | `fetch`, `record`, `validate`, `status`, `revisions`, `verify`, `release` | Print one JSON object on stdout instead of text; see [JSON reports](#json-reports). |

Results go to stdout: fetch lines, outputs written, and the problems `validate` and
`verify` find, followed by their count. Errors and warnings go to stderr. An error is
one line in the form `error: <what went wrong>`, followed by `help: <what to do>` when
there is a known next step:

```text
error: sources.toml is missing
help: run `sourceledger init` to write one, or pass --manifest PATH
```

When stderr is a terminal and `--json` is off, `fetch` also prints `fetching ID (n of N)`
there before each source. The output has no colour. It is UTF-8 with LF line ends on
every system, also when redirected to a file on Windows.

| Exit code | Meaning |
| --- | --- |
| 0 | Done, with nothing to report. `revisions` exits 0 when sources changed: your downstream checks decide what a revision breaks. `report` exits 0 when it writes a report, even one that lists problems. |
| 1 | Problems found: `validate` or `verify` found at least one, or `release` refused its input or found a problem in the folder. |
| 2 | Usage error: an unknown stage or option, a missing argument, or an `--only` or `record` id that is not in `sources.toml`, or a `--retrieved-at` that is not an ISO 8601 time. Nothing is written. |
| 3 | Could not run: a missing or invalid file, a failed download, another fetch holding the lock, a validator `release` needs that is not installed or that stopped without a result, or an unexpected error such as a project's parser failing, printed as one `error:` line with its type. When some sources fail to fetch, the others are still written and logged. |
| 130 | Interrupted with Ctrl-C. What finished before is kept, and the `help:` line repeats the command to finish. With `--json` the report still prints, and `fetch` lists the sources fetched before. The command then ends by SIGINT, as Ctrl-C does, so a shell loop around it stops too; on Windows it exits 130. `main()` returns 130, so a project's own command calls `sourceledger.entry()` instead, which ends by SIGINT ([custom readers](recipes.md#write-a-custom-reader)). |

## JSON reports

Use `--json` when a script needs to read a report:

```sh
sourceledger status --json
sourceledger validate --json --only co2
sourceledger revisions before.jsonl --json
sourceledger fetch --json
```

These commands print one JSON object on stdout. They do not print the usual text report.
Every object has `schema_version` (now `1`; it goes up when a field changes meaning or
goes away), `stage` (the command name) and `errors`. Each error is an object with
`message` and `help`, the same two lines the text output prints; `help` is `null` when
there is no known next step. A successful run has an empty `errors` list. Warnings can
still appear on stderr. `init`, `build`, `attribution` and `report` do not accept `--json`.

| Command | Result field | Meaning |
| --- | --- | --- |
| `fetch` | `records` | One fetch record per source fetched, with the fields of a line in `data/fetches.jsonl` and `new`: `true` when its bytes differ from the last ones logged, and `relogged`: `true` when the same bytes were logged again because they came in another way (fetched after a record or under a new `url` or `vintage`, or recorded with other details), so the outputs need a `build`. A run where some sources failed lists the ones that worked. |
| `record` | `records` | The one record, as for `fetch`. |
| `status` | `sources` | One object per source, in manifest order. |
| `validate` | `problems` | The same list of problem messages the Python function returns. |
| `verify` | `problems` | The same list of problem messages the Python function returns. |
| `revisions` | `revisions` | One object per changed source, in current-log order. New sources are excluded, as in text output. |
| `release` | `files`, `excluded`, `formats`, `validators` | The paths in the folder, the ids left out, `"written"` or `"skipped"` for each optional format, and the `file`, `name` and `version` of each check that ran. A refused release has `problems` and `validators` instead. |
| `release --check` | `problems`, `validators` | The problems found, and each check that ran. |

With `--dry-run`, the object also has `"dry_run": true`. `fetch --dry-run` lists
`would_fetch`, one `{"source_id", "url"}` object per source, in place of `records`.
`record --dry-run` gives the record it would write.

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
{"schema_version": 1, "stage": "validate", "problems": [], "errors": []}
```

Validation problems go in `problems` and cause exit code 1. Runtime failures, such as a
missing manifest or log, go in `errors` and cause exit code 3. A failed run omits its
result field, except `fetch`, which lists the sources that worked. For example:

```json
{"schema_version": 1, "stage": "status", "errors": [{"message": "sources.toml is missing", "help": "run `sourceledger init` to write one, or pass --manifest PATH"}]}
```

An `--only` id that is not in `sources.toml` is also reported in `errors`, with exit
code 2. Other usage errors, such as a missing `revisions` path, are found before the
stage runs: they print the text error on stderr and exit 2. The
[exit code table](#options-output-and-exit-codes) holds the rest.

These JSON structures are part of the CLI contract. Read fields by name; JSON object key
order and whitespace are not significant. The [stability policy](https://github.com/simonvanlierde/sourceledger/blob/main/README.md#stability) applies.

## Release a dataset

`release OUT_DIR` writes the folder you publish, from a build that `validate` passes:

```sh
sourceledger release release-v1
sourceledger release --check release-v1
```

It runs `validate` first and stops with exit code 1 if it finds a problem. `OUT_DIR` must be
new or empty. The folder holds:

- `data/`: the output of each implemented source whose `redistribute` is `true`. The list
  comes from `sources.toml`, not from the files in `data/processed/`, so a source with
  `redistribute = false` is left out, whether it is a copied file or a table, and listed as
  excluded.
- `datapackage.json`: the one `build` wrote, without the excluded sources, each path under
  `data/`. `verify` checks the folder against it.
- `ro-crate-metadata.json` ([RO-Crate 1.3](https://w3id.org/ro/crate/1.3)) and
  `croissant.json` ([Croissant 1.1](https://docs.mlcommons.org/croissant/)), only when
  `release_formats` asks for them; otherwise each is reported as skipped. The RO-Crate lists
  each shipped file with its SHA-256, and every source with its URL, licence, terms and
  download time, an excluded one marked as such. The Croissant lists each file with the same
  SHA-256, and the columns of each table.
- `SHA256SUMS`: every other file, written last, in the format of `sha256sum -b`, so
  `sha256sum -c SHA256SUMS` checks the folder without sourceledger.

The dataset's name, description, authors, licence, version, identifier, `datePublished` and
`url` come from `codemeta.json` in the current directory, the project's CodeMeta file; `release`
does not write it. RO-Crate needs
`name`, `description`, `license` and `datePublished`, and Croissant `name`, `description`,
`license` and `url`. A missing one stops `release` with exit code 1, naming it.

After writing, `release` hashes every file in the folder, and refuses with exit code 1 when
one has the bytes of a restricted source's raw file or output, whatever its name. Then it runs
the same checks as `release --check DIR`, which checks a folder that exists:

| File | Check |
| --- | --- |
| `SHA256SUMS` | Every listed file is there with its SHA-256, and no file is unlisted. Built in. |
| `ro-crate-metadata.json` | `rocrate-validator validate`, profile `ro-crate-1.3`. It downloads the RO-Crate context, so it needs the network. |
| `croissant.json` | `mlcroissant validate --jsonld`. |

Each validator runs in its own process and every finding is a problem, with exit code 1. A
validator that stops without findings, such as roc-validator offline, or that runs past 10
minutes, exits 3: the folder was not checked. The
report names each check that ran, with its version. The two validators come with the release
extra: `uv tool install 'sourceledger[release]'`, or `uv add 'sourceledger[release]'` in a
project. When a metadata file is there but its validator is not, `release` exits 3 with the
install line. The folder is written beside `OUT_DIR` and moved into place only once every
check passes, so a refused release leaves nothing at `OUT_DIR`.

Above 100 files, `release` warns: Zenodo takes at most 100 files a record, so zip the folder
yourself. It does not upload, and it writes no `.zenodo.json`, DataCite fields, dataset card
or licence files. In Python, `release()` returns the fields of the `--json` report and raises
`ReleaseError`, a `ValueError`, for refused input and findings: `RestrictedSourceError` when a
restricted file reached the folder and `ValidatorFailed` when a validator reported findings.
`ValidatorMissing` and `ValidatorCouldNotRun` are `OSError`s. `check_release(DIR)` is
`release --check DIR`: it returns the problems and the checks that ran.

## HTML provenance report

After building a dataset, create a standalone HTML report for someone who receives it:

```sh
sourceledger report > provenance.html
```

Keep the report outside `data/processed/`: validation expects that folder to hold only
outputs described by the data package. Open `provenance.html` in a browser. It uses no
scripts, remote styles, fonts or network requests.
[See an example report](https://simonvanlierde.com/sourceledger/example-report.html), made
from the test suite's sample sources.

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
exits 3 and writes no HTML. A missing fetch log produces a provenance problem in the report. Run `validate` to gate a pipeline on those problems.

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
