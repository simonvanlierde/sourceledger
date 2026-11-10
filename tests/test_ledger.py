"""The fetch log, the sidecar records and `record()`: what a raw file is checked against."""

import contextlib
import errno
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from support import (
    _DATA,
    _SNAPSHOT,
    FIXTURE,
    LOG,
    RAW,
    TERMS,
    WHEN,
    _lines,
    _manifest,
    _peak,
    _record,
    _Response,
    _run,
    _serve,
    _source,
    fetch_mod,
    report_mod,
)

from sourceledger import ledger
from sourceledger import manifest as manifest_mod
from sourceledger.cli import main


def test_revisions_names_a_source_whose_bytes_changed(
    upstream: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The weekly report lists a revised source, and neither an unchanged one nor one new to the log."""
    _run("fetch")
    before = tmp_path / "before.jsonl"
    before.write_text(LOG.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["revisions"])
    capsys.readouterr()
    assert main(["revisions", str(before)]) == 0
    assert capsys.readouterr().out == "no revisions\n"

    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    _run("fetch")
    capsys.readouterr()
    assert main(["revisions", str(before)]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    old, new = (hashlib.sha256(t.encode()).hexdigest()[:12] for t in (FIXTURE, FIXTURE + "2022,3.5\n"))
    assert line.startswith(f"- `example`: {old} -> {new}, ")

    before.write_text("", encoding="utf-8", newline="")
    capsys.readouterr()
    assert main(["revisions", str(before)]) == 0
    assert capsys.readouterr().out == "no revisions\n"


def test_a_cut_off_last_log_line_is_dropped_not_fatal(upstream: Path, tmp_path: Path) -> None:
    """An interrupted append leaves half a line; the next fetch drops it and carries on."""
    _run("fetch")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write('{"source_id": "exa')
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    before = tmp_path / "before.jsonl"
    before.write_text(LOG.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["revisions", str(before)]) == 0
    assert _run("fetch") == [0]
    lines = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2


def test_a_whole_last_log_line_without_its_newline_is_kept(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An editor or a merge can drop the final newline; the record is history, not a cut-off append."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", 4)  # find the line start across several reads
    _run("fetch")
    first = LOG.read_text(encoding="utf-8")
    LOG.write_text(first.removesuffix("\n"), encoding="utf-8", newline="")
    upstream.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    assert _run("fetch") == [0]
    lines = LOG.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0] == first
    assert "dropping" not in caplog.text


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": 1},
        {"source_id": "a", "retrieved_at": "t", "sha256": "0" * 64, "bytes": 1},
        {"source_id": "a", "url": "u", "sha256": "0" * 64, "bytes": 1},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0", "bytes": 1},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": 123, "bytes": 1},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0" * 64},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": True},
        {"source_id": "a", "url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": "1"},
    ],
)
def test_a_log_line_missing_a_field_or_of_the_wrong_type_is_not_a_record(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    """Revisions print the size, time and URL, so a line without them is reported, not a KeyError."""
    before, log = tmp_path / "before.jsonl", tmp_path / "log.jsonl"
    before.write_text(_record("a", "1") + "\n", encoding="utf-8", newline="")
    log.write_text(json.dumps(fields) + "\n", encoding="utf-8", newline="")
    with pytest.raises(ValueError, match=r"log\.jsonl:1 is not a fetch record$"):
        ledger.revisions(before, log=log)


@pytest.mark.usefixtures("upstream")
def test_a_bad_line_inside_the_log_is_an_error_that_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    """Only a cut-off last line is forgiven: damage earlier in the history is reported."""
    LOG.parent.mkdir(parents=True)
    LOG.write_text('not json\n{"source_id": "x", "sha256": "0"}\n', encoding="utf-8", newline="")
    assert _run("fetch") == [3]
    assert "fetches.jsonl:1 is not a fetch record" in capsys.readouterr().err


def test_revisions_needs_the_earlier_log_to_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing copy is an error, not an empty report; and revisions needs no sources.toml."""
    monkeypatch.chdir(tmp_path)
    assert main(["revisions", "missing.jsonl"]) == 3
    assert "missing.jsonl is missing" in capsys.readouterr().err
    (tmp_path / "before.jsonl").write_text("", encoding="utf-8", newline="")
    assert main(["revisions", "before.jsonl"]) == 3
    assert "data/fetches.jsonl is missing" in capsys.readouterr().err
    LOG.parent.mkdir()
    LOG.write_text("", encoding="utf-8", newline="")
    assert main(["revisions", "before.jsonl"]) == 0


@pytest.mark.usefixtures("upstream")
def test_a_symlink_where_the_temp_file_would_go_is_not_followed(tmp_path: Path) -> None:
    """Temp files get fresh names, so a planted link can't redirect a write."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8", newline="")
    RAW.parent.mkdir(parents=True)
    RAW.with_name(RAW.name + ".part").symlink_to(victim)
    assert _run("fetch") == [0]
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert RAW.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.usefixtures("upstream")
def test_a_symlinked_lock_file_leaves_its_target_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A checkout can carry a symlink at data/raw/.lock; taking the lock must not empty the file it points at."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8", newline="")
    RAW.parent.mkdir(parents=True)
    try:
        (RAW.parent / ".lock").symlink_to(victim)
    except OSError:  # Windows without Developer Mode
        pytest.skip("this host cannot make symlinks")
    assert _run("fetch") == [3]
    assert "data/raw/.lock" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "keep me"
    (RAW.parent / ".lock").unlink()
    (RAW.parent / ".lock").write_text("held before", encoding="utf-8", newline="")
    assert _run("fetch") == [0]
    assert (RAW.parent / ".lock").read_text(encoding="utf-8") == "held before"


@pytest.mark.skipif(sys.platform == "win32", reason="patches fcntl")
def test_another_lock_error_is_not_reported_as_a_running_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(ledger.fcntl, "flock", fail)
    with pytest.raises(OSError, match="no locks available") as caught, ledger._lock(tmp_path):  # noqa: SLF001
        pass
    assert "already running" not in str(caught.value)


@pytest.mark.usefixtures("upstream")
def test_a_second_fetch_at_once_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """Two runs interleaving their writes could pair one run's raw file with the other's record."""
    RAW.parent.mkdir(parents=True)
    with ledger._lock(RAW.parent):  # noqa: SLF001
        assert _run("fetch") == [3]
    assert "another fetch is already running" in capsys.readouterr().err
    assert _run("fetch") == [0]


@pytest.mark.usefixtures("upstream")
def test_temp_files_a_killed_run_left_are_swept_under_the_lock(tmp_path: Path) -> None:
    """A SIGKILL mid-download or mid-record leaves its temp file; the next run that holds the lock removes it."""
    RAW.parent.mkdir(parents=True)
    stale = [RAW.parent / ".example.abc123.part", RAW.parent / ".example.csv.fetch.json.def456.part"]
    for path in stale:
        path.write_bytes(b"half a file")
    kept = RAW.parent / "notes.part"  # not a temp file sourceledger names
    kept.write_text("mine", encoding="utf-8")
    assert _run("fetch") == [0]
    assert not any(path.exists() for path in stale)
    assert kept.read_text(encoding="utf-8") == "mine"
    (RAW.parent / ".example.ghi789.part").write_bytes(b"half a file")
    download = tmp_path / "download.csv"
    download.write_text(FIXTURE, encoding="utf-8")
    ledger.record(manifest_mod.load_manifest()[0], download)
    assert not list(RAW.parent.glob(".*.part"))


@pytest.mark.usefixtures("upstream")
def test_a_refused_fetch_leaves_temp_files_alone(capsys: pytest.CaptureFixture[str]) -> None:
    """Another run holds the lock, so its temp file may still be in use: only the lock holder sweeps."""
    RAW.parent.mkdir(parents=True)
    other = RAW.parent / ".x.part"
    with ledger._lock(RAW.parent):  # noqa: SLF001
        other.write_bytes(b"being written")
        assert _run("fetch") == [3]
    assert "another fetch is already running" in capsys.readouterr().err
    assert other.exists()


def test_fetch_error_reads_as_its_failures() -> None:
    """A library caller that prints the error sees one line per failed source."""
    assert str(ledger.FetchError([], ["a: down", "b: gone"])) == "a: down\nb: gone"


@pytest.mark.usefixtures("upstream")
def test_a_cut_off_line_after_several_records_drops_only_itself(caplog: pytest.LogCaptureFixture) -> None:
    """The log is truncated at its last newline, so every complete record before it stays."""
    LOG.parent.mkdir(parents=True)
    old = [_record(s, "0") for s in ("x", "y")]
    LOG.write_text("\n".join(old) + '\n{"source_id": "exa', encoding="utf-8", newline="")
    assert _run("fetch") == [0]
    lines = LOG.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == old
    assert len(lines) == 3
    assert "dropping a last line" in caplog.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{_record('a', '1')}\n\n{_record('b', '2')}\n", {"a": "1" * 64, "b": "2" * 64}),
        (f'{_record("a", "1")}\n{{"source_id": "a"', {"a": "1" * 64}),
        (f"not json\n{_record('a', '1')}", "log.jsonl:1 is not a fetch record"),
        (f"{_record('a', '1')}\nnot json\n", "log.jsonl:2 is not a fetch record"),
        (f"{_record('a', '1')}\rnot json\r", {"a": "1" * 64}),
    ],
)
def test_the_log_reader_skips_blank_lines_and_forgives_only_a_cut_off_end(
    tmp_path: Path, text: str, expected: dict[str, str] | str
) -> None:
    """A blank line is skipped, a cut-off last line is dropped, and junk anywhere else is an error."""
    log = tmp_path / "log.jsonl"
    log.write_text(text, encoding="utf-8", newline="")
    if isinstance(expected, dict):
        assert ledger.logged_versions(log) == expected
    else:
        with pytest.raises(ValueError, match=expected):
            ledger.logged_versions(log)


@pytest.mark.usefixtures("upstream")
def test_verified_gives_the_record_and_bytes_and_refuses_an_edited_raw_file() -> None:
    (record,) = fetch_mod.fetch(manifest_mod.load_manifest())
    source = manifest_mod.load_manifest()[0]
    assert ledger.verified(source) == (record, FIXTURE.encode())
    assert ledger.verified(source, raw=RAW.parent) == (record, FIXTURE.encode())
    RAW.write_text("edited\n", encoding="utf-8", newline="")
    with pytest.raises(ValueError, match=r"^example\.csv changed since it was fetched; re-run fetch, or record"):
        ledger.verified(source)


@pytest.mark.usefixtures("upstream")
def test_temp_files_go_beside_their_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never in the system temp folder: a rename from another disk is not atomic, or fails."""
    monkeypatch.setattr(tempfile, "tempdir", "/nonexistent-temp-folder")
    assert _run("fetch", "build", "validate") == [0, 0, 0]


def test_a_failed_write_raises_its_own_error_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="No space left"):
        ledger._write_atomic(tmp_path / "out.csv", b"x")  # noqa: SLF001
    assert list(tmp_path.iterdir()) == []


