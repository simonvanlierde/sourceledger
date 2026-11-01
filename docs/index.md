---
title: sourceledger
description: Build a dataset from published sources and check when they change.
---

# Know when your source data changes

Build a dataset from published sources. Keep a record of each download, trace the outputs back to it,
and check when the publisher changes the file.

```sh
uv tool install sourceledger
```

Python 3.12+ · Linux and macOS · [Installation options](https://github.com/simonvanlierde/sourceledger#install)

{% include revision-example.html %}

## Use sourceledger in your project

Use the [manifest and command reference](reference.md) for your own sources.

For scripts and CI, [JSON reports](reports.md) provide structured status and validation results.
If you received a dataset, you can [verify its files offline](secure-use.md#check-a-dataset-you-downloaded).

See [how sourceledger compares with other tools](comparison.md) before choosing it for your project.
