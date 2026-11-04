"""Finite requests retain logical grouping and per-response provenance."""

# ruff: noqa: SLF001  # exercise cache identities and stored-record validation

import contextlib
import dataclasses
import hashlib
import json
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from frictionless import Package
from hypothesis import given
from hypothesis import strategies as st

from sourceledger import build, expand_requests, fetch, ledger, read_output, revisions, validate
from sourceledger.report import report


def source(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "x",
        "status": "implemented",
        "format": "csv",
        "terms": "CC0",
        "terms_url": "https://example.org/terms",
        "license": "CC0-1.0",
        "redistribute": True,
        "request": request,
    }


def test_expanded_fetch_build_records_and_reports(tmp_path: Path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    for name in ["a", "b"]:
        (upstream / f"{name}.csv").write_text(f"value\n{name}\n", encoding="utf-8")
    sources = [
        source({"url": upstream.as_uri() + "/{part}.csv", "filename": "x-{part}.csv", "expand": {"part": ["a", "b"]}})
    ]
    raw, processed, log = tmp_path / "raw", tmp_path / "processed", tmp_path / "fetches.jsonl"
    records = fetch(sources, raw=raw, log=log, workers=2)
    assert [r.parameters for r in records] == [{"part": "a"}, {"part": "b"}]
    assert {r.source_id for r in records} == {"x"}
    assert len({r.request_id for r in records}) == 2
    paths = build(sources, raw=raw, processed=processed)
    assert [p.name for p in paths] == ["x-a.csv", "x-b.csv"]
    assert [read_output(p)[0]["value"] for p in paths] == ["a", "b"]
    assert [read_output(p)[0]["request_id"] for p in paths] == [r.request_id for r in records]
    assert validate(sources, raw=raw, processed=processed) == []
    assert "Integrity checks passed" in report(sources, processed=processed, log=log)
    package = json.loads((processed / "datapackage.json").read_text())
    assert [r["sourceledger"]["parameters"] for r in package["resources"]] == [{"part": "a"}, {"part": "b"}]
    assert Package(package, basepath=str(processed)).validate().valid


@pytest.mark.parametrize(
    "override",
    [
        {"expand": {"n": []}},
        {"expand": {"n": [1, 1]}},
        {"expand": {"n": [True]}},
        {"expand": {"n": {"start": 2, "stop": 1}}},
        {"expand": {"n": {"start": 1, "stop": 1001}}},
        {"values": {"n": "env:SECRET"}},
        {"values": {"n": 1}, "expand": {"n": [1]}},
        {"url": "https://example.org/{missing}"},
        {"url": "https://example.org/{n.foo}"},
        {"url": "https://example.org/{n!r}"},
        {"url": "https://example.org/{n:02}"},
        {"url": "https://example.org/{{n}}"},
        {"filename": "../x.csv"},
        {"filename": "CON.csv"},
        {"filename": "x."},
        {"filename": "x/n.csv"},
        {"filename": "é.csv"},
        {"method": "POST"},
        {"sha256": {}},
        {"sha256": {"x-1.csv": "x"}},
        {"sha256": {"x-1.csv": "a" * 64, "extra.csv": "a" * 64}},
        {"url": "https://user:secret@example.org/{n}"},
        {"values": {"unused": 1}},
    ],
)
def test_invalid_plans_fail_before_files(override: dict[str, Any]):
    base = {"url": "https://example.org/{n}", "filename": "x-{n}.csv", "expand": {"n": [1]}}
    with pytest.raises(ValueError, match=r"request|declarative"):
        expand_requests([source({**base, **override})])


def test_exact_pins_and_url_escaping():
    sources = [
        source(
            {
                "url": "https://example.org/BRA;DEU/{n}",
                "filename": "x.csv",
                "values": {"n": "a/b ?"},
                "sha256": {"x.csv": "A" * 64},
            }
        )
    ]
    resolved = expand_requests(sources)[0]
    assert resolved["url"] == "https://example.org/BRA;DEU/a%2Fb%20%3F"
    assert resolved["sha256"] == "a" * 64


@pytest.mark.parametrize(
    "filenames",
    [["X.csv", "x.csv"], ["x.csv", "x.csv.fetch.json"], ["x.csv", "x.json"], ["datapackage.json", "other.csv"]],
)
def test_collisions(filenames: list[str]):
    sources = [
        {**source({"url": "https://example.org/x", "filename": name}), "id": f"s{i}"}
        for i, name in enumerate(filenames)
    ]
    with pytest.raises(ValueError, match="collid"):
        expand_requests(sources)


def test_total_bound_and_only_preflight(tmp_path: Path):
    sources = [
        source(
            {
                "url": "https://example.org/{n}/{m}",
                "filename": "x-{n}-{m}.csv",
                "expand": {"n": {"start": 1, "stop": 100}, "m": {"start": 1, "stop": 11}},
            }
        )
    ]
    with pytest.raises(ValueError, match="1000"):
        fetch(sources, raw=tmp_path / "raw", log=tmp_path / "log")
    assert not (tmp_path / "raw").exists()
    assert fetch(sources, raw=tmp_path / "raw", log=tmp_path / "log", only=[]) == []


@given(st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=8, unique=True))
def test_identity_survives_reordering_and_renaming(values: list[int]):
    request = {"url": "https://example.org/{n}", "filename": "x-{n}.csv", "expand": {"n": values}}
    before = expand_requests([source(request)])
    after = expand_requests([source({**request, "filename": "renamed-{n}.json", "expand": {"n": values[::-1]}})])
    assert {s["parameters"]["n"]: s["request_id"] for s in before} == {
        s["parameters"]["n"]: s["request_id"] for s in after
    }
    assert all(len(s["request_id"]) == 64 for s in before)


