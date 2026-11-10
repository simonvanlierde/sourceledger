"""Reading sources.toml: ids, formats, pins, headers and the other checks on a manifest."""

import hashlib
import json
import os
import re
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import pytest
from support import (
    FIXTURE,
    LOG,
    RAW,
    TERMS,
    WHEN,
    _lines,
    _manifest,
    _Response,
    _run,
    _serve,
    _source,
    builder_mod,
    fetcher_mod,
    report_mod,
)

from sourceledger import ledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main
from sourceledger.report import report

SCHEMA = json.loads((Path(ledger.__file__).parent / "sources.schema.json").read_text(encoding="utf-8"))


def test_a_toml_date_vintage_is_recorded_as_a_string(upstream: Path, tmp_path: Path) -> None:
    """An unquoted `vintage = 2024-06-01` loads as a date; the fetch record still holds text."""
    _manifest(tmp_path, upstream.as_uri(), terms="vintage = 2024-06-01")
    assert _run("fetch") == [0]
    record = json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert record["vintage"] == "2024-06-01"


def test_fetch_refuses_bytes_that_miss_the_pinned_sha256(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source changed upstream is refused at the door, not recorded as a new fetch."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{"0" * 64}"')
    assert _run("fetch") == [3]
    assert "not the sha256 in sources.toml" in capsys.readouterr().err
    assert not RAW.exists()
    good = hashlib.sha256(FIXTURE.encode()).hexdigest()
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{good}"')
    assert _run("fetch") == [0]


def test_a_new_sha256_pin_makes_the_refetch_unconditional(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 304 cannot vouch for bytes the manifest no longer pins, so they are fetched and checked again."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode(), ETag='"abc123"'), _Response(FIXTURE.encode()))
    _run("fetch")
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{"0" * 64}"')
    assert _run("fetch") == [3]
    assert calls[1].get_header("If-none-match") is None


@pytest.mark.parametrize("cap", ["0", "true", '"1 GB"'])
def test_max_bytes_must_be_a_positive_whole_number(
    upstream: Path, tmp_path: Path, cap: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cap that is not a byte count is a manifest error, not a silent 0- or 1-byte limit."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"max_bytes = {cap}")
    assert _run("fetch") == [3]
    assert "positive whole number" in capsys.readouterr().err


def test_an_empty_manifest_is_no_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest with no `[[source]]` yet is valid: nothing to fetch, build or check."""
    (tmp_path / "sources.toml").write_text("# no sources yet\n", encoding="utf-8", newline="")
    monkeypatch.chdir(tmp_path)
    assert manifest_mod.load_manifest() == []
    assert _run("fetch", "build", "validate") == [0, 0, 0]


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("[[sources]]\nid = 'a'\nstatus = 'implemented'\n", "source"),
        ("[[Source]]\nid = 'a'\nstatus = 'implemented'\n", "source"),
        ("output-format = 'parquet'\n", "output_format"),
        ("out-fmt = 'csv'\n", "output_format"),
    ],
)
def test_a_misspelt_top_level_key_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, hint: str
) -> None:
    """A typo must not read as an empty manifest, or build writes an empty datapackage.json over a good one."""
    (tmp_path / "sources.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=rf"not a sources.toml key.*did you mean {hint}"):
        manifest_mod.load_manifest()
    assert _run("build") == [3]
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(("key", "value"), [("output_format", '"parquet"'), ("release_formats", '["ro-crate"]')])
def test_a_project_key_inside_a_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    """Appended below the sources, the line belongs to the last [[source]], where it would do nothing."""
    text = f'[[source]]\nid = "a"\nstatus = "unimplemented"\n{key} = {value}\n'
    (tmp_path / "sources.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=rf"^source 'a' in sources.toml sets {key}; put it above"):
        manifest_mod.load_manifest()


@pytest.mark.parametrize("value", ['"ro-crate"', '["zip"]', '["croissant", "croissant"]', "[1]", "[[]]"])
def test_release_formats_lists_known_formats_once(tmp_path: Path, value: str) -> None:
    path = tmp_path / "sources.toml"
    path.write_text(f"release_formats = {value}\n", encoding="utf-8")
    message = r"^release_formats in sources.toml must list each of \['ro-crate', 'croissant'\] at most once$"
    with pytest.raises(ValueError, match=message):
        manifest_mod.load_manifest(path)
    with pytest.raises(ValueError, match=message):
        manifest_mod.load_release_formats(path)


def test_release_formats_is_empty_unless_set(tmp_path: Path) -> None:
    path = tmp_path / "sources.toml"
    path.write_text("", encoding="utf-8")
    assert manifest_mod.load_release_formats(path) == []
    path.write_text('release_formats = ["croissant", "ro-crate"]\n', encoding="utf-8")
    assert manifest_mod.load_release_formats(path) == ["croissant", "ro-crate"]


def test_an_unknown_top_level_key_with_no_close_match_gets_no_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sources.toml").write_text("zzz = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=r"'zzz' is not a sources\.toml key$"):
        manifest_mod.load_manifest()


def test_the_schema_closes_the_top_level() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["properties"]) == {"output_format", "release_formats", "source"}


@pytest.mark.usefixtures("upstream")
def test_the_manifest_rejects_a_repeated_id(capsys: pytest.CaptureFixture[str]) -> None:
    """Ids name the raw and output files, so a repeat would overwrite the first source's."""
    Path("sources.toml").write_text('[[source]]\nid = "a"\n' * 2, encoding="utf-8", newline="")
    assert _run("fetch") == [3]
    assert "appear more than once" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_a_source_id_is_a_plain_file_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An id names files under data/, so one that is a path is refused before anything is written."""
    manifest = tmp_path / "sources.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"example"', '"../escaped"'), encoding="utf-8")
    assert _run("fetch") == [3]
    assert "../escaped" in capsys.readouterr().err
    assert not Path("data").exists()


def test_a_sha256_pin_in_upper_case_matches(upstream: Path, tmp_path: Path) -> None:
    """Windows tools print hashes in upper case; the pin still matches the bytes."""
    pin = hashlib.sha256(FIXTURE.encode()).hexdigest().upper()
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsha256 = "{pin}"')
    assert _run("fetch", "build", "validate") == [0, 0, 0]


def test_a_sha256_pin_must_be_a_sha256(upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A truncated or mistyped pin is a manifest error, not a download that never matches."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nsha256 = "abc123"')
    assert _run("fetch") == [3]
    assert "64 hexadecimal characters" in capsys.readouterr().err


def test_the_old_key_release_is_refused_with_its_new_name(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kept as an unknown key, `release` would silently leave the vintage "not stated"."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'{TERMS}\nrelease = "2024"')
    assert _run("fetch") == [3]
    assert "`release` in sources.toml is now `vintage`" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["0", "-1", "true", '"30"', "nan", "inf", "-inf"])
@pytest.mark.usefixtures("upstream")
def test_invalid_source_timeouts_are_manifest_errors(tmp_path: Path, timeout: str) -> None:
    _manifest(tmp_path, "https://example.org/data.csv", terms=f"{TERMS}\ntimeout_s = {timeout}")
    with pytest.raises(ValueError, match=r"timeout_s.*positive finite number"):
        manifest_mod.load_manifest()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ('{ Authorization = "Bearer s3cret" }', "never write a secret"),
        ('{ User-Agent = "env:UA" }', "sets the User-Agent header itself"),
    ],
)
@pytest.mark.usefixtures("upstream")
def test_a_literal_or_reserved_header_is_a_manifest_error(
    tmp_path: Path, headers: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Secrets stay out of the manifest, and the fetch's own headers stay the fetch's."""
    _manifest(tmp_path, "https://example.invalid/data.csv", terms=f"{TERMS}\nheaders = {headers}")
    assert _run("fetch") == [3]
    assert expected in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
def test_headers_on_a_plain_http_url_are_a_manifest_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A token would cross the network in clear text from the first request."""
    _manifest(
        tmp_path, "http://example.invalid/data.csv", terms=f'{TERMS}\nheaders = {{ Authorization = "env:SL_TOKEN" }}'
    )
    assert _run("fetch") == [3]
    assert "example: headers in sources.toml need an https url" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize("url", ["https://user:secret@example.org/x.csv", "https://token@example.org/x.csv"])
def test_a_url_with_a_user_name_or_password_is_refused(tmp_path: Path, url: str) -> None:
    """The url goes into the log, the fetch record, datapackage.json and the RO-Crate, all published."""
    _manifest(tmp_path, url)
    with pytest.raises(ValueError, match=r"^example: the url in sources\.toml holds a user name or password; "):
        manifest_mod.load_manifest()


@pytest.mark.usefixtures("upstream")
def test_an_id_must_be_ascii(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An id names files, which must mean the same file on every system."""
    manifest = tmp_path / "sources.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"example"', '"exämple"'), encoding="utf-8")
    assert _run("fetch") == [3]
    assert "ASCII" in capsys.readouterr().err


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize("char", ["\\u001b", "\\u009b", "\\u007f", "\\u0000"])
def test_a_control_character_in_the_manifest_is_refused(tmp_path: Path, char: str) -> None:
    """An ESC or C1 byte in a url or name would reach the terminal raw and could rewrite it."""
    _manifest(tmp_path, f"https://example.invalid/{char}[31mdata.csv")
    with pytest.raises(ValueError, match=r"example: sources\.toml text may not hold a control character"):
        manifest_mod.load_manifest()


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize(
    "nested",
    [
        'headers = { "X-\\u001b[2J" = "env:TOKEN" }',
        'headers = { Authorization = "env:TOKEN\\u001b" }',
        'tags = ["ok", { note = "\\u009b" }]',
        '"key\\u001b" = 1',
    ],
)
def test_a_control_character_in_a_nested_table_or_key_is_refused(tmp_path: Path, nested: str) -> None:
    """A header name reaches stderr in fetch; nested tables, arrays and keys are text too."""
    _manifest(tmp_path, "https://example.invalid/data.csv", terms=f"{TERMS}\n{nested}")
    with pytest.raises(ValueError, match=r"example: sources\.toml text may not hold a control character"):
        manifest_mod.load_manifest()


@pytest.mark.usefixtures("upstream")
def test_line_breaks_and_tabs_in_manifest_text_are_allowed(tmp_path: Path) -> None:
    """Terms are often quoted over several lines."""
    _manifest(tmp_path, "https://example.invalid/data.csv", terms=f'{TERMS}\nname = "a\\tb\\r\\nc"')
    assert manifest_mod.load_manifest()[0]["name"] == "a\tb\r\nc"


_GOOD = f'[[source]]\nid = "a"\nstatus = "implemented"\nurl = "https://example.org/a.csv"\n{TERMS}\n'


@pytest.mark.parametrize(
    "text",
    [
        _GOOD,
        _GOOD + 'sha256 = "' + "A" * 64 + '"\nmax_bytes = 10\nformat = "json"\nsheet = "x"\nvintage = "2024"\n',
        _GOOD + 'headers = { Authorization = "env:TOKEN" }\ncolumn_map = "kept for the project\'s own reader"\n',
        _GOOD + "archive = true\n",
        _GOOD + 'format = "file"\n',
        *[_GOOD + f"timeout_s = {value}\n" for value in ["1", "0.5", "0", "true", '"1"']],
        _GOOD + 'archive = "yes"\n',
        '[[source]]\nid = "b"\nstatus = "unimplemented"\nreason = "later"\n',
        _GOOD.replace('id = "a"', 'id = "a/b"'),
        _GOOD.replace('status = "implemented"', 'status = "done"'),
        _GOOD.replace('license = "CC0-1.0"\n', ""),
        _GOOD.replace("redistribute = true", 'redistribute = "yes"'),
        _GOOD + 'sha256 = "abc"\n',
        _GOOD + "max_bytes = 0\n",
        _GOOD + 'format = "parquet"\n',
        _GOOD + 'headers = { Authorization = "Bearer x" }\n',
        _GOOD.replace("https://", "http://") + 'headers = { Authorization = "env:TOKEN" }\n',
        _GOOD.replace("https://", "HTTPS://") + 'headers = { Authorization = "env:TOKEN" }\n',
        _GOOD.replace('url = "https://example.org/a.csv"\n', ""),
        _GOOD + 'release = "2024"\n',
        'release_formats = ["ro-crate", "croissant"]\n' + _GOOD,
        "release_formats = []\n" + _GOOD,
        'release_formats = ["ro-crate", "ro-crate"]\n' + _GOOD,
        'release_formats = ["zip"]\n' + _GOOD,
        'release_formats = "ro-crate"\n' + _GOOD,
        # Appended below the last [[source]], so inside it, where both keys are refused.
        _GOOD + 'output_format = "csv"\n',
        _GOOD + "release_formats = []\n",
        _GOOD.replace("https://", "https://user:pw@"),
        _GOOD.replace("https://", "https://token@"),
        _GOOD.replace("a.csv", "a@2x.csv?by=me@host"),
    ],
)
def test_the_schema_and_the_loader_agree(tmp_path: Path, text: str) -> None:
    """An editor using the published schema accepts exactly the manifests sourceledger accepts."""
    jsonschema = pytest.importorskip("jsonschema")
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8", newline="")
    data = tomllib.loads(text)
    schema_ok = jsonschema.Draft202012Validator(SCHEMA).is_valid(data)
    try:
        sources = manifest_mod.load_manifest(path)
    except ValueError:
        loader_ok = False
    else:
        bad_status = [s for s in sources if s["status"] not in (manifest_mod.IMPLEMENTED, manifest_mod.UNIMPLEMENTED)]
        terms = [p for s in manifest_mod.implemented(sources) for p in report_mod._terms_problems(s)]  # noqa: SLF001
        loader_ok = not bad_status and not terms
    assert schema_ok == loader_ok


def test_init_writes_a_starter_manifest_and_never_overwrites_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The starter names this version's schema and loads as no sources. test_docs runs its example."""
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    manifest = Path("sources.toml").read_text(encoding="utf-8")
    tagged = SCHEMA["$id"].replace("/main/", f"/v{version('sourceledger')}/")
    assert manifest.splitlines()[0] == f"#:schema {tagged}"
    assert manifest_mod.load_manifest() == []
    capsys.readouterr()
    assert main(["init"]) == 3
    assert capsys.readouterr().err == (
        "error: sources.toml already exists; not overwriting it\n"
        "help: edit it, or pass --manifest PATH to write one elsewhere\n"
    )
    assert Path("sources.toml").read_text(encoding="utf-8") == manifest


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset does not exist on Windows")
@pytest.mark.usefixtures("upstream")
def test_a_fetch_record_is_written_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every field of a plain fetch, in UTC, and the record file as indented JSON."""
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()))
    zone = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-14"  # far from UTC, so a local timestamp shows
    time.tzset()
    try:
        assert main(["fetch"], user_agent="my-dataset") == 0
    finally:
        if zone is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = zone
        time.tzset()
    text = Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8")
    record = json.loads(text)
    assert text == json.dumps(record, indent=2) + "\n"
    retrieved = datetime.strptime(record.pop("retrieved_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - retrieved) < timedelta(minutes=1)
    assert record == {
        "source_id": "example",
        "url": manifest_mod.load_manifest()[0]["url"],
        "sha256": hashlib.sha256(FIXTURE.encode()).hexdigest(),
        "bytes": len(FIXTURE),
        "vintage": "not stated",
        "last_modified": "",
        "etag": "",
        "final_url": "",
        "env_headers": [],
        "archive_url": "",
    }
    assert (calls[0].get_header("User-agent"), calls[0].get_header("Accept-encoding")) == ("my-dataset", "identity")


@pytest.mark.usefixtures("upstream")
def test_the_fetch_build_and_init_lines_are_exact(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What each stage prints is part of the command line's contract."""
    assert _run("fetch", "build") == [0, 0]
    sha = hashlib.sha256(FIXTURE.encode()).hexdigest()
    url = manifest_mod.load_manifest()[0]["url"]
    assert capsys.readouterr().out.splitlines() == [
        f"example  new               29 B  {sha[:12]}  not stated  {url}",
        "2 rows -> data/processed/example.csv",
        "package -> data/processed/datapackage.json",
    ]
    (tmp_path / "fresh").mkdir()
    monkeypatch.chdir(tmp_path / "fresh")
    assert main(["init"]) == 0
    assert capsys.readouterr().out == "wrote sources.toml; fill in your sources, then run: sourceledger fetch\n"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            'format = "parquet"',
            "example: format in sources.toml must be one of ['csv', 'file', 'json', 'xlsx'], not 'parquet'",
        ),
        ('headers = "x"', "example: headers in sources.toml must be a table"),
        (
            'headers = { Authorization = "Bearer x" }',
            (
                'example: header Authorization must name an environment variable, as "env:NAME"; '
                "never write a secret into sources.toml"
            ),
        ),
    ],
)
def test_manifest_errors_are_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, expected: str) -> None:
    _manifest(tmp_path, "https://example.org/a.csv", terms=f"{TERMS}\n{extra}")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        manifest_mod.load_manifest()


