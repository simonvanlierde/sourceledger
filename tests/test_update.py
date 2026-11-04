"""Updates publish one complete snapshot and keep verified partial downloads for retry."""

import hashlib
import json
from pathlib import Path
from urllib.request import Request

import pytest

from sourceledger import ledger
from sourceledger.update import published, update


def _sources(tmp_path: Path) -> list[dict]:
    for name in ("one", "two"):
        (tmp_path / f"{name}.csv").write_text(f"value\n{name}\n", encoding="utf-8")
    return [
        {
            "id": "example",
            "status": "implemented",
            "format": "csv",
            "terms": "CC0",
            "terms_url": "https://example.org/terms",
            "license": "CC0-1.0",
            "redistribute": True,
            "request": {
                "url": tmp_path.as_uri() + "/{part}.csv",
                "filename": "example-{part}.csv",
                "expand": {"part": ["one", "two"]},
            },
        }
    ]


def test_update_failure_keeps_old_snapshot_and_retry_reuses_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    first = update(sources, root=root)
    assert published(root) == first
    (tmp_path / "one.csv").write_text("value\nchanged\n", encoding="utf-8")
    (tmp_path / "two.csv").unlink()
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    with pytest.raises(ledger.FetchError):
        update(sources, root=root)
    assert published(root) == first
    assert ledger.read_output(first / "processed/example-one.csv")[0]["value"] == "one"
    (tmp_path / "two.csv").write_text("value\nrecovered\n", encoding="utf-8")
    requested = []
    original = ledger._get  # noqa: SLF001  # count real transport attempts

    def track(source: ledger.Source, previous: ledger.FetchRecord | None, user_agent: str, folder: Path) -> object:
        requested.append(source["filename"])
        return original(source, previous, user_agent, folder)

    monkeypatch.setattr(ledger, "_get", track)
    second = update(sources, root=root)
    assert requested == ["example-two.csv"]
    assert second != first
    assert published(root) == second
    assert ledger.read_output(second / "processed/example-one.csv")[0]["value"] == "changed"
    assert ledger.validate(sources, raw=second / "raw", processed=second / "processed") == []


def test_incomplete_first_update_has_no_published_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    (tmp_path / "two.csv").unlink()
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    with pytest.raises(ledger.FetchError):
        update(sources, root=root)
    with pytest.raises(ValueError, match="no complete dataset"):
        published(root)


