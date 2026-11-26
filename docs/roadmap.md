# Roadmap

Last reviewed: 2026-11-26

The next work follows use in real data pipelines. sourceledger keeps its small provenance core:
record downloads, connect outputs to their sources, validate files and package a redistributable release.

## Compare rows across source revisions

[The WDI comparison pilot](https://github.com/simonvanlierde/sourceledger/issues/107) reproduces
an existing pipeline's change counts using country and year keys for three indicators.
All 360 original adjacent indicator/version pairs match the baseline across 121 archived vintages.
`align_rows`, added in 0.6.0, pairs rows by validated keys while keeping numeric rules and reports in each project.
WISE uses the released API. Its results match the original counts and shares across all 360 comparisons.

## Integrate adopting pipelines

[Adopter extensions](https://github.com/simonvanlierde/sourceledger/issues/108) were evaluated in
both migration branches. Vintage selection, reader findings and project-defined stages stay in each project;
the [integration recipe](recipes.md#add-a-project-specific-stage) shows how to extend a command.
Both projects use released sourceledger. The [Pooch recipe](recipes.md#pooch) uses a shared SHA-256 pin for downloading and recording.

## Outside the scope

sourceledger will not become a workflow scheduler or a plugin framework for download tools.
Projects that already fetch with pooch, DVC, dlt or custom code can keep those tools and call `record()`.
It does not publish a dataset automatically or decide whether a publisher's terms permit redistribution.
