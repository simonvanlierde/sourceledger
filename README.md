# sourceledger

[![python](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml/badge.svg)](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23460694.svg)](https://doi.org/10.5281/zenodo.23460694)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/simonvanlierde/sourceledger/badge)](https://scorecard.dev/viewer/?uri=github.com/simonvanlierde/sourceledger)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/simonvanlierde/sourceledger?utm_source=badge)

Build a dataset from published sources and keep a record of the downloads behind it.

sourceledger is a Python library and command for researchers who combine other people's
CSV, JSON, Excel or binary files. List the sources and reuse terms in `sources.toml`, then:

- `fetch` downloads the files and records their URLs, dates and SHA-256 checksums.
- `build` writes tables with provenance columns, or copies file sources unchanged.
- `validate` checks the outputs against the downloads and the manifest.

Each table row identifies its download. A `datapackage.json` describes the outputs.
The [documentation website](https://simonvanlierde.com/sourceledger/) follows a source through a publisher's revision.

## Install

```sh
uv tool install sourceledger
```

For a Python project that imports the library, use `uv add sourceledger` instead.
Use `sourceledger[xlsx]` in either command to read Excel files.
Python 3.12 or later is required; Linux and macOS are supported (POSIX).
This README follows `main`, including [unreleased features](CHANGELOG.md#unreleased).

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
URL's extension. Set `format = "file"` to keep a raster, archive or other file unchanged.
For another table format, see [custom readers](docs/readers.md).

## What to commit

Commit `sources.toml` and `data/fetches.jsonl`. The log holds checksums and URLs, not
data: it's the history of every version you have fetched.

Build output is a local working dataset. A file source is copied unchanged, including
when `redistribute = false`. Check [reuse terms before publication](docs/secure-use.md#check-redistribute-before-you-publish-raw-files).

Keep `data/raw/` and `data/processed/` out of git, so the repository never redistributes
a publisher's data. The `.fetch.json` records live in `data/raw/` too, so a fresh clone
runs `fetch` before `build` and `validate`.

## Documentation

- [Worked example](docs/index.md): notice a revised source and rebuild the dataset.
- [Reference](docs/reference.md): manifest keys, commands, selection and exit codes.
- [JSON reports](docs/reports.md): consume status, validation and revisions in CI.
- [How it works](docs/architecture.md) and [safe use](docs/secure-use.md): files, checks and limits.
- [Comparison](docs/comparison.md): where sourceledger fits alongside other tools.

Create a [standalone HTML provenance report](docs/provenance-report.md) with
`sourceledger report > provenance.html` for someone who receives your dataset.

See [what the checks establish](docs/assurance-case.md) for trust assumptions and limits.

### Verify a release

See [release verification](docs/releases.md) for signed provenance and rebuilding from a tag.

## sources.toml

See the [manifest reference](docs/reference.md#sourcestoml) for every key and editor checks.

## How fetch behaves

See [download limits and retries](docs/fetching.md).

## Other commands

See the [command reference](docs/reference.md#other-commands) for `init`, `status`, `attribution`,
`verify`, `revisions` and `report`. `status`, `validate` and `revisions` also accept `--json`.

## Your own readers

See [custom readers](docs/readers.md) to integrate another table format or use the Python API.

## Compared with

See the [comparison](docs/comparison.md) with pooch, DVC, DataLad and frictionless-py.

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
just check
```

[Contributing](.github/CONTRIBUTING.md) covers hooks, coding standards and tests.
[Governance](GOVERNANCE.md) explains project decisions. Report vulnerabilities through
[Security](.github/SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
