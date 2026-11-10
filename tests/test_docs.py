"""The examples in the README and the docs page, run as written, with a local file for the publisher."""

import ast
import json
import re
import runpy
import shlex
import shutil
import signal
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from support import builder_mod, fetcher_mod, report_mod

from sourceledger import cli, ledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
REFERENCE = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
RECIPES = (ROOT / "docs" / "recipes.md").read_text(encoding="utf-8")
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
    upstream.write_text(FIXTURE, encoding="utf-8", newline="")
    manifest, count = re.subn(
        r'^url = ".*"$', f'url = "{upstream.as_uri()}"', _block(README, "## Quick start", "toml"), flags=re.MULTILINE
    )
    assert count == 1
    (tmp_path / "sources.toml").write_text(manifest, encoding="utf-8", newline="")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.usefixtures("project")
def test_the_quick_start_runs() -> None:
    """The manifest the README starts with fetches, builds and validates."""
    assert [main([stage]) for stage in ("fetch", "build", "validate")] == [0, 0, 0]
    assert [row["mean"] for row in builder_mod.read_output(Path("data/processed/co2.csv"))] == ["418.53", "421.08"]


def test_the_own_reader_example_runs(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The project entry point in "Your own readers" runs every stage with its reader."""
    module = project / "pipeline.py"
    module.write_text(_block(RECIPES, "## Write a custom reader", "python"), encoding="utf-8", newline="")
    entry = runpy.run_path(str(module))["main"]
    codes = []
    for stage in ("fetch", "build", "validate"):
        monkeypatch.setattr(sys, "argv", ["my-dataset", stage])
        monkeypatch.setattr(signal, "signal", lambda *_: None)
        with pytest.raises(SystemExit) as exit_:
            entry()
        codes.append(exit_.value.code)
    assert codes == [0, 0, 0]


def test_every_schema_line_names_the_published_schema() -> None:
    """The docs show the line `init` writes: the schema at this version's tag. A release bumps both."""
    docs = [README, *[p.read_text(encoding="utf-8") for p in (ROOT / "docs").rglob("*.md")]]
    urls = [url for text in docs for url in re.findall(r"^#:schema (\S+)$", text, re.MULTILINE)]
    assert urls
    assert set(urls) == {SCHEMA["$id"].replace("/main/", f"/v{version('sourceledger')}/")}


def test_the_starter_example_is_the_quick_start_one() -> None:
    """`init` writes the README's example, commented out, so running the quick start covers both."""
    example = manifest_mod._STARTER[manifest_mod._STARTER.index("# [[source]]") :]  # noqa: SLF001
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
    sources = manifest_mod.load_manifest()
    upstream.write_text(example["before"], encoding="utf-8", newline="")
    (before,) = fetcher_mod.fetch(sources)
    builder_mod.build(sources)
    assert report_mod.validate(sources) == []
    assert before.sha256 == example["before_sha256"]
    upstream.write_text(example["after"], encoding="utf-8", newline="")
    (after,) = fetcher_mod.fetch(sources)
    assert after.sha256 == example["after_sha256"]
    assert before.sha256 != after.sha256
    assert report_mod.validate(sources) == [example["problem"]]
    builder_mod.build(sources)
    assert report_mod.validate(sources) == []


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
    assert list(builder_mod.read_output(Path("data/processed/co2.csv")))[-1]["co2_ppm"] == "419.32"
    assert _shell(_block(guide, "## Record a file another tool downloaded", "sh")) == [0] * 5
    rows = list(builder_mod.read_output(Path("data/processed/co2.csv")))
    assert {row["retrieved_at"] for row in rows} == {"2026-11-05T09:30:00Z"}
    assert _shell(_block(guide, "## Release the dataset", "sh")) == [0, 0]
    assert sorted(p.relative_to("release-v1").as_posix() for p in Path("release-v1").rglob("*.*")) == [
        "data/co2.csv",
        "datapackage.json",
    ]
    assert report_mod.verify(Path("release-v1")) == []


def _shell(script: str) -> list[int]:
    """Run a docs shell block's mkdir, cp and sourceledger lines in-process; the exit code of each."""
    codes = []
    for line in script.splitlines():
        command, *args = shlex.split(line)
        if command == "mkdir":
            Path(*args).mkdir()
            codes.append(0)
        elif command == "cp":
            shutil.copy(*args)
            codes.append(0)
        else:
            assert command == "sourceledger", line
            codes.append(main(args))
    return codes


def test_every_recipe_is_python_that_parses() -> None:
    """The recipes for tools the tests do not install must at least be valid Python."""
    blocks = re.findall(r"```python\n(.*?)```", RECIPES, re.DOTALL)
    assert len(blocks) == 5
    for block in blocks:
        ast.parse(block)


def test_the_own_fetcher_recipe_records_each_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop downloads with urllib, here from local files, and records one source per country."""
    monkeypatch.chdir(tmp_path)
    entries = []
    for country in ("nl", "be"):
        upstream = tmp_path / "upstream" / f"{country}.csv"
        upstream.parent.mkdir(exist_ok=True)
        upstream.write_text(f"year,people\n2024,{len(country)}\n", encoding="utf-8", newline="")
        entries.append(
            f'[[source]]\nid = "population-{country}"\nstatus = "implemented"\nurl = "{upstream.as_uri()}"\n'
            'terms = "CC0"\nterms_url = "https://example.org"\nlicense = "CC0-1.0"\nredistribute = true\n'
        )
    Path("sources.toml").write_text("\n".join(entries), encoding="utf-8", newline="")
    exec(_block(RECIPES, "### Your own fetcher", "python"), {})  # noqa: S102  # the recipe, run as written
    records = [json.loads(line) for line in Path("data/fetches.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["parameters"] for r in records] == [{"country": "nl"}, {"country": "be"}]
    assert all(r["recorded_at"] and r["retrieved_at"] for r in records)

    exec(_block(RECIPES, "## Publish a build only when it validates", "python"), {})  # noqa: S102
    assert sorted(p.name for p in Path("data/published").iterdir()) == [
        "datapackage.json",
        "population-be.csv",
        "population-nl.csv",
    ]
    assert report_mod.verify(Path("data/published")) == []
