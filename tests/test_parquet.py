"""Parquet keeps the table contract, provenance and package checks of CSV outputs."""

import io
import json
import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sourceledger import ledger
from sourceledger.cli import main


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "input.csv"
    source.write_text('code,value\n001,1.10\n002,""\n', encoding="utf-8")
    Path("sources.toml").write_text(
        f"""output_format = "parquet"
[[source]]
id = "example"
status = "implemented"
url = "{source.as_uri()}"
terms = "CC0-1.0"
terms_url = "https://example.org/terms"
license = "CC0-1.0"
redistribute = true
""",
        encoding="utf-8",
    )
    return source


def test_parquet_build_and_validate(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert [main([stage]) for stage in ("fetch", "build", "validate")] == [0, 0, 0]
    out = Path("data/processed/example.parquet")
    assert "2 rows -> data/processed/example.parquet" in capsys.readouterr().out
    table = pl.read_parquet(out)
    compressed = io.BytesIO()
    table.write_parquet(compressed, compression="zstd")
    assert out.read_bytes() == compressed.getvalue()
    assert table.schema == dict.fromkeys(
        ["source_id", "source_url", "retrieved_at", "vintage", "raw_sha256", "code", "value"], pl.String
    )
    rows = ledger.read_output(out)
    assert [(row["code"], row["value"]) for row in rows] == [("001", "1.10"), ("002", "")]
    assert {row["source_id"] for row in rows} == {"example"}
    package_path = Path("data/processed/datapackage.json")
    resource = json.loads(package_path.read_text())["resources"][0]
    assert resource["format"] == "parquet"
    assert resource["mediatype"] == "application/vnd.apache.parquet"
    assert "encoding" not in resource
    assert resource["path"] == out.name
    assert resource["schema"]["fields"] == [{"name": name, "type": "string"} for name in table.columns]
    before = out.read_bytes(), package_path.read_bytes()
    assert main(["build"]) == 0
    assert (out.read_bytes(), package_path.read_bytes()) == before
    assert main(["verify", "data/processed"]) == 0
    assert main(["report"]) == 0
    assert main(["validate", "--json", "--only", "example"]) == 0
    project.write_text("code,value\n003,2.0\n", encoding="utf-8")
    assert main(["fetch"]) == 0
    assert main(["validate"]) == 1
    assert "earlier fetch" in capsys.readouterr().err


@pytest.mark.parametrize("damage", [b"broken", b"", None])
@pytest.mark.usefixtures("project")
def test_parquet_damage_is_a_validation_problem(damage: bytes | None) -> None:
    assert main(["fetch"]) == main(["build"]) == 0
    out = Path("data/processed/example.parquet")
    if damage is None:
        table = pl.read_parquet(out).with_columns(pl.lit("wrong").alias("raw_sha256"))
        table.write_parquet(out)
    else:
        out.write_bytes(damage)
        with pytest.raises(ValueError, match=r"data/processed/example.parquet: not a readable Parquet table"):
            ledger.read_output(out)
    assert main(["validate", "--json"]) == 1


@pytest.mark.parametrize("value", ['"xml"', "true", "123", "[]"])
@pytest.mark.usefixtures("project")
def test_unknown_output_format_fails(value: str) -> None:
    manifest = Path("sources.toml")
    manifest.write_text(manifest.read_text().replace('"parquet"', value), encoding="utf-8")
    with pytest.raises(ValueError, match=r"^output_format must be 'csv' or 'parquet'$"):
        ledger.load_manifest()


@pytest.mark.usefixtures("project")
def test_missing_extra_has_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main(["fetch"]) == 0
    monkeypatch.setitem(sys.modules, "polars", None)
    with pytest.raises(ValueError, match=r"^Parquet needs the parquet extra: uv add 'sourceledger\[parquet\]'$"):
        ledger.build(ledger.load_manifest(), output_format="parquet")
    assert not Path("data/processed/example.parquet").exists()


@pytest.mark.usefixtures("project")
def test_parquet_checks_the_package_hash_even_when_provenance_is_current() -> None:
    assert main(["fetch"]) == main(["build"]) == 0
    out = Path("data/processed/example.parquet")
    pl.read_parquet(out).with_columns(pl.lit("changed").alias("value")).write_parquet(out)
    expected = (
        "data/processed/datapackage.json describes an earlier build of data/processed/example.parquet; re-run build"
    )
    assert ledger.validate(ledger.load_manifest(), output_format="parquet") == [expected]
    assert ledger.validate(ledger.load_manifest(), output_format="parquet", only=["example"]) == [expected]


def test_core_import_keeps_polars_unloaded() -> None:
    subprocess.run(
        [sys.executable, "-c", "import sourceledger, sys; assert 'polars' not in sys.modules"],
        check=True,
    )


@pytest.mark.usefixtures("project")
def test_parquet_partial_build_and_file_sources() -> None:
    assert main(["fetch"]) == main(["build"]) == 0
    sources = ledger.load_manifest()
    package = Path("data/processed/datapackage.json")
    before = package.read_bytes()
    assert ledger.build(sources, only=[], output_format="parquet") == []
    assert package.read_bytes() == before
    assert ledger.validate(sources, output_format="parquet") == []
    sources[0]["format"] = "file"
    ledger.build(sources, processed=Path("files"), output_format="parquet")
    assert Path("files/example.csv").read_bytes() == Path("input.csv").read_bytes()
    assert ledger.validate(sources, processed=Path("files"), output_format="parquet") == []


@pytest.mark.usefixtures("project")
def test_parquet_empty_table_has_an_explicit_schema() -> None:
    Path("input.csv").write_text("code,value\n", encoding="utf-8")
    assert main(["fetch"]) == main(["build"]) == 0
    frame = pl.read_parquet("data/processed/example.parquet")
    assert frame.height == 0
    assert frame.schema == dict.fromkeys(
        ["source_id", "source_url", "retrieved_at", "vintage", "raw_sha256", "code", "value"], pl.String
    )
    assert main(["validate"]) == 1


def test_parquet_refuses_extra_row_fields_without_overwriting(tmp_path: Path) -> None:
    out = tmp_path / "table.parquet"
    out.write_bytes(b"previous build")
    with pytest.raises(ValueError, match="not in the header"):
        ledger._write_rows({"id": "test"}, out, {}, ["x"], [{"x": "1", "extra": "2"}])  # noqa: SLF001
    assert out.read_bytes() == b"previous build"


def test_parquet_fills_missing_cells_and_protects_the_stamp(tmp_path: Path) -> None:
    out = tmp_path / "table.parquet"
    ledger._write_rows(  # noqa: SLF001
        {"id": "test"}, out, {"source_id": "test"}, ["x"], [{}, {"x": "1", "source_id": "wrong"}]
    )
    assert ledger.read_output(out) == [{"source_id": "test", "x": ""}, {"source_id": "test", "x": "1"}]


@pytest.mark.parametrize("output_format", ["csv", "parquet", "xml", True, []])
def test_output_format_schema_matches_manifest(tmp_path: Path, output_format: object) -> None:
    import jsonschema  # noqa: PLC0415

    manifest = tmp_path / "sources.toml"
    manifest.write_text(f"output_format = {json.dumps(output_format)}\n", encoding="utf-8")
    schema = json.loads(Path(ledger.__file__).with_name("sources.schema.json").read_text())
    accepted = jsonschema.Draft202012Validator(schema).is_valid({"output_format": output_format})
    try:
        ledger.load_manifest(manifest)
    except ValueError:
        assert not accepted
    else:
        assert accepted


@settings(deadline=None, max_examples=25)
@given(values=st.lists(st.text(alphabet=st.characters(codec="utf-8"), max_size=30), max_size=8))
def test_parquet_round_trips_text(values: list[str], tmp_path_factory: pytest.TempPathFactory) -> None:
    out = tmp_path_factory.mktemp("parquet") / "table.parquet"
    rows = [{"value": value} for value in values]
    ledger._write_rows({"id": "test"}, out, {"source_id": "test"}, ["value"], rows)  # noqa: SLF001
    assert ledger.read_output(out) == [{"source_id": "test", **row} for row in rows]
