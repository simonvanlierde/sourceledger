# AGENTS.md

Notes for coding agents working on sourceledger. The `justfile` holds every command;
`just` lists them.

## Check

Run `just check` before you call a change done. It runs the stack checks CI runs, so a miss here is caught there too, then
the `local-check` recipe in `just/local.just` if that file has one.
`just fmt` fixes what it can first.

## Test

- Python: `just py-test`. Extra arguments reach the test runner.

## Docs

`README.md` says how to set up and run the project, and `just release` cuts a release.
Record user-facing changes under `[Unreleased]` in `CHANGELOG.md`.
Read `docs/decisions/` before changing a recorded choice.
`.github/CONTRIBUTING.md` has the commit and sign-off rules.
