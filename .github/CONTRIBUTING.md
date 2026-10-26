# Contributing

Thank you for your interest in sourceledger. Bug reports, feature requests and pull
requests are welcome. Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first, and
report vulnerabilities as [SECURITY.md](SECURITY.md) describes, not in a public issue.

## Report a bug or ask for a feature

Open an [issue](https://github.com/simonvanlierde/sourceledger/issues). For a bug, say
what you did, what you expected and what happened, with the version you used.

## Set up and check

[Development](../README.md#development) in the README installs the tools, the dependencies
and the git hooks. `just check` runs the checks CI runs, and `just py-fmt` fixes and
formats.

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
  `uv run pytest -m live`, which CI does weekly.
- The README's examples run as tests (`tests/test_docs.py`), so a change that breaks
  them fails.
- CI runs the suite on Python 3.12, 3.13, 3.14 and 3.15, and fails under 95% coverage
  of lines and branches. `just py-cov` shows the coverage locally.

## Dependencies

sourceledger has no runtime dependencies. It reads CSV and JSON with the standard
library and fetches with `urllib`. Only the optional `xlsx` extra adds one package,
openpyxl. Keep it that way: a new runtime dependency needs an issue first, saying why
the standard library cannot do the job.

Development tools are in the `dev` dependency group. All versions are locked in
`uv.lock`, and CI installs with `uv sync --locked`. uv only resolves releases that are at
least a week old (`exclude-newer` in `pyproject.toml`), and Dependabot waits the same week
before it proposes an update. A compromised release is usually caught and yanked in that
time. `uv audit` checks every locked package against the OSV vulnerability database in CI,
and a pull request that adds a dependency with a known advisory fails the dependency
review check.

## Write commits and pull requests

- **Commits** follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):
  `type(scope): summary`, in the imperative, lower case, with no period. Add a body only
  when the change needs one, with a bullet per logical chunk.
- **The pull request title** becomes the squash commit subject, so it follows the same rule.
- **The pull request body** says what it fixes and why, in three or four bullets, and ends
  with a `Tests:` line.
- **The changelog** ([CHANGELOG.md](../CHANGELOG.md)) follows
  [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). Write each entry for
  the people who use the project, not as a commit subject. Mark breaking changes
  **Breaking**, and put upgrade steps first under the version.
- **The release notes** are that version's changelog section.

## Who maintains this

Simon van Lierde maintains sourceledger: reviews pull requests, merges them and
makes releases. The repository belongs to [simonvanlierde](https://github.com/simonvanlierde)
on GitHub. Only maintainers can merge to `main`. A new maintainer is added by an existing one, by name in this section.
If no maintainer can continue, the repository is archived with a note in the README,
and anyone may fork it under its license. [GOVERNANCE.md](../GOVERNANCE.md) says how
decisions are made.
