# Product

## Mission

Help research teams publish datasets that other people can verify. Record which source
files went in, link outputs to those files, and make publisher revisions visible.

## Vision

A dataset citation should lead to a specific release that a reader can check without the
producer's environment. A returning user should be able to see which inputs changed and
decide whether to reuse or rebuild the dataset.

## Users

- Researchers who combine published files and need to know when the source changes.
- Research software developers who need provenance in an existing pipeline.
- Dataset recipients who want to check delivered files and find their sources and reuse terms.

## Goals

- Make a dataset release independently verifiable from a downloaded archive.
- Make source revisions and their effects on outputs diagnosable in a pipeline and explainable
  to a dataset recipient.
- Earn adoption in independently maintained research pipelines by working with their existing tools.

The [roadmap](docs/roadmap.md) gives the pilots and the checks that would show progress.

## Product boundaries

Sourceledger is a Python library and CLI configured with `sources.toml`. It records
downloads, tracks output provenance, checks outputs and prepares a release folder. Projects
can keep their own downloaders and hand files to SourceLedger through `record()`.

Each project owns its retrieval logic, numerical comparison rules and publication decisions.
The publisher's terms govern redistribution. The release stage applies the project's recorded
reuse settings.

## Website

The Zensical site helps readers install the tool, follow a worked revision, find a command or
API, and check a release safely. The homepage uses a synthetic example. It follows a download
through a change from 419.31 to 419.32 in the 2023 value.