def test_credential_rotation_and_private_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources = [source({"url": "https://example.org/x", "filename": "x.csv"})]
    sources[0]["headers"] = {"Authorization": "env:TEST_TOKEN"}
    monkeypatch.setenv("TEST_TOKEN", "first-secret")
    raw, log = tmp_path / "raw", tmp_path / "log"

    def get(_item: dict[str, Any], _previous: ledger.FetchRecord | None, *_args: object) -> ledger._Download:
        path = raw / ".download"
        payload = b"value\na\n"
        path.write_bytes(payload)
        headers = Message()
        headers["ETag"] = "first-secret"
        headers["Last-Modified"] = "first-secret"
        return ledger._Download(
            path,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            headers,
            "https://example.org/first-secret?token=first-secret",
        )

    monkeypatch.setattr(ledger, "_get", get)
    record = fetch(sources, raw=raw, log=log)[0]
    resolved = expand_requests(sources)[0]
    assert ledger._previous(resolved, raw) == record
    assert "first-secret" not in (raw / "x.csv.fetch.json").read_text()
    assert "first-secret" not in log.read_text()
    assert record.header_refs == {"Authorization": "env:TEST_TOKEN"}
    assert record.etag == record.last_modified == ""
    monkeypatch.setenv("TEST_TOKEN", "second-secret")
    assert ledger._previous(resolved, raw) is None
    monkeypatch.setenv("TEST_TOKEN", "first-secret")
    assert ledger._previous(resolved, raw) == record
    (raw / ".credentials" / "key").unlink()
    assert ledger._previous(resolved, raw) is None
    assert (raw / ".credentials").stat().st_mode & 0o077 == 0


def test_added_removed_request_revisions(tmp_path: Path):
    before, after = tmp_path / "before", tmp_path / "after"
    record = ledger.FetchRecord("x", "https://example.org", "2026-01-01", "a" * 64, 1, "v", request_id="b" * 64)
    before.write_text(json.dumps(dataclasses.asdict(record)) + "\n")
    after.write_text(json.dumps(dataclasses.asdict(dataclasses.replace(record, request_id="c" * 64))) + "\n")
    changes = ledger._revision_records(before, log=after)
    assert [c["change"] for c in changes] == ["added", "removed"]
    assert all(c["source_id"] == "x" for c in changes)
    assert "request added" in revisions(before, log=after)[0]
    assert "request removed" in revisions(before, log=after)[1]