def test_revisions_raises_a_standard_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as error:
        ledger.revisions(tmp_path / "missing.jsonl")
    assert (error.value.errno, error.value.strerror) == (errno.ENOENT, os.strerror(errno.ENOENT))


@pytest.mark.usefixtures("upstream")
def test_record_keeps_the_snapshot_of_the_same_bytes_and_asks_for_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As fetch does for bytes it downloads again; record makes no request, so new bytes get no snapshot."""
    _manifest(tmp_path, _DATA, terms=f"{TERMS}\narchive = true")
    calls = _serve(monkeypatch, _Response(FIXTURE.encode()), _Response(b"", url=_SNAPSHOT))
    assert _run("fetch") == [0]
    download = tmp_path / "download.csv"
    download.write_text(FIXTURE, encoding="utf-8", newline="")
    source = manifest_mod.load_manifest()[0]
    assert ledger.record(source, download).archive_url == _SNAPSHOT
    download.write_text(FIXTURE + "2022,3.5\n", encoding="utf-8", newline="")
    assert ledger.record(source, download).archive_url == ""
    assert len(calls) == 2
    assert [json.loads(line)["archive_url"] for line in LOG.read_text(encoding="utf-8").splitlines()] == [
        _SNAPSHOT,
        _SNAPSHOT,
        "",
    ]


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize("count", [100, 1000])
def test_cli_fetch_reads_history_once(count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    assert main(["fetch"]) == 0
    LOG.write_text((_record("example", "0") + "\n") * count, encoding="utf-8", newline="")
    reads: list[Path] = []
    original = ledger._last_records  # noqa: SLF001
    monkeypatch.setattr(fetch_mod, "_last_records", lambda log: reads.append(log) or original(log))
    assert main(["fetch"]) == 0
    assert reads == [LOG]


@pytest.mark.parametrize("count", [1000, 10000])
def test_history_memory_tracks_sources_instead_of_versions(count: int, tmp_path: Path) -> None:
    log = tmp_path / "history.jsonl"
    line = _record("example", "0") + "\n"
    log.write_text(line * count, encoding="utf-8", newline="")
    assert _peak(lambda: ledger.logged_versions(log)) < 256 * 1024
    assert ledger.logged_versions(log) == {"example": "0" * 64}


def test_a_bad_cr_terminated_history_batch_is_not_an_interrupted_tail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = tmp_path / "history.jsonl"
    log.write_bytes((" " * 32_768 + "{\r" + _record("a", "1") + "\n").encode())
    with pytest.raises(ValueError, match=r":1 is not a fetch record"):
        ledger._last_records(log)  # noqa: SLF001
    assert "skipping" not in caplog.text


def test_history_keeps_latest_records_across_batches(tmp_path: Path) -> None:
    log = tmp_path / "history.jsonl"
    log.write_text(
        (_record("a", "1") + "\n") * 1000 + _record("a", "2") + "\n" + _record("b", "3") + "\n", encoding="utf-8"
    )
    assert ledger._last_records(log) == {"a": json.loads(_record("a", "2")), "b": json.loads(_record("b", "3"))}  # noqa: SLF001


def test_history_replaces_invalid_utf8_in_a_record(tmp_path: Path) -> None:
    log = tmp_path / "history.jsonl"
    log.write_bytes((_record("a", "1") + "\n").encode().replace(b'"url": "u"', b'"url": "\xff"'))
    record = json.loads(_record("a", "1"))
    record["url"] = "\ufffd"
    assert ledger._last_records(log) == {"a": record}  # noqa: SLF001


def test_record_takes_a_download_time_up_to_the_clock_skew_ahead(upstream: Path, frozen_clock: datetime) -> None:
    """A tool's clock may run a little ahead of this host's: five minutes is accepted, a second more is not."""
    ahead = frozen_clock + timedelta(minutes=5)
    assert ledger.record(_source(), upstream, retrieved_at=ahead).retrieved_at == f"{ahead:%Y-%m-%dT%H:%M:%SZ}"
    with pytest.raises(ValueError, match="in the future"):
        ledger.record(_source(), upstream, retrieved_at=ahead + timedelta(seconds=1))


def test_record_takes_a_timezone_aware_datetime(upstream: Path) -> None:
    when = datetime(2026, 11, 5, 10, 30, tzinfo=UTC) + timedelta(microseconds=5)
    assert ledger.record(_source(), upstream, retrieved_at=when).retrieved_at == "2026-11-05T10:30:00Z"


def test_the_last_log_line_and_the_sidecar_agree_on_how_the_bytes_came_in(upstream: Path) -> None:
    """The same bytes fetched after a record, or recorded after a fetch, add a line; repeating either adds none."""
    sidecar = Path(f"{RAW}.fetch.json")
    ledger.record(_source(), upstream, retrieved_at=WHEN)
    ledger.record(_source(), upstream, retrieved_at=WHEN)
    assert len(_lines()) == 1
    (fetched,) = fetch_mod.fetch(manifest_mod.load_manifest())
    assert "recorded_at" not in _lines()[-1]
    assert json.loads(sidecar.read_text(encoding="utf-8")) == ledger._fields(fetched)
    fetch_mod.fetch(manifest_mod.load_manifest())
    assert len(_lines()) == 2
    ledger.record(_source(), upstream, parameters={"country": "NL"})
    ledger.record(_source(), upstream, parameters={"country": "BE"})
    assert [line.get("parameters") for line in _lines()] == [None, None, {"country": "NL"}, {"country": "BE"}]
    assert json.loads(sidecar.read_text(encoding="utf-8"))["parameters"] == {"country": "BE"}
    assert "recorded" in report_mod.status(manifest_mod.load_manifest())[0]


def test_record_keeps_another_url_as_the_final_url_without_its_validators(
    upstream: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An ETag from a mirror must never be sent to the source's own URL on a later fetch."""
    result = ledger.record(_source(), upstream, retrieved_at=WHEN, url="https://mirror.example/x.csv", etag='"m"')
    assert (result.url, result.final_url, result.etag) == (upstream.as_uri(), "https://mirror.example/x.csv", "")
    assert "not the source URL's; not kept" in caplog.text


