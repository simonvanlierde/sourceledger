# Roadmap

Last reviewed: 2026-11-15

The next work follows use in real data pipelines. sourceledger keeps its small provenance core:
record downloads, connect outputs to their sources, validate files and package a redistributable release.

## Compare rows across source revisions

[The WDI comparison pilot](https://github.com/simonvanlierde/sourceledger/issues/107) reproduces
an existing pipeline's change counts using country and year keys for three indicators.
Cached indicator/version pairs match the baseline; missing publisher archives still block the full grade.
`align_rows`, added in 0.6.0, pairs rows by validated keys while keeping numeric rules and reports in each project.
The complete pilot grade remains open. WISE will replace its local alignment helper after 0.6.0 is published.

## Integrate adopting pipelines

[Adopter extensions](https://github.com/simonvanlierde/sourceledger/issues/108) were evaluated in
both migration branches. Vintage selection, reader findings and project-defined stages stay in each project;
the [integration recipe](recipes.md#add-a-project-specific-stage) shows how to extend a command.
Both migrations have merged: cml-ia-factors and wise-pipeline use released sourceledger 0.5.1.

## Outside the scope

sourceledger will not become a workflow scheduler or a plugin framework for download tools.
Projects that already fetch with pooch, DVC, dlt or custom code can keep those tools and call `record()`.
It does not publish a dataset automatically or decide whether a publisher's terms permit redistribution.
