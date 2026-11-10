---
title: Use sourceledger safely
redirect_from:
  - /releases.html
description: What sourceledger checks for you when it downloads and builds data, and what it leaves to you.
---

# Use sourceledger safely

sourceledger downloads files from servers you don't control, and you may publish what it
builds. This page lists what it checks for you, and what you still have to do.

## Treat sources.toml as code

sourceledger trusts the manifest as much as your code: it downloads every URL in it. It
also follows `file://` URLs, which the tests use. Review a change to `sources.toml` as you
would a code change.

## Use https

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
- Redirects discard their unused response bodies, so a redirect cannot make urllib
  buffer an unlimited body before it follows the next URL.
- The URL a redirect ends at is logged as `final_url` without its query and fragment, so
  the signature of a signed download link is not published with the dataset.
- The file is requested and stored uncompressed, so the SHA-256 is always that of the file
  as published.

## Keep API keys in environment variables

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

## Pin the SHA-256 of a fixed release

For a file that should never change, such as a dated release, set `sha256` in the
manifest:

```toml
sha256 = "38fe14e8b0ed..."  # all 64 characters, as sha256sum prints them
```

`fetch` then refuses bytes with another hash, and `build` and `validate` refuse a raw file
that no longer matches. Without a pin, a change upstream is recorded as a new version and
shown by `revisions`, but not refused.

## Limit what a download can cost

- `max_bytes` caps each download, 200 MiB unless set. A gzipped file or an Excel
  workbook is also refused when it unpacks past the cap, so a small file cannot expand
  without limit.
- `fetch` refuses a symlink at `data/raw/.lock` or `data/fetches.jsonl`.
- `timeout_s` caps the time for one source. It is not a hard limit; see
  [download limits and retries](fetching.md).

## Record files from other tools with care

`record` takes the file at the path you give it, so treat that path like a manifest URL.
It applies the same checks as a fetch to the bytes: the `sha256` pin and `max_bytes`. It
takes only a regular file, never a pipe or device.

The log, the record and `datapackage.json` go out with your dataset, so `record` keeps
nothing it can recognize as a secret:

- A URL with a user name or password is refused.
- Any other URL loses its query and fragment.
- A parameter whose name looks like a credential is refused. That covers a name that holds
  `token`, `secret`, `passw` or `credential`, or has `key`, `auth`, `sig`, `session` or
  `cookie` as a part (`x-api-key`, `apiKey`, `sig`).

Values are published as given, and a secret under another name gets through. Pass only what
describes the request.

## Check redistribute before you publish raw files

Every implemented source must say whether its terms let you republish the raw file:
`redistribute = true` or `false`. `validate` fails without it. sourceledger never
publishes anything itself. It records `redistribute` in `datapackage.json`, and shows it in
the `status` and `attribution` reports. The README tells you to keep `data/raw/` and
`data/processed/` out of git, so a push never republishes them.

`build` prepares a local working dataset. For `format = "file"` it copies the original
file into `data/processed/`, even when `redistribute = false`. That flag records reuse
terms; it does not prevent private local processing. A successful `validate` or `verify`
checks integrity and does not clear the dataset for publication.

Before you publish a dataset, check the sources marked `redistribute = false`.
[`release`](reference.md#release-a-dataset) leaves their files out, raw copies and tables
alike, and refuses a folder in which a file has their bytes under another name. Outputs built
from several sources can still hold their values, so check the terms for those too.

## Check a dataset you downloaded

`sourceledger verify DIR` checks each file a published `datapackage.json` lists against
the size and SHA-256 it gives. It needs no network, no manifest and no log. It refuses a
path that leads out of the folder, including through a symlink.

`sourceledger release --check DIR` also checks a received folder, but it is not offline. The
RO-Crate validator fetches the `@context` that `ro-crate-metadata.json` names, so the
folder's author chooses which addresses it requests. Run it on a folder you do not trust only
where that is acceptable.

## Check a sourceledger release

Each release is built and published by the
[release workflow](https://github.com/simonvanlierde/sourceledger/blob/main/.github/workflows/python-release.yml),
with no API token. PyPI keeps a signed record of the workflow run that built each file. To check a wheel you downloaded,
pass its path:

```sh
uvx pypi-attestations verify pypi --repository https://github.com/simonvanlierde/sourceledger \
  sourceledger-0.3.1-py3-none-any.whl
```

From 0.2.0, GitHub also keeps a signed build-provenance attestation for each file:

```sh
gh attestation verify sourceledger-0.3.1-py3-none-any.whl --repo simonvanlierde/sourceledger
```

Both fail unless the file is byte for byte the one this repository's workflow built.
From 0.5.0, each GitHub release asset has its provenance beside it as `<asset>.intoto.jsonl`,
so you can check a downloaded file without asking GitHub:

```sh
gh attestation verify sourceledger-0.5.0-py3-none-any.whl --repo simonvanlierde/sourceledger \
  --bundle sourceledger-0.5.0-py3-none-any.whl.intoto.jsonl
```

0.3.1 to 0.4.0 attached one bundle for all files, `sourceledger-<version>.sigstore.json`.

From 0.5.0, a release also carries a CycloneDX SBOM, `sourceledger-<version>.tar.gz.cdx.json`.
It lists only the package itself, because sourceledger has no required dependencies. The
optional extras are not in it. Its attestation,
`sourceledger-<version>.tar.gz.sbom.intoto.jsonl`, ties it to the sdist:

```sh
gh attestation verify sourceledger-0.5.0.tar.gz --repo simonvanlierde/sourceledger \
  --bundle sourceledger-0.5.0.tar.gz.sbom.intoto.jsonl --predicate-type https://cyclonedx.org/bom
```

Release tags from 0.3.0 on are signed with the SSH key in
[`.github/allowed_signers`](https://github.com/simonvanlierde/sourceledger/blob/main/.github/allowed_signers),
and the release workflow publishes nothing for a tag signed with another key. To check a tag,
with the `allowed_signers` file from `main`, not the one in the tagged tree:

```sh
git -c gpg.ssh.allowedSignersFile=.github/allowed_signers tag -v v0.5.0
```

You can also rebuild a release from its tag and compare. Built with the uv version the
release workflow pins (0.13.1 for 0.3.1), the wheel and sdist match the ones on PyPI byte
for byte:

```sh
git clone https://github.com/simonvanlierde/sourceledger && cd sourceledger
mkdir sl-0.3.1 && git archive v0.3.1 | tar -x -C sl-0.3.1
uvx --from uv==0.13.1 uv build sl-0.3.1 -o dist
sha256sum dist/*
```

Compare the hashes with the ones under "Download files" on PyPI.

## How dependencies are chosen

sourceledger has no runtime dependencies, so installing it pulls in no other code. The
optional `xlsx` extra adds openpyxl, `parquet` adds PyArrow, and `release` adds the RO-Crate
and Croissant validators.
[CONTRIBUTING.md](https://github.com/simonvanlierde/sourceledger/blob/main/.github/CONTRIBUTING.md#dependencies)
explains how the development tools are locked and audited.

[What the checks establish](assurance-case.md) explains their trust assumptions, evidence and limits.

## Report a problem

Report a vulnerability privately, as
[SECURITY.md](https://github.com/simonvanlierde/sourceledger/blob/main/.github/SECURITY.md)
describes.