def test_a_source_without_a_string_id_is_named_exactly(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.toml"
    manifest.write_text("[[source]]\nid = 1\n", encoding="utf-8", newline="")
    with pytest.raises(ValueError, match=r"^sources.toml: every \[\[source\]\] needs a string id$"):
        manifest_mod.load_manifest(manifest)


def test_max_bytes_of_one_is_allowed(tmp_path: Path) -> None:
    manifest = tmp_path / "sources.toml"
    manifest.write_text('[[source]]\nid = "a"\nstatus = "unimplemented"\nmax_bytes = 1\n', encoding="utf-8", newline="")
    assert manifest_mod.load_manifest(manifest)[0]["max_bytes"] == 1


@pytest.mark.usefixtures("upstream")
def test_a_new_vintage_error_is_exact(tmp_path: Path) -> None:
    _run("fetch")
    _manifest(tmp_path, manifest_mod.load_manifest()[0]["url"], terms=f'{TERMS}\nvintage = "2025"')
    with pytest.raises(
        ValueError, match=r"^example\.csv was fetched as vintage 'not stated', not '2025'; re-run fetch$"
    ):
        ledger.verified(manifest_mod.load_manifest()[0])


@pytest.mark.parametrize("value", ["2026-11-01", "03:04:05", "2026-11-01T03:04:05Z"])
def test_json_status_describes_non_text_status_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("sources.toml").write_text(f'[[source]]\nid = "example"\nstatus = {value}\n', encoding="utf-8")
    assert main(["status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sources"][0]["status"] == str(manifest_mod.load_manifest()[0]["status"])
    assert result["errors"] == []


def test_a_recorded_file_is_marked_in_the_log_package_and_report(upstream: Path) -> None:
    """Whoever reads the dataset can tell sourceledger did not download these bytes itself."""
    result = ledger.record(_source(), upstream, retrieved_at=WHEN, parameters={"country": "NL", "year": 2024})
    assert result.retrieved_at == "2026-11-05T09:30:00Z"
    line = json.loads(LOG.read_text(encoding="utf-8"))
    assert (line["recorded_at"], line["parameters"]) == (result.recorded_at, {"country": "NL", "year": "2024"})
    assert json.loads(Path(f"{RAW}.fetch.json").read_text(encoding="utf-8")) == line
    builder_mod.build(manifest_mod.load_manifest())
    package = json.loads(Path("data/processed/datapackage.json").read_text(encoding="utf-8"))
    assert package["resources"][0]["sourceledger"]["recorded_at"] == result.recorded_at
    html = report(manifest_mod.load_manifest())
    assert "<dt>Recorded at</dt>" in html
    assert "<dt>Request parameters</dt><dd><code>country=NL</code> <code>year=2024</code></dd>" in html
    assert "Fetched at" not in html


def test_a_crash_between_moving_the_file_and_logging_it_loses_nothing(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the record matches the raw file, build refuses it and the next record or fetch starts over."""
    fetcher_mod.fetch(manifest_mod.load_manifest())
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")

    def crash(*_: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_append", crash)
        with pytest.raises(KeyboardInterrupt):
            ledger.record(_source(), upstream, retrieved_at=WHEN)
    assert RAW.read_text(encoding="utf-8") == FIXTURE + "2022,3.5\n"
    with pytest.raises(ValueError, match="changed since it was fetched"):
        builder_mod.build(manifest_mod.load_manifest())
    assert not list(RAW.parent.glob("*.part"))
    ledger.record(_source(), upstream, retrieved_at=WHEN)
    assert len(_lines()) == 2
    assert builder_mod.build(manifest_mod.load_manifest())
