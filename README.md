# sourceledger

[![python](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml/badge.svg)](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Build a dataset from public sources where every row says which download it came from.

A project lists its inputs in `sources.toml`: each source's URL, the publisher's reuse
terms in their words, the same terms as an SPDX `license`, and whether the raw file may
be republished. sourceledger then runs four stages over that manifest:

- `fetch` downloads each source and writes a record beside it: the URL, when it was
  fetched, the SHA-256 and size of the bytes, the release the manifest names, and the
  server's ETag and Last-Modified. It retries what a publisher recovers from, refuses a
  download over its size cap or one that misses a pinned `sha256`, and sends the ETag back
  next time, so an unchanged source is not downloaded again. Each new version of a source
  is appended to `data/fetches.jsonl`, a log you commit.
- `build` re-hashes each download against its record before it reads it, and stamps
  every output row with that record.
- `validate` fails when an output came from an older download, a raw file changed after
  it was fetched, or a source has no reuse terms.
- `revisions OLD_LOG` lists the sources whose bytes changed since an earlier copy of the
  fetch log.

The standard library does all of it: sourceledger has no dependencies.

## Use it

The `data-pipeline` kind of
[project-templates](https://github.com/simonvanlierde/project-templates) sets up a
project around it. By hand, give your project an entry point that passes its own reader
and name:

```python
from sourceledger import Source, main as run, parse_csv


def parse(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Every source is CSV here. Branch on source["id"] for one that is not."""
    return parse_csv(source, payload)


def main(argv: list[str] | None = None) -> int:
    return run(argv, parse=parse, prog="my-dataset", user_agent="my-dataset")
```

Then `my-dataset fetch`, `my-dataset build` and `my-dataset validate` run from the
project root, where `sources.toml` is. Raw files go to `data/raw/` and outputs to
`data/processed/`. Keep both out of git, so the repository never redistributes source
data. The fetch records and `data/fetches.jsonl` hold checksums, not values, and are
safe to commit.

## Development

```sh
uv sync
uv tool install prek   # once per machine
prek install     # git hooks
```

`just` lists the tasks. They mirror what [.github/workflows/](.github/workflows/) runs.

```sh
just check       # lint, types and tests for every stack here
```

## License

See [LICENSE](LICENSE) for the license terms.
