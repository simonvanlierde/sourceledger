---
title: Use sourceledger safely
description: What sourceledger checks for you when it downloads and publishes data, and what it leaves to you.
---

## Use sourceledger safely

sourceledger downloads files from servers you don't control, and you may publish what it
builds. This page lists what it checks for you, and what you still have to do.

### Treat sources.toml as code

sourceledger trusts the manifest as much as your code: it downloads every URL in it. It
also follows `file://` URLs, which the tests use. Review a change to `sources.toml` as you
would a code change.

### Use https

Write each `url` as `https://` where the publisher serves it. sourceledger accepts `http://`
for a source without `headers`, but then anyone on the network path can change the bytes.
A `sha256` pin, described below, catches that for a fixed file.

These checks apply to every fetch:

- A redirect from a public host to a loopback, private or link-local address is refused.
  The check uses the address the connection actually reached, not a separate DNS lookup,
  so a name that changes its answer cannot get past it. Behind an HTTP(S) proxy the
  connection goes to the proxy, so the check is skipped there. That is the remaining gap.
  Without this, a third-party server could make the fetch save an internal service's
  response under the manifest's URL.
- A redirect that changes the scheme, host or port drops the `headers` from the manifest.
  An API key meant for one host never goes to another, and never goes out over http after
  a redirect from https.
- The file is requested and stored uncompressed, so the SHA-256 is always that of the file
  as published.

### Keep API keys in environment variables

A source that needs a key names an environment variable in `headers`:

```toml
headers = { Authorization = "env:EIA_TOKEN" }
```

- A literal value in `headers` is a manifest error, so a key never lands in git.
- A source with `headers` must have an https `url`, so the key never crosses the network
  in clear text.
- The fetch record lists the header names, never their values. The log does the same.
- The Internet Archive request for `archive = true` sends no `headers`. Don't use
  `archive` on a source that needs a login: the archive could not fetch it anyway.

In CI, put the key in a repository secret and pass it to the job as an environment
variable.

### Pin the SHA-256 of a fixed release

For a file that should never change, such as a dated release, set `sha256` in the
manifest:

```toml
sha256 = "38fe14e8b0ed..."  # all 64 characters, as sha256sum prints them
```

`fetch` then refuses bytes with another hash, and `build` and `validate` refuse a raw file
that no longer matches. Without a pin, a change upstream is recorded as a new version and
shown by `revisions`, but not refused.

### Limit what a download can cost

- `max_bytes` caps each download, 200 MiB unless set. A gzipped file or an Excel
  workbook is also refused when it unpacks past the cap, so a small file cannot expand
  without limit.
- `fetch` refuses a symlink at `data/raw/.lock` or `data/fetches.jsonl`.
- Timeouts bound each request and each source's whole fetch. The
  [README](https://github.com/simonvanlierde/sourceledger#how-fetch-behaves) has the numbers.

### Check redistribute before you publish raw files

Every implemented source must say whether its terms let you republish the raw file:
`redistribute = true` or `false`. `validate` fails without it. sourceledger never
publishes anything itself. It records `redistribute` in `datapackage.json`, and shows it in
the `status` and `attribution` reports. The README tells you to keep `data/raw/` and
`data/processed/` out of git, so a push never republishes them.

`build` prepares a local working dataset. For `format = "file"` it copies the original
file into `data/processed/`, even when `redistribute = false`. That flag records reuse
terms; it does not prevent private local processing. A successful `validate` or `verify`
checks integrity and does not clear the dataset for publication.

Before you publish a dataset, check the sources marked `redistribute = false`. Leave their
raw files out of the release. Your outputs still hold their values, so check the terms
for those too.

### Check a dataset you downloaded

`sourceledger verify DIR` checks each file a published `datapackage.json` lists against
the size and SHA-256 it gives. It needs no network, no manifest and no log. It refuses a
path that leads out of the folder, including through a symlink.

### Check a sourceledger release

Each release on PyPI comes with signed provenance that ties it to the workflow that built
it. The README's
[Verify a release](https://github.com/simonvanlierde/sourceledger#verify-a-release)
section shows how to check it.

### How dependencies are chosen

sourceledger has no runtime dependencies, so installing it pulls in no other code. The
optional `xlsx` extra adds openpyxl. [CONTRIBUTING.md](https://github.com/simonvanlierde/sourceledger/blob/main/.github/CONTRIBUTING.md#dependencies)
explains how the development tools are locked and audited.

### Report a problem

Report a vulnerability privately, as
[SECURITY.md](https://github.com/simonvanlierde/sourceledger/blob/main/.github/SECURITY.md)
describes.