def test_expansion_order_identity():
    sources = [
        {
            "id": "x",
            "status": "implemented",
            "request": {
                "url": "https://example.org/{a}/{b}",
                "filename": "x-{a}-{b}.csv",
                "expand": {"b": {"start": 1, "stop": 2}, "a": ["one", "two"]},
            },
        }
    ]
    resolved = expand_requests(sources)
    assert [s["filename"] for s in resolved] == ["x-one-1.csv", "x-one-2.csv", "x-two-1.csv", "x-two-2.csv"]
    assert len({s["request_id"] for s in resolved}) == 4
    assert all(s["id"] == "x" for s in resolved)


def test_published_wdi_manifest_matches_schema():
    import tomllib  # noqa: PLC0415  # parse the documentation fixture here

    from jsonschema import Draft202012Validator  # noqa: PLC0415

    manifest = Path(__file__).parents[1] / "docs/examples/declarative-wdi.toml"
    schema = json.loads(Path(ledger.__file__).with_name("sources.schema.json").read_text())
    content = tomllib.loads(manifest.read_text())
    Draft202012Validator(schema).validate(content)
    from sourceledger import load_manifest  # noqa: PLC0415

    resolved = expand_requests(load_manifest(manifest))
    assert len(resolved) == 5
    assert resolved[0]["parameters"]["indicator"] == "NY.GDP.PCAP.KD"
    assert "BRA;DEU;IND;KEN;NLD" in resolved[0]["url"]


def test_http_envelope_requires_project_completeness_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import http.server  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from sourceledger import published, update  # noqa: PLC0415

    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # HTTP handler API
            calls.append(self.path)
            envelope = [{"pages": 2 if "paged" in self.path else 1}, [{"value": "001"}]]
            payload = json.dumps(envelope).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # HTTP handler API
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        sources = [
            source(
                {
                    "url": base + "/{indicator}",
                    "filename": "x-{indicator}.json",
                    "expand": {"indicator": ["good", "paged"]},
                }
            )
        ]
        sources[0]["format"] = "json"

        def parse(_source: ledger.Source, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
            metadata, rows = json.loads(payload)
            if metadata["pages"] != 1:
                msg = "publisher response has multiple pages"
                raise ValueError(msg)
            return ["value"], rows

        root = tmp_path / "updates"
        with pytest.raises(ValueError, match="multiple pages"):
            update(sources, root=root, parse=parse)
        with pytest.raises(ValueError, match="no complete dataset"):
            published(root)
        assert calls == ["/good", "/paged"]
        monkeypatch.setitem(sources[0]["request"]["expand"], "indicator", ["good"])
        generation = update(sources, root=root, parse=parse)
        assert read_output(generation / "processed" / "x-good.csv")[0]["value"] == "001"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_update_cli_publishes_at_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from sourceledger import main, published  # noqa: PLC0415

    upstream = tmp_path / "publisher.csv"
    upstream.write_text("value\n001\n")
    manifest = tmp_path / "sources.toml"
    manifest.write_text(f"""[[source]]
id = "x"
status = "implemented"
url = "{upstream.as_uri()}"
terms = "CC0"
terms_url = "https://example.org/terms"
license = "CC0-1.0"
redistribute = true
""")
    monkeypatch.chdir(tmp_path)
    assert main(["update", "--root", "snapshots", "--workers", "2", "--max-bytes", "100000"]) == 0
    snapshot = published(Path("snapshots"))
    assert f"published -> {snapshot}" in capsys.readouterr().out
    assert read_output(snapshot / "processed" / "x.csv")[0]["value"] == "001"
    assert not Path("data/raw").exists()
    assert not Path("data/processed").exists()
    assert main(["update", "--root", "small", "--max-bytes", "1"]) == 1
    assert "budget" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["build", "--root", "x"], ["fetch", "--max-bytes", "1"]])
