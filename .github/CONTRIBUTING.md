# Contributing

Bug reports, feature requests and pull requests are welcome. Read the [Code of Conduct](CODE_OF_CONDUCT.md) first, and
report vulnerabilities as [SECURITY.md](SECURITY.md) describes, not in a public issue.

## Report a bug or ask for a feature

Open an [issue](https://github.com/simonvanlierde/sourceledger/issues) with the bug or
feature form. For a bug, say what you did, what you expected and what happened, with the
version you used. A question the README and docs don't answer is a gap in the docs: open
a feature request for it.

## Scope and response

One person maintains sourceledger, in their own time. Expect a reply to an issue or
pull request within about two weeks.

Before a large pull request, such as a new feature or a change to how something works,
open an issue and agree on the change there first. A small fix needs no issue. A pull
request outside what the project sets out to do is closed with a link to this section.

## Set up and check

Use Python 3.12 or later, [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).
From the checkout, install the dependencies and git hooks:

```sh
uv sync
uv tool install prek
prek install
just check
```

`just check` runs the checks CI runs. `just py-fmt` fixes lint and formatting issues.

## Coding standard

- **Lint and format:** [ruff](https://docs.astral.sh/ruff/) with every rule on
  (`select = ["ALL"]`) and a short list of ignores in `pyproject.toml`, each with its
  reason. Lines are at most 120 characters. `ruff format` formats the code.
- **Types:** every function in `src/` is annotated, and [ty](https://docs.astral.sh/ty/) checks them.
  Silence a false positive with `# ty: ignore[<rule>]` and a reason, never a bare ignore.
- **Docstrings:** [Google style](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings),
  checked by ruff. A docstring says what the function does and what it raises. A comment
  says why, not what the next line does.
- **Errors:** an expected failure raises `ValueError` or `OSError` with a message that
  names the source or file, so the command line can print it as one line.

## Tests

- A new behaviour comes with a test. A bug fix comes with a test that fails without the
  fix.
- The tests never touch the network. A local file or a local HTTP server stands in for
  a publisher. A test that needs a real service is marked `live` and runs only with
  `uv run pytest -m live`, which CI does weekly. Where a test stands in for a web service
  (Save Page Now), a contract test runs against both the stand-in and the real service, so
  the stand-in cannot drift unnoticed.
- The README's examples run as tests (`tests/test_docs.py`), so a change that breaks
  them fails.
- CI runs the suite on Python 3.12, 3.13, 3.14 and 3.15, and fails under 95% coverage
  of lines and branches. `just py-cov` shows the coverage locally.
- Code that reads outside input (the fetch log, response headers, parsed rows) gets a
  Hypothesis property test in `tests/test_properties.py` as well as examples.
- `just py-mutation` changes the code one small step at a time and checks that a test
  fails. A change no test notices is a survivor. It fails when a function has more surviving mutants than
  `tests/mutation-baseline.txt` lists. Kill a new survivor with a test, or, when the
  change cannot alter behaviour, rerun `just py-mutation-baseline` after a full run.
- CodSpeed runs `tests/test_benchmarks.py` on every pull request and reports how each
  benchmark changed against `main`. A change to parsing, `build`, `validate` or the fetch
  log should not slow them down unexplained. Without `--codspeed` each benchmark runs once
  as a plain test.

## Dependencies

sourceledger has no runtime dependencies. It reads CSV and JSON with the standard
library and fetches with `urllib`. Only the optional `xlsx` extra adds one package,
openpyxl. Keep it that way: a new runtime dependency needs an issue first, saying why
the standard library cannot do the job.

Development tools are in the `dev` dependency group. All versions are locked in
`uv.lock`, and CI installs with `uv sync --locked`. uv only resolves releases that are at
least a week old (`exclude-newer` in `pyproject.toml`), and Renovate waits the same week
before it proposes an update. A compromised release is usually caught and yanked in that
time. `uv audit` checks every locked package against the OSV vulnerability database in CI,
and a pull request that adds a dependency with a known advisory fails the dependency
review check.

## Write commits and pull requests

- **Commits** follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):
  `type(scope): summary`, in the imperative, lower case, with no period. Add a body only
  when the change needs one, with a bullet per logical chunk.
- **The pull request title** becomes the squash commit subject, so it follows the same rule.
- **Both are checked** against [`committed.toml`](../committed.toml): a commit-msg
  hook (installed by `prek install`) refuses a commit message that breaks the rule, and
  the `pull request title` check fails a pull request whose title does. Lower case isn't
  checked. A revert is titled `revert: <original subject>`: rename the
  `Revert "..."` title that GitHub's Revert button writes.
- **The pull request body** has the shape of
  [the pull request template](pull_request_template.md): what it fixes and why, three or
  four bullets, and a `Tests:` line.
- **The changelog** ([CHANGELOG.md](../CHANGELOG.md)) follows
  [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). Write each entry for
  the people who use the project, not as a commit subject. Mark breaking changes
  **Breaking**, and put upgrade steps first under the version.
- **The release notes** are that version's changelog section.

## Sign off your commits

Every commit in a pull request carries a `Signed-off-by:` line, which says you wrote the
change or have the right to submit it under this project's license: the
[Developer Certificate of Origin 1.1](https://developercertificate.org/). `git commit -s`
adds the line. To add it to commits you already made, run
`git rebase --signoff upstream/main` (the branch you started from) and force-push the branch.

The `dco` workflow checks every pull request from a fork and fails on a commit without the line. Pull requests from a branch in this repository skip it: pushing that branch already needed write access.

## Who maintains this

Simon van Lierde maintains sourceledger. [GOVERNANCE.md](../GOVERNANCE.md) says how
decisions are made and how a maintainer is added; new maintainers are named here.
