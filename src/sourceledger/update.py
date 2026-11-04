"""Publish complete local generations while retaining verified downloads after failures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sourceledger import ledger
from sourceledger.budget import ACTIVE_BUDGET, StagingBudget
from sourceledger.ledger import (
    _HASH_BLOCK_BYTES,
    _check_headers,
    _check_output_format,
    _current,
    _lock,
    _output_name,
    _previous,
    _record_fields,
    _replacing,
    _select,
    _write_atomic,
)
from sourceledger.retrieval import _check_names, canonical, expand_requests

if TYPE_CHECKING:
    from collections.abc import Collection

UPDATES = Path("data/updates")
_DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
_GENERATION = re.compile(r"[0-9a-f]{32}")
_TEMPORARY = re.compile(r"\..+\.[a-z0-9_]{8}\.part")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        msg = f"{path.name} must contain a JSON object"
        raise ValueError(msg)  # noqa: TRY004  # corrupt on-disk metadata is a value error
    return value


def _generation(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not _GENERATION.fullmatch(value):
        msg = "invalid update generation pointer"
        raise ValueError(msg)
    path = root / "generations" / value
    if path.is_symlink() or (root / "generations").is_symlink():
        msg = "update generations must be local directories, not symlinks"
        raise ValueError(msg)
    return path


def published(root: Path = UPDATES) -> Path:
    """Resolve the current complete generation once for a consistent raw/output snapshot.

    Raises ValueError when no dataset has completed or the pointer/marker is damaged.
    Keep this path for an entire operation; retained generations are never deleted here.
    The atomic publication guarantee requires a local POSIX filesystem.
    """
    try:
        pointer = _read(root / "current.json")
        generation = _generation(root, pointer.get("generation"))
        marker = _read(generation / "complete.json")
        plan = (generation / "plan.json").read_bytes()
        if marker.get("plan_sha256") == hashlib.sha256(plan).hexdigest() == pointer.get("plan_sha256"):
            return generation
    except (FileNotFoundError, json.JSONDecodeError) as error:
        msg = "no complete dataset is published; run update successfully first"
        raise ValueError(msg) from error
    msg = "published generation has no valid completion marker"
    raise ValueError(msg)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_store(root: Path) -> None:
    missing = []
    directory = root
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in reversed(missing):
        _sync_directory(directory.parent)


def _sync_generation(generation: Path) -> None:
    for folder, _, files in os.walk(generation, topdown=False):
        for name in files:
            with (Path(folder) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        _sync_directory(Path(folder))


def _copy_file(source: Path, target: Path) -> None:
    if source.is_symlink():
        msg = "generation files must not be symlinks"
        raise ValueError(msg)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as stream, _replacing(target) as output:
        shutil.copyfileobj(stream, output, length=_HASH_BLOCK_BYTES)


def _is_complete(generation: Path) -> bool:
    marker, plan = generation / "complete.json", generation / "plan.json"
    if marker.is_symlink() or plan.is_symlink():
        return False
    try:
        return _read(marker).get("plan_sha256") == hashlib.sha256(plan.read_bytes()).hexdigest()
    except (FileNotFoundError, ValueError):
        return False


def _staged_size(root: Path) -> int:
    for part in root.iterdir():
        if _TEMPORARY.fullmatch(part.name) and part.is_file():
            part.unlink()
    attempt = root / "attempt.json"
    size = attempt.stat().st_size if attempt.exists() else 0
    for generation in (root / "generations").iterdir():
        if generation.is_symlink() or not generation.is_dir():
            msg = "update generations must be local directories"
            raise ValueError(msg)
        if _is_complete(generation):
            continue
        for folder, directories, files in os.walk(generation):
            if any((Path(folder) / name).is_symlink() for name in [*directories, *files]):
                msg = "generation files must not be symlinks"
                raise ValueError(msg)
            for name in files:
                path = Path(folder) / name
                if _TEMPORARY.fullmatch(name):
                    path.unlink()
                else:
                    size += path.stat().st_size
    return size


def _preflight(sources: list[ledger.Source], output_format: str) -> list[ledger.Source]:
    _check_output_format(output_format)
    resolved = expand_requests(sources, output_format=output_format)
    _check_names(resolved, output_format)
    for source in ledger.implemented(resolved):
        for name in (ledger.raw_name(source), _output_name(source, output_format)):
            if name != Path(name).name or chr(92) in name or name in {".", ".."}:
                msg = "update filenames must be local file names"
                raise ValueError(msg)
        url = urlsplit(source["url"])
        if url.username is not None or url.password is not None:
            msg = "update URLs cannot contain credentials; use environment headers over HTTPS"
            raise ValueError(msg)
        _check_headers(source)
    return resolved


def _check_partial(sources: list[ledger.Source], selected: set[str], previous: Path | None, output_format: str) -> None:
    if previous is None:
        msg = "a partial update needs a previous complete dataset; run a full update first"
        raise ValueError(msg)
    old = _read(previous / "plan.json")
    before = {s["id"]: s for s in old["sources"] if s["id"] not in selected}
    after = {s["id"]: s for s in sources if s["id"] not in selected}
    if canonical(before) != canonical(after) or old["output_format"] != output_format:
        msg = "unselected sources or output format changed; run a full update"
        raise ValueError(msg)


def _clone(previous: Path, generation: Path, resolved: list[ledger.Source]) -> None:
    raw = generation / "raw"
    previous_raw = previous / "raw"
    credentials = previous_raw / ".credentials"
    if credentials.exists():
        if credentials.is_symlink():
            msg = "credential cache must not be a symlink"
            raise ValueError(msg)
        for path in credentials.iterdir():
            if path.is_file() and not (raw / ".credentials" / path.name).exists():
                _copy_file(path, raw / ".credentials" / path.name)
    for source in ledger.implemented(resolved):
        if _previous(source, previous_raw) is not None:
            name = ledger.raw_name(source)
            _copy_file(previous_raw / name, raw / name)
            _copy_file(previous_raw / f"{name}.fetch.json", raw / f"{name}.fetch.json")


def _attempt(root: Path, plan: bytes) -> tuple[Path, bool]:
    path = root / "attempt.json"
    if path.exists():
        attempt = _read(path)
        candidate = _generation(root, attempt.get("generation"))
        if (
            (candidate / "plan.json").is_file()
            and (candidate / "plan.json").read_bytes() == plan
            and not _is_complete(candidate)
        ):
            return candidate, True
    generation = root / "generations" / uuid.uuid4().hex
    generation.mkdir(mode=0o700)
    (generation / "raw").mkdir(mode=0o700)
    (generation / "processed").mkdir(mode=0o700)
    _write_atomic(generation / "plan.json", plan)
    _sync_directory(generation)
    _write_atomic(path, canonical({"generation": generation.name}))
    _sync_directory(root / "generations")
    _sync_directory(root)
    return generation, False


def update(
    sources: list[ledger.Source],
    *,
    root: Path = UPDATES,
    parse: ledger.Parser = ledger.parse_source,
    only: Collection[str] | None = None,
    output_format: str = "csv",
    user_agent: str = ledger.USER_AGENT,
    workers: int = 1,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> Path:
    """Fetch, build, validate and publish one immutable complete generation.

    Existing fetch/build paths remain independent. Only this operation publishes complete
    updates. Fetch, build and validation failures retain verified downloads for retry and
    leave the previous pointer unchanged. Publication I/O errors can occur after a complete
    pointer changes. `only` replaces whole logical source groups in a previous complete dataset.
    `max_bytes` bounds incomplete generations plus bytes written during this attempt;
    deleted/replaced temporary bytes stay charged until retry. Completed history is retained
    separately and can accumulate. POSIX local filesystems only; no automatic deletion.

    Raises FetchError, OSError or ValueError for download, parsing, validation, locking or
    budget failures. Returns the generation directory containing raw and processed folders.
    """
    if os.name != "posix":  # noqa: PLR2004  # publication needs POSIX directory syncing
        msg = "complete update publication requires a local POSIX filesystem"
        raise ValueError(msg)
    if type(max_bytes) is not int or max_bytes < 1:
        msg = "max_bytes must be a positive whole number"
        raise ValueError(msg)
    if type(workers) is not int or workers not in (1, 2):
        msg = "workers must be 1 or 2"
        raise ValueError(msg)
    _preflight(sources, output_format)
    selected = {source["id"] for source in _select(sources, only)}
    plan = canonical(
        {
            "version": 1,
            "sources": sources,
            "output_format": output_format,
            "only": None if only is None else sorted(selected),
        }
    )
    _mkdir_store(root)
    if (root / "generations").is_symlink():
        msg = "update generations must be local directories, not symlinks"
        raise ValueError(msg)
    (root / "generations").mkdir(mode=0o700, exist_ok=True)
    with _lock(root):
        previous = published(root) if (root / "current.json").exists() else None
        if only is not None:
            _check_partial(sources, selected, previous, output_format)
        token = ACTIVE_BUDGET.set(StagingBudget(max_bytes, _staged_size(root)))
        try:
            generation, resume = _attempt(root, plan)
            return _update_generation(
                sources, root, generation, previous, parse, output_format, user_agent, workers, plan, resume=resume
            )
        finally:
            ACTIVE_BUDGET.reset(token)


def _update_generation(
    sources: list[ledger.Source],
    root: Path,
    generation: Path,
    previous: Path | None,
    parse: ledger.Parser,
    output_format: str,
    user_agent: str,
    workers: int,
    plan: bytes,
    *,
    resume: bool,
) -> Path:
    selected = {source["id"] for source in _select(sources, json.loads(plan)["only"])}
    resolved = [{**s, "_update_cache": True} for s in expand_requests(sources, output_format=output_format)]
    if previous is not None:
        _clone(previous, generation, [s for s in resolved if s["id"] not in selected])
    raw, processed, log = generation / "raw", generation / "processed", generation / "fetches.jsonl"
    pending = [
        s
        for s in ledger.implemented(resolved)
        if s["id"] in selected and not (resume and _previous(s, raw) is not None)
    ]
    if pending:
        ledger.fetch(pending, raw=raw, log=log, user_agent=user_agent, workers=workers)
    for source in ledger.implemented(resolved):
        if _previous(source, raw) is None:
            msg = f"{source['id']}: cached bytes or credentials changed; run a full update"
            raise ValueError(msg)
    # Unselected groups use immutable output copies, including custom-parser results.
    if previous is not None:
        for source in ledger.implemented(resolved):
            if source["id"] not in selected:
                name = _output_name(source, output_format)
                _copy_file(previous / "processed" / name, processed / name)
        previous_package = previous / "processed" / ledger.DATAPACKAGE
        _copy_file(previous_package, processed / ledger.DATAPACKAGE)
    ledger.build(
        sources,
        raw=raw,
        processed=processed,
        parse=parse,
        only=None if json.loads(plan)["only"] is None else sorted(selected),
        output_format=output_format,
    )
    records = [_record_fields(_current(source, raw)) for source in ledger.implemented(resolved)]
    _write_atomic(log, b"".join(canonical(record) + b"\n" for record in records))
    problems = ledger.validate(sources, raw=raw, processed=processed, output_format=output_format)
    if problems:
        raise ValueError("update is incomplete: " + "; ".join(problems))
    digest = hashlib.sha256(plan).hexdigest()
    _sync_generation(generation)
    _write_atomic(generation / "complete.json", canonical({"version": 1, "plan_sha256": digest}))
    _sync_directory(generation)
    _write_atomic(root / "current.json", canonical({"generation": generation.name, "plan_sha256": digest}))
    _sync_directory(root)
    return generation
