# Security

## Report a vulnerability

Report it privately, through
[a GitHub security advisory](https://github.com/simonvanlierde/sourceledger/security/advisories/new).
Don't open a public issue or pull request for it: anyone can read those before there is
a fix.

Please include:

- What the problem is and where it is: a file, a function or a dependency.
- Steps to reproduce it, or a proof of concept.
- The impact you expect, and the version you saw it in.

I acknowledge a report within 14 days, then tell you whether I can reproduce it and when I
expect a fix.

## Fixes and disclosure

A fix ships in a new release. When it is out, I publish the GitHub security advisory for
it, with a CVE where one applies, and credit you unless you'd rather not be named. The
CHANGELOG entry for that release links the advisory.

## Scope

sourceledger is a library and a command that run on your machine or in your CI. It runs
no server and stores no user data. It downloads the URLs in your `sources.toml` and
writes files under `data/`.

In scope:

- The code in this repository and the package it publishes to PyPI.
- The CI and release workflows in `.github/workflows`.
- The dependencies in `uv.lock`, when the problem affects this project.

Out of scope:

- A problem in a dependency that doesn't affect this project. Report it upstream.
- The data a publisher serves, and the publisher's own server.
- A project's own reader (`parse`) or entry point built on sourceledger.

[Use sourceledger safely](../docs/secure-use.md) describes what sourceledger protects
against, and what it leaves to you.
