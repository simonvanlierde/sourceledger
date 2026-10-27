# sourceledger

[![python](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml/badge.svg)](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23460694.svg)](https://doi.org/10.5281/zenodo.23460694)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/simonvanlierde/sourceledger/badge)](https://scorecard.dev/viewer/?uri=github.com/simonvanlierde/sourceledger)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/simonvanlierde/sourceledger?utm_source=badge)

Build a dataset from public sources where every row says which download it came from.

A project lists its inputs in `sources.toml`: each source's URL, the publisher's reuse
terms, and whether the raw file may be republished. sourceledger runs three stages over it.
[How it works](docs/architecture.md) has the detail of each.

- `fetch` downloads each source into `data/raw/`, with a record beside it: the URL, the
  time, the SHA-256 and size, the vintage, and the server's ETag and Last-Modified. An
  unchanged source isn't downloaded again. Each new version is added to
  `data/fetches.jsonl`.
- `build` writes `data/processed/<id>.csv` with five provenance columns in front, and a
  [Frictionless Data Package](https://datapackage.org), `datapackage.json`, that describes
  every output. The same records always give the same files.
- `validate` re-reads the outputs and lists every problem it finds, such as an output from
  an older fetch, an edited raw file, or a source without reuse terms.

`init`, `status`, `attribution`, `verify` and `revisions` are commands around them: see
[Other commands](#other-commands).

There are no dependencies. Excel files need the optional `xlsx` extra. The
[docs page](https://simonvanlierde.github.io/sourceledger/) walks through a fetch and a
publisher's silent revision.

## Install

```sh
uv add sourceledger            # or 'sourceledger[xlsx]' to read Excel files
```

It needs Python 3.12 or later, and runs on Linux and macOS only (POSIX).

### Verify a release

Each release is built and published by the
[release workflow](.github/workflows/python-release.yml), with no API token. PyPI keeps a
signed record of the workflow run that built each file. To check a wheel you downloaded,
pass its path:

```sh
uvx pypi-attestations verify pypi --repository https://github.com/simonvanlierde/sourceledger \
  sourceledger-0.2.0-py3-none-any.whl
```

From 0.2.0, GitHub also keeps a signed build-provenance attestation for each file:

```sh
gh attestation verify sourceledger-0.2.0-py3-none-any.whl --repo simonvanlierde/sourceledger
```

Both fail unless the file is byte for byte the one this repository's workflow built.

## Quick start

Write a `sources.toml` in the project root (`sourceledger init` writes a starter):

```toml
[[source]]
id = "co2"
status = "implemented"
url = "https://raw.githubusercontent.com/datasets/co2-ppm/main/data/co2-annmean-mlo.csv"
vintage = "main branch, unversioned"
terms = "ODC-PDDL-1.0 (datasets/co2-ppm package; original data from NOAA GML)"
terms_url = "https://opendatacommons.org/licenses/pddl/"
license = "PDDL-1.0"
redistribute = true
```

Then run the stages from that directory:

```sh
sourceledger fetch
sourceledger build
sourceledger validate
```

`sourceledger` reads CSV (gzipped or not), JSON and Excel, by each source's `format` or its
URL's extension. For other formats, see [Your own readers](#your-own-readers).

## What to commit

Commit `sources.toml` and `data/fetches.jsonl`. The log holds checksums and URLs, not
data: it's the history of every version you have fetched.

Keep `data/raw/` and `data/processed/` out of git, so the repository never redistributes
a publisher's data. The `.fetch.json` records live in `data/raw/` too, so a fresh clone
runs `fetch` before `build` and `validate`.

## sources.toml

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
| `format` | no | `"csv"`, `"json"` (an array of objects) or `"xlsx"`. Otherwise from the URL's extension: `.json`, `.xlsx`, and CSV for anything else. A gzipped CSV or JSON file is decompressed, within `max_bytes`. JSON numbers are written as they appear in the file (`1e5` stays `1e5`, `1.10` stays `1.10`). |
| `sheet` | no | The Excel sheet to read. The first unless set. A cell right of the last header cell is refused, and a missing cell in a row reads as `""`. |
| `headers` | no | Extra request headers, such as an API key, each value naming an environment variable: `{ Authorization = "env:EIA_TOKEN" }`. A literal value is refused, and so is a `url` that is not https. The record keeps the header names, not the values. A redirect to another scheme, host or port drops them. |
| `archive` | no | `true` to ask the Internet Archive's [Save Page Now](https://web.archive.org/save) to snapshot the URL after each fetch that brings new bytes. The record keeps the snapshot's address as `archive_url`. Only for http and https URLs, and not for a source that needs `headers`: the archive's request sends none. |
| `reason` | no | A note for people, such as why a source is unimplemented. sourceledger doesn't read it. |

`validate` reports a missing `terms`, `terms_url`, `license` or `redistribute`. The other
keys are checked when the manifest loads. Keys not listed here are kept for your own reader.

For checks in an editor with a TOML language server (Tombi, Taplo), put this line at the
top of `sources.toml`:

```toml
#:schema https://raw.githubusercontent.com/simonvanlierde/sourceledger/main/src/sourceledger/sources.schema.json
```

The schema accepts exactly what sourceledger accepts. A test keeps the two in step.

## How fetch behaves

- It tries four times, waiting 5, 10 and 20 s between attempts, on 429, 500, 502, 503,
  504, network errors and a body cut short. A `Retry-After` header, in seconds or as a
  date, raises the wait to at most 120 s. Any other error status fails at once.
- Each connect and read times out after 30 s, and one source's whole fetch after 300 s. The
  300 s holds against a server that sends bytes slowly, give or take one 30 s socket timeout.
- Every source is tried. When some fail, the others are still written, and the run
  exits 1 with one line per failed source.
- A second fetch in the same project, while one runs, exits at once (a lock on
  `data/raw/.lock`).
- Each line it prints says `new` or `unchanged`, against the last version in the log.
- For a source with `archive = true`, the Internet Archive request times out after 120 s
  per connect and read. When it fails, fetch prints a warning, leaves `archive_url` empty,
  and still succeeds. Save Page Now answers only once the snapshot is taken (49 s for a
  GitHub page on 2026-10-20), and it captures one URL at most 5 times a day across all its
  users. Past that it answers HTTP 429 with no `Retry-After`, and that fetch records no
  snapshot.

The checks on redirects, compression and keys are in [Use sourceledger
safely](docs/secure-use.md). [How it works](docs/architecture.md#fetch) has the steps and
the crash safety.

## Other commands

- `init` writes a starter `sources.toml` in the current directory: the schema line and one
  example source, commented out. It won't overwrite an existing one.
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
- `verify DIR` is for someone who downloaded a published dataset. It reads
  `DIR/datapackage.json` and checks that each file it lists exists, with the size and
  SHA-256 it gives. It prints one line per problem and exits 1 if there are any. It needs
  no `sources.toml` and no network.

Every command exits 1 on a failure or a problem. `revisions` exits 0 when sources changed,
by design: a test decides what a revision breaks.

`fetch`, `build` and `validate` take `--only ID`, as often as needed, to work on just those
sources. An id that is not in `sources.toml` is an error. `build --only` keeps the other
sources' entries in `datapackage.json` as the last build wrote them, and
`validate --only` skips the checks that cover the whole output.

Every command reads `./sources.toml` unless given `--manifest PATH`. The data folder stays
`data/` in the current directory either way.

## Your own readers

`sourceledger` reads every source with `parse_source`, which hands it to `parse_csv`,
`parse_json` or `parse_xlsx`. For another format, a project writes its own reader, a
function from a source's manifest table and raw bytes to columns and rows, and its own
command:

```python
# src/my_dataset/pipeline.py
import sourceledger
from sourceledger import Source


def parse(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Branch on source["id"] for a source in a format sourceledger doesn't read."""
    return sourceledger.parse_source(source, payload)


def main(argv: list[str] | None = None) -> int:
    return sourceledger.main(argv, parse=parse, prog="my-dataset", user_agent="my-dataset")
```

```toml
# pyproject.toml
[project.scripts]
my-dataset = "my_dataset.pipeline:main"
```

`my-dataset fetch`, `build` and `validate` then use that reader, and publishers see
`my-dataset` as the User-Agent. A project that ships its `sources.toml` inside its package
passes the path as `manifest=Path(__file__).with_name("sources.toml")`. The
[project-templates](https://github.com/simonvanlierde/project-templates) `data-pipeline`
kind sets this up, with CI and a weekly revision report. The stages are also
functions: `load_manifest`, `fetch`, `build`, `validate`, `revisions`, `status`,
`attribution` and `verify`. `verified(source)` returns a source's fetch record and raw
bytes after checking that both are current, for a reader that needs them.

## Compared with

Other tools cover parts of this. Use one of them when that part is all you need.

| Tool | What it does well | What sourceledger adds |
| --- | --- | --- |
| [pooch](https://www.fatiando.org/pooch/) | Downloads files with a known hash, caches them, retries. Made for packages that ship sample data. | A record of every fetch on disk, conditional re-fetches, a history of versions, provenance in each output row, and reuse terms checked per source. |
| [DVC](https://dvc.org) `import-url` | Tracks a downloaded file's URL with its checksum or ETag, and `dvc update` notices a change. The closest overlap. | No cache or `.dvc` files to adopt, reuse terms per source, provenance in each output row, and a Data Package for the outputs. |
| [DataLad](https://www.datalad.org) `download-url` | Records where every file came from in git and git-annex, as part of full dataset version control. | A plain manifest and one committed log, no git-annex, and reuse terms per source. |
| [frictionless-py](https://framework.frictionlessdata.io) | Describes and validates Data Packages, with licences, sources and hashes per resource. | The fetch, verify and revision steps. `build` writes a Data Package, so the two work together. |

Checked against each project's code and docs on 2026-10-18.

## Stability

Until 1.0, the public API is `sourceledger`'s `__all__`, the keys of `sources.toml`, and
the format of fetch records and the log. A breaking change to any of them comes only in a
minor version, listed in [CHANGELOG.md](CHANGELOG.md), with a warning first where that's
cheap. Older fetch logs stay readable.

## Roadmap

- A `release` stage that writes the metadata for a dataset release (RO-Crate, Croissant,
  CodeMeta, Zenodo and a `SHA256SUMS` file), checks each with its official validator, and
  keeps the data of `redistribute = false` sources out of the release.
- An error class per kind of failure, under `ValueError` and `OSError`, once the release
  stage needs to tell them apart. This will not break code that catches the base classes.
- A review by [pyOpenSci](https://www.pyopensci.org) once the project has users besides
  its own template.

Ideas and requests are welcome as [issues](https://github.com/simonvanlierde/sourceledger/issues).

## Development

```sh
uv sync
uv tool install prek   # once per machine
prek install           # git hooks
just check             # lint, types and tests, as CI runs them
```

[CONTRIBUTING.md](.github/CONTRIBUTING.md) has the coding standard, the test policy and
how dependencies are chosen. [GOVERNANCE.md](GOVERNANCE.md) says who decides what.
[docs/architecture.md](docs/architecture.md) explains how the stages fit together, and
[docs/secure-use.md](docs/secure-use.md) what sourceledger checks for you. Report a
vulnerability as [SECURITY.md](.github/SECURITY.md) describes.

The tests never touch the network, except those marked `live`. The mutation-testing run
(`just py-mutation`, about 2 minutes) and its baseline are explained in
[CONTRIBUTING.md](.github/CONTRIBUTING.md#tests).

## License

MIT. See [LICENSE](LICENSE).
