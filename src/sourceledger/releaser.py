"""Write and check a release folder: the shippable outputs, their metadata and SHA256SUMS."""

from __future__ import annotations

import errno
import json
import logging
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

from sourceledger.builder import DATAPACKAGE, PROCESSED, _by_name, json_bytes
from sourceledger.ledger import RAW, _digest, current_record
from sourceledger.manifest import RELEASE_FORMATS, Source, implemented, output_name, printable
from sourceledger.report import leaves_folder, read_received, validate

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

_logger = logging.getLogger("sourceledger")


CODEMETA = Path("codemeta.json")


SUMS = "SHA256SUMS"


_DATA = "data"


_ROCRATE = "ro-crate-metadata.json"


_CROISSANT = "croissant.json"


_FORMAT_ROCRATE, _FORMAT_CROISSANT = RELEASE_FORMATS


_FILES = {_FORMAT_ROCRATE: _ROCRATE, _FORMAT_CROISSANT: _CROISSANT}


# A SHA256SUMS line is about 100 bytes, so this lists over half a million files.


# Zenodo takes at most 100 files a record; past that, the project zips the folder itself.
_ZENODO_FILES = 100


# The codemeta.json terms each format copies, and those it cannot do without. All are
# schema.org terms, so they keep their names. Croissant names the authors `creator`.
_TERMS = ("name", "description", "license", "version", "identifier", "datePublished", "url")
_REQUIRED = {
    _FORMAT_ROCRATE: ("name", "description", "license", "datePublished"),
    _FORMAT_CROISSANT: ("name", "description", "license", "url"),
}


# Each check's PyPI distribution and its arguments after `python -I`, run in the folder. Every
# one runs in its own interpreter, so one that crashes does not stop the others.
_VALIDATORS = {
    _ROCRATE: (
        "roc-validator",
        (
            "-c",
            "from rocrate_validator.cli import cli; cli()",
            "--no-interactive",
            "--disable-color",
            "validate",
            "--profile-identifier",
            "ro-crate-1.3",
            "--no-paging",
            "--output-format",
            "json",
            ".",
        ),
    ),
    _CROISSANT: ("mlcroissant", ("-m", "mlcroissant.scripts.validate", "--jsonld", _CROISSANT)),
}


_INSTALL = "uv tool install 'sourceledger[release]'"


# Seconds a validator may take. roc-validator downloads the RO-Crate context; a stalled
# network must not hold the release forever.
_TIMEOUT_S = 600


# A line of rich's box drawing, which roc-validator prints around its messages.
_DIVIDER = re.compile(r"[\s\u2500-\u257f]*")


# mlcroissant logs its errors as "Found the following N error(s) during the validation:"
# and one indented "  -  <what is wrong>" line each, then its warnings the same way.
_CROISSANT_ERRORS = re.compile(r"error\(s\) during the validation:\n((?:[ \t]+-[ \t]+.+\n?)*)")


_VERSION = version("sourceledger")


