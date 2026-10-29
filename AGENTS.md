# AGENTS.md

Notes for coding agents working on sourceledger. The `justfile` holds every command;
`just` lists them.

## Check

Run `just check` before you call a change done. It runs what CI runs for each stack.
`just fmt` fixes what it can first.

## Test

- Python: `just py-test`. Extra arguments reach the test runner.

## Docs

`README.md` says how to set up, run and release the project. Record user-facing changes
under `[Unreleased]` in `CHANGELOG.md`.
`.github/CONTRIBUTING.md` has the commit and sign-off rules.
