---
title: Write a custom reader
---

# Write a custom reader

For a file you want to keep unchanged, set `format = "file"`; you do not need a reader.
For tables, `parse_source` chooses `parse_csv`, `parse_json` or `parse_xlsx`.

To read another table format, write a function that takes a source's manifest table and raw bytes.
Return the column names and rows. Pass the function to your project's command:

```python
# src/my_dataset/pipeline.py
import sourceledger
from sourceledger import Source


def parse(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Add a branch on source["id"] for a format the default reader cannot read."""
    return sourceledger.parse_source(source, payload)


def main(argv: list[str] | None = None) -> int:
    return sourceledger.main(argv, parse=parse, prog="my-dataset", user_agent="my-dataset")
```

```toml
# pyproject.toml
[project.scripts]
my-dataset = "my_dataset.pipeline:main"
```

`my-dataset build` uses your reader. The other stages keep their usual behavior.
Publishers see `my-dataset` as the User-Agent when you fetch. If your project ships `sources.toml` inside its package, pass the path as `manifest=Path(__file__).with_name("sources.toml")`. The
[project-templates](https://github.com/simonvanlierde/project-templates) `data-pipeline`
kind sets this up, with CI and a weekly revision report. The stages are also
functions: `load_manifest`, `fetch`, `build`, `validate`, `revisions`, `status`,
`attribution` and `verify`. `verified(source)` returns a source's fetch record and raw
bytes after checking that both are current, for a reader that needs them.