def test_update_options_refused_for_other_stages(args: list[str]):
    from sourceledger import main  # noqa: PLC0415

    with pytest.raises(SystemExit, match="2"):
        main(args)


def test_project_filename_metadata_does_not_change_legacy_paths(tmp_path: Path):
    upstream = tmp_path / "publisher.csv"
    upstream.write_text("value\n001\n")
    item = {**source({}), "url": upstream.as_uri(), "filename": "project-metadata"}
    del item["request"]
    raw, processed = tmp_path / "raw", tmp_path / "processed"
    fetch([item], raw=raw, log=tmp_path / "log")
    assert (raw / "x.csv").exists()
    assert build([item], raw=raw, processed=processed) == [processed / "x.csv"]


@pytest.mark.parametrize("redirect", ["secret%2ftoken", "secret%252Ftoken", "SECRET%2FTOKEN"])
def test_auth_redirects_and_errors_never_reflect_credentials(redirect: str):
    item = {"id": "x", "headers": {"Authorization": "env:TEST_TOKEN"}, "request_id": "a" * 64}
    assert ledger._safe_final_url(item, f"https://example.org/{redirect}") == ""
    assert redirect not in ledger._redact(item, f"response failed at https://example.org/{redirect}")


def test_private_key_creation_recovers_after_budget_and_corruption(tmp_path: Path):
    from sourceledger.budget import ACTIVE_BUDGET, StagingBudget  # noqa: PLC0415

    item = {"id": "x", "request_id": "a" * 64}
    token = ACTIVE_BUDGET.set(StagingBudget(1))
    try:
        with pytest.raises(ValueError, match="budget"):
            ledger._credential_digest(item, tmp_path)
    finally:
        ACTIVE_BUDGET.reset(token)
    key = tmp_path / ".credentials" / "key"
    assert not key.exists()
    first = ledger._credential_digest(item, tmp_path)
    key.write_bytes(b"broken")
    second = ledger._credential_digest(item, tmp_path)
    assert first != second
    assert len(key.read_bytes()) == 32
    assert not list(tmp_path.rglob("*.part"))


def test_request_limit_ignores_unimplemented_sources():
    planned = [source({"url": "https://example.org/x", "filename": "x.csv"})]
    unresolved = [{"id": f"unused{i}", "status": "unimplemented"} for i in range(1000)]
    assert len(expand_requests([*unresolved, *planned])) == 1001


def test_cache_symlinks_and_public_permissions_are_refused(tmp_path: Path):
    item = {"id": "x", "request_id": "a" * 64}
    ledger._save_credential_marker(item, tmp_path)
    credentials = tmp_path / ".credentials"
    marker = credentials / ("request-" + item["request_id"])
    other = tmp_path / "other"
    other.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(other)
    assert not ledger._credential_matches(item, tmp_path)
    marker.unlink()
    ledger._save_credential_marker(item, tmp_path)
    key = credentials / "key"
    key.chmod(0o644)
    with pytest.raises(ValueError, match="permissions 0600"):
        ledger._credential_digest(item, tmp_path)
    key.chmod(0o600)
    key.unlink()
    key.symlink_to(other)
    with pytest.raises(ValueError, match="file of its own"):
        ledger._credential_digest(item, tmp_path)
    credentials.chmod(0o755)
    with pytest.raises(ValueError, match="permissions 0700"):
        ledger._credential_digest(item, tmp_path)


def test_cli_only_bounds_selected_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from sourceledger import main  # noqa: PLC0415

    upstream = tmp_path / "publisher.csv"
    upstream.write_text("value\n001\n")
    (tmp_path / "sources.toml").write_text(f"""[[source]]
id = "fixed"
status = "implemented"
url = "{upstream.as_uri()}"

[[source]]
id = "large"
status = "implemented"
[source.request]
url = "https://example.org/{{n}}"
filename = "large-{{n}}.csv"
[source.request.expand]
n = {{ start = 1, stop = 1001 }}
""")
    monkeypatch.chdir(tmp_path)
    assert main(["fetch", "--only", "fixed"]) == 0
    assert Path("data/raw/fixed.csv").exists()
    assert not list(Path("data/raw").glob("large*"))


