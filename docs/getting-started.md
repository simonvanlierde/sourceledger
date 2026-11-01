---
title: Follow a publisher revision
description: Fetch a source, notice changed bytes, and rebuild the output.
---

# Follow a publisher revision

This example uses a fictional two-row dataset. A local file stands in for a publisher,
so you can change its contents and see what sourceledger reports. Nothing is downloaded
from the internet.

Install [sourceledger](https://github.com/simonvanlierde/sourceledger#install) first.
The setup commands below also use Python through `uv`.

## Create a small source

Start in a new directory:

```sh
mkdir sourceledger-example
cd sourceledger-example
```

Save this as `setup.py`. It creates the publisher's file and a manifest with its full
local URL:

```python
from pathlib import Path

publisher = Path("publisher/annual.csv")
publisher.parent.mkdir()
publisher.write_text("year,co2_ppm\n2022,417.06\n2023,419.31\n", encoding="utf-8")
Path("sources.toml").write_text(
    f'''[[source]]
id = "co2"
status = "implemented"
url = "{publisher.resolve().as_uri()}"
vintage = "fictional example"
terms = "Fictional example data, dedicated to the public domain under CC0."
terms_url = "https://creativecommons.org/publicdomain/zero/1.0/"
license = "CC0-1.0"
redistribute = true
''',
    encoding="utf-8",
)
```

Run it once:

```sh
uv run --no-project python setup.py
```

## Fetch, build and validate

```sh
sourceledger fetch
sourceledger build
sourceledger validate
```

Fetch records 37 bytes with a checksum starting `943000a3ec89`. Build writes
`data/processed/co2.csv` and `datapackage.json`; validation reports `0 problems`.
The output has the source's columns plus provenance columns naming its download.

Run `sourceledger fetch` again. It reports `unchanged`: the bytes still match.
For HTTP sources, a publisher that supports conditional requests can answer without
sending the file again. See [download behavior](fetching.md).

## Revise the publisher's file

Keep the old log so you can compare versions:

```sh
cp data/fetches.jsonl before.jsonl
```

Save this as `revise.py`, then run it:

```python
from pathlib import Path

publisher = Path("publisher/annual.csv")
publisher.write_text(
    publisher.read_text(encoding="utf-8").replace("419.31", "419.32"),
    encoding="utf-8",
)
```

```sh
uv run --no-project python revise.py
sourceledger fetch
sourceledger validate
```

The new checksum starts `b5e4e235da30`. Fetch adds a record to the log. Validation exits
with code 1 because the existing output was built from the earlier download:

```text
data/processed/co2.csv: rows come from an earlier fetch than co2.csv; re-run build
```

Compare the logs to see which source changed:

```sh
sourceledger revisions before.jsonl
```

`revisions` reports the old and new hashes. It exits 0: a revision is information;
your project's tests decide whether it changes the scientific result.

## Rebuild from the revision

```sh
sourceledger build
sourceledger validate
```

Validation reports `0 problems` again. The 2023 row now contains `419.32` and names the
new download's full SHA-256:

```text
b5e4e235da3066bccd4bf836b9f823b45709fb5e06afdb2b5328210c62c796e5
```

Keep `sources.toml` and the fetch log in version control. Keep the raw and processed
files out of git; [what to commit](https://github.com/simonvanlierde/sourceledger#what-to-commit)
explains why.

For your own sources, use the [manifest and command reference](reference.md).
To check a dataset someone sent you, follow [offline verification](secure-use.md#check-a-dataset-you-downloaded).
