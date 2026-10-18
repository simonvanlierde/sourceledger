"""The examples in the README and the docs page, run as written, with a local file for the publisher."""

import json
import re
import runpy
from pathlib import Path

import pytest

from sourceledger import cli, ledger
from sourceledger.cli import main

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SCHEMA = json.loads((Path(ledger.__file__).parent / "sources.schema.json").read_text(encoding="utf-8"))
FIXTURE = "year,mean,unc\n2022,418.53,0.12\n2023,421.08,0.12\n"


def _block(text: str, heading: str, lang: str) -> str:
    """The first fenced `lang` block in the section under `heading`."""
    assert f"\n{heading}\n" in text, f"no heading {heading!r}"
    section = text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]
    match = re.search(rf"```{lang}\n(.*?)```", section, re.DOTALL)
    assert match, f"no {lang} block under {heading}"
    return match.group(1)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The quick start's sources.toml, its URL pointed at a local copy; the test runs in the project."""
    upstream = tmp_path / "upstream" / "co2-annmean-mlo.csv"
    upstream.parent.mkdir()
    upstream.write_text(FIXTURE, encoding="utf-8")
    manifest, count = re.subn(
        r'^url = ".*"$', f'url = "{upstream.as_uri()}"', _block(README, "## Quick start", "toml"), flags=re.MULTILINE
    )
    assert count == 1
    (tmp_path / "sources.toml").write_text(manifest, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.usefixtures("project")
def test_the_quick_start_runs() -> None:
    """The manifest the README starts with fetches, builds and validates."""
    assert [main([stage]) for stage in ("fetch", "build", "validate")] == [0, 0, 0]
    assert [row["mean"] for row in ledger.read_output(Path("data/processed/co2.csv"))] == ["418.53", "421.08"]


def test_the_own_reader_example_runs(project: Path) -> None:
    """The project entry point in "Your own readers" runs every stage with its reader."""
    module = project / "pipeline.py"
    module.write_text(_block(README, "## Your own readers", "python"), encoding="utf-8")
    entry = runpy.run_path(str(module))["main"]
    assert [entry([stage]) for stage in ("fetch", "build", "validate")] == [0, 0, 0]


def test_every_schema_line_names_the_published_schema() -> None:
    """A `#:schema` URL in the docs that drifted from the schema's $id would check nothing."""
    docs = [README, (ROOT / "docs" / "index.md").read_text(encoding="utf-8")]
    urls = [url for text in docs for url in re.findall(r"^#:schema (\S+)$", text, re.MULTILINE)]
    assert urls
    assert set(urls) == {SCHEMA["$id"]}


def test_the_starter_example_is_the_quick_start_one() -> None:
    """`init` writes the README's example, commented out, so running the quick start covers both."""
    example = ledger._STARTER[ledger._STARTER.index("# [[source]]") :]  # noqa: SLF001
    assert "".join(line.removeprefix("# ") + "\n" for line in example.splitlines()) == _block(
        README, "## Quick start", "toml"
    )


def test_the_readme_names_every_stage_and_key() -> None:
    """A stage or manifest key added without docs fails here."""
    names = [*cli._STAGES, *SCHEMA["$defs"]["source"]["properties"]]  # noqa: SLF001
    assert [name for name in names if not re.search(rf"`{name}[` ]", README)] == []
