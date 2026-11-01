---
title: Verify a sourceledger release
---

# Verify a sourceledger release

Each release is built and published by the
[release workflow](../.github/workflows/python-release.yml), with no API token. PyPI keeps a
signed record of the workflow run that built each file. To check a wheel you downloaded,
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
From 0.3.1, the same provenance is also attached to each GitHub release as
`sourceledger-<version>.sigstore.json`.

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
