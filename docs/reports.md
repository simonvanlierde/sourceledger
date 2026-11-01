---
title: JSON reports for scripts and CI
---

# JSON reports

Use `--json` when a script needs to read a report:

```sh
sourceledger status --json
sourceledger validate --json --only co2
sourceledger revisions before.jsonl --json
```

These commands print one JSON object on stdout. They do not print the usual text report.
Every object has `stage` (the command name) and `errors` (a list of runtime error messages).
A successful run has an empty `errors` list. Existing warnings can still appear on stderr.
Other commands do not accept `--json`.

| Command | Result field | Meaning |
| --- | --- | --- |
| `status` | `sources` | One object per source, in manifest order. |
| `validate` | `problems` | The same list of problem messages the Python function returns. |
| `revisions` | `revisions` | One object per changed source, in current-log order. New sources are excluded, as in text output. |

## Source status

Each `sources` object has `source_id`, `status`, `vintage`, `retrieved_at`, `sha256`,
`license` and `redistribute`. Hashes have all 64 characters. Missing fetch information
is `null`; an absent licence or unknown redistribution flag is also `null`.
`redistribute` is otherwise a JSON boolean. `status` reads no raw files and makes no network requests.

## Revisions

Each revision has `source_id`, `before_sha256` and `after`.
`before_sha256` is the earlier full hash. `after` is the complete latest fetch record,
with the same fields as a line in `data/fetches.jsonl`.
The two logs must exist. An empty `revisions` list means no existing source changed bytes.

## Problems, failures and exit codes

A validation run with no problems prints:

```json
{"stage": "validate", "errors": [], "problems": []}
```

Validation problems go in `problems` and cause exit code 1. Runtime failures, such as a
missing manifest or log, go in `errors` and cause exit code 1. A failed run omits its
result field. For example:

```json
{"stage": "status", "errors": ["sources.toml is missing"]}
```

Other successful reports exit 0. Finding revisions also exits 0: your downstream checks
decide whether a revision breaks the dataset. Invalid command arguments, such as a
missing `revisions` path, use argparse's text error on stderr and exit code 2.

These JSON structures are part of the CLI contract. Read fields by name; JSON object key
order and whitespace are not significant. The [stability policy](https://github.com/simonvanlierde/sourceledger/blob/main/README.md#stability) applies.
