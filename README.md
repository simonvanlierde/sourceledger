# sourceledger

[![python](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml/badge.svg)](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23460694.svg)](https://doi.org/10.5281/zenodo.23460694)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/simonvanlierde/sourceledger/badge)](https://scorecard.dev/viewer/?uri=github.com/simonvanlierde/sourceledger)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/16545/badge)](https://www.bestpractices.dev/projects/16545)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/simonvanlierde/sourceledger?utm_source=badge)

Build a dataset from published sources and keep a record of the downloads behind it.

sourceledger is a Python library and command for researchers who combine other people's
CSV, JSON, Excel or binary files. List the sources and reuse terms in `sources.toml`, then:

- `fetch` downloads the files and records their URLs, dates and SHA-256 checksums.
- `build` writes tables with provenance columns, or copies file sources unchanged.
- `validate` checks the outputs against the downloads and the manifest.
- `release` writes the folder to publish, without the sources whose terms forbid it, and
  checks it.

Each table row identifies its download. A `datapackage.json` describes the outputs.
The [documentation website](https://simonvanlierde.com/sourceledger/) follows a source through a publisher's revision.

## Install

```sh
uv tool install sourceledger
```

For a Python project that imports the library, use `uv add sourceledger` instead.
Use `sourceledger[xlsx]` in either command to read Excel files, `sourceledger[parquet]`
to write Parquet outputs, or `sourceledger[release]` to check RO-Crate and Croissant metadata.
Python 3.12 or later is required; Linux, macOS and Windows are supported.
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

`sourceledger fetch --help` lists one stage's options, and `--dry-run` on `fetch`, `record`
and `build` shows what they would write. Exit code 1 means `validate` or `release` found problems, 2 a
usage error and 3 that a stage could not run; see [options and exit codes](docs/reference.md#options-output-and-exit-codes).

For [Parquet outputs](docs/reference.md#sourcestoml), set `output_format = "parquet"`
before the first `[[source]]` table in `sources.toml`, after installing the Parquet extra above.
CSV remains the default. Parquet writing and validation use batches of at most 10,000 rows;
see [table memory limits](docs/reference.md#sourcestoml).

`sourceledger` reads CSV (gzipped or not), JSON and Excel, by each source's `format` or its
URL's extension. Set `format = "file"` to keep a raster, archive or other file unchanged.
For another table format, see [custom readers](docs/recipes.md#write-a-custom-reader).
If another tool downloads a source, [record its file](docs/recipes.md#record-a-file-another-tool-downloaded)
instead of fetching it.

### What to commit

Commit `sources.toml` and `data/fetches.jsonl`. The log holds checksums and URLs, not
data: it's the history of every version you have fetched.

Build output is a local working dataset. A file source is copied unchanged, including
when `redistribute = false`. Check [reuse terms before publication](docs/secure-use.md#check-redistribute-before-you-publish-raw-files).

Keep `data/raw/` and `data/processed/` out of git, so the repository never redistributes
a publisher's data. The `.fetch.json` records live in `data/raw/` too, so a fresh clone
runs `fetch` before `build` and `validate`.

## Documentation

- **Get started:** follow the [worked example](docs/getting-started.md) to notice a revised
  source and rebuild the dataset.
- **Configure and run:** use the [manifest and command reference](docs/reference.md),
  [download limits and retries](docs/fetching.md), and [recipes](docs/recipes.md) for
  pooch, DVC, dlt, your own fetcher and custom readers.
- **Inspect and share:** consume [JSON reports](docs/reference.md#json-reports) in CI, or create a
  [standalone HTML provenance report](docs/reference.md#html-provenance-report) for dataset recipients
  with `sourceledger report > provenance.html`.
- **Understand the checks:** read [how it works](docs/architecture.md),
  [safe use](docs/secure-use.md), and [what the checks establish](docs/assurance-case.md).
- **Choose and verify:** see [where sourceledger fits](docs/where-it-fits.md) among related
  tools, or check a sourceledger release's
  [signed provenance and reproducible build](docs/secure-use.md#check-a-sourceledger-release).

## Project status

### Stability

Until 1.0, the public API is `sourceledger`'s `__all__`, the keys of `sources.toml`, the
format of fetch records and the log, and the CLI's stages, options, exit codes and `--json`
reports. A breaking change to any of them comes only in a
minor version, listed in [CHANGELOG.md](CHANGELOG.md), with a warning first where that's
cheap. Older fetch logs stay readable. A record written by `record()`, or with
`parameters`, is not readable by 0.4.0: after a downgrade, run `fetch` again.

### Roadmap

- An error class per kind of failure in the other stages, under `ValueError` and `OSError`,
  once a caller needs to tell them apart. This will not break code that catches the base classes.
- Comparing two versions of a table row by row, with the row keys the caller names, so a
  revision shows which values changed and not only that the bytes did. It starts as a pilot on
  real data, the archived World Development Indicators releases, before it becomes an API.
- A review by [pyOpenSci](https://www.pyopensci.org) once the project has users besides
  its own template.

Not planned:

- Templated, paginated or parallel downloads, an `update` command, or a plugin per download
  tool. Other tools fetch those files, and `record` logs them
  ([0005](docs/decisions/0005-provenance-core.md)).

Ideas and requests are welcome as [issues](https://github.com/simonvanlierde/sourceledger/issues).

## Social card

[`site/public/og.png`](site/public/og.png) is the 1200x630 card shown when the link is shared. It is made with the generator in
[my site repo](https://github.com/simonvanlierde/simonvanlierde.github.io/tree/main/scripts/social-card)
(needs Docker). The visual is a screenshot of the live page at phone width, cropped. Astro serves `site/public/`, and `site/src/layouts/Base.astro` holds the tags. From a checkout of
that repo, with this repo next to it:

```sh
pnpm gen:card --visual ../sourceledger/site/og-visual.jpg --name "sourceledger" \
  --line "Fetch or record a dataset's public files with provenance." \
  --domain simonvanlierde.com/sourceledger --out ../sourceledger/site/public/og.png
```

## Development

You need [uv](https://docs.astral.sh/uv/), [just](https://just.systems), and Node 26 with
[pnpm](https://pnpm.io) for the documentation site, which `just check` also builds.

```sh
uv sync
just check
```

The site is built with Astro from the Markdown in `docs/`. `just site` builds it into
`site/dist/`, and every push to `main` deploys it to GitHub Pages at
<https://simonvanlierde.com/sourceledger/>.

[Contributing](.github/CONTRIBUTING.md) covers hooks, coding standards and tests.
[Governance](GOVERNANCE.md) explains project decisions. Report vulnerabilities through
[Security](.github/SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