class ReleaseError(ValueError):
    """The release was refused: its input or its output has the `problems` listed."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class RestrictedSourceError(ReleaseError):
    """A file in the release folder holds the bytes of a source whose terms forbid redistribution."""


class ValidatorFailed(ReleaseError):  # noqa: N818  # the names decision 0006 gives
    """A check of the release folder reported findings, one per problem; `ran` names each check that ran."""

    def __init__(self, problems: list[str], ran: list[dict[str, str]]) -> None:
        super().__init__(problems)
        self.ran = ran


class ValidatorMissing(OSError):  # noqa: N818
    """A check the release needs is not installed: the `release` extra is missing."""


class ValidatorCouldNotRun(OSError):  # noqa: N818
    """A check stopped without reporting findings, such as offline or past its time limit, so it did not check."""


def release(
    sources: list[Source],
    out_dir: Path,
    *,
    formats: Iterable[str] = (),
    raw: Path = RAW,
    processed: Path = PROCESSED,
    codemeta: Path = CODEMETA,
    output_format: str = "csv",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write a release folder at `out_dir` from a validated build, check it, and return what it holds.

    The folder gets `data/` with the output of every implemented source whose `redistribute` is
    true, `datapackage.json` without the others, `ro-crate-metadata.json` and `croissant.json`
    when `formats` (any iterable of their names) asks for them, and `SHA256SUMS`, written last.
    The descriptive fields come from `codemeta`. The folder is written beside `out_dir` and moved
    into place only once every check passed, so a refused release leaves nothing at `out_dir`.
    `dry_run` checks the input and returns what would be written, without writing.

    The result has `files` (paths in the folder), `excluded` (source ids), `formats` (each
    optional format "written" or "skipped") and `validators` (`file`, `name`, `version` of each
    check that ran). Raises ReleaseError when `validate` finds problems or codemeta.json is missing or lacks a
    field a format needs, RestrictedSourceError when a restricted source's bytes reached the
    folder, ValidatorFailed when a check reports findings, ValidatorMissing when a check a format
    needs is not installed, ValidatorCouldNotRun when a check stopped without reporting, and
    FileExistsError when `out_dir` exists and is not empty.
    """
    # A str is an iterable of str too, and would be read one letter at a time.
    wanted = set(formats)
    if isinstance(formats, str) or not wanted <= set(RELEASE_FORMATS):
        msg = f"formats must be drawn from {list(RELEASE_FORMATS)}, not {formats!r}"
        raise ValueError(msg)
    formats = [fmt for fmt in RELEASE_FORMATS if fmt in wanted]
    if problems := validate(sources, raw=raw, processed=processed, output_format=output_format):
        raise ReleaseError(problems)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        msg = f"{out_dir.as_posix()} already exists; a release goes into a new folder"
        raise FileExistsError(msg)
    for fmt in formats:
        _validator_version(_FILES[fmt])
    described = _described(codemeta, formats)
    shipped, excluded = _split(sources)
    files = [f"{_DATA}/{output_name(s, output_format)}" for s in shipped]
    files += [DATAPACKAGE, *(_FILES[fmt] for fmt in formats), SUMS]
    result: dict[str, Any] = {
        "files": files,
        "excluded": [s["id"] for s in excluded],
        "formats": {fmt: "written" if fmt in formats else "skipped" for fmt in RELEASE_FORMATS},
        "validators": [],
    }
    if len(files) > _ZENODO_FILES:
        _logger.warning(
            "the release has %d files; Zenodo takes at most %d, so zip them first", len(files), _ZENODO_FILES
        )
    if dry_run:
        return result
    folder = _part_folder(out_dir)
    try:
        _write(folder, sources, described, formats, raw=raw, processed=processed, output_format=output_format)
        _refuse_restricted(folder, out_dir, sources, raw, processed, output_format)
        problems, result["validators"] = check_release(folder)
        if problems:
            raise ValidatorFailed(problems, result["validators"])
        _place(folder, out_dir)
    finally:
        if folder.exists():
            shutil.rmtree(folder)
    return result


def _place(folder: Path, out_dir: Path) -> None:
    """Move the checked `folder` to `out_dir`, or into it when it exists, empty.

    An existing folder is filled, not replaced: it can be a mount point, or carry its own owner and mode.
    It is filled SHA256SUMS last, and emptied again when a move fails, so it never holds part of a release.
    """
    if not out_dir.exists():
        folder.rename(out_dir)
        return
    moved: list[Path] = []
    try:
        for child in sorted(folder.iterdir(), key=lambda child: child.name == SUMS):
            # Listed first: a move across devices copies, and can fail with part of the copy in place.
            moved.append(out_dir / child.name)
            shutil.move(child, out_dir / child.name)
    except BaseException:
        for path in moved:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        raise
    folder.rmdir()


def _part_folder(out_dir: Path) -> Path:
    """A new, empty folder beside `out_dir` to write the release in; FileExistsError when a killed run left one."""
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    # Not mkdtemp: its folder is private to the user, and the release is meant to be read by others.
    # A folder left by a killed run is refused, never deleted: it may hold someone's files by now.
    folder = out_dir.parent / f".{out_dir.name}.part"
    try:
        folder.mkdir()
    except FileExistsError:
        msg = f"{folder.as_posix()} is left from a release that did not finish; delete it"
        raise FileExistsError(msg) from None
    return folder