class _NoOffset(tzinfo):
    """A tzinfo that does not know its offset: Python then treats the time as naive."""

    def utcoffset(self, _: datetime | None) -> None:
        return None

    def dst(self, _: datetime | None) -> None:
        return None

    def tzname(self, _: datetime | None) -> None:
        return None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"retrieved_at": "2026-11-05T10:30:00"}, "needs a time zone"),
        ({"retrieved_at": datetime(2026, 11, 5)}, "needs a time zone"),  # noqa: DTZ001  # the naive time refused
        ({"retrieved_at": datetime(2026, 1, 1, tzinfo=_NoOffset())}, "needs a time zone"),
        ({"retrieved_at": "yesterday"}, "is not an ISO 8601 time"),
        ({"retrieved_at": 1762335000}, "is not an ISO 8601 time"),
        ({"url": "https://user:secret@example.org/x.csv"}, "user name or password"),
        ({"url": "https://token@example.org/x.csv"}, "user name or password"),
        ({"parameters": {"flag": True}}, "str or int values"),
        ({"parameters": {"ratio": 1.5}}, "str or int values"),
        ({"parameters": {"nested": {"a": 1}}}, "str or int values"),
        ({"parameters": {1: "a"}}, "str or int values"),
        ({"parameters": {"": "a"}}, "str or int values"),
        ({"parameters": {"api_key": "a"}}, r"\['api_key'\] look like credentials"),
        ({"parameters": {"Authorization": "a"}}, "look like credentials"),
        ({"retrieved_at": "2999-01-01T00:00:00Z"}, "is in the future"),
    ],
)
def test_record_refuses_what_it_cannot_record_honestly(upstream: Path, change: dict[str, object], message: str) -> None:
    """Each refusal names the source and comes before anything is written."""
    with pytest.raises(ValueError, match=f"^example: .*{message}"):
        ledger.record(_source(), upstream, **change)  # ty: ignore[invalid-argument-type]
    assert not Path("data").exists()


