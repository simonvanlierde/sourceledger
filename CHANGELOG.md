# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `fetch`, `build`, `validate` and `revisions` over a `sources.toml` manifest, with a
  fetch record per download, conditional re-fetches, retries with a capped Retry-After,
  a size cap, an optional pinned `sha256`, and a committed log of every new version.
  `build` takes the project's own parser, and `main` its name and User-Agent. They move
  here from the project-templates `data-pipeline` kind, which copied them into every
  project.

[Unreleased]: https://github.com/simonvanlierde/sourceledger/commits/HEAD
