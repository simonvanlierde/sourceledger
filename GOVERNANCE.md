# Governance

sourceledger has one maintainer, Simon van Lierde. This page says who decides what, and
what happens if he can't continue.

## Roles

- **Maintainer:** reviews and merges pull requests, triages issues, makes releases, and
  handles security reports. Only a maintainer can merge to `main` or publish to PyPI.
- **Contributor:** anyone who opens an issue or a pull request. [CONTRIBUTING.md](.github/CONTRIBUTING.md)
  says how.

## How decisions are made

The maintainer decides. A change that affects users, such as a new manifest key, a new
stage or a breaking change, starts as an issue, so anyone can comment before the code is
written. The reasons for a decision go in the issue or the pull request, where anyone can
read them later.

Two design choices hold unless an issue argues them away first: sourceledger has no
runtime dependencies, and every output row names the download it came from. The
[architecture page](docs/architecture.md) shows how the code keeps them.

## Releases

The maintainer publishes a GitHub release from a tag on `main`. The release workflow runs
the full suite on the tagged commit, and again against the built wheel, before it
publishes to PyPI. The maintainer approves each publish by hand. Each release has a
section in the [changelog](CHANGELOG.md), with breaking changes marked and upgrade steps
first. [Stability](README.md#stability) in the README says which changes may break what.

## Adding a maintainer

An existing maintainer adds a new one, by name in this file and in
[CONTRIBUTING.md](.github/CONTRIBUTING.md#who-maintains-this). A new maintainer gets write access to the repository and owner access
to the PyPI project.

## If the maintainer can't continue

The repository is archived with a note in the README. The code stays available under the
MIT license, and anyone may fork it and publish it under another name.

## Changing this document

A change to this file is a pull request like any other, and the maintainer decides it.
