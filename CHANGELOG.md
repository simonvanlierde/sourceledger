# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
a breaking change comes only in a minor version (see Stability in the README).

## [Unreleased]

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

[Unreleased]: https://github.com/simonvanlierde/sourceledger/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/simonvanlierde/sourceledger/releases/tag/v0.1.0
