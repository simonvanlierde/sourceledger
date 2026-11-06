---
title: Where sourceledger fits
description: What sourceledger does that download, pipeline and packaging tools leave out, and how to use them together.
redirect_from:
  - /comparison.html
---

# Where sourceledger fits

sourceledger keeps track of what happened to the bytes a dataset is built from, and
whether you may use them. For each source it records the reuse terms, every version
downloaded, and which version each output row came from. It notices when a publisher
revises a file, and it lets the people who receive your dataset check it offline.

It downloads too, but only the plain case: one URL per source, with conditional requests
and retries. When a tool below already fetches your data, keep it and hand each file to
[`record`](recipes.md#record-a-file-another-tool-downloaded).

| Tool | What it does well | What sourceledger adds | Together |
| --- | --- | --- | --- |
| [pooch](https://www.fatiando.org/pooch/) | Downloads and caches files with a known hash. Made for packages that ship sample data. | A log of every version, reuse terms per source, provenance in each output row, and revision reports. | [pooch recipe](recipes.md#pooch) |
| [DVC](https://dvc.org) `import-url` | Tracks a downloaded file's URL with its ETag, and `dvc update` notices a change. | Reuse terms per source, provenance in each output row, a Data Package and an offline check for recipients. | [DVC recipe](recipes.md#dvc) |
| [dlt](https://dlthub.com) | Paginated and authenticated APIs, incremental loading, and loading into databases. | A record of the files a dataset is built from, with their terms, and the rows that came from each. | [dlt recipe](recipes.md#dlt) |
| [DataLad](https://www.datalad.org) `download-url` | Records where every file came from in git and git-annex, as part of full dataset version control. | A plain manifest and one committed log, no git-annex, and reuse terms per source. | Run either; both keep their own records. |
| [frictionless-py](https://framework.frictionlessdata.io) | Describes and validates Data Packages, with licences, sources and hashes per resource. | The fetch, record, verify and revision steps. | `build` writes a Data Package that frictionless reads. |
| [Croissant](https://mlcommons.org/working-groups/data/croissant/) | Describes machine-learning datasets, their files and fields for loaders and search. | Records of where the files came from and under what terms. | Describe the built dataset with Croissant. |

sourceledger has no plugins or adapters for these tools. A recipe is a few lines of your
own code around `record`, so neither tool constrains how you use the other.

Checked against each project's code and docs on 2026-11-05 and 2026-11-06.
[Decision 0005](decisions/0005-provenance-core.md) explains why sourceledger records other
tools' downloads instead of growing its own fetcher.