def _split(sources: list[Source]) -> tuple[list[Source], list[Source]]:
    """The implemented sources to ship, whose `redistribute` is exactly true, and those to leave out."""
    shipped = [s for s in implemented(sources) if s.get("redistribute") is True]
    return shipped, [s for s in implemented(sources) if s not in shipped]


def _write(
    folder: Path,
    sources: list[Source],
    described: dict[str, Any],
    formats: Collection[str],
    *,
    raw: Path,
    processed: Path,
    output_format: str,
) -> None:
    """Write the release into the empty `folder`, SHA256SUMS last. The file list comes from the manifest."""
    shipped, excluded = _split(sources)
    (folder / _DATA).mkdir()
    for source in shipped:
        name = output_name(source, output_format)
        shutil.copyfile(processed / name, folder / _DATA / name)
    package = _package(processed, shipped)
    (folder / DATAPACKAGE).write_bytes(json_bytes(package))
    if _FORMAT_ROCRATE in formats:
        records = {s["id"]: current_record(s, raw) for s in implemented(sources)}
        (folder / _ROCRATE).write_bytes(json_bytes(_ro_crate(described, package, shipped, excluded, records)))
    if _FORMAT_CROISSANT in formats:
        (folder / _CROISSANT).write_bytes(json_bytes(_croissant(described, package)))
    _write_sums(folder)


