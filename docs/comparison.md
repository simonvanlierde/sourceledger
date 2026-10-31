---
title: Compare sourceledger with other tools
---

[Documentation](index.md) · [Reference](reference.md)

## Compared with

Other tools cover parts of this. Use one of them when that part is all you need.

| Tool | What it does well | What sourceledger adds |
| --- | --- | --- |
| [pooch](https://www.fatiando.org/pooch/) | Downloads files with a known hash, caches them, retries. Made for packages that ship sample data. | A record of every fetch on disk, conditional re-fetches, a history of versions, provenance in each output row, and reuse terms checked per source. |
| [DVC](https://dvc.org) `import-url` | Tracks a downloaded file's URL with its checksum or ETag, and `dvc update` notices a change. The closest overlap. | No cache or `.dvc` files to adopt, reuse terms per source, provenance in each output row, and a Data Package for the outputs. |
| [DataLad](https://www.datalad.org) `download-url` | Records where every file came from in git and git-annex, as part of full dataset version control. | A plain manifest and one committed log, no git-annex, and reuse terms per source. |
| [frictionless-py](https://framework.frictionlessdata.io) | Describes and validates Data Packages, with licences, sources and hashes per resource. | The fetch, verify and revision steps. `build` writes a Data Package, so the two work together. |

Checked against each project's code and docs on 2026-10-18.
