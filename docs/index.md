---
title: sourceledger
home: true
description: Build a dataset from published sources and check when they change.
---

# Know when your source data changes

Build a dataset from published sources. Keep a record of each download, trace the outputs back to it,
and check when the publisher changes the file.

```sh
uv tool install sourceledger
```

Python 3.12+ · Linux, macOS and Windows

## From published bytes to a built dataset

<!-- rumdl-disable MD033 -->
<div class="provenance-flow" role="group" aria-label="Original dataset provenance">
<ol>
<li><strong>Published source</strong><br>annual.csv · declared in sources.toml<br><code>2023 · 419.31 ppm</code></li>
<li><strong>Recorded download</strong><br>SHA-256 · first 12 characters<br><code>943000a3ec89</code></li>
<li><strong>Built output</strong><br>data/processed/co2.csv<br>Linked to the original download</li>
</ol>
</div>

### A one-value change leaves a trail

Synthetic example · CO₂ concentration, ppm:

| Year | Original download | After the revision |
| --- | --- | --- |
| 2022 | 417.06 | 417.06 |
| 2023 | 419.31 | **419.32** |

The original output passes validation. The publisher then changes 2023’s value from **419.31 to 419.32**.
After a fresh fetch, validation reports that the existing output needs rebuilding:

```text
data/processed/co2.csv: rows come from an earlier fetch than co2.csv; re-run build
```

<details>
<summary>Read the recorded checksums</summary>
<p>Original SHA-256: <code>943000a3ec89b4be3372b10e0f904c45cef4f9168aa728639e0eb7e9d2747505</code></p>
<p>Revised SHA-256: <code>b5e4e235da3066bccd4bf836b9f823b45709fb5e06afdb2b5328210c62c796e5</code></p>
</details>
<!-- rumdl-enable MD033 -->

Follow the [complete example](getting-started.md), or review the
[installation options](https://github.com/simonvanlierde/sourceledger#install).

## Use sourceledger in your project

Use the [manifest and command reference](reference.md) for your own sources.

If pooch, DVC, dlt or your own code already downloads your data, keep it and
[record each file](recipes.md#record-a-file-another-tool-downloaded).
For scripts and CI, [JSON reports](reference.md#json-reports) provide structured status and validation results.
If you received a dataset, you can [verify its files offline](secure-use.md#check-a-dataset-you-downloaded).

See [where sourceledger fits](where-it-fits.md) among other data tools before choosing it for your project.