def check_release(folder: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Check a release folder: SHA256SUMS against the files, then each metadata file with its validator.

    Returns one string per problem, with names from the folder as they are (pass each through
    `printable` before a terminal), and the `file`, `name` and `version` of each check that ran.
    Raises FileNotFoundError when `folder` is not a folder, ValidatorMissing when a metadata
    file is there but its validator is not installed, and ValidatorCouldNotRun when a validator
    stopped without reporting findings or ran past its time limit.
    """
    if not folder.is_dir():
        raise FileNotFoundError(errno.ENOENT, "no such folder", str(folder))
    present = [name for name in _VALIDATORS if (folder / name).is_file()]
    versions = {name: _validator_version(name) for name in present}
    problems = _sums_problems(folder)
    ran = [{"file": SUMS, "name": "sourceledger", "version": _VERSION}]
    for name in present:
        dist, args = _VALIDATORS[name]
        # -I: the folder is someone else's, and a module planted in it must not shadow the validator.
        # -X utf8: -I ignores PYTHONIOENCODING, and Windows would read the JSON and pipe the output as cp1252.
        command = [sys.executable, "-I", "-X", "utf8", *args]
        try:
            subprocess.run(command, cwd=folder, capture_output=True, check=True, timeout=_TIMEOUT_S)  # noqa: S603  # a fixed command line, with this interpreter
        except subprocess.CalledProcessError as failed:
            problems += [f"{name}: {finding}" for finding in _findings(name, dist, failed)]
        except subprocess.TimeoutExpired:
            msg = f"{name}: {dist} did not finish in {_TIMEOUT_S} s"
            raise ValidatorCouldNotRun(msg) from None
        ran.append({"file": name, "name": dist, "version": versions[name]})
    return problems, ran


def _validator_version(name: str) -> str:
    """The installed version of the validator for metadata file `name`; ValidatorMissing when there is none."""
    dist = _VALIDATORS[name][0]
    try:
        return version(dist)
    except PackageNotFoundError:
        msg = f"checking {name} needs {dist}, from the release extra: {_INSTALL}"
        raise ValidatorMissing(msg) from None


def _rocrate_findings(stdout: str, _stderr: str) -> list[str]:
    """The issues in rocrate-validator's JSON report, each with its check id; none when it wrote no report."""
    try:
        return [f"{issue['message']} ({issue['check']['identifier']})" for issue in json.loads(stdout)["issues"]]
    except (ValueError, KeyError, TypeError):
        return []


def _croissant_findings(_stdout: str, stderr: str) -> list[str]:
    """The errors mlcroissant logs, the indented lines under its error count, without its warnings."""
    match = _CROISSANT_ERRORS.search(stderr)
    return [line.strip().removeprefix("-").strip() for line in match[1].splitlines()] if match else []


def _findings(name: str, dist: str, failed: subprocess.CalledProcessError) -> list[str]:
    """What a validator of `name` reported, one line per finding; ValidatorCouldNotRun when it reported none.

    Both validators exit 1 for findings. An mlcroissant finding in a form the parser does not
    know (it words some differently on Windows) is kept as the last line that says something.
    Any other exit, exit 1 with a traceback (a crash), or exit 1 with nothing parsed from
    rocrate-validator means the check did not happen.
    """
    # Decoded here, not by subprocess: a validator writes in its own locale, and a stray byte must not hide a finding.
    stdout, stderr = failed.stdout.decode(errors="replace"), failed.stderr.decode(errors="replace")
    if failed.returncode == 1 and (findings := _FINDINGS[dist](stdout, stderr)):
        return findings
    said = [line.strip() for line in (stdout + "\n" + stderr).splitlines() if not _DIVIDER.fullmatch(line)]
    last = said[-1] if said else "with no output"
    crashed = any(line.startswith("Traceback") for line in stderr.splitlines())
    if dist in _EXIT_1_IS_FINDINGS and failed.returncode == 1 and said and not crashed:
        return [last]
    msg = f"{name}: {dist} exited {failed.returncode}: {printable(last)}"
    raise ValidatorCouldNotRun(msg)


_FINDINGS = {"roc-validator": _rocrate_findings, "mlcroissant": _croissant_findings}
# mlcroissant exits 1 only on a ValidationError (a crash prints a traceback first).
_EXIT_1_IS_FINDINGS = {"mlcroissant"}


def _described(codemeta: Path, formats: Collection[str]) -> dict[str, Any]:
    """The dataset's descriptive fields from codemeta.json; ReleaseError when it is missing or lacks a field."""
    if not formats:
        return {}
    try:
        meta = json.loads(codemeta.read_bytes())
    except FileNotFoundError:
        path = codemeta.as_posix()
        raise ReleaseError(
            [f"{path} is missing, which {fmt} needs; write it, or drop {fmt} from release_formats" for fmt in formats]
        ) from None
    except ValueError as error:  # also bytes outside UTF-8
        raise ReleaseError([f"{codemeta.as_posix()} is not valid JSON: {error}"]) from None
    if not isinstance(meta, dict):
        msg = f"{codemeta.as_posix()} is not a JSON object"
        raise ReleaseError([msg])
    if missing := [
        f"{codemeta.as_posix()} has no {field}, which {fmt} needs"
        for fmt in formats
        for field in _REQUIRED[fmt]
        if not meta.get(field)
    ]:
        raise ReleaseError(missing)
    described = {term: meta[term] for term in _TERMS if meta.get(term)}
    if authors := meta.get("author"):
        described["author"] = authors if isinstance(authors, list) else [authors]
    return described


def _package(processed: Path, shipped: list[Source]) -> dict[str, Any]:
    """The built datapackage.json with only the shipped resources, each path under data/."""
    package = json.loads((processed / DATAPACKAGE).read_bytes())
    ids = {s["id"] for s in shipped}
    package["resources"] = [
        {**resource, "path": f"{_DATA}/{resource['path']}"}
        for name, resource in _by_name(package).items()
        if name in ids
    ]
    return package


def _files(folder: Path) -> list[str]:
    """Every file under `folder` but SHA256SUMS, as sorted paths with `/`."""
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and p != folder / SUMS)


def _write_sums(folder: Path) -> None:
    """SHA256SUMS for every other file, as `sha256sum -b` prints it, so `sha256sum -c` checks the folder."""
    lines = "".join(f"{_digest(folder / name)} *{name}\n" for name in _files(folder))
    (folder / SUMS).write_bytes(lines.encode())


