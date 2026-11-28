---
description: Plans to make research dataset releases independently verifiable and source revisions easier to evaluate.
---

# Roadmap

A reader should be able to start from a dataset citation, check the files it refers to, and
find the source versions behind them. When a new release appears, they should be able to see
which inputs and outputs changed. These are the proposed priorities for getting there.

## Now · Verify a real release from a clean environment

Use a frozen research dataset from the [R9lab vocabulary pipeline](https://github.com/simonvanlierde/r9lab-vocabulary)
for the first deposit pilot. Build on the [pinned Pooch workflow](recipes.md#pooch).
Pooch retrieves the files and SourceLedger records the exact inputs and checks the outputs.
Deposit the archive in Zenodo for a version-specific DOI.

- Start with a frozen research dataset whose reuse terms can be checked before publication.
- Have someone outside the producing pipeline verify the archive with
  [`sourceledger verify DIR`](reference.md#other-commands) and trace one output to its source.
- Repeat the check after modifying a delivered file and confirm that verification flags it.
  Inspect the archive for raw files marked as excluded from redistribution.

## Next · Explain what changed between releases

Use the existing weekly trial in a World Development Indicators (WDI) pipeline. The
pipeline keeps its own retrieval and comparison rules. Aim to capture a real publisher
change within three months of the first scheduled report.
Document the changed source version, the affected output and the maintainer's decision to
rebuild or keep the existing release. Then use two published releases to test what a dataset
recipient can learn from their provenance records alone.

The WDI pilot matched [360 adjacent comparisons across 121 archived vintages](https://github.com/simonvanlierde/sourceledger/issues/107)
using SourceLedger's row-alignment API.
The [two-release PUDL recipe](recipes.md#follow-a-publishers-revision-across-two-releases) provides a second
case with stable source identifiers and domain-specific comparison rules.

## Reuse what the pilots prove

| Project | What to reuse and check |
| --- | --- |
| Existing workbook pipeline | Keep its workbook reader and supplement steps. Check that release and verification cover a pipeline with different inputs and reuse terms. |
| [project-templates](https://github.com/simonvanlierde/project-templates) | Share a proven verification example through the existing data-pipeline template. Render a fresh project and run the example there before updating the template. |
| [citesync](https://github.com/simonvanlierde/citesync) | Keep software citation files in sync with citesync. Pass dataset-specific CodeMeta to the existing [release command](reference.md#release-a-dataset) when the dataset has its own DOI or licence. |
| [CRediT Matrix](https://github.com/simonvanlierde/credit-matrix/tree/main/integrations/ro-crate) | Test its attribution fragment against a dataset release once the census has a frozen input extract. Check profile compatibility and validate the combined RO-Crate before documenting the recipe. |

## By the end of 2027 · Verify adoption across projects

| Target | Evidence |
| --- | --- |
| Three DOI-versioned datasets | Each deposit links to its verification instructions, and someone outside the producing pipeline verifies its archive. |
| Three maintained pipeline integrations, including one independent team | The team runs SourceLedger in its own release or revision workflow and can share the evidence publicly. |
| One worked case from a real publisher revision | A reader can follow the input change, identify an affected output and understand the decision about the next release. |

Start the independent pilot with a team that builds a dataset from published inputs. The PUDL
recipe gives them a way to test provenance across two releases using
[PUDL's existing versioned Parquet files and Data Package](https://docs.catalyst.coop/pudl/en/v2026.10.0/data_access.html).
Give them the [downstream release example](recipes.md#package-a-downstream-result-from-both-releases)
and ask whether its provenance answers a question they have about their next release.
Consider a software paper once an independent team can reproduce the workflow.

## Scope

Sourceledger owns the provenance record, integrity checks and release packaging. Pipelines own
scheduling, downloading at scale and data-specific judgments. Keep existing retrieval tools
and [record the files they produce](recipes.md#record-a-file-another-tool-downloaded).
Projects decide which files their source terms allow them to publish.

Before adding a feature, try it with the tools in [Where SourceLedger fits](where-it-fits.md).
Use their public APIs and the existing Data Package, RO-Crate or Croissant outputs where a
pilot needs them. Test the artifact with the tool that will read it. Share general fixes
and examples with the upstream project. Add to SourceLedger when the pilot identifies a
provenance or integrity gap that belongs here.