def test_record_refuses_bytes_that_miss_the_pin(upstream: Path, tmp_path: Path) -> None:
    """A pinned source takes only its pinned bytes, and leaves no temp file behind."""
    _manifest(tmp_path, upstream.as_uri(), terms=f'sha256 = "{"0" * 64}"')
    with pytest.raises(ValueError, match=r"not the sha256 in sources\.toml"):
        ledger.record(_source(), upstream, retrieved_at=WHEN)
    assert [p.name for p in Path("data/raw").iterdir()] == [".lock"]
    assert not LOG.exists()


def test_record_keeps_another_url_without_its_query(upstream: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A signed download URL carries its secret in the query, which would go into the published records."""
    signed = "https://bucket.example.org/x.csv?X-Amz-Signature=abc#part"
    assert ledger.record(_source(), upstream, url=signed).final_url == "https://bucket.example.org/x.csv"
    assert "abc" not in LOG.read_text(encoding="utf-8")
    assert "without its query and fragment" in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs on Windows")
@pytest.mark.usefixtures("upstream")
def test_record_refuses_what_is_not_a_regular_file(tmp_path: Path) -> None:
    """A named pipe is refused before it is opened, which would wait for a writer with the lock held."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for path in (fifo, tmp_path):
        with pytest.raises(ValueError, match="is not a regular file"):
            ledger.record(_source(), path)
    assert not RAW.parent.exists()


def test_record_refuses_an_unimplemented_source(upstream: Path) -> None:
    with pytest.raises(ValueError, match="only an implemented source"):
        ledger.record(manifest_mod.load_manifest()[1], upstream, retrieved_at=WHEN)


def test_a_missing_file_leaves_nothing_behind(upstream: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ledger.record(_source(), upstream.with_name("missing.csv"))
    assert not RAW.parent.exists()


def test_record_waits_for_no_fetch(upstream: Path) -> None:
    """Record and fetch share the raw lock, so they never interleave their writes."""
    RAW.parent.mkdir(parents=True)
    with (
        ledger._lock(RAW.parent),  # noqa: SLF001
        pytest.raises(OSError, match="another fetch is already running"),
    ):
        ledger.record(_source(), upstream, retrieved_at=WHEN)


@pytest.mark.skipif(sys.platform == "win32", reason="no raw lock on Windows")
def test_the_record_stage_reads_the_log_under_the_lock(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fetch that logs these bytes while record waits for the lock makes them unchanged, not new."""
    real = ledger._lock  # noqa: SLF001
    raced: list[bool] = []

    @contextlib.contextmanager
    def racing(raw: Path) -> Generator[None]:
        if not raced:
            raced.append(True)
            ledger.record(_source(), upstream)  # the other process, first to the lock
        with real(raw):
            yield

    monkeypatch.setattr(ledger, "_lock", racing)
    assert main(["record", "example", str(upstream)]) == 0
    assert capsys.readouterr().out.startswith("example  unchanged")


def test_record_writes_empty_validators_and_creates_the_log_folder(upstream: Path, tmp_path: Path) -> None:
    """Defaults stay empty strings, as a fetch writes them, and a log in a new folder is made."""
    log = tmp_path / "logs" / "nested" / "fetches.jsonl"
    result = ledger.record(_source(), upstream, log=log)
    assert (result.etag, result.last_modified, result.final_url) == ("", "", "")
    assert json.loads(log.read_text(encoding="utf-8"))["last_modified"] == ""


def test_record_keeps_the_query_of_the_source_url_itself(
    tmp_path: Path, upstream: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Only another URL loses its query: the manifest's own URL is reviewed and kept as it is."""
    _manifest(tmp_path, "https://example.org/data.csv?format=csv")
    result = ledger.record(_source(), upstream)
    assert (result.url, result.final_url) == ("https://example.org/data.csv?format=csv", "")
    assert "without its query" not in caplog.text


def test_record_copies_through_a_temp_file_beside_the_raw_file(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The temp file sits in data/raw/, so moving it into place is one rename on one file system."""
    made: list[Path] = []
    mkstemp = tempfile.mkstemp

    def spy(**kwargs: str | Path) -> tuple[int, str]:
        fd, name = mkstemp(**kwargs)  # ty: ignore[no-matching-overload]
        made.append(Path(name))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    ledger.record(_source(), upstream)
    temp = made[0]  # the copy; the sidecar's own temp file comes after it
    assert temp.parent.resolve() == RAW.parent.resolve()
    assert temp.name.startswith(".example.")
    assert temp.name.endswith(".part")
    assert not temp.exists()


@pytest.mark.parametrize(("cap", "chunk", "ok"), [(len(FIXTURE), 4, True), (len(FIXTURE) - 1, 4, False)])
def test_record_counts_every_chunk_against_the_cap(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: int, chunk: int, *, ok: bool
) -> None:
    """A file of exactly max_bytes is taken; one byte more is refused, however it is chunked."""
    monkeypatch.setattr(ledger, "_CHUNK_BYTES", chunk)
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\nmax_bytes = {cap}")
    if ok:
        assert ledger.record(_source(), upstream).bytes == cap
    else:
        with pytest.raises(ValueError, match=f"exceeds {cap} bytes"):
            ledger.record(_source(), upstream)
        assert not list(Path("data/raw").glob("*.part"))


def test_recording_the_same_request_again_adds_no_line(upstream: Path) -> None:
    for _ in range(2):
        ledger.record(_source(), upstream, parameters={"country": "NL"})
    assert len(_lines()) == 1


@pytest.mark.parametrize("value", ["a=", "a=b=c"])
def test_a_parameter_value_may_be_empty_or_hold_an_equals_sign(upstream: Path, value: str) -> None:
    assert main(["record", "example", str(upstream), "--parameter", value]) == 0
    name, _, rest = value.partition("=")
    assert json.loads(LOG.read_text(encoding="utf-8"))["parameters"] == {name: rest}


def test_record_treats_the_source_url_with_a_query_as_another_url(upstream: Path, tmp_path: Path) -> None:
    """Validators from a variant of the source URL are not the source URL's, even once its query is gone."""
    _manifest(tmp_path, "https://example.org/data.csv")
    result = ledger.record(_source(), upstream, url="https://example.org/data.csv?format=csv", etag='"v9"')
    assert (result.final_url, result.etag) == ("", "")


def test_record_logs_no_query_it_drops(upstream: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The warning about a dropped query names the URL without it, so the key never reaches a CI log."""
    with caplog.at_level(logging.WARNING, logger="sourceledger"):
        ledger.record(_source(), upstream, url="https://cdn.example.org/a.csv?X-Amz-Signature=s3cr3t", etag='"v1"')
    assert "https://cdn.example.org/a.csv without its query" in caplog.text
    assert "s3cr3t" not in caplog.text


@pytest.mark.parametrize(
    ("name", "refused"),
    [
        ("api_key", True),
        ("x-api-key", True),
        ("key", True),
        ("sig", True),
        ("X-Amz-Credential", True),
        ("client_secret", True),
        ("accessToken", True),
        ("pwd", True),
        ("session", True),
        ("authKey", True),
        ("authCode", True),
        ("oauth", True),
        ("oauth_code", True),
        ("sessionid", True),
        ("PHPSESSID", True),
        ("userKey", True),
        ("auth_code", True),
        ("apiKey", True),
        ("accessKeyId", True),
        ("session_date", True),
        ("pass_word", True),
        ("pass-word", True),
        ("mySig", True),
        ("author", False),
        ("authors", False),
        ("year", False),
        ("format", False),
        ("keyword", False),
        ("country", False),
    ],
)
def test_record_refuses_a_parameter_named_like_a_credential(upstream: Path, name: str, *, refused: bool) -> None:
    if refused:
        with pytest.raises(ValueError, match="look like credentials"):
            ledger.record(_source(), upstream, parameters={name: "x"})
    else:
        assert ledger.record(_source(), upstream, parameters={name: "x"}).parameters == {name: "x"}


def test_record_stores_parameters_as_text_in_name_order(upstream: Path) -> None:
    """The CLI's text and the library's int give one request, so recording it again adds no line."""
    first = ledger.record(_source(), upstream, parameters={"year": 2024, "country": "NL"})
    assert list(first.parameters.items()) == [("country", "NL"), ("year", "2024")]
    ledger.record(_source(), upstream, parameters={"country": "NL", "year": "2024"})
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 1


def test_record_refuses_a_time_before_utc_can_hold_it(upstream: Path) -> None:
    with pytest.raises(ValueError, match="out of range"):
        ledger.record(_source(), upstream, retrieved_at="0001-01-01T00:00:00+05:00")


@pytest.mark.parametrize(("when", "url"), [("2026-11-05T10:00:00Z", None), (WHEN, "https://mirror.example.org/a.csv")])
def test_recording_the_same_bytes_from_elsewhere_or_another_time_adds_a_line(
    upstream: Path, when: str, url: str | None
) -> None:
    """The log's last line names where and when the sidecar says the bytes came from."""
    ledger.record(_source(), upstream, retrieved_at=WHEN)
    ledger.record(_source(), upstream, retrieved_at=when, url=url)
    last = json.loads(LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert last == json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 2


def test_validate_sees_a_package_built_before_the_same_bytes_were_recorded_again(upstream: Path) -> None:
    """Rows stamp the bytes, not the request; the package's provenance block names both."""
    ledger.record(_source(), upstream, parameters={"country": "NL"})
    assert _run("build", "validate") == [0, 0]
    ledger.record(_source(), upstream, parameters={"country": "BE"})
    assert any("describes an earlier fetch" in p for p in report_mod.validate(manifest_mod.load_manifest()))


def test_recording_the_same_bytes_again_leaves_the_sidecar_as_logged(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-op record keeps the logged time, so the package built after the first stays current."""
    ticks = iter(range(10, 60))
    monkeypatch.setattr(ledger, "_timestamp", lambda _when: f"2026-11-05T09:30:{next(ticks)}Z")
    ledger.record(_source(), upstream)
    assert _run("build") == [0]
    ledger.record(_source(), upstream)
    assert report_mod.validate(manifest_mod.load_manifest()) == []
    assert _lines()[-1] == json.loads(Path("data/raw/example.csv.fetch.json").read_text(encoding="utf-8"))


def test_a_record_with_parameters_that_are_not_text_is_not_a_record() -> None:
    fields = json.loads(_record("a", "1"))
    assert ledger._is_record(fields)  # noqa: SLF001
    for damaged in ({"parameters": []}, {"parameters": {"n": 3}}, {"recorded_at": None}):
        assert not ledger._is_record({**fields, **damaged})  # noqa: SLF001


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_record_takes_the_earliest_utc_time_whatever_the_local_zone(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The range check is against UTC: a local zone west of it would put this time before year 1."""
    monkeypatch.setenv("TZ", "Etc/GMT+12")
    time.tzset()
    try:
        result = ledger.record(_source(), upstream, retrieved_at="0001-01-01T00:00:00Z")
    finally:
        monkeypatch.undo()
        time.tzset()
    assert result.retrieved_at.endswith("-01-01T00:00:00Z")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot delete a file that is still open")
def test_a_failed_copy_raises_its_own_error_even_if_the_temp_file_is_gone(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def vanish(*_args: object) -> None:
        for part in Path("data/raw").glob(".example.*.part"):
            part.unlink()
        raise OSError(errno.EIO, "disk gone")

    monkeypatch.setattr(ledger, "_copy", vanish)
    with pytest.raises(OSError, match="disk gone"):
        ledger.record(_source(), upstream)


def test_record_gives_no_snapshot_when_the_log_holds_none(upstream: Path, tmp_path: Path) -> None:
    """No log line, or a line from before archive_url, leaves archive_url empty."""
    _manifest(tmp_path, upstream.as_uri(), terms=f"{TERMS}\narchive = true")
    assert ledger.record(_source(), upstream).archive_url == ""
    line = json.loads(LOG.read_text(encoding="utf-8"))
    del line["archive_url"]
    LOG.write_text(json.dumps(line) + "\n", encoding="utf-8", newline="")
    assert ledger.record(_source(), upstream, retrieved_at=WHEN).archive_url == ""


@pytest.mark.parametrize("field", ["etag", "last_modified"])
def test_recording_the_same_bytes_with_another_validator_adds_a_line(upstream: Path, field: str) -> None:
    for value in ("a", "b"):
        ledger.record(
            _source(),
            upstream,
            etag=value if field == "etag" else "",
            last_modified=value if field == "last_modified" else "",
        )
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 2


def test_a_log_line_with_empty_parameters_reads_as_having_none(upstream: Path) -> None:
    ledger.record(_source(), upstream)
    line = json.loads(LOG.read_text(encoding="utf-8"))
    LOG.write_text(json.dumps({**line, "parameters": {}}) + "\n", encoding="utf-8", newline="")
    ledger.record(_source(), upstream)
    assert len(LOG.read_text(encoding="utf-8").splitlines()) == 1