def _sums_problems(folder: Path) -> list[str]:
    """Whether SHA256SUMS lists every other file in `folder`, each with its current SHA-256."""
    path = folder / SUMS
    try:
        text = read_received(path).decode()
    except FileNotFoundError:
        return [f"{path.as_posix()} is missing"]
    except UnicodeDecodeError:
        return [f"{path.as_posix()} is not UTF-8 text"]
    except ValueError as refused:
        return [str(refused)]
    problems, listed = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not match:
            problems.append(f"{path.as_posix()}: line {number} is not `<sha256> *<path>`")
            continue
        digest, name = match.groups()
        name = re.sub(r"^(?:\./)+", "", name)  # as `find . -exec sha256sum` writes it
        listed.add(name)
        file = folder / name
        if leaves_folder(name) or not file.resolve().is_relative_to(folder.resolve()):
            problems.append(f"{path.as_posix()}: {name!r} is not a file in {folder.as_posix()}")
        elif not file.is_file():
            problems.append(f"{file.as_posix()} is listed in {SUMS} but missing")
        elif _digest(file) != digest:
            problems.append(f"{file.as_posix()} does not match its SHA-256 in {SUMS}")
    problems += [
        f"{(folder / name).as_posix()} is not listed in {SUMS}" for name in _files(folder) if name not in listed
    ]
    return problems


def _refuse_restricted(
    folder: Path, shown: Path, sources: list[Source], raw: Path, processed: Path, output_format: str
) -> None:
    """RestrictedSourceError when any file in `folder` has the bytes of a restricted source's raw file or output.

    The restricted sources are read from `sources` here, not taken from `_split`, so a bug that
    ships one cannot also clear it. Each problem names the file as it would be under `shown`.
    """
    banned = {}
    for source in implemented(sources):
        if source.get("redistribute") is True:
            continue
        record = current_record(source, raw)
        banned[record.sha256] = source["id"]
        out = processed / output_name(source, output_format)
        if out.is_file():
            banned[_digest(out)] = source["id"]
    if found := [
        f"{(shown / name).as_posix()} would hold the bytes of {banned[digest]}, whose redistribute is not true"
        for name in [*_files(folder), SUMS]
        if (digest := _digest(folder / name)) in banned
    ]:
        raise RestrictedSourceError(found)


def _hex(resource: dict[str, Any]) -> str:
    return str(resource["hash"]).removeprefix("sha256:")


def _media_type(resource: dict[str, Any]) -> str:
    """A resource's media type; a copied file has none in the package, so it is generic bytes."""
    return resource.get("mediatype", "application/octet-stream")


