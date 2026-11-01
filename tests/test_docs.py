"""The examples in the README and the docs page, run as written, with a local file for the publisher."""

import json
import re
import runpy
from importlib.metadata import version
from pathlib import Path

import pytest

from sourceledger import cli, ledger
from sourceledger.cli import main

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
REFERENCE = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
READERS = (ROOT / "docs" / "readers.md").read_text(encoding="utf-8")
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
    module.write_text(_block(READERS, "# Write a custom reader", "python"), encoding="utf-8")
    entry = runpy.run_path(str(module))["main"]
    assert [entry([stage]) for stage in ("fetch", "build", "validate")] == [0, 0, 0]


def test_every_schema_line_names_the_published_schema() -> None:
    """The docs show the line `init` writes: the schema at this version's tag. A release bumps both."""
    docs = [README, *[p.read_text(encoding="utf-8") for p in (ROOT / "docs").rglob("*.md")]]
    urls = [url for text in docs for url in re.findall(r"^#:schema (\S+)$", text, re.MULTILINE)]
    assert urls
    assert set(urls) == {SCHEMA["$id"].replace("/main/", f"/v{version('sourceledger')}/")}


def test_the_starter_example_is_the_quick_start_one() -> None:
    """`init` writes the README's example, commented out, so running the quick start covers both."""
    example = ledger._STARTER[ledger._STARTER.index("# [[source]]") :]  # noqa: SLF001
    assert "".join(line.removeprefix("# ") + "\n" for line in example.splitlines()) == _block(
        README, "## Quick start", "toml"
    )


def test_the_reference_names_every_stage_and_key() -> None:
    """A stage or manifest key added without docs fails here."""
    names = [*cli._STAGES, *SCHEMA["$defs"]["source"]["properties"]]  # noqa: SLF001
    assert [name for name in names if not re.search(rf"`{name}[` ]", REFERENCE)] == []


def test_the_website_revision_example_matches_the_pipeline(project: Path) -> None:
    """The browser's recorded example must agree with real fetch/build/validate results."""
    example = json.loads((ROOT / "docs" / "_data" / "revision.json").read_text())
    guide = (ROOT / "docs" / "getting-started.md").read_text()
    assert example["before_sha256"][:12] in guide
    assert example["after_sha256"] in guide
    upstream = project / "upstream" / "co2-annmean-mlo.csv"
    sources = ledger.load_manifest()
    upstream.write_text(example["before"], encoding="utf-8")
    (before,) = ledger.fetch(sources)
    ledger.build(sources)
    assert ledger.validate(sources) == []
    assert before.sha256 == example["before_sha256"]
    upstream.write_text(example["after"], encoding="utf-8")
    (after,) = ledger.fetch(sources)
    assert after.sha256 == example["after_sha256"]
    assert before.sha256 != after.sha256
    assert ledger.validate(sources) == [example["problem"]]
    ledger.build(sources)
    assert ledger.validate(sources) == []


def test_the_publisher_revision_tutorial_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tutorial creates its own input and manifest, then rebuilds after an edit."""
    guide = (ROOT / "docs" / "getting-started.md").read_text()
    monkeypatch.chdir(tmp_path)
    for heading in ("## Create a small source", "## Revise the publisher's file"):
        script = tmp_path / "example.py"
        script.write_text(_block(guide, heading, "python"))
        if "Revise" in heading:
            (tmp_path / "before.jsonl").write_bytes(Path("data/fetches.jsonl").read_bytes())
        runpy.run_path(str(script))
        if "Create" in heading:
            assert [main([stage]) for stage in ("fetch", "build", "validate")] == [0, 0, 0]
        else:
            assert [main([stage]) for stage in ("fetch", "validate")] == [0, 1]
            assert main(["revisions", "before.jsonl"]) == 0
            assert [main([stage]) for stage in ("build", "validate")] == [0, 0]
    assert ledger.read_output(Path("data/processed/co2.csv"))[-1]["co2_ppm"] == "419.32"
