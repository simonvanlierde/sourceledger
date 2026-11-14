# Roadmap

Last reviewed: 2026-11-14

The next work follows use in real data pipelines. sourceledger keeps its small provenance core:
record downloads, connect outputs to their sources, validate files and package a redistributable release.

## Compare rows across source revisions

[The WDI comparison pilot](https://github.com/simonvanlierde/sourceledger/issues/107) will reproduce
an existing pipeline's change counts using country and year keys for three indicators.
A shared function belongs in sourceledger only after the pilot establishes what callers need.

## Evaluate requests from adopting pipelines

[Adopter extensions](https://github.com/simonvanlierde/sourceledger/issues/108) cover server-derived
vintages, per-output metadata and project-defined stages. Evaluate them after the second pipeline
has moved to the released provenance API. Keep retrieval-specific behaviour in each project.

## Outside the scope

sourceledger will not become a workflow scheduler or a plugin framework for download tools.
Projects that already fetch with pooch, DVC, dlt or custom code can keep those tools and call `record()`.
It does not publish a dataset automatically or decide whether a publisher's terms permit redistribution.