def _people(authors: list[Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """References to the authors, and the authors as entities, each with an @id (`#author-<n>` without one)."""
    entities = [
        {**author, "@id": author.get("@id") or f"#author-{n}"}
        if isinstance(author, dict)
        else {"@id": f"#author-{n}", "name": str(author)}
        for n, author in enumerate(authors, 1)
    ]
    for entity in entities:
        entity.setdefault("@type", "Person")
        if not entity.get("name"):
            entity["name"] = " ".join(str(entity[k]) for k in ("givenName", "familyName") if entity.get(k))
    return [{"@id": entity["@id"]} for entity in entities], entities


def _ro_crate(
    described: dict[str, Any],
    package: dict[str, Any],
    shipped: list[Source],
    excluded: list[Source],
    records: dict[str, Any],
) -> dict[str, Any]:
    """An RO-Crate 1.3 description: the root dataset, a File per shipped output, and every source with its fetch."""
    root: dict[str, Any] = {"@id": "./", "@type": "Dataset"}
    root.update(described)
    authors, people = _people(described.get("author", []))
    if authors:
        root["author"] = authors
    resources = _by_name(package)
    files = []
    for source in shipped:
        resource = resources[source["id"]]
        files.append(
            {
                "@id": resource["path"],
                "@type": "File",
                "name": Path(resource["path"]).name,
                "encodingFormat": _media_type(resource),
                "contentSize": str(resource["bytes"]),
                "sha256": _hex(resource),
                "isBasedOn": {"@id": f"#source-{source['id']}"},
            }
        )
    origins, fetches = [], []
    withheld = {s["id"] for s in excluded}
    for source in [*shipped, *excluded]:
        sid, record = source["id"], records[source["id"]]
        origin: dict[str, Any] = {
            "@id": f"#source-{sid}",
            "@type": "CreativeWork",
            "identifier": sid,
            "name": str(source.get("name", sid)),
            "url": record.url,
            "license": source["license"],
            "usageInfo": source["terms_url"],
            "conditionsOfAccess": source["terms"],
        }
        if sid in withheld:
            origin["description"] = "Excluded from this release: its terms do not allow redistribution."
        origins.append(origin)
        fetch: dict[str, Any] = {"@id": f"#fetch-{sid}", "@type": "DownloadAction", "object": {"@id": f"#source-{sid}"}}
        if record.retrieved_at:
            fetch["endTime"] = record.retrieved_at
        fetches.append(fetch)
    root["hasPart"] = [{"@id": file["@id"]} for file in files]
    root["isBasedOn"] = [{"@id": origin["@id"]} for origin in origins]
    root["mentions"] = [{"@id": fetch["@id"]} for fetch in fetches]
    descriptor = {
        "@id": _ROCRATE,
        "@type": "CreativeWork",
        "conformsTo": {"@id": "https://w3id.org/ro/crate/1.3"},
        "about": {"@id": "./"},
    }
    return {
        "@context": "https://w3id.org/ro/crate/1.3/context",
        "@graph": _flat([descriptor, root, *people, *files, *origins, *fetches]),
    }


def _flat(graph: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`graph` with each nested object, such as an author's affiliation, lifted into an entity of its own.

    RO-Crate refuses a graph that is not flat. A lifted object keeps its @id, or gets `#<key>-<n>`,
    and leaves an `{"@id": ...}` reference behind; one @id is one entity, the first one met.
    """
    ids = {entity["@id"] for entity in graph}
    lifted = 0

    def lift(key: str, value: object) -> object:
        nonlocal lifted
        if isinstance(value, list):
            return [lift(key, item) for item in value]
        # {"@value": ...} is a JSON-LD literal, not an entity.
        if not isinstance(value, dict) or value.keys() <= {"@id"} or "@value" in value:  # noqa: PLR2004
            return value
        lifted += 1
        entity = {**value, "@id": value.get("@id") or f"#{key}-{lifted}"}
        if entity["@id"] not in ids:
            ids.add(entity["@id"])
            graph.append(entity)
        return {"@id": entity["@id"]}

    for entity in graph:  # grows as objects are lifted, so theirs are lifted too
        entity.update({key: lift(key, value) for key, value in entity.items()})
    return graph


# Croissant's JSON-LD context, as mlcroissant writes it.
_CROISSANT_CONTEXT = {
    "@language": "en",
    "@vocab": "https://schema.org/",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "cr": "http://mlcommons.org/croissant/",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "dct": "http://purl.org/dc/terms/",
    "examples": {"@id": "cr:examples", "@type": "@json"},
    "extract": "cr:extract",
    "field": "cr:field",
    "fileProperty": "cr:fileProperty",
    "fileObject": "cr:fileObject",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "includes": "cr:includes",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "parentField": "cr:parentField",
    "path": "cr:path",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "repeated": "cr:repeated",
    "replace": "cr:replace",
    "sc": "https://schema.org/",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
}


# mlcroissant 1.1 reads Parquet only under this older type, not IANA's application/vnd.apache.parquet.
_CROISSANT_TYPES = {"application/vnd.apache.parquet": "application/x-parquet"}


def _croissant(described: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    """A Croissant 1.1 description built from the package: a FileObject per output, a RecordSet per table."""
    document: dict[str, Any] = {
        "@context": _CROISSANT_CONTEXT,
        "@type": "sc:Dataset",
        "conformsTo": "http://mlcommons.org/croissant/1.1",
    }
    document.update(described)
    if authors := document.pop("author", None):
        document["creator"] = _people(authors)[1]
    resources = package["resources"]
    document["distribution"] = [
        {
            "@type": "cr:FileObject",
            "@id": r["path"],
            "name": Path(r["path"]).name,
            "contentUrl": r["path"],
            "encodingFormat": _CROISSANT_TYPES.get(_media_type(r), _media_type(r)),
            "sha256": _hex(r),
        }
        for r in resources
    ]
    document["recordSet"] = [
        {
            "@type": "cr:RecordSet",
            "@id": r["name"],
            "name": r["name"],
            "field": [
                {
                    "@type": "cr:Field",
                    "@id": f"{r['name']}/{quote_plus(field['name'])}",
                    "name": field["name"],
                    "dataType": "sc:Text",
                    "source": {"fileObject": {"@id": r["path"]}, "extract": {"column": field["name"]}},
                }
                for field in r["schema"]["fields"]
            ],
        }
        for r in resources
        if r.get("schema")
    ]
    return document
