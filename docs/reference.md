---
title: Manifest and command reference
---

[Documentation](index.md) · [Reference](reference.md)

## sources.toml

A source is one file you download. A vintage is the edition you expect to use, such as a release year.
The manifest records its reuse terms; it does not decide whether those terms permit your intended use.

Each `[[source]]` table takes these keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | ASCII letters, digits, `_` and `-`. Names the raw file (`id` plus the URL's extension) and the output. |
| `name` | no | A title for people. The data package and `attribution` show it in place of the id. |
| `status` | yes | `"implemented"`, read by every stage, or `"unimplemented"`: a source you know about but don't read yet. `validate` fails if one of those has output. |
| `url` | for implemented sources | What `fetch` downloads. Change it and `build` refuses the old download until you fetch again. |
| `terms`, `terms_url` | for implemented sources | The publisher's reuse terms in their words, and where they are stated. |
| `license` | for implemented sources | The same terms as an [SPDX id](https://spdx.org/licenses/), such as `"CC-BY-4.0"`, or `"LicenseRef-<name>"` for terms with none. |
| `redistribute` | for implemented sources | `true` if the terms let you republish the raw file, `false` if not. |
| `vintage` | no | The edition you expect, such as `"2024 release"` or a TOML date. Recorded with each fetch and stamped on each row. `"not stated"` when absent. The old name `release` is refused. |
| `sha256` | no | The SHA-256 the download must have. Pin it for a fixed release, and `fetch` refuses bytes that changed upstream. |
| `max_bytes` | no | The largest download `fetch` accepts. 200 MiB unless set. A gzipped file or Excel workbook that unpacks past it is refused. |
| `timeout_s` | no | A positive finite number of seconds for one source's download, retries and waits included. 300 unless set. |
| `format` | no | `"file"` copies bytes unchanged. Table formats are `"csv"`, `"json"` and `"xlsx"`; see the details below. |
| `sheet` | no | The Excel sheet to read. The first unless set. A cell right of the last header cell is refused, and a missing cell in a row reads as `""`. |
| `headers` | no | Extra request headers, such as an API key, each value naming an environment variable: `{ Authorization = "env:EIA_TOKEN" }`. A literal value is refused, and so is a `url` that is not https. The record keeps the header names, not the values. A redirect to another scheme, host or port drops them. |
| `archive` | no | `true` to ask the Internet Archive's [Save Page Now](https://web.archive.org/save) to snapshot the URL after each fetch that brings new bytes. The record keeps the snapshot's address as `archive_url`. Only for http and https URLs, and not for a source that needs `headers`: the archive's request sends none. |
| `reason` | no | A note for people, such as why a source is unimplemented. sourceledger doesn't read it. |

Without `format`, the URL extension selects JSON (`.json`) or Excel (`.xlsx`); other extensions select CSV.
CSV and JSON can be gzipped; decompression stays within `max_bytes`.
JSON input must be an array of objects. Numbers keep their spelling: `1e5` stays `1e5`, and `1.10` stays `1.10`.
`format = "file"` keeps archives, rasters and other files unchanged, without unpacking or parsing them.

`validate` reports a missing `terms`, `terms_url`, `license` or `redistribute`. The other
keys are checked when the manifest loads. Keys not listed here are kept for your own reader.

For checks in an editor with a TOML language server (Tombi, Taplo), put this line at the
top of `sources.toml`. `init` writes it, with the version you have installed in place of
`v0.3.1`:

```toml
#:schema https://raw.githubusercontent.com/simonvanlierde/sourceledger/v0.3.1/src/sourceledger/sources.schema.json
```

The schema accepts exactly what sourceledger accepts. A test keeps the two in step.

## Other commands

- `init` writes a starter `sources.toml` in the current directory, or at `--manifest PATH`:
  the schema line and one example source, commented out. It won't overwrite an existing one.
- `revisions OLD_LOG` lists the sources whose bytes changed since an earlier copy of the
  log. Copy the log before you fetch (`cp data/fetches.jsonl before.jsonl`), then run
  `sourceledger revisions before.jsonl`. A weekly CI run can use it to report a
  publisher's revision.
- `status` prints one line per source: its id, status and vintage, when its last logged
  version was fetched (or `never`), the start of its SHA-256, its licence, and whether
  the raw file may be republished. It reads the manifest and the log, never the network.
- `attribution` prints a Markdown table of the implemented sources, in manifest order:
  name, terms in the publisher's words, licence, terms URL, and whether the raw file may be
  republished. Paste it into a dataset's README or release notes.
- `verify DIR` is for someone who downloaded a published dataset. It reads
  `DIR/datapackage.json` and checks that each file it lists exists, with the size and
  SHA-256 it gives. It prints one line per problem and exits 1 if there are any. It needs
  no `sources.toml` and no network.

Commands exit 0 on success and 1 on a runtime failure or validation problem.
Invalid arguments exit 2. `revisions` exits 0 when sources changed: your downstream checks decide what a revision breaks.

`fetch`, `build` and `validate` take `--only ID`, as often as needed, to work on just those
sources. An id that is not in `sources.toml` is an error. `build --only` keeps the other
sources' entries in `datapackage.json` as the last build wrote them, and
`validate --only` skips the checks that cover the whole output.

Every command that reads the manifest reads `./sources.toml` unless given `--manifest PATH`.
The data folder stays `data/` in the current directory either way.

`status`, `validate` and `revisions` accept `--json`; see [JSON reports](reports.md) for fields and exit codes.
