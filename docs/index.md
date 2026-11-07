---
title: sourceledger
home: true
description: Build a dataset from published sources and check when they change.
---

# Know when your source data changes

Build a dataset from published sources. Keep a record of each download, trace the outputs back to it,
and check when the publisher changes the file.

{% include home-install.html %}

{% include revision-example.html %}

Follow the [complete example](getting-started.md), or review the
[installation options](https://github.com/simonvanlierde/sourceledger#install).

## Use sourceledger in your project

Use the [manifest and command reference](reference.md) for your own sources.

If pooch, DVC, dlt or your own code already downloads your data, keep it and
[record each file](recipes.md#record-a-file-another-tool-downloaded).
For scripts and CI, [JSON reports](reference.md#json-reports) provide structured status and validation results.
If you received a dataset, you can [verify its files offline](secure-use.md#check-a-dataset-you-downloaded).

See [where sourceledger fits](where-it-fits.md) among other data tools before choosing it for your project.
