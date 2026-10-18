# sourceledger

[![python](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml/badge.svg)](https://github.com/simonvanlierde/sourceledger/actions/workflows/python.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Build a dataset from public sources where every row says which download it came from.

A project lists its inputs in `sources.toml`: each source's URL, the publisher's reuse
terms, and whether the raw file may be republished. sourceledger runs four stages over
that manifest:

- `fetch` downloads each source into `data/raw/`, with a record beside it: the URL, when
  it was fetched, the SHA-256 and size of the bytes, the vintage the manifest declares,
  and the server's ETag and Last-Modified. The next fetch sends the ETag back, so an
  unchanged source is not downloaded again. Each new version of a source is also
  appended to `data/fetches.jsonl`.
- `build` re-hashes each download against its record, reads it, and writes
  `data/processed/<id>.csv` with five provenance columns in front of the source's own:
  `source_id`, `source_url`, `retrieved_at`, `vintage` and `raw_sha256`.
- `validate` re-reads the output and reports every problem it finds: output built from
  an older fetch, a raw file changed since its fetch, a record made for another URL,
  vintage or pin, a source with no reuse terms, an output with no rows, and output left
  by a source that is gone or marked unimplemented.
- `revisions OLD_LOG` lists the sources whose bytes changed since an earlier copy of the
  fetch log. A weekly CI run can use it to say when a publisher revised its data.

sourceledger has no dependencies: the standard library does all of it.

## Install

```sh
uv add sourceledger
```

Until the first release is on PyPI, install it from GitHub:
`uv add git+https://github.com/simonvanlierde/sourceledger`.

## Quick start

Write a `sources.toml` in the project root:

```toml
[[source]]
id = "co2"
status = "implemented"
url = "https://raw.githubusercontent.com/datasets/co2-ppm/main/data/co2-annmean-mlo.csv"
vintage = "main branch, unversioned"
terms = "ODC-PDDL-1.0 (datasets/co2-ppm package; original data from NOAA GML)"
terms_url = "https://opendatacommons.org/licenses/pddl/"
license = "PDDL-1.0"
redistribute = true
```

Then run the stages from that directory:

```sh
sourceledger fetch
sourceledger build
sourceledger validate
```

`sourceledger` reads every source as CSV. For other formats, see
[Your own readers](#your-own-readers).

## What to commit

Commit `sources.toml` and `data/fetches.jsonl`. The log holds checksums and URLs, not
data, and it is the history of every version of every source you have fetched.

Keep `data/raw/` and `data/processed/` out of git, so the repository never
redistributes a publisher's data. The per-file `.fetch.json` records live in
`data/raw/` too. A fresh clone runs `fetch` before `build` and `validate`.

## sources.toml

Each `[[source]]` table takes these keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Letters, digits, `_` and `-`. Names the raw file (`id` plus the URL's extension) and the output. |
| `status` | yes | `"implemented"`, read by every stage, or `"unimplemented"`: a source you know about but don't read yet. `validate` fails if one of those has output. |
| `url` | for implemented sources | What `fetch` downloads. Change it and `build` refuses the old download until you fetch again. |
| `terms`, `terms_url` | for implemented sources | The publisher's reuse terms in their words, and where they are stated. |
| `license` | for implemented sources | The same terms as an [SPDX id](https://spdx.org/licenses/), such as `"CC-BY-4.0"`, or `"LicenseRef-<name>"` for terms with none. |
| `redistribute` | for implemented sources | `true` if the terms let you republish the raw file, `false` if not. |
| `vintage` | no | The edition you expect, such as `"2024 release"` or a TOML date. Recorded with each fetch and stamped on each row. `"not stated"` when absent. The old key `release` still works, with a warning. |
| `sha256` | no | The SHA-256 the download must have. Pin it for a fixed release, and `fetch` refuses bytes that changed upstream. |
| `max_bytes` | no | The largest download `fetch` accepts. 200 MiB unless set. |
| `reason` | no | A note for people, such as why a source is unimplemented. sourceledger doesn't read it. |

`validate` reports a missing `terms`, `terms_url`, `license` or `redistribute`. The other
keys are checked when the manifest loads.

## How fetch behaves

- It tries four times, waiting 5, 10 and 20 s between attempts, on 429, 500, 502, 503,
  504, network errors and a body cut short. A `Retry-After` header, in seconds or as a
  date, raises the wait to at most 120 s. Any other error status fails at once.
- Each connect and read times out after 30 s, and one source's whole fetch after 300 s.
- It asks for the file uncompressed, and refuses a compressed reply. The checksum is
  always of the file as published.
- It refuses a redirect from a public host to a loopback, private or link-local
  address. When a redirect is followed, the record keeps the final URL too.
- Every source is tried. When some fail, the others are still written, and the run
  exits 1 with one line per failed source.
- Each file is written to a new temporary file and renamed into place, so a crash never
  leaves half a file. A fetch log line cut off by a crash is dropped on the next run.

## Your own readers

`sourceledger` reads every source with `parse_csv`. A project with other formats gives
it its own reader, a function from a source's manifest table and raw bytes to columns
and rows, and its own command:

```python
# src/my_dataset/pipeline.py
import sourceledger
from sourceledger import Source


def parse(source: Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Every source is CSV here. Branch on source["id"] for one that is not."""
    return sourceledger.parse_csv(source, payload)


def main(argv: list[str] | None = None) -> int:
    return sourceledger.main(argv, parse=parse, prog="my-dataset", user_agent="my-dataset")
```

```toml
# pyproject.toml
[project.scripts]
my-dataset = "my_dataset.pipeline:main"
```

`my-dataset fetch`, `my-dataset build` and `my-dataset validate` then run with that
reader, and publishers see `my-dataset` as the User-Agent. The
[project-templates](https://github.com/simonvanlierde/project-templates) `data-pipeline`
kind sets all of this up, with CI and a weekly run that reports revised sources.

The same stages are functions too: `load_manifest`, `fetch`, `build`, `validate` and
`revisions`.

## Stability

Until 1.0, the public API is what `sourceledger` exports in `__all__`, the keys of
`sources.toml`, and the format of fetch records and the fetch log. A breaking change to
any of them comes only in a minor version, listed in [CHANGELOG.md](CHANGELOG.md), with
a warning first where that is cheap. Fetch logs written by an earlier version stay
readable.

## Development

```sh
uv sync
uv tool install prek   # once per machine
prek install           # git hooks
just check             # lint, types and tests, as CI runs them
```

## License

MIT. See [LICENSE](LICENSE).
