# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a breaking change comes only in a minor version (see Stability in the README).

## [Unreleased]

## [0.5.0] - 2026-11-10

Upgrade note for scripts that call the CLI: exit code 3 now means a stage could not run
(it was 1), errors on stderr read `error: ...` and `help: ...` instead of `STAGE: ...`,
`validate` and `verify` print their problems on stdout, and `--json` reports have a
`schema_version` and error objects. Options go after the stage name. `revisions` prints
`no revisions` instead of nothing when no source changed. In Python, `main()` also returns
3 and 130, where 0.4.0 returned only 0 or 1. See
[options, output and exit codes](docs/reference.md#options-output-and-exit-codes).

Upgrade note for Python callers and manifests: `read_output()` now yields rows instead of
returning a list, so wrap it in `list()` where the code indexes or counts them. `sources.toml`
refuses top-level keys other than `output_format`, `release_formats` and `[[source]]`. A dataset with a
recorded file needs 0.5.0 to build. Details under Changed.

### Added

- [`record`](docs/reference.md#other-commands), and `record()` in the library, take in a file that another tool such as pooch, DVC or your own script downloaded. It copies the file into `data/raw/`, checks the `sha256` pin, and writes the same log line and fetch record as `fetch`, with a `recorded_at` time and optional request `parameters`, stored as text in name order. The download time is the one the other tool reports, or empty. `datapackage.json` and the HTML report show these outputs as recorded, not fetched, and `status` shows the record time when there is no download time. A recorded URL other than the source's loses its query, and a parameter named like a credential (`api_key`, `apiKey`, `authKey`, `sessionid`, `sig`) is refused, since both are published with the dataset. Recording the same bytes again leaves the fetch record as logged; from another URL or download time it adds a log line, and `validate` reports a `datapackage.json` built before such a record. For a source with `archive = true`, `record` keeps the snapshot a fetch logged for the same bytes and asks for no new one. A dataset with a recorded file needs this version to build: 0.4.0 refuses its fetch records.
- `sourceledger STAGE --help` lists only that stage's options. `--dry-run` on `fetch`, `record` and `build` says what they would do and writes nothing. `fetch`, `record` and `verify` accept `--json`. On a terminal, `fetch` shows which source it is fetching.
- [Parquet outputs](docs/reference.md#sourcestoml) through `sourceledger[parquet]` and the project-wide `output_format = "parquet"` setting. Tables use zstd compression and an explicit string schema, with the same provenance columns and validation as CSV. `build` and `validate` accept `output_format=` in the library, and `read_output` reads either format.
  Writing and validation use batches of at most 10,000 rows through PyArrow, including full-column decoding to detect corruption.
- `load_output_format()` reads the project's `output_format`, for library callers to pass to `build` and `validate`. `validate` in the wrong format names the setting instead of asking you to delete the outputs.
- `entry()` runs the command for a project with its own reader, as `sourceledger` does: point the console script at a function that calls it with your `parse` and `prog` ([recipe](docs/recipes.md#write-a-custom-reader)). After Ctrl-C it ends by SIGINT, so a shell loop around it stops.
- [`release OUT_DIR`](docs/reference.md#release-a-dataset), and `release()` in the library, write the folder you publish from a build that validates: `data/` with the outputs of the sources whose `redistribute` is `true`, a `datapackage.json` without the others, and a `SHA256SUMS` in the format of `sha256sum -b`. With `release_formats = ["ro-crate", "croissant"]` in `sources.toml`, it also writes `ro-crate-metadata.json` (RO-Crate 1.3) and `croissant.json` (Croissant 1.1), with the dataset's name, authors, licence, version and date from `codemeta.json`; a format not asked for is reported as skipped. `load_release_formats()` reads the key, for library callers to pass to `release`. `release --check DIR` checks a folder: every file against `SHA256SUMS`, and each metadata file with its official validator, rocrate-validator or mlcroissant, from the new `sourceledger[release]` extra. `check_release()` is the same check in the library. The report names each check that ran and its version, and a refused release lists its findings under `problems` with exit code 1, as `--check` does. Without the extra, a check that is needed exits 3 with the install line; a validator that stops without findings, such as offline, or runs past 10 minutes also exits 3. Above 100 files, the Zenodo limit, it warns. `ReleaseError`, `RestrictedSourceError` and `ValidatorFailed` are `ValueError`s, and `ValidatorMissing` and `ValidatorCouldNotRun` `OSError`s, so code that catches the base classes still works.
- A [recipe](docs/recipes.md#follow-a-publishers-revision-across-two-releases) follows a publisher's revision across two PUDL releases: `revisions` flags the new file and `vintage` names the release and DOI, then [`compare.py`](docs/examples/pudl-revisions/compare.py) diffs the rows and carries the change into a direct operational CO2 estimate.

### Changed

- **Breaking:** `sources.toml` accepts only `output_format`, `release_formats` and `[[source]]` at the top level. A misspelt key such as `[[sources]]` is refused, with the intended key when one is close; it used to read as a manifest with no sources, so `build` wrote an empty `datapackage.json` over a good one. A project that kept its own table there, for its reader's settings, moves it into its own file or into each `[[source]]`, whose other keys stay allowed.
- **Breaking:** `read_output()` is a generator that yields one row at a time, CSV row by row and Parquet in batches of at most 10,000 rows, so reading a table of 1.5 million rows no longer holds them all in one list. Wrap the call in `list()` where the code indexes the rows, takes their length, or reads them twice. Every error, a missing file included, now raises on the first `next()`, and an unfinished iterator keeps the file open, which on Windows blocks replacing the output.
- Windows is supported and tested in CI. 0.4.0 said it was not.
- The code behind the public names moved from `sourceledger.ledger` into `manifest`, `ledger`, `fetcher`, `builder` and `report` modules. Import from `sourceledger` itself: the submodules may change in any release, as the [stability policy](README.md#stability) says.
- The CLI's stages, options, exit codes and `--json` reports are part of the public API in the [stability policy](README.md#stability).
- The HTML report puts the integrity result in a box at the top, lists the outputs with links under one heading, and names hashes and times in plain words ("SHA-256 of the downloaded file", "Served from"). It follows the reader's dark mode, prints without the in-page links, wraps long ids, and shows a recorded file's request parameters. A script that reads the report's HTML needs updating.
- The CLI help ends with a first run, the exit codes and a link to the docs, and `--version` prints the installed version, after the project's name when a project calls `main(prog=...)`. Ctrl-C ends a stage with an error, the command to finish it and exit code 130 instead of a traceback, and the command ends by SIGINT so a shell loop around it stops (on Windows it exits 130); under `--json` the report still prints, with the sources fetched so far. `init` says what to do next, and a count of one reads "1 problem" or "1 row".
- An unexpected error, such as a project's parser raising `KeyError`, prints one `error:` line with its type and exits 3 instead of a traceback. A TOML syntax error names the manifest and the line to fix. A missing `record`, `revisions` or `verify` path says what to pass. A `--retrieved-at` that is not ISO 8601 is a usage error (exit 2).
- CLI output is UTF-8 with LF line ends on every system, so `report > provenance.html` on Windows matches the encoding the HTML declares. Paths in the CLI's own lines use `/`.
- Stages that take no operand refuse one: `build foo` exits 2 instead of ignoring `foo`.
- **Breaking:** the CLI exits 0 when done, 1 when `validate` or `verify` finds problems, 2 for a usage error, and 3 when a stage could not run, such as a missing file, a failed download or a held lock. Before, a stage that could not run exited 1, as one that found problems does. An `--only` or `record` id that is not in `sources.toml` is a usage error. Errors print as `error: <what>`, with a `help:` line when there is a next step. `validate` and `verify` print their problems and the count on stdout, not stderr.
- **Breaking:** every [JSON report](docs/reference.md#json-reports) has `"schema_version": 1`, and each entry in `errors` is an object with `message` and `help` instead of a string. `--json` must follow the stage name.
- The website has [recipes](docs/recipes.md) for recording files from pooch, DVC, dlt and your own fetcher, and a [where it fits](docs/where-it-fits.md) page in place of the tool comparison. JSON and HTML reports moved into the [reference](docs/reference.md#json-reports), and release verification into [safe use](docs/secure-use.md#check-a-sourceledger-release); the old addresses redirect.
- The CLI reports build row counts without rereading the outputs. Fetch history is read in small batches, once per CLI fetch, retaining only each source's latest record.
- Builds of CSV sources with the default reader take about a third less time to CSV and about half as long to Parquet. Rows pass through as lists instead of dictionaries, and `validate` checks a CSV output's rows the same way, about a fifth faster. Output bytes and error messages stay the same.
- Builds stream gzipped CSV inputs with the default reader, while enforcing the decompressed size limit. JSON parsing releases each decoded object as it converts the rows, reducing peak memory. Output bytes and parser errors stay the same.

### Fixed

- `output_format` inside a `[[source]]` table, where a line appended to `sources.toml` lands, is refused. Before, it was ignored and `build` wrote CSV.
- `sourceledger status | head` and other early-closing readers end the command quietly instead of with a `BrokenPipeError` traceback.
- `fetch` and `record` print `relogged`, and `relogged: true` under `--json`, when the same bytes come in another way, such as a fetch after a `record`, and say to run `build`. Before, they printed `unchanged` while the outputs went stale, and `validate` then failed.
- `fetch --json` and `record --json` give the fields of the log line, as the reference says, without an empty `recorded_at` and `parameters` for a fetched file.
- `fetch` and `record` remove the `.part` temp files that a killed run left in `data/raw/`. Before, each download or record cut short by a kill stayed on disk.
- `fetch` and `record` take the `data/raw/.lock` lock on Windows too, so a second run is refused there as on Linux and macOS. The test suite and CI now run on Windows.
- The HTML report said "Local or non-HTTP URL omitted" for an empty final or terms URL, and "None" for an unknown redistribution flag. It now says "the URL above", "not stated in sources.toml", and "yes" or "no".
- HTTPS downloads keep the SSL context on their own handler instead of reading a private standard-library attribute.
- A `fetch` that downloads the same bytes again in full, as from a `file://` URL or a server that sends no ETag, keeps the logged download time and final URL in the fetch record. Before, the record took the new time, the next `build` stamped it, and the HTML report said the outputs were built from an earlier fetch while `validate` passed.
- A changed `url` or `vintage` that brings the same bytes adds a log line and prints `relogged`. Before, the log kept the old line while the fetch record took the new values, so the log disagreed with the record that `build` and `validate` read. 0.4.0 did the same.

### Security

- `release` never ships the data of a source with `redistribute = false`: it takes the file list from `sources.toml`, not from `data/processed/`, and after writing it hashes every file in the folder and refuses one that has the bytes of a restricted raw file or output, whatever its name. Before, `build` copied such files into `data/processed/` and only the docs said to leave them out of a release.
- The release workflow publishes only a tag signed with a key listed in `.github/allowed_signers` on `main`. Before, it published from any pushed `v*` tag, signed or not. See [checking a release](docs/secure-use.md#check-a-sourceledger-release).
- `fetch` keeps a redirect's final URL without its query and fragment, which can hold a signature, as signed download links from cloud storage do. Before, the full URL went into `fetches.jsonl`, the fetch record and `datapackage.json`.
- A redirect from a public host to a non-HTTP URL, such as `ftp://` on an internal address, is refused. Before, urllib followed it past the check that refuses inward redirects.
- `sources.toml` text with a control character other than a tab or line break, such as the ESC that starts a terminal escape sequence, is refused. Before, an ESC in a `url` reached the terminal raw in `fetch` and could recolour or rewrite the screen. The check covers nested tables, arrays and keys too, such as a `[source.headers]` name.
- A `url` in `sources.toml` that holds a user name or password is refused. Before, it was copied into `fetches.jsonl`, the fetch record, `datapackage.json` and `ro-crate-metadata.json`, which go out with the dataset.
- `release --check` and `verify` show a control character in a received folder's file names, its `SHA256SUMS` or a validator's output as an escape such as `\x1b`, so a crafted name cannot recolour or rewrite the terminal.
- `release --check` reads `SHA256SUMS` only when it is a regular file of at most 64 MiB. Before, a named pipe there made the check hang, and a link to `/dev/zero` filled memory.

## [0.4.0] - 2026-11-01

Move manually added files out of `data/processed/` before running `validate`.

### Added

- `report` creates a [standalone HTML provenance report](docs/reference.md#html-provenance-report) that recipients can read offline. It links outputs to their sources and lists integrity problems without including raw data.
- An [assurance case](docs/assurance-case.md) explains the trust assumptions, safeguards and limits behind integrity and provenance checks.
- `status`, `validate` and `revisions` accept `--json` for scripts and CI. See [JSON reports](docs/reference.md#json-reports).
- Per-source `timeout_s` sets the fetch budget, including retries and waits. The default
  remains 300 seconds; see [download limits and retries](docs/fetching.md).
- `format = "file"` keeps files unchanged and validates their bytes, provenance and
  reuse metadata. Build describes them as non-tabular resources in `datapackage.json`;
  see [How build works](docs/architecture.md#build).

### Changed

- The [documentation website](https://simonvanlierde.com/sourceledger/) uses a navy and lime reading layout, responsive navigation and copyable code. A connected provenance diagram and revision comparison use an example checked against the pipeline. The [full tutorial](docs/getting-started.md) has its own page.

- Documentation separates the quick start from the [manifest and command reference](docs/reference.md), custom readers and release verification.

- **Breaking:** `validate` reports unclaimed non-CSV files in `data/processed/`, alongside
  unclaimed CSV files. See [validation](docs/architecture.md#validate).

### Fixed

- [JSON status reports](docs/reference.md#source-status) render non-text status values as text instead of crashing on TOML dates and times.

- [File validation](docs/architecture.md#validate) checks each output hash once while preserving the checks of its provenance and package metadata.

- The [HTML provenance report](docs/reference.md#html-provenance-report) flags missing, duplicate or no-longer-implemented outputs instead of reporting successful checks for an incomplete package.

- The citation helper removes old Zenodo version, date and DOI overrides so each release can receive its own archive metadata. See [release verification](docs/secure-use.md#check-a-sourceledger-release).

- A malformed resource name in `datapackage.json` produces [validation problems](docs/reference.md#problems-failures-and-exit-codes) instead of an uncaught error.

- Command options can appear between the stage and its path on Python 3.12, including
  `revisions --json OLD_LOG`. See the [command reference](docs/reference.md#other-commands).

### Security

- Fetch follows redirects without reading their unused response bodies, avoiding an
  unbounded read. See [Use sourceledger safely](docs/secure-use.md#use-https).

## [0.3.1] - 2026-10-27

### Added

- The README shows how to rebuild a release from its tag and get the same wheel and sdist
  as on PyPI, byte for byte.

### Changed

- `init` writes a `#:schema` line that names the schema at your installed version's tag,
  not the one on main. An editor then checks the manifest against the rules your version
  applies, not against keys a later release adds or refuses. Update the line when you
  upgrade. The schema's `$id`, and SchemaStore's entry, stay on main.
- Each GitHub release now carries the wheel, the sdist and their signed provenance
  bundle (`sourceledger-<version>.sigstore.json`) as assets. The maintainer pushes a tag,
  and the release workflow creates the release.

## [0.3.0] - 2026-10-27

Upgrade steps:

- Rename `release` to `vintage` in `sources.toml`. The old key is now refused, with a
  message naming the new one (see Removed below).
- A source with `headers` needs an https `url` now (see Security below). Switch an
  `http://` URL to `https://`, which most APIs serve.
- Run `build` again for JSON sources: numbers are now written as they appear in the file,
  so such an output can change (see Changed below).

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
- JSON sources keep numbers as written: `1.10` stays `1.10` and `1e5` stays `1e5`, at the
  top level and inside nested values. Before, they were read as numbers and written back
  as `1.1` and `100000.0`. An output built from such a file changes on the next `build`.
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
- A fetch log whose last line is a whole record without its final newline, as an editor
  or a merge can leave it, keeps that record. Before, the next fetch took it for a line
  cut off by an interrupted fetch and deleted it from the history. Appending now reads
  only the end of the log, not all of it.
- A source's last record in the fetch log, or its `.fetch.json`, with a missing field or a
  field of the wrong type (a SHA-256 that is not 64 hex digits, a size that is not a whole
  number), is now reported as not a fetch record. Before, `fetch`, `build`, `validate` and `revisions`
  could stop with a traceback, and `fetch` skipped the sources after it. `fetch` now
  downloads such a source again, and `validate` lists it as a problem.
- `revisions` fails when the current log does not exist, as it already did for the
  earlier copy. Run from the wrong directory, it used to report no revisions.
- `fetch` and `build` called from Python with a `sha256` pin in upper case, bypassing
  `load_manifest`, now treat it the same way: both compare it as written.
- A cut-off or damaged gzip, a file that is not an Excel workbook, and JSON nested too
  deeply to read now fail `build` and `validate` with one line that names the source,
  not a traceback.
- An Excel sheet with a value right of its header is refused, as a CSV row with more
  fields than its header is. Before, it became a column with an empty name, and two such
  values gave a misleading "appear more than once" error. Empty header cells at the end
  of the first row are no longer read as columns.

### Security

- **Breaking:** `headers` on a source whose `url` is not https is a manifest error, since
  the API key would cross the network in clear text.
- A redirect that changes the scheme, such as https to http on the same host, now drops
  the `headers` too, as a redirect to another host already did. Before, the key went out
  in clear text after the downgrade.
- A redirect from a public host is now checked against the address the connection
  actually reached, not a separate lookup before it. A name that resolved to a public
  address for the check and to an internal one for the connection could get past it.
  Through a proxy the check is skipped, since the proxy resolves the name.
- The 300 s limit on one source's fetch now holds against a server that sends a few
  bytes at a time. Each read used to wait for a full 1 MiB, so a slow trickle kept a
  fetch going far past the limit. It now stops within one 30 s socket timeout of it.
- `fetch` no longer follows a symlink at `data/raw/.lock` or `data/fetches.jsonl`. A
  checkout could plant one, and taking the lock emptied the file it pointed at. The lock
  file is never truncated now, and a symlinked log fails the fetch.
- An Excel workbook is refused when it unzips past `max_bytes`, as a gzipped file
  already was. A small workbook could otherwise expand to more memory than the machine
  has.

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

[Unreleased]: https://github.com/simonvanlierde/sourceledger/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/simonvanlierde/sourceledger/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/simonvanlierde/sourceledger/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/simonvanlierde/sourceledger/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/simonvanlierde/sourceledger/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/simonvanlierde/sourceledger/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/simonvanlierde/sourceledger/releases/tag/v0.1.0