@pytest.mark.parametrize("workers", [1, 2])
def test_download_budget_stops_writes_across_workers(tmp_path: Path, workers: int):
    from sourceledger import FetchError, update  # noqa: PLC0415

    for name in ["a", "b"]:
        (tmp_path / f"{name}.csv").write_text("value\n" + "a" * 60000 + "\n")
    sources = [
        source({"url": tmp_path.as_uri() + "/{part}.csv", "filename": "x-{part}.csv", "expand": {"part": ["a", "b"]}})
    ]
    root = tmp_path / "updates"
    with pytest.raises((FetchError, ValueError), match="budget"):
        update(sources, root=root, workers=workers, max_bytes=100000)
    size = sum(path.stat().st_size for path in (root / "generations").rglob("*") if path.is_file())
    assert size <= 100000
    assert not (root / "current.json").exists()


@pytest.mark.parametrize(
    "override",
    [
        {"url": 3},
        {"filename": 3},
        {"url": "https://example.org/env:TOKEN/{n}"},
        {"url": "https://example.org/{{/{n}"},
        {"url": "https://example.org/}}/{n}"},
        {"values": []},
        {"expand": []},
        {"values": {"n": True}},
        {"unknown": "x"},
    ],
)
def test_template_type_and_literal_validation(override: dict[str, Any]):
    request = {"url": "https://example.org/{n}", "filename": "x-{n}.csv", "expand": {"n": [1]}}
    with pytest.raises(ValueError, match="request"):
        expand_requests([source({**request, **override})])


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [
        ({"start": 2024, "stop": 2024}, [2024]),
        ({"start": 2018, "stop": 2022}, list(range(2018, 2023))),
        (list(range(1000)), list(range(1000))),
    ],
)
def test_dimension_boundaries(dimension: object, expected: list[int]):
    from sourceledger.retrieval import _dimension  # noqa: PLC0415

    assert list(_dimension(dimension)) == expected


def test_writer_cannot_swallow_a_staging_budget_failure(tmp_path: Path):
    from sourceledger.budget import ACTIVE_BUDGET, StagingBudget  # noqa: PLC0415

    output = tmp_path / "output.csv"
    token = ACTIVE_BUDGET.set(StagingBudget(1))
    try:
        with (
            pytest.raises(ValueError, match="budget"),
            ledger._replacing(output) as stream,
            contextlib.suppress(ValueError),
        ):
            stream.write(b"too large")
    finally:
        ACTIVE_BUDGET.reset(token)
    assert not output.exists()
    assert not list(tmp_path.glob("*.part"))


def test_forged_resolved_request_cannot_escape_raw_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    upstream = tmp_path / "publisher.csv"
    upstream.write_text("value\n001\n")
    monkeypatch.setattr(ledger, "_open", lambda *_args, **_kwargs: pytest.fail("transport reached before preflight"))
    forged = {
        "id": "x",
        "status": "implemented",
        "url": upstream.as_uri(),
        "request_id": "a" * 64,
        "filename": "../escaped.csv",
    }
    with pytest.raises(ValueError, match="filename"):
        fetch([forged], raw=tmp_path / "raw", log=tmp_path / "log")
    assert not (tmp_path / "raw").exists()
    assert not (tmp_path / "escaped.csv").exists()
    assert not (tmp_path / "escaped.csv.fetch.json").exists()


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "not-a-digest"},
        {"request_id": True},
        {"filename": None},
        {"filename": "NUL.json"},
        {"filename": "x."},
    ],
)
def test_resolved_request_fields_are_validated_before_fetch(tmp_path: Path, override: dict[str, Any]):
    item = {
        "id": "x",
        "status": "implemented",
        "url": "https://example.org/data.csv",
        "request_id": "a" * 64,
        "filename": "x.csv",
        **override,
    }
    with pytest.raises(ValueError, match=r"identifier|filename"):
        fetch([item], raw=tmp_path / "raw", log=tmp_path / "log")
    assert not (tmp_path / "raw").exists()
