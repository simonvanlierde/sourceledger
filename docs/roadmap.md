---
description: Plans to make research dataset releases independently verifiable and source revisions easier to evaluate.
---

# Roadmap

A reader should be able to start from a dataset citation, check the files it refers to, and
find the source versions behind them. When a new release appears, they should be able to see
which inputs and outputs changed. These are the proposed priorities for getting there.

## Now · Verify a real release from a clean environment

Use one research dataset to test the whole path from the maintainer's pipeline to the
recipient's machine. Publish a versioned archive with a DOI, a manifest, recorded source
checksums and a command the recipient can run after downloading it.

- Start with a frozen research dataset whose reuse terms can be checked before publication.
- Have someone outside the producing pipeline verify the archive with
  [`sourceledger verify DIR`](reference.md#other-commands) and trace one output to its source.
- Repeat the check after modifying a delivered file and confirm that verification flags it.
  Inspect the archive for raw files marked as excluded from redistribution.

## Next · Explain what changed between releases

Run a weekly revision check in a pipeline that uses WDI. Aim to capture a real publisher
change within three months.
Document the changed source version, the affected output and the maintainer's decision to
rebuild or keep the existing release. Then use two published releases to test what a dataset
recipient can learn from their provenance records alone.

The WDI pilot matched [360 adjacent comparisons across 121 archived vintages](https://github.com/simonvanlierde/sourceledger/issues/107)
and WISE [adopted the released row-alignment API](https://github.com/simonvanlierde/wise-pipeline/pull/21).
The [two-release PUDL recipe](recipes.md#follow-a-publishers-revision-across-two-releases) provides a second
case with stable source identifiers and domain-specific comparison rules.

## By the end of 2027 · Verify adoption across projects

| Target | Evidence |
| --- | --- |
| Three DOI-versioned datasets | Each deposit links to its verification instructions, and someone outside the producing pipeline verifies its archive. |
| Three maintained pipeline integrations, including one independent team | The team runs SourceLedger in its own release or revision workflow and can share the evidence publicly. |
| One worked case from a real publisher revision | A reader can follow the input change, identify an affected output and understand the decision about the next release. |

Start the independent pilot with a team that already publishes versioned data. Share the PUDL
example when asking for feedback and let the maintainers decide if it fits their workflow.
Use the pilots to identify common gaps in release metadata and verification. Consider a software
paper once there is independent use and evidence that others can reproduce.

## Scope

Sourceledger owns the provenance record, integrity checks and release packaging. Pipelines own
scheduling, downloading at scale and data-specific judgments. Keep existing retrieval tools
and [record the files they produce](recipes.md#record-a-file-another-tool-downloaded).
Projects decide which files their source terms allow them to publish.