def test_parser_failure_and_disk_budget_never_publish(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    first = update(sources, root=root)

    def incomplete(_source: ledger.Source, _payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        msg = "publisher returned an incomplete page"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="incomplete page"):
        update(sources, root=root, parse=incomplete)
    assert published(root) == first
    with pytest.raises(ValueError, match="budget"):
        update(sources, root=root, max_bytes=1)
    assert published(root) == first


def test_partial_update_requires_matching_unselected_groups(tmp_path: Path):
    sources = _sources(tmp_path)
    other = {**sources[0], "id": "other", "url": (tmp_path / "one.csv").as_uri()}
    del other["request"]
    sources.append(other)
    root = tmp_path / "updates"
    with pytest.raises(ValueError, match="partial update"):
        update(sources, root=root, only=["example"])
    first = update(sources, root=root)
    old = (first / "processed/other.csv").read_bytes()
    second = update(sources, root=root, only=["example"])
    assert (second / "processed/other.csv").read_bytes() == old
    assert published(root) == second
    sources[1] = {**sources[1], "vintage": "changed"}
    with pytest.raises(ValueError, match="full update"):
        update(sources, root=root, only=["example"])


def test_pointer_rejects_unmarked_and_escaping_generations(tmp_path: Path):
    root = tmp_path / "updates"
    root.mkdir()
    for generation in ("../escape", "0" * 32):
        (root / "current.json").write_text(json.dumps({"generation": generation}), encoding="utf-8")
        with pytest.raises(ValueError, match=r"generation|no complete dataset"):
            published(root)


def test_retry_rechecks_corrupted_staged_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    (tmp_path / "two.csv").unlink()
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    with pytest.raises(ledger.FetchError):
        update(sources, root=root)
    attempt = json.loads((root / "attempt.json").read_text())
    staged = root / "generations" / attempt["generation"]
    (staged / "raw/example-one.csv").write_text("value\nforged\n", encoding="utf-8")
    (tmp_path / "two.csv").write_text("value\ntwo\n", encoding="utf-8")
    generation = update(sources, root=root)
    assert ledger.read_output(generation / "processed/example-one.csv")[0]["value"] == "one"


def test_interrupt_keeps_success_and_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    original = ledger._get  # noqa: SLF001  # interrupt the second real transport call
    requests = []

    def interrupted(
        source: ledger.Source, previous: ledger.FetchRecord | None, user_agent: str, folder: Path
    ) -> object:
        requests.append(source["filename"])
        if source["parameters"]["part"] == "two":
            raise KeyboardInterrupt
        return original(source, previous, user_agent, folder)

    monkeypatch.setattr(ledger, "_get", interrupted)
    with pytest.raises(KeyboardInterrupt):
        update(sources, root=root)
    monkeypatch.setattr(ledger, "_get", original)
    (tmp_path / "one.csv").unlink()
    generation = update(sources, root=root)
    assert requests == ["example-one.csv", "example-two.csv"]
    assert ledger.read_output(generation / "processed/example-one.csv")[0]["value"] == "one"


def test_changed_plan_removes_requests_without_mutating_old_snapshot(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    first = update(sources, root=root)
    sources[0]["request"]["expand"]["part"] = ["two"]
    second = update(sources, root=root)
    package = json.loads((second / "processed/datapackage.json").read_text())
    assert [resource["path"] for resource in package["resources"]] == ["example-two.csv"]
    assert not (second / "raw/example-one.csv").exists()
    assert (first / "raw/example-one.csv").is_file()
    assert "request removed" in "\n".join(ledger.revisions(first / "fetches.jsonl", log=second / "fetches.jsonl"))


@pytest.mark.parametrize("output_format", ["csv", "parquet"])
def test_output_budget_stops_writes_before_publication(tmp_path: Path, output_format: str):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    sources[0]["request"]["expand"]["part"] = ["one"]

    def huge_output(_source: ledger.Source, _payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
        return ["value"], [{"value": hashlib.shake_256(str(i).encode()).hexdigest(100)} for i in range(1000)]

    with pytest.raises(ValueError, match="budget"):
        update(sources, root=root, parse=huge_output, max_bytes=12_000, output_format=output_format)
    staged = next((root / "generations").iterdir())
    assert (staged / "raw/example-one.csv").is_file()
    assert not (root / "current.json").exists()
    assert not list(staged.rglob("*.part"))
    assert sum(path.stat().st_size for path in staged.rglob("*") if path.is_file()) <= 12_000


def test_update_lock_is_nonblocking(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    root.mkdir()
    with ledger._lock(root), pytest.raises(OSError, match="already running"):  # noqa: SLF001
        update(sources, root=root)


def test_publication_failure_keeps_previous_pointer_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib  # noqa: PLC0415  # package also exports the update function

    module = importlib.import_module("sourceledger.update")
    sources, root = _sources(tmp_path), tmp_path / "updates"
    first = update(sources, root=root)
    original = module._write_atomic  # noqa: SLF001  # inject the final publication failure

    def fail_pointer(path: Path, data: bytes) -> None:
        if path.name == "current.json":
            msg = "publication interrupted"
            raise OSError(msg)
        original(path, data)

    monkeypatch.setattr(module, "_write_atomic", fail_pointer)
    with pytest.raises(OSError, match="publication interrupted"):
        update(sources, root=root)
    assert published(root) == first
    monkeypatch.setattr(module, "_write_atomic", original)
    second = update(sources, root=root)
    assert published(root) == second
    assert second != first


@pytest.mark.parametrize("rotate_during_response", [False, True])
def test_update_retry_refreshes_legacy_credential_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rotate_during_response: bool
):
    import io  # noqa: PLC0415
    from email.message import Message  # noqa: PLC0415

    sources = _sources(tmp_path)
    original = sources[0]
    sources = [
        {
            **original,
            "id": name,
            "url": f"https://example.org/{name}.csv",
            "headers": {"Authorization": "env:UPDATE_TEST_CREDENTIAL"},
        }
        for name in ("one", "two")
    ]
    for source in sources:
        del source["request"]
    monkeypatch.setenv("UPDATE_TEST_CREDENTIAL", "first-account")
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    fail = True
    calls = []

    class Response(io.BytesIO):
        def __init__(self, url: str, value: str) -> None:
            body = f"value\n{value}\n".encode()
            super().__init__(body)
            self.url = url
            self.headers = Message()
            self.headers["Content-Length"] = str(len(body))

    def open_response(request: Request, timeout: float) -> Response:  # noqa: ARG001
        calls.append(request.full_url)
        if fail and request.full_url.endswith("two.csv"):
            msg = "publisher unavailable"
            raise OSError(msg)
        value = request.get_header("Authorization") or ""
        if rotate_during_response and fail and request.full_url.endswith("one.csv"):
            monkeypatch.setenv("UPDATE_TEST_CREDENTIAL", "second-account")
        return Response(request.full_url, value)

    monkeypatch.setattr(ledger, "_open", open_response)
    root = tmp_path / "updates"
    with pytest.raises(ledger.FetchError):
        update(sources, root=root)
    fail = False
    monkeypatch.setenv("UPDATE_TEST_CREDENTIAL", "second-account")
    generation = update(sources, root=root)
    assert calls.count("https://example.org/one.csv") == 2
    assert ledger.read_output(generation / "processed/one.csv")[0]["value"] == "second-account"
    monkeypatch.setenv("UPDATE_TEST_CREDENTIAL", "third-account")
    with pytest.raises(ValueError, match="full update"):
        update(sources, root=root, only=["two"])
    assert published(root) == generation


def test_update_rejects_generation_store_symlink(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "generations").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        update(sources, root=root)
    assert list(outside.iterdir()) == []


def test_update_recovers_missing_attempt_plan(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    root.mkdir()
    missing = "a" * 32
    (root / "generations" / missing).mkdir(parents=True)
    (root / "attempt.json").write_text(json.dumps({"generation": missing}), encoding="utf-8")
    generation = update(sources, root=root)
    assert generation.name != missing
    assert published(root) == generation


@pytest.mark.parametrize("max_bytes", [True, 0, -1, 1.5])
def test_update_refuses_invalid_budget_before_creating_files(tmp_path: Path, max_bytes: object):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    with pytest.raises(ValueError, match="max_bytes"):
        update(sources, root=root, max_bytes=max_bytes)  # ty: ignore[invalid-argument-type]  # deliberately invalid API input
    assert not root.exists()


@pytest.mark.parametrize("workers", [True, 0, 3])
def test_update_refuses_invalid_workers_before_creating_files(tmp_path: Path, workers: object):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    with pytest.raises(ValueError, match="workers"):
        update(sources, root=root, workers=workers)  # ty: ignore[invalid-argument-type]  # deliberately invalid API input
    assert not root.exists()


def test_update_validation_failure_does_not_publish(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    sources[0]["terms"] = ""
    with pytest.raises(ValueError, match="update is incomplete"):
        update(sources, root=root)
    assert not (root / "current.json").exists()


def test_published_rejects_damaged_completion_marker(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    generation = update(sources, root=root)
    (generation / "complete.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="completion marker"):
        published(root)


def test_first_publication_syncs_new_store_parent_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib  # noqa: PLC0415  # package also exports the update function

    module = importlib.import_module("sourceledger.update")
    sources, root = _sources(tmp_path), tmp_path / "nested/updates"
    original = module._sync_directory  # noqa: SLF001  # observe directory durability ordering
    synced = []

    def sync_directory(path: Path) -> None:
        synced.append(path)
        original(path)

    monkeypatch.setattr(module, "_sync_directory", sync_directory)
    update(sources, root=root)
    assert synced[:2] == [tmp_path, tmp_path / "nested"]


def test_corrupt_completion_marker_cannot_hide_staging_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    (tmp_path / "two.csv").unlink()
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    with pytest.raises(ledger.FetchError):
        update(sources, root=root)
    generation = next((root / "generations").iterdir())
    (generation / "residue").write_bytes(b"x" * 100_000)
    (generation / "complete.json").write_text("{}", encoding="utf-8")

    def unexpected_request(*_args: object) -> None:
        pytest.fail("staging quota must be checked before retrieval")

    monkeypatch.setattr(ledger, "_get", unexpected_request)
    with pytest.raises(ValueError, match="budget"):
        update(sources, root=root, max_bytes=10_000)


def test_retry_removes_orphaned_temporary_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    (tmp_path / "two.csv").unlink()
    monkeypatch.setattr(ledger, "_ATTEMPTS", 1)
    with pytest.raises(ledger.FetchError):
        update(sources, root=root)
    generation = next((root / "generations").iterdir())
    orphan = generation / "processed/.example-one.csv.deadbeef.part"
    orphan.write_bytes(b"interrupted output")
    (tmp_path / "two.csv").write_text("value\ntwo\n", encoding="utf-8")
    assert update(sources, root=root) == generation
    assert not orphan.exists()


def test_generation_directories_are_private(tmp_path: Path):
    sources, root = _sources(tmp_path), tmp_path / "updates"
    generation = update(sources, root=root)
    for path in (root, generation, generation / "raw", generation / "processed"):
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", ["plan.json", "complete.json"])
def test_completion_marker_rejects_symlinks_even_with_matching_digest(tmp_path: Path, name: str):
    import importlib  # noqa: PLC0415  # package also exports the update function

    module = importlib.import_module("sourceledger.update")
    sources, root = _sources(tmp_path), tmp_path / "updates"
    generation = update(sources, root=root)
    path = generation / name
    outside = tmp_path / name
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    assert not module._is_complete(generation)  # noqa: SLF001  # integrity includes local-file ownership
